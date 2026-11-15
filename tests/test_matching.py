from discord_mcp.config import Limits
from discord_mcp.matching import (
    ChannelMessages,
    Identity,
    RenderContext,
    find_pings,
    ping_reason,
    render_message,
)
from tests.fakes import GUILD_ID, ME, OTHER, ROLE_DEV, ROLE_OPS, channel, msg, user

ROLE_NAMES = {ROLE_DEV: "dev", ROLE_OPS: "ops"}
IDENT = Identity(user_id=ME, role_ids=frozenset({ROLE_DEV}))
CH = channel("10", "general")


def ctx(**limits) -> RenderContext:
    return RenderContext(
        guild_id=GUILD_ID,
        role_names=ROLE_NAMES,
        channel_names={"10": "general"},
        limits=Limits(**limits),
    )


def reason(m):
    return ping_reason(m, IDENT, ROLE_NAMES)


# --- ping reasons -------------------------------------------------------------


def test_user_mention_is_a_ping():
    m = msg("1", "10", content=f"<@{ME}> can you check", mentions=[user(ME)])
    assert reason(m) == ("mention", None)


def test_role_the_person_holds_is_a_ping_with_role_name():
    m = msg("1", "10", content=f"<@&{ROLE_DEV}> deploy", mention_roles=[ROLE_DEV])
    assert reason(m) == ("role", "dev")


def test_role_the_person_does_not_hold_is_not_a_ping():
    m = msg("1", "10", content=f"<@&{ROLE_OPS}> deploy", mention_roles=[ROLE_OPS])
    assert reason(m) is None


def test_everyone_is_a_ping():
    m = msg("1", "10", content="@everyone standup moved", mention_everyone=True)
    assert reason(m) == ("everyone", None)


def test_here_is_a_ping():
    m = msg("1", "10", content="@here quick one", mention_everyone=True)
    assert reason(m) == ("here", None)


def test_everyone_text_without_discord_flag_is_not_a_ping():
    # Discord only sets the flag when the author was allowed to ping everyone.
    m = msg("1", "10", content="@everyone hi", mention_everyone=False)
    assert reason(m) is None


def test_reply_to_the_person_is_a_ping():
    mine = msg("1", "10", author_id=ME, content="my question")
    reply = msg("2", "10", content="answer", referenced=mine)
    assert reason(reply) == ("reply", None)


def test_reply_with_ping_on_is_reply_not_mention():
    # Discord adds the replied-to author to `mentions` when the reply pings them.
    mine = msg("1", "10", author_id=ME, content="my question")
    reply = msg("2", "10", content="answer", referenced=mine, mentions=[user(ME)])
    assert reason(reply) == ("reply", None)


def test_reply_that_also_names_the_person_is_mention():
    mine = msg("1", "10", author_id=ME)
    reply = msg("2", "10", content=f"<@!{ME}> see this", referenced=mine, mentions=[user(ME)])
    assert reason(reply) == ("mention", None)


def test_reply_to_someone_else_is_not_a_ping():
    theirs = msg("1", "10", author_id=OTHER, content="their question")
    reply = msg("2", "10", content="answer", referenced=theirs)
    assert reason(reply) is None


def test_reply_whose_original_was_deleted_is_not_a_ping():
    reply = msg("2", "10", content="answer", mtype=19)
    reply["message_reference"] = {"message_id": "1"}
    reply["referenced_message"] = None
    assert reason(reply) is None


def test_own_message_is_never_a_ping():
    m = msg("1", "10", author_id=ME, content=f"<@{ME}> note to self", mentions=[user(ME)], mention_everyone=True)
    assert reason(m) is None


def test_direct_mention_wins_over_role_and_everyone():
    m = msg("1", "10", mentions=[user(ME)], mention_roles=[ROLE_DEV], mention_everyone=True)
    assert reason(m) == ("mention", None)


def test_reply_wins_over_role():
    mine = msg("1", "10", author_id=ME)
    m = msg("2", "10", referenced=mine, mention_roles=[ROLE_DEV])
    assert reason(m) == ("reply", None)


def test_plain_message_is_not_a_ping():
    assert reason(msg("1", "10", content="lunch?")) is None


# --- rendering ----------------------------------------------------------------


def test_render_has_required_fields_and_link():
    m = msg("77", "10", content="hi")
    out = render_message(m, "general", ctx())
    assert out["id"] == "77"
    assert out["channel"] == "general"
    assert out["author"] == f"user{OTHER}"
    assert out["text"] == "hi"
    assert out["time"] == m["timestamp"]
    assert out["link"] == f"https://discord.com/channels/{GUILD_ID}/10/77"
    assert out["is_bot"] is False
    assert "reply_to" not in out


def test_render_marks_bot_messages():
    out = render_message(msg("1", "10", bot=True), "general", ctx())
    assert out["is_bot"] is True


