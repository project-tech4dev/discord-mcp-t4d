"""End-to-end sign-in and tool calls over HTTP, with Discord faked."""

import base64
import hashlib
import io
import json
import logging
import secrets
import time

import pytest
from starlette.testclient import TestClient

from discord_mcp import audit
from discord_mcp.config import ChannelEntry, Config, Limits
from discord_mcp.digest import Digest
from discord_mcp.http_app import build_http_app
from discord_mcp.settings import load_settings
from tests.fakes import GUILD_ID, ME, NOW, FakeClient, channel, msg, user

PUBLIC = "https://mcp.example.org"
RESOURCE = f"{PUBLIC}/mcp"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
CLIENT_ID = "claude-client"
CLIENT_SECRET = "oauth-secret-value"
SENTINEL = "PRIVATE-MESSAGE-TEXT-42"
ENV = {
    "DISCORD_BOT_TOKEN": "bot-token-value",
    "DISCORD_GUILD_ID": GUILD_ID,
    "PUBLIC_URL": PUBLIC,
    "DISCORD_CLIENT_ID": "123",
    "DISCORD_CLIENT_SECRET": "discord-secret-value",
    "OAUTH_CLIENT_ID": CLIENT_ID,
    "OAUTH_CLIENT_SECRET": CLIENT_SECRET,
    "ENCRYPTION_KEY": base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("="),
}


class FakeDiscordOAuth:
    def __init__(self):
        self.user_id = ME
        self.fail = False
        self.seen_redirect: str | None = None

    def authorize_url(self, *, redirect_uri: str, state: str) -> str:
        self.seen_redirect = redirect_uri
        return f"https://discord.com/oauth2/authorize?state={state}"

    async def identify(self, code: str, redirect_uri: str) -> str:
        if self.fail:
            from discord_mcp.discord_client import DiscordError

            raise DiscordError(400)
        return self.user_id

    async def aclose(self):
        pass


class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


@pytest.fixture
def logs():
    s = io.StringIO()
    restore = audit.configure_logging(s)
    yield s
    restore()


@pytest.fixture
def env():
    fake = FakeClient()
    fake.add_channel(
        channel("10", "general"),
        [msg("100", "10", content=f"<@{ME}> {SENTINEL}", mentions=[user(ME)], hours_ago=2)],
    )
    cfg = Config(allowlist={"10": ChannelEntry("10", None, 1)}, deny=frozenset(), limits=Limits())
    digest = Digest(fake, guild_id=GUILD_ID, config=cfg, clock=lambda: NOW)
    oauth = FakeDiscordOAuth()
    clock = Clock()
    app = build_http_app(load_settings(ENV), digest, oauth, clock=clock)
    client = TestClient(app, base_url=PUBLIC, follow_redirects=False)
    with client:
        yield type("Env", (), {"client": client, "oauth": oauth, "discord": fake, "clock": clock, "digest": digest})


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def authorize(client, challenge, *, redirect=CALLBACK, resource=RESOURCE, state="claude-state"):
    return client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": redirect,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "resource": resource,
        },
    )


def start_and_callback(client, authorize_resp, *, keep_cookie=True):
    start = client.get(authorize_resp.headers["location"].removeprefix(PUBLIC))
    assert start.status_code == 302
    discord_state = start.headers["location"].split("state=", 1)[1]
    if not keep_cookie:
        client.cookies.clear()
    return client.get("/oauth/discord/callback", params={"code": "discord-code", "state": discord_state})


def token_request(client, **form):
    return client.post("/token", data={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, **form})


def sign_in(client) -> dict:
    verifier, challenge = pkce()
    cb = start_and_callback(client, authorize(client, challenge))
    assert cb.status_code == 302, cb.text
    location = cb.headers["location"]
    assert location.startswith(CALLBACK)
    code = dict(p.split("=", 1) for p in location.split("?", 1)[1].split("&"))["code"]
    resp = token_request(
        client,
        grant_type="authorization_code",
        code=code,
        redirect_uri=CALLBACK,
        code_verifier=verifier,
        resource=RESOURCE,
    )
    assert resp.status_code == 200, resp.text
    return resp.json() | {"_code": code, "_verifier": verifier}


