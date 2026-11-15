"""Entry point. Stage 1 runs only locally over stdio, as the DEV_USER_ID person."""

from __future__ import annotations

import sys

from discord_mcp import audit
from discord_mcp.config import ConfigError, load_config
from discord_mcp.digest import DevOwnerAccess, Digest
from discord_mcp.discord_client import DiscordClient
from discord_mcp.server import build_server
from discord_mcp.settings import SettingsError, load_settings


def main() -> None:
    sys.excepthook = audit.safe_excepthook
    audit.configure_logging()
    try:
        settings = load_settings()
        config = load_config(settings.config_path)
    except (SettingsError, ConfigError) as e:
        sys.stderr.write(f"fatal: {e}\n")  # these messages name settings, never values
        raise SystemExit(2) from None
    if settings.dev_user_id is None:
        sys.stderr.write("fatal: stage 1 runs only in local mode; set DEV_USER_ID. Sign-in arrives in stage 2.\n")
        raise SystemExit(2)

    client = DiscordClient(settings.bot_token)
    digest = Digest(client, guild_id=settings.guild_id, config=config, access=DevOwnerAccess())
    dev_user_id = settings.dev_user_id

    async def identity():
        return await digest.resolve_identity(dev_user_id)

    server = build_server(digest, identity, name=settings.server_name)
    audit.configure_logging()  # the SDK installs a default handler when the server is built
    server.run("stdio")


if __name__ == "__main__":
    main()
