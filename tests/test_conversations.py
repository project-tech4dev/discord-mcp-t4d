import pytest
from discord_mcp.config import Limits
from discord_mcp.conversations import group_conversations
from discord_mcp.matching import ChannelMessages, Identity, RenderContext
from tests.fakes import GUILD_ID, ME, OTHER, ROLE_DEV, THIRD, channel, msg, user

IDENT = Identity(user_id=ME, role_ids=frozenset({ROLE_DEV}))
CH = channel("10", "general", parent_id="1")


def ctx(**limits) -> RenderContext:
    return RenderContext(guild_id=GUILD_ID, role_names={ROLE_DEV: "dev"}, channel_names={}, limits=Limits(**limits))


def ping(mid, author=OTHER, h=5.0, ch="10", **kw):
    return msg(mid, ch, author, f"<@{ME}> can you look", mentions=[user(ME)], hours_ago=h, **kw)


def say(mid, author, h, ch="10", **kw):
    return msg(mid, ch, author, f"text {mid}", hours_ago=h, **kw)


def thread(tid, name="topic"):
    return channel(tid, name, 11, parent_id="10")


def group(*cms, **limits):
    return group_conversations(list(cms), IDENT, ctx(**limits))


def plain(*messages):
    return ChannelMessages(CH, "general", list(messages))


def ids(conv):
    return [m["id"] for m in conv.messages]


# --- threads ------------------------------------------------------------------


def test_thread_with_a_ping_is_one_conversation():
    t = ChannelMessages(thread("50"), "general › topic", [say("51", THIRD, 6, "50"), ping("52", ch="50"), say("53", ME, 4, "50")])
    convs = group(t)
    assert len(convs) == 1
    assert ids(convs[0]) == ["51", "52", "53"]
    assert [p[0]["id"] for p in convs[0].pings] == ["52"]
    assert convs[0].label == "general › topic"


def test_thread_without_ping_gives_no_conversation():
    t = ChannelMessages(thread("50"), "general › topic", [say("51", THIRD, 6, "50"), say("52", OTHER, 5, "50")])
    assert group(t) == []


def test_ping_that_started_a_thread_joins_the_thread():
    parent = plain(ping("100", h=6, thread={"id": "100"}))
    t = ChannelMessages(thread("100"), "general › topic", [say("101", OTHER, 5, "100"), say("102", ME, 4, "100")])
    convs = group(parent, t)
    assert len(convs) == 1
    assert convs[0].channel_id == "10"
    assert ids(convs[0]) == ["100", "101", "102"]
    assert convs[0].id == "10/100"


# --- plain channels: follow the people ----------------------------------------


def test_plain_channel_follows_pinger_and_me():
    convs = group(plain(ping("100", h=6), say("101", THIRD, 5.5), say("102", OTHER, 5), say("103", ME, 4)))
    assert len(convs) == 1
    assert ids(convs[0]) == ["100", "102", "103"]


def test_reply_link_joins_even_from_non_participant():
    p = ping("100", h=6)
    convs = group(plain(p, say("101", THIRD, 5, referenced=p)))
    assert ids(convs[0]) == ["100", "101"]


def test_gap_ends_conversation():
    convs = group(plain(ping("100", h=30), say("101", ME, 17)))
    assert ids(convs[0]) == ["100"]


def test_max_hours_ends_conversation():
    convs = group(plain(ping("100", h=40), say("101", OTHER, 30), say("102", OTHER, 20), say("103", ME, 10)))
    assert ids(convs[0]) == ["100", "101", "102"]


def test_my_message_goes_to_most_recent_ping():
    convs = group(plain(ping("100", OTHER, 5), ping("101", THIRD, 4), say("102", ME, 3)))
    by_first = {c.messages[0]["id"]: ids(c) for c in convs}
    assert by_first == {"100": ["100"], "101": ["101", "102"]}


def test_my_reply_goes_to_its_target_conversation():
    p = ping("100", OTHER, 5)
    convs = group(plain(p, ping("101", THIRD, 4), say("102", ME, 3, referenced=p)))
    by_first = {c.messages[0]["id"]: ids(c) for c in convs}
    assert by_first == {"100": ["100", "102"], "101": ["101"]}


def test_ping_from_participant_joins_open_conversation():
    convs = group(plain(ping("100", h=5), ping("101", h=4)))
    assert len(convs) == 1
    assert [p[0]["id"] for p in convs[0].pings] == ["100", "101"]


