"""The sign-in server Claude talks to, sitting in front of Discord.

Flow:
  1. Claude sends the person to /authorize (handled by the MCP SDK, which checks
     the client, redirect URI and PKCE). authorize() below seals the request into
     an encrypted state and sends the browser to /oauth/start.
  2. /oauth/start sets a short-lived cookie holding a random nonce, then sends the
     browser to Discord. The nonce is also inside the sealed state.
  3. Discord returns to /oauth/discord/callback. The cookie must match the nonce,
     so a sign-in link started in one browser cannot be finished in another.
     The server learns the Discord user ID, revokes Discord's token, checks the
     person is a server member, and sends Claude its own one-time code.
  4. Claude swaps the code at /token (SDK) for this server's own access and
     refresh tokens. Discord's token never reaches Claude, and Claude's token
     never reaches Discord.

Only one client exists: the pre-registered Claude client. Self-registration is off.
Access tokens last an hour. A sign-in lasts 30 days from first sign-in and does
not slide when refreshed.
"""

from __future__ import annotations

import hmac
import secrets
import time
from collections.abc import Awaitable, Callable

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from discord_mcp import audit
from discord_mcp.errors import NotAMember
from discord_mcp.discord_client import DiscordError
from discord_mcp.tokens import ReplayGuard, TokenBox

ACCESS_TTL = 3600
CODE_TTL = 300
STATE_TTL = 600
SESSION_TTL = 30 * 86400
COOKIE = "dmcp_signin"

_PAGE = "<!doctype html><meta charset=utf-8><title>Sign-in</title><p>{}</p>"
BAD_REQUEST = "Sign-in could not be completed. Close this window and try connecting again."
NOT_MEMBER = "Your Discord account is not a member of this Discord server."


def _page(text: str, status: int) -> Response:
    return HTMLResponse(_PAGE.format(text), status_code=status)


