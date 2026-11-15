"""Server overview and open loops. Synthetic data only."""

import pytest

from discord_mcp.brief import fit_server_overview
from discord_mcp.config import ChannelEntry, Config, Limits
from discord_mcp.digest import Digest, InputError
from discord_mcp.matching import Identity
from tests.fakes import GUILD_ID, ME, NOW, OTHER, ROLE_DEV, THIRD, FakeClient, channel, msg, user

IDENT = Identity(user_id=ME, role_ids=frozenset({ROLE_DEV}))
DAY = 24


def cfg(*ids, pings=None, allow_all=False, deny_channels=(), timezone="UTC", **limits):
    allow = {i: ChannelEntry(i, None, 1, pings=(pings or {}).get(i, "all")) for i in ids}
    return Config(
        allowlist=allow,
        deny=frozenset(),
        limits=Limits(**limits),
        allow_all_channels=allow_all,
        deny_channels=frozenset(deny_channels),
        timezone=timezone,
    )


def make(client, config):
    return Digest(client, guild_id=GUILD_ID, config=config, clock=lambda: NOW)


def server(**channels) -> FakeClient:
    """channels: id -> list of messages; names are 'c<id>'."""
    c = FakeClient()
    c.add_channel(channel("1", "Team", 4), [])
    for cid, messages in channels.items():
        c.add_channel(channel(cid.lstrip("c"), f"chan{cid.lstrip('c')}", parent_id="1"), messages)
    return c


def thread(c: FakeClient, tid: str, parent: str, messages, ctype=11):
    t = channel(tid, f"thread{tid}", ctype, parent_id=parent, thread_metadata={"archived": False})
    c.add_channel(t, messages)
    c.active_threads = [*c.active_threads, t]
    return t


async def overview(c, config, **kw):
    return (await make(c, config).get_server_overview(IDENT, **kw)).payload


# --- get_server_overview: last week's discussion ------------------------------------


async def test_discussion_counts_only_messages_inside_days():
    c = server(c10=[msg("100", "10", OTHER, "new", hours_ago=2 * DAY), msg("90", "10", OTHER, "old", hours_ago=10 * DAY)])
    p = await overview(c, cfg("10"))
    [entry] = p["channels"]
    assert entry["channel"] == "chan10"
    assert entry["message_count"] == 1
    assert entry["people"] == 1


async def test_channel_with_no_activity_is_counted_as_quiet():
    c = server(c10=[msg("100", "10", hours_ago=DAY)], c20=[msg("200", "20", hours_ago=12 * DAY)])
    p = await overview(c, cfg("10", "20"))
    assert [e["channel"] for e in p["channels"]] == ["chan10"]
    assert p["quiet_channels"] == 1


async def test_top_topics_strong_signal_first_then_most_messages():
    c = server(c10=[])
    thread(c, "41", "10", [msg(f"41{i}", "41", OTHER, f"chat {i}", hours_ago=DAY + i) for i in range(5)])
    thread(c, "42", "10", [msg("421", "42", OTHER, "report due Nov 20", hours_ago=DAY)])
    thread(c, "43", "10", [msg(f"43{i}", "43", OTHER, f"chat {i}", hours_ago=DAY + i) for i in range(3)])
    thread(c, "44", "10", [msg(f"44{i}", "44", OTHER, f"chat {i}", hours_ago=DAY + i) for i in range(2)])
    p = await overview(c, cfg("10"))
    entry = p["channels"][0]
    assert entry["topic_count"] == 4
    assert [t["id"] for t in entry["topics"]] == ["t:42", "t:41", "t:43"]
    assert "deadline" in entry["topics"][0]["matched"]


# --- get_server_overview: deadlines -------------------------------------------------


async def test_deadlines_in_the_coming_week_from_the_last_30_days():
    c = server(
        c10=[
            msg("101", "10", OTHER, "report due Nov 20", hours_ago=10 * DAY),  # outside days=7, inside 30
            msg("102", "10", THIRD, "please submit by Nov 17", hours_ago=DAY),
            msg("103", "10", OTHER, "final due Nov 30", hours_ago=DAY),  # beyond the coming week
            msg("104", "10", OTHER, "was due Nov 10", hours_ago=6 * DAY),  # already past
            msg("105", "10", OTHER, "I'm out Nov 18", hours_ago=DAY),  # no deadline wording
        ]
    )
    p = await overview(c, cfg("10"))
    assert [d["due"] for d in p["deadlines"]] == ["2026-11-17", "2026-11-20"]
    first = p["deadlines"][0]
    assert first["phrase"] == "Nov 17"
    assert first["where"] == "chan10"
    assert first["message"]["link"].endswith("/10/102")
    assert first["id"].startswith("m:10/")


