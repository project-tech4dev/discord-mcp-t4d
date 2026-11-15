from datetime import timedelta

import pydantic_core

from discord_mcp.config import Limits
from discord_mcp.matching import ChannelMessages, Identity, RenderContext
from discord_mcp.signals import Words
from discord_mcp.topics import build_topics, fit_overview, fit_search, overview_row, render_topic, search_results
from tests.fakes import GUILD_ID, ME, NOW, OTHER, ROLE_DEV, THIRD, channel, msg, user

IDENT = Identity(user_id=ME, role_ids=frozenset({ROLE_DEV}))
MAIN = channel("10", "grants", parent_id="1")
NONE = Words.build([])


def ctx(**limits) -> RenderContext:
    return RenderContext(guild_id=GUILD_ID, role_names={}, channel_names={}, limits=Limits(**limits))


def main(*messages):
    return ChannelMessages(MAIN, "grants", list(messages))


def thread(tid, name, *messages):
    return ChannelMessages(channel(tid, name, 11, parent_id="10"), f"grants › {name}", list(messages))


def say(mid, h, author=OTHER, text=None, ch="10", **kw):
    return msg(mid, ch, author, text or f"text {mid}", hours_ago=h, **kw)


def ids(topic):
    return [m["id"] for m in topic.messages]


def row(topic, **limits):
    return overview_row(topic, IDENT, ctx(**limits), NONE, NOW)


# --- building topics ------------------------------------------------------------


def test_thread_is_one_topic_with_starter():
    starter = say("40", 900, ch="50")
    topics = build_topics(None, [thread("50", "acme", say("51", 5, ch="50"), say("52", 4, ch="50"))], {"50": starter}, ctx())
    assert len(topics) == 1
    assert ids(topics[0]) == ["40", "51", "52"]
    assert topics[0].id == "t:50"
    assert topics[0].kind == "thread"


def test_thread_started_from_main_message_excludes_it_from_main():
    m = main(say("100", 10, thread={"id": "100"}), say("101", 9))
    topics = build_topics(m, [thread("100", "topic", say("102", 8, ch="100"))], {}, ctx())
    by_kind = {t.kind: ids(t) for t in topics}
    assert by_kind == {"thread": ["100", "102"], "main": ["101"]}


def test_main_channel_splits_at_24h_gap():
    topics = build_topics(main(say("1", 100), say("2", 90), say("3", 60), say("4", 55)), [], {}, ctx())
    assert [ids(t) for t in topics] == [["1", "2"], ["3", "4"]]
    assert [t.id for t in topics] == ["m:10/1", "m:10/3"]


def test_main_topic_cut_at_72h():
    msgs = [say(str(i + 1), 100 - 10 * i) for i in range(11)]
    topics = build_topics(main(*msgs), [], {}, ctx())
    for t in topics:
        first, last = t.messages[0]["timestamp"], t.messages[-1]["timestamp"]
        from datetime import datetime

        assert (datetime.fromisoformat(last) - datetime.fromisoformat(first)).total_seconds() <= 72 * 3600
    assert len(topics) == 2


# --- overview rows --------------------------------------------------------------


def test_overview_row_fields():
    t = build_topics(None, [thread("50", "acme", say("51", 50, text="x" * 900, ch="50"), say("52", 49, ch="50"),
                                   say("53", 48, ch="50"), say("54", 48, ch="50"))], {}, ctx())[0]
    r, _ = row(t)
    assert set(r) == {"where", "kind", "id", "started", "last_activity", "quiet_days", "message_count",
                      "people", "your_part", "opening", "key_messages", "latest"}
    assert len(r["opening"]["text"]) == 300
    assert [x["link"][-2:] for x in r["latest"]] == ["53", "54"]
    assert r["quiet_days"] == 2
    assert r["where"] == "grants › acme"
    assert r["message_count"] == 4


def test_your_part_values():
    def part(*messages):
        return row(build_topics(main(*messages), [], {}, ctx())[0])[0]["your_part"]

    ping = say("1", 5, text=f"<@{ME}> look", mentions=[user(ME)])
    assert part(ping) == "pinged"
    assert part(say("1", 5), say("2", 4, ME)) == "posted"
    assert part(ping, say("2", 4, ME)) == "pinged_and_posted"
    assert part(say("1", 5), say("2", 4, THIRD)) == "none"


def test_key_messages_are_strong_only_and_not_repeated():
    t = build_topics(main(
        say("1", 20, text="kickoff"),
        say("2", 19, text="due 20 Nov"),
        say("3", 18, text="see you Monday"),
        say("4", 17, text="ok"),
        say("5", 16, text="draft https://docs.google.com/d/x"),
    ), [], {}, ctx())[0]
    r, strong = row(t)
    assert strong is True
    assert [k["link"][-1] for k in r["key_messages"]] == ["2"]
    assert "date" in r["key_messages"][0]["matched"]
    assert "doc_link" in r["latest"][-1]["matched"]
    assert "matched" not in r["latest"][0]