class SignInProvider:
    def __init__(
        self,
        *,
        public_url: str,
        client: OAuthClientInformationFull,
        box: TokenBox,
        guard: ReplayGuard,
        discord_oauth,
        check_member: Callable[[str], Awaitable[object]],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.public_url = public_url
        self.resource = f"{public_url}/mcp"
        self.callback_url = f"{public_url}/oauth/discord/callback"
        self._client = client
        self._box = box
        self._guard = guard
        self._discord = discord_oauth
        self._check_member = check_member
        self._clock = clock
        self._secure_cookie = public_url.startswith("https://")

    # --- client registry: exactly one pre-registered client -------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        if isinstance(client_id, str) and hmac.compare_digest(client_id.encode(), self._client.client_id.encode()):
            return self._client
        return None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        raise NotImplementedError("client registration is disabled")

    # --- step 1: /authorize ------------------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource is not None and params.resource.rstrip("/") != self.resource:
            raise AuthorizeError("invalid_target", "unknown resource")
        state = self._box.seal(
            "state",
            {
                "cid": client.client_id,
                "ru": str(params.redirect_uri),
                "rux": params.redirect_uri_provided_explicitly,
                "cc": params.code_challenge,
                "st": params.state,
                "sc": params.scopes or [],
                "n": secrets.token_urlsafe(24),
            },
            ttl=STATE_TTL,
        )
        return f"{self.public_url}/oauth/start?s={state}"

    # --- step 2: /oauth/start -------------------------------------------------------

    async def start(self, request: Request) -> Response:
        sealed = request.query_params.get("s", "")
        state = self._box.open("state", sealed)
        if state is None:
            return _page(BAD_REQUEST, 400)
        resp = RedirectResponse(self._discord.authorize_url(redirect_uri=self.callback_url, state=sealed), 302)
        resp.set_cookie(
            COOKIE,
            state["n"],
            max_age=STATE_TTL,
            path="/oauth",
            secure=self._secure_cookie,
            httponly=True,
            samesite="lax",
        )
        return resp

    # --- step 3: /oauth/discord/callback ------------------------------------------------

    async def callback(self, request: Request) -> Response:
        started = time.monotonic()

        def fail(text: str, status: int, reason: str, user_id: object = None) -> Response:
            audit.record(
                user_id=user_id,
                tool="sign_in",
                channel_ids=[],
                item_count=0,
                duration_ms=int((time.monotonic() - started) * 1000),
                status=f"error:{reason}",
            )
            resp = _page(text, status)
            resp.delete_cookie(COOKIE, path="/oauth")
            return resp

        q = request.query_params
        code, sealed = q.get("code"), q.get("state")
        if q.get("error") or not code or not sealed:
            return fail(BAD_REQUEST, 400, "discord_denied")
        state = self._box.open("state", sealed)
        nonce = request.cookies.get(COOKIE, "")
        if state is None or not nonce or not hmac.compare_digest(nonce.encode(), str(state["n"]).encode()):
            return fail(BAD_REQUEST, 400, "bad_state")

        try:
            user_id = await self._discord.identify(code, self.callback_url)
        except DiscordError:
            return fail(BAD_REQUEST, 400, "discord_exchange")
        try:
            await self._check_member(user_id)
        except NotAMember:
            return fail(NOT_MEMBER, 403, "not_member", user_id)
        except Exception:
            return fail(BAD_REQUEST, 400, "member_check", user_id)

        our_code = self._box.seal(
            "code",
            {k: state[k] for k in ("cid", "ru", "rux", "cc", "sc")} | {"sub": user_id, "bt": self._guard.boot},
            ttl=CODE_TTL,
        )
        audit.record(
            user_id=user_id,
            tool="sign_in",
            channel_ids=[],
            item_count=0,
            duration_ms=int((time.monotonic() - started) * 1000),
            status="ok",
        )
        resp = RedirectResponse(construct_redirect_uri(state["ru"], code=our_code, state=state["st"]), 302)
        resp.delete_cookie(COOKIE, path="/oauth")
        return resp

    # --- step 4: /token ---------------------------------------------------------------

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str):
        p = self._box.open("code", authorization_code)
        if p is None or p["cid"] != client.client_id or not self._this_boot(p):
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=p["sc"],
            expires_at=p["exp"],
            client_id=p["cid"],
            code_challenge=p["cc"],
            redirect_uri=p["ru"],
            redirect_uri_provided_explicitly=p["rux"],
            resource=self.resource,
            subject=p["sub"],
        )

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code) -> OAuthToken:
        p = self._box.open("code", authorization_code.code)
        if p is None or not self._this_boot(p) or not self._guard.use(p["jti"], p["exp"]):
            raise TokenError("invalid_grant", "code is invalid or already used")
        return self._issue(p["sub"], client.client_id, p["sc"], session_exp=int(self._clock()) + SESSION_TTL)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str):
        p = self._box.open("refresh", refresh_token)
        if p is None or p["cid"] != client.client_id or not self._this_boot(p):
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=p["cid"],
            scopes=p["sc"],
            expires_at=p["exp"],
            resource=p["aud"],
            subject=p["sub"],
        )

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token, scopes) -> OAuthToken:
        p = self._box.open("refresh", refresh_token.token)
        if p is None or not self._this_boot(p):
            raise TokenError("invalid_grant", "sign-in has expired")
        try:
            await self._check_member(p["sub"])
        except NotAMember:
            raise TokenError("invalid_grant", "no longer a member") from None
        except Exception:
            raise TokenError("invalid_request", "try again shortly") from None
        if not self._guard.use(p["jti"], p["exp"]):
            raise TokenError("invalid_grant", "refresh token already used")
        return self._issue(p["sub"], client.client_id, p["sc"], session_exp=p["exp"])

    def _issue(self, subject: str, client_id: str, scopes: list[str], *, session_exp: int) -> OAuthToken:
        now = int(self._clock())
        if session_exp <= now:
            raise TokenError("invalid_grant", "sign-in has expired")
        body = {"sub": subject, "cid": client_id, "aud": self.resource, "sc": scopes}
        access_ttl = min(ACCESS_TTL, session_exp - now)
        return OAuthToken(
            access_token=self._box.seal("access", body, ttl=access_ttl),
            token_type="Bearer",
            expires_in=access_ttl,
            refresh_token=self._box.seal("refresh", body | {"bt": self._guard.boot}, exp=session_exp),
            scope=" ".join(scopes) or None,
        )

    def _this_boot(self, payload: dict) -> bool:
        """Codes and refresh tokens work once; that memory dies with the process, so so do they."""
        bt = payload.get("bt")
        return isinstance(bt, str) and hmac.compare_digest(bt.encode(), self._guard.boot.encode())

    # --- every MCP request -------------------------------------------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        p = self._box.open("access", token)
        if p is None:
            return None
        return AccessToken(
            token=token,
            client_id=p["cid"],
            scopes=p["sc"],
            expires_at=p["exp"],
            resource=p["aud"],
            subject=p["sub"],
        )

    async def revoke_token(self, token) -> None:
        return None  # revocation endpoint is disabled; sign-ins end by expiry or key rotation
