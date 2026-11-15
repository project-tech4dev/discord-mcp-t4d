"""Server-wide views for the brief: last week's discussion, deadlines, and open loops.

Pure functions over messages already fetched for one request (see
Reader.read_server). Nothing is kept after a request.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from discord_mcp.budget import fewest_to_drop
from discord_mcp.budget import size as _size
from discord_mcp.deadlines import find_deadlines
from discord_mcp.matching import ChannelMessages, Identity, RenderContext
from discord_mcp.messages import THREAD_TYPES
from discord_mcp.messages import author_id as _author
from discord_mcp.messages import is_bot as _is_bot
from discord_mcp.messages import time_of as _time
from discord_mcp.render import brief, display_name
from discord_mcp.signals import Words, signals
from discord_mcp.topics import Topic, build_topics
from discord_mcp.wording import asks as _asks
from discord_mcp.wording import is_promise


@dataclass
class ChannelGroup:
    """One channel with its threads, as read for this request."""

    channel: dict
    name: str
    topics: list[Topic]  # threads and main-channel stretches over the whole read window
    extra: Words  # the channel's watch words


def group_by_channel(fetched: list[ChannelMessages], names: dict[str, str], ctx: RenderContext, watch) -> list[ChannelGroup]:
    """Main channel and its threads together, keyed by the channel the threads live in."""
    mains: dict[str, ChannelMessages] = {}
    threads: dict[str, list[ChannelMessages]] = {}
    channels: dict[str, dict] = {}
    for cm in fetched:
        if cm.channel.get("type") in THREAD_TYPES:
            home = cm.channel.get("parent_id")
            threads.setdefault(home, []).append(cm)
        else:
            home = cm.channel["id"]
            mains[home] = cm
            channels[home] = cm.channel
    out = []
    for home in [*mains, *[h for h in threads if h not in mains]]:
        topics = build_topics(mains.get(home), threads.get(home, []), {}, ctx)
        channel = channels.get(home, {"id": home})
        out.append(ChannelGroup(channel, names.get(home, home), topics, watch(home)))
    return out


# --- last week's discussion ------------------------------------------------------


def channel_entry(group: ChannelGroup, since: datetime, ctx: RenderContext) -> dict | None:
    """One entry for a channel with activity since `since`, its topics ranked (all of them; fitting trims)."""
    ranked = []
    people: set[str] = set()
    total = 0
    last = ""
    for t in group.topics:
        recent = [m for m in t.messages if _time(m) >= since]
        if not recent:
            continue
        labels: set[str] = set()
        strong = False
        for m in recent:
            found, s = signals(m.get("content", ""), group.extra)
            labels.update(found)
            strong = strong or s
            if not _is_bot(m) and _author(m):
                people.add(_author(m))
        total += len(recent)
        when = recent[-1]["timestamp"][:16]
        last = max(last, when)
        row = {
            "id": t.id,
            "where": t.label,
            "message_count": len(recent),
            "last_activity": when,
            "opening": brief(t.messages[0], ctx, ctx.limits.summary_chars),
        }
        if labels:
            row["matched"] = sorted(labels)
        ranked.append((strong, len(recent), when, row))
    if not ranked:
        return None
    ranked.sort(key=lambda r: (r[0], r[1], r[2]), reverse=True)
    return {
        "channel": group.name,
        "message_count": total,
        "topic_count": len(ranked),
        "people": len(people),
        "last_activity": last,
        "topics": [r[3] for r in ranked],
    }


# --- deadlines ----------------------------------------------------------------------


def deadline_items(
    groups: list[ChannelGroup],
    since: datetime,
    today: date,
    last_day: date,
    tz: ZoneInfo,
    date_order: str,
    ctx: RenderContext,
) -> list[dict]:
    """Deadlines due from `today` to `last_day`, from messages since `since`, soonest first.

    Broadcast (direct_only) channels hold leave notices and social posts, so they are not scanned.
    """
    found = []
    for g in groups:
        if g.channel["id"] in ctx.direct_only:
            continue
        for t in g.topics:
            for m in t.messages:
                posted = _time(m)
                if posted < since:
                    continue
                for due, phrase in find_deadlines(m.get("content") or "", posted, tz, date_order):
                    if today <= due <= last_day:
                        item = {
                            "due": due.isoformat(),
                            "phrase": phrase,
                            "where": t.label,
                            "id": t.id,
                            "message": brief(m, ctx, ctx.limits.summary_chars),
                        }
                        found.append((due, posted, item))
    found.sort(key=lambda f: (f[0], f[1]))
    return [f[2] for f in found]


def fit_server_overview(
    channels: list[dict], deadlines: list[dict], base: dict, max_chars: int, topics_per_channel: int
) -> dict:
    """Deadlines are kept first; then topics per channel shrink to 1; then the quietest channels go."""
    newest = sorted(channels, key=lambda e: e["last_activity"], reverse=True)
    quiet_first = sorted(range(len(newest)), key=lambda i: (newest[i]["message_count"], newest[i]["last_activity"]))
    already = int(base.get("omitted_deadlines", 0))

    def payload(per: int, drop_channels: int, drop_deadlines: int) -> dict:
        gone = set(quiet_first[:drop_channels])
        kept = [e for i, e in enumerate(newest) if i not in gone]
        shown = [e | {"topics": e["topics"][:per]} for e in kept]
        # Showing only the top topics is by design (topic_count says how many); fewer than that is a cut.
        cut = drop_channels > 0 or drop_deadlines > 0
        cut = cut or (per < topics_per_channel and any(len(e["topics"]) > per for e in kept))
        return base | {
            "deadlines": deadlines[: len(deadlines) - drop_deadlines],
            "channels": shown,
            "omitted_channels": drop_channels,
            "omitted_deadlines": already + drop_deadlines,
            "truncated": bool(base.get("truncated")) or already > 0 or cut,
        }

    def fits(p: dict) -> bool:
        return _size(p) <= max_chars

    for per in range(max(topics_per_channel, 1), 0, -1):
        if fits(payload(per, 0, 0)):
            return payload(per, 0, 0)
    n = fewest_to_drop(len(quiet_first), lambda k: fits(payload(1, k, 0)))
    if fits(payload(1, n, 0)):
        return payload(1, n, 0)
    d = fewest_to_drop(len(deadlines), lambda k: fits(payload(1, len(newest), k)))
    return payload(1, len(newest), d)


# --- open loops ---------------------------------------------------------------------

LOOP_ORDER = ("your_promise", "unanswered_ask", "promise_to_you", "stalled")
CUT_ORDER = ("stalled", "promise_to_you", "unanswered_ask", "your_promise")


def _named(m: dict, me: str) -> dict[str, str]:
    """People a message names directly: user mentions and the author it replies to (never bots or me)."""
    out: dict[str, str] = {}
    for u in m.get("mentions") or []:
        if u.get("id") and u["id"] != me and not u.get("bot"):
            out[u["id"]] = display_name(u)
    ref = (m.get("referenced_message") or {}).get("author") or {}
    if ref.get("id") and ref["id"] != me and not ref.get("bot"):
        out[ref["id"]] = display_name(ref)
    return out


def _names_me(m: dict, me: str) -> bool:
    if any(u.get("id") == me for u in m.get("mentions") or []):
        return True
    return ((m.get("referenced_message") or {}).get("author") or {}).get("id") == me


def _replies_to(m: dict) -> str | None:
    return (m.get("message_reference") or {}).get("message_id") or (m.get("referenced_message") or {}).get("id")


def _days_since(now: datetime, when: datetime) -> int:
    return max(int((now - when).total_seconds() // 86400), 0)


def open_loops(groups: list[ChannelGroup], identity: Identity, ctx: RenderContext, now: datetime) -> list[dict]:
    """Loops the person is part of: their own promises, and what others owe them."""
    me = identity.user_id
    lim = ctx.limits
    items: list[tuple[str, datetime, dict]] = []

    def item(kind: str, t: Topic, m: dict, who: list[str]) -> None:
        when = _time(m)
        row = {
            "kind": kind,
            "where": t.label,
            "id": t.id,
            "who": who,
            "since": m["timestamp"][:16],
            "quiet_days": _days_since(now, when if kind != "stalled" else _time(t.messages[-1])),
            "message": brief(m, ctx, lim.summary_chars),
        }
        if t.messages[-1]["id"] != m["id"]:
            row["latest"] = brief(t.messages[-1], ctx, lim.summary_chars)
        items.append((kind, when, row))

    for g in groups:
        # One pass per channel group, so each check below is a dictionary lookup.
        latest_post: dict[str, datetime] = {}
        my_replies: set[str] = set()
        for t in g.topics:
            for x in t.messages:
                uid = _author(x)
                if uid:
                    when = _time(x)
                    if uid not in latest_post or when > latest_post[uid]:
                        latest_post[uid] = when
                if uid == me and _replies_to(x):
                    my_replies.add(_replies_to(x))

        def posted_after(uid: str, when: datetime) -> bool:
            return uid in latest_post and latest_post[uid] > when

        listed: set[str] = set()
        for t in g.topics:
            my_last = max((_time(x) for x in t.messages if _author(x) == me), default=None)
            # kind -> (latest qualifying message, people owing, in first-seen order)
            found: dict[str, tuple[dict, list[str]]] = {}

            def note(kind: str, m: dict, who: list[str]) -> None:
                people = found[kind][1] if kind in found else []
                found[kind] = (m, people + [w for w in who if w not in people])

            for m in t.messages:
                age = _days_since(now, _time(m))
                text = m.get("content") or ""
                if _author(m) == me:
                    named = _named(m, me)
                    asked = bool(named) and _asks(text) and age >= lim.nudge_ask_days
                    if asked:
                        owing = [name for uid, name in named.items() if not posted_after(uid, _time(m))]
                        if owing:
                            note("unanswered_ask", m, owing)
                    if not asked and is_promise(text) and age >= lim.nudge_ask_days:
                        followed = (my_last is not None and my_last > _time(m)) or m["id"] in my_replies
                        if not followed:
                            note("your_promise", m, [])
                elif not _is_bot(m) and _names_me(m, me) and is_promise(text) and age >= lim.nudge_promise_days:
                    if not posted_after(_author(m), _time(m)):
                        note("promise_to_you", m, [display_name(m.get("author", {}))])
            for kind, (m, who) in found.items():
                item(kind, t, m, who)
                listed.add(t.id)

        for t in g.topics:
            if t.kind != "thread" or t.id in listed or not any(_author(m) == me for m in t.messages):
                continue
            last = t.messages[-1]
            if _days_since(now, _time(last)) < lim.nudge_stall_days:
                continue
            text = last.get("content") or ""
            if _asks(text) or signals(text, g.extra)[1]:
                others: list[str] = []
                for m in t.messages:
                    name = display_name(m.get("author", {}))
                    if _author(m) != me and not _is_bot(m) and name not in others:
                        others.append(name)
                item("stalled", t, last, others)

    rank = {k: i for i, k in enumerate(LOOP_ORDER)}
    items.sort(key=lambda i: (rank[i[0]], i[1]))
    return [i[2] for i in items]


def fit_loops(loops: list[dict], base: dict, max_chars: int) -> dict:
    """Drop stalled first, then promises to you, then unanswered asks, then your promises.

    Within a kind the oldest go first, so the newest loops stay.
    """
    counts = {k: 0 for k in LOOP_ORDER}
    for x in loops:
        counts[x["kind"]] += 1
    # Removal order: by CUT_ORDER kind, oldest first within a kind (loops are sorted oldest first).
    order = [i for k in CUT_ORDER for i, x in enumerate(loops) if x["kind"] == k]

    def payload(n: int) -> dict:
        gone = set(order[:n])
        omitted = {k: 0 for k in LOOP_ORDER}
        for i in gone:
            omitted[loops[i]["kind"]] += 1
        return base | {
            "loops": [x for i, x in enumerate(loops) if i not in gone],
            "counts": counts,
            "omitted": omitted,
            "truncated": bool(base.get("truncated")) or n > 0,
        }

    return payload(fewest_to_drop(len(order), lambda n: _size(payload(n)) <= max_chars))
