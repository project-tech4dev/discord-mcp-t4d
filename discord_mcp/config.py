"""Loads config.toml: channel allowlist, deny list and limits. Holds no secrets."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Limits:
    default_days: int = 7
    max_days: int = 14
    text_chars: int = 1500
    reply_chars: int = 200
    channel_default_days: int = 3
    channel_default_limit: int = 100
    channel_max_limit: int = 300
    conversation_gap_hours: int = 12
    conversation_max_hours: int = 24
    summary_chars: int = 200
    max_result_chars: int = 40000
    conversation_max_messages: int = 50
    row_max_pings: int = 5


@dataclass(frozen=True)
class ChannelEntry:
    id: str
    name: str | None
    phase: int


@dataclass(frozen=True)
class Config:
    allowlist: dict[str, ChannelEntry]
    deny: frozenset[tuple[str, str]]
    limits: Limits = field(default_factory=Limits)


def _snowflake(value: object, what: str) -> str:
    if not isinstance(value, str) or not value.isdigit():
        raise ConfigError(f"{what} must be a numeric Discord ID written as a string")
    return value


def load_config(path: str | Path) -> Config:
    try:
        raw = tomllib.loads(Path(path).read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"cannot read config file ({type(e).__name__})") from None

    allowlist: dict[str, ChannelEntry] = {}
    for entry in raw.get("allowlist", []):
        cid = _snowflake(entry.get("id"), "allowlist id")
        phase = entry.get("phase")
        if not isinstance(phase, int):
            raise ConfigError("each allowlist entry needs an integer phase")
        allowlist[cid] = ChannelEntry(id=cid, name=entry.get("name"), phase=phase)

    deny = frozenset(
        (_snowflake(d.get("user_id"), "deny user_id"), _snowflake(d.get("channel_id"), "deny channel_id"))
        for d in raw.get("deny", [])
    )

    known = {f.name for f in fields(Limits)}
    given = raw.get("limits", {})
    unknown = set(given) - known
    if unknown:
        raise ConfigError(f"unknown limits: {sorted(unknown)}")
    if not all(isinstance(v, int) and v > 0 for v in given.values()):
        raise ConfigError("limits must be positive integers")

    return Config(allowlist=allowlist, deny=deny, limits=Limits(**given))
