"""Discord permission calculation, with fake roles and channel overrides only."""

import pytest

from discord_mcp.permissions import (
    ADMINISTRATOR,
    MANAGE_CHANNELS,
    MANAGE_GUILD,
    MANAGE_ROLES,
    READ_MESSAGE_HISTORY,
    VIEW_CHANNEL,
    GuildSnapshot,
    UnrecognisedCase,
    can_read,
    channel_permissions,
)

GUILD = "900"  # the @everyone role has the guild's ID
OWNER = "1"
ME = "1001"
READ = VIEW_CHANNEL | READ_MESSAGE_HISTORY

ROLE_STAFF = "501"
ROLE_TEAM_A = "502"
ROLE_TEAM_B = "503"
ROLE_ADMINISH = "504"
ROLE_REAL_ADMIN = "505"
ROLE_MUTED = "506"


def guild(everyone: int = READ, **role_perms: int) -> GuildSnapshot:
    roles = {GUILD: str(everyone)}
    names = {
        "staff": ROLE_STAFF,
        "team_a": ROLE_TEAM_A,
        "team_b": ROLE_TEAM_B,
        "adminish": ROLE_ADMINISH,
        "real_admin": ROLE_REAL_ADMIN,
        "muted": ROLE_MUTED,
    }
    for name, perms in role_perms.items():
        roles[names[name]] = str(perms)
    return GuildSnapshot(guild_id=GUILD, owner_id=OWNER, role_permissions=roles)


def ow(target: str, kind: int, allow: int = 0, deny: int = 0) -> dict:
    return {"id": target, "type": kind, "allow": str(allow), "deny": str(deny)}


def chan(*overwrites: dict, ctype: int = 0) -> dict:
    return {"id": "10", "type": ctype, "permission_overwrites": list(overwrites)}


PRIVATE = ow(GUILD, 0, deny=VIEW_CHANNEL)  # @everyone cannot view


# --- owner and Administrator ----------------------------------------------------


def test_owner_sees_channel_that_denies_everyone():
    assert can_read(guild(), OWNER, [], chan(PRIVATE))


def test_administrator_bypasses_channel_deny():
    g = guild(real_admin=ADMINISTRATOR)
    c = chan(PRIVATE, ow(ROLE_REAL_ADMIN, 0, deny=READ))
    assert can_read(g, ME, [ROLE_REAL_ADMIN], c)


def test_administrator_on_everyone_role_bypasses_everything():
    assert can_read(guild(everyone=ADMINISTRATOR), ME, [], chan(PRIVATE, ow(ME, 1, deny=READ)))


def test_admin_role_with_administrator_off_cannot_see_channel_its_overrides_deny():
    # Requested test: a powerful role without the Administrator bit is still bound by overrides.
    g = guild(adminish=MANAGE_GUILD | MANAGE_ROLES | MANAGE_CHANNELS | READ)
    c = chan(PRIVATE, ow(ROLE_ADMINISH, 0, deny=VIEW_CHANNEL))
    assert not can_read(g, ME, [ROLE_ADMINISH], c)


def test_manage_permissions_alone_give_no_read_access():
    g = guild(everyone=0, adminish=MANAGE_GUILD | MANAGE_ROLES | MANAGE_CHANNELS)
    assert not can_read(g, ME, [ROLE_ADMINISH], chan())


# --- override order ---------------------------------------------------------------


def test_plain_member_sees_public_channel():
    assert can_read(guild(), ME, [], chan())


def test_everyone_deny_hides_channel():
    assert not can_read(guild(), ME, [], chan(PRIVATE))


def test_role_allow_beats_everyone_deny():
    c = chan(PRIVATE, ow(ROLE_TEAM_A, 0, allow=READ))
    assert can_read(guild(team_a=0), ME, [ROLE_TEAM_A], c)


def test_role_allow_does_not_help_people_without_the_role():
    c = chan(PRIVATE, ow(ROLE_TEAM_A, 0, allow=READ))
    assert not can_read(guild(team_a=0, team_b=0), ME, [ROLE_TEAM_B], c)