def call_tool(client, token: str, name: str, args: dict | None = None):
    return client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-11-25",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args or {}}},
    )


# --- discovery ------------------------------------------------------------------


def test_mcp_without_token_is_401_with_resource_metadata(env):
    resp = env.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert resp.status_code == 401
    assert "resource_metadata=" in resp.headers["www-authenticate"]
    assert "/.well-known/oauth-protected-resource" in resp.headers["www-authenticate"]


def test_protected_resource_metadata_points_to_this_server(env):
    meta = env.client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert meta["resource"].rstrip("/") == RESOURCE
    assert [s.rstrip("/") for s in meta["authorization_servers"]] == [PUBLIC]


def test_authorization_server_metadata_has_pkce_and_no_registration(env):
    meta = env.client.get("/.well-known/oauth-authorization-server").json()
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert "registration_endpoint" not in meta or meta["registration_endpoint"] is None
    assert env.client.post("/register", json={"redirect_uris": ["https://evil.example/cb"]}).status_code in (404, 405)


# --- full flow ------------------------------------------------------------------


def test_full_sign_in_then_tool_call_returns_pings(env, logs):
    tokens = sign_in(env.client)
    assert tokens["token_type"].lower() == "bearer"
    assert env.oauth.seen_redirect == f"{PUBLIC}/oauth/discord/callback"
    resp = call_tool(env.client, tokens["access_token"], "get_my_pings")
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert not result.get("isError")
    convs = result["structuredContent"]["conversations"]
    assert [(p["link"].rsplit("/", 1)[1], p["reason"]) for c in convs for p in c["pings"]] == [("100", "mention")]
    assert SENTINEL not in logs.getvalue()
    lines = [json.loads(line) for line in logs.getvalue().splitlines()]
    assert [line["tool"] for line in lines] == ["sign_in", "get_my_pings"]
    assert all(line["user_id"] == ME for line in lines)


def test_tokens_hold_no_readable_identity(env):
    tokens = sign_in(env.client)
    for key in ("access_token", "refresh_token"):
        assert ME not in tokens[key]


# --- the Discord step is bound to the browser that started it ------------------------


def test_callback_without_starting_cookie_is_refused(env):
    _, challenge = pkce()
    cb = start_and_callback(env.client, authorize(env.client, challenge), keep_cookie=False)
    assert cb.status_code == 400
    assert "location" not in cb.headers


def test_callback_error_page_does_not_echo_input(env):
    resp = env.client.get("/oauth/discord/callback", params={"state": "<script>x</script>", "code": "zz"})
    assert resp.status_code == 400
    assert "<script>" not in resp.text and "zz" not in resp.text


def test_discord_denial_is_refused(env):
    resp = env.client.get("/oauth/discord/callback", params={"error": "access_denied"})
    assert resp.status_code == 400


def test_failed_discord_exchange_is_refused(env):
    env.oauth.fail = True
    _, challenge = pkce()
    assert start_and_callback(env.client, authorize(env.client, challenge)).status_code == 400


def test_member_without_an_allowed_role_cannot_sign_in(env):
    from dataclasses import replace

    from tests.fakes import ROLE_OPS

    access = env.digest._access
    access._config = replace(access._config, allowed_roles=frozenset({ROLE_OPS}))
    _, challenge = pkce()
    cb = start_and_callback(env.client, authorize(env.client, challenge))
    assert cb.status_code == 403
    assert "location" not in cb.headers
    assert "role allowed" in cb.text


def test_non_member_cannot_sign_in(env):
    env.oauth.user_id = "7777"
    _, challenge = pkce()
    cb = start_and_callback(env.client, authorize(env.client, challenge))
    assert cb.status_code == 403
    assert "location" not in cb.headers


