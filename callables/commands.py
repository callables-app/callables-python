"""`call <group> <action> --flag value`: run a deployed function like a Typer command.

The flags come from the same JSON Schema the web form is built from, and the values go through the same
validation as a run on the platform, so the terminal and the form can't disagree.
"""
import argparse
import json
from typing import Any

from callables.files import FILE_FORMAT


def _resolve(prop: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    """The non-null branch of `X | None`, with $refs (enums, models) inlined."""
    if "$ref" in prop:
        prop = {**defs.get(prop["$ref"].rsplit("/", 1)[-1], {}), **{k: v for k, v in prop.items() if k != "$ref"}}
    branches = [b for b in prop.get("anyOf", []) if b.get("type") != "null"]
    if len(branches) == 1:
        merged = {**prop, **_resolve(branches[0], defs)}
        merged.pop("anyOf", None)
        return merged
    return prop


def _kind(prop: dict[str, Any]) -> str:
    if "enum" in prop:
        return "choice"
    if prop.get("format") == FILE_FORMAT:
        return "file"
    if "properties" in prop or prop.get("type") == "object":
        return "json"
    return prop.get("type") or "json"


def _coerce(prop: dict[str, Any], raw: str) -> Any:
    """Strings stay strings; everything else is read as JSON when it parses (numbers, models, enum values)."""
    if _kind(prop) in ("string", "file") or (_kind(prop) == "choice" and all(isinstance(v, str) for v in prop["enum"])):
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def build_parser(prog: str, fn: dict[str, Any]) -> tuple[argparse.ArgumentParser, dict[str, dict[str, Any]]]:
    schema = fn["input_schema"]
    defs = schema.get("$defs", {})
    required = set(schema.get("required", []))
    description = fn["title"] + (f": {fn['description']}" if fn.get("description") else "")
    parser = argparse.ArgumentParser(prog=prog, description=description)
    props = {}
    for name in schema.get("x-order") or list(schema.get("properties", {})):
        prop = props[name] = _resolve(schema["properties"][name], defs)
        kind = _kind(prop)
        flag = "--" + name.replace("_", "-")
        help_text = prop.get("description") or prop.get("title") or name
        if "default" in prop:
            help_text += f" (default: {json.dumps(prop['default'])})"
        # Unset flags are left out, so the function's own defaults apply
        options: dict[str, Any] = {"dest": name, "help": help_text, "default": argparse.SUPPRESS,
                                   "required": name in required}
        if kind == "boolean":
            parser.add_argument(flag, action=argparse.BooleanOptionalAction, **options)
            continue
        if kind == "array":
            options.update(nargs="+", metavar=_kind(_resolve(prop.get("items", {}), defs)).upper())
        elif kind == "choice":
            options.update(choices=[str(v) for v in prop["enum"]], metavar="{" + ",".join(map(str, prop["enum"])) + "}")
        else:
            options["metavar"] = {"file": "PATH", "json": "JSON"}.get(kind, kind.upper())
        parser.add_argument(flag, **options)
    return parser, props


def parse_inputs(prog: str, fn: dict[str, Any], argv: list[str]) -> dict[str, Any]:
    parser, props = build_parser(prog, fn)
    values = vars(parser.parse_args(argv))
    inputs = {}
    for name, raw in values.items():
        prop = props[name]
        if isinstance(raw, bool):
            inputs[name] = raw
        elif isinstance(raw, list):
            items = _resolve(prop.get("items", {}), fn["input_schema"].get("$defs", {}))
            inputs[name] = [_coerce(items, r) for r in raw]
        else:
            inputs[name] = _coerce(prop, raw)
    return inputs


def _local(fn: dict[str, Any]) -> str:
    return "  (local only)" if fn.get("local_only") else ""


def command_tree(functions: list[dict[str, Any]]) -> str:
    """What `call <group> <action>` can run, for help and "not found" messages."""
    lines, grouped = [], {}
    for fn in functions:
        if fn.get("group"):
            grouped.setdefault(fn["group"]["name"], (fn["group"], []))[1].append(fn)
        else:
            lines.append(f"  {fn['name']:<24} {fn['title']}{_local(fn)}")
    for name, (group, fns) in sorted(grouped.items()):
        lines.append(f"  {name:<24} {group['title']}")
        lines += [f"    {fn['action']:<22} {fn['title']}{_local(fn)}" for fn in fns]
    return "\n".join(lines) or "  (no @deploy functions found)"


def find(functions: list[dict[str, Any]], argv: list[str]) -> tuple[dict[str, Any] | None, list[str], str]:
    """Matches `group action ...` or `function ...`; returns the function, the remaining args and the prog name."""
    head, rest = argv[0], argv[1:]
    in_group = [fn for fn in functions if fn.get("group") and fn["group"]["name"] == head]
    if in_group:
        if not rest or rest[0].startswith("-"):
            return None, rest, f"call {head}"
        match = next((fn for fn in in_group if fn["action"] == rest[0]), None)
        if match is None:
            return None, rest, f"call {head}"
        return match, rest[1:], f"call {head} {rest[0]}"
    match = next((fn for fn in functions if not fn.get("group") and fn["name"] == head), None)
    return match, rest, f"call {head}"
