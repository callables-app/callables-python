import json
import textwrap

import pytest

from callables import Group, registry
from callables.cli import main
from callables.manifest import build_manifest


@pytest.fixture(autouse=True)
def isolated():
    registry.clear()
    yield
    registry.clear()


PROJECT = '''
from datetime import date
from enum import Enum
from typing import Literal

from callables import Group, deploy

backups = Group("backups", title="Database backups", description="Dumps to Spaces",
                secrets=["SPACES_KEY", "SPACES_SECRET"])


class Level(str, Enum):
    low = "low"
    high = "high"


@backups.deploy(title="Back up now", secrets=["DATABASE_URL", "SPACES_KEY"])
def run(env: Literal["prod", "staging"] = "prod", keep: int = 7, dry_run: bool = False,
        tables: list[str] | None = None, since: date | None = None, level: Level = Level.low) -> str:
    """Dumps the database."""
    return f"{env} keep={keep} dry_run={dry_run} tables={tables} since={since} level={level.value}"


@backups.deploy
def latest(code: str) -> str:
    return f"code={code!r}"


@deploy
def hello(name: str = "world") -> str:
    return f"hello {name}"
'''


@pytest.fixture
def project(tmp_path, monkeypatch, request):
    # A unique module name per test, so imports from earlier tests aren't reused
    module = f"grp_{request.node.name.replace('[', '_').replace(']', '').replace('-', '_')}"
    (tmp_path / f"{module}.py").write_text(textwrap.dedent(PROJECT))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_group_names_actions_and_shares_secrets(project):
    manifest = build_manifest(project)
    by_name = {f["name"]: f for f in manifest["functions"]}
    assert sorted(by_name) == ["backups.latest", "backups.run", "hello"]
    run = by_name["backups.run"]
    assert run["action"] == "run" and run["title"] == "Back up now"
    assert run["group"] == {"name": "backups", "title": "Database backups", "description": "Dumps to Spaces"}
    assert run["secrets"] == ["SPACES_KEY", "SPACES_SECRET", "DATABASE_URL"]  # group's first, no repeats
    assert by_name["backups.latest"]["title"] == "Latest"
    assert by_name["hello"]["group"] is None and by_name["hello"]["action"] == "hello"


@pytest.mark.parametrize("bad", ["Backups", "1st", "my group", "check"])
def test_group_names_must_work_as_commands(bad):
    with pytest.raises(ValueError):
        Group(bad)


def test_a_group_defined_twice_differently_fails_the_manifest(tmp_path):
    (tmp_path / "grp_twice.py").write_text(textwrap.dedent('''
        from callables import Group
        a, b = Group("ops", title="Ops"), Group("ops", title="Operations")

        @a.deploy
        def one() -> None: ...

        @b.deploy
        def two() -> None: ...
    '''))
    errors = build_manifest(tmp_path)["errors"]
    assert len(errors) == 1 and "defined twice" in errors[0]["error"]


def test_actions_run_like_typer_commands(project, capsys):
    argv = ["backups", "run", "--env", "staging", "--keep", "3", "--dry-run", "--tables", "users", "orders",
            "--since", "2026-10-01", "--level", "high"]
    assert main(argv) == 0
    assert capsys.readouterr().out.strip() == (
        "staging keep=3 dry_run=True tables=['users', 'orders'] since=2026-10-01 level=high")

    assert main(["backups", "run"]) == 0  # defaults apply
    assert capsys.readouterr().out.strip().startswith("prod keep=7 dry_run=False tables=None")

    assert main(["backups", "latest", "--code", "007"]) == 0  # strings stay strings
    assert capsys.readouterr().out.strip() == "code='007'"
    assert main(["hello", "--name", "Ann"]) == 0  # functions without a group
    assert capsys.readouterr().out.strip() == "hello Ann"


def test_help_and_mistakes(project, capsys):
    assert main(["backups"]) == 0
    out = capsys.readouterr().out
    assert "Database backups" in out and "run" in out and "Back up now" in out

    assert main(["backups", "nope"]) == 2
    assert "no action named 'nope'" in capsys.readouterr().err

    assert main(["nothing"]) == 2
    err = capsys.readouterr().err
    assert "isn't a command" in err and "backups" in err and "hello" in err

    with pytest.raises(SystemExit) as exc:
        main(["backups", "run", "--env", "dev"])  # not one of the choices
    assert exc.value.code == 2
    with pytest.raises(SystemExit):
        main(["backups", "latest"])  # --code is required
    assert "--code" in capsys.readouterr().err

    with pytest.raises(SystemExit) as exc:
        main(["backups", "run", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--env {prod,staging}" in out and "--dry-run, --no-dry-run" in out and '(default: "prod")' in out


def test_local_takes_full_names_too(project, capsys):
    assert main(["local", "backups.run", "-i", "keep=1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["value"].startswith("prod keep=1")
