"""Runs each tool request: works out channels, fetches messages, matches pings.

Any channel that cannot be verified or read (lookup failure, 403, unknown
channel type, any unexpected error) is dropped from the result and its ID is
logged. Nothing is kept after a request finishes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from discord_mcp import audit
from discord_mcp.config import Config
from discord_mcp.discord_client import DiscordError
from discord_mcp.matching import CONTENT_TYPES, ChannelMessages, Identity, RenderContext, find_pings, render_message

TEXT_TYPES = frozenset({0, 5})  # text, announcement
FORUM_TYPES = frozenset({15})  # messages live only in its threads
THREAD_TYPES = frozenset({10, 11, 12})
PRIVATE_THREAD = 12
DISCORD_EPOCH_MS = 1420070400000


class InputError(ValueError):
    """Bad tool input. The message is fixed text and safe to show."""


class AccessDenied(PermissionError):
    """The channel is not on the allowlist or not visible to the person."""


class Access(Protocol):
    async def can_view(self, identity: Identity, channel: dict) -> bool: ...


class DevOwnerAccess:
    """Stage 1 only: local testing as the server owner, who can see every channel.

    Replaced by the real role calculation in stage 2. Private threads are
    always refused because thread membership is not checked yet.
    """

    async def can_view(self, identity: Identity, channel: dict) -> bool:
        return channel.get("type") != PRIVATE_THREAD


@dataclass
class ToolResult:
    payload: dict
    channel_ids: list[str]


def _snowflake_time(snowflake: str) -> datetime:
    return datetime.fromtimestamp(((int(snowflake) >> 22) + DISCORD_EPOCH_MS) / 1000, tz=UTC)


class Digest:
    def __init__(
        self,
        client,
        *,
        guild_id: str,
        config: Config,
        access: Access,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        concurrency: int = 8,
    ) -> None:
        self._client = client
        self._guild_id = guild_id
        self._config = config
        self._limits = config.limits
        self._access = access
        self._clock = clock
        self._concurrency = concurrency

    # --- identity -----------------------------------------------------------

    async def resolve_identity(self, user_id: str) -> Identity:
        member = await self._client.get_guild_member(self._guild_id, user_id)
        return Identity(user_id=user_id, role_ids=frozenset(member.get("roles", [])))

    # --- helpers ------------------------------------------------------------

    def _drop(self, identity: Identity, tool: str, channel_id: str, code: str) -> None:
        audit.record(
            user_id=identity.user_id,
            tool=tool,
            channel_ids=[channel_id],
            item_count=0,
            duration_ms=0,
            status=f"dropped:{code}",
        )

    def _days(self, days: int | None, default: int) -> int:
        days = default if days is None else days
        if not isinstance(days, int) or not 1 <= days <= self._limits.max_days:
            raise InputError(f"days must be between 1 and {self._limits.max_days}")
        return days

    async def _visible_channels(
        self, identity: Identity, tool: str, wanted: list[str] | None
    ) -> tuple[list[dict], dict[str, dict], int]:
        """Allowlisted channels this person can see. Returns (channels, all guild channels by id, skipped)."""
        guild = {c["id"]: c for c in await self._client.get_guild_channels(self._guild_id)}
        ids = [c for c in self._config.allowlist if wanted is None or c in wanted]
        visible: list[dict] = []
        skipped = 0
        for cid in ids:
            ch = guild.get(cid)
            code = None
            if ch is None:
                code = "not_found"
            elif ch.get("type") not in TEXT_TYPES | FORUM_TYPES:
                code = "unknown_type"
            elif (identity.user_id, cid) in self._config.deny:
                code = "denied"
            elif not await self._safe_can_view(identity, ch):
                code = "no_access"
            if code:
                self._drop(identity, tool, cid, code)
                skipped += 1
            else:
                visible.append(ch)
        return visible, guild, skipped

    async def _safe_can_view(self, identity: Identity, channel: dict) -> bool:
        try:
            return await self._access.can_view(identity, channel) is True
        except Exception:
            return False

    async def _threads(
        self, identity: Identity, tool: str, parents: list[dict], cutoff: datetime
    ) -> tuple[list[dict], set[str], int]:
        """Threads under visible parents. Returns (threads, parents that failed, threads skipped).

        A parent whose thread lookup fails is dropped completely, so a channel
        the bot can no longer read is logged once and never half-read.
        """
        parent_ids = [p["id"] for p in parents]
        failed: dict[str, str] = {}
        found: dict[str, dict] = {}
        try:
            for t in await self._client.get_active_threads(self._guild_id):
                if t.get("parent_id") in parent_ids:
                    found[t["id"]] = t
        except DiscordError as e:
            failed = {pid: f"threads_{e.code}" for pid in parent_ids}
        for pid in parent_ids:
            if pid in failed:
                continue
            try:
                for t in await self._client.get_public_archived_threads(pid, cutoff):
                    found.setdefault(t["id"], t)
            except DiscordError as e:
                failed[pid] = e.code
        for pid, code in failed.items():
            self._drop(identity, tool, pid, code)

        threads: list[dict] = []
        skipped = 0
        for t in found.values():
            if t.get("parent_id") in failed:
                continue
            last = t.get("last_message_id")
            if last and _snowflake_time(last) < cutoff:
                continue
            code = None
            if t.get("type") not in THREAD_TYPES:
                code = "unknown_type"
            elif not await self._safe_can_view(identity, t):
                code = "no_access"
            if code:
                self._drop(identity, tool, t["id"], code)
                skipped += 1
            else:
                threads.append(t)
        return threads, set(failed), skipped

    async def _fetch(
        self, identity: Identity, tool: str, targets: list[tuple[dict, str]], cutoff: datetime
    ) -> tuple[list[ChannelMessages], int]:
        sem = asyncio.Semaphore(self._concurrency)

        async def one(ch: dict, label: str) -> ChannelMessages | None:
            async with sem:
                try:
                    newest_first = await self._client.get_messages_since(ch["id"], cutoff)
                except DiscordError as e:
                    self._drop(identity, tool, ch["id"], e.code)
                    return None
                except Exception:
                    self._drop(identity, tool, ch["id"], "unexpected")
                    return None
            return ChannelMessages(channel=ch, label=label, messages=list(reversed(newest_first)))

        results = await asyncio.gather(*(one(ch, label) for ch, label in targets))
        ok = [r for r in results if r is not None]
        return ok, len(results) - len(ok)

    async def _context(self, guild: dict[str, dict]) -> RenderContext:
        roles = await self._client.get_guild_roles(self._guild_id)
        return RenderContext(
            guild_id=self._guild_id,
            role_names={r["id"]: r["name"] for r in roles if r["id"] != self._guild_id},
            channel_names={cid: c.get("name", cid) for cid, c in guild.items()},
            limits=self._limits,
        )

    # --- tools ----------------------------------------------------------------

    async def list_my_channels(self, identity: Identity) -> ToolResult:
        channels, guild, skipped = await self._visible_channels(identity, "list_my_channels", None)
        out = [
            {
                "id": c["id"],
                "name": c.get("name"),
                "category": guild.get(c.get("parent_id") or "", {}).get("name"),
            }
            for c in channels
        ]
        return ToolResult({"channels": out, "skipped_channels": skipped}, [c["id"] for c in channels])

    async def get_my_pings(
        self, identity: Identity, days: int | None = None, channel_ids: list[str] | None = None
    ) -> ToolResult:
        tool = "get_my_pings"
        days = self._days(days, self._limits.default_days)
        cutoff = self._clock() - timedelta(days=days)

        channels, guild, skipped = await self._visible_channels(identity, tool, channel_ids)
        threads, failed, t_skipped = await self._threads(identity, tool, channels, cutoff)
        channels = [c for c in channels if c["id"] not in failed]
        skipped += len(failed)
        names = {c["id"]: c.get("name", c["id"]) for c in channels}
        targets = [(c, c.get("name", c["id"])) for c in channels if c.get("type") in TEXT_TYPES]
        targets += [(t, f"{names.get(t.get('parent_id'), '?')} › {t.get('name', t['id'])}") for t in threads]

        fetched, f_skipped = await self._fetch(identity, tool, targets, cutoff)
        ctx = await self._context(guild)
        result = find_pings(fetched, identity, ctx)
        return ToolResult(
            {
                "pings": result.pings,
                "truncated": result.truncated,
                "days": days,
                "skipped_channels": skipped + t_skipped + f_skipped,
            },
            [cm.channel["id"] for cm in fetched],
        )

    async def get_channel_messages(
        self, identity: Identity, channel_id: str, days: int | None = None, limit: int | None = None
    ) -> ToolResult:
        tool = "get_channel_messages"
        days = self._days(days, self._limits.channel_default_days)
        limit = self._limits.channel_default_limit if limit is None else limit
        if not isinstance(limit, int) or not 1 <= limit <= self._limits.channel_max_limit:
            raise InputError(f"limit must be between 1 and {self._limits.channel_max_limit}")
        if channel_id not in self._config.allowlist:
            raise AccessDenied("channel is not available")

        channels, guild, _ = await self._visible_channels(identity, tool, [channel_id])
        targets = [(c, c.get("name", c["id"])) for c in channels if c.get("type") in TEXT_TYPES]
        if not targets:
            raise AccessDenied("channel is not available")

        cutoff = self._clock() - timedelta(days=days)
        fetched, _ = await self._fetch(identity, tool, targets, cutoff)
        if not fetched:
            raise AccessDenied("channel is not available")
        ctx = await self._context(guild)
        cm = fetched[0]
        content = [m for m in reversed(cm.messages) if m.get("type") in CONTENT_TYPES]
        rendered = [render_message(m, cm.label, ctx) for m in content[:limit]]
        return ToolResult(
            {"messages": rendered, "truncated": len(content) > limit, "days": days},
            [channel_id],
        )