def test_render_replaces_mention_codes_with_names():
    m = msg(
        "1",
        "10",
        content=f"<@{ME}> and <@!{OTHER}> ask <@&{ROLE_DEV}> in <#10>, unknown <@&999>",
        mentions=[user(ME, "Asha"), user(OTHER, "Ravi", nick="Ravi K")],
    )
    out = render_message(m, "general", ctx())
    assert out["text"] == "@Asha and @Ravi K ask @dev in #general, unknown <@&999>"


def test_render_cuts_text_to_limit():
    out = render_message(msg("1", "10", content="x" * 2000), "general", ctx())
    assert len(out["text"]) == 1500


def test_render_reply_carries_first_200_chars_of_original():
    orig = msg("1", "10", author_id=ME, content="q" * 500)
    out = render_message(msg("2", "10", referenced=orig), "general", ctx())
    assert out["reply_to"] == "q" * 200


# --- find_pings -----------------------------------------------------------------


def chronological(*messages):
    return sorted(messages, key=lambda m: int(m["id"]))


def test_finds_pings_newest_first_with_reason():
    a = msg("1", "10", mentions=[user(ME)], hours_ago=5)
    b = msg("2", "10", mention_roles=[ROLE_DEV], hours_ago=3)
    c = msg("3", "10", content="noise", hours_ago=2)
    result = find_pings([ChannelMessages(CH, "general", chronological(a, b, c))], IDENT, ctx())
    assert [p["id"] for p in result.pings] == ["2", "1"]
    assert result.pings[0]["reason"] == "role" and result.pings[0]["role"] == "dev"
    assert result.pings[1]["reason"] == "mention" and "role" not in result.pings[1]
    assert result.truncated is False


def test_follow_up_is_next_ten_channel_messages_including_own():
    ping = msg("100", "10", mentions=[user(ME)], hours_ago=20)
    after = [msg(str(101 + i), "10", hours_ago=19 - i) for i in range(12)]
    after[0]["author"] = user(ME)
    result = find_pings([ChannelMessages(CH, "general", chronological(ping, *after))], IDENT, ctx())
    assert len(result.pings) == 1
    follow = result.pings[0]["follow_up"]
    assert [f["id"] for f in follow] == [str(101 + i) for i in range(10)]
    assert follow[0]["author"] == f"user{ME}"


def test_follow_up_text_uses_shorter_limit():
    ping = msg("100", "10", mentions=[user(ME)], hours_ago=2)
    after = msg("101", "10", content="y" * 1000, hours_ago=1)
    result = find_pings([ChannelMessages(CH, "general", chronological(ping, after))], IDENT, ctx())
    assert len(result.pings[0]["follow_up"][0]["text"]) == 300


def test_follow_up_comes_from_thread_started_on_the_ping():
    ping = msg("100", "10", mentions=[user(ME)], hours_ago=5, thread={"id": "100"})
    channel_noise = msg("101", "10", content="unrelated", hours_ago=4)
    thread = channel("100", "deploy-thread", 11, parent_id="10")
    t1 = msg("200", "100", content="on it", hours_ago=3)
    t2 = msg("201", "100", content="done", hours_ago=2)
    result = find_pings(
        [
            ChannelMessages(CH, "general", chronological(ping, channel_noise)),
            ChannelMessages(thread, "general › deploy-thread", chronological(t1, t2)),
        ],
        IDENT,
        ctx(),
    )
    assert [f["id"] for f in result.pings[0]["follow_up"]] == ["200", "201"]


def test_ping_inside_thread_follows_up_within_thread():
    thread = channel("100", "deploy-thread", 11, parent_id="10")
    p = msg("200", "100", mentions=[user(ME)], hours_ago=3)
    t2 = msg("201", "100", hours_ago=2)
    result = find_pings([ChannelMessages(thread, "general › deploy-thread", chronological(p, t2))], IDENT, ctx())
    assert result.pings[0]["channel"] == "general › deploy-thread"
    assert result.pings[0]["link"].endswith("/100/200")
    assert [f["id"] for f in result.pings[0]["follow_up"]] == ["201"]


def test_system_messages_are_ignored():
    join = msg("1", "10", mention_everyone=True, mtype=7)
    result = find_pings([ChannelMessages(CH, "general", [join])], IDENT, ctx())
    assert result.pings == []


def test_cap_cuts_list_and_sets_flag():
    msgs = [msg(str(i), "10", mentions=[user(ME)], hours_ago=100 - i) for i in range(1, 6)]
    result = find_pings([ChannelMessages(CH, "general", msgs)], IDENT, ctx(), max_items=3)
    assert [p["id"] for p in result.pings] == ["5", "4", "3"]
    assert result.truncated is True
