"""`call manifest` and `call run` are what the platform calls inside the image; the rest are for builders
(and their coding agents: `--json` gives one parseable object on stdout)."""
import argparse
import contextlib
import json
import sys
import traceback
from pathlib import Path

ROOT_HELP = "Folder to build from (default: this repo, or the root set in callables.toml / [tool.callables])"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="call", description="Deploy Python functions as forms for your team.",
        epilog="Run your own functions like commands: call <group> <action> --flag value (or call <function> ...). "
               "`call <group> --help` lists a group's actions; `call check` lists everything.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="Find the problems a deploy would hit, with fixes (exit 1 on errors)")
    p_check.add_argument("--root", help=ROOT_HELP)
    p_check.add_argument("--json", action="store_true", help="Print one JSON object instead of text")

    p_local = sub.add_parser("local", help="Run a function by name on this machine")
    p_local.add_argument("name")
    p_local.add_argument("--root", help=ROOT_HELP)
    p_local.add_argument("--input", "-i", action="append", default=[], metavar="KEY=VALUE",
                         help="An input value; JSON is parsed when it can be (-i days=7 -i region=NA), "
                              "and @path passes a file (-i sheet=@data.csv)")
    p_local.add_argument("--json", action="store_true",
                         help="Print one JSON object on stdout (the function's own output goes to stderr)")

    p_manifest = sub.add_parser("manifest", help="Print the JSON manifest of every @deploy function")
    p_manifest.add_argument("--root", help=ROOT_HELP)

    sub.add_parser("docs", help="Print the guide to writing automations (for people and coding agents)")

    p_init = sub.add_parser("init", help="Add an example automation and coding-agent instructions to this repo")
    p_init.add_argument("--root", default=".")
    p_init.add_argument("--no-example", action="store_true", help="Only write the agent instructions")
    p_init.add_argument("--no-agents", action="store_true", help="Skip the agent instructions")

    p_run = sub.add_parser("run", help="(Used by the platform) run one function with inputs from $SHIPFN_INPUTS")
    p_run.add_argument("entrypoint", help="module:function")
    p_run.add_argument("--root", default=".")

    argv = sys.argv[1:] if argv is None else argv
    if argv and not argv[0].startswith("-") and argv[0] not in sub.choices:
        return _command(argv)  # call <group> <action> --flags, or call <function> --flags

    args = parser.parse_args(argv)

    if args.command == "docs":
        from callables.init import guide

        print(guide())
        return 0

    if args.root is None:  # same folder the platform would build
        from callables.project import ConfigError, build_root

        try:
            root, source = build_root(Path.cwd())
        except ConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if source and args.command != "manifest":
            print(f"Using {root.relative_to(Path.cwd().resolve())}/ (root in {source})", file=sys.stderr)
    else:
        root = Path(args.root).resolve()

    if args.command == "manifest":
        from callables.manifest import build_manifest

        print(json.dumps(build_manifest(root)))
        return 0

    if args.command == "run":
        from callables.runtime import run

        return run(args.entrypoint, root)

    if args.command == "check":
        return _check(root, args.json)

    if args.command == "local":
        return _local(root, args.name, _parse_inputs(args.input), args.json)

    if args.command == "init":
        from callables.init import init_project

        for path, what in init_project(root, example=not args.no_example, agents=not args.no_agents):
            print(f"  {what:<10} {path}")
        print("\nNext: `call check`, then `call local <name>` to try a function. Push to deploy.")
        return 0

    return 1


def _check(root: Path, as_json: bool) -> int:
    from callables.checks import check_project

    # Imports run user code; keep its prints off stdout so --json stays parseable
    with contextlib.redirect_stdout(sys.stderr):
        report = check_project(root)
    if as_json:
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1

    for fn in report["functions"]:
        local = "  (local only)" if fn["local_only"] else ""
        print(f"  {fn['name']}  ({', '.join(fn['inputs']) or 'no inputs'}) -> {fn['output_kind']}{local}")
    if report["functions"]:
        print()
    for issue in report["issues"]:
        where = f"[{issue['function']}] " if issue.get("function") else ""
        print(f"{issue['level']}: {where}{issue['message']}")
        if issue.get("fix"):
            print(f"  fix: {issue['fix']}")
    errors = sum(i["level"] == "error" for i in report["issues"])
    warnings = len(report["issues"]) - errors
    if errors:
        print(f"\n{errors} error(s), {warnings} warning(s). A deploy would fail.")
    else:
        print(f"{'' if not report['issues'] else chr(10)}Ready to deploy"
              f"{f' ({warnings} warning(s))' if warnings else ''}.")
    return 0 if report["ok"] else 1


