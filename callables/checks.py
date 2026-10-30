"""`call check`: the problems a deploy would hit, found on this machine before pushing.

Errors match what makes the platform reject a deploy; warnings match what it would ignore or cap.
"""
import inspect
import re
from pathlib import Path
from typing import Any

from callables.decorator import registry
from callables.manifest import build_manifest
from callables.schema import file_fields

MAX_TIMEOUT = 900  # the platform caps timeouts here
SECRET_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
CRON_FIELD = re.compile(r"^[\d*/,\-A-Za-z?]+$")


def _issue(level: str, message: str, function: str | None = None, fix: str | None = None) -> dict[str, Any]:
    issue: dict[str, Any] = {"level": level, "message": message}
    if function:
        issue["function"] = function
    if fix:
        issue["fix"] = fix
    return issue


def _check_function(fn: dict[str, Any]) -> list[dict[str, Any]]:
    name, issues = fn["name"], []
    schema = fn["input_schema"]
    deployed = registry.get(name)

    if not fn.get("description") and not fn.get("local_only"):
        issues.append(_issue("warning", "No description; people see a blank card", name,
                             "Add a docstring: its first paragraph becomes the description"))
    if deployed is not None:
        untyped = [p for p in inspect.signature(deployed.func).parameters if p not in deployed.func.__annotations__]
        for param in untyped:
            issues.append(_issue("warning", f"Parameter '{param}' has no type hint, so it's a plain text field", name,
                                 f"Annotate it, e.g. `{param}: int` or `{param}: Literal['a', 'b']`"))
        if "return" not in deployed.func.__annotations__:
            issues.append(_issue("warning", "No return type, so the result is shown as raw JSON", name,
                                 "Annotate the return: -> str (Markdown), -> list[dict] (table), -> dict (key-value), "
                                 "-> File (download) or -> None"))

    if fn["timeout"] > MAX_TIMEOUT:
        issues.append(_issue("warning", f"timeout={fn['timeout']} is above the {MAX_TIMEOUT}s limit and will be capped",
                             name, f"Use timeout={MAX_TIMEOUT} or less"))
    elif fn["timeout"] <= 0:
        issues.append(_issue("error", "timeout must be a positive number of seconds", name))

    for secret in fn.get("secrets") or []:
        if not SECRET_NAME.match(secret):
            issues.append(_issue("error", f"Secret name '{secret}' isn't a valid environment variable name", name,
                                 "Use letters, digits and underscores, e.g. SALES_API_TOKEN"))

    schedule = fn.get("schedule")
    if schedule:
        fields = schedule.split()
        if len(fields) != 5 or not all(CRON_FIELD.match(f) for f in fields):
            issues.append(_issue("warning", f"schedule '{schedule}' isn't a 5-field cron expression and will be ignored",
                                 name, "e.g. schedule='0 9 * * MON' (minute hour day month weekday, UTC)"))
        if schema.get("required"):
            issues.append(_issue("warning", "Scheduled functions need a default for every input; the schedule will "
                                 f"be ignored (no default: {', '.join(schema['required'])})", name))
        if file_fields(schema):
            issues.append(_issue("warning", "Functions with file inputs can't run on a schedule", name,
                                 "Drop the schedule, or read the file from storage instead of an upload"))
    return issues


def _check_next(fn: dict[str, Any], by_name: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """A next action that needs a file this function doesn't hand it: people will have to pick the file themselves."""
    issues = []
    for option in fn.get("next") or []:
        target = by_name.get(option["action"])
        needs = [f for f in file_fields(target["input_schema"]) if f in target["input_schema"].get("required", [])] \
            if target else []
        if needs and not option.get("file_input"):
            issues.append(_issue("warning", f"next: {option['action']} needs a file ({', '.join(needs)}), and this "
                                 "function doesn't return one, so people will have to pick it themselves", fn["name"],
                                 "Return the file (-> File) to pass it along, and print() any summary instead: it "
                                 "shows in the run's logs"))
    return issues


def check_project(root: Path) -> dict[str, Any]:
    manifest = build_manifest(root, include_local=True)
    issues = []
    for err in manifest["errors"]:
        where = err.get("module") or err.get("function")
        issues.append(_issue("error", f"{where}: {err['error']}", err.get("function"),
                             "Fix this first: if any module fails to import, the platform publishes nothing"))
    if not manifest["functions"] and not manifest["errors"]:
        issues.append(_issue("error", "No @deploy functions found",
                             fix="Decorate a function: `from callables import deploy` then `@deploy` above `def ...`"))
    elif manifest["functions"] and all(f.get("local_only") for f in manifest["functions"]):
        issues.append(_issue("warning", "Every function is a local @command, so a deploy publishes nothing",
                             fix="Use @deploy for anything teammates should run"))
    by_name = {f["name"]: f for f in manifest["functions"]}
    for fn in manifest["functions"]:
        issues.extend(_check_function(fn))
        issues.extend(_check_next(fn, by_name))
    if not (Path(root) / "requirements.txt").is_file() and not (Path(root) / "pyproject.toml").is_file():
        issues.append(_issue("warning", "No requirements.txt, so only the standard library is installed",
                             fix="List your dependencies (not callables itself) in requirements.txt"))
    return {
        "ok": not any(i["level"] == "error" for i in issues),
        "functions": [
            {"name": f["name"], "title": f["title"], "entrypoint": f["entrypoint"],
             "inputs": f["input_schema"].get("x-order", []), "output_kind": f["output_kind"],
             "local_only": bool(f.get("local_only"))}
            for f in manifest["functions"]
        ],
        "issues": issues,
    }