async def test_broadcast_channels_give_no_deadlines():
    c = server(c30=[msg("301", "30", OTHER, "leave until Nov 18, back by Nov 19", hours_ago=DAY)])
    p = await overview(c, cfg("30", pings={"30": "direct_only"}))
    assert p["deadlines"] == []
    assert p["channels"][0]["channel"] == "chan30"  # still part of last week's discussion


async def test_deadline_list_is_capped():
    c = server(c10=[msg("101", "10", OTHER, "due Nov 17", hours_ago=DAY), msg("102", "10", OTHER, "due Nov 18", hours_ago=DAY)])
    p = await overview(c, cfg("10", brief_max_deadlines=1))
    assert [d["due"] for d in p["deadlines"]] == ["2026-11-17"]
    assert p["omitted_deadlines"] == 1


async def test_deadlines_use_the_configured_time_zone():
    # Posted Sat 14 Nov 20:00 UTC = Sun 15 Nov 05:00 in Tokyo; NOW is Sun 15 Nov 09:00 UTC.
    c = server(c10=[msg("101", "10", OTHER, "due today", hours_ago=13)])
    assert (await overview(c, cfg("10")))["deadlines"] == []
    p = await overview(c, cfg("10", timezone="Asia/Tokyo"))
    assert [d["due"] for d in p["deadlines"]] == ["2026-11-15"]


async def test_upcoming_days_includes_dates_within_range():
    c = server(c10=[msg("101", "10", OTHER, "final due Nov 28", hours_ago=DAY)])
    assert (await overview(c, cfg("10")))["deadlines"] == []
    p = await overview(c, cfg("10"), upcoming_days=14)
    assert [d["due"] for d in p["deadlines"]] == ["2026-11-28"]


# --- get_server_overview: access ----------------------------------------------------


async def test_hidden_private_thread_never_appears_in_allow_all_mode():
    c = server(c10=[msg("100", "10", OTHER, "hi", hours_ago=DAY)])
    thread(c, "43", "10", [msg("430", "43", OTHER, "secret due Nov 20", hours_ago=DAY)], ctype=12)
    p = await overview(c, cfg(allow_all=True))
    assert p["deadlines"] == []
    assert p["skipped_channels"] == 0
    assert all(t["id"] != "t:43" for e in p["channels"] for t in e["topics"])
    assert "messages:43" not in c.calls


async def test_denied_channel_is_never_read():
    c = server(c10=[msg("100", "10", hours_ago=DAY)], c20=[msg("200", "20", OTHER, "due Nov 20", hours_ago=DAY)])
    p = await overview(c, cfg(allow_all=True, deny_channels=["20"]))
    assert [e["channel"] for e in p["channels"]] == ["chan10"]
    assert p["quiet_channels"] == 0
    assert "messages:20" not in c.calls


@pytest.mark.parametrize("kw", [{"days": 31}, {"days": 0}, {"deadline_days": 31}, {"upcoming_days": 15}, {"upcoming_days": 0}])
async def test_window_bounds(kw):
    with pytest.raises(InputError):
        await overview(server(c10=[]), cfg("10"), **kw)


# --- fitting the overview to its budget ---------------------------------------------


def _entry(name, n_topics, count, when):
    topic = {"id": f"t:{name}", "where": name, "message_count": 1, "last_activity": when, "opening": {"text": "x" * 300}}
    return {"channel": name, "message_count": count, "last_activity": when, "topics": [dict(topic) for _ in range(n_topics)]}


def test_budget_keeps_deadlines_then_trims_topics_then_drops_quietest_channels():
    channels = [_entry("busy", 3, 50, "2026-11-14T10:00"), _entry("calm", 3, 2, "2026-11-14T11:00")]
    deadlines = [{"due": "2026-11-17", "text": "y" * 200}]
    full = fit_server_overview(channels, deadlines, {}, 10**6, 3)
    assert [len(e["topics"]) for e in full["channels"]] == [3, 3]
    assert full["truncated"] is False

    trimmed = fit_server_overview(channels, deadlines, {}, 2200, 3)
    assert trimmed["deadlines"] == deadlines
    assert all(len(e["topics"]) < 3 for e in trimmed["channels"])

    dropped = fit_server_overview(channels, deadlines, {}, 1000, 3)
    assert dropped["deadlines"] == deadlines
    assert [e["channel"] for e in dropped["channels"]] in (["busy"], [])
    assert dropped["omitted_channels"] >= 1
    assert dropped["truncated"] is True


