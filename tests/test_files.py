import json
from typing import Annotated

import pytest

from callables import File, accept, deploy, registry, save_file
from callables import files as files_mod
from callables.cli import main
from callables.runtime import RESULT_MARKER, run
from callables.schema import input_schema, output_kind


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    registry.clear()
    files_mod._saved.clear()
    monkeypatch.setenv("SHIPFN_OUTPUT_DIR", str(tmp_path / "out"))
    yield
    registry.clear()
    files_mod._saved.clear()


def test_file_parameters_become_upload_fields():
    @deploy
    def clean(sheet: Annotated[File, accept(".csv", ".xlsx")], notes: File | None = None) -> File:
        ...

    props = input_schema(clean)["properties"]
    assert props["sheet"] == {"type": "string", "format": "shipfn-file", "title": "Sheet", "x-accept": ".csv,.xlsx"}
    assert {"type": "string", "format": "shipfn-file"} in props["notes"]["anyOf"]
    assert input_schema(clean)["required"] == ["sheet"]
    assert output_kind(clean) == "file"

    @deploy
    def many() -> list[File]:
        ...
    assert output_kind(many) == "file"


def test_runtime_hands_uploads_in_and_files_out(tmp_path, capsys, monkeypatch):
    upload = tmp_path / "in" / "data.csv"
    upload.parent.mkdir()
    upload.write_text("a,b\n1,2\n")
    (tmp_path / "fx_double.py").write_text(
        "from callables import deploy, File, save_file\n"
        "@deploy\n"
        "def double(sheet: File) -> File:\n"
        "    save_file(b'side output', name='notes.txt')\n"
        "    out = sheet.path.parent / 'doubled.csv'\n"
        "    out.write_text(sheet.read_text() * 2)\n"
        "    return File(out, name=f'doubled-{sheet.name}')\n"
    )
    monkeypatch.setenv("SHIPFN_INPUTS", json.dumps({"sheet": {"path": str(upload), "name": "Sales Q3.csv"}}))
    assert run("fx_double:double", tmp_path) == 0
    payload = json.loads(capsys.readouterr().out.splitlines()[-1].removeprefix(RESULT_MARKER))
    assert payload["ok"] and payload["value"] is None
    by_name = {f["name"]: f for f in payload["files"]}
    assert set(by_name) == {"notes.txt", "doubled-Sales Q3.csv"}
    assert by_name["doubled-Sales Q3.csv"]["content_type"] == "text/csv"
    out = tmp_path / "out" / by_name["doubled-Sales Q3.csv"]["path"]
    assert out.read_text() == "a,b\n1,2\n" * 2 and by_name["notes.txt"]["size"] == len(b"side output")


def test_missing_upload_is_a_validation_error(tmp_path, capsys, monkeypatch):
    (tmp_path / "fx_missing.py").write_text("from callables import deploy, File\n@deploy\ndef f(sheet: File) -> str:\n    return 'x'\n")
    monkeypatch.setenv("SHIPFN_INPUTS", json.dumps({"sheet": {"path": str(tmp_path / "nope.csv")}}))
    assert run("fx_missing:f", tmp_path) == 1
    payload = json.loads(capsys.readouterr().out.splitlines()[-1].removeprefix(RESULT_MARKER))
    assert "File not found" in payload["error"]


def test_save_file_names_are_made_safe():
    assert save_file(b"x", name="../../etc/passwd").name == "passwd"
    assert save_file(b"x", name="..hidden").name == "hidden"
    with pytest.raises(ValueError):
        save_file(b"no name")


def test_local_cli_takes_and_reports_files(tmp_path, capsys):
    (tmp_path / "data.csv").write_text("x\n")
    (tmp_path / "fx_echo.py").write_text(
        "from callables import deploy, File\n@deploy\ndef echo(sheet: File) -> File:\n    return sheet\n")
    assert main(["local", "echo", "--root", str(tmp_path), "-i", f"sheet=@{tmp_path / 'data.csv'}"]) == 0
    err = capsys.readouterr().err
    assert "Files:" in err and "data.csv" in err
