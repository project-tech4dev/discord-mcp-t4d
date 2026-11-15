"""Runs each tool request: checks its input, then reads and shapes the answer.

Which channels a person may read is decided in access.py (both gates); reading
messages within the size limits is in reader.py. Nothing is kept after a request finishes.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from discord_mcp.access import Access
from discord_mcp.config import Config
from discord_mcp.conversations import fit_budget, group_conversations, render_full, render_row
from discord_mcp.discord_client import DiscordError
from discord_mcp.errors import InputError, busy_or_denied, not_available, rate_limited_count
from discord_mcp.matching import ChannelMessages, Identity
from discord_mcp.messages import TEXT_TYPES, channel_name, content_messages, thread_label
from discord_mcp.reader import Reader, ServerRead
from discord_mcp.render import render_message
from discord_mcp.signals import Words, check_words
from discord_mcp.brief import (
    channel_entry,
    deadline_items,
    fit_loops,
    fit_server_overview,
    group_by_channel,
    open_loops,
)
from discord_mcp.topics import build_topics, fit_overview, fit_search, overview_row, render_topic, search_results

_LINK = re.compile(r"^https://discord\.com/channels/(\d+)/(\d+)/(\d+)$")
_CONV_ID = re.compile(r"^(\d+)/(\d+)$")
_THREAD_TOPIC = re.compile(r"^t:(\d+)$")
_MAIN_TOPIC = re.compile(r"^m:(\d+)/(\d+)$")


@dataclass
class ToolResult:
    payload: dict
    channel_ids: list[str]


def _with_rate_limited(base: dict, count: int) -> dict:
    """Add rate_limited_channels only when some channel was missed that way."""
    return {**base, "rate_limited_channels": count} if count else base


def _window(value: object, default: int, top: int, name: str) -> int:
    """A whole number between 1 and top, or the default; anything else is an InputError."""
    value = default if value is None else value
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= top:
        raise InputError(f"{name} must be between 1 and {top}")
    return value


def _words(words: object, max_count: int, what: str) -> list[str]:
    try:
        return check_words(words, max_count=max_count, what=what)
    except ValueError as e:
        raise InputError(str(e)) from None


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
        self._guild_id = guild_id
        self._config = config
        self._limits = config.limits
        self._clock = clock
        self._access = Access(client, guild_id=guild_id, config=config, concurrency=concurrency)
        self._reader = Reader(client, self._access, guild_id=guild_id, config=config, concurrency=concurrency)

    async def resolve_identity(self, user_id: str) -> Identity:
        """Current roles for the person, read by the bot. Raises NotAMember if they left."""
        return await self._access.resolve_identity(user_id)

    def _cutoff(self, days: int) -> datetime:
        return self._clock() - timedelta(days=days)

    # --- tools ----------------------------------------------------------------

    async def list_my_channels(self, identity: Identity) -> ToolResult:
        view = await self._access.view(identity, "list_my_channels", None)
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
        days = _window(days, self._limits.default_days, self._limits.max_days, "days")
        read = await self._reader.read_server(identity, tool, self._cutoff(days), channel_ids)
        ctx = read.ctx
        rows = [render_row(c, identity, ctx) for c in group_conversations(read.fetched, identity, ctx)]
        payload = fit_budget(
            rows,
            _with_rate_limited({"days": days, "skipped_channels": read.skipped}, read.rate_limited),
            self._limits.pings_max_chars,
            compact_chars=self._limits.summary_chars,
        )
        if read.capped:
            payload["truncated"] = True  # a fetch hit its cap, so older messages were not read
        return ToolResult(payload, [cm.channel["id"] for cm in read.fetched])

    async def get_channel_messages(
        self, identity: Identity, channel_id: str, days: int | None = None, limit: int | None = None
    ) -> ToolResult:
        tool = "get_channel_messages"
        lim = self._limits
        days = _window(days, lim.channel_default_days, lim.max_days, "days")
        limit = _window(limit, lim.channel_default_limit, lim.channel_max_limit, "limit")
        view = await self._access.one_channel(identity, tool, channel_id)
        channel = view.visible[0]
        if channel.get("type") not in TEXT_TYPES:
            raise not_available()

        target = [(channel, channel_name(channel))]
        fetched, _ = await self._reader.fetch(identity, tool, target, self._cutoff(days), limit + 1)  # one extra shows "truncated"
        if not fetched:
            raise not_available()
        cm = fetched[0]
        content = content_messages(list(reversed(cm.messages)))
        ctx = self._reader.context(view)
        rendered = [render_message(m, cm.label, ctx) for m in content[:limit]]
        return ToolResult(
            # more raw messages than the limit means older content may exist even if some were system messages
            {"messages": rendered, "truncated": len(content) > limit or len(cm.messages) > limit, "days": days},
            [channel_id],
        )

    async def get_conversation(self, identity: Identity, link: str) -> ToolResult:
        tool = "get_conversation"
        link = link.strip() if isinstance(link, str) else ""
        if m := _THREAD_TOPIC.match(link):
            return await self._open_thread(identity, tool, m.group(1))
        if m := _MAIN_TOPIC.match(link):
            return await self._open_main(identity, tool, m.group(1), m.group(2), anchored=True)
        if m := _LINK.match(link):
            guild_id, channel_id, message_id = m.groups()
            if guild_id != self._guild_id:
                raise InputError("that link is from a different Discord server")
        elif m := _CONV_ID.match(link):
            channel_id, message_id = m.groups()
        else:
            raise InputError("link must be a Discord message link or conversation id")

        cutoff = self._cutoff(self._limits.max_days)
        # Look up only the link's own channel (or a thread's parent), not every channel on the server.
        home = await self._access.home_of(channel_id)
        view = await self._access.view(identity, tool, [home])
        visible = {c["id"]: c for c in view.visible}
        found = await self._access.threads(identity, tool, view, cutoff)
        threads = found.threads
        readable_threads = {t["id"]: t for t in threads}

        if channel_id in found.failed:
            raise busy_or_denied([found.failed[channel_id]])
        if channel_id in visible:
            parent = visible[channel_id]
        elif channel_id in readable_threads:
            parent = visible[readable_threads[channel_id]["parent_id"]]
        else:
            # Possibly a thread quiet for longer than the ping window; this checks access itself.
            return await self._open_thread(identity, tool, channel_id, message_id)

        name = channel_name(parent)
        targets = [(parent, name)] if parent.get("type") in TEXT_TYPES else []
        targets += [(t, thread_label(name, t)) for t in threads if t.get("parent_id") == parent["id"]]
        fetched, codes = await self._reader.fetch(identity, tool, targets, cutoff)
        if not fetched and rate_limited_count(codes):
            raise DiscordError(429)

        ctx = self._reader.context(view)
        for conv in group_conversations(fetched, identity, ctx):
            if any(msg["id"] == message_id for msg in conv.messages):
                return ToolResult(render_full(conv, ctx), [cm.channel["id"] for cm in fetched])
        # Not part of a ping conversation: open the thread, or the main-channel stretch, holding it.
        if channel_id in readable_threads:
            return await self._open_thread(identity, tool, channel_id, message_id)
        return await self._open_main(identity, tool, parent["id"], message_id, anchored=False)

    async def _open_thread(
        self, identity: Identity, tool: str, thread_id: str, message_id: str | None = None
    ) -> ToolResult:
        """One thread over the overview window, fetching only that thread and its starter."""
        days = self._limits.overview_max_days
        cutoff = self._cutoff(days)
        view, parent, thread = await self._access.one_thread(identity, tool, thread_id, cutoff)
        label = thread_label(channel_name(parent), thread)
        fetched, codes = await self._reader.fetch(identity, tool, [(thread, label)], cutoff)
        if not fetched:
            raise busy_or_denied(codes)
        starter = await self._reader.starter(parent, thread_id)
        ctx = self._reader.context(view)
        topics = build_topics(None, fetched, {thread_id: starter} if starter else {}, ctx)
        if topics and (message_id is None or any(m["id"] == message_id for m in topics[0].messages)):
            return ToolResult(render_topic(topics[0], ctx), [thread_id])
        raise InputError(f"no conversation found for that link in the last {days} days")

    async def _open_main(
        self, identity: Identity, tool: str, channel_id: str, message_id: str, *, anchored: bool
    ) -> ToolResult:
        """The main-channel stretch holding a message, fetching only the main channel.

        `anchored` (an m: id) rebuilds from the stretch's own first message, so it opens
        exactly the stretch the overview row showed, whatever window the overview used.
        """
        days = self._limits.overview_max_days
        view = await self._access.one_channel(identity, tool, channel_id)
        parent = view.visible[0]
        not_found = InputError(f"no conversation found for that link in the last {days} days")
        if parent.get("type") not in TEXT_TYPES:
            raise not_found
        window = self._cutoff(days)
        found = await self._access.threads(identity, tool, view, window)
        if parent["id"] in found.failed:
            raise busy_or_denied([found.failed[parent["id"]]])
        fetched, codes = await self._reader.fetch(identity, tool, [(parent, channel_name(parent))], window)
        if not fetched:
            raise busy_or_denied(codes)
        main = fetched[0]
        if anchored:
            main = ChannelMessages(main.channel, main.label, [m for m in main.messages if int(m["id"]) >= int(message_id)])
        # Messages that started a thread belong to that thread, as in the overview.
        placeholders = [ChannelMessages(t, "", []) for t in found.threads]
        ctx = self._reader.context(view)
        for t in build_topics(main, placeholders, {}, ctx):
            if t.kind == "main" and any(m["id"] == message_id for m in t.messages):
                return ToolResult(render_topic(t, ctx), [channel_id])
        raise not_found

    # --- channel overview and search ----------------------------------------------

    async def get_channel_overview(
        self,
        identity: Identity,
        channel_id: str,
        days: int | None = None,
        look_for: list[str] | None = None,
        only_mine: bool = False,
    ) -> ToolResult:
        tool = "get_channel_overview"
        days = _window(days, self._limits.default_days, self._limits.overview_max_days, "days")
        look_for = _words(look_for or [], 20, "look_for")
        read = await self._reader.channel_topics(identity, tool, channel_id, self._cutoff(days))
        parent = read.parent
        ctx = self._reader.context(read.view)
        entry = self._config.allowlist.get(channel_id)
        extra = Words.build([*(entry.watch if entry else ()), *look_for])
        now = self._clock()
        quiet = parent["id"] in ctx.direct_only
        rows = [overview_row(t, identity, ctx, extra, now, quiet_channel=quiet) for t in read.topics]
        if only_mine:
            rows = [r for r in rows if r[0]["your_part"] != "none"]
        base = {
            "channel": channel_name(parent),
            "days": days,
            "look_for": look_for,
            "skipped_channels": read.skipped,
            "truncated": read.capped,
        }
        base = _with_rate_limited(base, read.rate_limited)
        return ToolResult(fit_overview(rows, base, self._limits.max_result_chars), read.channel_ids)

    async def search(
        self, identity: Identity, channel_id: str, words: list[str], days: int | None = None
    ) -> ToolResult:
        tool = "search"
        days = _window(days, 14, self._limits.overview_max_days, "days")
        words = _words(words, 10, "words")
        if not words:
            raise InputError("words must be a list of at most 10 words, each 2 to 40 characters")
        read = await self._reader.channel_topics(identity, tool, channel_id, self._cutoff(days))
        ctx = self._reader.context(read.view)
        results, count = search_results(read.topics, Words.build(words), ctx, since=read.cutoff)
        base = {
            "channel": channel_name(read.parent),
            "days": days,
            "match_count": count,
            "skipped_channels": read.skipped,
            "truncated": read.capped,
        }
        base = _with_rate_limited(base, read.rate_limited)
        return ToolResult(fit_search(results, base, self._limits.max_result_chars), read.channel_ids)

    # --- server-wide brief ------------------------------------------------------

    def _groups(self, read: ServerRead):
        names = {c["id"]: channel_name(c) for c in read.view.visible}

        def watch(cid: str) -> Words:
            entry = self._config.allowlist.get(cid)
            return Words.build(list(entry.watch) if entry else [])

        return group_by_channel(read.fetched, names, read.ctx, watch)

    async def get_server_overview(
        self,
        identity: Identity,
        days: int | None = None,
        deadline_days: int | None = None,
        upcoming_days: int | None = None,
    ) -> ToolResult:
        """Last week's discussion across every readable channel, and deadlines coming up."""
        tool = "get_server_overview"
        top = self._limits.overview_max_days
        days = _window(days, 7, top, "days")
        deadline_days = _window(deadline_days, top, top, "deadline_days")
        upcoming_days = _window(upcoming_days, 7, 14, "upcoming_days")
        now = self._clock()
        tz = ZoneInfo(self._config.timezone)
        read = await self._reader.read_server(identity, tool, now - timedelta(days=max(days, deadline_days)))
        groups = self._groups(read)

        since = now - timedelta(days=days)
        pairs = [(g, channel_entry(g, since, read.ctx)) for g in groups]
        entries = [e for _, e in pairs if e is not None]
        active = {g.channel["id"] for g, e in pairs if e is not None}
        quiet = [c for c in read.view.visible if c["id"] not in active and c["id"] not in read.unread]

        today = now.astimezone(tz).date()
        deadlines = deadline_items(
            groups, now - timedelta(days=deadline_days), today, today + timedelta(days=upcoming_days),
            tz, self._config.date_order, read.ctx,
        )
        cap = self._limits.brief_max_deadlines
        base = {
            "days": days,
            "deadline_days": deadline_days,
            "upcoming_days": upcoming_days,
            "today": today.isoformat(),
            "quiet_channels": len(quiet),
            "skipped_channels": read.skipped,
            "omitted_deadlines": max(len(deadlines) - cap, 0),
            "truncated": read.capped,
        }
        base = _with_rate_limited(base, read.rate_limited)
        payload = fit_server_overview(
            entries, deadlines[:cap], base, self._limits.max_result_chars, self._limits.brief_topics_per_channel
        )
        return ToolResult(payload, [cm.channel["id"] for cm in read.fetched])

    async def get_open_loops(self, identity: Identity, days: int | None = None) -> ToolResult:
        """Your promises not yet followed up, and what others owe you, across every readable channel."""
        tool = "get_open_loops"
        top = self._limits.overview_max_days
        days = _window(days, top, top, "days")
        now = self._clock()
        read = await self._reader.read_server(identity, tool, now - timedelta(days=days))
        found = open_loops(self._groups(read), identity, read.ctx, now)
        base = _with_rate_limited(
            {"days": days, "skipped_channels": read.skipped, "truncated": read.capped}, read.rate_limited
        )
        return ToolResult(fit_loops(found, base, self._limits.max_result_chars), [cm.channel["id"] for cm in read.fetched])
