"""Runs each tool request: works out channels, fetches messages, matches pings.

A channel is returned only when configuration permits it (the allowlist, or
every channel when allow_all_channels is on), it is not on the channel deny
list (itself, its category, or for threads their parent), the person is not
denied it, and it is readable by the person under Discord's permission rules
(permissions.py). In allowlist mode, any listed channel that cannot be verified
or read is dropped and its ID is logged. In allow-all mode, channels a person
cannot see are skipped quietly so their number is never revealed, and a
failed server lookup fails the call instead of returning an empty result. Nothing is kept after a request finishes.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from discord_mcp import audit
from discord_mcp.config import Config
from discord_mcp.discord_client import DiscordError
from discord_mcp.conversations import fit_budget, group_conversations, render_full, render_row
from discord_mcp.matching import CONTENT_TYPES, ChannelMessages, Identity, RenderContext, render_message
from discord_mcp.signals import Words, check_words
from discord_mcp.brief import (
    channel_entry,
    deadline_items,
    fit_loops,
    fit_server_overview,
    group_by_channel,
    open_loops,
)
from discord_mcp.topics import Topic, build_topics, fit_overview, fit_search, overview_row, render_topic, search_results
from discord_mcp.permissions import GuildSnapshot, can_read

TEXT_TYPES = frozenset({0, 5})  # text, announcement
FORUM_TYPES = frozenset({15})  # messages live only in its threads
READABLE_TYPES = TEXT_TYPES | FORUM_TYPES
THREAD_TYPES = frozenset({10, 11, 12})
PRIVATE_THREAD = 12
DISCORD_EPOCH_MS = 1420070400000
_LINK = re.compile(r"^https://discord\.com/channels/(\d+)/(\d+)/(\d+)$")
_CONV_ID = re.compile(r"^(\d+)/(\d+)$")
_THREAD_TOPIC = re.compile(r"^t:(\d+)$")
_MAIN_TOPIC = re.compile(r"^m:(\d+)/(\d+)$")


class InputError(ValueError):
    """Bad tool input. The message is fixed text and safe to show."""


class AccessDenied(PermissionError):
    """The channel is not permitted by configuration, is denied, or is not visible to the person."""


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
    visible: list[dict]  # permitted channels the person can read
    skipped: int


@dataclass
class _ServerRead:
    """Every readable channel and thread over a window, read once for one request."""

    view: _View
    ctx: RenderContext
    fetched: list[ChannelMessages]  # main channels and threads, oldest message first
    skipped: int  # channels or threads that could not be read (quiet skips not counted)
    capped: bool  # a channel hit its share of the message budget, so older messages were not read
    unread: set[str]  # channels or threads that could not be read (counted in skipped)


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

    @staticmethod
    def _window(value: object, default: int, top: int, name: str) -> int:
        value = default if value is None else value
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= top:
            raise InputError(f"{name} must be between 1 and {top}")
        return value

    def _days(self, days: int | None, default: int, maximum: int | None = None) -> int:
        top = self._limits.max_days if maximum is None else maximum
        days = default if days is None else days
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= top:
            raise InputError(f"days must be between 1 and {top}")
        return days

    @staticmethod
    def _words(words: object, max_count: int, what: str) -> list[str]:
        try:
            return check_words(words, max_count=max_count, what=what)
        except ValueError as e:
            raise InputError(str(e)) from None

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

    def _denied(self, channel: dict) -> bool:
        """On the channel deny list itself, or inside a denied category."""
        d = self._config.deny_channels
        return channel.get("id") in d or channel.get("parent_id") in d

    def _may_read(self, channel_id: str) -> bool:
        """The configuration allows this channel id at all (deny list first, then allow-all or allowlist)."""
        if channel_id in self._config.deny_channels:
            return False
        return self._config.allow_all_channels or channel_id in self._config.allowlist

    async def _view(self, identity: Identity, tool: str, wanted: list[str] | None) -> _View:
        allow_all = self._config.allow_all_channels
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
            if allow_all:
                # With no fixed channel list there is nothing to report as skipped, and an
                # empty result would look like "nothing needs you". Fail the call instead.
                raise e if isinstance(e, DiscordError) else DiscordError(None) from None
            code = f"guild_{e.code}" if isinstance(e, DiscordError) else "guild_unexpected"
            for cid in ids:
                self._drop(identity, tool, cid, code)
            empty = GuildSnapshot(self._guild_id, "", {})
            return _View(empty, {}, {}, {}, [], len(ids))

        if allow_all:
            # Every channel that can hold messages; unreadable or denied ones are skipped quietly,
            # so the result never reveals how many channels a person cannot see.
            ids = [
                cid
                for cid, c in channels.items()
                if c.get("type") in READABLE_TYPES and (wanted is None or cid in wanted)
            ]
        visible: list[dict] = []
        skipped = 0
        for cid in ids:
            ch = channels.get(cid)
            if allow_all and (self._denied(ch) or not self._readable(snapshot, identity, ch)):
                continue  # quietly: never reveal hidden or denied channels by count or log line
            code = None
            if ch is None:
                code = "not_found"
            elif ch.get("type") not in READABLE_TYPES:
                code = "unknown_type"
            elif self._denied(ch):
                code = "deny_list"
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
            if c.get("type") in READABLE_TYPES
            and not self._denied(c)
            and (identity.user_id, cid) not in self._config.deny
            and self._readable(snapshot, identity, c)
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
        sem = asyncio.Semaphore(self._concurrency)

        async def archived(pid: str) -> tuple[str, list[dict] | str]:
            async with sem:
                try:
                    return pid, await self._client.get_public_archived_threads(pid, cutoff)
                except DiscordError as e:
                    return pid, e.code

        for pid, got in await asyncio.gather(*(archived(p) for p in parents if p not in failed)):
            if isinstance(got, str):
                failed[pid] = got
            else:
                for t in got:
                    found.setdefault(t["id"], t)
        for pid, code in failed.items():
            self._drop(identity, tool, pid, code)

        threads: list[dict] = []
        skipped = 0
        for t in found.values():
            if t["id"] in self._config.deny_channels:
                continue  # a denied thread is never read and never mentioned
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
            if self._config.allow_all_channels and (code == "no_access" or (code or "").startswith("member_")):
                # Quietly, as for channels: never reveal private threads by count or log line. A failed
                # membership lookup is quiet too, since the person may not be in the thread.
                continue
            if code:
                self._drop(identity, tool, t["id"], code)
                skipped += 1
            else:
                threads.append(t)
        return threads, set(failed), skipped

    async def _fetch(
        self,
        identity: Identity,
        tool: str,
        targets: list[tuple[dict, str]],
        cutoff: datetime,
        max_count: int | None = None,
    ) -> tuple[list[ChannelMessages], int]:
        cap = self._limits.overview_max_messages if max_count is None else max_count
        sem = asyncio.Semaphore(self._concurrency)

        async def one(ch: dict, label: str) -> ChannelMessages | None:
            async with sem:
                try:
                    newest_first = await self._client.get_messages_since(ch["id"], cutoff, cap)
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
            direct_only=frozenset(c for c, e in self._config.allowlist.items() if e.pings == "direct_only"),
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

    async def _read_server(
        self, identity: Identity, tool: str, cutoff: datetime, channel_ids: list[str] | None = None
    ) -> _ServerRead:
        """Read every permitted, readable channel and thread since `cutoff`, within one message budget."""
        view = await self._view(identity, tool, channel_ids)
        threads, failed, t_skipped = await self._threads(identity, tool, view, cutoff)
        lim = self._limits
        # Bound the work: the newest threads only; the rest show up as `truncated`.
        newest = sorted(threads, key=lambda t: int(t.get("last_message_id") or t["id"]), reverse=True)
        dropped_threads = len(newest) > lim.server_max_threads
        threads = newest[: lim.server_max_threads]
        channels = [c for c in view.visible if c["id"] not in failed]
        names = {c["id"]: c.get("name", c["id"]) for c in channels}
        targets = [(c, c.get("name", c["id"])) for c in channels if c.get("type") in TEXT_TYPES]
        targets += [(t, f"{names.get(t.get('parent_id'), '?')} › {t.get('name', t['id'])}") for t in threads]

        # One hard message budget for the request: an equal share first, then what quiet channels
        # left unused goes back to the ones that filled their share.
        total = lim.server_request_messages
        cap = min(lim.overview_max_messages, max(1, total // max(len(targets), 1)))
        fetched, f_skipped = await self._fetch(identity, tool, targets, cutoff, cap)
        caps = {cm.channel["id"]: cap for cm in fetched}
        full = [cm for cm in fetched if len(cm.messages) >= cap]
        spare = total - sum(len(cm.messages) for cm in fetched)
        if full and spare > 0 and cap < lim.overview_max_messages:
            bigger = min(lim.overview_max_messages, cap + spare // len(full))
            if bigger > cap:
                again, _ = await self._fetch(identity, tool, [(cm.channel, cm.label) for cm in full], cutoff, bigger)
                more = {cm.channel["id"]: cm for cm in again}
                fetched = [more.get(cm.channel["id"], cm) for cm in fetched]
                caps.update({cid: bigger for cid in more})
        got = {cm.channel["id"] for cm in fetched}
        return _ServerRead(
            view=view,
            ctx=self._context(view),
            fetched=fetched,
            skipped=view.skipped + len(failed) + t_skipped + f_skipped,
            capped=dropped_threads or any(len(cm.messages) >= caps[cm.channel["id"]] for cm in fetched),
            unread=set(failed) | {ch["id"] for ch, _ in targets if ch["id"] not in got},
        )

    async def get_my_pings(
        self, identity: Identity, days: int | None = None, channel_ids: list[str] | None = None
    ) -> ToolResult:
        tool = "get_my_pings"
        days = self._days(days, self._limits.default_days)
        read = await self._read_server(identity, tool, self._clock() - timedelta(days=days), channel_ids)
        ctx = read.ctx
        rows = [render_row(c, identity, ctx) for c in group_conversations(read.fetched, identity, ctx)]
        payload = fit_budget(
            rows,
            {"days": days, "skipped_channels": read.skipped},
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
        days = self._days(days, self._limits.channel_default_days)
        limit = self._limits.channel_default_limit if limit is None else limit
        if not isinstance(limit, int) or not 1 <= limit <= self._limits.channel_max_limit:
            raise InputError(f"limit must be between 1 and {self._limits.channel_max_limit}")
        if not self._may_read(channel_id):
            raise AccessDenied("channel is not available")

        view = await self._view(identity, tool, [channel_id])
        targets = [(c, c.get("name", c["id"])) for c in view.visible if c.get("type") in TEXT_TYPES]
        if not targets:
            raise AccessDenied("channel is not available")

        cutoff = self._clock() - timedelta(days=days)
        fetched, _ = await self._fetch(identity, tool, targets, cutoff, limit + 1)  # one extra shows "truncated"
        if not fetched:
            raise AccessDenied("channel is not available")
        cm = fetched[0]
        content = [m for m in reversed(cm.messages) if m.get("type") in CONTENT_TYPES]
        rendered = [render_message(m, cm.label, self._context(view)) for m in content[:limit]]
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

        days = self._limits.max_days
        cutoff = self._clock() - timedelta(days=days)
        # Look up only the link's own channel (or a thread's parent), not every channel on the server.
        try:
            meta = await self._client.get_channel(channel_id)
        except DiscordError:
            raise AccessDenied("channel is not available") from None
        home = meta.get("parent_id") if meta.get("type") in THREAD_TYPES else channel_id
        if not home or not self._may_read(home):
            raise AccessDenied("channel is not available")
        view = await self._view(identity, tool, [home])
        visible = {c["id"]: c for c in view.visible}
        threads, failed, _ = await self._threads(identity, tool, view, cutoff)
        readable_threads = {t["id"]: t for t in threads}

        if channel_id in visible and channel_id not in failed:
            parent = visible[channel_id]
        elif channel_id in readable_threads:
            parent = visible[readable_threads[channel_id]["parent_id"]]
        else:
            # Possibly a thread quiet for longer than the ping window; this checks access itself.
            return await self._open_thread(identity, tool, channel_id, message_id)

        name = parent.get("name", parent["id"])
        targets = [(parent, name)] if parent.get("type") in TEXT_TYPES else []
        targets += [(t, f"{name} › {t.get('name', t['id'])}") for t in threads if t.get("parent_id") == parent["id"]]
        fetched, _ = await self._fetch(identity, tool, targets, cutoff)

        ctx = self._context(view)
        for conv in group_conversations(fetched, identity, ctx):
            if any(msg["id"] == message_id for msg in conv.messages):
                return ToolResult(render_full(conv, ctx), [cm.channel["id"] for cm in fetched])
        # Not part of a ping conversation: open the thread, or the main-channel stretch, holding it.
        if channel_id in readable_threads:
            return await self._open_thread(identity, tool, channel_id, message_id)
        return await self._open_main(identity, tool, parent["id"], message_id, anchored=False)

    async def _thread_access(self, identity: Identity, tool: str, thread_id: str, days: int) -> tuple[_View, dict, dict]:
        """(view, parent, thread) for one readable thread, checked without fetching any messages."""
        try:
            meta = await self._client.get_channel(thread_id)
        except DiscordError:
            raise AccessDenied("channel is not available") from None
        parent_id = meta.get("parent_id")
        if meta.get("type") not in THREAD_TYPES or not self._may_read(parent_id) or thread_id in self._config.deny_channels:
            raise AccessDenied("channel is not available")
        view = await self._view(identity, tool, [parent_id])
        if not view.visible:
            raise AccessDenied("channel is not available")
        threads, _, _ = await self._threads(identity, tool, view, self._clock() - timedelta(days=days))
        thread = next((t for t in threads if t["id"] == thread_id), None)
        if thread is None:
            raise AccessDenied("channel is not available")
        return view, view.visible[0], thread

    async def _starter(self, parent: dict, thread_id: str) -> dict | None:
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

    async def _open_thread(
        self, identity: Identity, tool: str, thread_id: str, message_id: str | None = None
    ) -> ToolResult:
        """One thread over the overview window, fetching only that thread and its starter."""
        days = self._limits.overview_max_days
        view, parent, thread = await self._thread_access(identity, tool, thread_id, days)
        label = f"{parent.get('name', parent['id'])} › {thread.get('name', thread_id)}"
        fetched, _ = await self._fetch(identity, tool, [(thread, label)], self._clock() - timedelta(days=days))
        if not fetched:
            raise AccessDenied("channel is not available")
        starter = await self._starter(parent, thread_id)
        ctx = self._context(view)
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
        if not self._may_read(channel_id):
            raise AccessDenied("channel is not available")
        view = await self._view(identity, tool, [channel_id])
        if not view.visible:
            raise AccessDenied("channel is not available")
        parent = view.visible[0]
        not_found = InputError(f"no conversation found for that link in the last {days} days")
        if parent.get("type") not in TEXT_TYPES:
            raise not_found
        window = self._clock() - timedelta(days=days)
        threads, failed, _ = await self._threads(identity, tool, view, window)
        if parent["id"] in failed:
            raise AccessDenied("channel is not available")
        fetched, _ = await self._fetch(identity, tool, [(parent, parent.get("name", parent["id"]))], window)
        if not fetched:
            raise AccessDenied("channel is not available")
        main = fetched[0]
        if anchored:
            main = ChannelMessages(main.channel, main.label, [m for m in main.messages if int(m["id"]) >= int(message_id)])
        # Messages that started a thread belong to that thread, as in the overview.
        placeholders = [ChannelMessages(t, "", []) for t in threads]
        ctx = self._context(view)
        for t in build_topics(main, placeholders, {}, ctx):
            if t.kind == "main" and any(m["id"] == message_id for m in t.messages):
                return ToolResult(render_topic(t, ctx), [channel_id])
        raise not_found

    # --- channel overview and search ----------------------------------------------

    async def _channel_topics(
        self, identity: Identity, tool: str, channel_id: str, days: int
    ) -> tuple[_View, dict, list[Topic], bool, list[str], int, datetime]:
        """Topics in one channel and its threads. Raises AccessDenied before any message is fetched."""
        if not self._may_read(channel_id):
            raise AccessDenied("channel is not available")
        view = await self._view(identity, tool, [channel_id])
        if not view.visible:
            raise AccessDenied("channel is not available")
        parent = view.visible[0]
        cutoff = self._clock() - timedelta(days=days)
        threads, failed, t_skipped = await self._threads(identity, tool, view, cutoff)
        if parent["id"] in failed:
            raise AccessDenied("channel is not available")

        # Bound the work of one request: the newest threads only, and a shared message budget.
        lim = self._limits
        newest = sorted(threads, key=lambda t: int(t.get("last_message_id") or t["id"]), reverse=True)
        dropped_threads = len(newest) > lim.overview_max_threads
        threads = newest[: lim.overview_max_threads]
        main_cap = lim.overview_max_messages
        thread_cap = min(main_cap, max(100, (lim.overview_request_messages - main_cap) // max(len(threads), 1)))

        name = parent.get("name", parent["id"])
        main_fetch: list[ChannelMessages] = []
        if parent.get("type") in TEXT_TYPES:
            main_fetch, main_skipped = await self._fetch(identity, tool, [(parent, name)], cutoff, main_cap)
        else:
            main_skipped = 0
        thread_cms, skipped_threads = await self._fetch(
            identity, tool, [(t, f"{name} › {t.get('name', t['id'])}") for t in threads], cutoff, thread_cap
        )
        fetched = main_fetch + thread_cms
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
            if cm.channel["id"] not in main_ids and _snowflake_time(cm.channel["id"]) < cutoff
        ]
        sem = asyncio.Semaphore(self._concurrency)

        async def starter(tid: str) -> tuple[str, dict | None]:
            async with sem:
                return tid, await self._starter(parent, tid)

        starters = {tid: m for tid, m in await asyncio.gather(*(starter(t) for t in old_threads)) if m}

        topics = build_topics(main, thread_cms, starters, self._context(view))
        skipped = t_skipped + main_skipped + skipped_threads
        return view, parent, topics, capped, [cm.channel["id"] for cm in fetched], skipped, cutoff

    async def get_channel_overview(
        self,
        identity: Identity,
        channel_id: str,
        days: int | None = None,
        look_for: list[str] | None = None,
        only_mine: bool = False,
    ) -> ToolResult:
        tool = "get_channel_overview"
        days = self._days(days, self._limits.default_days, self._limits.overview_max_days)
        look_for = self._words(look_for or [], 20, "look_for")
        view, parent, topics, capped, fetched, skipped, _ = await self._channel_topics(
            identity, tool, channel_id, days
        )
        ctx = self._context(view)
        entry = self._config.allowlist.get(channel_id)
        extra = Words.build([*(entry.watch if entry else ()), *look_for])
        now = self._clock()
        quiet = parent["id"] in ctx.direct_only
        rows = [overview_row(t, identity, ctx, extra, now, quiet_channel=quiet) for t in topics]
        if only_mine:
            rows = [r for r in rows if r[0]["your_part"] != "none"]
        base = {
            "channel": parent.get("name", parent["id"]),
            "days": days,
            "look_for": look_for,
            "skipped_channels": skipped,
            "truncated": capped,
        }
        return ToolResult(fit_overview(rows, base, self._limits.max_result_chars), fetched)

    async def search(
        self, identity: Identity, channel_id: str, words: list[str], days: int | None = None
    ) -> ToolResult:
        tool = "search"
        days = self._days(days, 14, self._limits.overview_max_days)
        words = self._words(words, 10, "words")
        if not words:
            raise InputError("words must be a list of at most 10 words, each 2 to 40 characters")
        view, parent, topics, capped, fetched, skipped, cutoff = await self._channel_topics(
            identity, tool, channel_id, days
        )
        results, count = search_results(topics, Words.build(words), self._context(view), since=cutoff)
        base = {
            "channel": parent.get("name", parent["id"]),
            "days": days,
            "match_count": count,
            "skipped_channels": skipped,
            "truncated": capped,
        }
        return ToolResult(fit_search(results, base, self._limits.max_result_chars), fetched)

    # --- server-wide brief ------------------------------------------------------

    def _groups(self, read: _ServerRead):
        names = {c["id"]: c.get("name", c["id"]) for c in read.view.visible}

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
        days = self._window(days, 7, top, "days")
        deadline_days = self._window(deadline_days, top, top, "deadline_days")
        upcoming_days = self._window(upcoming_days, 7, 14, "upcoming_days")
        now = self._clock()
        tz = ZoneInfo(self._config.timezone)
        read = await self._read_server(identity, tool, now - timedelta(days=max(days, deadline_days)))
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
        payload = fit_server_overview(
            entries, deadlines[:cap], base, self._limits.max_result_chars, self._limits.brief_topics_per_channel
        )
        return ToolResult(payload, [cm.channel["id"] for cm in read.fetched])

    async def get_open_loops(self, identity: Identity, days: int | None = None) -> ToolResult:
        """Your promises not yet followed up, and what others owe you, across every readable channel."""
        tool = "get_open_loops"
        top = self._limits.overview_max_days
        days = self._window(days, top, top, "days")
        now = self._clock()
        read = await self._read_server(identity, tool, now - timedelta(days=days))
        found = open_loops(self._groups(read), identity, read.ctx, now)
        base = {"days": days, "skipped_channels": read.skipped, "truncated": read.capped}
        return ToolResult(fit_loops(found, base, self._limits.max_result_chars), [cm.channel["id"] for cm in read.fetched])
