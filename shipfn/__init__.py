"""The old name of the callables package. `from shipfn import deploy` and `import shipfn.files` keep working, so
repos written before the rename still deploy. New code imports callables."""
import importlib
import sys

from callables import *  # noqa: F401,F403
from callables import __all__, __version__  # noqa: F401

for _name in ("checks", "cli", "commands", "decorator", "files", "init", "manifest", "project", "runtime", "schema"):
    sys.modules[f"{__name__}.{_name}"] = globals()[_name] = importlib.import_module(f"callables.{_name}")
