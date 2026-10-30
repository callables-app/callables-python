"""Files in and out of a run: `File` parameters become upload fields, returned or saved files become downloads."""
import mimetypes
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from pydantic import Field, GetCoreSchemaHandler, GetJsonSchemaHandler
from pydantic_core import core_schema

OUTPUT_DIR_ENV = "CALLABLES_OUTPUT_DIR"
LOCAL_OUTPUT_DIR = "callables-output"  # where `call local` puts files
FILE_FORMAT = "callables-file"

_saved: list["File"] = []
_returned: set[Path] = set()  # saved copies of files the function returned, not ones it saved alongside


class File:
    """A file a person uploaded (as a parameter) or one your function hands back (as the return value).

    As a parameter, `sheet: File` (or `Annotated[File, accept(".csv")]`) becomes an upload field and the
    function receives the uploaded file. Returning `File(path, name=...)` (or a list of them) gives the
    person download buttons.
    """

    def __init__(self, path: str | os.PathLike, name: str | None = None, content_type: str | None = None):
        """`name` is what the person sees and downloads as (defaults to the file's own name);
        `content_type` is guessed from the name when not given."""
        self.path = Path(path)
        self.name = safe_name(name or self.path.name)
        self.content_type = content_type or mimetypes.guess_type(self.name)[0]

    @property
    def size(self) -> int:
        """Size in bytes."""
        return self.path.stat().st_size

    def read_bytes(self) -> bytes:
        return self.path.read_bytes()

    def read_text(self, encoding: str = "utf-8") -> str:
        return self.path.read_text(encoding=encoding)

    def open(self, mode: str = "rb", **kwargs):
        """Open the file, e.g. `with sheet.open("r", newline="") as f: csv.reader(f)`."""
        return self.path.open(mode, **kwargs)

    def __repr__(self) -> str:
        return f"File({self.name!r}, path={str(self.path)!r})"

    @classmethod
    def _validate(cls, value: Any) -> "File":
        if isinstance(value, File):
            file = value
        elif isinstance(value, dict) and value.get("path"):
            file = File(value["path"], value.get("name"), value.get("content_type"))
        elif isinstance(value, (str, os.PathLike)) and str(value):
            file = File(value)
        else:
            raise ValueError("Expected an uploaded file")
        if not file.path.is_file():
            raise ValueError(f"File not found: {file.path}")
        return file

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: GetCoreSchemaHandler) -> core_schema.CoreSchema:
        return core_schema.no_info_plain_validator_function(
            cls._validate,
            serialization=core_schema.plain_serializer_function_ser_schema(lambda f: f.name),
        )

    @classmethod
    def __get_pydantic_json_schema__(cls, schema: core_schema.CoreSchema, handler: GetJsonSchemaHandler) -> dict:
        return {"type": "string", "format": FILE_FORMAT}


def accept(*extensions: str) -> Any:
    """Limit an upload field's file picker: `sheet: Annotated[File, accept(".csv", ".xlsx")]`."""
    return Field(json_schema_extra={"x-accept": ",".join(extensions)})


def safe_name(name: str) -> str:
    """A file name with any directories and leading dots removed."""
    name = Path(str(name).replace("\\", "/")).name.strip().lstrip(".")
    return name[:200] or "file"


def output_dir() -> Path:
    """Where this run's downloads are kept: a platform folder when deployed, ./callables-output locally."""
    path = Path(os.environ.get(OUTPUT_DIR_ENV) or LOCAL_OUTPUT_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_file(source: str | os.PathLike | bytes | File, name: str | None = None,
              content_type: str | None = None) -> File:
    """Offer a file for download from this run (alongside whatever the function returns).

    `source` is a path, a File, or the file's bytes (then `name` is required):
        save_file(b"3 rows", name="summary.txt")
        save_file("/tmp/report.pdf", name="Weekly report.pdf")
    Returns the saved copy. Limits: 20 files and 100 MB per run; files are deleted after 24 hours.
    """
    if isinstance(source, File):
        name, content_type, source = name or source.name, content_type or source.content_type, source.path
    if isinstance(source, bytes):
        if not name:
            raise ValueError("save_file(bytes) needs a name, e.g. save_file(data, name='report.csv')")
    else:
        source = Path(source)
        if not source.is_file():
            raise FileNotFoundError(f"save_file: {source} doesn't exist")
    dest = output_dir() / uuid.uuid4().hex[:8] / safe_name(name or source.name)
    dest.parent.mkdir(parents=True)
    if isinstance(source, bytes):
        dest.write_bytes(source)
    else:
        shutil.copyfile(source, dest)
    saved = File(dest, dest.name, content_type)
    _saved.append(saved)
    return saved


def keep_returned(file: "File") -> "File":
    """Save a file the function returned. The platform can hand it to a next action's file input."""
    saved = save_file(file)
    _returned.add(saved.path)
    return saved


def saved_files() -> list[dict[str, Any]]:
    """What this run produced, for the platform: paths are relative to the output directory."""
    if not _saved:
        return []
    root = output_dir().resolve()
    return [
        {"name": f.name, "path": str(f.path.resolve().relative_to(root)), "size": f.size,
         "content_type": f.content_type, **({"returned": True} if f.path in _returned else {})}
        for f in _saved if f.path.is_file()
    ]