# --- get_open_loops ----------------------------------------------------------------


async def loops(c, config, **kw):
    return (await make(c, config).get_open_loops(IDENT, **kw)).payload


def kinds(p):
    return [(x["kind"], x["id"]) for x in p["loops"]]


def ask(mid, ch, h, text="can you share the draft?", to=OTHER, **kw):
    return msg(mid, ch, ME, text, hours_ago=h, mentions=[user(to)], **kw)


async def test_unanswered_ask_to_a_named_person():
    c = server(c10=[ask("100", "10", 2 * DAY)])
    p = await loops(c, cfg("10"))
    [item] = p["loops"]
    assert item["kind"] == "unanswered_ask"
    assert item["who"] == [f"user{OTHER}"]
    assert item["message"]["link"].endswith("/10/100")
    assert item["quiet_days"] == 2


async def test_ask_younger_than_a_day_is_not_a_loop_yet():
    c = server(c10=[ask("100", "10", 5)])
    assert (await loops(c, cfg("10")))["loops"] == []


async def test_ask_answered_next_day_in_a_new_stretch_is_closed():
    c = server(c10=[ask("100", "10", 3 * DAY), msg("101", "10", OTHER, "here it is", hours_ago=DAY)])
    assert (await loops(c, cfg("10")))["loops"] == []


async def test_ask_answered_in_a_thread_of_the_same_channel_is_closed():
    c = server(c10=[ask("100", "10", 3 * DAY)])
    thread(c, "41", "10", [msg("411", "41", OTHER, "replying here", hours_ago=2 * DAY)])
    assert all(x["kind"] != "unanswered_ask" for x in (await loops(c, cfg("10")))["loops"])


async def test_role_or_here_asks_do_not_name_anyone():
    role = msg("100", "10", ME, "<@&501> can you check?", hours_ago=2 * DAY, mention_roles=[ROLE_DEV])
    here = msg("101", "10", ME, "@here can you check?", hours_ago=2 * DAY, mention_everyone=True)
    assert (await loops(server(c10=[role, here]), cfg("10")))["loops"] == []


async def test_reply_counts_as_naming_the_person():
    theirs = msg("99", "10", OTHER, "I have a question", hours_ago=3 * DAY)
    mine = msg("100", "10", ME, "what do you need?", hours_ago=2 * DAY, referenced=theirs)
    assert kinds(await loops(server(c10=[theirs, mine]), cfg("10")))[0][0] == "unanswered_ask"


async def test_a_bot_is_never_owed_a_reply():
    c = server(c10=[msg("100", "10", ME, "can you run?", hours_ago=2 * DAY, mentions=[user("1003", bot=True)])])
    assert (await loops(c, cfg("10")))["loops"] == []


async def test_promise_to_me_gone_quiet():
    promise = msg("100", "10", OTHER, "I'll send it to you", hours_ago=3 * DAY, mentions=[user(ME)])
    p = await loops(server(c10=[promise]), cfg("10"))
    assert kinds(p)[0][0] == "promise_to_you"
    assert p["loops"][0]["who"] == [f"user{OTHER}"]


async def test_promise_to_me_followed_up_is_closed():
    promise = msg("100", "10", OTHER, "I'll send it to you", hours_ago=3 * DAY, mentions=[user(ME)])
    later = msg("101", "10", OTHER, "sent", hours_ago=DAY)
    assert (await loops(server(c10=[promise, later]), cfg("10")))["loops"] == []


async def test_promise_to_me_younger_than_two_days_waits():
    promise = msg("100", "10", OTHER, "will do", hours_ago=DAY + 2, mentions=[user(ME)])
    assert (await loops(server(c10=[promise]), cfg("10")))["loops"] == []


