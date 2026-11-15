"""Size budgets: every tool result is measured the way it is sent, and trimmed to fit."""

from __future__ import annotations

from collections.abc import Callable

import pydantic_core


def size(payload: dict) -> int:
    """Characters as MCPServer sends it (indented JSON, non-ASCII kept raw)."""
    return len(pydantic_core.to_json(payload, indent=2).decode())


def fewest_to_drop(count: int, fits_without: Callable[[int], bool]) -> int:
    """The smallest n in 0..count with fits_without(n) true, or count if none (binary search).

    fits_without(n) must be monotonic: once dropping n items fits, dropping more fits too.
    """
    if fits_without(0):
        return 0
    lo, hi = 0, count
    while lo < hi:
        mid = (lo + hi) // 2
        if fits_without(mid):
            hi = mid
        else:
            lo = mid + 1
    return lo


def trim_messages(payload: dict, *, keep: set[str], max_chars: int) -> dict:
    """Drop the oldest messages (never those whose link is in `keep`, never the newest) until the JSON fits."""
    msgs = payload["messages"]
    while size(payload) > max_chars:
        drop = next((i for i, m in enumerate(msgs[:-1]) if m["link"] not in keep), None)
        if drop is None:
            break
        del msgs[drop]
        payload["truncated"] = True
    return payload
