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

from discord_mcp.budget import size as _size
from discord_mcp.budget import trim_messages
from discord_mcp.matching import ChannelMessages, Identity, RenderContext, channel_ping, is_direct, is_quiet
from discord_mcp.messages import THREAD_TYPES, content_messages, is_bot
from discord_mcp.messages import author_id as _author
from discord_mcp.messages import msg_id as _id
from discord_mcp.messages import time_of as _time
from discord_mcp.render import brief, display_name, render_thread
from discord_mcp.wording import ping_asks, reply_promises

STATUS_ORDER = ("needs_you", "reopened", "check", "fyi", "handled", "announcement")
# Rows the size budget drops first, from the end of STATUS_ORDER.
DROPPABLE = frozenset({"fyi", "handled", "announcement"})

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


def _hit(m: dict, cm: ChannelMessages, identity: Identity, ctx: RenderContext) -> tuple[str, str | None] | None:
    """ping_reason, with @here and role pings downgraded to "group" in direct_only channels."""
    return channel_ping(m, identity, ctx, quiet=is_quiet(cm.channel, ctx))


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
        for m in content_messages(cm.messages):
            hit = _hit(m, cm, identity, ctx)
            tid = (m.get("thread") or {}).get("id")
            if hit is not None and tid in threads:
                started.add(tid)
                body = content_messages(threads[tid].messages)
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
                if hit is not None and is_direct(hit[0]):
                    target.participants |= _people(m) | {identity.user_id}
            elif hit is not None:
                people = _people(m)
                if is_direct(hit[0]):
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
        body = content_messages(cm.messages)
        pings = _pings(cm, body, identity, ctx)
        if pings:
            out.append(Conversation(channel_id=tid, label=cm.label, messages=body, pings=pings))

    out.sort(key=lambda c: int(c.messages[0]["id"]))
    return out


# --- status and rows ------------------------------------------------------------


def _mine_after_first_ping(conv: Conversation, identity: Identity) -> list[dict]:
    direct = [p for p in conv.pings if is_direct(p[1])]
    first = _id((direct or conv.pings)[0][0])
    return [m for m in conv.messages if _author(m) == identity.user_id and _id(m) > first]


def _asked(conv: Conversation, direct: list[Ping], after: int, before: int | None) -> bool:
    """Whether a direct ping after `after`, or a later message by its author before `before`,
    asks something ("Hi @you" then the ask in the next message)."""
    pings = [p for p in direct if _id(p[0]) > after]
    if not pings:
        return False
    authors = {_author(p[0]) for p in pings}
    start = _id(pings[0][0])
    for m in conv.messages:
        i = _id(m)
        if i >= start and (before is None or i < before) and _author(m) in authors:
            if ping_asks(m.get("content") or ""):
                return True
    return False


def status_of(conv: Conversation, identity: Identity) -> str:
    direct = [p for p in conv.pings if is_direct(p[1])]
    if not direct:
        return "announcement"
    mine = _mine_after_first_ping(conv, identity)
    if not mine:
        return "needs_you" if _asked(conv, direct, 0, None) else "fyi"
    last_mine = _id(mine[-1])
    if _asked(conv, direct, last_mine, None):
        return "reopened"
    return "check" if check_reasons(conv, identity) else "handled"


def _others_after(conv: Conversation, identity: Identity, after: dict) -> list[dict]:
    """Messages from other people (not bots) after `after`."""
    return [
        m
        for m in conv.messages
        if _id(m) > _id(after) and _author(m) != identity.user_id and not is_bot(m)
    ]


def check_reasons(conv: Conversation, identity: Identity) -> list[str]:
    """Why a conversation the person replied to may still be open."""
    mine = _mine_after_first_ping(conv, identity)
    if not mine:
        return []
    reasons = []
    if _others_after(conv, identity, mine[-1]):
        reasons.append("others_after_you")
    if reply_promises(mine[-1].get("content") or ""):
        reasons.append("you_promised")
    return reasons


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
            name = display_name(m.get("author", {}))
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
    elif status in ("check", "handled"):
        row |= {
            "ping_count": len(conv.pings),
            "pings": [_ping_item(conv.pings[-1], ctx, lim.summary_chars)],
            "your_last_reply": reply,
        }
        if status == "check":
            after = _others_after(conv, identity, mine[-1])
            row["check"] = check_reasons(conv, identity)
            row["latest"] = brief(after[-1], ctx, lim.summary_chars) if after else None
    else:
        # fyi shows the direct ping that named the person, not a later @everyone.
        shown = [p for p in conv.pings if is_direct(p[1])] if status == "fyi" else conv.pings
        row["pings"] = [_ping_item(shown[-1], ctx, lim.summary_chars)]
    row["id"] = conv.id
    return row


