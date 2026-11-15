"""Small accessors for Discord message and channel dicts, shared by every view.

No rendering and no decisions here: just the fields every module reads the same way.
"""

from __future__ import annotations

from datetime import datetime

# Normal messages and replies. Everything else (joins, pins, thread starters) is skipped.
CONTENT_TYPES = frozenset({0, 19})
TEXT_TYPES = frozenset({0, 5})  # text, announcement
FORUM_TYPES = frozenset({15})  # messages live only in its threads
READABLE_TYPES = TEXT_TYPES | FORUM_TYPES
THREAD_TYPES = frozenset({10, 11, 12})
PRIVATE_THREAD = 12
# Between a channel and a thread name in labels ("general › launch plan").
LABEL_SEP = " › "


def time_of(m: dict) -> datetime:
    return datetime.fromisoformat(m["timestamp"])


def author_id(m: dict) -> str | None:
    return (m.get("author") or {}).get("id")


def is_bot(m: dict) -> bool:
    return bool((m.get("author") or {}).get("bot"))


def msg_id(m: dict) -> int:
    return int(m["id"])


def content_messages(messages: list[dict]) -> list[dict]:
    """Only normal messages and replies."""
    return [m for m in messages if m.get("type") in CONTENT_TYPES]


def home_id(channel: dict) -> str | None:
    """The channel a thread lives in, or the channel itself."""
    return channel.get("parent_id") if channel.get("type") in THREAD_TYPES else channel["id"]


def message_link(guild_id: str, channel_id: str, message_id: str) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def channel_name(channel: dict) -> str:
    return channel.get("name", channel["id"])


def thread_label(parent_name: str, thread: dict) -> str:
    return f"{parent_name}{LABEL_SEP}{channel_name(thread)}"


def thread_title(label: str) -> str:
    """The thread name from a thread label, or "" if the label has none."""
    return label.split(LABEL_SEP, 1)[1] if LABEL_SEP in label else ""
