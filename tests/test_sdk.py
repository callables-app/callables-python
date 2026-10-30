import json
import os
from datetime import date
from enum import Enum
from typing import Literal

import pytest
from pydantic import BaseModel

from callables import deploy, registry
from callables.runtime import RESULT_MARKER, run
from callables.schema import input_schema, output_kind


class Color(str, Enum):
    red = "red"
    blue = "blue"


class Address(BaseModel):
    city: str
    zip: str = ""


def test_schema_maps_types_defaults_and_order():
    def f(name: str, count: int = 3, region: Literal["NA", "EMEA"] = "NA", when: date | None = None,
          color: Color = Color.red, tags: list[str] = [], addr: Address | None = None) -> str:
        return ""

    schema = input_schema(f)
    props = schema["properties"]
    assert schema["x-order"] == ["name", "count", "region", "when", "color", "tags", "addr"]
    assert schema["required"] == ["name"]
    assert props["count"] == {"default": 3, "title": "Count", "type": "integer"}
    assert props["region"]["enum"] == ["NA", "EMEA"]
    assert {"type": "string", "format": "date"} in props["when"]["anyOf"]
    assert props["color"]["$ref"] == "#/$defs/Color"
    assert props["tags"]["items"] == {"type": "string"}
    assert "Address" in schema["$defs"]


@pytest.mark.parametrize("annotation,kind", [
    (str, "markdown"), (None, "none"), (list[dict], "table"), (dict, "kv"), (Address, "kv"),
    (list[Address], "table"), (int, "json"),
])
def test_output_kind(annotation, kind):
    def f() -> None: ...
    f.__annotations__["return"] = annotation
    assert output_kind(f) == kind


def test_output_kind_without_annotation_is_json():
    def f():
        return 1
    assert output_kind(f) == "json"


def test_deploy_registers_with_metadata():
    @deploy(title="Nice title", secrets=["TOKEN"], schedule="0 9 * * *", timeout=60)
    def sdk_test_fn(x: int = 1) -> int:
        """First paragraph.

        More detail."""
        return x * 2

    deployed = registry["sdk_test_fn"]
    entry = deployed.manifest_entry()
    assert entry["title"] == "Nice title"
    assert entry["description"] == "First paragraph."
    assert entry["secrets"] == ["TOKEN"]
    assert entry["entrypoint"].endswith(":test_deploy_registers_with_metadata.<locals>.sdk_test_fn")
    assert sdk_test_fn(4) == 8  # still callable normally


def test_call_coerces_json_inputs():
    @deploy
    def sdk_coerce(when: date, color: Color, addr: Address) -> dict:
        return {"when": when, "color": color, "city": addr.city}

    out = registry["sdk_coerce"].call({"when": "2026-01-02", "color": "blue", "addr": {"city": "Oslo"}})
    assert out == {"when": date(2026, 1, 2), "color": Color.blue, "city": "Oslo"}


def test_duplicate_names_are_rejected():
    @deploy(name="sdk_dupe")
    def a(): ...

    with pytest.raises(ValueError, match="sdk_dupe"):
        @deploy(name="sdk_dupe")
        def b(): ...


def test_runtime_emits_result_and_errors(tmp_path, capsys, monkeypatch):
    pkg = tmp_path / "autos"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "m.py").write_text(
        "from datetime import date\nfrom callables import deploy\n"
        "@deploy\ndef rt_ok(n: int = 2) -> list[dict]:\n    print('working')\n    return [{'n': n, 'd': date(2026, 1, 1)}]\n"
        "@deploy\ndef rt_bad() -> None:\n    raise ValueError('nope')\n"
    )
    monkeypatch.setenv("CALLABLES_INPUTS", json.dumps({"n": 5}))
    assert run("autos.m:rt_ok", tmp_path) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert out[0] == "working"
    assert json.loads(out[-1].removeprefix(RESULT_MARKER)) == {"ok": True, "value": [{"n": 5, "d": "2026-01-01"}], "files": []}
    assert "CALLABLES_INPUTS" not in os.environ  # inputs are consumed, not left for user code

    assert run("autos.m:rt_bad", tmp_path) == 1
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert json.loads(last.removeprefix(RESULT_MARKER)) == {"ok": False, "error": "ValueError: nope", "files": []}


def test_dataframes_keep_plain_dates_and_null_missing_values():
    pd = pytest.importorskip("pandas")
    from callables.runtime import to_jsonable

    df = pd.DataFrame({"week": [date(2026, 8, 3), None], "at": [pd.Timestamp("2026-08-03 10:00", tz="UTC"), pd.NaT],
                       "trials": [3, 4], "rate": [0.5, float("nan")]})
    assert to_jsonable(df) == [
        {"week": "2026-08-03", "at": "2026-08-03T10:00:00Z", "trials": 3, "rate": 0.5},
        {"week": None, "at": None, "trials": 4, "rate": None},
    ]


def test_manifest_reports_import_errors(tmp_path):
    from callables.manifest import build_manifest

    (tmp_path / "broken.py").write_text("from callables import deploy\nimport not_a_real_module_xyz\n")
    manifest = build_manifest(tmp_path)
    assert manifest["errors"][0]["module"] == "broken"
    assert "not_a_real_module_xyz" in manifest["errors"][0]["error"]
