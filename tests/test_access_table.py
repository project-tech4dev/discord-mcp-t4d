"""The PRD's access test table, run against a fake Discord server.

Server layout:
  #general      everyone can read
  #team-a       only role team-a
  #team-b       only role team-b
  #cross        only role cross (a limited cross-team channel)
  #leadership   only role leaders; the "adminish" role has Manage perms but no Administrator
"""

import io
import json
import logging

import pytest

from discord_mcp import audit
from discord_mcp.config import ChannelEntry, Config, Limits
from discord_mcp.digest import Digest
from discord_mcp.discord_client import DiscordError
from discord_mcp.errors import NotAMember
from discord_mcp.permissions import MANAGE_CHANNELS, MANAGE_GUILD, MANAGE_ROLES, VIEW_CHANNEL
from tests.fakes import GUILD_ID, NOW, OWNER, READ, FakeClient, channel, msg, user

ALICE = "2001"  # team-a, cross
BOB = "2002"  # team-b
CARL = "2003"  # adminish (no Administrator)
POSTER = "2999"

R_TEAM_A, R_TEAM_B, R_CROSS, R_LEADERS, R_ADMINISH = "601", "602", "603", "604", "605"
CHANNELS = {"general": "10", "team-a": "11", "team-b": "12", "cross": "13", "leadership": "14"}
SENTINEL = "PRIVATE-MESSAGE-TEXT-42"


def private_to(role_id: str) -> list[dict]:
    return [
        {"id": GUILD_ID, "type": 0, "allow": "0", "deny": str(VIEW_CHANNEL)},
        {"id": role_id, "type": 0, "allow": str(READ), "deny": "0"},
    ]


@pytest.fixture
def server() -> FakeClient:
    c = FakeClient()
    c.roles = [
        {"id": GUILD_ID, "name": "@everyone", "permissions": str(READ)},
        {"id": R_TEAM_A, "name": "team-a", "permissions": "0"},
        {"id": R_TEAM_B, "name": "team-b", "permissions": "0"},
        {"id": R_CROSS, "name": "cross", "permissions": "0"},
        {"id": R_LEADERS, "name": "leaders", "permissions": "0"},
        {"id": R_ADMINISH, "name": "adminish", "permissions": str(MANAGE_GUILD | MANAGE_ROLES | MANAGE_CHANNELS)},
    ]
    c.members = {
        ALICE: {"user": user(ALICE), "roles": [R_TEAM_A, R_CROSS]},
        BOB: {"user": user(BOB), "roles": [R_TEAM_B]},
        CARL: {"user": user(CARL), "roles": [R_ADMINISH]},
        OWNER: {"user": user(OWNER), "roles": []},
    }
    overwrites = {
        "general": [],
        "team-a": private_to(R_TEAM_A),
        "team-b": private_to(R_TEAM_B),
        "cross": private_to(R_CROSS),
        "leadership": private_to(R_LEADERS),
    }
    everyone_ping = 100
    for name, cid in CHANNELS.items():
        c.add_channel(
            channel(cid, name, permission_overwrites=overwrites[name]),
            [msg(str(everyone_ping + int(cid)), cid, POSTER, f"@everyone {SENTINEL}", mention_everyone=True, hours_ago=3)],
        )
    return c


def digest(server: FakeClient, deny: frozenset = frozenset()) -> Digest:
    cfg = Config(
        allowlist={cid: ChannelEntry(cid, name, 1) for name, cid in CHANNELS.items()},
        deny=deny,
        limits=Limits(),
    )
    return Digest(server, guild_id=GUILD_ID, config=cfg, clock=lambda: NOW)



def all_pings(payload: dict) -> list[dict]:
    """Every ping shown in the conversations, with its id and where it was."""
    return [
        p | {"id": p["link"].rsplit("/", 1)[1], "channel": c["where"]}
        for c in payload["conversations"]
        for p in c["pings"]
    ]

async def channels_with_pings(server: FakeClient, user_id: str, deny: frozenset = frozenset()) -> set[str]:
    d = digest(server, deny)
    ident = await d.resolve_identity(user_id)
    result = await d.get_my_pings(ident)
    return {p["channel"] for p in all_pings(result.payload)}


async def test_member_of_common_channel_receives_items(server):
    assert "general" in await channels_with_pings(server, BOB)


async def test_internal_team_member_sees_own_team_only(server):
    assert await channels_with_pings(server, BOB) == {"general", "team-b"}


