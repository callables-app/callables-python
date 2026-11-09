import json
import os
import textwrap

import pytest

from callables import cli, registry
from callables.cli import main
from callables.envfile import load, parse


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    registry.clear()
    monkeypatch.setattr(cli, "_env_loaded", False)
    for key in ("CALLABLES_T_TOKEN", "CALLABLES_T_SHELL"):
        monkeypatch.delenv(key, raising=False)
    yield
    registry.clear()
    for key in ("CALLABLES_T_TOKEN", "CALLABLES_T_SHELL"):  # load() sets os.environ directly
        os.environ.pop(key, None)


def test_parse_reads_the_usual_dotenv_forms():
    text = textwrap.dedent('''
        # a comment
        PLAIN=abc
        export EXPORTED=1
        SPACED = padded value
        SINGLE='keep $this \\n raw'
        DOUBLE="line\\nbreak \\"quoted\\""
        INLINE=value # trailing comment
        HASH=pass#word
        URL=postgresql://u:p@host:5432/db?sslmode=require
        EMPTY=
        not a line
        1BAD=x
    ''')
    assert parse(text) == {
        "PLAIN": "abc",
        "EXPORTED": "1",
        "SPACED": "padded value",
        "SINGLE": "keep $this \\n raw",
        "DOUBLE": 'line\nbreak "quoted"',
        "INLINE": "value",
        "HASH": "pass#word",
        "URL": "postgresql://u:p@host:5432/db?sslmode=require",
        "EMPTY": "",
    }


def test_load_never_overrides_what_is_already_set(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("CALLABLES_T_TOKEN=from-file\nCALLABLES_T_SHELL=from-file\n")
    monkeypatch.setenv("CALLABLES_T_SHELL", "from-shell")
    files, added = load(tmp_path)
    assert files == [(tmp_path / ".env").resolve()]
    assert added == ["CALLABLES_T_TOKEN"]
    assert os.environ["CALLABLES_T_TOKEN"] == "from-file"
    assert os.environ["CALLABLES_T_SHELL"] == "from-shell"


def test_the_build_folder_env_wins_over_the_current_folder(tmp_path):
    sub = tmp_path / "automations"
    sub.mkdir()
    (sub / ".env").write_text("CALLABLES_T_TOKEN=folder\n")
    (tmp_path / ".env").write_text("CALLABLES_T_TOKEN=repo\nCALLABLES_T_SHELL=repo\n")
    load(sub, tmp_path)
    assert os.environ["CALLABLES_T_TOKEN"] == "folder"
    assert os.environ["CALLABLES_T_SHELL"] == "repo"


PROJECT = '''
import os
from callables import deploy


@deploy(secrets=["CALLABLES_T_TOKEN"])
def token() -> str:
    return os.environ.get("CALLABLES_T_TOKEN", "missing")
'''


@pytest.fixture
def project(tmp_path, monkeypatch, request):
    (tmp_path / f"env_{request.node.name}.py").write_text(textwrap.dedent(PROJECT))
    (tmp_path / ".env").write_text("CALLABLES_T_TOKEN=secret-from-env-file\n")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_local_runs_see_the_env_file(project, capsys):
    assert main(["local", "token", "--json"]) == 0
    out = capsys.readouterr()
    assert json.loads(out.out)["value"] == "secret-from-env-file"
    assert "Loaded 1 variable from .env" in out.err


def test_commands_see_the_env_file(project, capsys):
    assert main(["token"]) == 0
    assert capsys.readouterr().out.strip() == "secret-from-env-file"


def test_the_shell_wins_over_the_env_file(project, capsys, monkeypatch):
    monkeypatch.setenv("CALLABLES_T_TOKEN", "from-shell")
    assert main(["token"]) == 0
    assert capsys.readouterr().out.strip() == "from-shell"


def test_the_platform_runner_ignores_the_env_file(project, monkeypatch, capsys, request):
    monkeypatch.setenv("CALLABLES_INPUTS", "{}")
    main(["run", f"env_{request.node.name}:token", "--root", str(project)])
    assert "missing" in capsys.readouterr().out
    assert "CALLABLES_T_TOKEN" not in os.environ


def test_a_env_at_the_top_of_the_repo_is_found_from_a_project_folder(tmp_path, monkeypatch):
    from callables.envfile import repo_root
    (tmp_path / ".git").mkdir()
    project = tmp_path / "gmail"
    project.mkdir()
    assert repo_root(project) == tmp_path
    (tmp_path / ".env").write_text("CALLABLES_T_TOKEN=top\n")
    monkeypatch.delenv("CALLABLES_T_TOKEN", raising=False)
    files, _ = load(project, repo_root(project))
    assert files == [tmp_path / ".env"] and os.environ["CALLABLES_T_TOKEN"] == "top"
