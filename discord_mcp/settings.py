"""Reads secrets and deployment settings from environment variables only.

Error messages name the variable, never its value.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field


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


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise SettingsError(f"{name} is not set")
    return value


def _snowflake(env: Mapping[str, str], name: str, value: str) -> str:
    if not value.isdigit():
        raise SettingsError(f"{name} must be a numeric Discord ID")
    return value


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    bot_token = _required(env, "DISCORD_BOT_TOKEN")
    guild_id = _snowflake(env, "DISCORD_GUILD_ID", _required(env, "DISCORD_GUILD_ID"))
    public_url = env.get("PUBLIC_URL", "").strip() or None
    dev_user_id = env.get("DEV_USER_ID", "").strip() or None
    if dev_user_id is not None:
        _snowflake(env, "DEV_USER_ID", dev_user_id)
        if public_url is not None:
            raise SettingsError("DEV_USER_ID is for local testing only and cannot be used with PUBLIC_URL")
    return Settings(
        bot_token=bot_token,
        guild_id=guild_id,
        public_url=public_url,
        dev_user_id=dev_user_id,
        config_path=env.get("DIGEST_CONFIG", "config.toml"),
        server_name=env.get("SERVER_NAME", "").strip() or "discord-mcp",
    )