def test_look_for_words_make_rows_strong():
    t = build_topics(main(say("1", 20, text="kickoff"), say("2", 19, text="LOI went out"), say("3", 18), say("4", 17)), [], {}, ctx())[0]
    r, strong = overview_row(t, IDENT, ctx(), Words.build(["LOI"]), NOW)
    assert strong is True
    assert r["key_messages"][0]["matched"] == ["word:LOI"]


# --- budget -----------------------------------------------------------------------


def busy(n, strong_every=None):
    rows = []
    for i in range(n):
        text = "y" * 280 + (" due 20 Nov" if strong_every and i % strong_every == 0 else "")
        body = [say(f"{i}1", 600 - i, ch=f"5{i}", text="o" * 400), say(f"{i}2", 599 - i, ch=f"5{i}", text="z" * 300),
                say(f"{i}3", 598 - i, ch=f"5{i}", text=text), say(f"{i}4", 597 - i, ch=f"5{i}", text="w" * 300),
                say(f"{i}5", 596 - i, ch=f"5{i}", text="v" * 300)]
        t = build_topics(None, [thread(f"5{i}", f"topic {i}", *body)], {}, ctx())[0]
        rows.append(row(t))
    return rows


def test_overview_orders_signal_rows_first_then_newest():
    out = fit_overview(busy(6, strong_every=3), {"channel": "grants"}, 10**6)
    whos = [r["where"] for r in out["topics"]]
    assert whos[:2] == ["grants › topic 3", "grants › topic 0"]
    assert whos[2:] == ["grants › topic 5", "grants › topic 4", "grants › topic 2", "grants › topic 1"]


def test_busy_channel_keeps_all_signal_rows_within_budget():
    out = fit_overview(busy(40, strong_every=4), {"channel": "grants"}, 40000)
    kept = out["topics"]
    assert len(pydantic_core.to_json(out, indent=2).decode()) <= 40000
    assert sum(1 for r in kept if r["key_messages"]) == 10
    assert out["omitted"] == 40 - len(kept)
    assert out["truncated"] is (out["omitted"] > 0)
    assert out["counts"] == {"thread": 40, "main": 0}


def typical_rows(n):
    """Typical lengths (an opening of ~250 characters, replies of ~120) and real-length 19-digit IDs."""
    rows = []
    for i in range(n):
        tid = str(100000000000000000 + i * 1000)
        body = [msg(str(int(tid) + k), tid, OTHER, ("o" * 250) if k == 0 else ("r" * 120), hours_ago=600 - i * 10 - k)
                for k in range(6)]
        if i % 5 == 0:
            body[2]["content"] = "please submit by 20 Nov " + "s" * 100
        t = build_topics(None, [thread(tid, f"grant {i}", *body)], {}, ctx())[0]
        rows.append(overview_row(t, IDENT, ctx(), NONE, NOW))
    return rows


def test_twenty_five_typical_threads_fit_budget():
    out = fit_overview(typical_rows(25), {"channel": "grants", "days": 30}, 40000)
    assert out["omitted"] == 0
    assert out["truncated"] is False
    assert len(pydantic_core.to_json(out, indent=2).decode()) <= 40000


def test_thirty_typical_threads_keep_every_signal_row():
    out = fit_overview(typical_rows(30), {"channel": "grants", "days": 30}, 40000)
    assert sum(1 for r in out["topics"] if r["key_messages"]) == 6
    assert out["omitted"] <= 6


def test_fetch_cap_in_base_keeps_truncated():
    out = fit_overview(busy(1), {"truncated": True}, 10**6)
    assert out["truncated"] is True


# --- search -------------------------------------------------------------------------


def test_search_groups_by_topic_and_matches_title():
    topics = build_topics(
        main(say("1", 30, text="the Acme call went well")),
        [thread("60", "Acme proposal", say("61", 5, ch="60", text="whitepaper notes"))],
        {},
        ctx(),
    )
    results, count = search_results(topics, Words.build(["acme"]), ctx())
    assert count == 1
    by_kind = {r["kind"]: r for r in results}
    assert by_kind["thread"]["title_match"] is True
    assert by_kind["thread"]["matches"][0]["text"] == "whitepaper notes"
    assert by_kind["main"]["title_match"] is False
    assert by_kind["main"]["matches"][0]["text"] == "the Acme call went well"


def test_search_caps_matches_per_topic():
    topics = build_topics(main(*[say(str(i + 1), 20 - i, text=f"budget v{i}") for i in range(8)]), [], {}, ctx())
    results, count = search_results(topics, Words.build(["budget"]), ctx())
    assert count == 8
    assert [m["text"] for m in results[0]["matches"]] == ["budget v7", "budget v6", "budget v5", "budget v4", "budget v3"]


