"""Entry point.

  PUBLIC_URL set  -> remote connector over HTTPS with Discord sign-in.
  DEV_USER_ID set -> local testing over stdio as that person (no sign-in).
"""

from __future__ import annotations

import sys

import uvicorn

from discord_mcp import audit
from discord_mcp.config import ConfigError, load_config
from discord_mcp.digest import Digest
from discord_mcp.discord_client import DiscordClient
from discord_mcp.discord_oauth import DiscordOAuth
from discord_mcp.http_app import build_http_app
from discord_mcp.server import build_server
from discord_mcp.settings import Settings, SettingsError, load_settings


def uvicorn_config(app, settings: Settings) -> uvicorn.Config:
    # log_config=None stops uvicorn installing its own handlers, which would
    # bypass the audit-only filter. The access log would record request URLs,
    # including the one-time sign-in code, so it stays off.
    return uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=False,
        server_header=False,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


def main() -> None:
    sys.excepthook = audit.safe_excepthook
    audit.configure_logging()
    try:
        settings = load_settings()
        config = load_config(settings.config_path)
    except (SettingsError, ConfigError) as e:
        sys.stderr.write(f"fatal: {e}\n")  # these messages name settings, never values
        raise SystemExit(2) from None

    client = DiscordClient(settings.bot_token)
    digest = Digest(client, guild_id=settings.guild_id, config=config)

    if settings.mode == "http":
        oauth = DiscordOAuth(settings.discord_client_id, settings.discord_client_secret)
        app = build_http_app(settings, digest, oauth)
        audit.configure_logging()  # the SDK installs a default handler when the server is built
        audit.record(user_id=None, tool="startup", channel_ids=[], item_count=0, duration_ms=0, status="ok")
        uvicorn.Server(uvicorn_config(app, settings)).run()
        return

    dev_user_id = settings.dev_user_id

    async def identity():
        return await digest.resolve_identity(dev_user_id)

    server = build_server(digest, identity, name=settings.server_name)
    audit.configure_logging()
    server.run("stdio")


if __name__ == "__main__":
    main()
