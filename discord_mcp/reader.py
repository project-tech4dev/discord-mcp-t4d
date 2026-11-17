"""Reads messages from the channels and threads that access.py allowed, within the size limits.

`fetch` and `starter` read single channels and threads for every tool. On top of them, two
bulk reads: one channel with its threads (get_channel_overview, search), and every readable
channel on the server (get_my_pings, get_server_overview, get_open_loops). Both bound the
work of one request: the newest threads only, and one message budget.
Messages live only for the request; nothing is kept.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime

from discord_mcp.access import Access, View
from discord_mcp.config import Config
from discord_mcp.discord_client import DiscordError
from discord_mcp.errors import busy_or_denied, rate_limited_count
from discord_mcp.matching import ChannelMessages, Identity, RenderContext
from discord_mcp.messages import CONTENT_TYPES, TEXT_TYPES, channel_name, snowflake_time, thread_label
from discord_mcp.topics import Topic, build_topics


@dataclass
class ServerRead:
    """Every readable channel and thread over a window, read once for one request."""

    view: View
    ctx: RenderContext
    fetched: list[ChannelMessages]  # main channels and threads, oldest message first
    skipped: int  # channels or threads that could not be read (quiet skips not counted)
    capped: bool  # a channel hit its share of the message budget, so older messages were not read
    unread: set[str]  # channels or threads that could not be read (counted in skipped)
    rate_limited: int  # of those, how many Discord refused for sending too many requests


@dataclass
class ChannelRead:
    """One channel and its threads, grouped into topics, read once for one request."""

    view: View
    parent: dict
    topics: list[Topic]
    capped: bool  # threads were left out, or a fetch hit its cap, so older messages were not read
    channel_ids: list[str]  # channels and threads that were read
    skipped: int  # threads or fetches that failed (quiet skips not counted)
    rate_limited: int  # of those, how many Discord refused for sending too many requests
    cutoff: datetime  # start of the window that was read


def newest_threads(threads: list[dict], n: int) -> tuple[list[dict], bool]:
    """The n most recently active threads, and whether any were left out."""
    newest = sorted(threads, key=lambda t: int(t.get("last_message_id") or t["id"]), reverse=True)
    return newest[:n], len(newest) > n


class Reader:
    def __init__(self, client, access: Access, *, guild_id: str, config: Config, concurrency: int = 8) -> None:
        self._client = client
        self._access = access
        self._guild_id = guild_id
        self._config = config
        self._limits = config.limits
        self._concurrency = concurrency

    def context(self, view: View) -> RenderContext:
        return RenderContext(
            guild_id=self._guild_id,
            role_names=view.role_names,
            channel_names=view.readable_names,
            limits=self._limits,
            direct_only=frozenset(c for c, e in self._config.allowlist.items() if e.pings == "direct_only"),
        )

    async def fetch(
        self,
        identity: Identity,
        tool: str,
        targets: list[tuple[dict, str]],
        cutoff: datetime,
        max_count: int | None = None,
        before: dict[str, str] | None = None,
    ) -> tuple[list[ChannelMessages], list[str]]:
        """Messages per target. Returns (what was read, the failure code of each target that wasn't).

        `before`: channel id -> message id to read below, to continue an earlier read.
        """
        cap = self._limits.overview_max_messages if max_count is None else max_count
        sem = asyncio.Semaphore(self._concurrency)
        before = before or {}

        async def one(ch: dict, label: str) -> ChannelMessages | str:
            async with sem:
                try:
                    newest_first = await self._client.get_messages_since(ch["id"], cutoff, cap, before.get(ch["id"]))
                except DiscordError as e:
                    self._access.drop(identity, tool, ch["id"], e.code)
                    return e.code
                except Exception:
                    self._access.drop(identity, tool, ch["id"], "unexpected")
                    return "unexpected"
            return ChannelMessages(channel=ch, label=label, messages=list(reversed(newest_first)))

        results = await asyncio.gather(*(one(ch, label) for ch, label in targets))
        return [r for r in results if not isinstance(r, str)], [r for r in results if isinstance(r, str)]

    async def starter(self, parent: dict, thread_id: str) -> dict | None:
        """A thread's true first message: the parent message it was started from, else its oldest message."""
        container = parent["id"] if parent.get("type") in TEXT_TYPES else thread_id
        try:
            try:
                first = await self._client.get_message(container, thread_id)
            except DiscordError as e:
                if e.status != 404:
                    raise
                first = None
            # A thread created on its own leaves a "thread created" system message (type 18)
            # with the thread's id in the parent; its real first post is the thread's oldest message.
            if first is None or first.get("type") not in CONTENT_TYPES:
                first = await self._client.get_oldest_message(thread_id)
        except DiscordError:
            return None  # the topic still works without its starter
        return first if first and first.get("type") in CONTENT_TYPES else None

    # --- every readable channel -------------------------------------------------

    async def read_server(
        self, identity: Identity, tool: str, cutoff: datetime, channel_ids: list[str] | None = None
    ) -> ServerRead:
        """Read every permitted, readable channel and thread since `cutoff`, within one message budget."""
        view = await self._access.view(identity, tool, channel_ids)
        found = await self._access.threads(identity, tool, view, cutoff)
        lim = self._limits
        # Bound the work: the newest threads only; the rest show up as `truncated`.
        threads, dropped_threads = newest_threads(found.threads, lim.server_max_threads)
        channels = [c for c in view.visible if c["id"] not in found.failed]
        names = {c["id"]: channel_name(c) for c in channels}
        targets = [(c, channel_name(c)) for c in channels if c.get("type") in TEXT_TYPES]
        targets += [(t, thread_label(names.get(t.get("parent_id"), "?"), t)) for t in threads]

        # One hard message budget for the request: an equal share first, then what quiet channels
        # left unused goes back to the ones that filled their share.
        total = lim.server_request_messages
        cap = min(lim.overview_max_messages, max(1, total // max(len(targets), 1)))
        fetched, f_failed = await self.fetch(identity, tool, targets, cutoff, cap)
        caps = {cm.channel["id"]: cap for cm in fetched}
        full = [cm for cm in fetched if len(cm.messages) >= cap]
        spare = total - sum(len(cm.messages) for cm in fetched)
        if full and spare > 0 and cap < lim.overview_max_messages:
            bigger = min(lim.overview_max_messages, cap + spare // len(full))
            if bigger > cap:
                # Continue below each busy channel's oldest message, so nothing is read twice (issue #7).
                oldest = {cm.channel["id"]: cm.messages[0]["id"] for cm in full}
                again, _ = await self.fetch(
                    identity, tool, [(cm.channel, cm.label) for cm in full], cutoff, bigger - cap, oldest
                )
                older = {cm.channel["id"]: cm.messages for cm in again}
                fetched = [
                    replace(cm, messages=older[cid] + cm.messages) if (cid := cm.channel["id"]) in older else cm
                    for cm in fetched
                ]
                caps.update({cid: bigger for cid in older})
        got = {cm.channel["id"] for cm in fetched}
        return ServerRead(
            view=view,
            ctx=self.context(view),
            fetched=fetched,
            skipped=view.skipped + len(found.failed) + len(found.skipped) + len(f_failed),
            capped=dropped_threads or any(len(cm.messages) >= caps[cm.channel["id"]] for cm in fetched),
            unread=set(found.failed) | {ch["id"] for ch, _ in targets if ch["id"] not in got},
            rate_limited=rate_limited_count([*found.failed.values(), *found.skipped, *f_failed]),
        )

    # --- one channel and its threads ----------------------------------------------

    async def channel_topics(self, identity: Identity, tool: str, channel_id: str, cutoff: datetime) -> ChannelRead:
        """Topics in one channel and its threads. Raises AccessDenied before any message is fetched."""
        view = await self._access.one_channel(identity, tool, channel_id)
        parent = view.visible[0]
        found = await self._access.threads(identity, tool, view, cutoff)
        if parent["id"] in found.failed:
            raise busy_or_denied([found.failed[parent["id"]]])  # busy is "try again", not "forbidden"

        # Bound the work of one request: the newest threads only, and a shared message budget.
        lim = self._limits
        threads, dropped_threads = newest_threads(found.threads, lim.overview_max_threads)
        main_cap = lim.overview_max_messages
        thread_cap = min(main_cap, max(100, (lim.overview_request_messages - main_cap) // max(len(threads), 1)))

        name = channel_name(parent)
        main_fetch: list[ChannelMessages] = []
        main_failed: list[str] = []
        if parent.get("type") in TEXT_TYPES:
            main_fetch, main_failed = await self.fetch(identity, tool, [(parent, name)], cutoff, main_cap)
        thread_cms, thread_failed = await self.fetch(
            identity, tool, [(t, thread_label(name, t)) for t in threads], cutoff, thread_cap
        )
        main = main_fetch[0] if main_fetch else None
        capped = (
            dropped_threads
            or (main is not None and len(main.messages) >= main_cap)
            or any(len(cm.messages) >= thread_cap for cm in thread_cms)
        )

        # A thread that started before the window still shows its true first message.
        main_ids = {m["id"] for m in main.messages} if main else set()
        old_threads = [
            cm.channel["id"]
            for cm in thread_cms
            if cm.channel["id"] not in main_ids and snowflake_time(cm.channel["id"]) < cutoff
        ]
        sem = asyncio.Semaphore(self._concurrency)

        async def starter(tid: str) -> tuple[str, dict | None]:
            async with sem:
                return tid, await self.starter(parent, tid)

        starters = {tid: m for tid, m in await asyncio.gather(*(starter(t) for t in old_threads)) if m}

        failures = [*found.skipped, *main_failed, *thread_failed]
        return ChannelRead(
            view=view,
            parent=parent,
            topics=build_topics(main, thread_cms, starters, self.context(view)),
            capped=capped,
            channel_ids=[cm.channel["id"] for cm in main_fetch + thread_cms],
            skipped=len(failures),
            rate_limited=rate_limited_count(failures),
            cutoff=cutoff,
        )
