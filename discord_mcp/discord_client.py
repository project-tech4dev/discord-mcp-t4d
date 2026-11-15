"""Read-only client for Discord's REST API, using the bot token.

There is deliberately one HTTP method in this module: GET. Nothing here can
post, edit, react or delete. Errors carry only the HTTP status, never the
response body or the original exception, so message content cannot leak
through a traceback.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import httpx

API_BASE = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (discord-mcp, 0.1.0)"
PAGE_SIZE = 100
MAX_PAUSE = 60.0  # longest wait taken from any one Discord answer, in seconds
MAX_WAIT = 30.0  # a request facing a longer wait fails at once ("try again shortly") instead of hanging


class DiscordError(Exception):
    """A failed Discord call. Holds the HTTP status only (None for network errors)."""

    def __init__(self, status: int | None) -> None:
        self.status = status
        super().__init__(f"Discord request failed (status {status})")

    @property
    def code(self) -> str:
        return f"http_{self.status}" if self.status is not None else "network"


class DiscordClient:
    def __init__(
        self,
        bot_token: str,
        *,
        max_retries: int = 5,
        timeout: float = 20.0,
        max_in_flight: int = 8,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], float] = time.monotonic,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=API_BASE,
            headers={"Authorization": f"Bot {bot_token}", "User-Agent": USER_AGENT},
            timeout=timeout,
            transport=transport,
        )
        self._max_retries = max_retries
        self._sleep = sleep
        self._now = now
        # Pacing is shared by every request on this client, so tool calls running in parallel
        # back off together instead of each retrying on its own. It holds paths and times only.
        self._in_flight = asyncio.Semaphore(max_in_flight)
        self._paused_until = 0.0  # every request waits until then (after any 429)
        self._path_free_at: dict[str, float] = {}  # a path whose rate-limit bucket ran out

    def _delay(self, bucket: str) -> float:
        return max(self._paused_until, self._path_free_at.get(bucket, 0.0)) - self._now()

    async def _take_slot(self, bucket: str) -> None:
        """Wait out any pause, then hold one of the in-flight slots.

        The pause is checked again after the slot is taken: a 429 that arrived while this
        request was queued must hold it back too. A wait longer than MAX_WAIT fails at once.
        """
        while True:
            while (delay := self._delay(bucket)) > 0:
                if delay > MAX_WAIT:
                    raise DiscordError(429)
                await self._sleep(delay)
            await self._in_flight.acquire()
            if self._delay(bucket) <= 0:
                return
            self._in_flight.release()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        bucket = _bucket(path)
        for _attempt in range(self._max_retries + 1):
            await self._take_slot(bucket)
            try:
                resp = await self._http.get(path, params=params)
            except httpx.HTTPError:
                raise DiscordError(None) from None
            finally:
                self._in_flight.release()

            if resp.status_code == 429:
                self._paused_until = max(self._paused_until, self._now() + _retry_after(resp))
                continue
            if resp.status_code >= 400:
                raise DiscordError(resp.status_code)

            if resp.headers.get("X-RateLimit-Remaining") == "0":
                now = self._now()
                self._path_free_at = {b: t for b, t in self._path_free_at.items() if t > now}
                self._path_free_at[bucket] = now + _float(resp.headers.get("X-RateLimit-Reset-After"), 1.0)
            try:
                return resp.json()
            except ValueError:
                raise DiscordError(resp.status_code) from None
        raise DiscordError(429)

    # --- server structure -------------------------------------------------

    async def get_current_user(self) -> dict:
        """The bot's own user (its ID is used to check which channels the bot itself can read)."""
        return await self._get("/users/@me")

    async def get_guild(self, guild_id: str) -> dict:
        return await self._get(f"/guilds/{guild_id}")

    async def is_thread_member(self, thread_id: str, user_id: str) -> bool:
        """True if the user is in the thread; False on 404; DiscordError on anything else."""
        try:
            await self._get(f"/channels/{thread_id}/thread-members/{user_id}")
        except DiscordError as e:
            if e.status == 404:
                return False
            raise
        return True

    async def get_guild_channels(self, guild_id: str) -> list[dict]:
        return await self._get(f"/guilds/{guild_id}/channels")

    async def get_guild_roles(self, guild_id: str) -> list[dict]:
        return await self._get(f"/guilds/{guild_id}/roles")

    async def get_guild_member(self, guild_id: str, user_id: str) -> dict:
        return await self._get(f"/guilds/{guild_id}/members/{user_id}")

    async def get_channel(self, channel_id: str) -> dict:
        return await self._get(f"/channels/{channel_id}")

    async def get_active_threads(self, guild_id: str) -> list[dict]:
        data = await self._get(f"/guilds/{guild_id}/threads/active")
        return data.get("threads", [])

    async def get_public_archived_threads(self, channel_id: str, since: datetime) -> list[dict]:
        """Public threads archived at or after `since`, newest archive first."""
        out: list[dict] = []
        before: str | None = None
        while True:
            params: dict[str, Any] = {"limit": PAGE_SIZE}
            if before:
                params["before"] = before
            data = await self._get(f"/channels/{channel_id}/threads/archived/public", params)
            threads = data.get("threads", [])
            for t in threads:
                archived_at = datetime.fromisoformat(t["thread_metadata"]["archive_timestamp"])
                if archived_at < since:
                    return out
                out.append(t)
            if not data.get("has_more") or not threads:
                return out
            before = threads[-1]["thread_metadata"]["archive_timestamp"]

    # --- messages ---------------------------------------------------------

    async def get_message(self, channel_id: str, message_id: str) -> dict:
        return await self._get(f"/channels/{channel_id}/messages/{message_id}")

    async def get_oldest_message(self, channel_id: str) -> dict | None:
        page = await self._get(f"/channels/{channel_id}/messages", {"after": "0", "limit": 1})
        return page[0] if page else None

    async def get_messages_since(self, channel_id: str, cutoff: datetime, max_count: int | None = None) -> list[dict]:
        """Messages at or after `cutoff`, newest first, paging backward 100 at a time.

        Stops after `max_count` messages, so one request cannot page a busy channel without end.
        """
        out: list[dict] = []
        before: str | None = None
        while True:
            params: dict[str, Any] = {"limit": PAGE_SIZE}
            if before:
                params["before"] = before
            page = await self._get(f"/channels/{channel_id}/messages", params)
            for m in page:
                if datetime.fromisoformat(m["timestamp"]) < cutoff:
                    return out
                out.append(m)
                if max_count is not None and len(out) >= max_count:
                    return out
            if len(page) < PAGE_SIZE:
                return out
            before = page[-1]["id"]


def _bucket(path: str) -> str:
    """The rate-limit bucket for a path: Discord limits per route and channel (or server),
    so a trailing message or user ID is folded away (/channels/1/messages/2 -> /channels/1/messages/:id)."""
    parts = path.split("/")
    return "/".join(p if i < 3 or not p.isdigit() else ":id" for i, p in enumerate(parts))


def _float(value: object, default: float) -> float:
    """A wait in seconds from Discord: finite, at least 0 and at most MAX_PAUSE, else `default`."""
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return min(max(f, 0.0), MAX_PAUSE) if math.isfinite(f) else default


def _retry_after(resp: httpx.Response) -> float:
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and "retry_after" in body:
        return _float(body["retry_after"], 1.0)
    return _float(resp.headers.get("Retry-After"), 1.0)
