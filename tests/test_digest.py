import json
import logging

import pytest

from discord_mcp.config import ChannelEntry, Config, Limits
from discord_mcp.digest import Digest
from discord_mcp.matching import Identity
from tests.fakes import GUILD_ID, ME, NOW, ROLE_DEV, FakeClient, channel, msg, user

IDENT = Identity(user_id=ME, role_ids=frozenset({ROLE_DEV}))


def config(*ids: str, **limits) -> Config:
    return Config(
        allowlist={i: ChannelEntry(id=i, name=None, phase=1) for i in ids},
        deny=frozenset(),
        limits=Limits(**limits),
    )


def make(client: FakeClient, cfg: Config) -> Digest:
    return Digest(client, guild_id=GUILD_ID, config=cfg, clock=lambda: NOW)


def ping_ids(payload: dict) -> list[str]:
    return [p["link"].rsplit("/", 1)[1] for c in payload["conversations"] for p in c["pings"]]


def audit_lines(caplog) -> list[dict]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "discord_mcp.audit"]


@pytest.fixture
def client() -> FakeClient:
    c = FakeClient()
    c.add_channel(channel("1", "Team", 4), [])
    c.add_channel(
        channel("10", "general", parent_id="1"),
        [
            msg("100", "10", content=f"<@{ME}> please review", mentions=[user(ME, "Me")], hours_ago=30),
            msg("101", "10", content="sure", hours_ago=29),
        ],
    )
    c.add_channel(channel("20", "secret"), [msg("200", "20", mentions=[user(ME)], hours_ago=2)])
    return c


async def test_stage1_acceptance_test_message_appears_with_right_reason(client):
    result = await make(client, config("10")).get_my_pings(IDENT)
    payload = result.payload
    convs = payload["conversations"]
    assert len(convs) == 1
    assert convs[0]["status"] == "needs_you"
    assert convs[0]["pings"][0]["reason"] == "mention"
    assert convs[0]["pings"][0]["text"] == "@Me please review"
    assert ping_ids(payload) == ["100"]
    assert payload["counts"]["needs_you"] == 1
    assert payload["truncated"] is False
    assert result.channel_ids == ["10"]


async def test_payload_has_no_follow_up_key(client):
    result = await make(client, config("10")).get_my_pings(IDENT)
    assert "follow_up" not in json.dumps(result.payload)
    assert "pings" not in result.payload


async def test_channel_off_allowlist_is_never_read(client):
    result = await make(client, config("10")).get_my_pings(IDENT, channel_ids=["20"])
    assert result.payload["conversations"] == []
    assert "messages:20" not in client.calls


async def test_window_excludes_older_messages(client):
    result = await make(client, config("10")).get_my_pings(IDENT, days=1)
    assert result.payload["conversations"] == []


async def test_days_above_maximum_is_rejected(client):
    with pytest.raises(ValueError):
        await make(client, config("10")).get_my_pings(IDENT, days=15)


async def test_bot_403_drops_channel_and_logs_only_its_id(client, caplog):
    caplog.set_level(logging.INFO, logger="discord_mcp.audit")
    client.fail["20"] = 403
    result = await make(client, config("10", "20")).get_my_pings(IDENT)
    assert ping_ids(result.payload) == ["100"]
    assert result.payload["skipped_channels"] == 1
    lines = audit_lines(caplog)
    assert lines == [
        {
            "user_id": ME,
            "tool": "get_my_pings",
            "channel_ids": ["20"],
            "item_count": 0,
            "duration_ms": 0,
            "status": "dropped:http_403",
        }
    ]


async def test_allowlisted_channel_missing_from_guild_is_dropped(client, caplog):
    caplog.set_level(logging.INFO, logger="discord_mcp.audit")
    result = await make(client, config("10", "99")).get_my_pings(IDENT)
    assert result.payload["skipped_channels"] == 1
    assert audit_lines(caplog)[0]["channel_ids"] == ["99"]
    assert audit_lines(caplog)[0]["status"] == "dropped:not_found"


