"""Topics for the channel overview and search: each thread, plus main-channel stretches split by quiet gaps.

Pure functions over already-fetched messages. Rows are kept small on purpose:
Claude reads the overview, then opens the few topics it needs with
get_conversation. Nothing is kept after a request.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from discord_mcp.budget import size as _size
from discord_mcp.budget import trim_messages
from discord_mcp.matching import ChannelMessages, Identity, RenderContext, ping_reason
from discord_mcp.messages import author_id as _author
from discord_mcp.messages import content_messages, thread_title
from discord_mcp.messages import time_of as _time
from discord_mcp.render import brief, display_name, render_thread
from discord_mcp.signals import Words, signals


@dataclass
class Topic:
    kind: str  # "thread" | "main"
    channel_id: str  # the thread's id, or the main channel's id
    label: str
    messages: list[dict]  # oldest first; a thread's true first message included

    @property
    def id(self) -> str:
        if self.kind == "thread":
            return f"t:{self.channel_id}"
        return f"m:{self.channel_id}/{self.messages[0]['id']}"


def build_topics(
    main: ChannelMessages | None,
    threads: list[ChannelMessages],
    starters: dict[str, dict],
    ctx: RenderContext,
) -> list[Topic]:
    """One topic per thread, then main-channel messages split at quiet gaps and a maximum length."""
    thread_ids = {cm.channel["id"] for cm in threads}
    main_msgs = content_messages(main.messages) if main is not None else []
    started_here = {m["id"]: m for m in main_msgs if (m.get("thread") or {}).get("id") in thread_ids}

    topics: list[Topic] = []
    for cm in threads:
        tid = cm.channel["id"]
        body = content_messages(cm.messages)
        starter = started_here.get(tid) or starters.get(tid)
        if starter is not None and all(m["id"] != starter["id"] for m in body):
            body = [starter, *body]
        if body:
            topics.append(Topic("thread", tid, cm.label, body))

    if main is not None:
        gap = timedelta(hours=ctx.limits.main_topic_gap_hours)
        longest = timedelta(hours=ctx.limits.main_topic_max_hours)
        current: list[dict] = []
        for m in main_msgs:
            if m["id"] in started_here:
                continue
            if current and (_time(m) - _time(current[-1]) >= gap or _time(m) - _time(current[0]) > longest):
                topics.append(Topic("main", main.channel["id"], main.label, current))
                current = []
            current.append(m)
        if current:
            topics.append(Topic("main", main.channel["id"], main.label, current))
    return topics


# --- overview rows ----------------------------------------------------------------


def _item(m: dict, ctx: RenderContext, chars: int, extra: Words) -> tuple[dict, bool]:
    item = brief(m, ctx, chars)
    labels, strong = signals(m.get("content", ""), extra)
    if labels:
        item["matched"] = labels
    return item, strong


def overview_row(
    t: Topic, identity: Identity, ctx: RenderContext, extra: Words, now: datetime, quiet_channel: bool = False
) -> tuple[dict, bool]:
    """A compact row for one topic, and whether it carries a strong signal."""
    lim = ctx.limits
    msgs = t.messages
    rest = msgs[1:]
    latest_msgs = rest[len(rest) - lim.overview_latest :] if lim.overview_latest else []
    middle = rest[: len(rest) - len(latest_msgs)]

    opening, strong = _item(msgs[0], ctx, lim.opening_chars, extra)
    latest = []
    for m in latest_msgs:
        item, s = _item(m, ctx, lim.summary_chars, extra)
        latest.append(item)
        strong = strong or s
    key = []
    for m in reversed(middle):
        if len(key) >= lim.overview_key_messages:
            break
        item, s = _item(m, ctx, lim.summary_chars, extra)
        if s:
            key.append(item)
            strong = True

    people: list[str] = []
    pinged = posted = False
    for m in msgs:
        if _author(m) == identity.user_id:
            posted = True
            continue
        name = display_name(m.get("author", {}))
        if name not in people:
            people.append(name)
        hit = ping_reason(m, identity, ctx.role_names)
        # Like get_my_pings: @everyone, and @here or role pings in a direct_only channel, don't count.
        direct = hit is not None and hit[0] != "everyone" and not (quiet_channel and hit[0] in ("role", "here"))
        pinged = pinged or direct
    part = {(True, True): "pinged_and_posted", (True, False): "pinged", (False, True): "posted"}.get(
        (pinged, posted), "none"
    )

    row = {
        "where": t.label,
        "kind": t.kind,
        "id": t.id,
        "started": msgs[0]["timestamp"][:16],
        "last_activity": msgs[-1]["timestamp"][:16],
        "quiet_days": max(int((now - _time(msgs[-1])).total_seconds() // 86400), 0),
        "message_count": len(msgs),
        "people": people,
        "your_part": part,
        "opening": opening,
        "key_messages": key,
        "latest": latest,
    }
    return row, strong


def fit_overview(rows: list[tuple[dict, bool]], base: dict, max_chars: int) -> dict:
    """Rows with a strong signal first, then newest activity; drop from the end until it fits."""
    ordered = sorted(rows, key=lambda r: r[0]["last_activity"], reverse=True)
    ordered.sort(key=lambda r: not r[1])
    counts = {"thread": 0, "main": 0}
    for r, _ in ordered:
        counts[r["kind"]] += 1
    kept = [r for r, _ in ordered]
    capped = bool(base.get("truncated"))

    def payload(omitted: int) -> dict:
        return base | {
            "topics": kept,
            "counts": counts,
            "omitted": omitted,
            "truncated": capped or omitted > 0,
        }

    omitted = 0
    while kept and _size(payload(omitted)) > max_chars:
        kept.pop()
        omitted += 1
    return payload(omitted)


# --- search ------------------------------------------------------------------------


def _title(t: Topic) -> str:
    return thread_title(t.label) if t.kind == "thread" else ""


def search_results(
    topics: list[Topic], words: Words, ctx: RenderContext, since: datetime | None = None
) -> tuple[list[dict], int]:
    """Topics with matching messages in the window (or a matching thread name), and the number of matches.

    A thread's true first message can be older than the window; it is kept for context but not searched.
    """
    lim = ctx.limits
    results: list[dict] = []
    total = 0
    for t in topics:
        hits = [
            m
            for m in t.messages
            if (since is None or _time(m) >= since) and words.find(m.get("content", ""))
        ]
        total += len(hits)
        title_match = bool(_title(t)) and bool(words.find(_title(t)))
        if not hits and not title_match:
            continue
        shown = list(reversed(hits))[: lim.search_matches_per_topic] if hits else [t.messages[-1]]
        results.append(
            {
                "where": t.label,
                "kind": t.kind,
                "id": t.id,
                "last_activity": t.messages[-1]["timestamp"][:16],
                "title_match": title_match,
                "matches": [brief(m, ctx, lim.summary_chars) for m in shown],
            }
        )
    return results, total


def fit_search(results: list[dict], base: dict, max_chars: int) -> dict:
    kept = sorted(results, key=lambda r: r["matches"][0]["time"], reverse=True)

    def payload(dropped: bool) -> dict:
        return base | {"results": kept, "truncated": bool(base.get("truncated")) or dropped}

    dropped = False
    while kept and _size(payload(dropped)) > max_chars:
        kept.pop()
        dropped = True
    return payload(dropped)


# --- one topic in full ----------------------------------------------------------------


def render_topic(t: Topic, ctx: RenderContext) -> dict:
    """Every message, oldest first. When capped, the first message and then the newest ones."""
    n = ctx.limits.conversation_max_messages
    msgs = t.messages
    kept = msgs if len(msgs) <= n else [msgs[0], *msgs[len(msgs) - n + 1 :]]
    out = render_thread(kept, ctx)
    payload = {"where": t.label, "id": t.id, "messages": out, "truncated": len(kept) < len(msgs)}
    return trim_messages(payload, keep={out[0]["link"]}, max_chars=ctx.limits.max_result_chars)