async def test_cross_team_channel_only_while_a_member(server):
    assert "cross" in await channels_with_pings(server, ALICE)
    server.members[ALICE]["roles"] = [R_TEAM_A]
    assert "cross" not in await channels_with_pings(server, ALICE)


async def test_no_access_means_zero_items_and_no_fetch(server):
    await channels_with_pings(server, BOB)
    assert "messages:14" not in server.calls
    assert "messages:11" not in server.calls


async def test_admin_role_with_administrator_off_gets_nothing_from_closed_channel(server):
    assert await channels_with_pings(server, CARL) == {"general"}


async def test_server_owner_receives_every_allowlisted_channel(server):
    assert await channels_with_pings(server, OWNER) == set(CHANNELS)


async def test_role_ping_has_role_reason(server):
    server.messages["11"].insert(0, msg("500", "11", POSTER, f"<@&{R_TEAM_A}> review", mention_roles=[R_TEAM_A], hours_ago=1))
    d = digest(server)
    result = await d.get_my_pings(await d.resolve_identity(ALICE))
    role_pings = [p for p in all_pings(result.payload) if p["reason"] == "role"]
    assert [(p["id"], p["role"]) for p in role_pings] == [("500", "team-a")]


async def test_reply_has_reply_reason(server):
    mine = msg("600", "10", BOB, "question", hours_ago=2)
    reply = msg("601", "10", POSTER, "answer", referenced=mine, mentions=[user(BOB)], hours_ago=1)
    server.messages["10"] = [reply, mine] + server.messages["10"]
    d = digest(server)
    result = await d.get_my_pings(await d.resolve_identity(BOB))
    assert ("601", "reply") in [(p["id"], p["reason"]) for p in all_pings(result.payload)]


async def test_bot_losing_access_drops_channel_and_logs_its_id(server, caplog):
    caplog.set_level(logging.INFO, logger="discord_mcp.audit")
    server.fail["12"] = 403
    assert await channels_with_pings(server, BOB) == {"general"}
    lines = [json.loads(r.getMessage()) for r in caplog.records if r.name == "discord_mcp.audit"]
    failures = [(d["channel_ids"], d["status"]) for d in lines if d["status"] != "dropped:no_access"]
    assert failures == [(["12"], "dropped:http_403")]


async def test_deny_list_blocks_one_person_from_one_channel(server):
    deny = frozenset({(BOB, "12")})
    assert await channels_with_pings(server, BOB, deny) == {"general"}
    assert await channels_with_pings(server, OWNER, deny) == set(CHANNELS)


async def test_guild_lookup_failure_fails_the_call(server):
    # Issue #6: an empty result would read as "nothing pings you"; fail like allow-all mode does.
    server.fail_guild = 500
    with pytest.raises(DiscordError):
        await channels_with_pings(server, BOB)


async def test_unrecognised_role_drops_channel(server, caplog):
    caplog.set_level(logging.INFO, logger="discord_mcp.audit")
    server.members[BOB]["roles"] = [R_TEAM_B, "999"]  # role missing from the server's list
    assert await channels_with_pings(server, BOB) == set()


async def test_person_who_left_the_server_gets_nothing(server):
    with pytest.raises(NotAMember):
        await digest(server).resolve_identity("7777")


async def test_get_channel_messages_refuses_channel_person_cannot_see(server):
    d = digest(server)
    with pytest.raises(PermissionError):
        await d.get_channel_messages(await d.resolve_identity(BOB), "11")
    assert "messages:11" not in server.calls


async def test_log_sweep_finds_no_message_text(server):
    stream = io.StringIO()
    restore = audit.configure_logging(stream)
    try:
        for who in (ALICE, BOB, CARL, OWNER):
            d = digest(server)
            ident = await d.resolve_identity(who)
            await d.get_my_pings(ident)
            await d.list_my_channels(ident)
    finally:
        restore()
    assert SENTINEL not in stream.getvalue()


async def test_channel_mentions_name_only_channels_the_person_can_see(server):
    server.messages["10"].insert(
        0, msg("700", "10", POSTER, "see <#14> and <#10>", mentions=[], mention_everyone=True, hours_ago=1)
    )

    async def text_for(user_id):
        d = digest(server)
        result = await d.get_my_pings(await d.resolve_identity(user_id))
        return next(p["text"] for p in all_pings(result.payload) if p["id"] == "700")

    assert await text_for(BOB) == "see <#14> and #general"
    assert await text_for(OWNER) == "see #leadership and #general"