def test_fit_search_orders_newest_match_first_and_drops_oldest():
    topics = build_topics(main(say("1", 200, text="budget a"), say("2", 100, text="budget b"), say("3", 2, text="budget c")), [], {}, ctx())
    results, count = search_results(topics, Words.build(["budget"]), ctx())
    out = fit_search(results, {"match_count": count}, 10**6)
    assert [r["matches"][0]["text"] for r in out["results"]] == ["budget c", "budget b", "budget a"]
    small = fit_search(results, {"match_count": count}, 700)
    assert small["truncated"] is True
    assert small["results"][0]["matches"][0]["text"] == "budget c"


# --- full topic -----------------------------------------------------------------


def test_render_topic_capped_keeps_first_and_newest():
    msgs = [say(str(i + 1), 60 - i * 0.5) for i in range(60)]
    t = build_topics(main(*msgs), [], {}, ctx())[0]
    out = render_topic(t, ctx(conversation_max_messages=50))
    got = [m["link"].rsplit("/", 1)[1] for m in out["messages"]]
    assert got[0] == "1" and len(got) == 50 and got[-1] == "60"
    assert out["truncated"] is True
    assert out["id"] == "m:10/1"


def test_render_topic_stays_within_budget():
    msgs = [say(str(i + 1), 60 - i * 0.5, text="z" * 1500) for i in range(50)]
    t = build_topics(main(*msgs), [], {}, ctx())[0]
    out = render_topic(t, ctx(max_result_chars=40000))
    assert len(pydantic_core.to_json(out, indent=2).decode()) <= 40000
    got = [m["link"].rsplit("/", 1)[1] for m in out["messages"]]
    assert got[0] == "1" and got[-1] == "50"
    assert out["truncated"] is True


# --- your_part counts direct pings only -------------------------------------------


def test_your_part_ignores_everyone_ping():
    t = build_topics(main(say("100", 5, text="@everyone standup", mention_everyone=True)), [], {}, ctx())[0]
    assert row(t)[0]["your_part"] == "none"


def test_your_part_ignores_role_ping_in_direct_only_channel():
    quiet = RenderContext(guild_id=GUILD_ID, role_names={}, channel_names={}, direct_only=frozenset({"10"}))
    t = build_topics(main(say("100", 5, text="<@&R> out", mention_roles=[ROLE_DEV])), [], {}, quiet)[0]
    assert overview_row(t, IDENT, quiet, NONE, NOW, quiet_channel=True)[0]["your_part"] == "none"


def test_your_part_counts_role_ping_elsewhere():
    t = build_topics(main(say("100", 5, text="<@&R> review", mention_roles=[ROLE_DEV])), [], {}, ctx())[0]
    assert row(t)[0]["your_part"] == "pinged"


def test_search_ignores_matches_before_the_window():
    old = say("5", 900, text="acme kickoff", thread={"id": "5"})
    t = thread("5", "old", say("501", 4, ch="5", text="acme update"))
    topics = build_topics(None, [t], {"5": old}, ctx())
    results, count = search_results(topics, Words.build(["acme"]), ctx(), since=NOW - timedelta(days=30))
    assert count == 1
    assert [m["text"] for m in results[0]["matches"]] == ["acme update"]


def test_fitting_measures_a_long_list_only_a_few_times(monkeypatch):
    import discord_mcp.topics as topics

    rows = busy(64)
    calls = []
    real = topics._size
    monkeypatch.setattr(topics, "_size", lambda p: calls.append(1) or real(p))
    out = fit_overview(rows, {"channel": "grants"}, 20000)
    assert 0 < out["omitted"] < 64
    assert len(calls) <= 10  # binary search, not one measurement per dropped row
    assert real(out) <= 20000
    # The fewest rows dropped: keeping one more would not fit.
    one_more = sorted(rows, key=lambda r: r[0]["last_activity"], reverse=True)
    one_more.sort(key=lambda r: not r[1])
    kept = len(out["topics"])
    bigger = out | {"topics": [r for r, _ in one_more[: kept + 1]], "omitted": out["omitted"] - 1}
    assert real(bigger) > 20000


def test_search_fitting_measures_a_long_list_only_a_few_times(monkeypatch):
    import discord_mcp.topics as topics

    msgs = [say(str(i + 1), 500 - i, text=f"budget {'x' * 300} {i}") for i in range(64)]
    topics_ = build_topics(main(*msgs), [], {}, ctx(main_topic_gap_hours=0))
    results, count = search_results(topics_, Words.build(["budget"]), ctx())
    calls = []
    real = topics._size
    monkeypatch.setattr(topics, "_size", lambda p: calls.append(1) or real(p))
    out = fit_search(results, {"match_count": count}, 5000)
    assert out["truncated"] is True and 0 < len(out["results"]) < len(results)
    assert len(calls) <= 10
    assert real(out) <= 5000
    newest = sorted(results, key=lambda r: r["matches"][0]["time"], reverse=True)
    assert real(out | {"results": newest[: len(out["results"]) + 1]}) > 5000  # the fewest dropped
