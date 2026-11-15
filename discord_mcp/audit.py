"""The only log output this server produces.

Each line is JSON with exactly: user_id, tool, channel_ids, item_count,
duration_ms, status. Every value is checked against a strict pattern, so
message text cannot slip in even by mistake. All other loggers (the MCP SDK,
httpx, asyncio, uvicorn) are silenced, because they can print request
details or exceptions.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Callable, Iterable
from typing import TextIO

AUDIT_LOGGER = "discord_mcp.audit"
TOOLS = frozenset(
    {
        "list_my_channels",
        "get_my_pings",
        "get_channel_messages",
        "get_conversation",
        "get_channel_overview",
        "search",
        "get_server_overview",
        "get_open_loops",
        "sign_in",
        "startup",
    }
)
_STATUS = re.compile(r"^(ok|error:[A-Za-z_][A-Za-z0-9_]{0,63}|dropped:[a-z0-9_]{1,32})$")

_log = logging.getLogger(AUDIT_LOGGER)


def _id(value: object) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) and value.isdigit() and len(value) <= 20 else "invalid"


def _count(value: object) -> int:
    return value if isinstance(value, int) and value >= 0 else -1


def record(
    *,
    user_id: object,
    tool: str,
    channel_ids: Iterable[object],
    item_count: int,
    duration_ms: int,
    status: str,
) -> None:
    line = {
        "user_id": _id(user_id),  # null when nobody is signed in yet
        "tool": tool if tool in TOOLS else "invalid",
        "channel_ids": [_id(c) for c in channel_ids],
        "item_count": _count(item_count),
        "duration_ms": _count(duration_ms),
        "status": status if isinstance(status, str) and _STATUS.match(status) else "invalid",
    }
    _log.info(json.dumps(line, separators=(",", ":")))


class _AuditOnly(logging.Filter):
    def filter(self, rec: logging.LogRecord) -> bool:
        return rec.name == AUDIT_LOGGER


class _MessageOnly(logging.Formatter):
    """Prints the pre-built JSON line and never a traceback or stack."""

    def format(self, rec: logging.LogRecord) -> str:
        return rec.getMessage()


def configure_logging(stream: TextIO | None = None) -> Callable[[], None]:
    """Route audit lines to `stream` (stderr by default) and drop everything else.

    Returns a function that restores the previous logging setup (used by tests).
    """
    root = logging.getLogger()
    saved = (list(root.handlers), root.level, logging.lastResort)

    handler = logging.StreamHandler(stream or sys.stderr)
    handler.addFilter(_AuditOnly())
    handler.setFormatter(_MessageOnly())
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    logging.lastResort = None

    def restore() -> None:
        root.handlers, level, logging.lastResort = saved[0], saved[1], saved[2]
        root.setLevel(level)

    return restore


def safe_excepthook(exc_type: type[BaseException], exc: BaseException, tb: object) -> None:
    """Uncaught errors print only their type, never their message or traceback."""
    sys.stderr.write(f"fatal: {exc_type.__name__}\n")
