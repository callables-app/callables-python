import json
import textwrap

import pytest

from callables import Ask, Connection, Group, Secret, deploy, registry
from callables import connections as connection_registry
from callables.checks import check_project
from callables.connections import ConnectionNotAvailable
from callables.manifest import build_manifest


@pytest.fixture(autouse=True)
def isolated():
    registry.clear()
    connection_registry.connections.clear()
    connection_registry.conflicts.clear()
    yield
    registry.clear()
    connection_registry.connections.clear()
    connection_registry.conflicts.clear()


def test_google_preset_expands_short_scopes_and_adds_sign_in_scopes():
    google = Connection("google", provider="google", scopes=["gmail.modify", "https://www.googleapis.com/auth/drive.file"])
    entry = google.manifest_entry()
    assert entry["scopes"] == ["openid", "email", "https://www.googleapis.com/auth/gmail.modify",
                               "https://www.googleapis.com/auth/drive.file"]
    assert entry["authorize_params"]["access_type"] == "offline"
    assert entry["pkce"] is True
    assert entry["client_id_secret"] == "GOOGLE_CLIENT_ID"
    assert entry["client_secret_secret"] == "GOOGLE_CLIENT_SECRET"
    assert entry["env_var"] == "GOOGLE_ACCESS_TOKEN"


def test_microsoft_tenant_and_offline_access():
    ms = Connection("outlook", provider="microsoft", scopes=["Mail.ReadWrite"], tenant="contoso.onmicrosoft.com")
    assert ms.authorize_url == "https://login.microsoftonline.com/contoso.onmicrosoft.com/oauth2/v2.0/authorize"
    assert ms.scopes == ["offline_access", "User.Read", "Mail.ReadWrite"]
    # The preset fills {tenant} with "common" unless values says otherwise
    assert Connection("ms", provider="microsoft").token_url.startswith("https://login.microsoftonline.com/common/")


def test_custom_provider_and_overrides():
    crm = Connection("crm", authorize_url="https://login.crm.example/oauth/authorize",
                     token_url="https://login.crm.example/oauth/token", scopes=["contacts.read"],
                     token_endpoint_auth_method="client_secret_basic", scope_separator=",",
                     client_id_secret="CRM_APP_ID")
    entry = crm.manifest_entry()
    assert entry["provider"] is None and entry["title"] == "Crm"
    assert entry["scopes"] == ["contacts.read"]
    assert entry["client_id_secret"] == "CRM_APP_ID" and entry["client_secret_secret"] == "CRM_CLIENT_SECRET"


@pytest.mark.parametrize("kwargs, message", [
    ({"provider": "gogle"}, "unknown provider"),
    ({}, "needs a provider"),
    ({"authorize_url": "http://x.example/a", "token_url": "https://x.example/t"}, "must start with https://"),
    ({"authorize_url": "https://{shop}.example/a", "token_url": "https://x.example/t"}, "nothing fills it in"),
    ({"provider": "google", "placeholders": {"shop": "Store"}}, "doesn't match a {shop}"),
    ({"provider": "google", "values": {"shop": Ask("Store")}}, "doesn't match a {shop}"),
    ({"provider": "google", "client_id": "GOOGLE_ID"}, "must be Secret"),
    ({"provider": "google", "client_id": Secret("A"), "client_id_secret": "B"}, "not both"),
    ({"provider": "microsoft", "values": {"tenant": "evil.com/x"}}, "plain value"),
    ({"provider": "microsoft", "values": {"tenant": 7}}, "must be Secret"),
    ({"provider": "google", "token_endpoint_auth_method": "basic"}, "token_endpoint_auth_method"),
    ({"provider": "google", "tenant": "x"}, "doesn't match a {tenant}"),
])
def test_bad_settings_fail_at_definition(kwargs, message):
    with pytest.raises(ValueError, match=message):
        Connection("svc", **kwargs)


def test_bad_name():
    with pytest.raises(ValueError, match="lowercase"):
        Connection("Google", provider="google")


def test_placeholders():
    shop = Connection("shopify", authorize_url="https://{shop}.myshopify.com/admin/oauth/authorize",
                      token_url="https://{shop}.myshopify.com/admin/oauth/access_token", scopes=["read_orders"],
                      placeholders={"shop": "Your store name"})
    assert shop.manifest_entry()["placeholders"] == {"shop": "Your store name"}


def test_deploy_takes_connection_objects_only():
    with pytest.raises(TypeError, match="Connection objects"):
        deploy(connections=["google"])(lambda: None)


def test_token_and_details_come_from_the_run_environment(monkeypatch):
    google = Connection("google", provider="google")
    with pytest.raises(ConnectionNotAvailable, match="GOOGLE_ACCESS_TOKEN"):
        google.token
    monkeypatch.setenv("GOOGLE_ACCESS_TOKEN", "ya29.abc")
    monkeypatch.setenv("CALLABLES_CONNECTIONS", json.dumps(
        {"google": {"account": "alice@example.com", "scopes": ["openid"], "extra": {"hd": "example.com"}}}))
    assert google.token == "ya29.abc"
    assert google.account == "alice@example.com"
    assert google.scopes_granted == ["openid"]
    assert google.extra == {"hd": "example.com"}


