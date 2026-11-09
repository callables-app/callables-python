"""OAuth connections: each person running a function connects their own account (their Gmail, their Slack), and every
run they start gets a fresh access token for it. The developer brings the OAuth app; the platform signs people in,
stores and refreshes their tokens, and injects the token into each run.

    google = Connection("google", provider="google", scopes=["gmail.modify"])

    @deploy(connections=[google])
    def triage_inbox() -> str:
        token = google.token
"""
import json
import os
import re
from typing import Any

NAME = re.compile(r"^[a-z][a-z0-9_]*$")
PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")
AUTH_METHODS = ("client_secret_post", "client_secret_basic")
# The run's connection details (account, scopes, extra) as JSON, keyed by connection name; tokens are separate
CONNECTIONS_ENV = "CALLABLES_CONNECTIONS"

GOOGLE_SCOPE_BASE = "https://www.googleapis.com/auth/"
GOOGLE_PLAIN_SCOPES = {"openid", "email", "profile"}

# Each service's sign-in details. Anything here can be overridden on the Connection.
PRESETS: dict[str, dict[str, Any]] = {
    "google": {
        "title": "Google",
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "revoke_url": "https://oauth2.googleapis.com/revoke",
        "userinfo_url": "https://openidconnect.googleapis.com/v1/userinfo",
        "account_field": "email",
        # offline + consent: Google only hands out a refresh token when asked for both
        "authorize_params": {"access_type": "offline", "prompt": "consent", "include_granted_scopes": "true"},
        "default_scopes": ["openid", "email"],
        "pkce": True,
    },
    "microsoft": {
        "title": "Microsoft",
        "authorize_url": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        "userinfo_url": "https://graph.microsoft.com/v1.0/me",
        "account_field": "userPrincipalName",
        "default_scopes": ["offline_access", "User.Read"],
        "pkce": True,
    },
    "slack": {
        "title": "Slack",
        "authorize_url": "https://slack.com/oauth/v2/authorize",
        "token_url": "https://slack.com/api/oauth.v2.access",
        "revoke_url": "https://slack.com/api/auth.revoke",
        "userinfo_url": "https://slack.com/api/auth.test",
        "account_field": "user",
        # User tokens (acting as the person), not the app's bot token
        "scope_param": "user_scope",
        "scope_separator": ",",
        "token_path": "authed_user.access_token",
    },
    "github": {
        "title": "GitHub",
        "authorize_url": "https://github.com/login/oauth/authorize",
        "token_url": "https://github.com/login/oauth/access_token",
        "userinfo_url": "https://api.github.com/user",
        "account_field": "login",
    },
    "notion": {
        "title": "Notion",
        "authorize_url": "https://api.notion.com/v1/oauth/authorize",
        "token_url": "https://api.notion.com/v1/oauth/token",
        "authorize_params": {"owner": "user"},
        "token_endpoint_auth_method": "client_secret_basic",
        "token_request_format": "json",
        "account_field": "workspace_name",  # read from the token response: Notion has no userinfo for users
    },
}


class ConnectionNotAvailable(RuntimeError):
    pass