def test_everyone_conversation_does_not_take_my_plain_message():
    everyone = msg("100", "10", OTHER, "@everyone standup moved", mention_everyone=True, hours_ago=5)
    convs = group(plain(everyone, say("101", ME, 4)))
    assert len(convs) == 1
    assert ids(convs[0]) == ["100"]
    convs = group(plain(everyone, say("101", ME, 4, referenced=everyone)))
    assert ids(convs[0]) == ["100", "101"]


def test_here_ping_counts_me_as_participant():
    here = msg("100", "10", OTHER, "@here quick one", mention_everyone=True, hours_ago=5)
    convs = group(plain(here, say("101", ME, 4)))
    assert ids(convs[0]) == ["100", "101"]


# --- status -------------------------------------------------------------------

import json  # noqa: E402
import re  # noqa: E402

from discord_mcp.conversations import fit_budget, render_row, status_of  # noqa: E402


def one(*messages, **limits):
    convs = group(plain(*messages), **limits)
    assert len(convs) == 1
    return convs[0]


def test_status_needs_you_when_i_never_replied():
    assert status_of(one(ping("100", h=5), say("101", OTHER, 4)), IDENT) == "needs_you"


def test_status_handled_when_my_last_message_is_after_last_ping():
    assert status_of(one(ping("100", h=5), say("101", ME, 4)), IDENT) == "handled"


def test_status_reopened_when_pinged_after_my_reply():
    assert status_of(one(ping("100", h=5), say("101", ME, 4), ping("102", h=3)), IDENT) == "reopened"


def test_status_announcement_when_only_everyone_pings():
    everyone = msg("100", "10", OTHER, "@everyone standup moved", mention_everyone=True, hours_ago=5)
    assert status_of(one(everyone), IDENT) == "announcement"


def test_here_conversation_is_not_announcement():
    here = msg("100", "10", OTHER, "@here quick one", mention_everyone=True, hours_ago=5)
    assert status_of(one(here), IDENT) == "needs_you"


# --- rows ---------------------------------------------------------------------

MINUTE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d$")


def test_needs_you_row_shape():
    row = render_row(one(ping("100", h=5)), IDENT, ctx())
    assert set(row) == {
        "status", "where", "people", "started", "last_activity", "message_count",
        "ping_count", "pings", "your_last_reply", "latest", "id",
    }
    assert row["status"] == "needs_you"
    assert row["your_last_reply"] is None
    assert row["id"] == "10/100"
    p = row["pings"][0]
    assert MINUTE.match(p["time"])
    assert "is_bot" not in p
    assert p["reason"] == "mention"
    assert p["link"] == f"https://discord.com/channels/{GUILD_ID}/10/100"


def test_handled_row_has_only_latest_ping_and_my_reply():
    long = "x" * 500
    conv = one(
        ping("100", h=6),
        msg("101", "10", OTHER, f"<@{ME}> {long}", mentions=[user(ME)], hours_ago=5),
        msg("102", "10", ME, "done, merged", hours_ago=4),
    )
    row = render_row(conv, IDENT, ctx())
    assert row["status"] == "handled"
    assert [p["link"].rsplit("/", 1)[1] for p in row["pings"]] == ["101"]
    assert len(row["pings"][0]["text"]) <= 200
    assert row["your_last_reply"]["text"] == "done, merged"
    assert "latest" not in row
    assert row["ping_count"] == 2


def test_row_keeps_first_and_newest_pings():
    body = [ping(str(51 + i), ch="50", h=10 - i) for i in range(8)]
    convs = group(ChannelMessages(thread("50"), "general › topic", body))
    row = render_row(convs[0], IDENT, ctx())
    assert row["ping_count"] == 8
    assert [p["link"].rsplit("/", 1)[1] for p in row["pings"]] == ["51", "55", "56", "57", "58"]


def test_latest_shown_when_someone_else_answered():
    here = msg("100", "10", OTHER, "@here who can review", mention_everyone=True, hours_ago=5)
    t = msg("101", "10", THIRD, "on it", hours_ago=4, referenced=here)
    row = render_row(one(here, t), IDENT, ctx())
    assert row["status"] == "needs_you"
    assert row["latest"]["author"] == "user1004"
    assert row["latest"]["text"] == "on it"


# --- budget -------------------------------------------------------------------


def fake_row(status, when, pad=0):
    return {"status": status, "last_activity": f"2026-11-{when:02d}T00:00", "pad": "x" * pad}