async def test_stalled_thread_ending_on_a_question():
    c = server(c10=[])
    thread(c, "41", "10", [
        msg("411", "41", ME, "draft is up", hours_ago=6 * DAY),
        msg("412", "41", THIRD, "who reviews this?", hours_ago=5 * DAY),
    ])
    p = await loops(c, cfg("10"))
    [item] = p["loops"]
    assert (item["kind"], item["id"]) == ("stalled", "t:41")
    assert item["who"] == [f"user{THIRD}"]
    assert item["quiet_days"] == 5


async def test_quiet_thread_without_an_ask_or_signal_is_not_stalled():
    c = server(c10=[])
    thread(c, "41", "10", [msg("411", "41", ME, "done", hours_ago=6 * DAY), msg("412", "41", THIRD, "thanks", hours_ago=5 * DAY)])
    assert (await loops(c, cfg("10")))["loops"] == []


async def test_recent_thread_is_not_stalled():
    c = server(c10=[])
    thread(c, "41", "10", [msg("411", "41", ME, "x", hours_ago=3 * DAY), msg("412", "41", THIRD, "ok?", hours_ago=2 * DAY)])
    assert (await loops(c, cfg("10")))["loops"] == []


async def test_main_channel_stretches_are_never_stalled():
    c = server(c10=[msg("100", "10", ME, "x", hours_ago=6 * DAY), msg("101", "10", THIRD, "who owns this?", hours_ago=5 * DAY)])
    assert (await loops(c, cfg("10")))["loops"] == []


async def test_a_thread_listed_for_an_ask_is_not_also_stalled():
    c = server(c10=[])
    thread(c, "41", "10", [ask("411", "41", 6 * DAY, text="can you review?")])
    assert kinds(await loops(c, cfg("10"))) == [("unanswered_ask", "t:41")]


async def test_my_unfollowed_promise():
    mine = msg("100", "10", ME, "I'll send the deck", hours_ago=2 * DAY)
    p = await loops(server(c10=[mine]), cfg("10"))
    assert kinds(p)[0][0] == "your_promise"
    assert p["loops"][0]["who"] == []


async def test_my_promise_followed_up_in_the_same_stretch_is_closed():
    mine = msg("100", "10", ME, "I'll send the deck", hours_ago=2 * DAY)
    later = msg("101", "10", ME, "here it is", hours_ago=2 * DAY - 1)
    assert (await loops(server(c10=[mine, later]), cfg("10")))["loops"] == []


async def test_my_promise_followed_up_by_a_reply_elsewhere_is_closed():
    mine = msg("100", "10", ME, "I'll send the deck", hours_ago=4 * DAY)
    later = msg("101", "10", ME, "attached", hours_ago=DAY, referenced=mine)  # a new stretch, but a reply
    assert (await loops(server(c10=[mine, later]), cfg("10")))["loops"] == []


async def test_my_message_in_a_later_stretch_does_not_close_an_unrelated_promise():
    mine = msg("100", "10", ME, "I'll send the deck", hours_ago=4 * DAY)
    later = msg("101", "10", ME, "lunch?", hours_ago=DAY)
    assert kinds(await loops(server(c10=[mine, later]), cfg("10")))[0][0] == "your_promise"


async def test_loops_order_and_counts():
    c = server(c10=[
        msg("100", "10", ME, "I'll send the deck", hours_ago=9 * DAY),
        ask("200", "10", 7 * DAY),
    ])
    c.add_channel(channel("20", "chan20", parent_id="1"), [
        msg("300", "20", OTHER, "will share by monday", hours_ago=5 * DAY, mentions=[user(ME)]),
    ])
    thread(c, "41", "20", [msg("411", "41", ME, "x", hours_ago=6 * DAY), msg("412", "41", THIRD, "status?", hours_ago=4 * DAY)])
    p = await loops(c, cfg("10", "20"))
    assert [x["kind"] for x in p["loops"]] == ["your_promise", "unanswered_ask", "promise_to_you", "stalled"]
    assert p["counts"] == {"your_promise": 1, "unanswered_ask": 1, "promise_to_you": 1, "stalled": 1}


async def test_budget_cuts_stalled_first():
    c = server(c10=[ask("100", "10", 3 * DAY, text="can you send it? " + "x" * 150)])
    for i in range(8):
        thread(c, f"4{i}", "10", [msg(f"4{i}1", f"4{i}", ME, "x", hours_ago=9 * DAY),
                                   msg(f"4{i}2", f"4{i}", THIRD, "status? " + "y" * 150, hours_ago=8 * DAY)])
    p = await loops(c, cfg("10", max_result_chars=2500))
    assert p["omitted"]["stalled"] >= 1 and p["omitted"]["unanswered_ask"] == 0
    assert p["loops"][0]["kind"] == "unanswered_ask"
    assert p["truncated"] is True


