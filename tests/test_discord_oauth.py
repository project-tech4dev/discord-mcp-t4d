import httpx
import pytest
import respx

from discord_mcp.discord_client import API_BASE, DiscordError
from discord_mcp.discord_oauth import AUTHORIZE_URL, DiscordOAuth

SECRET_BODY = "SENSITIVE-BODY"


def oauth() -> DiscordOAuth:
    return DiscordOAuth("123", "app-secret")


def test_authorize_url_asks_only_for_identify():
    url = httpx.URL(oauth().authorize_url(redirect_uri="https://mcp.example.org/oauth/discord/callback", state="S"))
    assert str(url).startswith(AUTHORIZE_URL)
    assert url.params["scope"] == "identify"
    assert url.params["client_id"] == "123"
    assert url.params["response_type"] == "code"
    assert url.params["state"] == "S"


@respx.mock
async def test_identify_exchanges_code_reads_user_and_revokes():
    token = respx.post(f"{API_BASE}/oauth2/token").mock(
        return_value=httpx.Response(200, json={"access_token": "user-tok", "token_type": "Bearer", "scope": "identify"})
    )
    me = respx.get(f"{API_BASE}/users/@me").mock(return_value=httpx.Response(200, json={"id": "1001"}))
    revoke = respx.post(f"{API_BASE}/oauth2/token/revoke").mock(return_value=httpx.Response(200, json={}))
    o = oauth()
    assert await o.identify("the-code", "https://mcp.example.org/oauth/discord/callback") == "1001"
    await o.aclose()

    form = dict(httpx.QueryParams(token.calls[0].request.content.decode()))
    assert form == {
        "grant_type": "authorization_code",
        "code": "the-code",
        "redirect_uri": "https://mcp.example.org/oauth/discord/callback",
    }
    assert token.calls[0].request.headers["Authorization"].startswith("Basic ")
    assert me.calls[0].request.headers["Authorization"] == "Bearer user-tok"
    assert dict(httpx.QueryParams(revoke.calls[0].request.content.decode()))["token"] == "user-tok"


@respx.mock
async def test_identify_still_succeeds_if_revoke_fails():
    respx.post(f"{API_BASE}/oauth2/token").mock(return_value=httpx.Response(200, json={"access_token": "t"}))
    respx.get(f"{API_BASE}/users/@me").mock(return_value=httpx.Response(200, json={"id": "1001"}))
    respx.post(f"{API_BASE}/oauth2/token/revoke").mock(return_value=httpx.Response(500))
    o = oauth()
    assert await o.identify("c", "https://x/cb") == "1001"
    await o.aclose()


@respx.mock
async def test_failed_exchange_raises_status_only():
    respx.post(f"{API_BASE}/oauth2/token").mock(return_value=httpx.Response(400, json={"error": SECRET_BODY}))
    o = oauth()
    with pytest.raises(DiscordError) as e:
        await o.identify("bad", "https://x/cb")
    await o.aclose()
    assert e.value.status == 400
    assert SECRET_BODY not in repr(e.value)
    assert e.value.__cause__ is None


@respx.mock
async def test_non_numeric_user_id_is_rejected():
    respx.post(f"{API_BASE}/oauth2/token").mock(return_value=httpx.Response(200, json={"access_token": "t"}))
    respx.get(f"{API_BASE}/users/@me").mock(return_value=httpx.Response(200, json={"id": "abc"}))
    respx.post(f"{API_BASE}/oauth2/token/revoke").mock(return_value=httpx.Response(200))
    o = oauth()
    with pytest.raises(DiscordError):
        await o.identify("c", "https://x/cb")
    await o.aclose()