# --- authorize request checks ----------------------------------------------------


def test_unregistered_redirect_uri_is_refused(env):
    _, challenge = pkce()
    resp = authorize(env.client, challenge, redirect="https://evil.example/cb")
    assert resp.status_code == 400
    assert "evil.example" not in resp.headers.get("location", "")


def test_unknown_client_is_refused(env):
    _, challenge = pkce()
    resp = env.client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": "someone-else",
            "redirect_uri": CALLBACK,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    assert resp.status_code == 400


def test_wrong_resource_is_refused(env):
    _, challenge = pkce()
    resp = authorize(env.client, challenge, resource="https://other.example/mcp")
    assert "error=invalid_target" in resp.headers.get("location", "")


# --- token endpoint checks -------------------------------------------------------


def test_code_works_only_once(env):
    tokens = sign_in(env.client)
    again = token_request(
        env.client,
        grant_type="authorization_code",
        code=tokens["_code"],
        redirect_uri=CALLBACK,
        code_verifier=tokens["_verifier"],
        resource=RESOURCE,
    )
    assert again.status_code == 400
    assert again.json()["error"] == "invalid_grant"


def test_wrong_client_secret_is_refused(env):
    resp = env.client.post(
        "/token", data={"client_id": CLIENT_ID, "client_secret": "nope", "grant_type": "refresh_token", "refresh_token": "x"}
    )
    assert resp.status_code == 401


def test_refresh_rotates_and_old_refresh_token_dies(env):
    tokens = sign_in(env.client)
    first = token_request(env.client, grant_type="refresh_token", refresh_token=tokens["refresh_token"])
    assert first.status_code == 200, first.text
    new = first.json()
    assert new["refresh_token"] != tokens["refresh_token"]
    replay = token_request(env.client, grant_type="refresh_token", refresh_token=tokens["refresh_token"])
    assert replay.status_code == 400
    assert call_tool(env.client, new["access_token"], "list_my_channels").status_code == 200


def test_sign_in_lasts_30_days_and_does_not_slide(env):
    tokens = sign_in(env.client)
    env.clock.t += 29 * 86400
    refreshed = token_request(env.client, grant_type="refresh_token", refresh_token=tokens["refresh_token"]).json()
    env.clock.t += 2 * 86400
    expired = token_request(env.client, grant_type="refresh_token", refresh_token=refreshed["refresh_token"])
    assert expired.status_code == 400
    assert expired.json()["error"] == "invalid_grant"


def test_refresh_fails_after_person_leaves_server(env):
    tokens = sign_in(env.client)
    del env.discord.members[ME]
    resp = token_request(env.client, grant_type="refresh_token", refresh_token=tokens["refresh_token"])
    assert resp.status_code == 400


def test_access_token_expires_after_an_hour(env):
    tokens = sign_in(env.client)
    env.clock.t += 3601
    assert call_tool(env.client, tokens["access_token"], "list_my_channels").status_code == 401


def test_refresh_token_is_not_an_access_token(env):
    tokens = sign_in(env.client)
    assert call_tool(env.client, tokens["refresh_token"], "list_my_channels").status_code == 401


def test_tampered_token_is_refused(env):
    tokens = sign_in(env.client)
    bad = tokens["access_token"][:-3] + "AAA"
    assert call_tool(env.client, bad, "list_my_channels").status_code == 401


# --- after sign-in --------------------------------------------------------------------


def test_person_who_left_gets_tool_error_not_data(env, logs):
    tokens = sign_in(env.client)
    del env.discord.members[ME]
    result = call_tool(env.client, tokens["access_token"], "get_my_pings").json()["result"]
    assert result["isError"]
    assert SENTINEL not in json.dumps(result)


def test_other_hosts_are_refused(env):
    tokens = sign_in(env.client)
    resp = env.client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {tokens['access_token']}", "Host": "evil.example"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert resp.status_code in (400, 403, 421)