def test_order_is_status_then_newest():
    rows = [fake_row("handled", 3), fake_row("needs_you", 1), fake_row("announcement", 9), fake_row("needs_you", 5)]
    out = fit_budget(rows, {}, 10**6)
    assert [(r["status"], r["last_activity"][8:10]) for r in out["conversations"]] == [
        ("needs_you", "05"), ("needs_you", "01"), ("handled", "03"), ("announcement", "09"),
    ]


def test_budget_drops_announcements_then_handled_first():
    rows = [fake_row("needs_you", 1, 900)]
    rows += [fake_row("handled", d, 900) for d in range(2, 7)]
    rows += [fake_row("announcement", d, 900) for d in range(7, 12)]
    out = fit_budget(rows, {"days": 7}, 4000)
    assert len(json.dumps(out)) <= 4000
    assert out["conversations"][0]["status"] == "needs_you"
    assert out["omitted"]["announcement"] == 5
    assert out["omitted"]["handled"] >= 1
    assert out["counts"] == {"needs_you": 1, "reopened": 0, "check": 0, "handled": 5, "announcement": 5}
    assert out["truncated"] is True
    assert out["days"] == 7
    kept_handled = [r["last_activity"] for r in out["conversations"] if r["status"] == "handled"]
    assert kept_handled == sorted(kept_handled, reverse=True)
    assert "2026-11-06T00:00" in kept_handled  # newest handled kept, oldest dropped


def test_budget_drops_oldest_needs_you_when_alone_over():
    rows = [fake_row("needs_you", d, 900) for d in range(1, 11)]
    out = fit_budget(rows, {}, 3000)
    kept = [r["last_activity"][8:10] for r in out["conversations"]]
    assert kept and kept == sorted(kept, reverse=True) and kept[0] == "10"
    assert out["omitted"]["needs_you"] == 10 - len(kept)
    assert out["truncated"] is True


def test_under_budget_not_truncated():
    out = fit_budget([fake_row("handled", 1)], {}, 10**6)
    assert out["omitted"] == {"needs_you": 0, "reopened": 0, "check": 0, "handled": 0, "announcement": 0}
    assert out["truncated"] is False


def test_system_messages_are_ignored():
    join = msg("100", "10", OTHER, "", mentions=[user(ME)], mtype=7, hours_ago=5)
    assert group(plain(join)) == []


def test_render_full_keeps_first_ping_and_newest_when_capped():
    from discord_mcp.conversations import render_full

    body = [ping("100", h=20, ch="50")] + [say(str(101 + i), OTHER, 19 - i * 0.1, "50") for i in range(59)]
    conv = group(ChannelMessages(thread("50"), "general › topic", body))[0]
    out = render_full(conv, ctx(conversation_max_messages=50))
    got = [m["link"].rsplit("/", 1)[1] for m in out["messages"]]
    assert len(got) == 50
    assert got[0] == "100"
    assert got[1:] == [str(i) for i in range(111, 160)]
    assert out["truncated"] is True
    assert out["id"] == "50/100"
    assert out["where"] == "general › topic"


# --- final review fixes ---------------------------------------------------------


def test_reply_to_a_non_ping_does_not_close_another_conversation():
    lunch = say("100", THIRD, 6)
    convs = group(plain(lunch, ping("101", OTHER, 5), say("102", ME, 4.9, referenced=lunch)))
    assert len(convs) == 1
    assert status_of(convs[0], IDENT) == "needs_you"
    assert ids(convs[0]) == ["101"]


def test_reply_to_an_ended_conversation_goes_there_not_to_the_live_one():
    old = ping("100", OTHER, 40)
    convs = group(plain(old, ping("101", THIRD, 10), say("102", ME, 9.9, referenced=old)))
    by_first = {c.messages[0]["id"]: c for c in convs}
    assert status_of(by_first["101"], IDENT) == "needs_you"
    assert ids(by_first["100"]) == ["100", "102"]
    assert status_of(by_first["100"], IDENT) == "handled"


def test_direct_ping_joining_announcement_makes_me_a_participant():
    everyone = msg("100", "10", OTHER, "@everyone launch", mention_everyone=True, hours_ago=6)
    convs = group(plain(everyone, ping("101", OTHER, 5), ping("102", THIRD, 4.5), say("103", ME, 4.4, referenced=None)))
    by_first = {c.messages[0]["id"]: c for c in convs}
    # my plain message goes to the conversation with the most recent ping (THIRD's), and OTHER's
    # direct ping now counts me as a participant so the conversation is not stuck as an announcement
    assert status_of(by_first["100"], IDENT) == "needs_you"
    assert "103" in ids(by_first["102"])
    convs = group(plain(everyone, ping("101", OTHER, 5), say("103", ME, 4.4)))
    assert ids(convs[0]) == ["100", "101", "103"]
    assert status_of(convs[0], IDENT) == "handled"


