"""Runs each tool request: works out channels, fetches messages, matches pings.

A channel is returned only when it is on the allowlist, not on the deny list,
and readable by the person under Discord's permission rules (permissions.py).
Any channel that cannot be verified or read (lookup failure, 403, unknown
channel type, unrecognised permission data, any unexpected error) is dropped
from the result and its ID is logged. Nothing is kept after a request finishes.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from discord_mcp import audit
from discord_mcp.config import Config
from discord_mcp.discord_client import DiscordError
from discord_mcp.conversations import fit_budget, group_conversations, render_full, render_row
from discord_mcp.matching import CONTENT_TYPES, ChannelMessages, Identity, RenderContext, render_message
from discord_mcp.permissions import GuildSnapshot, can_read

TEXT_TYPES = frozenset({0, 5})  # text, announcement
FORUM_TYPES = frozenset({15})  # messages live only in its threads
THREAD_TYPES = frozenset({10, 11, 12})
PRIVATE_THREAD = 12
DISCORD_EPOCH_MS = 1420070400000
_LINK = re.compile(r"^https://discord\.com/channels/(\d+)/(\d+)/(\d+)$")
_CONV_ID = re.compile(r"^(\d+)/(\d+)$")


class InputError(ValueError):
    """Bad tool input. The message is fixed text and safe to show."""


class AccessDenied(PermissionError):
    """The channel is not on the allowlist or not visible to the person."""


class NotAMember(PermissionError):
    """The person is not (or no longer) a member of the Discord server."""


@dataclass
class ToolResult:
    payload: dict
    channel_ids: list[str]


@dataclass
class _View:
    """What one request knows about the server. Lives only for that request."""

    guild: GuildSnapshot
    channels: dict[str, dict]  # every guild channel by ID (structure only; never shown as-is)
    role_names: dict[str, str]
    readable_names: dict[str, str]  # names of channels this person can read, for <#id> mentions
    visible: list[dict]  # allowlisted channels the person can read
    skipped: int


def _snowflake_time(snowflake: str) -> datetime:
    return datetime.fromtimestamp(((int(snowflake) >> 22) + DISCORD_EPOCH_MS) / 1000, tz=UTC)


class Digest:
    def __init__(
        self,
        client,
        *,
        guild_id: str,
        config: Config,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        concurrency: int = 8,
    ) -> None:
        self._client = client
        self._guild_id = guild_id
        self._config = config
        self._limits = config.limits
        self._clock = clock
        self._concurrency = concurrency

    # --- identity -----------------------------------------------------------

    async def resolve_identity(self, user_id: str) -> Identity:
        """Current roles for the person, read by the bot. Raises NotAMember if they left."""
        try:
            member = await self._client.get_guild_member(self._guild_id, user_id)
        except DiscordError as e:
            if e.status == 404:
                raise NotAMember("not a member of the server") from None
            raise
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

    def _readable(
        self,
        view_guild: GuildSnapshot,
        identity: Identity,
        channel: dict,
        *,
        thread: dict | None = None,
        is_thread_member: bool | None = None,
    ) -> bool:
        try:
            return can_read(
                view_guild,
                identity.user_id,
                identity.role_ids,
                channel,
                thread=thread,
                is_thread_member=is_thread_member,
            )
        except Exception:
            return False  # UnrecognisedCase or malformed data: fail closed

    async def _view(self, identity: Identity, tool: str, wanted: list[str] | None) -> _View:
        ids = [c for c in self._config.allowlist if wanted is None or c in wanted]
        try:
            guild_info, roles, guild_channels = await asyncio.gather(
                self._client.get_guild(self._guild_id),
                self._client.get_guild_roles(self._guild_id),
                self._client.get_guild_channels(self._guild_id),
            )
            snapshot = GuildSnapshot(
                guild_id=self._guild_id,
                owner_id=str(guild_info["owner_id"]),
                role_permissions={r["id"]: r["permissions"] for r in roles},
            )
            channels = {c["id"]: c for c in guild_channels}
            role_names = {r["id"]: r["name"] for r in roles if r["id"] != self._guild_id}
        except Exception as e:
            code = f"guild_{e.code}" if isinstance(e, DiscordError) else "guild_unexpected"
            for cid in ids:
                self._drop(identity, tool, cid, code)
            empty = GuildSnapshot(self._guild_id, "", {})
            return _View(empty, {}, {}, {}, [], len(ids))

        visible: list[dict] = []
        skipped = 0
        for cid in ids:
            ch = channels.get(cid)
            code = None
            if ch is None:
                code = "not_found"
            elif ch.get("type") not in TEXT_TYPES | FORUM_TYPES:
                code = "unknown_type"
            elif (identity.user_id, cid) in self._config.deny:
                code = "denied"
            elif not self._readable(snapshot, identity, ch):
                code = "no_access"
            if code:
                self._drop(identity, tool, cid, code)
                skipped += 1
            else:
                visible.append(ch)
        readable_names = {
            cid: c.get("name", cid)
            for cid, c in channels.items()
            if c.get("type") in TEXT_TYPES | FORUM_TYPES and self._readable(snapshot, identity, c)
        }
        return _View(snapshot, channels, role_names, readable_names, visible, skipped)

    async def _threads(
        self, identity: Identity, tool: str, view: _View, cutoff: datetime
    ) -> tuple[list[dict], set[str], int]:
        """Readable threads under visible parents. Returns (threads, parents that failed, threads skipped).

        A parent whose thread lookup fails is dropped completely, so a channel
        the bot can no longer read is logged once and never half-read.
        """
        parents = {p["id"]: p for p in view.visible}
        failed: dict[str, str] = {}
        found: dict[str, dict] = {}
        try:
            for t in await self._client.get_active_threads(self._guild_id):
                if t.get("parent_id") in parents:
                    found[t["id"]] = t
        except DiscordError as e:
            failed = {pid: f"threads_{e.code}" for pid in parents}
        for pid in parents:
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
            pid = t.get("parent_id")
            if pid in failed:
                continue
            last = t.get("last_message_id")
            if last and _snowflake_time(last) < cutoff:
                continue
            code = None
            member: bool | None = None
            if t.get("type") not in THREAD_TYPES:
                code = "unknown_type"
            elif (identity.user_id, pid) in self._config.deny:
                code = "denied"
            else:
                if t.get("type") == PRIVATE_THREAD:
                    try:
                        member = await self._client.is_thread_member(t["id"], identity.user_id)
                    except DiscordError as e:
                        code = f"member_{e.code}"
                if code is None and not self._readable(
                    view.guild, identity, parents[pid], thread=t, is_thread_member=member
                ):
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

    def _context(self, view: _View) -> RenderContext:
        return RenderContext(
            guild_id=self._guild_id,
            role_names=view.role_names,
            channel_names=view.readable_names,
            limits=self._limits,
        )

    # --- tools ----------------------------------------------------------------

    async def list_my_channels(self, identity: Identity) -> ToolResult:
        view = await self._view(identity, "list_my_channels", None)
        out = [
            {
                "id": c["id"],
                "name": c.get("name"),
                "category": view.channels.get(c.get("parent_id") or "", {}).get("name"),
            }
            for c in view.visible
        ]
        return ToolResult({"channels": out, "skipped_channels": view.skipped}, [c["id"] for c in view.visible])

    async def get_my_pings(
        self, identity: Identity, days: int | None = None, channel_ids: list[str] | None = None
    ) -> ToolResult:
        tool = "get_my_pings"
        days = self._days(days, self._limits.default_days)
        cutoff = self._clock() - timedelta(days=days)

        view = await self._view(identity, tool, channel_ids)
        threads, failed, t_skipped = await self._threads(identity, tool, view, cutoff)
        channels = [c for c in view.visible if c["id"] not in failed]
        names = {c["id"]: c.get("name", c["id"]) for c in channels}
        targets = [(c, c.get("name", c["id"])) for c in channels if c.get("type") in TEXT_TYPES]
        targets += [(t, f"{names.get(t.get('parent_id'), '?')} › {t.get('name', t['id'])}") for t in threads]

        fetched, f_skipped = await self._fetch(identity, tool, targets, cutoff)
        ctx = self._context(view)
        rows = [render_row(c, identity, ctx) for c in group_conversations(fetched, identity, ctx)]
        base = {"days": days, "skipped_channels": view.skipped + len(failed) + t_skipped + f_skipped}
        return ToolResult(
            fit_budget(rows, base, self._limits.max_result_chars),
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

        view = await self._view(identity, tool, [channel_id])
        targets = [(c, c.get("name", c["id"])) for c in view.visible if c.get("type") in TEXT_TYPES]
        if not targets:
            raise AccessDenied("channel is not available")

        cutoff = self._clock() - timedelta(days=days)
        fetched, _ = await self._fetch(identity, tool, targets, cutoff)
        if not fetched:
            raise AccessDenied("channel is not available")
        cm = fetched[0]
        content = [m for m in reversed(cm.messages) if m.get("type") in CONTENT_TYPES]
        rendered = [render_message(m, cm.label, self._context(view)) for m in content[:limit]]
        return ToolResult(
            {"messages": rendered, "truncated": len(content) > limit, "days": days},
            [channel_id],
        )

    async def get_conversation(self, identity: Identity, link: str) -> ToolResult:
        tool = "get_conversation"
        link = link.strip() if isinstance(link, str) else ""
        if m := _LINK.match(link):
            guild_id, channel_id, message_id = m.groups()
            if guild_id != self._guild_id:
                raise InputError("that link is from a different Discord server")
        elif m := _CONV_ID.match(link):
            channel_id, message_id = m.groups()
        else:
            raise InputError("link must be a Discord message link or conversation id")

        days = self._limits.max_days
        cutoff = self._clock() - timedelta(days=days)
        view = await self._view(identity, tool, None)
        visible = {c["id"]: c for c in view.visible}
        threads, failed, _ = await self._threads(identity, tool, view, cutoff)
        readable_threads = {t["id"]: t for t in threads}

        if channel_id in visible and channel_id not in failed:
            parent = visible[channel_id]
        elif channel_id in readable_threads:
            parent = visible[readable_threads[channel_id]["parent_id"]]
        else:
            raise AccessDenied("channel is not available")

        name = parent.get("name", parent["id"])
        targets = [(parent, name)] if parent.get("type") in TEXT_TYPES else []
        targets += [(t, f"{name} › {t.get('name', t['id'])}") for t in threads if t.get("parent_id") == parent["id"]]
        fetched, _ = await self._fetch(identity, tool, targets, cutoff)

        ctx = self._context(view)
        for conv in group_conversations(fetched, identity, ctx):
            if any(msg["id"] == message_id for msg in conv.messages):
                return ToolResult(render_full(conv, ctx), [cm.channel["id"] for cm in fetched])
        raise InputError(f"no conversation found for that link in the last {days} days")
