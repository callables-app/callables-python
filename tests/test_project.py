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


def test_the_projects_folder_wins_over_the_repo_config(tmp_path):
    (tmp_path / "automations" / "email").mkdir(parents=True)
    (tmp_path / "callables.toml").write_text('root = "automations"\n')
    assert build_root(tmp_path, "automations/email") == (
        (tmp_path / "automations" / "email").resolve(), "the project's settings")
    assert build_root(tmp_path, "") == ((tmp_path / "automations").resolve(), "callables.toml")
    with pytest.raises(ConfigError, match="no folder"):
        build_root(tmp_path, "salesforce")
    with pytest.raises(ConfigError, match="inside the repo"):
        build_root(tmp_path, "../other")


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


def _write(path, text=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _include_repo(tmp_path, include='["inc_shared/tools"]'):
    """A repo whose recipes/inc_seo folder imports a sibling folder, and has one it doesn't use."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    _write(repo / "inc_shared" / "tools" / "inc_tools_api.py", "def hello():\n    return 'hi'\n")
    _write(repo / "inc_shared" / "tools" / "inc_tools_review.py",
           "from callables import deploy\n\n@deploy()\ndef inc_tools_fn() -> str:\n    return 'tools'\n")
    _write(repo / "inc_unused" / "inc_unused_jobs.py",
           "from callables import deploy\n\n@deploy()\ndef inc_unused_fn() -> str:\n    return 'no'\n")
    _write(repo / "recipes" / "inc_seo" / "callables.toml", f"include = {include}\n")
    _write(repo / "recipes" / "inc_seo" / "requirements.txt")
    _write(repo / "recipes" / "inc_seo" / "inc_seo_jobs.py",
           "from callables import deploy\nfrom inc_shared.tools.inc_tools_api import hello\n\n"
           "@deploy()\ndef inc_seo_fn() -> str:\n    return hello()\n")
    return repo


def test_include_builds_the_folder_with_its_includes_at_their_repo_paths(tmp_path):
    from callables.manifest import build_manifest
    from callables.project import layout

    repo = _include_repo(tmp_path)
    folder = repo / "recipes" / "inc_seo"
    assert layout(folder) == (repo.resolve(), [folder.resolve(), (repo / "inc_shared" / "tools").resolve()])
    manifest = build_manifest(folder)
    assert not manifest["errors"]
    assert sorted(f["entrypoint"] for f in manifest["functions"]) == [
        "inc_shared.tools.inc_tools_review:inc_tools_fn", "recipes.inc_seo.inc_seo_jobs:inc_seo_fn"]


def test_importing_a_folder_that_isnt_included_is_an_error(tmp_path):
    from callables.manifest import build_manifest

    repo = tmp_path / "repo"  # its own module names: sys.modules outlives each test's folder
    (repo / ".git").mkdir(parents=True)
    _write(repo / "out_shared" / "out_api.py", "def hello():\n    return 'hi'\n")
    _write(repo / "out_other" / "out_other_api.py", "X = 1\n")
    _write(repo / "out_recipe" / "callables.toml", 'include = ["out_other"]\n')
    _write(repo / "out_recipe" / "out_jobs.py",
           "from callables import deploy\nfrom out_shared.out_api import hello\n\n"
           "@deploy()\ndef out_fn() -> str:\n    return hello()\n")
    errors = build_manifest(repo / "out_recipe")["errors"]
    assert any('out_shared/, which isn\'t in the build. Add "out_shared"' in e["error"] for e in errors), errors


@pytest.mark.parametrize("include, message", [
    ('"inc_shared"', "must be a list"),
    ('["../elsewhere"]', "relative to the top of the repo"),
    ('["missing"]', "no folder 'missing'"),
    ('["recipes"]', "contains the folder being built"),
])
def test_include_must_list_folders_in_the_repo(tmp_path, include, message):
    from callables.project import includes

    repo = _include_repo(tmp_path, include=include)
    with pytest.raises(ConfigError, match=message):
        includes(repo, repo / "recipes" / "inc_seo")


def test_include_is_ignored_for_the_whole_repo_and_without_a_repo(tmp_path, monkeypatch):
    from callables.checks import _check_includes
    from callables.project import layout

    repo = _include_repo(tmp_path)
    _write(repo / "callables.toml", 'include = ["inc_unused"]\n')
    assert layout(repo) == (repo.resolve(), [repo.resolve()])
    assert "whole repo" in _check_includes(repo)[0]["message"]
    loose = tmp_path / "loose"
    _write(loose / "callables.toml", 'include = ["x"]\n')
    monkeypatch.chdir(loose)
    assert layout(loose) == (loose.resolve(), [loose.resolve()])


def test_include_needs_requirements_txt(tmp_path):
    from callables.checks import _check_includes

    repo = _include_repo(tmp_path)
    (repo / "recipes" / "inc_seo" / "requirements.txt").unlink()
    _write(repo / "recipes" / "inc_seo" / "pyproject.toml", '[project]\nname = "x"\ndependencies = ["requests"]\n')
    assert _check_includes(repo / "recipes" / "inc_seo")[0]["level"] == "error"
