"""check, local --json, docs and init: the tools coding agents lean on."""
import json

import pytest

from callables import registry
from callables import files as files_mod
from callables.cli import main
from callables.init import SECTION_END, SECTION_START, init_project
from callables.schema import input_model


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    registry.clear()
    files_mod._saved.clear()
    monkeypatch.setenv("SHIPFN_OUTPUT_DIR", str(tmp_path / "out"))
    yield
    registry.clear()
    files_mod._saved.clear()


def run_json(capsys, argv):
    code = main(argv)
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured.err


def test_check_passes_a_clean_project(tmp_path, capsys):
    (tmp_path / "requirements.txt").write_text("")
    (tmp_path / "fx_ag_clean.py").write_text(
        "from callables import deploy\n"
        "@deploy\n"
        "def greet(name: str) -> str:\n"
        "    '''Says hello.'''\n"
        "    print('import-time noise stays off stdout')\n"
        "    return name\n"
        "print('module noise')\n")
    code, report, err = run_json(capsys, ["check", "--root", str(tmp_path), "--json"])
    assert code == 0 and report["ok"]
    assert report["issues"] == []
    assert report["functions"] == [{"name": "greet", "title": "Greet", "entrypoint": "fx_ag_clean:greet",
                                    "inputs": ["name"], "output_kind": "markdown", "local_only": False}]
    assert "module noise" in err


def test_check_reports_errors_and_warnings_with_fixes(tmp_path, capsys):
    (tmp_path / "fx_ag_broken.py").write_text("from callables import deploy\nimport not_a_real_module\n")
    (tmp_path / "fx_ag_warn.py").write_text(
        "from callables import deploy, File\n"
        "@deploy(timeout=2000, secrets=['bad-name'], schedule='0 9 * * MON')\n"
        "def sloppy(x, sheet: File):\n"
        "    return x\n")
    code, report, _ = run_json(capsys, ["check", "--root", str(tmp_path), "--json"])
    assert code == 1 and not report["ok"]
    messages = {(i["level"], i["message"]) for i in report["issues"]}
    text = " ".join(m for _, m in messages)
    assert any(level == "error" and "not_a_real_module" in m for level, m in messages)
    assert any(level == "error" and "bad-name" in m for level, m in messages)
    for expected in ["No description", "Parameter 'x' has no type hint", "No return type", "timeout=2000",
                     "need a default for every input", "file inputs can't run on a schedule", "No requirements.txt"]:
        assert expected in text, expected
    assert all("fix" in i for i in report["issues"] if "type hint" in i["message"])


def test_check_with_no_functions_fails(tmp_path, capsys):
    assert main(["check", "--root", str(tmp_path)]) == 1
    assert "No @deploy functions found" in capsys.readouterr().out


def test_unsupported_input_type_names_the_parameter():
    class Thing:
        pass

    def f(ok: int, bad: Thing) -> None: ...

    with pytest.raises(TypeError, match=r"parameter 'bad: Thing' can't become a form field"):
        input_model(f)


def test_local_json_success_keeps_prints_off_stdout(tmp_path, capsys):
    (tmp_path / "fx_ag_local.py").write_text(
        "from callables import deploy, save_file\n"
        "@deploy\n"
        "def total(a: int, b: int = 2) -> dict:\n"
        "    print('working')\n"
        "    save_file(b'hi', name='note.txt')\n"
        "    return {'sum': a + b}\n")
    code, out, err = run_json(capsys, ["local", "total", "--root", str(tmp_path), "-i", "a=5", "--json"])
    assert code == 0
    assert out["ok"] and out["value"] == {"sum": 7}
    assert out["files"][0]["name"] == "note.txt" and out["files"][0]["local_path"].endswith("note.txt")
    assert "working" in err


def test_local_json_reports_field_errors_and_exceptions(tmp_path, capsys):
    (tmp_path / "fx_ag_fail.py").write_text(
        "from callables import deploy\n"
        "@deploy\n"
        "def boom(n: int) -> str:\n"
        "    raise ValueError(f'No deals for {n}')\n")
    code, out, _ = run_json(capsys, ["local", "boom", "--root", str(tmp_path), "-i", "n=abc", "--json"])
    assert code == 1 and out["field_errors"].keys() == {"n"}

    code, out, _ = run_json(capsys, ["local", "boom", "--root", str(tmp_path), "-i", "n=3", "--json"])
    assert code == 1 and out["error"] == "ValueError: No deals for 3" and "Traceback" in out["traceback"]

    code, out, _ = run_json(capsys, ["local", "nope", "--root", str(tmp_path), "--json"])
    assert code == 1 and "Found: boom" in out["error"]


def test_docs_prints_the_guide(capsys):
    assert main(["docs"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# Writing Callables automations") and "call check" in out


def test_init_sets_up_a_repo_and_is_safe_to_rerun(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# House rules\n\nUse tabs.\n")
    (tmp_path / "CLAUDE.md").write_text("Be brief.\n")
    done = dict(init_project(tmp_path))
    assert done["automations.py"] == "created" and done["requirements.txt"] == "created"
    assert done[".claude/skills/callables/SKILL.md"] == "created"
    assert done["AGENTS.md"] == "updated" and done["CLAUDE.md"] == "updated"
    skill = (tmp_path / ".claude/skills/callables/SKILL.md").read_text()
    assert skill.startswith("---\nname: callables\n") and "# Writing Callables automations" in skill
    agents = (tmp_path / "AGENTS.md").read_text()
    assert agents.startswith("# House rules\n\nUse tabs.\n\n" + SECTION_START) and agents.rstrip().endswith(SECTION_END)
    assert "callables-output/" in (tmp_path / ".gitignore").read_text()

    # The example passes check
    registry.clear()
    assert main(["check", "--root", str(tmp_path)]) == 0

    # Re-running changes nothing and never duplicates the section
    (tmp_path / "automations.py").write_text("# mine now\n")
    again = dict(init_project(tmp_path))
    assert again["AGENTS.md"] == "unchanged" and again[".claude/skills/callables/SKILL.md"] == "unchanged"
    assert "skipped" in again["automations.py"] and (tmp_path / "automations.py").read_text() == "# mine now\n"
    assert (tmp_path / "AGENTS.md").read_text().count(SECTION_START) == 1
    assert (tmp_path / ".gitignore").read_text().count("callables-output") == 1

    # An outdated section is refreshed in place
    (tmp_path / "AGENTS.md").write_text(f"top\n{SECTION_START}\nold\n{SECTION_END}\nbottom\n")
    assert dict(init_project(tmp_path, example=False))["AGENTS.md"] == "updated"


def test_init_replaces_what_the_shipfn_version_wrote(tmp_path):
    (tmp_path / "AGENTS.md").write_text("top\n<!-- shipfn:start -->\nold\n<!-- shipfn:end -->\nbottom\n")
    old_skill = tmp_path / ".claude/skills/shipfn/SKILL.md"
    old_skill.parent.mkdir(parents=True)
    old_skill.write_text("old guide")
    done = dict(init_project(tmp_path, example=False))
    assert done[".claude/skills/shipfn/SKILL.md"].startswith("removed") and not old_skill.parent.exists()
    agents = (tmp_path / "AGENTS.md").read_text()
    assert "shipfn:start" not in agents and agents.count(SECTION_START) == 1
    assert agents.startswith("top\n" + SECTION_START) and agents.endswith(SECTION_END + "\nbottom\n")
    refreshed = (tmp_path / "AGENTS.md").read_text()
    assert refreshed.startswith("top\n") and refreshed.endswith("bottom\n") and "old" not in refreshed
