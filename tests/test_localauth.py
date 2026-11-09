"""`call connect`: the local OAuth sign-in, token storage, and tokens for `call local`."""
import base64
import hashlib
import io
import json
import os
import socket
import stat
import threading
import time
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen as real_urlopen

import pytest

from callables import Connection
from callables import connections as connection_registry
from callables import localauth
from callables.connections import CONNECTIONS_ENV


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    connection_registry.connections.clear()
    connection_registry.conflicts.clear()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-123")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "shh")
    for name in ("GOOGLE_ACCESS_TOKEN", CONNECTIONS_ENV):
        monkeypatch.delenv(name, raising=False)
    yield
    connection_registry.connections.clear()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeGoogle:
    """The provider's side: the token and userinfo endpoints, and a browser that comes back with a code."""

    def __init__(self, granted="openid https://www.googleapis.com/auth/userinfo.email "
                               "https://www.googleapis.com/auth/gmail.modify"):
        self.granted = granted
        self.challenge = None
        self.token_requests: list[dict] = []
        self.issued = 0

    def browser(self, url: str) -> bool:
        """Like a person clicking Allow: the browser follows the redirect with a code and the same state."""
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        assert query["client_id"] == "client-123" and query["access_type"] == "offline"
        self.challenge = query.get("code_challenge")
        target = f"{query['redirect_uri']}?code=the-code&state={query['state']}"
        threading.Thread(target=lambda: real_urlopen(target, timeout=5).read(), daemon=True).start()
        return True

    def urlopen(self, req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        if url.endswith("/token"):
            form = {k: v[0] for k, v in parse_qs(req.data.decode()).items()}
            self.token_requests.append(form)
            assert form["client_secret"] == "shh"
            if form["grant_type"] == "authorization_code":
                verifier = form["code_verifier"]
                expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                assert expected == self.challenge  # PKCE: the verifier matches what the sign-in link carried
                body = {"access_token": "ya29.first", "refresh_token": "1//refresh", "expires_in": 3599,
                        "scope": self.granted, "hd": "acme.io"}
            else:
                self.issued += 1
                body = {"access_token": f"ya29.refreshed-{self.issued}", "expires_in": 3599,
                        "refresh_token": f"1//rotated-{self.issued}"}
            return FakeResponse(json.dumps(body).encode())
        if "userinfo" in url:
            return FakeResponse(json.dumps({"email": "dan@gmail.com"}).encode())
        raise AssertionError(url)


@pytest.fixture
def google(monkeypatch):
    fake = FakeGoogle()
    monkeypatch.setattr(localauth, "urlopen", fake.urlopen)
    monkeypatch.setattr(localauth.webbrowser, "open", fake.browser)
    return fake


def gmail() -> Connection:
    return Connection("google", provider="google", scopes=["gmail.modify"])


def test_connect_signs_in_and_keeps_tokens_only_for_you(google):
    entry = localauth.connect(gmail(), port=free_port(), out=io.StringIO())
    assert entry["account"] == "dan@gmail.com" and entry["access_token"] == "ya29.first"
    path = localauth.store_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    stored = json.loads(path.read_text())
    assert stored["google|client-123"]["refresh_token"] == "1//refresh"
    assert stored["google|client-123"]["extra"] == {"hd": "acme.io"}


def test_unticked_permission_is_refused(google):
    google.granted = "openid https://www.googleapis.com/auth/userinfo.email"
    with pytest.raises(localauth.LocalAuthError, match="gmail.modify"):
        localauth.connect(gmail(), port=free_port(), out=io.StringIO())
    assert not localauth.store_path().exists()


def test_missing_client_credentials(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET")
    with pytest.raises(localauth.LocalAuthError, match="GOOGLE_CLIENT_SECRET in .env"):
        localauth.connect(gmail(), port=free_port(), out=io.StringIO())


def test_a_redirect_with_the_wrong_state_is_ignored():
    port = free_port()
    catcher = localauth._Catcher(port, "right-state")
    page = real_urlopen(f"http://localhost:{port}/callback?code=x&state=wrong", timeout=5).read().decode()
    assert "isn't from your terminal" in page
    assert catcher.wait(0) is None


def test_provide_puts_the_token_in_the_environment(google):
    conn = gmail()
    localauth.connect(conn, port=free_port(), out=io.StringIO())
    out = io.StringIO()
    localauth.provide([conn], out=out)
    assert os.environ["GOOGLE_ACCESS_TOKEN"] == "ya29.first"
    assert conn.token == "ya29.first" and conn.account == "dan@gmail.com"
    assert "Using your local Google connection (dan@gmail.com)" in out.getvalue()


def test_a_token_you_set_yourself_wins(google, monkeypatch):
    conn = gmail()
    localauth.connect(conn, port=free_port(), out=io.StringIO())
    monkeypatch.setenv("GOOGLE_ACCESS_TOKEN", "mine")
    localauth.provide([conn], out=io.StringIO())
    assert os.environ["GOOGLE_ACCESS_TOKEN"] == "mine"


def test_expiring_tokens_are_refreshed_and_rotation_is_kept(google):
    conn = gmail()
    localauth.connect(conn, port=free_port(), out=io.StringIO())
    store = json.loads(localauth.store_path().read_text())
    store["google|client-123"]["expires_at"] = time.time() + 60
    localauth._write_store(store)
    localauth.provide([conn], out=io.StringIO())
    assert os.environ["GOOGLE_ACCESS_TOKEN"] == "ya29.refreshed-1"
    saved = json.loads(localauth.store_path().read_text())["google|client-123"]
    assert saved["refresh_token"] == "1//rotated-1" and saved["expires_at"] > time.time() + 3000


def test_no_connection_or_new_scopes_say_what_to_run(google):
    out = io.StringIO()
    localauth.provide([gmail()], out=out)
    assert "call connect google" in out.getvalue() and "GOOGLE_ACCESS_TOKEN" not in os.environ

    localauth.connect(gmail(), port=free_port(), out=io.StringIO())
    connection_registry.connections.clear()
    more = Connection("google", provider="google", scopes=["gmail.modify", "calendar.readonly"])
    out = io.StringIO()
    localauth.provide([more], out=out)
    assert "missing permissions" in out.getvalue() and "GOOGLE_ACCESS_TOKEN" not in os.environ


def test_disconnect_forgets(google):
    conn = gmail()
    localauth.connect(conn, port=free_port(), out=io.StringIO())
    assert localauth.disconnect(conn) is True
    assert localauth.disconnect(conn) is False
    out = io.StringIO()
    localauth.provide([conn], out=out)
    assert "call connect google" in out.getvalue()


def test_status_says_what_each_connection_needs(google, monkeypatch):
    conn = gmail()
    assert localauth.status(conn) == ("missing", "not connected")
    localauth.connect(conn, port=free_port(), out=io.StringIO())
    assert localauth.status(conn) == ("connected", "dan@gmail.com")
    connection_registry.connections.clear()
    more = Connection("google", provider="google", scopes=["gmail.modify", "calendar.readonly"])
    assert localauth.status(more)[0] == "reconnect"
    monkeypatch.delenv("GOOGLE_CLIENT_ID")
    connection_registry.connections.clear()
    assert localauth.status(Connection("slack", provider="slack"))[0] == "setup"


def test_picker_keys():
    from callables.picker import DOWN, ENTER, QUIT, UP, step
    assert step(DOWN, 0, 3) == (1, None)
    assert step(UP, 0, 3) == (2, None)  # wraps around
    assert step(ENTER, 1, 3) == (1, True)
    assert step("2", 0, 3) == (1, True)  # a number picks straight away
    assert step("7", 0, 3) == (0, None)  # out of range: ignored
    assert step(QUIT, 1, 3) == (1, False)


def test_menu_starts_on_what_needs_connecting(google, monkeypatch, capsys):
    from callables import cli, picker
    conn = gmail()
    localauth.connect(conn, port=free_port(), out=io.StringIO())
    monkeypatch.setenv("SLACK_CLIENT_ID", "a")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "b")
    slack = Connection("slack", provider="slack", scopes=["chat:write"])
    shown = {}

    def fake_pick(rows, hint, start=0):
        shown.update(rows=rows, start=start)
        return start

    monkeypatch.setattr(picker, "pick", fake_pick)
    assert cli._choose_connection([conn, slack], "connect") is slack
    assert shown["start"] == 1
    assert shown["rows"][0].startswith("✓ google") and "dan@gmail.com" in shown["rows"][0]
    assert shown["rows"][1].startswith("· slack")
    monkeypatch.setattr(picker, "pick", lambda rows, hint, start=0: None)
    assert cli._choose_connection([conn, slack], "connect") is None


def test_without_a_terminal_it_only_lists(google, monkeypatch, capsys):
    from callables import cli
    cli._list_connections([gmail()])
    assert "1. · google" in capsys.readouterr().out


def test_function_commands_take_root():
    from callables.cli import _pop_root
    assert _pop_root(["gmail", "subscriptions", "--root", "gmail", "--days", "3"]) == (
        ["gmail", "subscriptions", "--days", "3"], "gmail")
    assert _pop_root(["--root=gmail", "gmail"]) == (["gmail"], "gmail")
    assert _pop_root(["gmail"]) == (["gmail"], None)


def test_secret_values_come_from_env(monkeypatch):
    from callables import Secret
    conn = Connection("outlook", provider="microsoft", scopes=["Mail.Read"],
                      values={"tenant": Secret("MS_TENANT_ID", default="common")})
    assert localauth._url_values(conn, {}) == {"tenant": "common"}
    monkeypatch.setenv("MS_TENANT_ID", "1111-2222")
    assert localauth._fill(conn.token_url, localauth._url_values(conn, {})) == \
        "https://login.microsoftonline.com/1111-2222/oauth2/v2.0/token"
    monkeypatch.setenv("MS_TENANT_ID", "evil.com/x?")
    with pytest.raises(localauth.LocalAuthError, match="MS_TENANT_ID"):
        localauth._url_values(conn, {})
    connection_registry.connections.clear()
    required = Connection("outlook", provider="microsoft", values={"tenant": Secret("NEEDED_TENANT")})
    with pytest.raises(localauth.LocalAuthError, match="NEEDED_TENANT in .env"):
        localauth._url_values(required, {})