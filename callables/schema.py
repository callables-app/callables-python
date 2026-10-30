import functools
import inspect
import typing
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, TypeAdapter, create_model

from callables.files import FILE_FORMAT, File

SUPPORTED_TYPES = ("str, int, float, bool, date, datetime, Literal[...], an Enum, list[...] of those, "
                   "X | None, a Pydantic model, or File")


@functools.cache
def input_model(func: Callable[..., Any]) -> type[BaseModel]:
    """A Pydantic model whose fields are the function's parameters; its JSON Schema drives the form."""
    hints = typing.get_type_hints(func, include_extras=True)
    fields: dict[str, Any] = {}
    for param in inspect.signature(func).parameters.values():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            raise TypeError(f"{func.__name__}: *args and **kwargs can't become form fields")
        annotation = hints.get(param.name, str)
        default = ... if param.default is inspect.Parameter.empty else param.default
        fields[param.name] = (annotation, default)
    try:
        model = create_model(f"{func.__name__}_inputs", __config__=ConfigDict(extra="forbid"), **fields)
        model.model_json_schema()
    except Exception as exc:
        _raise_unsupported(func, fields, exc)
    return model


def _raise_unsupported(func: Callable[..., Any], fields: dict[str, Any], exc: Exception) -> typing.NoReturn:
    """Name the parameter that can't become a form field, instead of Pydantic's internals."""
    for param, (annotation, _) in fields.items():
        try:
            TypeAdapter(annotation).json_schema()
        except Exception:
            shown = getattr(annotation, "__name__", None) or repr(annotation)
            raise TypeError(f"{func.__name__}: parameter '{param}: {shown}' can't become a form field. "
                            f"Use {SUPPORTED_TYPES}.") from exc
    raise exc


def input_schema(func: Callable[..., Any]) -> dict[str, Any]:
    schema = input_model(func).model_json_schema()
    schema["title"] = func.__name__
    schema["x-order"] = list(inspect.signature(func).parameters)
    return schema


def output_kind(func: Callable[..., Any]) -> str:
    """How the launcher renders the return value: markdown, table, kv, file, none or json."""
    ret = typing.get_type_hints(func).get("return", inspect.Signature.empty)
    if ret is inspect.Signature.empty:
        return "json"
    if ret is None or ret is type(None):
        return "none"
    if ret is File or (typing.get_origin(ret) is list and typing.get_args(ret) in ((File,),)):
        return "file"
    if ret is str:
        return "markdown"
    origin = typing.get_origin(ret)
    args = typing.get_args(ret)
    if origin is list and args:
        item = args[0]
        if typing.get_origin(item) is dict or item is dict or _is_model(item):
            return "table"
    if ret is dict or origin is dict or _is_model(ret):
        return "kv"
    if getattr(ret, "__name__", "") == "DataFrame":
        return "table"
    return "json"


def _is_model(tp: Any) -> bool:
    return inspect.isclass(tp) and issubclass(tp, BaseModel)


def file_fields(schema: dict[str, Any]) -> list[str]:
    """The inputs that take an uploaded file (`File`, or `File | None`)."""
    return [
        name for name, prop in (schema.get("properties") or {}).items()
        if any(o.get("format") == FILE_FORMAT for o in [prop, *prop.get("anyOf", [])])
    ]


def returns_one_file(func: Callable[..., Any]) -> bool:
    """`-> File`: the result is a single file, which a next action can be handed."""
    return typing.get_type_hints(func).get("return") is File