async def test_hidden_private_thread_gives_no_loops_in_allow_all_mode():
    c = server(c10=[])
    thread(c, "43", "10", [ask("431", "43", 3 * DAY)], ctype=12)
    p = await loops(c, cfg(allow_all=True))
    assert p["loops"] == [] and p["skipped_channels"] == 0


@pytest.mark.parametrize("days", [0, 31])
async def test_loops_window_bounds(days):
    with pytest.raises(InputError):
        await loops(server(c10=[]), cfg("10"), days=days)


def test_showing_the_top_topics_is_not_truncation():
    out = fit_server_overview([_entry("busy", 6, 50, "2026-11-14T10:00")], [], {}, 10**6, 3)
    assert len(out["channels"][0]["topics"]) == 3
    assert out["truncated"] is False


# --- review findings: open loops ------------------------------------------------------


async def test_two_unanswered_asks_in_one_stretch_keep_both_people():
    first = ask("100", "10", 3 * DAY, text="can you review?", to=OTHER)
    second = ask("101", "10", 3 * DAY - 1, text="could you sign off?", to=THIRD)
    p = await loops(server(c10=[first, second]), cfg("10"))
    [item] = [x for x in p["loops"] if x["kind"] == "unanswered_ask"]
    assert sorted(item["who"]) == sorted([f"user{OTHER}", f"user{THIRD}"])


async def test_two_promises_to_me_in_one_thread_keep_both_people():
    c = server(c10=[])
    thread(c, "41", "10", [
        msg("411", "41", OTHER, "I'll send it", hours_ago=4 * DAY, mentions=[user(ME)]),
        msg("412", "41", THIRD, "I will share mine", hours_ago=3 * DAY, mentions=[user(ME)]),
    ])
    p = await loops(c, cfg("10"))
    [item] = [x for x in p["loops"] if x["kind"] == "promise_to_you"]
    assert sorted(item["who"]) == sorted([f"user{OTHER}", f"user{THIRD}"])


@pytest.mark.parametrize(
    "text",
    ["That will work, thanks!", "this will be great", "see you later", "soon enough", "let me know if you need anything"],
)
async def test_ordinary_will_later_soon_are_not_my_promises(text):
    p = await loops(server(c10=[msg("100", "10", ME, text, hours_ago=2 * DAY)]), cfg("10"))
    assert p["loops"] == []


async def test_a_question_to_someone_is_an_ask_not_a_promise():
    m = msg("100", "10", ME, "will you send it?", hours_ago=2 * DAY, mentions=[user(OTHER)])
    assert kinds(await loops(server(c10=[m]), cfg("10"))) == [("unanswered_ask", "m:10/100")]


@pytest.mark.parametrize("text", ["I'll send the deck", "I will share it", "will send by friday", "let me check", "On it"])
async def test_first_person_promises_count(text):
    p = await loops(server(c10=[msg("100", "10", ME, text, hours_ago=2 * DAY)]), cfg("10"))
    assert kinds(p)[0][0] == "your_promise"


async def test_someone_elses_ordinary_will_is_not_a_promise_to_me():
    m = msg("100", "10", OTHER, "that will work for me", hours_ago=4 * DAY, mentions=[user(ME)])
    assert (await loops(server(c10=[m]), cfg("10")))["loops"] == []


def test_open_loops_is_linear_in_messages():
    import time

    from discord_mcp.brief import ChannelGroup, open_loops
    from discord_mcp.matching import RenderContext
    from discord_mcp.signals import Words
    from discord_mcp.topics import Topic
    from tests.fakes import NOW as now

    def group(n):
        msgs = [msg(str(1000 + i), "10", OTHER, "I'll do it", hours_ago=900 - i * 0.01, mentions=[user(ME)]) for i in range(n)]
        return [ChannelGroup({"id": "10"}, "c", [Topic("main", "10", "c", msgs)], Words.build([]))]

    ctx = RenderContext(guild_id=GUILD_ID, role_names={}, channel_names={})
    t0 = time.perf_counter(); open_loops(group(2000), IDENT, ctx, now); small = time.perf_counter() - t0
    t0 = time.perf_counter(); open_loops(group(8000), IDENT, ctx, now); big = time.perf_counter() - t0
    assert big < small * 8  # 4x the messages must not cost 16x the time


