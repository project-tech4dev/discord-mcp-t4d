"""Groups pings into conversations so Claude gets one row per conversation, not raw messages.

A thread is one conversation. In a plain channel, a conversation starts at a
ping and "follows the people": later messages join when they reply to a
message already in it, or come from a participant (the pinger, the person,
and anyone the ping names), until a quiet gap or a maximum length.
Pure functions over already-fetched messages; nothing is kept.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pydantic_core

from discord_mcp.matching import (
    CONTENT_TYPES,
    ChannelMessages,
    Identity,
    RenderContext,
    _display_name,
    _replace_mentions,
    ping_reason,
)

THREAD_TYPES = frozenset({10, 11, 12})
# Ping reasons not asked of the person directly: @everyone anywhere, and @here or a role
# ping ("group") in a direct_only channel.
BROADCASTS = frozenset({"everyone", "group"})
STATUS_ORDER = ("needs_you", "reopened", "handled", "announcement")

Ping = tuple[dict, str, "str | None"]


@dataclass
class Conversation:
    channel_id: str
    label: str
    messages: list[dict]  # oldest first
    pings: list[Ping]  # (message, reason, role name), oldest first

    @property
    def id(self) -> str:
        return f"{self.channel_id}/{self.messages[0]['id']}"


@dataclass
class _Open:
    conv: Conversation
    participants: set[str]
    start: datetime
    last: datetime
    message_ids: set[str] = field(default_factory=set)


def _time(m: dict) -> datetime:
    return datetime.fromisoformat(m["timestamp"])


def _author(m: dict) -> str | None:
    return (m.get("author") or {}).get("id")


def _content(cm: ChannelMessages) -> list[dict]:
    return [m for m in cm.messages if m.get("type") in CONTENT_TYPES]


def _hit(m: dict, cm: ChannelMessages, identity: Identity, ctx: RenderContext) -> tuple[str, str | None] | None:
    """ping_reason, with @here and role pings downgraded to "group" in direct_only channels."""
    hit = ping_reason(m, identity, ctx.role_names)
    home = cm.channel.get("parent_id") if cm.channel.get("type") in THREAD_TYPES else cm.channel["id"]
    quiet = home in ctx.direct_only or cm.channel["id"] in ctx.direct_only
    if hit is not None and quiet and hit[0] in ("role", "here"):
        return ("group", hit[1])
    return hit


def _pings(cm: ChannelMessages, messages: list[dict], identity: Identity, ctx: RenderContext) -> list[Ping]:
    out: list[Ping] = []
    for m in messages:
        hit = _hit(m, cm, identity, ctx)
        if hit is not None:
            out.append((m, hit[0], hit[1]))
    return out


def _assign(m: dict, open_: list[_Open], identity: Identity, ctx: RenderContext) -> _Open | None:
    ref = (m.get("referenced_message") or {}).get("id")
    if ref:
        # A Discord reply belongs with what it replies to, even after that conversation went quiet.
        for o in open_:
            if ref in o.message_ids:
                return o
        if _author(m) == identity.user_id:
            return None  # my reply to something else must never close an unrelated conversation
    now = _time(m)
    gap = timedelta(hours=ctx.limits.conversation_gap_hours)
    longest = timedelta(hours=ctx.limits.conversation_max_hours)
    live = [o for o in open_ if now - o.last <= gap and now - o.start <= longest]
    author = _author(m)
    candidates = [o for o in live if author in o.participants]
    if not candidates:
        return None
    return max(candidates, key=lambda o: int(o.conv.pings[-1][0]["id"]))


def _people(m: dict) -> set[str]:
    """The author and everyone the message names directly."""
    people = {_author(m)} | {u.get("id") for u in m.get("mentions", [])}
    people.discard(None)
    return people


def _add(o: _Open, m: dict, hit: tuple[str, str | None] | None) -> None:
    o.conv.messages.append(m)
    o.message_ids.add(m["id"])
    o.last = _time(m)
    if hit is not None:
        o.conv.pings.append((m, hit[0], hit[1]))


def group_conversations(
    channels: list[ChannelMessages], identity: Identity, ctx: RenderContext
) -> list[Conversation]:
    threads = {cm.channel["id"]: cm for cm in channels if cm.channel.get("type") in THREAD_TYPES}
    plains = [cm for cm in channels if cm.channel.get("type") not in THREAD_TYPES]
    started: set[str] = set()  # threads started from a ping in their parent channel
    out: list[Conversation] = []

    for cm in plains:
        open_: list[_Open] = []
        for m in _content(cm):
            hit = _hit(m, cm, identity, ctx)
            tid = (m.get("thread") or {}).get("id")
            if hit is not None and tid in threads:
                started.add(tid)
                body = _content(threads[tid])
                out.append(
                    Conversation(
                        channel_id=cm.channel["id"],
                        label=threads[tid].label,
                        messages=[m, *body],
                        pings=[(m, hit[0], hit[1]), *_pings(threads[tid], body, identity, ctx)],
                    )
                )
                continue
            target = _assign(m, open_, identity, ctx)
            if target is not None:
                _add(target, m, hit)
                if hit is not None and hit[0] not in BROADCASTS:
                    target.participants |= _people(m) | {identity.user_id}
            elif hit is not None:
                people = _people(m)
                if hit[0] not in BROADCASTS:
                    people.add(identity.user_id)
                o = _Open(
                    conv=Conversation(channel_id=cm.channel["id"], label=cm.label, messages=[], pings=[]),
                    participants=people,
                    start=_time(m),
                    last=_time(m),
                )
                _add(o, m, hit)
                open_.append(o)
                out.append(o.conv)

    for tid, cm in threads.items():
        if tid in started:
            continue
        body = _content(cm)
        pings = _pings(cm, body, identity, ctx)
        if pings:
            out.append(Conversation(channel_id=tid, label=cm.label, messages=body, pings=pings))

    out.sort(key=lambda c: int(c.messages[0]["id"]))
    return out


# --- status and rows ------------------------------------------------------------


def _id(m: dict) -> int:
    return int(m["id"])


def _mine_after_first_ping(conv: Conversation, identity: Identity) -> list[dict]:
    direct = [p for p in conv.pings if p[1] not in BROADCASTS]
    first = _id((direct or conv.pings)[0][0])
    return [m for m in conv.messages if _author(m) == identity.user_id and _id(m) > first]


def status_of(conv: Conversation, identity: Identity) -> str:
    direct = [p for p in conv.pings if p[1] not in BROADCASTS]
    if not direct:
        return "announcement"
    mine = _mine_after_first_ping(conv, identity)
    if not mine:
        return "needs_you"
    last_mine = _id(mine[-1])
    return "reopened" if any(_id(p[0]) > last_mine for p in direct) else "handled"


def brief(message: dict, ctx: RenderContext, chars: int) -> dict:
    author = message.get("author", {})
    out = {
        "author": _display_name(author),
        "time": message["timestamp"][:16],
        "text": _replace_mentions(message.get("content", ""), message, ctx)[:chars],
        "link": f"https://discord.com/channels/{ctx.guild_id}/{message['channel_id']}/{message['id']}",
    }
    if author.get("bot"):
        out["is_bot"] = True
    return out


def _ping_item(p: Ping, ctx: RenderContext, chars: int) -> dict:
    item = brief(p[0], ctx, chars) | {"reason": p[1]}
    if p[2] is not None:
        item["role"] = p[2]
    return item


def render_row(conv: Conversation, identity: Identity, ctx: RenderContext) -> dict:
    lim = ctx.limits
    status = status_of(conv, identity)
    people: list[str] = []
    for m in conv.messages:
        if _author(m) != identity.user_id:
            name = _display_name(m.get("author", {}))
            if name not in people:
                people.append(name)
    row: dict = {
        "status": status,
        "where": conv.label,
        "people": people,
        "started": conv.messages[0]["timestamp"][:16],
        "last_activity": conv.messages[-1]["timestamp"][:16],
        "message_count": len(conv.messages),
    }
    mine = _mine_after_first_ping(conv, identity)
    reply = brief(mine[-1], ctx, lim.summary_chars) if mine else None
    if status in ("needs_you", "reopened"):
        n = lim.row_max_pings
        chosen = conv.pings if len(conv.pings) <= n else [conv.pings[0], *conv.pings[len(conv.pings) - n + 1 :]]
        others = [m for m in conv.messages if _author(m) != identity.user_id]
        newest = others[-1] if others else None
        row |= {
            "ping_count": len(conv.pings),
            "pings": [_ping_item(p, ctx, lim.text_chars) for p in chosen],
            "your_last_reply": reply,
            "latest": brief(newest, ctx, lim.summary_chars)
            if newest is not None and _id(newest) > _id(conv.pings[-1][0])
            else None,
        }
    elif status == "handled":
        row |= {
            "ping_count": len(conv.pings),
            "pings": [_ping_item(conv.pings[-1], ctx, lim.summary_chars)],
            "your_last_reply": reply,
        }
    else:
        row["pings"] = [_ping_item(conv.pings[-1], ctx, lim.summary_chars)]
    row["id"] = conv.id
    return row


# --- size budget ----------------------------------------------------------------


def _size(payload: dict) -> int:
    """Characters as MCPServer sends it (indented JSON, non-ASCII kept raw)."""
    return len(pydantic_core.to_json(payload, indent=2).decode())


def fit_budget(rows: list[dict], base: dict, max_chars: int) -> dict:
    """Order rows most urgent first, then drop from the end until the JSON fits."""
    rank = {s: i for i, s in enumerate(STATUS_ORDER)}
    ordered = sorted(rows, key=lambda r: r["last_activity"], reverse=True)
    ordered.sort(key=lambda r: rank[r["status"]])
    counts = {s: 0 for s in STATUS_ORDER}
    for r in ordered:
        counts[r["status"]] += 1
    omitted = {s: 0 for s in STATUS_ORDER}

    def payload(kept: list[dict]) -> dict:
        return base | {
            "conversations": kept,
            "counts": counts,
            "omitted": dict(omitted),
            "truncated": any(omitted.values()),
        }

    kept = list(ordered)
    while kept and _size(payload(kept)) > max_chars:
        omitted[kept.pop()["status"]] += 1
    return payload(kept)


# --- one conversation in full ---------------------------------------------------


def render_full(conv: Conversation, ctx: RenderContext) -> dict:
    """Every message, oldest first. When capped, keep the first message and the newest ones."""
    n = ctx.limits.conversation_max_messages
    msgs = conv.messages
    head = [msgs[0]] if msgs[0] is conv.pings[0][0] else [msgs[0], conv.pings[0][0]]
    if len(msgs) <= n:
        kept = msgs
    else:
        kept = head + [m for m in msgs[len(msgs) - max(n - len(head), 0) :] if m not in head]
    out = []
    for m in kept:
        item = brief(m, ctx, ctx.limits.text_chars)
        ref = m.get("referenced_message")
        if ref:
            item["reply_to"] = _replace_mentions(ref.get("content", ""), ref, ctx)[: ctx.limits.reply_chars]
        out.append(item)
    payload = {"where": conv.label, "id": conv.id, "messages": out, "truncated": len(kept) < len(msgs)}
    keep = {out[kept.index(m)]["link"] for m in head}
    return trim_to_budget(payload, keep=keep, max_chars=ctx.limits.max_result_chars)


def trim_to_budget(payload: dict, *, keep: set[str], max_chars: int) -> dict:
    """Drop the oldest messages (never those whose link is in `keep`, never the newest) until the JSON fits."""
    msgs = payload["messages"]
    while _size(payload) > max_chars:
        drop = next((i for i, m in enumerate(msgs[:-1]) if m["link"] not in keep), None)
        if drop is None:
            break
        del msgs[drop]
        payload["truncated"] = True
    return payload
