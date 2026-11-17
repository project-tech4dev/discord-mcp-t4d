"""The two access gates: which channels and threads one person may read in this request.

A channel is returned only when configuration permits it (the allowlist, or
every channel when allow_all_channels is on), it is not on the channel deny
list (itself, its category, or for threads their parent), the person is not
denied it, and it is readable by the person under Discord's permission rules
(permissions.py). In allowlist mode, any listed channel that cannot be verified
or read is dropped and its ID is logged. In allow-all mode, channels a person
cannot see are skipped quietly so their number is never revealed, and a
failed server lookup fails the call instead of returning an empty result.
No message is fetched here, and nothing is kept after a request finishes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime

from discord_mcp import audit
from discord_mcp.config import Config
from discord_mcp.discord_client import DiscordError
from discord_mcp.errors import NotAMember, RoleNotAllowed, busy_or_denied, not_available
from discord_mcp.matching import Identity
from discord_mcp.messages import PRIVATE_THREAD, READABLE_TYPES, THREAD_TYPES, home_id, snowflake_time
from discord_mcp.permissions import GuildSnapshot, can_read


@dataclass
class View:
    """What one request knows about the server. Lives only for that request."""

    guild: GuildSnapshot
    channels: dict[str, dict]  # every guild channel by ID (structure only; never shown as-is)
    role_names: dict[str, str]
    readable_names: dict[str, str]  # names of channels this person can read, for <#id> mentions
    visible: list[dict]  # permitted channels the person can read
    skipped: int


@dataclass
class Located:
    """Where a link's channel id lives, found without asking Discord about that id when possible."""

    meta: dict  # the channel or thread itself (structure only)
    home: str  # the channel itself, or a thread's parent
    guild_channels: list[dict]  # the server's channel list, reused by view()
    active_threads: list[dict] | None = None  # the server's active threads, if home_of read them


@dataclass
class Threads:
    """Readable threads under a view's visible channels."""

    threads: list[dict]
    failed: dict[str, str]  # parent channel id -> failure code; such a parent is never half-read
    skipped: list[str]  # failure codes of threads that were dropped and logged