# --- size budget ----------------------------------------------------------------


def _compact(row: dict, chars: int) -> dict:
    """The same row with every message text cut to `chars` (marked text_cut)."""

    def cut(item: dict | None) -> dict | None:
        if not item or len(item.get("text", "")) <= chars:
            return item
        return item | {"text": item["text"][:chars], "text_cut": True}

    out = dict(row)
    if "pings" in out:
        pings = out["pings"]
        out["pings"] = [cut(p) for p in (pings if len(pings) <= 2 else [pings[0], pings[-1]])]
    for key in ("latest", "your_last_reply"):
        if key in out:
            out[key] = cut(out[key])
    return out


def _minimal(row: dict, chars: int) -> dict:
    """The row cut to its bones: the newest ping, shortened, with its link. Open the row for the rest."""
    out = {k: v for k, v in _compact(row, chars).items() if k not in ("latest", "your_last_reply", "started")}
    if out.get("pings"):
        out["pings"] = [{k: v for k, v in out["pings"][-1].items() if k != "time"}]
    if len(out.get("people", [])) > 3:
        out["people_count"] = len(out["people"])
        out["people"] = out["people"][:3]
    return out


def fit_budget(rows: list[dict], base: dict, max_chars: int, compact_chars: int | None = None) -> dict:
    """Order rows most urgent first; if too big, drop handled/announcement rows, then shorten the least
    urgent rows only as far as needed, then drop from the end until it fits."""
    rank = {s: i for i, s in enumerate(STATUS_ORDER)}
    ordered = sorted(rows, key=lambda r: r["last_activity"], reverse=True)
    ordered.sort(key=lambda r: rank[r["status"]])
    counts = {s: 0 for s in STATUS_ORDER}
    for r in ordered:
        counts[r["status"]] += 1
    omitted = {s: 0 for s in STATUS_ORDER}

    kept = list(ordered)
    levels: list[int] = [0] * len(kept)  # 0 whole, 1 texts cut, 2 cut to the bones

    def payload() -> dict:
        out = base | {
            "conversations": kept,
            "counts": counts,
            "omitted": dict(omitted),
            "truncated": any(omitted.values()),
        }
        if any(levels):
            out["compact"] = True
            out["compact_level"] = max(levels)
        return out

    def shrink(i: int, lvl: int) -> None:
        row = _compact(kept[i], compact_chars) if lvl == 1 else _minimal(kept[i], compact_chars // 2)
        kept[i] = row | {"compact": lvl}  # per row, so Claude knows which rows to open for detail
        levels[i] = lvl

    def drop() -> None:
        omitted[kept.pop()["status"]] += 1
        levels.pop()

    # First drop announcement, handled and fyi rows (least urgent last in order), keeping urgent rows whole.
    while kept and kept[-1]["status"] in DROPPABLE and _size(payload()) > max_chars:
        drop()
    if compact_chars is not None and _size(payload()) > max_chars:
        # Then shorten, least urgent rows first and only as far as needed: level 1 cuts texts and keeps
        # the first and last ping; level 2 keeps only the newest ping, shorter still. A size estimate
        # keeps this linear; the exact checks below correct it, shortening further before dropping.
        size = _size(payload())
        for lvl in (1, 2):
            i = len(kept) - 1
            while size > max_chars and i >= 0:
                before = _size(kept[i])
                shrink(i, lvl)
                size += _size(kept[i]) - before
                i -= 1
            if size <= max_chars:
                break
        while _size(payload()) > max_chars:
            nxt = next((i for i in reversed(range(len(kept))) if levels[i] < 2), None)
            if nxt is None:
                break
            shrink(nxt, levels[nxt] + 1)
    while kept and _size(payload()) > max_chars:
        drop()
    return payload()


# --- one conversation in full ---------------------------------------------------


def render_full(conv: Conversation, ctx: RenderContext) -> dict:
    """Every message, oldest first. When capped, keep the first message and the newest ones."""
    n = ctx.limits.conversation_max_messages
    msgs = conv.messages
    head = [msgs[0]] if msgs[0] is conv.pings[0][0] else [msgs[0], conv.pings[0][0]]
    if len(msgs) <= n:
        kept = msgs
    else:
        # The head, then the newest messages until `n` are kept, rendered oldest first (issue #8).
        chosen = {id(m) for m in head}
        for m in reversed(msgs):
            if len(chosen) >= n:
                break
            chosen.add(id(m))
        kept = [m for m in msgs if id(m) in chosen]
    out = render_thread(kept, ctx)
    payload = {"where": conv.label, "id": conv.id, "messages": out, "truncated": len(kept) < len(msgs)}
    keep = {out[kept.index(m)]["link"] for m in head}
    return trim_messages(payload, keep=keep, max_chars=ctx.limits.max_result_chars)
