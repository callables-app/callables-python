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
        # Multi-tenant apps sign in through "common"; a single-tenant app passes its Directory (tenant) ID in values
        "defaults": {"tenant": "common"},
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


SECRET_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# A filled-in value becomes part of a hostname or path, so it can't change the URL's shape
VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,99}$")


class Secret:
    """A value read when it's needed rather than written in code: a project secret on Callables, and an environment
    variable (or `.env`) locally. Use it for a connection's client ID and secret, and for URL values such as a
    Microsoft tenant ID: `values={"tenant": Secret("MS_TENANT_ID", default="common")}`.

    It's a reference, not a read: Callables reads connection settings when it builds (before secrets exist) and signs
    people in without running your code, so it needs the variable's name and looks the value up itself.
    """

    def __init__(self, name: str, default: str | None = None):
        if not isinstance(name, str) or not SECRET_NAME.match(name):
            raise ValueError(f"Secret({name!r}): the name must be an environment variable name, like MS_TENANT_ID")
        if default is not None and (not isinstance(default, str) or not VALUE.match(default)):
            raise ValueError(f"Secret({name!r}, default={default!r}): the default must be a plain value, like 'common'")
        self.name = name
        self.default = default

    def __repr__(self) -> str:
        return f"Secret({self.name!r}" + (f", default={self.default!r})" if self.default is not None else ")")


class Ask:
    """A value each person types when they connect, such as their Shopify store: `values={"shop": Ask("Your store
    name")}`. On Callables the Connect button shows a text box with the prompt; locally `call connect` asks in the
    terminal. Their answer is saved with their own connection."""

    def __init__(self, prompt: str):
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Ask() needs a prompt to show people, such as Ask('Your store name')")
        self.prompt = prompt.strip()

    def __repr__(self) -> str:
        return f"Ask({self.prompt!r})"


class ConnectionNotAvailable(RuntimeError):
    pass