async def test_unrecognised_channel_type_is_dropped(client, caplog):
    caplog.set_level(logging.INFO, logger="discord_mcp.audit")
    client.add_channel(channel("30", "voice", 2), [])
    result = await make(client, config("10", "30")).get_my_pings(IDENT)
    assert result.payload["skipped_channels"] == 1
    assert audit_lines(caplog)[0]["status"] == "dropped:unknown_type"


async def test_public_threads_of_allowlisted_channel_are_read(client):
    t = channel("40", "deploy", 11, parent_id="10", thread_metadata={"archived": False})
    client.add_channel(t, [msg("400", "40", mention_roles=[ROLE_DEV], hours_ago=3)])
    client.active_threads = [t, channel("41", "other", 11, parent_id="20")]
    client.add_channel(client.active_threads[1], [msg("410", "41", mentions=[user(ME)], hours_ago=1)])
    result = await make(client, config("10")).get_my_pings(IDENT)
    ids = ping_ids(result.payload)
    assert ids == ["400", "100"]
    assert result.payload["conversations"][0]["where"] == "general › deploy"
    assert "messages:41" not in client.calls


async def test_archived_public_threads_are_read(client):
    t = channel("42", "old", 11, parent_id="10", thread_metadata={"archived": True})
    client.add_channel(t, [msg("420", "42", mentions=[user(ME)], hours_ago=10)])
    client.archived_threads["10"] = [t]
    result = await make(client, config("10")).get_my_pings(IDENT)
    assert "420" in ping_ids(result.payload)


async def test_private_thread_needs_membership(client, caplog):
    caplog.set_level(logging.INFO, logger="discord_mcp.audit")
    t = channel("43", "hush", 12, parent_id="10")
    client.add_channel(t, [msg("430", "43", mentions=[user(ME)], hours_ago=1)])
    client.active_threads = [t]
    result = await make(client, config("10")).get_my_pings(IDENT)
    assert "430" not in ping_ids(result.payload)
    assert "messages:43" not in client.calls
    assert audit_lines(caplog)[0]["channel_ids"] == ["43"]
    assert audit_lines(caplog)[0]["status"] == "dropped:no_access"

    client.thread_members["43"] = {ME}
    result = await make(client, config("10")).get_my_pings(IDENT)
    assert "430" in ping_ids(result.payload)


async def test_list_my_channels_returns_id_name_category(client):
    result = await make(client, config("10", "20")).list_my_channels(IDENT)
    assert result.payload["channels"] == [
        {"id": "10", "name": "general", "category": "Team"},
        {"id": "20", "name": "secret", "category": None},
    ]


async def test_get_channel_messages_newest_first_and_limited(client):
    result = await make(client, config("10")).get_channel_messages(IDENT, "10", days=3, limit=1)
    assert [m["id"] for m in result.payload["messages"]] == ["101"]
    assert result.payload["truncated"] is True


async def test_get_channel_messages_refuses_channel_off_allowlist(client):
    with pytest.raises(PermissionError):
        await make(client, config("10")).get_channel_messages(IDENT, "20")
    assert "messages:20" not in client.calls


async def test_get_channel_messages_limit_above_maximum_is_rejected(client):
    with pytest.raises(ValueError):
        await make(client, config("10")).get_channel_messages(IDENT, "10", limit=301)


async def test_resolve_identity_reads_member_roles(client):
    ident = await make(client, config()).resolve_identity(ME)
    assert ident == Identity(user_id=ME, role_ids=frozenset({ROLE_DEV}))


