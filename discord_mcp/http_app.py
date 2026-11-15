"""The remote connector: MCP over Streamable HTTP with sign-in in front of it."""

from __future__ import annotations

import base64
import binascii
import time
from collections.abc import Callable
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthClientInformationFull
from starlette.applications import Starlette

from discord_mcp.digest import Digest
from discord_mcp.errors import NotAMember
from discord_mcp.matching import Identity
from discord_mcp.oauth_provider import SignInProvider
from discord_mcp.server import build_server
from discord_mcp.settings import Settings
from discord_mcp.tokens import ReplayGuard, TokenBox


MAX_TOKEN_BODY = 16 * 1024


class BasicAuthToForm:
    """Lets /token accept the client secret in either place the metadata advertises.

    The SDK checks one method per client. This wrapper copies HTTP Basic
    credentials into the form (client_secret_post), refusing conflicts, so the
    SDK's constant-time secret check still decides.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] != "/token" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        auth = headers.get(b"authorization", b"").decode("latin-1")
        if not auth.lower().startswith("basic "):
            return await self.app(scope, receive, send)

        body = b""
        more = True
        while more:
            message = await receive()
            body += message.get("body", b"")
            more = message.get("more_body", False)
            if len(body) > MAX_TOKEN_BODY:
                return await _reply(send, 413, b'{"error":"invalid_request"}')
        try:
            cid, secret = base64.b64decode(auth[6:], validate=True).decode().split(":", 1)
            cid, secret = unquote(cid), unquote(secret)
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return await _reply(send, 401, b'{"error":"invalid_client"}')
        form = dict(parse_qsl(body.decode("latin-1"), keep_blank_values=True))
        if form.get("client_id", cid) != cid or form.get("client_secret", secret) != secret:
            return await _reply(send, 401, b'{"error":"invalid_client"}')
        form["client_id"], form["client_secret"] = cid, secret
        new_body = urlencode(form).encode()

        new_headers = [(k, v) for k, v in scope["headers"] if k not in (b"authorization", b"content-length")]
        new_headers.append((b"content-length", str(len(new_body)).encode()))
        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": new_body, "more_body": False}
            return await receive()

        return await self.app(dict(scope, headers=new_headers), replay, send)


async def _reply(send, status: int, body: bytes) -> None:
    await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": body})


def build_http_app(settings: Settings, digest: Digest, discord_oauth, *, clock: Callable[[], float] = time.time) -> Starlette:
    if settings.mode != "http" or settings.encryption_key is None:
        raise ValueError("HTTP mode needs PUBLIC_URL and the sign-in settings")
    public = settings.public_url

    client = OAuthClientInformationFull(
        client_id=settings.oauth_client_id,
        client_secret=settings.oauth_client_secret,
        redirect_uris=list(settings.oauth_redirect_uris),
        token_endpoint_auth_method="client_secret_post",  # Basic is converted by BasicAuthToForm
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
    )
    provider = SignInProvider(
        public_url=public,
        client=client,
        box=TokenBox(settings.encryption_key, clock=clock),
        guard=ReplayGuard(clock=clock),
        discord_oauth=discord_oauth,
        check_member=digest.resolve_identity,
        clock=clock,
    )
    auth = AuthSettings(
        issuer_url=public,
        resource_server_url=provider.resource,
        validate_token_resource=True,
        client_registration_options=ClientRegistrationOptions(enabled=False),
        revocation_options=RevocationOptions(enabled=False),
    )

    async def identity() -> Identity:
        token = get_access_token()
        if token is None or not token.subject:
            raise NotAMember("no signed-in person")
        return await digest.resolve_identity(token.subject)

    server = build_server(digest, identity, name=settings.server_name, auth_server_provider=provider, auth=auth)
    server.custom_route("/oauth/start", methods=["GET"], include_in_schema=False)(provider.start)
    server.custom_route("/oauth/discord/callback", methods=["GET"], include_in_schema=False)(provider.callback)

    host = urlsplit(public).netloc
    app = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[host],
            allowed_origins=[public, "https://claude.ai"],
        ),
        host=settings.host,
    )
    app.add_middleware(BasicAuthToForm)
    return app
