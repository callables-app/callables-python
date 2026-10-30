"""Runs one function inside the sandbox. The platform reads the result from the marker line on stdout."""
import importlib
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter

from callables.files import File, keep_returned, saved_files

RESULT_MARKER = "__SHIPFN_RESULT__"
# Lines the platform shows only to the function's builders (end users never see stack traces)
BUILDER_MARKER = "__SHIPFN_BUILDER__"


def to_jsonable(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "to_dict") and type(value).__name__ == "DataFrame":
        # Not to_json: it turns plain dates into midnight datetimes. Missing values (NaN, NaT) become null
        rows = value.astype(object).where(value.notna(), None).to_dict(orient="records")
        return TypeAdapter(Any).dump_python(rows, mode="json")
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    try:
        return TypeAdapter(Any).dump_python(value, mode="json")
    except Exception:
        return str(value)


def resolve(entrypoint: str):
    module_name, _, qualname = entrypoint.partition(":")
    obj: Any = importlib.import_module(module_name)
    for attr in qualname.split("."):
        obj = getattr(obj, attr)
    deployed = getattr(obj, "__shipfn__", None)
    if deployed is None:
        raise RuntimeError(f"{entrypoint} is not decorated with @deploy")
    return deployed


def emit(payload: dict[str, Any]) -> None:
    sys.stdout.flush()
    sys.stderr.flush()
    print(RESULT_MARKER + json.dumps(payload), flush=True)


def run(entrypoint: str, root: Path) -> int:
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    inputs = json.loads(os.environ.pop("SHIPFN_INPUTS", "{}") or "{}")
    try:
        result = finish(resolve(entrypoint).call(inputs))
    except Exception as exc:
        sys.stdout.flush()
        # On the platform, tag the traceback so only builders see it; locally, print it as-is
        prefix = BUILDER_MARKER if os.environ.get("SHIPFN_RUN_ID") else ""
        for line in traceback.format_exc().rstrip().splitlines():
            print(prefix + line, file=sys.stderr, flush=True)
        emit({"ok": False, "error": f"{type(exc).__name__}: {exc}", "files": saved_files()})
        return 1
    emit({"ok": True, "value": result, "files": saved_files()})
    return 0


def finish(result: Any) -> Any:
    """Returned files become downloads (the value is then empty); anything else becomes JSON."""
    files = result if isinstance(result, list) and result and all(isinstance(f, File) for f in result) else (
        [result] if isinstance(result, File) else None)
    if files is None:
        return to_jsonable(result)
    for f in files:
        keep_returned(f)
    return None