def test_allow_on_one_role_beats_deny_on_another():
    c = chan(ow(ROLE_TEAM_A, 0, deny=VIEW_CHANNEL), ow(ROLE_TEAM_B, 0, allow=VIEW_CHANNEL))
    assert can_read(guild(team_a=0, team_b=0), ME, [ROLE_TEAM_A, ROLE_TEAM_B], c)


def test_role_deny_beats_base_role_permission():
    c = chan(ow(ROLE_MUTED, 0, deny=VIEW_CHANNEL))
    assert not can_read(guild(muted=0), ME, [ROLE_MUTED], c)


def test_member_deny_beats_role_allow():
    c = chan(PRIVATE, ow(ROLE_TEAM_A, 0, allow=READ), ow(ME, 1, deny=VIEW_CHANNEL))
    assert not can_read(guild(team_a=0), ME, [ROLE_TEAM_A], c)


def test_member_allow_beats_role_deny():
    c = chan(ow(ROLE_MUTED, 0, deny=VIEW_CHANNEL), ow(ME, 1, allow=VIEW_CHANNEL))
    assert can_read(guild(muted=0), ME, [ROLE_MUTED], c)


def test_member_override_for_someone_else_is_ignored():
    c = chan(PRIVATE, ow("2002", 1, allow=READ))
    assert not can_read(guild(), ME, [], c)


def test_view_without_read_history_is_not_readable():
    c = chan(ow(GUILD, 0, deny=READ_MESSAGE_HISTORY))
    assert not can_read(guild(), ME, [], c)
    assert channel_permissions(guild(), ME, [], c) & VIEW_CHANNEL


def test_base_role_permission_grants_view_when_no_overrides():
    assert can_read(guild(everyone=0, staff=READ), ME, [ROLE_STAFF], chan())


def test_losing_view_removes_everything_else():
    c = chan(ow(GUILD, 0, deny=VIEW_CHANNEL))
    assert channel_permissions(guild(), ME, [], c) == 0


# --- threads ------------------------------------------------------------------------


def test_public_thread_follows_parent():
    parent = chan(PRIVATE, ow(ROLE_TEAM_A, 0, allow=READ))
    g = guild(team_a=0)
    assert can_read(g, ME, [ROLE_TEAM_A], parent, thread={"type": 11})
    assert not can_read(g, ME, [], parent, thread={"type": 11})


def test_private_thread_needs_membership():
    parent = chan()
    assert can_read(guild(), ME, [], parent, thread={"type": 12}, is_thread_member=True)
    assert not can_read(guild(), ME, [], parent, thread={"type": 12}, is_thread_member=False)


def test_private_thread_still_needs_parent_access():
    assert not can_read(guild(), ME, [], chan(PRIVATE), thread={"type": 12}, is_thread_member=True)


def test_private_thread_with_unknown_membership_raises():
    with pytest.raises(UnrecognisedCase):
        can_read(guild(), ME, [], chan(), thread={"type": 12}, is_thread_member=None)


# --- unrecognised cases fail loudly so the caller drops the channel -------------------


def test_unknown_override_type_raises():
    with pytest.raises(UnrecognisedCase):
        can_read(guild(), ME, [], chan(ow(GUILD, 7, allow=READ)))


def test_held_role_missing_from_guild_raises():
    with pytest.raises(UnrecognisedCase):
        can_read(guild(), ME, ["999"], chan())


def test_non_numeric_permission_value_raises():
    g = GuildSnapshot(guild_id=GUILD, owner_id=OWNER, role_permissions={GUILD: "lots"})
    with pytest.raises(UnrecognisedCase):
        can_read(g, ME, [], chan())


def test_missing_everyone_role_raises():
    g = GuildSnapshot(guild_id=GUILD, owner_id=OWNER, role_permissions={})
    with pytest.raises(UnrecognisedCase):
        can_read(g, ME, [], chan())


def test_unknown_thread_type_raises():
    with pytest.raises(UnrecognisedCase):
        can_read(guild(), ME, [], chan(), thread={"type": 99})
