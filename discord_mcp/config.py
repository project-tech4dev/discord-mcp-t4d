"""Loads config.toml: channel allowlist, deny list and limits. Holds no secrets."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from discord_mcp.signals import check_words


class ConfigError(Exception):
    pass


PING_MODES = ("all", "direct_only")


@dataclass(frozen=True)
class Limits:
    default_days: int = 7
    max_days: int = 30
    text_chars: int = 1500
    reply_chars: int = 200
    channel_default_days: int = 3
    channel_default_limit: int = 100
    channel_max_limit: int = 300
    conversation_gap_hours: int = 12
    conversation_max_hours: int = 24
    summary_chars: int = 200
    max_result_chars: int = 40000
    # get_my_pings only; raise it for people with very busy weeks (see README on Claude Code's cap).
    pings_max_chars: int = 40000
    conversation_max_messages: int = 50
    row_max_pings: int = 5
    overview_max_days: int = 30
    overview_latest: int = 2
    overview_key_messages: int = 2
    opening_chars: int = 300
    main_topic_gap_hours: int = 24
    main_topic_max_hours: int = 72
    search_matches_per_topic: int = 5
    overview_max_messages: int = 3000
    overview_max_threads: int = 50
    overview_request_messages: int = 15000
    # Server-wide reads (get_my_pings, get_server_overview, get_open_loops): one message budget per request.
    server_request_messages: int = 30000
    server_max_threads: int = 200
    nudge_ask_days: int = 1
    nudge_promise_days: int = 2
    nudge_stall_days: int = 3
    brief_topics_per_channel: int = 3
    brief_max_deadlines: int = 50


@dataclass(frozen=True)
class ChannelEntry:
    id: str
    name: str | None
    phase: int
    watch: tuple[str, ...] = ()
    # "direct_only": @here and role pings here are announcements; only pings naming the person need them.
    pings: str = "all"


@dataclass(frozen=True)
class Config:
    allowlist: dict[str, ChannelEntry]
    deny: frozenset[tuple[str, str]]
    limits: Limits = field(default_factory=Limits)
    # When true, every text, announcement and forum channel is a candidate, not only the allowlist.
    allow_all_channels: bool = False
    # Channels, categories or threads no tool ever reads, for anyone. Wins over everything.
    deny_channels: frozenset[str] = frozenset()
    # Resolves "today", "Friday" and dates in deadlines. An IANA name such as "Europe/Berlin".
    timezone: str = "UTC"
    # How "20/11" is read: day first ("dmy") or month first ("mdy").
    date_order: str = "dmy"


def _valid_timezone(name: object) -> bool:
    if not isinstance(name, str) or not name or ".." in name or name.startswith("/"):
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def _snowflake(value: object, what: str) -> str:
    if not isinstance(value, str) or not value.isdigit():
        raise ConfigError(f"{what} must be a numeric Discord ID written as a string")
    return value


def _known_keys(entries: object, allowed: set[str], what: str) -> None:
    """A misspelled key inside an entry must fail loudly, not load as a default."""
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        raise ConfigError(f"{what} must be a list of {{ ... }} entries")
    for entry in entries:
        if set(entry) - allowed:
            raise ConfigError(f"unknown {what} settings: {sorted(set(entry) - allowed)}")


def load_config(path: str | Path) -> Config:
    try:
        raw = tomllib.loads(Path(path).read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"cannot read config file ({type(e).__name__})") from None

    unknown_top = set(raw) - {"allowlist", "deny", "limits", "allow_all_channels", "deny_channels", "timezone", "date_order"}
    if unknown_top:
        # A misspelled deny list must never load as an empty one.
        raise ConfigError(f"unknown settings: {sorted(unknown_top)}")

    _known_keys(raw.get("allowlist", []), {"id", "name", "phase", "watch", "pings"}, "allowlist")
    _known_keys(raw.get("deny_channels", []), {"id", "note"}, "deny_channels")
    _known_keys(raw.get("deny", []), {"user_id", "channel_id", "note"}, "deny")

    allowlist: dict[str, ChannelEntry] = {}
    for entry in raw.get("allowlist", []):
        cid = _snowflake(entry.get("id"), "allowlist id")
        phase = entry.get("phase")
        if not isinstance(phase, int):
            raise ConfigError("each allowlist entry needs an integer phase")
        try:
            watch = tuple(check_words(entry.get("watch", []), max_count=30, what="watch"))
        except ValueError as e:
            raise ConfigError(str(e)) from None
        pings = entry.get("pings", "all")
        if pings not in PING_MODES:
            raise ConfigError(f"pings must be one of {list(PING_MODES)}")
        allowlist[cid] = ChannelEntry(id=cid, name=entry.get("name"), phase=phase, watch=watch, pings=pings)

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
    if given.get("pings_max_chars", 0) > 200000:
        raise ConfigError("pings_max_chars must be at most 200000")

    allow_all = raw.get("allow_all_channels", False)
    if not isinstance(allow_all, bool):
        raise ConfigError("allow_all_channels must be true or false")
    deny_channels = raw.get("deny_channels", [])
    if not isinstance(deny_channels, list) or not all(isinstance(d, dict) for d in deny_channels):
        raise ConfigError("deny_channels must be a list of { id = \"...\" } entries")
    denied = frozenset(_snowflake(d.get("id"), "deny_channels id") for d in deny_channels)

    tz = raw.get("timezone", "UTC")
    if not _valid_timezone(tz):
        raise ConfigError("timezone must be an IANA time zone name such as Europe/Berlin or UTC")
    date_order = raw.get("date_order", "dmy")
    if date_order not in ("dmy", "mdy"):
        raise ConfigError('date_order must be "dmy" or "mdy"')

    return Config(
        allowlist=allowlist,
        deny=deny,
        limits=Limits(**given),
        allow_all_channels=allow_all,
        deny_channels=denied,
        timezone=tz,
        date_order=date_order,
    )
