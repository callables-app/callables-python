import json

import pytest

from callables import registry
from callables.cli import main
from callables.project import ConfigError, build_root


@pytest.fixture(autouse=True)
def isolated():
    registry.clear()
    yield
    registry.clear()


def test_whole_repo_unless_a_root_is_set(tmp_path):
    assert build_root(tmp_path) == (tmp_path.resolve(), None)
    (tmp_path / "automations").mkdir()
    (tmp_path / "pyproject.toml").write_text('[tool.callables]\nroot = "automations/"\n')
    assert build_root(tmp_path) == ((tmp_path / "automations").resolve(), "pyproject.toml [tool.callables]")
    (tmp_path / "callables.toml").write_text('root = "./automations"\n')  # a config file wins over pyproject.toml
    assert build_root(tmp_path) == ((tmp_path / "automations").resolve(), "callables.toml")


def test_tool_callables_in_pyproject(tmp_path):
    (tmp_path / "automations").mkdir()
    (tmp_path / "pyproject.toml").write_text('[tool.callables]\nroot = "automations"\n')
    assert build_root(tmp_path) == ((tmp_path / "automations").resolve(), "pyproject.toml [tool.callables]")


@pytest.mark.parametrize("value, message", [
    ('"../elsewhere"', "inside the repo"),
    ('"/etc"', "inside the repo"),
    ('"missing"', "no folder there"),
    ('"escape"', "points outside the repo"),
    ("3", "must be a folder path"),
])
def test_root_must_be_a_folder_in_the_repo(tmp_path, value, message):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "escape").symlink_to(tmp_path)
    (repo / "callables.toml").write_text(f"root = {value}\n")
    with pytest.raises(ConfigError, match=message):
        build_root(repo)


def test_commands_use_the_same_folder_the_platform_builds(tmp_path, monkeypatch, capsys):
    (tmp_path / "callables.toml").write_text('root = "autos"\n')
    (tmp_path / "autos").mkdir()
    (tmp_path / "autos" / "proj_root_jobs.py").write_text(
        "from callables import deploy\n\n@deploy()\ndef proj_root_fn() -> str:\n    return 'hi'\n")
    (tmp_path / "unrelated.py").write_text("import callables  # mentions deploy, but outside the root\nraise SystemExit\n")
    monkeypatch.chdir(tmp_path)
    assert main(["manifest"]) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert [f["entrypoint"] for f in manifest["functions"]] == ["proj_root_jobs:proj_root_fn"]
    assert not manifest["errors"]