def test_tool_list_has_titles_and_read_only_hints(env):
    tokens = sign_in(env.client)
    resp = env.client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {tokens['access_token']}",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-11-25",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    tools = resp.json()["result"]["tools"]
    assert len(tools) == 8
    for t in tools:
        assert t["title"]
        assert t["annotations"]["readOnlyHint"] is True


def test_no_other_logger_output_during_sign_in(env, logs):
    logging.getLogger("uvicorn.error").error("leak %s", SENTINEL)
    sign_in(env.client)
    assert SENTINEL not in logs.getvalue()


def _basic(cid: str, secret: str) -> str:
    from urllib.parse import quote

    return "Basic " + base64.b64encode(f"{quote(cid)}:{quote(secret)}".encode()).decode()


def _code_for(client) -> tuple[str, str]:
    verifier, challenge = pkce()
    cb = start_and_callback(client, authorize(client, challenge))
    code = dict(p.split("=", 1) for p in cb.headers["location"].split("?", 1)[1].split("&"))["code"]
    return code, verifier


def test_token_accepts_basic_auth_without_client_id_in_form(env):
    code, verifier = _code_for(env.client)
    resp = env.client.post(
        "/token",
        headers={"Authorization": _basic(CLIENT_ID, CLIENT_SECRET)},
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK, "code_verifier": verifier},
    )
    assert resp.status_code == 200, resp.text


def test_token_basic_auth_with_wrong_secret_is_refused(env):
    code, verifier = _code_for(env.client)
    resp = env.client.post(
        "/token",
        headers={"Authorization": _basic(CLIENT_ID, "wrong")},
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK, "code_verifier": verifier},
    )
    assert resp.status_code == 401


def test_token_basic_auth_conflicting_with_form_client_id_is_refused(env):
    code, verifier = _code_for(env.client)
    resp = env.client.post(
        "/token",
        headers={"Authorization": _basic("someone-else", CLIENT_SECRET)},
        data={
            "client_id": CLIENT_ID,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CALLBACK,
            "code_verifier": verifier,
        },
    )
    assert resp.status_code in (400, 401)


def test_metadata_advertises_both_secret_methods(env):
    meta = env.client.get("/.well-known/oauth-authorization-server").json()
    assert set(meta["token_endpoint_auth_methods_supported"]) == {"client_secret_post", "client_secret_basic"}


def test_restart_signs_everyone_in_again(env):
    """Used refresh tokens are remembered in memory only, so after a restart old ones must not work."""
    tokens = sign_in(env.client)
    digest = Digest(env.discord, guild_id=GUILD_ID, config=Config(allowlist={"10": ChannelEntry("10", None, 1)}, deny=frozenset(), limits=Limits()), clock=lambda: NOW)
    restarted = build_http_app(load_settings(ENV), digest, env.oauth, clock=env.clock)
    with TestClient(restarted, base_url=PUBLIC, follow_redirects=False) as client:
        resp = token_request(client, grant_type="refresh_token", refresh_token=tokens["refresh_token"])
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_grant"
        again = sign_in(client)
        assert token_request(client, grant_type="refresh_token", refresh_token=again["refresh_token"]).status_code == 200


def test_code_from_before_restart_is_rejected(env):
    verifier, challenge = pkce()
    cb = start_and_callback(env.client, authorize(env.client, challenge))
    code = dict(p.split("=", 1) for p in cb.headers["location"].split("?", 1)[1].split("&"))["code"]
    digest = Digest(env.discord, guild_id=GUILD_ID, config=Config(allowlist={}, deny=frozenset(), limits=Limits()), clock=lambda: NOW)
    restarted = build_http_app(load_settings(ENV), digest, env.oauth, clock=env.clock)
    with TestClient(restarted, base_url=PUBLIC, follow_redirects=False) as client:
        resp = token_request(client, grant_type="authorization_code", code=code, redirect_uri=CALLBACK, code_verifier=verifier, resource=RESOURCE)
        assert resp.status_code == 400
