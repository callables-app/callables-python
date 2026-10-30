import importlib
import os
import sys
from pathlib import Path
from typing import Any

from callables.decorator import registry

SKIP_DIRS = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", "tests", "test", ".callables_sdk", "build", "dist"}
MANIFEST_VERSION = 1


def discover_modules(root: Path) -> list[str]:
    """Dotted names of every module under root that mentions callables, so they can be imported."""
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
            rel = path.relative_to(root).with_suffix("")
            parts = list(rel.parts)
            if parts[-1] == "__init__":
                parts = parts[:-1]
            if parts:
                modules.append(".".join(parts))
    return modules


def build_manifest(root: Path, include_local: bool = False) -> dict[str, Any]:
    """What the platform deploys. `include_local` adds the @command functions, for the CLI and `call check`."""
    from callables import __version__

    root = root.resolve()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    errors = []
    for module in discover_modules(root):
        try:
            importlib.import_module(module)
        except Exception as exc:  # report every broken module, not just the first
            errors.append({"module": module, "error": f"{type(exc).__name__}: {exc}"})

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
        if first is not fn.group and (first.manifest_entry(), first.secrets) != (fn.group.manifest_entry(), fn.group.secrets):
            errors.append({"function": fn.name, "error": f"Group '{fn.group.name}' is defined twice with different "
                           "settings. Define it once and import it where you need it."})

    return {
        "manifest_version": MANIFEST_VERSION,
        "sdk_version": __version__,
        "functions": functions,
        "errors": errors,
    }