async def test_fifteen_ping_fixture_is_small():
    """Modelled on the first live test: 8 pings in one channel that I answered, 4 threads, 2 unanswered."""
    from tests.fakes import OTHER, THIRD

    c = FakeClient()
    grants = []
    for i in range(8):
        who = OTHER if i % 2 else THIRD
        grants.append(msg(str(1000 + 2 * i), "60", who, f"<@{ME}> point {i} " + "x" * 300, mentions=[user(ME)], hours_ago=100 - 8 * i))
        grants.append(msg(str(1001 + 2 * i), "60", ME, f"answer {i} " + "y" * 300, hours_ago=99 - 8 * i))
    c.add_channel(channel("60", "grants"), grants)
    threads = []
    for n, answered in [("61", True), ("62", True), ("63", False), ("64", False)]:
        t = channel(n, f"topic {n}", 11, parent_id="60", thread_metadata={"archived": False})
        body = [msg(f"{n}0", n, OTHER, f"<@{ME}> look " + "z" * 300, mentions=[user(ME)], hours_ago=20)]
        body += [msg(f"{n}{k}", n, THIRD, "chat " + "w" * 300, hours_ago=19 - k) for k in range(1, 6)]
        if answered:
            body.append(msg(f"{n}9", n, ME, "done", hours_ago=10))
        c.add_channel(t, body)
        threads.append(t)
    c.active_threads = threads
    result = await make(c, config("60")).get_my_pings(IDENT, days=14)
    payload = result.payload
    assert payload["counts"]["needs_you"] == 2
    assert sorted(r["where"] for r in payload["conversations"] if r["status"] == "needs_you") == [
        "grants › topic 63",
        "grants › topic 64",
    ]
    assert len(json.dumps(payload)) < 20000


# --- get_conversation -----------------------------------------------------------

from discord_mcp.digest import AccessDenied, InputError  # noqa: E402

LINK = f"https://discord.com/channels/{GUILD_ID}"


def conv_ids(result) -> list[str]:
    return [m["link"].rsplit("/", 1)[1] for m in result.payload["messages"]]


async def test_get_conversation_by_ping_link_returns_all_its_messages(client):
    result = await make(client, config("10")).get_conversation(IDENT, f"{LINK}/10/100")
    assert conv_ids(result) == ["100", "101"]
    assert result.payload["where"] == "general"
    assert result.payload["truncated"] is False
    assert result.channel_ids == ["10"]


async def test_get_conversation_by_id_matches_link(client):
    d = make(client, config("10"))
    row = (await d.get_my_pings(IDENT)).payload["conversations"][0]
    by_id = await d.get_conversation(IDENT, row["id"])
    assert conv_ids(by_id) == ["100", "101"]


async def test_get_conversation_by_my_reply_link_finds_same_conversation(client):
    client.add_channel(
        channel("10", "general", parent_id="1"),
        [*client.messages["10"], msg("102", "10", ME, "done", hours_ago=28)],
    )
    result = await make(client, config("10")).get_conversation(IDENT, f"{LINK}/10/102")
    assert conv_ids(result) == ["100", "101", "102"]


async def test_link_to_channel_off_allowlist_is_denied_without_fetch(client):
    with pytest.raises(AccessDenied):
        await make(client, config("10")).get_conversation(IDENT, f"{LINK}/20/200")
    assert "messages:20" not in client.calls


async def test_link_to_private_thread_not_a_member_is_denied(client):
    t = channel("43", "hush", 12, parent_id="10")
    client.add_channel(t, [msg("430", "43", mentions=[user(ME)], hours_ago=1)])
    client.active_threads = [t]
    with pytest.raises(AccessDenied):
        await make(client, config("10")).get_conversation(IDENT, f"{LINK}/43/430")
    assert "messages:43" not in client.calls


async def test_link_from_other_guild_is_input_error(client):
    with pytest.raises(InputError):
        await make(client, config("10")).get_conversation(IDENT, "https://discord.com/channels/999/10/100")


@pytest.mark.parametrize("bad", ["hello", f"{LINK}/10", "10/abc", ""])
async def test_malformed_link_is_input_error(client, bad):
    with pytest.raises(InputError):
        await make(client, config("10")).get_conversation(IDENT, bad)


async def test_message_not_in_any_conversation_is_input_error(client):
    from tests.fakes import THIRD

    client.add_channel(
        channel("10", "general", parent_id="1"),
        [*client.messages["10"], msg("105", "10", THIRD, "unrelated", hours_ago=2), msg("90", "10", hours_ago=400)],
    )
    d = make(client, config("10"))
    for mid in ["105", "90"]:
        with pytest.raises(InputError):
            await d.get_conversation(IDENT, f"{LINK}/10/{mid}")