def test_render_full_keeps_the_first_ping_when_it_is_not_the_first_message():
    from discord_mcp.conversations import render_full

    body = [say(str(100 + i), THIRD, 30 - i * 0.1, "50") for i in range(10)]
    body.append(ping("110", h=28, ch="50"))
    body += [say(str(111 + i), OTHER, 27 - i * 0.1, "50") for i in range(60)]
    conv = group(ChannelMessages(thread("50"), "general › topic", body))[0]
    got = [m["link"].rsplit("/", 1)[1] for m in render_full(conv, ctx(conversation_max_messages=50))["messages"]]
    assert len(got) == 50
    assert got[:2] == ["100", "110"]
    assert got[-1] == "170"


def test_budget_is_measured_as_the_transport_sends_it():
    import pydantic_core

    rows = [fake_row("handled", d % 28 + 1) | {f"k{i}": i for i in range(10)} for d in range(400)]
    out = fit_budget(rows, {}, 20000)
    assert len(pydantic_core.to_json(out, indent=2).decode()) <= 20000
    assert out["truncated"] is True


def test_heavy_week_keeps_every_needs_you_row_and_stays_in_budget():
    import pydantic_core

    from discord_mcp.conversations import render_row

    rows = []
    for c in range(10):
        ch = channel(str(70 + c), f"team-{c}", parent_id="1")
        msgs = []
        for i in range(14):  # 14 pings per channel, 140 in all, 13h apart so each is its own conversation
            mid = 10_000 * (c + 1) + 10 * i
            h = 160 - 13 * i
            text = f"<@{ME}> " + "q" * 240
            msgs.append(msg(str(mid), ch["id"], OTHER, text, mentions=[user(ME)], hours_ago=h))
            if i % 7:  # 2 of every 14 left unanswered
                msgs.append(msg(str(mid + 1), ch["id"], ME, "a" * 240, hours_ago=h - 0.5))
        rows += [render_row(conv, IDENT, ctx()) for conv in group(ChannelMessages(ch, ch["name"], msgs))]
    out = fit_budget(rows, {"days": 7}, 40000)
    assert out["counts"]["needs_you"] == 20
    assert out["omitted"]["needs_you"] == 0
    assert sum(r["status"] == "needs_you" for r in out["conversations"]) == 20
    assert len(pydantic_core.to_json(out, indent=2).decode()) <= 40000


def test_render_full_stays_within_budget():
    from discord_mcp.conversations import render_full

    body = [ping("100", h=20, ch="50")] + [say(str(101 + i), OTHER, 19 - i * 0.1, "50") for i in range(45)]
    for m in body:
        m["content"] = "z" * 1500
    conv = group(ChannelMessages(thread("50"), "general › topic", body))[0]
    import pydantic_core

    out = render_full(conv, ctx(max_result_chars=40000))
    assert len(pydantic_core.to_json(out, indent=2).decode()) <= 40000
    assert out["truncated"] is True
    assert out["messages"][0]["link"].endswith("/100")


def test_render_full_budget_never_drops_the_ping():
    from discord_mcp.conversations import render_full

    body = [say(str(100 + i), THIRD, 30 - i * 0.1, "50") for i in range(10)]
    body.append(ping("110", h=28, ch="50"))
    body += [say(str(111 + i), OTHER, 27 - i * 0.1, "50") for i in range(30)]
    for m in body:
        m["content"] = "z" * 1500
    conv = group(ChannelMessages(thread("50"), "general › topic", body))[0]
    got = [m["link"].rsplit("/", 1)[1] for m in render_full(conv, ctx(max_result_chars=20000))["messages"]]
    assert got[:2] == ["100", "110"]


# --- direct_only channels ------------------------------------------------------


def quiet_ctx() -> RenderContext:
    return RenderContext(
        guild_id=GUILD_ID, role_names={ROLE_DEV: "dev"}, channel_names={}, direct_only=frozenset({"10"})
    )


def quiet_group(*cms):
    return group_conversations(list(cms), IDENT, quiet_ctx())


