import json
import logging

import pytest

from discord_mcp.config import ChannelEntry, Config, Limits
from discord_mcp.digest import DevOwnerAccess, Digest
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
    return Digest(client, guild_id=GUILD_ID, config=cfg, access=DevOwnerAccess(), clock=lambda: NOW)


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
    pings = result.payload["pings"]
    assert len(pings) == 1
    assert pings[0]["id"] == "100"
    assert pings[0]["reason"] == "mention"
    assert pings[0]["text"] == "@Me please review"
    assert pings[0]["follow_up"][0]["id"] == "101"
    assert result.payload["truncated"] is False
    assert result.channel_ids == ["10"]


async def test_channel_off_allowlist_is_never_read(client):
    result = await make(client, config("10")).get_my_pings(IDENT, channel_ids=["20"])
    assert result.payload["pings"] == []
    assert "messages:20" not in client.calls


async def test_window_excludes_older_messages(client):
    result = await make(client, config("10")).get_my_pings(IDENT, days=1)
    assert result.payload["pings"] == []


async def test_days_above_maximum_is_rejected(client):
    with pytest.raises(ValueError):
        await make(client, config("10")).get_my_pings(IDENT, days=15)


async def test_bot_403_drops_channel_and_logs_only_its_id(client, caplog):
    caplog.set_level(logging.INFO, logger="discord_mcp.audit")
    client.fail["20"] = 403
    result = await make(client, config("10", "20")).get_my_pings(IDENT)
    assert [p["id"] for p in result.payload["pings"]] == ["100"]
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
    ids = [p["id"] for p in result.payload["pings"]]
    assert ids == ["400", "100"]
    assert result.payload["pings"][0]["channel"] == "general › deploy"
    assert "messages:41" not in client.calls


async def test_archived_public_threads_are_read(client):
    t = channel("42", "old", 11, parent_id="10", thread_metadata={"archived": True})
    client.add_channel(t, [msg("420", "42", mentions=[user(ME)], hours_ago=10)])
    client.archived_threads["10"] = [t]
    result = await make(client, config("10")).get_my_pings(IDENT)
    assert "420" in [p["id"] for p in result.payload["pings"]]


async def test_private_threads_are_dropped_until_membership_check_exists(client, caplog):
    caplog.set_level(logging.INFO, logger="discord_mcp.audit")
    t = channel("43", "hush", 12, parent_id="10")
    client.add_channel(t, [msg("430", "43", mentions=[user(ME)], hours_ago=1)])
    client.active_threads = [t]
    result = await make(client, config("10")).get_my_pings(IDENT)
    assert "430" not in [p["id"] for p in result.payload["pings"]]
    assert "messages:43" not in client.calls
    assert audit_lines(caplog)[0] == {
        "user_id": ME,
        "tool": "get_my_pings",
        "channel_ids": ["43"],
        "item_count": 0,
        "duration_ms": 0,
        "status": "dropped:no_access",
    }


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
