"""Decides which messages ping a person, and the request types shared by every view.

Rendering functions live in render.py.

Matching uses Discord's own fields (mentions, mention_roles, mention_everyone,
referenced_message), never the raw text, so a typed "@everyone" that Discord
did not deliver as a ping does not count.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from discord_mcp.config import Limits
from discord_mcp.messages import home_id

USER_MENTION = re.compile(r"<@!?(\d+)>")
# Ping reasons not asked of the person directly: @everyone anywhere, and @here or a role
# ping ("group") in a direct_only channel.
BROADCASTS = frozenset({"everyone", "group"})


@dataclass(frozen=True)
class Identity:
    user_id: str
    role_ids: frozenset[str]


@dataclass(frozen=True)
class RenderContext:
    guild_id: str
    role_names: dict[str, str]
    channel_names: dict[str, str]
    limits: Limits = field(default_factory=Limits)
    # Channels (and their threads) where @here and role pings count as announcements.
    direct_only: frozenset[str] = frozenset()


@dataclass
class ChannelMessages:
    channel: dict
    label: str
    messages: list[dict]  # oldest first


def ping_reason(message: dict, identity: Identity, role_names: dict[str, str]) -> tuple[str, str | None] | None:
    """Return (reason, role_name) if the message pings the person, else None."""
    if message.get("author", {}).get("id") == identity.user_id:
        return None
    named_in_text = any(m[1] == identity.user_id for m in USER_MENTION.finditer(message.get("content", "")))
    ref = message.get("referenced_message")
    is_reply_to_me = bool(ref) and ref.get("author", {}).get("id") == identity.user_id
    if named_in_text:
        return ("mention", None)
    if is_reply_to_me:
        # A reply with "ping on" also lists the person in `mentions`; it is still a reply.
        return ("reply", None)
    if any(u.get("id") == identity.user_id for u in message.get("mentions", [])):
        return ("mention", None)
    for role_id in message.get("mention_roles", []):
        if role_id in identity.role_ids:
            return ("role", role_names.get(role_id, role_id))
    if message.get("mention_everyone"):
        content = message.get("content", "")
        if "@everyone" not in content and "@here" in content:
            return ("here", None)
        return ("everyone", None)
    return None


def is_quiet(channel: dict, ctx: RenderContext) -> bool:
    """A direct_only channel, or a thread inside one: @here and role pings there are announcements."""
    return home_id(channel) in ctx.direct_only or channel["id"] in ctx.direct_only


def channel_ping(
    message: dict, identity: Identity, ctx: RenderContext, *, quiet: bool
) -> tuple[str, str | None] | None:
    """ping_reason, with @here and role pings downgraded to "group" in a quiet channel."""
    hit = ping_reason(message, identity, ctx.role_names)
    if hit is not None and quiet and hit[0] in ("role", "here"):
        return ("group", hit[1])
    return hit


def is_direct(reason: str) -> bool:
    """Asked of the person, not broadcast to everyone (get_my_pings and only_mine agree on this)."""
    return reason not in BROADCASTS