def test_role_ping_in_direct_only_channel_is_an_announcement():
    role = msg("100", "10", OTHER, "<@&R> I am out today", mention_roles=[ROLE_DEV], hours_ago=5)
    [conv] = quiet_group(plain(role))
    assert status_of(conv, IDENT) == "announcement"
    assert conv.pings[0][1] == "group"
    assert conv.pings[0][2] == "dev"


def test_here_ping_in_direct_only_channel_is_an_announcement():
    here = msg("100", "10", OTHER, "@here offline this afternoon", mention_everyone=True, hours_ago=5)
    [conv] = quiet_group(plain(here))
    assert status_of(conv, IDENT) == "announcement"


def test_direct_mention_in_direct_only_channel_still_needs_me():
    [conv] = quiet_group(plain(ping("100")))
    assert status_of(conv, IDENT) == "needs_you"


def test_role_ping_in_thread_of_direct_only_channel_is_an_announcement():
    role = msg("51", "50", OTHER, "<@&R> heads up", mention_roles=[ROLE_DEV], hours_ago=5)
    [conv] = quiet_group(ChannelMessages(thread("50"), "general › topic", [role]))
    assert status_of(conv, IDENT) == "announcement"


def test_role_ping_elsewhere_still_needs_me():
    role = msg("100", "10", OTHER, "<@&R> please check", mention_roles=[ROLE_DEV], hours_ago=5)
    assert status_of(one(role), IDENT) == "needs_you"


def test_direct_only_applies_only_to_its_own_channel():
    other = channel("11", "dev", parent_id="1")
    quiet = msg("100", "10", OTHER, "<@&R> out today", mention_roles=[ROLE_DEV], hours_ago=5)
    loud = msg("110", "11", OTHER, "<@&R> please check", mention_roles=[ROLE_DEV], hours_ago=4)
    convs = quiet_group(plain(quiet), ChannelMessages(other, "dev", [loud]))
    assert {c.channel_id: status_of(c, IDENT) for c in convs} == {"10": "announcement", "11": "needs_you"}


def test_direct_only_on_a_category_does_not_quiet_its_channels():
    ctx_ = RenderContext(guild_id=GUILD_ID, role_names={ROLE_DEV: "dev"}, channel_names={}, direct_only=frozenset({"1"}))
    role = msg("100", "10", OTHER, "<@&R> please check", mention_roles=[ROLE_DEV], hours_ago=5)
    [conv] = group_conversations([plain(role)], IDENT, ctx_)
    assert status_of(conv, IDENT) == "needs_you"


def test_here_and_reply_in_direct_only_channel():
    here = msg("100", "10", OTHER, "@here offline", mention_everyone=True, hours_ago=5)
    reply = say("101", OTHER, 4, referenced=say("90", ME, 6))
    convs = quiet_group(plain(here, reply))
    reasons = sorted(p[1] for c in convs for p in c.pings)
    assert reasons == ["group", "reply"]


def test_my_plain_message_does_not_join_a_group_ping_conversation():
    role = msg("100", "10", OTHER, "<@&R> out today", mention_roles=[ROLE_DEV], hours_ago=5)
    [conv] = quiet_group(plain(role, say("101", ME, 4)))
    assert ids(conv) == ["100"]


def test_group_ping_does_not_set_the_answer_point():
    role = msg("100", "10", OTHER, "<@&R> out today", mention_roles=[ROLE_DEV], hours_ago=5)
    reply = say("102", OTHER, 3, referenced=say("90", ME, 6))
    mine = say("101", ME, 4, referenced=role)
    [conv] = [c for c in quiet_group(plain(role, mine, reply)) if any(p[1] == "reply" for p in c.pings)]
    assert status_of(conv, IDENT) == "needs_you"


def test_second_group_ping_does_not_make_me_a_participant():
    first = msg("100", "10", OTHER, "<@&R> out today", mention_roles=[ROLE_DEV], hours_ago=5)
    second = msg("101", "10", OTHER, "<@&R> back at 3", mention_roles=[ROLE_DEV], hours_ago=4)
    [conv] = quiet_group(plain(first, second, say("102", ME, 3)))
    assert ids(conv) == ["100", "101"]


def test_brief_marks_text_that_was_cut():
    from discord_mcp.conversations import brief

    long = msg("100", "10", OTHER, "x" * 50, hours_ago=1)
    assert brief(long, ctx(), 20)["text_cut"] is True
    assert "text_cut" not in brief(long, ctx(), 50)


# --- check status ----------------------------------------------------------------