PROJECT = '''
from callables import Connection, Group, deploy

google = Connection("google", provider="google", scopes=["calendar.readonly"])
slack = Connection("slack", provider="slack", scopes=["chat:write"])
unused = Connection("unused", provider="github")

mail = Group("mail", title="Mail", connections=[google])


@mail.deploy(connections=[slack, google])
def digest() -> str:
    """Posts your agenda."""
    return google.token


@deploy
def plain() -> str:
    """No connections."""
    return "ok"
'''


@pytest.fixture
def project(tmp_path, monkeypatch, request):
    module = f"conn_{request.node.name.replace('[', '_').replace(']', '').replace('-', '_')}"
    (tmp_path / f"{module}.py").write_text(textwrap.dedent(PROJECT))
    (tmp_path / "requirements.txt").write_text("")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_manifest_lists_used_connections_and_each_function_names_them(project):
    manifest = build_manifest(project)
    assert not manifest["errors"]
    by_name = {f["name"]: f for f in manifest["functions"]}
    assert by_name["mail.digest"]["connections"] == ["google", "slack"]  # group's first, no repeats
    assert by_name["plain"]["connections"] == []
    assert [c["name"] for c in manifest["connections"]] == ["google", "slack"]  # unused one left out
    slack = next(c for c in manifest["connections"] if c["name"] == "slack")
    assert slack["scope_param"] == "user_scope" and slack["token_path"] == "authed_user.access_token"


def test_check_reports_the_secrets_each_connection_needs(project):
    report = check_project(project)
    assert report["ok"], report["issues"]
    google = next(c for c in report["connections"] if c["name"] == "google")
    assert google["secrets"] == ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"]
    assert google["env_var"] == "GOOGLE_ACCESS_TOKEN"


def test_a_connection_defined_twice_differently_fails_the_manifest(tmp_path):
    (tmp_path / "conn_twice.py").write_text(textwrap.dedent('''
        from callables import Connection, deploy

        a = Connection("google", provider="google", scopes=["gmail.modify"])
        b = Connection("google", provider="google", scopes=["calendar.readonly"])

        @deploy(connections=[a])
        def f() -> str:
            """x"""
            return ""
    '''))
    errors = build_manifest(tmp_path)["errors"]
    assert any("defined twice" in e["error"] for e in errors)


def test_group_rejects_strings():
    with pytest.raises(TypeError):
        Group("mail", connections=["google"])


def test_values_fill_placeholders_from_secrets_asks_or_strings():
    from callables import connections as registry
    registry.connections.clear()
    ms = Connection("outlook", provider="microsoft", scopes=["Mail.Read"], client_id=Secret("MS_CLIENT_ID"),
                    client_secret=Secret("MS_CLIENT_SECRET"), values={"tenant": Secret("MS_TENANT_ID", default="common")})
    entry = ms.manifest_entry()
    assert "{tenant}" in entry["authorize_url"] and entry["secret_placeholders"] == {"tenant": ["MS_TENANT_ID", "common"]}
    assert entry["client_id_secret"] == "MS_CLIENT_ID" and entry["client_secret_secret"] == "MS_CLIENT_SECRET"
    registry.connections.clear()
    assert Connection("o", provider="microsoft", values={"tenant": Secret("T")}).secret_placeholders == {"tenant": ["T", None]}
    registry.connections.clear()
    fixed = Connection("o", provider="microsoft", values={"tenant": "acme.onmicrosoft.com"})
    assert "/acme.onmicrosoft.com/" in fixed.token_url and fixed.secret_placeholders == {}
    # Any service, any placeholder, in URLs or sign-in parameters: no SDK change needed
    okta = Connection("okta", authorize_url="https://{domain}/oauth2/v1/authorize", token_url="https://{domain}/oauth2/v1/token",
                      authorize_params={"audience": "{audience}"},
                      values={"domain": Secret("OKTA_DOMAIN"), "audience": Ask("Your API audience")})
    assert okta.secret_placeholders == {"domain": ["OKTA_DOMAIN", None]} and okta.placeholders == {"audience": "Your API audience"}
    with pytest.raises(ValueError, match="environment variable name"):
        Secret("not valid")
    with pytest.raises(ValueError, match="needs a prompt"):
        Ask("  ")

def test_older_spellings_still_work():
    from callables import connections as registry
    registry.connections.clear()
    shop = Connection("shopify", authorize_url="https://{shop}.myshopify.com/a", token_url="https://{shop}.myshopify.com/t",
                      placeholders={"shop": "Your store"}, client_id_secret="SHOP_ID", client_secret_secret="SHOP_SECRET")
    assert shop.placeholders == {"shop": "Your store"} and shop.client_id_secret == "SHOP_ID"
    registry.connections.clear()
    ms = Connection("outlook", provider="microsoft", tenant="contoso.onmicrosoft.com")
    assert "/contoso.onmicrosoft.com/" in ms.authorize_url
