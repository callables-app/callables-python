"""`call connect`: sign in to a connection's service on this machine, so `call local` runs with your own account.

The standard sign-in for command-line tools (RFC 8252): a local web server catches the redirect at
http://localhost:<port>/callback, and the code is exchanged here with your OAuth app's client ID and secret from
`.env`. Nothing goes through Callables. Tokens are kept in your user config folder, readable only by you, and
`call local` refreshes them as needed. On the platform none of this runs: Callables injects tokens itself.
"""
import base64
import hashlib
import json
import os
import secrets
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

from callables.connections import CONNECTIONS_ENV, PLACEHOLDER, Connection

DEFAULT_PORT = 8765
WAIT_SECONDS = 300
REFRESH_MARGIN = 600  # refresh when less than 10 minutes are left
SIGN_IN_SCOPES = {"openid", "email", "profile", "offline_access", "user.read", "userinfo.email", "userinfo.profile"}

DONE_PAGE = """<!doctype html><meta charset="utf-8"><title>Callables</title>
<body style="font-family:system-ui;max-width:32rem;margin:15vh auto;text-align:center">
<h2>{title}</h2><p>{body}</p></body>"""


class LocalAuthError(Exception):
    pass


# ---------- where tokens are kept ----------

def store_path() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home())
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "callables" / "connections.json"


def _read_store() -> dict[str, Any]:
    try:
        return json.loads(store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_store(data: dict[str, Any]) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _key(conn: Connection, client_id: str) -> str:
    """One entry per connection and OAuth app: switching apps, or two projects with the same name, don't collide."""
    return f"{conn.name}|{client_id}"


# ---------- the OAuth pieces ----------

def _client(conn: Connection) -> tuple[str, str]:
    client_id = os.environ.get(conn.client_id_secret, "")
    client_secret = os.environ.get(conn.client_secret_secret, "")
    missing = [n for n, v in ((conn.client_id_secret, client_id), (conn.client_secret_secret, client_secret)) if not v]
    if missing:
        raise LocalAuthError(f"Put your OAuth app's {' and '.join(missing)} in .env (the same values as the "
                             "project's secrets on Callables).")
    return client_id, client_secret


def _fill(url: str, values: dict[str, str]) -> str:
    return PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), url)