def _reply(mid, h, text="sure, done"):
    return msg(mid, "10", ME, text, hours_ago=h)


def test_human_after_my_reply_is_check():
    conv = one(ping("100", h=6), _reply("101", 5), say("102", OTHER, 4))
    assert status_of(conv, IDENT) == "check"
    row = render_row(conv, IDENT, ctx())
    assert row["check"] == ["others_after_you"]
    assert row["latest"]["text"] == "text 102"


def test_bot_after_my_reply_stays_handled():
    conv = one(ping("100", h=6), _reply("101", 5), msg("102", "10", OTHER, "beep", hours_ago=4, bot=True))
    assert status_of(conv, IDENT) == "handled"


@pytest.mark.parametrize("text", ["will look at it", "I’ll send it", "I‘ll send it", "Iʼll send it", "let me check", "by EOD", "I'm on it", "On it!"])
def test_promise_words_make_check(text):
    conv = one(ping("100", h=6), _reply("101", 5, text))
    assert status_of(conv, IDENT) == "check"
    assert render_row(conv, IDENT, ctx())["check"] == ["you_promised"]


def test_plain_reply_stays_handled():
    conv = one(ping("100", h=6), _reply("101", 5, "done, merged"))
    assert status_of(conv, IDENT) == "handled"
    row = render_row(conv, IDENT, ctx())
    assert "check" not in row and "latest" not in row


@pytest.mark.parametrize("text", ["willingly done", "done, let me know if anything else", "I commented on it", "done, Let Me Know"])
def test_closing_phrases_and_word_parts_do_not_count(text):
    conv = one(ping("100", h=6), _reply("101", 5, text))
    assert status_of(conv, IDENT) == "handled"


def test_both_reasons():
    conv = one(ping("100", h=6), _reply("101", 5, "will do"), say("102", OTHER, 4))
    assert render_row(conv, IDENT, ctx())["check"] == ["others_after_you", "you_promised"]


def test_new_ping_after_reply_is_still_reopened():
    conv = one(ping("100", h=6), _reply("101", 5, "will do"), ping("102", h=4))
    assert status_of(conv, IDENT) == "reopened"


def test_check_row_shows_newest_other_message_after_my_reply():
    conv = one(ping("100", h=6), say("101", OTHER, 5.5), _reply("102", 5), say("103", OTHER, 4), say("104", OTHER, 3))
    row = render_row(conv, IDENT, ctx())
    assert row["status"] == "check"
    assert row["latest"]["text"] == "text 104"
    assert row["your_last_reply"]["text"] == "sure, done"


def test_promise_only_check_has_no_latest():
    row = render_row(one(ping("100", h=6), _reply("101", 5, "on it")), IDENT, ctx())
    assert row["latest"] is None


def test_check_sorts_between_reopened_and_handled():
    rows = [fake_row("handled", 9), fake_row("check", 1), fake_row("reopened", 2), fake_row("announcement", 8)]
    out = fit_budget(rows, {}, 10**6)
    assert [r["status"] for r in out["conversations"]] == ["reopened", "check", "handled", "announcement"]


def test_budget_cuts_handled_before_check():
    rows = [fake_row("check", d, 900) for d in range(1, 4)] + [fake_row("handled", d, 900) for d in range(4, 9)]
    out = fit_budget(rows, {}, 4000)
    assert out["omitted"]["check"] == 0
    assert out["omitted"]["handled"] >= 1
    assert out["counts"]["check"] == 3


def test_promise_is_read_from_my_last_reply_only():
    conv = one(ping("100", h=6), _reply("101", 5, "will do"), _reply("102", 4, "done, merged"))
    assert status_of(conv, IDENT) == "handled"


def test_latest_skips_a_bot_after_a_human():
    bot = msg("103", "10", OTHER, "beep", hours_ago=3, bot=True)
    conv = one(ping("100", h=6), _reply("101", 5), say("102", OTHER, 4), bot)
    assert render_row(conv, IDENT, ctx())["latest"]["text"] == "text 102"


def test_null_content_in_my_reply_is_handled_safely():
    reply = _reply("101", 5)
    reply["content"] = None
    assert status_of(one(ping("100", h=6), reply), IDENT) == "handled"


def test_cut_check_rows_are_counted_in_omitted():
    rows = [fake_row("check", d, 900) for d in range(1, 9)]
    out = fit_budget(rows, {}, 4000)
    assert out["omitted"]["check"] >= 1
    assert out["omitted"]["check"] + len(out["conversations"]) == 8
