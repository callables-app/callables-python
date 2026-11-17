import importlib
import os
import sys
from pathlib import Path
from typing import Any

from callables import connections as connection_registry
from callables.decorator import registry

SKIP_DIRS = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", "tests", "test", ".callables_sdk", "build", "dist"}
MANIFEST_VERSION = 1


def discover_modules(root: Path, base: Path | None = None) -> list[str]:
    """Dotted names of every module under root that mentions callables, so they can be imported. Names are relative
    to `base` (default: root), the folder on sys.path."""
    base = base or root
    modules = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for filename in sorted(filenames):
            if not filename.endswith(".py"):
                continue
            path = Path(dirpath) / filename
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if "callables" not in text or ("deploy" not in text and "command" not in text):
                continue
            rel = path.relative_to(base).with_suffix("")
            parts = list(rel.parts)
            if parts[-1] == "__init__":
                parts = parts[:-1]
            if parts:
                modules.append(".".join(parts))
    return modules


def _outside_includes(root: Path, base: Path, folders: list[Path]) -> list[dict[str, str]]:
    """Locally the whole repo is importable, but the platform builds only the folder and its includes: report any
    module loaded from elsewhere in the repo, which would fail to import there."""
    errors = []
    for name, module in list(sys.modules.items()):
        file = getattr(module, "__file__", None)
        if not file:
            continue
        path = Path(file).resolve()
        if not path.is_relative_to(base) or any(path.is_relative_to(f) for f in folders):
            continue
        if any(part in SKIP_DIRS or part.startswith(".") for part in path.relative_to(base).parts[:-1]):
            continue  # a virtualenv or tooling inside the repo, not project code
        folder = path.parent.relative_to(base).as_posix()
        if folder == ".":
            errors.append({"module": name, "error": f"imported from {path.name} at the top of the repo, which isn't "
                           "in the build. Move it into a folder and add that folder to include"})
            continue
        errors.append({"module": name, "error": f"imported from {folder}/, which isn't in the build. Add \"{folder}\" "
                       f"to include in {root.relative_to(base).as_posix()}/callables.toml"})
    return errors


def build_manifest(root: Path, include_local: bool = False) -> dict[str, Any]:
    """What the platform deploys. `include_local` adds the @command functions, for the CLI and `call check`."""
    from callables import __version__
    from callables.project import ConfigError, layout

    root = root.resolve()
    errors = []
    try:
        base, folders = layout(root)
    except ConfigError as exc:
        base, folders = root, [root]
        errors.append({"module": "include", "error": str(exc)})
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))
    for module in dict.fromkeys(m for folder in folders for m in discover_modules(folder, base)):
        try:
            importlib.import_module(module)
        except Exception as exc:  # report every broken module, not just the first
            errors.append({"module": module, "error": f"{type(exc).__name__}: {exc}"})
    if base != root:
        errors.extend(_outside_includes(root, base, folders))

    functions = []
    for fn in sorted(registry.values(), key=lambda f: f.name):
        if fn.local_only and not include_local:
            continue
        try:
            functions.append(fn.manifest_entry())
        except Exception as exc:
            errors.append({"function": fn.name, "error": f"{type(exc).__name__}: {exc}"})

    # Two different Group(...) objects with one name would make one section with two descriptions
    groups: dict[str, Any] = {}
    for fn in sorted(registry.values(), key=lambda f: f.name):
        if fn.group is None:
            continue
        first = groups.setdefault(fn.group.name, fn.group)
        settings = lambda g: (g.manifest_entry(), g.secrets, [c.name for c in g.connections])  # noqa: E731
        if first is not fn.group and settings(first) != settings(fn.group):
            errors.append({"function": fn.name, "error": f"Group '{fn.group.name}' is defined twice with different "
                           "settings. Define it once and import it where you need it."})

    for name in dict.fromkeys(connection_registry.conflicts):
        errors.append({"module": f"Connection '{name}'", "error": "defined twice with different settings. Define it "
                       "once and import it where you need it."})
    # Only the connections a deployed function uses; the platform keys them by name within the project
    used: dict[str, Any] = {}
    for fn in registry.values():
        if include_local or not fn.local_only:
            for conn in fn.all_connections:
                used[conn.name] = conn
    connections = [used[name].manifest_entry() for name in sorted(used)]

    return {
        "manifest_version": MANIFEST_VERSION,
        "sdk_version": __version__,
        "functions": functions,
        "connections": connections,
        "errors": errors,
    }
