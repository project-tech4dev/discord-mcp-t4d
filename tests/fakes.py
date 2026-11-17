"""Fake Discord objects for tests. No real data."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from discord_mcp.discord_client import DiscordError
from discord_mcp.permissions import READ_MESSAGE_HISTORY, VIEW_CHANNEL

NOW = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)
GUILD_ID = "900"
ME = "1001"
OTHER = "1002"
BOT_USER = "1003"
THIRD = "1004"
ROLE_DEV = "501"
ROLE_OPS = "502"
OWNER = "1"
READ = VIEW_CHANNEL | READ_MESSAGE_HISTORY


def ts(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat()


def user(uid: str, name: str | None = None, *, bot: bool = False, nick: str | None = None) -> dict:
    u = {"id": uid, "username": name or f"user{uid}", "global_name": name, "bot": bot}
    if nick:
        u["member"] = {"nick": nick}
    return u


def msg(
    mid: str,
    channel_id: str,
    author_id: str = OTHER,
    content: str = "hello",
    *,
    hours_ago: float = 1,
    mentions: list[dict] | None = None,
    mention_roles: list[str] | None = None,
    mention_everyone: bool = False,
    referenced: dict | None = None,
    bot: bool = False,
    thread: dict | None = None,
    mtype: int | None = None,
) -> dict:
    m = {
        "id": mid,
        "channel_id": channel_id,
        "type": mtype if mtype is not None else (19 if referenced else 0),
        "author": user(author_id, bot=bot),
        "content": content,
        "timestamp": ts(hours_ago),
        "mentions": mentions or [],
        "mention_roles": mention_roles or [],
        "mention_everyone": mention_everyone,
    }
    if referenced is not None:
        m["message_reference"] = {"message_id": referenced["id"], "channel_id": channel_id}
        m["referenced_message"] = referenced
    if thread is not None:
        m["thread"] = thread
    return m


def channel(cid: str, name: str, ctype: int = 0, parent_id: str | None = None, **extra) -> dict:
    c = {"id": cid, "name": name, "type": ctype, "guild_id": GUILD_ID}
    if parent_id:
        c["parent_id"] = parent_id
    c.update(extra)
    return c


class FakeClient:
    """Stands in for DiscordClient. Messages are stored newest first per channel."""

    def __init__(self) -> None:
        self.channels: dict[str, dict] = {}
        self.messages: dict[str, list[dict]] = {}
        self.owner_id = OWNER
        self.roles: list[dict] = [
            {"id": GUILD_ID, "name": "@everyone", "permissions": str(READ)},
            {"id": ROLE_DEV, "name": "dev", "permissions": "0"},
            {"id": ROLE_OPS, "name": "ops", "permissions": "0"},
        ]
        self.thread_members: dict[str, set[str]] = {}
        self.fail_guild: int | None = None
        self.members: dict[str, dict] = {ME: {"user": user(ME, "Me"), "roles": [ROLE_DEV]}}
        self.active_threads: list[dict] = []
        self.archived_threads: dict[str, list[dict]] = {}
        self.fail: dict[str, int] = {}  # channel id -> HTTP status to raise
        self.calls: list[str] = []
        self.max_counts: dict[str, int | None] = {}
        self.returned: dict[str, list[str]] = {}  # channel id -> every message id handed out, in order

    def add_channel(self, c: dict, messages: list[dict]) -> None:
        self.channels[c["id"]] = c
        self.messages[c["id"]] = sorted(messages, key=lambda m: int(m["id"]), reverse=True)

    def _maybe_fail(self, cid: str) -> None:
        if cid in self.fail:
            raise DiscordError(self.fail[cid])

    async def get_channel(self, channel_id: str) -> dict:
        self.calls.append(f"channel:{channel_id}")
        self._maybe_fail(channel_id)
        if channel_id not in self.channels:
            raise DiscordError(404)
        return self.channels[channel_id]

    async def get_current_user(self) -> dict:
        self.calls.append("users_me")
        return {"id": BOT_USER, "username": "bot", "bot": True}

    async def get_guild(self, guild_id: str) -> dict:
        if self.fail_guild:
            raise DiscordError(self.fail_guild)
        return {"id": guild_id, "owner_id": self.owner_id}

    async def is_thread_member(self, thread_id: str, user_id: str) -> bool:
        self._maybe_fail(thread_id)
        return user_id in self.thread_members.get(thread_id, set())

    async def get_guild_channels(self, guild_id: str) -> list[dict]:
        self.calls.append("guild_channels")
        return [c for c in self.channels.values() if c["type"] not in (10, 11, 12)]

    async def get_guild_roles(self, guild_id: str) -> list[dict]:
        return self.roles

    async def get_guild_member(self, guild_id: str, user_id: str) -> dict:
        if user_id not in self.members:
            raise DiscordError(404)
        return self.members[user_id]

    async def get_active_threads(self, guild_id: str) -> list[dict]:
        return self.active_threads

    async def get_public_archived_threads(self, channel_id: str, since: datetime) -> list[dict]:
        self.calls.append(f"archived:{channel_id}")
        self._maybe_fail(channel_id)
        return self.archived_threads.get(channel_id, [])

    async def get_messages_since(
        self, channel_id: str, cutoff: datetime, max_count: int | None = None, before: str | None = None
    ) -> list[dict]:
        self.calls.append(f"messages:{channel_id}")
        self.max_counts[channel_id] = max_count
        self._maybe_fail(channel_id)
        found = [
            m
            for m in self.messages.get(channel_id, [])
            if datetime.fromisoformat(m["timestamp"]) >= cutoff and (before is None or int(m["id"]) < int(before))
        ]
        found = found[:max_count] if max_count else found
        self.returned.setdefault(channel_id, []).extend(m["id"] for m in found)
        return found

    async def get_message(self, channel_id: str, message_id: str) -> dict:
        self.calls.append(f"message:{channel_id}/{message_id}")
        self._maybe_fail(channel_id)
        for m in self.messages.get(channel_id, []):
            if m["id"] == message_id:
                return m
        raise DiscordError(404)

    async def get_oldest_message(self, channel_id: str) -> dict | None:
        self.calls.append(f"oldest:{channel_id}")
        self._maybe_fail(channel_id)
        msgs = self.messages.get(channel_id, [])
        return msgs[-1] if msgs else None
