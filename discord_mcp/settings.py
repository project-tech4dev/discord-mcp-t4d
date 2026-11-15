"""Reads secrets and deployment settings from environment variables only.

Two modes:
  stdio  local testing as DEV_USER_ID (no sign-in). Refused if PUBLIC_URL is set.
  http   remote connector with sign-in. Chosen when PUBLIC_URL is set.

Error messages name the variable, never its value. Secrets are hidden from repr.
"""

from __future__ import annotations

import base64
import binascii
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
LOCAL_HOSTS = ("localhost", "127.0.0.1")


class SettingsError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    bot_token: str = field(repr=False)
    guild_id: str
    public_url: str | None
    dev_user_id: str | None
    config_path: str
    server_name: str = "discord-mcp"
    discord_client_id: str | None = None
    discord_client_secret: str | None = field(default=None, repr=False)
    oauth_client_id: str | None = None
    oauth_client_secret: str | None = field(default=None, repr=False)
    oauth_redirect_uris: tuple[str, ...] = (CLAUDE_CALLBACK,)
    encryption_key: bytes | None = field(default=None, repr=False)
    host: str = "127.0.0.1"
    port: int = 8000

    @property
    def mode(self) -> str:
        return "http" if self.public_url else "stdio"


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise SettingsError(f"{name} is not set")
    return value


def _snowflake(name: str, value: str) -> str:
    if not value.isdigit():
        raise SettingsError(f"{name} must be a numeric Discord ID")
    return value


def _key(env: Mapping[str, str]) -> bytes:
    raw = _required(env, "ENCRYPTION_KEY")
    try:
        key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (binascii.Error, ValueError):
        raise SettingsError("ENCRYPTION_KEY must be base64url") from None
    if len(key) != 32:
        raise SettingsError("ENCRYPTION_KEY must decode to exactly 32 bytes")
    return key


def _public_url(raw: str) -> str:
    url = raw.rstrip("/")
    parts = urlsplit(url)
    if parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOCAL_HOSTS):
        if parts.hostname and not parts.query and not parts.fragment:
            return url
    raise SettingsError("PUBLIC_URL must be an https URL (http is allowed only for localhost)")


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    bot_token = _required(env, "DISCORD_BOT_TOKEN")
    guild_id = _snowflake("DISCORD_GUILD_ID", _required(env, "DISCORD_GUILD_ID"))
    public_url = env.get("PUBLIC_URL", "").strip() or None
    dev_user_id = env.get("DEV_USER_ID", "").strip() or None
    common = dict(
        bot_token=bot_token,
        guild_id=guild_id,
        config_path=env.get("DIGEST_CONFIG", "config.toml"),
        server_name=env.get("SERVER_NAME", "").strip() or "discord-mcp",
    )

    if public_url is None:
        if dev_user_id is None:
            raise SettingsError("set PUBLIC_URL for the remote server, or DEV_USER_ID for local testing")
        return Settings(public_url=None, dev_user_id=_snowflake("DEV_USER_ID", dev_user_id), **common)

    if dev_user_id is not None:
        raise SettingsError("DEV_USER_ID is for local testing only and cannot be used with PUBLIC_URL")
    redirects = tuple(u.strip() for u in env.get("OAUTH_REDIRECT_URIS", CLAUDE_CALLBACK).split(",") if u.strip())
    try:
        port = int(env.get("PORT", "8000"))
    except ValueError:
        raise SettingsError("PORT must be a number") from None

    return Settings(
        public_url=_public_url(public_url),
        dev_user_id=None,
        discord_client_id=_snowflake("DISCORD_CLIENT_ID", _required(env, "DISCORD_CLIENT_ID")),
        discord_client_secret=_required(env, "DISCORD_CLIENT_SECRET"),
        oauth_client_id=_required(env, "OAUTH_CLIENT_ID"),
        oauth_client_secret=_required(env, "OAUTH_CLIENT_SECRET"),
        oauth_redirect_uris=redirects,
        encryption_key=_key(env),
        host=env.get("HOST", "").strip() or "127.0.0.1",
        port=port,
        **common,
    )
