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


PROJECT = '''
from callables import Group

audits = Group("audits", title="Audits")


@audits.deploy
def weekly(weeks: int = 4) -> str:
    """Weekly numbers."""
    return f"weekly {weeks}"


@audits.command
def render(slug: str, open_browser: bool = False) -> str:
    return f"rendered {slug} open={open_browser}"
'''


@pytest.fixture
def project(tmp_path, monkeypatch, request):
    module = f"loc_{request.node.name}"
    (tmp_path / f"{module}.py").write_text(textwrap.dedent(PROJECT))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_commands_stay_out_of_the_deploy(project):
    assert [f["name"] for f in build_manifest(project)["functions"]] == ["audits.weekly"]
    local = build_manifest(project, include_local=True)["functions"]
    assert [(f["name"], f.get("local_only", False)) for f in local] == [("audits.render", True), ("audits.weekly", False)]


def test_commands_run_from_the_cli(project, capsys):
    assert main(["audits", "render", "--slug", "us-v2", "--open-browser"]) == 0
    assert capsys.readouterr().out.strip() == "rendered us-v2 open=True"
    assert main(["audits"]) == 0
    assert "(local only)" in capsys.readouterr().out
    assert main(["local", "audits.render", "-i", "slug=x"]) == 0


def test_check_lists_them_without_card_warnings(project):
    report = check_project(project)
    assert report["ok"]
    assert [f["local_only"] for f in report["functions"]] == [True, False]
    assert not any(i.get("function") == "audits.render" and "description" in i["message"] for i in report["issues"])


def test_a_module_with_only_commands_is_found(tmp_path, monkeypatch, capsys):
    (tmp_path / "cmd_only.py").write_text("from callables import command\n@command\ndef hi(name: str) -> str:\n"
                                          "    return f'hi {name}'\n")
    monkeypatch.chdir(tmp_path)
    assert main(["hi", "--name", "Ann"]) == 0
    assert capsys.readouterr().out.strip() == "hi Ann"
    report = check_project(tmp_path)
    assert any("publishes nothing" in i["message"] for i in report["issues"])


def test_deployed_actions_cant_offer_a_command_next(tmp_path):
    (tmp_path / "cmd_next.py").write_text("from callables import command, deploy\n@command\ndef a() -> None: ...\n"
                                          "@deploy(next=[a])\ndef b() -> dict: ...\n")
    report = check_project(tmp_path)
    assert not report["ok"] and any("local @command" in i["message"] for i in report["issues"])


def test_command_takes_no_platform_options():
    from callables import command

    with pytest.raises(TypeError):
        command(schedule="0 9 * * MON")