class Connection:
    """An account each person connects themselves, through your own OAuth app.

    Declare it at module level and pass it to `@deploy(connections=[...])`. During a run, `.token` is an access token
    for the person who started it. The app's client ID and secret come from the project secrets
    `<NAME>_CLIENT_ID` and `<NAME>_CLIENT_SECRET` (override with `client_id_secret` / `client_secret_secret`).
    """

    def __init__(
        self,
        name: str,
        *,
        provider: str | None = None,
        scopes: list[str] | None = None,
        title: str | None = None,
        authorize_url: str | None = None,
        token_url: str | None = None,
        authorize_params: dict[str, str] | None = None,
        token_endpoint_auth_method: str | None = None,
        token_request_format: str | None = None,
        scope_separator: str | None = None,
        scope_param: str | None = None,
        pkce: bool | None = None,
        token_path: str | None = None,
        userinfo_url: str | None = None,
        account_field: str | None = None,
        revoke_url: str | None = None,
        placeholders: dict[str, str] | None = None,
        tenant: str | None = None,
        client_id_secret: str | None = None,
        client_secret_secret: str | None = None,
    ):
        if not NAME.match(name):
            raise ValueError(f"Connection name '{name}' must be lowercase letters, digits or '_', starting with a "
                             "letter (it names the GOOGLE_CLIENT_ID-style secrets)")
        preset: dict[str, Any] = {}
        if provider is not None:
            if provider not in PRESETS:
                raise ValueError(f"Connection '{name}': unknown provider '{provider}'. Presets: "
                                 f"{', '.join(sorted(PRESETS))}. For another service, leave out provider and give "
                                 "authorize_url and token_url")
            preset = PRESETS[provider]
        if tenant is not None and provider != "microsoft":
            raise ValueError(f"Connection '{name}': tenant only applies to provider='microsoft'")

        def pick(value: Any, key: str, default: Any = None) -> Any:
            return value if value is not None else preset.get(key, default)

        self.name = name
        self.provider = provider
        self.title = title or preset.get("title") or name.replace("_", " ").capitalize()
        self.authorize_url = pick(authorize_url, "authorize_url")
        self.token_url = pick(token_url, "token_url")
        if not self.authorize_url or not self.token_url:
            raise ValueError(f"Connection '{name}' needs a provider, or authorize_url and token_url")
        if provider == "microsoft":
            # Multi-tenant apps sign in through "common"; a single-tenant app needs its own tenant
            self.authorize_url = self.authorize_url.replace("{tenant}", tenant or "common")
            self.token_url = self.token_url.replace("{tenant}", tenant or "common")
        self.authorize_params = {**preset.get("authorize_params", {}), **(authorize_params or {})}
        self.token_endpoint_auth_method = pick(token_endpoint_auth_method, "token_endpoint_auth_method",
                                               "client_secret_post")
        if self.token_endpoint_auth_method not in AUTH_METHODS:
            raise ValueError(f"Connection '{name}': token_endpoint_auth_method must be one of {', '.join(AUTH_METHODS)}")
        self.token_request_format = pick(token_request_format, "token_request_format", "form")
        if self.token_request_format not in ("form", "json"):
            raise ValueError(f"Connection '{name}': token_request_format must be 'form' or 'json'")
        self.scope_separator = pick(scope_separator, "scope_separator", " ")
        self.scope_param = pick(scope_param, "scope_param", "scope")
        self.pkce = bool(pick(pkce, "pkce", False))
        self.token_path = pick(token_path, "token_path", "access_token")
        self.userinfo_url = pick(userinfo_url, "userinfo_url")
        self.account_field = pick(account_field, "account_field", "email")
        self.revoke_url = pick(revoke_url, "revoke_url")
        self.placeholders = dict(placeholders or {})
        self.client_id_secret = client_id_secret or f"{name.upper()}_CLIENT_ID"
        self.client_secret_secret = client_secret_secret or f"{name.upper()}_CLIENT_SECRET"

        self.scopes = list(dict.fromkeys([*preset.get("default_scopes", []),
                                          *(self._expand(s) for s in (scopes or []))]))

        for label, url in (("authorize_url", self.authorize_url), ("token_url", self.token_url),
                           ("userinfo_url", self.userinfo_url), ("revoke_url", self.revoke_url)):
            if url is None:
                continue
            if not url.startswith("https://"):
                raise ValueError(f"Connection '{name}': {label} must start with https://")
            unknown = [p for p in PLACEHOLDER.findall(url) if p not in self.placeholders]
            if unknown:
                raise ValueError(f"Connection '{name}': {label} uses {{{unknown[0]}}}, which isn't in placeholders. "
                                 f"Add placeholders={{'{unknown[0]}': 'What to ask people'}}")
        unused = [p for p in self.placeholders
                  if not any(f"{{{p}}}" in (u or "") for u in (self.authorize_url, self.token_url))]
        if unused:
            raise ValueError(f"Connection '{name}': placeholder '{unused[0]}' isn't used in authorize_url or token_url")
        _register(self)

    def _expand(self, scope: str) -> str:
        """Presets take short scope names: Google's "gmail.modify" is its full https://www.googleapis.com/... URL."""
        if self.provider == "google" and "://" not in scope and scope not in GOOGLE_PLAIN_SCOPES:
            return GOOGLE_SCOPE_BASE + scope
        return scope

    @property
    def env_var(self) -> str:
        return f"{self.name.upper()}_ACCESS_TOKEN"

    # During a run --------------------------------------------------------------------------------------------------

    @property
    def token(self) -> str:
        """An access token for the person running the function, valid for the whole run."""
        value = os.environ.get(self.env_var)
        if not value:
            raise ConnectionNotAvailable(
                f"Connection '{self.name}' has no token here. On Callables it's set for functions that declare "
                f"connections=[{self.name}]. To run it locally, run `call connect {self.name}` once, or put a token "
                f"for your own account in .env as {self.env_var}")
        return value

    def _details(self) -> dict[str, Any]:
        try:
            return json.loads(os.environ.get(CONNECTIONS_ENV) or "{}").get(self.name) or {}
        except json.JSONDecodeError:
            return {}

    @property
    def account(self) -> str | None:
        """Which account the person connected, such as alice@company.com, when the service says."""
        return self._details().get("account")

    @property
    def scopes_granted(self) -> list[str]:
        """The scopes the person granted."""
        return list(self._details().get("scopes") or [])

    @property
    def extra(self) -> dict[str, Any]:
        """Other values the service returned at sign-in, such as Salesforce's instance_url."""
        return dict(self._details().get("extra") or {})

    def manifest_entry(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "provider": self.provider,
            "scopes": self.scopes,
            "authorize_url": self.authorize_url,
            "token_url": self.token_url,
            "authorize_params": self.authorize_params,
            "token_endpoint_auth_method": self.token_endpoint_auth_method,
            "token_request_format": self.token_request_format,
            "scope_separator": self.scope_separator,
            "scope_param": self.scope_param,
            "pkce": self.pkce,
            "token_path": self.token_path,
            "userinfo_url": self.userinfo_url,
            "account_field": self.account_field,
            "revoke_url": self.revoke_url,
            "placeholders": self.placeholders,
            "client_id_secret": self.client_id_secret,
            "client_secret_secret": self.client_secret_secret,
            "env_var": self.env_var,
        }

    def __repr__(self) -> str:
        return f"Connection({self.name!r})"


# Every Connection(...) made while importing lands here; one name must mean one set of settings
connections: dict[str, Connection] = {}
conflicts: list[str] = []


def _register(conn: Connection) -> None:
    existing = connections.get(conn.name)
    if existing is not None and existing.manifest_entry() != conn.manifest_entry():
        conflicts.append(conn.name)
    connections[conn.name] = conn
