import textwrap

import pytest

from callables import registry
from callables.checks import check_project
from callables.cli import main
from callables.manifest import build_manifest


@pytest.fixture(autouse=True)
def isolated():
    registry.clear()
    yield
    registry.clear()


USERS = '''
from typing import Literal

from callables import Group, Next

users = Group("users", title="Users")
DB = {42: {"name": "Ann Lee", "admin": False, "sub": "Not required"},
      77: {"name": "Ann Park", "admin": False, "sub": "Not required"}}


@users.deploy(title="Make admin")
def make_admin(user_id: int) -> str:
    DB[user_id]["admin"] = True
    return f"{DB[user_id]['name']} is now an admin"


@users.deploy(title="Subscription requirement")
def set_subscription(user_id: int, required: Literal["Required", "Not required"], note: str = "") -> str:
    DB[user_id]["sub"] = required
    return f"{DB[user_id]['name']}: {required} {note}".strip()


@users.deploy(title="Find user", next=[make_admin, Next("set_subscription", label="Change subscription", user_id="id")])
def search(query: str) -> list[dict]:
    return [{"id": i, "user_id": i, "name": u["name"]} for i, u in DB.items() if query.lower() in u["name"].lower()]
'''


@pytest.fixture
def project(tmp_path, monkeypatch, request):
    module = f"nxt_{request.node.name.replace('[', '_').replace(']', '').replace('-', '_')}"
    (tmp_path / f"{module}.py").write_text(textwrap.dedent(USERS))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_next_actions_resolve_into_the_manifest(project):
    search = next(f for f in build_manifest(project)["functions"] if f["name"] == "users.search")
    assert search["next"] == [
        {"action": "users.make_admin", "label": "Make admin", "inputs": {"user_id": "user_id"}},
        {"action": "users.set_subscription", "label": "Change subscription",
         "inputs": {"user_id": "id", "required": "required", "note": "note"}},
    ]


@pytest.mark.parametrize("bad, message", [
    ('next=["nope"]', "no deployed action named 'nope'"),
    ('next=[Next(make_admin, uid="id")]', "has no input named uid"),
])
def test_bad_next_actions_fail_the_manifest(tmp_path, bad, message):
    name = "bad_next_unknown" if "nope" in bad else "bad_next_input"
    (tmp_path / f"{name}.py").write_text(textwrap.dedent(f'''
        from callables import Next, deploy

        @deploy
        def make_admin(user_id: int) -> str: ...

        @deploy({bad})
        def search(query: str) -> list[dict]: ...
    '''))
    errors = build_manifest(tmp_path)["errors"]
    assert len(errors) == 1 and message in errors[0]["error"]


def test_check_warns_when_the_result_has_no_rows(tmp_path, monkeypatch):
    (tmp_path / "next_markdown.py").write_text(textwrap.dedent('''
        from callables import deploy

        @deploy
        def make_admin(user_id: int) -> str: ...

        @deploy(next=[make_admin])
        def search(query: str) -> str:
            """Finds people."""
    '''))
    issues = check_project(tmp_path)["issues"]
    assert any("table rows or a single record" in i["message"] for i in issues)


def tty(monkeypatch, answers):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    replies = iter(answers)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(replies))


def test_terminal_flow_find_pick_act(project, monkeypatch, capsys):
    # Two Anns: pick the first, choose "Change subscription", answer the one input the row couldn't fill
    tty(monkeypatch, ["1", "2", "Required", ""])
    assert main(["users", "search", "--query", "ann"]) == 0
    out = capsys.readouterr().out
    assert "1. id=42" in out and "2. Change subscription" in out
    assert "Ann Lee: Required" in out


def test_one_row_skips_the_pick_and_enter_stops(project, monkeypatch, capsys):
    tty(monkeypatch, ["1"])
    assert main(["users", "search", "--query", "park"]) == 0
    assert "Ann Park is now an admin" in capsys.readouterr().out

    tty(monkeypatch, [""])  # Enter at "What next?" ends the flow
    assert main(["users", "search", "--query", "park"]) == 0


def test_no_menu_when_output_isnt_a_terminal(project, capsys):
    assert main(["users", "search", "--query", "ann"]) == 0
    assert '"name": "Ann Lee"' in capsys.readouterr().out  # plain JSON, no prompts


def test_tables_print_as_columns_at_a_terminal(project, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    assert main(["users", "search", "--query", "ann"]) == 0  # stdin isn't a terminal, so no menu
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["id", "user_id", "name"]
    assert lines[2].split() == ["42", "42", "Ann", "Lee"] and lines[-1] == "2 rows"
