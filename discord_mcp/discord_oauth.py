"""Discord sign-in, used only to learn who the person is.

Asks for the `identify` scope, reads the user ID, then immediately revokes
the Discord token. Nothing from Discord's sign-in is stored. This is separate
from the bot client because sign-in requires POST requests; it never touches
messages or channels.
"""

from __future__ import annotations

from urllib.parse import urlencode

import httpx

from discord_mcp.discord_client import API_BASE, USER_AGENT, DiscordError

AUTHORIZE_URL = "https://discord.com/oauth2/authorize"


class DiscordOAuth:
    def __init__(self, client_id: str, client_secret: str, *, timeout: float = 10.0) -> None:
        self._client_id = client_id
        self._http = httpx.AsyncClient(
            base_url=API_BASE,
            auth=httpx.BasicAuth(client_id, client_secret),
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    def authorize_url(self, *, redirect_uri: str, state: str) -> str:
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._client_id,
                "scope": "identify",
                "redirect_uri": redirect_uri,
                "state": state,
                "prompt": "none",
            }
        )
        return f"{AUTHORIZE_URL}?{query}"

    async def _call(self, method: str, path: str, **kwargs) -> dict:
        try:
            resp = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError:
            raise DiscordError(None) from None
        if resp.status_code >= 400:
            raise DiscordError(resp.status_code)
        try:
            return resp.json()
        except ValueError:
            raise DiscordError(resp.status_code) from None

    async def identify(self, code: str, redirect_uri: str) -> str:
        """Exchange the sign-in code, return the Discord user ID, and revoke the token."""
        token = await self._call(
            "POST",
            "/oauth2/token",
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
        )
        access = token.get("access_token")
        if not isinstance(access, str) or not access:
            raise DiscordError(None)
        try:
            me = await self._call("GET", "/users/@me", headers={"Authorization": f"Bearer {access}"}, auth=None)
        finally:
            try:
                await self._call("POST", "/oauth2/token/revoke", data={"token": access, "token_type_hint": "access_token"})
            except DiscordError:
                pass  # the token expires on its own; never block sign-in on cleanup
        user_id = me.get("id")
        if not isinstance(user_id, str) or not user_id.isdigit():
            raise DiscordError(None)
        return user_id
