from discord_mcp.config import Limits
from discord_mcp.matching import Identity, RenderContext, ping_reason
from discord_mcp.render import render_message
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


# --- direct pings (shared by get_my_pings and get_channel_overview) -------------------

from discord_mcp.matching import channel_ping, is_direct, is_quiet  # noqa: E402


def quiet_ctx(*quiet_ids) -> RenderContext:
    return RenderContext(GUILD_ID, ROLE_NAMES, {}, Limits(), direct_only=frozenset(quiet_ids))


def test_role_and_here_pings_become_group_in_a_quiet_channel():
    role = msg("1", "10", content=f"<@&{ROLE_DEV}> deploy", mention_roles=[ROLE_DEV])
    here = msg("2", "10", content="@here lunch", mention_everyone=True)
    assert channel_ping(role, IDENT, ctx(), quiet=True) == ("group", "dev")
    assert channel_ping(here, IDENT, ctx(), quiet=True) == ("group", None)
    assert channel_ping(role, IDENT, ctx(), quiet=False) == ("role", "dev")
    everyone = msg("3", "10", content="@everyone party", mention_everyone=True)
    assert channel_ping(everyone, IDENT, ctx(), quiet=True) == ("everyone", None)


def test_only_named_reply_and_loud_group_pings_are_direct():
    assert is_direct("mention") and is_direct("reply") and is_direct("role") and is_direct("here")
    assert not is_direct("everyone") and not is_direct("group")


def test_a_thread_is_quiet_when_its_parent_is_direct_only():
    th = channel("11", "plan", 11, parent_id="10")
    assert is_quiet(th, quiet_ctx("10"))
    assert is_quiet(CH, quiet_ctx("10"))
    assert not is_quiet(CH, quiet_ctx("20"))