def test_fit_loops_drops_the_oldest_of_a_kind_first():
    from discord_mcp.brief import fit_loops

    rows = [
        {"kind": "stalled", "since": f"2026-11-0{d}T00:00", "pad": "x" * 600, "id": f"t:{d}"} for d in range(1, 6)
    ]
    out = fit_loops(rows, {}, 2500)
    assert out["omitted"]["stalled"] >= 1
    assert out["loops"][-1]["id"] == "t:5"  # the newest stays


def test_fit_loops_is_not_quadratic():
    import time

    from discord_mcp.brief import fit_loops

    rows = [{"kind": "stalled", "since": "2026-11-01T00:00", "pad": "x" * 100, "id": f"t:{i}"} for i in range(3000)]
    t0 = time.perf_counter()
    out = fit_loops(rows, {}, 40000)
    assert time.perf_counter() - t0 < 1.0
    assert out["omitted"]["stalled"] > 0


async def test_deadline_cap_sets_truncated():
    c = server(c10=[msg(f"1{i:02d}", "10", OTHER, "report due Nov 20", hours_ago=DAY + i) for i in range(5)])
    p = await overview(c, cfg("10", brief_max_deadlines=2))
    assert p["omitted_deadlines"] == 3
    assert p["truncated"] is True


async def test_a_question_mark_inside_a_link_is_not_an_ask():
    shared = ask("100", "10", 3 * DAY, text="for folks trying it - https://example.com/post?ref=abc")
    assert (await loops(server(c10=[shared]), cfg("10")))["loops"] == []


async def test_a_real_question_next_to_a_link_is_still_an_ask():
    q = ask("100", "10", 3 * DAY, text="https://example.com/post?ref=abc worth a look?")
    assert [x["kind"] for x in (await loops(server(c10=[q]), cfg("10")))["loops"]] == ["unanswered_ask"]


def test_a_question_mark_right_after_a_link_is_still_a_question():
    from discord_mcp.wording import asks as _asks

    assert _asks("did you see https://example.com/doc?")
    assert _asks("have you read <https://example.com/doc>?")
    assert not _asks("https://example.com/a?b=1")


async def test_broadcast_channels_still_give_loops():
    """direct_only only changes how @here and role pings count; work happens there too."""
    promise = msg("100", "10", OTHER, "I'll send it to you", hours_ago=3 * DAY, mentions=[user(ME)])
    mine = ask("101", "10", 2 * DAY, to=THIRD)  # a different person, so the two loops stay separate
    p = await loops(server(c10=[promise, mine]), cfg("10", pings={"10": "direct_only"}))
    assert sorted(k for k, _ in kinds(p)) == ["promise_to_you", "unanswered_ask"]


async def test_leave_notice_naming_me_is_not_a_loop():
    c = server(c10=[])
    thread(c, "41", "10", [msg("411", "41", OTHER, f"I will be on leave next week <@{ME}>", hours_ago=3 * DAY, mentions=[user(ME)])])
    assert (await loops(c, cfg("10", pings={"10": "direct_only"})))["loops"] == []


@pytest.mark.parametrize(
    "text",
    [
        "I will not be able to make the sync on Friday",
        "I'll not make it today",
        "I will be on leave next Tuesday",
        "I'll be out tomorrow",
        "I will be unavailable all of next week",
        "I'll be back on Monday",
        "I will be OOO Friday",
        "I'll be on PTO next week",
        "I will be on vacation till the 5th",
        "I'll be on holiday",
    ],
)
async def test_refusals_and_absences_are_not_promises(text):
    theirs = msg("100", "10", OTHER, text, hours_ago=3 * DAY, mentions=[user(ME)])
    mine = msg("101", "20", ME, text, hours_ago=3 * DAY)
    assert (await loops(server(c10=[theirs], c20=[mine]), cfg("10", "20")))["loops"] == []


async def test_will_be_doing_something_is_still_a_promise():
    m = msg("100", "10", ME, "I will be sending the deck tomorrow", hours_ago=2 * DAY)
    p = await loops(server(c10=[m]), cfg("10"))
    assert kinds(p)[0][0] == "your_promise"