def _parse_inputs(pairs: list[str]) -> dict:
    inputs = {}
    for pair in pairs:
        key, _, raw = pair.partition("=")
        if raw.startswith("@"):  # -i sheet=@data.csv passes a file
            inputs[key] = raw[1:]
            continue
        try:
            inputs[key] = json.loads(raw)
        except json.JSONDecodeError:
            inputs[key] = raw
    return inputs


def _field_errors(exc) -> dict[str, str]:
    return {".".join(str(p) for p in err["loc"]) or "inputs": err["msg"] for err in exc.errors()}


def _command(argv: list[str]) -> int:
    from callables.commands import command_tree, find, parse_inputs
    from callables.manifest import build_manifest
    from callables.project import ConfigError, build_root

    try:
        root, _ = build_root(Path.cwd())
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    with contextlib.redirect_stdout(sys.stderr):  # import-time prints shouldn't mix with the result
        manifest = build_manifest(root, include_local=True)
    for err in manifest["errors"]:
        print(f"warning: {err.get('module') or err.get('function')}: {err['error']}", file=sys.stderr)
    fn, rest, prog = find(manifest["functions"], argv)
    if fn is None:
        group = [f for f in manifest["functions"] if (f.get("group") or {}).get("name") == argv[0]]
        if group:
            asked = not rest or rest[0] in ("-h", "--help")
            if not asked:
                print(f"{prog}: no action named '{rest[0]}'", file=sys.stderr)
            print(f"usage: {prog} <action> [--flags]\n\n{group[0]['group']['title']}", file=sys.stdout if asked else sys.stderr)
            print(command_tree(group).split("\n", 1)[1], file=sys.stdout if asked else sys.stderr)
            return 0 if asked else 2
        print(f"call: '{argv[0]}' isn't a command, group or function. You can run:\n"
              f"{command_tree(manifest['functions'])}\n\nBuilt-in commands: call --help", file=sys.stderr)
        return 2
    return _local(root, fn["name"], parse_inputs(prog, fn, rest), as_json=False, follow=True)


def _rows(result) -> list[dict] | None:
    if isinstance(result, dict):
        return [result]
    if isinstance(result, list) and result and all(isinstance(r, dict) for r in result):
        return result
    return None


CELL_WIDTH = 40


def _cell(value) -> str:
    text = "" if value is None else value if isinstance(value, str) else json.dumps(value, default=str)
    text = text.replace("\n", " ")
    return text if len(text) <= CELL_WIDTH else text[:CELL_WIDTH - 1] + "…"


def _table(rows: list[dict]) -> str:
    """Rows as aligned columns, for a person at a terminal (pipes and --json get JSON)."""
    columns = list(dict.fromkeys(k for row in rows for k in row))
    cells = [[_cell(row.get(c)) for c in columns] for row in rows]
    numeric = [all(isinstance(row.get(c), (int, float)) or row.get(c) is None for row in rows) for c in columns]
    widths = [max(len(c), *(len(r[i]) for r in cells)) for i, c in enumerate(columns)]

    def line(values: list[str]) -> str:
        return "  ".join(v.rjust(w) if num else v.ljust(w) for v, w, num in zip(values, widths, numeric)).rstrip()

    return "\n".join([line(columns), line(["-" * w for w in widths]), *(line(r) for r in cells),
                      f"\n{len(rows)} row{'s' if len(rows) != 1 else ''}"])


def _record(record: dict) -> str:
    width = max(len(k) for k in record)
    return "\n".join(f"{k.ljust(width)}  {_cell(v) if not isinstance(v, str) else v}" for k, v in record.items())


def _ask(prompt: str) -> str | None:
    try:
        return input(prompt).strip()
    except EOFError:  # Ctrl-D ends the flow
        print(file=sys.stderr)
        return None


def _pick(prompt: str, count: int) -> int | None:
    """1-based choice; Enter (or Ctrl-D) stops."""
    while True:
        raw = _ask(f"{prompt} [1-{count}, Enter to stop]: ")
        if not raw:
            return None
        if raw.isdigit() and 1 <= int(raw) <= count:
            return int(raw) - 1
        print(f"Type a number from 1 to {count}.", file=sys.stderr)


