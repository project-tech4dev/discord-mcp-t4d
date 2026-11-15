"""Discord's channel permission calculation, copied from Discord's documented rules.

Order:
  1. The server owner has every permission.
  2. Base = @everyone role permissions OR every role the member holds.
     Administrator in the base grants every permission; overrides cannot remove it.
  3. Channel overrides, in this order, each applying deny before allow:
     a. the @everyone override,
     b. all of the member's role overrides combined (all denies, then all allows),
     c. the member's own override.
  4. Without View Channel, every other permission is implicitly denied.

A channel is readable only when View Channel and Read Message History both
survive. Threads use their parent channel's permissions; a private thread
additionally requires membership.

Anything this module does not recognise raises UnrecognisedCase. Callers must
treat that as "no access", drop the channel and log its ID.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

VIEW_CHANNEL = 1 << 10
READ_MESSAGE_HISTORY = 1 << 16
ADMINISTRATOR = 1 << 3
MANAGE_CHANNELS = 1 << 4
MANAGE_GUILD = 1 << 5
MANAGE_ROLES = 1 << 28
ALL = (1 << 64) - 1

OVERWRITE_ROLE = 0
OVERWRITE_MEMBER = 1
PUBLIC_THREAD_TYPES = frozenset({10, 11})
PRIVATE_THREAD = 12


class UnrecognisedCase(Exception):
    """The calculation met data it does not understand. Treat as no access."""


@dataclass(frozen=True)
class GuildSnapshot:
    guild_id: str  # also the ID of the @everyone role
    owner_id: str
    role_permissions: dict[str, str]  # role ID -> permission bits as Discord sends them (string)


def _bits(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise UnrecognisedCase("permission value is not a number")


def _base_permissions(guild: GuildSnapshot, user_id: str, role_ids: Iterable[str]) -> int:
    if guild.guild_id not in guild.role_permissions:
        raise UnrecognisedCase("@everyone role missing")
    perms = _bits(guild.role_permissions[guild.guild_id])
    for role_id in role_ids:
        if role_id not in guild.role_permissions:
            raise UnrecognisedCase("member holds a role the server does not list")
        perms |= _bits(guild.role_permissions[role_id])
    return perms


def channel_permissions(guild: GuildSnapshot, user_id: str, role_ids: Iterable[str], channel: dict) -> int:
    """Effective permission bits for one member in one (non-thread) channel."""
    role_ids = list(role_ids)
    if user_id == guild.owner_id:
        return ALL

    perms = _base_permissions(guild, user_id, role_ids)
    if perms & ADMINISTRATOR:
        return ALL

    everyone = None
    member = None
    role_allow = 0
    role_deny = 0
    held = set(role_ids)
    for ow in channel.get("permission_overwrites") or []:
        kind = ow.get("type")
        target = ow.get("id")
        allow, deny = _bits(ow.get("allow", "0")), _bits(ow.get("deny", "0"))
        if kind == OVERWRITE_ROLE:
            if target == guild.guild_id:
                everyone = (allow, deny)
            elif target in held:
                role_allow |= allow
                role_deny |= deny
        elif kind == OVERWRITE_MEMBER:
            if target == user_id:
                member = (allow, deny)
        else:
            raise UnrecognisedCase("unknown override type")

    if everyone:
        perms = (perms & ~everyone[1]) | everyone[0]
    perms = (perms & ~role_deny) | role_allow
    if member:
        perms = (perms & ~member[1]) | member[0]

    if not perms & VIEW_CHANNEL:
        return 0
    return perms


def can_read(
    guild: GuildSnapshot,
    user_id: str,
    role_ids: Iterable[str],
    channel: dict,
    *,
    thread: dict | None = None,
    is_thread_member: bool | None = None,
) -> bool:
    """True only if the member can view and read history in `channel` (or `thread` inside it)."""
    if thread is not None:
        ttype = thread.get("type")
        if ttype == PRIVATE_THREAD:
            if is_thread_member is None:
                raise UnrecognisedCase("private thread membership unknown")
        elif ttype not in PUBLIC_THREAD_TYPES:
            raise UnrecognisedCase("unknown thread type")

    perms = channel_permissions(guild, user_id, role_ids, channel)
    readable = perms & VIEW_CHANNEL and perms & READ_MESSAGE_HISTORY
    if not readable:
        return False
    if thread is not None and thread.get("type") == PRIVATE_THREAD:
        return is_thread_member is True
    return True