def _dig(data: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(data, dict):
            return None
        data = data.get(part)
    return data


def _strip_tokens(data: Any) -> Any:
    if isinstance(data, dict):
        return {k: _strip_tokens(v) for k, v in data.items()
                if "token" not in k.lower() and k not in ("expires_in", "scope", "code")}
    if isinstance(data, list):
        return [_strip_tokens(v) for v in data]
    return data


def _scope_key(scope: str) -> str:
    return scope.strip().lower().rstrip("/").rsplit("/", 1)[-1]


def missing_scopes(requested: list[str], granted: list[str] | None) -> list[str]:
    """Scopes left unticked on the consent screen; unknown (the service didn't say) counts as all granted."""
    if granted is None:
        return []
    have = {_scope_key(s) for s in granted}
    return [s for s in requested if _scope_key(s) not in have and _scope_key(s) not in SIGN_IN_SCOPES]


def _post(url: str, data: dict[str, str], conn: Connection, client_id: str, client_secret: str) -> dict[str, Any]:
    headers = {"Accept": "application/json", "User-Agent": "callables-cli"}
    if conn.token_endpoint_auth_method == "client_secret_basic":
        headers["Authorization"] = "Basic " + base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    else:
        data = {**data, "client_id": client_id, "client_secret": client_secret}
    if conn.token_request_format == "json":
        body, headers["Content-Type"] = json.dumps(data).encode(), "application/json"
    else:
        body, headers["Content-Type"] = urlencode(data).encode(), "application/x-www-form-urlencoded"
    host = urlsplit(url).hostname
    try:
        with urlopen(Request(url, data=body, headers=headers, method="POST"), timeout=30) as resp:
            text = resp.read().decode()
    except HTTPError as exc:
        text = exc.read().decode(errors="replace")
        payload = _parse(text)
        raise LocalAuthError(f"{host} refused: {payload.get('error_description') or payload.get('error') or exc}")
    except URLError as exc:
        raise LocalAuthError(f"Couldn't reach {host}: {exc.reason}")
    payload = _parse(text)
    if payload.get("error") or payload.get("ok") is False:
        raise LocalAuthError(f"{host} refused: {payload.get('error_description') or payload.get('error')}")
    return payload


def _parse(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except ValueError:
        data = {k: v[0] for k, v in parse_qs(text).items()}
    return data if isinstance(data, dict) else {}


def _tokens(conn: Connection, body: dict[str, Any], old_refresh: str | None = None) -> dict[str, Any]:
    access = _dig(body, conn.token_path)
    if not access or not isinstance(access, str):
        raise LocalAuthError(f"The service's answer had no access token at '{conn.token_path}'")
    parent = conn.token_path.rpartition(".")[0]
    siblings = _dig(body, parent) if parent else body
    siblings = siblings if isinstance(siblings, dict) else {}
    try:
        expires_at = time.time() + int(siblings["expires_in"]) if siblings.get("expires_in") else None
    except (TypeError, ValueError):
        expires_at = None
    scope = siblings.get("scope") if isinstance(siblings.get("scope"), str) else None
    return {
        "access_token": access,
        "refresh_token": siblings.get("refresh_token") or old_refresh,
        "expires_at": expires_at,
        "granted": [s for s in scope.replace(",", " ").split() if s] if scope else None,
        "extra": _strip_tokens(body),
        "raw": body,
    }


def _account(conn: Connection, tokens: dict[str, Any]) -> str | None:
    if not conn.userinfo_url:
        value = _dig(tokens["raw"], conn.account_field)
        return str(value) if value else None
    try:
        req = Request(conn.userinfo_url, headers={"Authorization": f"Bearer {tokens['access_token']}",
                                                  "Accept": "application/json", "User-Agent": "callables-cli"})
        with urlopen(req, timeout=15) as resp:
            value = _dig(json.loads(resp.read().decode()), conn.account_field)
    except (OSError, ValueError):
        return None
    return str(value) if value else None


# ---------- the local redirect ----------

class _Catcher:
    """Waits for the browser to come back to http://localhost:<port>/callback, on IPv4 and IPv6."""

    def __init__(self, port: int, state: str):
        self.state, self.result, self.done = state, None, threading.Event()
        catcher = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 (http.server's naming)
                parts = urlsplit(self.path)
                if parts.path != "/callback":
                    self.send_response(404)
                    self.end_headers()
                    return
                query = {k: v[0] for k, v in parse_qs(parts.query).items()}
                if query.get("state") != catcher.state:
                    page = DONE_PAGE.format(title="This sign-in isn't from your terminal",
                                            body="Run <code>call connect</code> again.")
                elif query.get("error"):
                    catcher.result = query
                    page = DONE_PAGE.format(title="Not connected", body="You can close this tab and go back to "
                                            "your terminal.")
                else:
                    catcher.result = query
                    page = DONE_PAGE.format(title="Connected", body="You can close this tab and go back to your "
                                            "terminal.")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(page.encode())
                if catcher.result is not None:
                    catcher.done.set()

            def log_message(self, *args):  # keep the terminal quiet
                pass

        class V6Server(HTTPServer):
            address_family = socket.AF_INET6

        self.servers = []
        for cls, host in ((HTTPServer, "127.0.0.1"), (V6Server, "::1")):
            try:
                self.servers.append(cls((host, port), Handler))
            except OSError:
                continue  # no IPv6 here, or the other one already holds it
        if not self.servers:
            raise LocalAuthError(f"Port {port} is in use. Pick another with --port (and add that redirect URI to "
                                 "your OAuth app).")
        for server in self.servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def wait(self, seconds: int) -> dict[str, str] | None:
        self.done.wait(seconds)
        for server in self.servers:
            server.shutdown()
            server.server_close()
        return self.result


def connect(conn: Connection, port: int = DEFAULT_PORT, open_browser: bool = True,
            ask: Callable[[str], str] = input, out=sys.stderr) -> dict[str, Any]:
    """Signs in through the browser and keeps the tokens. Returns the stored entry."""
    client_id, client_secret = _client(conn)
    values = {name: ask(f"{prompt}: ").strip() for name, prompt in conn.placeholders.items()}
    redirect_uri = f"http://localhost:{port}/callback"
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "state": state,
              **conn.authorize_params}
    if conn.scopes:
        params[conn.scope_param] = conn.scope_separator.join(conn.scopes)
    if conn.pkce:
        params["code_challenge"] = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(
            b"=").decode()
        params["code_challenge_method"] = "S256"
    url = f"{_fill(conn.authorize_url, values)}?{urlencode(params)}"

    catcher = _Catcher(port, state)
    print(f"Your OAuth app needs this redirect URI (besides Callables' own): {redirect_uri}", file=out)
    print(f"Opening {conn.title} sign-in. If no browser opens, go to:\n  {url}", file=out)
    if conn.provider == "google":
        print("On Google's screen, tick every box (or Select all).", file=out)
    if open_browser:
        webbrowser.open(url)
    result = catcher.wait(WAIT_SECONDS)
    if result is None:
        raise LocalAuthError(f"Didn't hear back from {conn.title} within {WAIT_SECONDS // 60} minutes. If the "
                             f"browser showed redirect_uri_mismatch, add {redirect_uri} to your OAuth app.")
    if result.get("error"):
        raise LocalAuthError(f"{conn.title} said: {result.get('error_description') or result['error']}")

    data = {"grant_type": "authorization_code", "code": result["code"], "redirect_uri": redirect_uri}
    if conn.pkce:
        data["code_verifier"] = verifier
    tokens = _tokens(conn, _post(_fill(conn.token_url, values), data, conn, client_id, client_secret))
    missing = missing_scopes(conn.scopes, tokens["granted"])
    if missing:
        raise LocalAuthError(f"{conn.title} didn't grant every permission this connection asks for (missing: "
                             f"{', '.join(missing)}). Run call connect again and tick every box.")
    entry = {
        "name": conn.name,
        "title": conn.title,
        "client_id": client_id,
        "account": _account(conn, tokens),
        "scopes": conn.scopes,
        "values": values,
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "expires_at": tokens["expires_at"],
        "extra": tokens["extra"],
        "connected_at": time.time(),
    }
    store = _read_store()
    store[_key(conn, client_id)] = entry
    _write_store(store)
    return entry


def disconnect(conn: Connection) -> bool:
    """Forgets this machine's tokens for the connection (every OAuth app it was connected through)."""
    store = _read_store()
    keys = [k for k, v in store.items() if v.get("name") == conn.name]
    for key in keys:
        del store[key]
    if keys:
        _write_store(store)
    return bool(keys)


def _find(conn: Connection) -> tuple[str, dict[str, Any]] | None:
    store = _read_store()
    client_id = os.environ.get(conn.client_id_secret)
    if client_id and _key(conn, client_id) in store:
        return _key(conn, client_id), store[_key(conn, client_id)]
    matches = [(k, v) for k, v in store.items() if v.get("name") == conn.name and not client_id]
    return max(matches, key=lambda kv: kv[1].get("connected_at", 0)) if matches else None


def status(conn: Connection) -> tuple[str, str]:
    """(state, detail) for `call connect`'s list. state: connected, reconnect, missing or setup."""
    missing_creds = [n for n in (conn.client_id_secret, conn.client_secret_secret) if not os.environ.get(n)]
    found = _find(conn)
    if found is None:
        if missing_creds:
            return "setup", f"put {' and '.join(missing_creds)} in .env first"
        return "missing", "not connected"
    _, entry = found
    account = entry.get("account") or "connected"
    if missing_scopes(conn.scopes, entry.get("scopes")):
        return "reconnect", f"{account}, but the code now asks for more permissions"
    if entry.get("expires_at") and entry["expires_at"] < time.time() and not entry.get("refresh_token"):
        return "reconnect", f"{account}, access expired"
    if missing_creds:
        return "setup", f"{account}, but {' and '.join(missing_creds)} isn't in .env (needed to refresh)"
    return "connected", account


def provide(conns: list[Connection], out=sys.stderr) -> None:
    """Before a local run: puts each connection's token in the environment, as the platform would. A token already
    set (in the shell or .env) wins. Problems are reported, not raised: the function's own `.token` says what's
    missing if it needs it."""
    details = json.loads(os.environ.get(CONNECTIONS_ENV) or "{}")
    for conn in conns:
        if os.environ.get(conn.env_var):
            continue
        found = _find(conn)
        if found is None:
            print(f"No local {conn.title} connection. Run: call connect {conn.name}", file=out)
            continue
        key, entry = found
        if missing_scopes(conn.scopes, entry.get("scopes")):
            print(f"Your local {conn.title} connection is missing permissions the code now asks for. Run: "
                  f"call connect {conn.name}", file=out)
            continue
        if entry.get("expires_at") and entry["expires_at"] - time.time() < REFRESH_MARGIN:
            try:
                entry = _refresh(conn, key, entry)
            except LocalAuthError as exc:
                print(f"Couldn't refresh your local {conn.title} connection ({exc}). Run: call connect {conn.name}",
                      file=out)
                continue
        os.environ[conn.env_var] = entry["access_token"]
        details[conn.name] = {"account": entry.get("account"), "scopes": entry.get("scopes") or [],
                              "extra": entry.get("extra") or {}}
        print(f"Using your local {conn.title} connection ({entry.get('account') or 'connected'})", file=out)
    if details:
        os.environ[CONNECTIONS_ENV] = json.dumps(details)


def _refresh(conn: Connection, key: str, entry: dict[str, Any]) -> dict[str, Any]:
    if not entry.get("refresh_token"):
        raise LocalAuthError("access expired and the service gave no refresh token")
    client_id, client_secret = _client(conn)
    body = _post(_fill(conn.token_url, entry.get("values") or {}),
                 {"grant_type": "refresh_token", "refresh_token": entry["refresh_token"]},
                 conn, client_id, client_secret)
    tokens = _tokens(conn, body, old_refresh=entry["refresh_token"])
    entry = {**entry, "access_token": tokens["access_token"], "refresh_token": tokens["refresh_token"],
             "expires_at": tokens["expires_at"], "extra": {**(entry.get("extra") or {}), **tokens["extra"]}}
    store = _read_store()
    store[key] = entry
    _write_store(store)
    return entry