class Connection:
    """An account each person connects themselves, through your own OAuth app.

    Declare it at module level and pass it to `@deploy(connections=[...])`. During a run, `.token` is an access token
    for the person who started it. The app's client ID and secret come from secrets: by default `<NAME>_CLIENT_ID`
    and `<NAME>_CLIENT_SECRET`, or name your own with `client_id=Secret("...")` and `client_secret=Secret("...")`.

    Any `{placeholder}` in the URLs or `authorize_params` is filled from `values`: `Secret(...)` for a value that's
    the same for everyone (a Microsoft tenant ID), `Ask(...)` for one each person types when they connect (their
    Shopify store), or a plain string. Presets fill some themselves, like Microsoft's tenant ("common").
    """

    def __init__(
        self,
        name: str,
        *,
        provider: str | None = None,
        scopes: list[str] | None = None,
        client_id: Secret | None = None,
        client_secret: Secret | None = None,
        values: dict[str, "Secret | Ask | str"] | None = None,
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
        # Older spellings, still accepted: placeholders={"shop": "prompt"} is values={"shop": Ask("prompt")},
        # tenant="..." is values={"tenant": "..."}, client_id_secret="X" is client_id=Secret("X")
        placeholders: dict[str, str] | None = None,
        tenant: str | None = None,
        client_id_secret: str | None = None,
        client_secret_secret: str | None = None,
    ):
        if not NAME.match(name):
            raise ValueError(f"Connection name '{name}' must be lowercase letters, digits or '_', starting with a "
                             "letter")
        preset: dict[str, Any] = {}
        if provider is not None:
            if provider not in PRESETS:
                raise ValueError(f"Connection '{name}': unknown provider '{provider}'. Presets: "
                                 f"{', '.join(sorted(PRESETS))}. For another service, leave out provider and give "
                                 "authorize_url and token_url")
            preset = PRESETS[provider]

        def pick(value: Any, key: str, default: Any = None) -> Any:
            return value if value is not None else preset.get(key, default)

        self.name = name
        self.provider = provider
        self.title = title or preset.get("title") or name.replace("_", " ").capitalize()
        self.client_id_secret = self._credential(name, "client_id", client_id, client_id_secret,
                                                 f"{name.upper()}_CLIENT_ID")
        self.client_secret_secret = self._credential(name, "client_secret", client_secret, client_secret_secret,
                                                     f"{name.upper()}_CLIENT_SECRET")
        self.authorize_url = pick(authorize_url, "authorize_url")
        self.token_url = pick(token_url, "token_url")
        if not self.authorize_url or not self.token_url:
            raise ValueError(f"Connection '{name}' needs a provider, or authorize_url and token_url")
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
        self.scopes = list(dict.fromkeys([*preset.get("default_scopes", []),
                                          *(self._expand(s) for s in (scopes or []))]))
        self._fill_values(self._gather_values(values, placeholders, tenant), preset.get("defaults") or {})

        for label, url in self._urls():
            if not url.startswith("https://"):
                raise ValueError(f"Connection '{name}': {label} must start with https://")
        _register(self)

    @staticmethod
    def _credential(name: str, label: str, given: "Secret | None", old_name: str | None, default: str) -> str:
        """The name of the secret holding the client ID or secret."""
        if given is not None and old_name is not None:
            raise ValueError(f"Connection '{name}': give {label}=Secret(...) or {label}_secret=..., not both")
        if given is None:
            return old_name or default
        if not isinstance(given, Secret):
            raise ValueError(f"Connection '{name}': {label} must be Secret(\"NAME\"), the secret that holds it, so "
                             "it never appears in your code")
        if given.default is not None:
            raise ValueError(f"Connection '{name}': {label} can't have a default")
        return given.name

    def _gather_values(self, values: dict | None, placeholders: dict | None, tenant: str | None) -> dict[str, Any]:
        gathered: dict[str, Any] = {}
        for key, value in (values or {}).items():
            if not isinstance(key, str) or not NAME.match(key):
                raise ValueError(f"Connection '{self.name}': value name {key!r} must be lowercase letters, digits or "
                                 "'_', matching a {placeholder} in its URLs")
            if not isinstance(value, (Secret, Ask, str)):
                raise ValueError(f"Connection '{self.name}': values['{key}'] must be Secret(...), Ask(...) or a "
                                 "string")
            gathered[key] = value
        for key, prompt in (placeholders or {}).items():
            if key in gathered:
                raise ValueError(f"Connection '{self.name}': '{key}' is in both values and placeholders")
            gathered[key] = Ask(prompt)
        if tenant is not None:
            if "tenant" in gathered:
                raise ValueError(f"Connection '{self.name}': give the tenant in values or tenant=, not both")
            gathered["tenant"] = tenant
        return gathered

    def _templates(self) -> list[tuple[str, str]]:
        """Every place a {placeholder} may appear: the URLs and the sign-in parameters."""
        out = [(label, url) for label, url in self._urls()]
        out += [(f"authorize_params['{k}']", v) for k, v in self.authorize_params.items() if isinstance(v, str)]
        return out

    def _urls(self) -> list[tuple[str, str]]:
        return [(label, url) for label, url in (("authorize_url", self.authorize_url), ("token_url", self.token_url),
                                                ("userinfo_url", self.userinfo_url), ("revoke_url", self.revoke_url))
                if url is not None]

    def _fill_values(self, given: dict[str, Any], defaults: dict[str, str]) -> None:
        """Fills plain values now, and records which come from secrets or from the person connecting."""
        used = {p for _, text in self._templates() for p in PLACEHOLDER.findall(text)}
        unused = [k for k in given if k not in used]
        if unused:
            raise ValueError(f"Connection '{self.name}': values['{unused[0]}'] doesn't match a {{{unused[0]}}} in its "
                             f"URLs or authorize_params" + (f" (they use: {', '.join(sorted(used))})" if used else ""))
        values = {**{k: v for k, v in defaults.items() if k in used}, **given}
        missing = sorted(used - set(values))
        if missing:
            raise ValueError(f"Connection '{self.name}': its URLs use {{{missing[0]}}}, but nothing fills it in. Add "
                             f"values={{'{missing[0]}': Secret('...')}} (the same for everyone), or "
                             f"Ask('...') (each person types it when they connect)")
        literal = {}
        self.placeholders: dict[str, str] = {}
        self.secret_placeholders: dict[str, list[str | None]] = {}
        for key, value in values.items():
            if isinstance(value, Ask):
                self.placeholders[key] = value.prompt
            elif isinstance(value, Secret):
                self.secret_placeholders[key] = [value.name, value.default]
            else:
                if not VALUE.match(value):
                    raise ValueError(f"Connection '{self.name}': values['{key}'] = {value!r} must be a plain value "
                                     "(letters, digits, '.' and '-')")
                literal[key] = value
        if literal:
            def fill(text: str) -> str:
                return PLACEHOLDER.sub(lambda m: literal.get(m.group(1), m.group(0)), text)
            self.authorize_url, self.token_url = fill(self.authorize_url), fill(self.token_url)
            self.userinfo_url = fill(self.userinfo_url) if self.userinfo_url else None
            self.revoke_url = fill(self.revoke_url) if self.revoke_url else None
            self.authorize_params = {k: fill(v) if isinstance(v, str) else v for k, v in self.authorize_params.items()}

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
            "secret_placeholders": self.secret_placeholders,
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