def _follow(root: Path, fn: dict, functions: list[dict], rows: list[dict], files: list[dict]) -> int:
    """The terminal side of `next`: pick a row (if the result has rows), pick what to do next, fill in what's left,
    run it. A file the function returned goes to the next action's file input."""
    from callables.commands import _coerce, _resolve

    row: dict | None = {} if not rows else rows[0] if len(rows) == 1 else None
    if row is None:
        index = _pick("Pick a row", len(rows))
        if index is None:
            return 0
        row = rows[index]
    for i, option in enumerate(fn["next"], 1):
        print(f"  {i}. {option['label']}")
    choice = _pick("What next?", len(fn["next"]))
    if choice is None:
        return 0
    option = fn["next"][choice]
    target = next(f for f in functions if f["name"] == option["action"])
    inputs = {name: row[field] for name, field in option["inputs"].items() if field in row}
    returned = next((f["local_path"] for f in files if f.get("returned")), None)
    if option.get("file_input") and returned:
        inputs[option["file_input"]] = returned
    schema = target["input_schema"]
    for name in schema.get("x-order", []):
        if name in inputs:
            continue
        prop = _resolve(schema["properties"][name], schema.get("$defs", {}))
        hint = f" [{'/'.join(map(str, prop['enum']))}]" if "enum" in prop else ""
        hint += f" (default {prop['default']})" if "default" in prop else ""
        while True:
            raw = _ask(f"{name}{hint}: ")
            if raw is None:
                return 0
            if raw:
                inputs[name] = _coerce(prop, raw)
                break
            if name not in schema.get("required", []):
                break  # keep the default
            print(f"{name} is required.", file=sys.stderr)
    return _local(root, target["name"], inputs, as_json=False, follow=True)


def _local(root: Path, name: str, inputs: dict, as_json: bool, follow: bool = False) -> int:
    from pydantic import ValidationError

    from callables.decorator import registry
    from callables.files import output_dir, saved_files
    from callables.manifest import build_manifest
    from callables.runtime import finish

    stdout = sys.stdout  # the real one, even while user code's prints are redirected

    def report(payload: dict) -> int:
        if as_json:
            print(json.dumps(payload, indent=2, default=str), file=stdout)
        return 0 if payload["ok"] else 1

    # In --json mode everything the user's code prints goes to stderr
    out = contextlib.redirect_stdout(sys.stderr) if as_json else contextlib.nullcontext()
    with out:
        manifest = build_manifest(root, include_local=True)
        for err in manifest["errors"]:
            print(f"warning: {err.get('module') or err.get('function')}: {err['error']}", file=sys.stderr)
        if name not in registry:
            message = f"No function named '{name}'. Found: {', '.join(sorted(registry)) or 'none'}"
            if not as_json:
                print(message, file=sys.stderr)
            return report({"ok": False, "error": message, "import_errors": manifest["errors"]})
        try:
            result = finish(registry[name].call(inputs))
        except ValidationError as exc:
            fields = _field_errors(exc)
            if not as_json:
                print("Some inputs aren't valid:", file=sys.stderr)
                for field, msg in fields.items():
                    print(f"  {field}: {msg}", file=sys.stderr)
            return report({"ok": False, "error": "Some inputs aren't valid", "field_errors": fields})
        except Exception as exc:
            traceback.print_exc()
            return report({"ok": False, "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc(),
                           "files": saved_files()})
        files = [{**f, "local_path": str(output_dir() / f["path"])} for f in saved_files()]

    if as_json:
        return report({"ok": True, "value": result, "files": files})
    entry = next((f for f in manifest["functions"] if f["name"] == name), {})
    rows = _rows(result) or []
    # `next` turns a result into a menu, but only for a person at a terminal; scripts get plain output
    interactive = follow and entry.get("next") and sys.stdin.isatty() and sys.stdout.isatty()
    if interactive and len(rows) > 1:
        for i, row in enumerate(rows, 1):
            print(f"  {i}. " + "   ".join(f"{k}={v}" for k, v in list(row.items())[:5]))
    elif rows and sys.stdout.isatty():
        print(_table(rows) if isinstance(result, list) else _record(result))
    elif result is not None:
        print(result if isinstance(result, str) else json.dumps(result, indent=2))
    if files:
        print("\nFiles:", file=sys.stderr)
        for f in files:
            print(f"  {f['local_path']}  ({f['size']:,} bytes)", file=sys.stderr)
    if interactive:
        return _follow(root, entry, manifest["functions"], rows, files)
    return 0


if __name__ == "__main__":
    sys.exit(main())
