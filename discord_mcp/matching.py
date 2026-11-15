"""Pure functions that decide which messages ping a person and shape them for Claude.

Matching uses Discord's own fields (mentions, mention_roles, mention_everyone,
referenced_message), never the raw text, so a typed "@everyone" that Discord
did not deliver as a ping does not count.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from discord_mcp.config import Limits

# Normal messages and replies. Everything else (joins, pins, thread starters) is skipped.
CONTENT_TYPES = frozenset({0, 19})

_USER = re.compile(r"<@!?(\d+)>")
_ROLE = re.compile(r"<@&(\d+)>")
_CHANNEL = re.compile(r"<#(\d+)>")


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


@dataclass
class ChannelMessages:
    channel: dict
    label: str
    messages: list[dict]  # oldest first


def ping_reason(message: dict, identity: Identity, role_names: dict[str, str]) -> tuple[str, str | None] | None:
    """Return (reason, role_name) if the message pings the person, else None."""
    if message.get("author", {}).get("id") == identity.user_id:
        return None
    named_in_text = any(m[1] == identity.user_id for m in _USER.finditer(message.get("content", "")))
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


def _display_name(u: dict) -> str:
    return (u.get("member") or {}).get("nick") or u.get("global_name") or u.get("username") or "unknown"


def _replace_mentions(text: str, message: dict, ctx: RenderContext) -> str:
    users = {u["id"]: _display_name(u) for u in message.get("mentions", []) if "id" in u}
    text = _USER.sub(lambda m: f"@{users[m[1]]}" if m[1] in users else m[0], text)
    text = _ROLE.sub(lambda m: f"@{ctx.role_names[m[1]]}" if m[1] in ctx.role_names else m[0], text)
    return _CHANNEL.sub(lambda m: f"#{ctx.channel_names[m[1]]}" if m[1] in ctx.channel_names else m[0], text)


def render_message(message: dict, label: str, ctx: RenderContext, *, text_chars: int | None = None) -> dict:
    limit = text_chars if text_chars is not None else ctx.limits.text_chars
    author = message.get("author", {})
    out = {
        "id": message["id"],
        "time": message["timestamp"],
        "channel": label,
        "author": _display_name(author),
        "is_bot": bool(author.get("bot")),
        "text": _replace_mentions(message.get("content", ""), message, ctx)[:limit],
        "link": f"https://discord.com/channels/{ctx.guild_id}/{message['channel_id']}/{message['id']}",
    }
    ref = message.get("referenced_message")
    if ref:
        out["reply_to"] = _replace_mentions(ref.get("content", ""), ref, ctx)[: ctx.limits.reply_chars]
    return out