class Access:
    def __init__(self, client, *, guild_id: str, config: Config, concurrency: int = 8) -> None:
        self._client = client
        self._guild_id = guild_id
        self._config = config
        self._concurrency = concurrency
        self._bot_id: str | None = None  # the bot's own user ID; static, so kept between requests

    # --- identity -----------------------------------------------------------

    async def resolve_identity(self, user_id: str) -> Identity:
        """Current roles for the person, read by the bot. Raises NotAMember if they left."""
        try:
            member = await self._client.get_guild_member(self._guild_id, user_id)
        except DiscordError as e:
            if e.status == 404:
                raise NotAMember("not a member of the server") from None
            raise
        roles = frozenset(member.get("roles", []))
        allowed = self._config.allowed_roles
        if allowed and not roles & allowed:
            raise RoleNotAllowed("no allowed role")
        return Identity(user_id=user_id, role_ids=roles)

    async def _bot_identity(self) -> Identity | None:
        """The bot's own roles, to skip channels the bot cannot read before asking Discord.

        Each refused request counts toward Discord's invalid-request limit. If the lookup
        fails, None: channels are then tried as before, and a refusal is skipped as usual.
        """
        try:
            if self._bot_id is None:
                self._bot_id = str((await self._client.get_current_user())["id"])
            member = await self._client.get_guild_member(self._guild_id, self._bot_id)
        except Exception:
            return None
        return Identity(user_id=self._bot_id, role_ids=frozenset(member.get("roles", [])))

    # --- checks -------------------------------------------------------------

    def drop(self, identity: Identity, tool: str, channel_id: str, code: str) -> None:
        """Log a channel or thread that was left out, by ID and code only."""
        audit.record(
            user_id=identity.user_id,
            tool=tool,
            channel_ids=[channel_id],
            item_count=0,
            duration_ms=0,
            status=f"dropped:{code}",
        )

    @staticmethod
    def _readable(
        guild: GuildSnapshot,
        identity: Identity,
        channel: dict,
        *,
        thread: dict | None = None,
        is_thread_member: bool | None = None,
    ) -> bool:
        try:
            return can_read(
                guild,
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

    def may_read(self, channel_id: str | None) -> bool:
        """The configuration allows this channel id at all (deny list first, then allow-all or allowlist)."""
        if channel_id in self._config.deny_channels:
            return False
        return self._config.allow_all_channels or channel_id in self._config.allowlist

    # --- channels -----------------------------------------------------------

    async def view(
        self, identity: Identity, tool: str, wanted: list[str] | None, *, guild_channels: list[dict] | None = None
    ) -> View:
        """`guild_channels`: the server's channel list when this request already holds it."""
        allow_all = self._config.allow_all_channels
        ids = [c for c in self._config.allowlist if wanted is None or c in wanted]

        async def channel_list() -> list[dict]:
            if guild_channels is not None:
                return guild_channels
            return await self._client.get_guild_channels(self._guild_id)

        try:
            guild_info, roles, guild_channels, bot = await asyncio.gather(
                self._client.get_guild(self._guild_id),
                self._client.get_guild_roles(self._guild_id),
                channel_list(),
                self._bot_identity(),
            )
            snapshot = GuildSnapshot(
                guild_id=self._guild_id,
                owner_id=str(guild_info["owner_id"]),
                role_permissions={r["id"]: r["permissions"] for r in roles},
            )
            channels = {c["id"]: c for c in guild_channels}
            role_names = {r["id"]: r["name"] for r in roles if r["id"] != self._guild_id}
        except Exception as e:
            # Without the server's roles and channels no channel can be checked, and an empty
            # result would read as "nothing needs you". Fail the call instead, in both modes (#6).
            raise e if isinstance(e, DiscordError) else DiscordError(None) from None

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
            elif bot is not None and not self._readable(snapshot, bot, ch):
                code = "bot_no_access"  # the person may read it; the bot may not, so don't ask
            if code:
                self.drop(identity, tool, cid, code)
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
        return View(snapshot, channels, role_names, readable_names, visible, skipped)

    async def one_channel(self, identity: Identity, tool: str, channel_id: str) -> View:
        """A view of exactly one permitted channel the person can read; AccessDenied before any lookup
        if configuration forbids it, and after the lookup if the person cannot read it."""
        if not self.may_read(channel_id):
            raise not_available()
        view = await self.view(identity, tool, [channel_id])
        if not view.visible:
            raise not_available()
        return view

    async def home_of(self, channel_id: str) -> Located:
        """The permitted channel a link's channel lives in (itself, or a thread's parent).

        Any id from a link is untrusted, and Discord counts every refused request toward
        the bot's invalid-request limit (issue #4). So the id is first looked for in the
        server's channel list and then among its active threads, both requests the bot may
        make. Only an id in neither (an archived thread, or junk) is looked up by itself.
        AccessDenied if configuration forbids its home.

        A failed server-wide lookup (channel list, active threads) fails the call with
        "Discord could not be reached", as in `view`; only the lookup of the id itself can
        mean "not available".
        """
        active = None
        guild_channels = await self._client.get_guild_channels(self._guild_id)
        meta = next((c for c in guild_channels if c.get("id") == channel_id), None)
        if meta is None:
            active = await self._client.get_active_threads(self._guild_id)
            meta = next((t for t in active if t.get("id") == channel_id), None)
        if meta is None:
            try:
                meta = await self._client.get_channel(channel_id)
            except DiscordError as e:
                raise busy_or_denied([e.code]) from None
        meta = {**meta, "id": channel_id}
        home = home_id(meta)
        if not home or not self.may_read(home):
            raise not_available()
        return Located(meta, home, guild_channels, active)

    # --- threads ------------------------------------------------------------

    async def threads(
        self, identity: Identity, tool: str, view: View, cutoff: datetime, *, active: list[dict] | None = None
    ) -> Threads:
        """Readable threads under visible parents, active since `cutoff`.

        `active`: the server's active threads when this request already holds them.

        A parent whose archived-thread lookup fails is dropped completely, so a
        channel the bot can no longer read is logged once and never half-read.
        The server's active-thread list covers every channel, so if it fails
        the whole call fails, like a failed role lookup in `view`.
        """
        parents = {p["id"]: p for p in view.visible}
        failed: dict[str, str] = {}
        found: dict[str, dict] = {}
        # A DiscordError from the server-wide list propagates: dropping every parent would answer
        # "nothing needs you" when Discord could not be reached.
        for t in active if active is not None else await self._client.get_active_threads(self._guild_id):
            if t.get("parent_id") in parents:
                found[t["id"]] = t
        sem = asyncio.Semaphore(self._concurrency)

        async def archived(pid: str) -> tuple[str, list[dict] | str]:
            async with sem:
                try:
                    return pid, await self._client.get_public_archived_threads(pid, cutoff)
                except DiscordError as e:
                    return pid, e.code

        for pid, got in await asyncio.gather(*(archived(p) for p in parents)):
            if isinstance(got, str):
                failed[pid] = got
            else:
                for t in got:
                    found.setdefault(t["id"], t)
        for pid, code in failed.items():
            self.drop(identity, tool, pid, code)

        threads: list[dict] = []
        skipped: list[str] = []
        for t in found.values():
            if t["id"] in self._config.deny_channels:
                continue  # a denied thread is never read and never mentioned
            pid = t.get("parent_id")
            if pid in failed:
                continue
            last = t.get("last_message_id")
            if last and snowflake_time(last) < cutoff:
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
                self.drop(identity, tool, t["id"], code)
                skipped.append(code)
            else:
                threads.append(t)
        return Threads(threads, failed, skipped)

    async def one_thread(
        self, identity: Identity, tool: str, thread_id: str, cutoff: datetime, located: Located | None = None
    ) -> tuple[View, dict, dict]:
        """(view, parent, thread) for one readable thread, checked without fetching any messages.

        `located`: what home_of already found for this id, so it is not looked up twice.
        """
        loc = located or await self.home_of(thread_id)
        if loc.meta.get("type") not in THREAD_TYPES or thread_id in self._config.deny_channels:
            raise not_available()
        parent_id = loc.home
        view = await self.view(identity, tool, [parent_id], guild_channels=loc.guild_channels)
        if not view.visible:
            raise not_available()
        found = await self.threads(identity, tool, view, cutoff, active=loc.active_threads)
        thread = next((t for t in found.threads if t["id"] == thread_id), None)
        if thread is None:
            raise busy_or_denied(found.failed.values())
        return view, view.visible[0], thread
