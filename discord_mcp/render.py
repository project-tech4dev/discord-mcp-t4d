"""Shapes messages for Claude: display names, readable mentions, and the item dicts every tool returns."""

from __future__ import annotations

import re

from discord_mcp.matching import USER_MENTION, RenderContext
from discord_mcp.messages import message_link

_ROLE = re.compile(r"<@&(\d+)>")
_CHANNEL = re.compile(r"<#(\d+)>")


def display_name(u: dict) -> str:
    return (u.get("member") or {}).get("nick") or u.get("global_name") or u.get("username") or "unknown"


def replace_mentions(text: str, message: dict, ctx: RenderContext) -> str:
    users = {u["id"]: display_name(u) for u in message.get("mentions", []) if "id" in u}
    text = USER_MENTION.sub(lambda m: f"@{users[m[1]]}" if m[1] in users else m[0], text)
    text = _ROLE.sub(lambda m: f"@{ctx.role_names[m[1]]}" if m[1] in ctx.role_names else m[0], text)
    return _CHANNEL.sub(lambda m: f"#{ctx.channel_names[m[1]]}" if m[1] in ctx.channel_names else m[0], text)


def _link(message: dict, ctx: RenderContext) -> str:
    return message_link(ctx.guild_id, message["channel_id"], message["id"])


def _reply_to(message: dict, ctx: RenderContext) -> str | None:
    ref = message.get("referenced_message")
    if not ref:
        return None
    return replace_mentions(ref.get("content", ""), ref, ctx)[: ctx.limits.reply_chars]


def render_message(message: dict, label: str, ctx: RenderContext, *, text_chars: int | None = None) -> dict:
    """One message for get_channel_messages."""
    limit = text_chars if text_chars is not None else ctx.limits.text_chars
    author = message.get("author", {})
    out = {
        "id": message["id"],
        "time": message["timestamp"],
        "channel": label,
        "author": display_name(author),
        "is_bot": bool(author.get("bot")),
        "text": replace_mentions(message.get("content", ""), message, ctx)[:limit],
        "link": _link(message, ctx),
    }
    reply = _reply_to(message, ctx)
    if reply is not None:
        out["reply_to"] = reply
    return out


def brief(message: dict, ctx: RenderContext, chars: int) -> dict:
    """A compact message item: author, time, text cut to `chars`, and its link."""
    author = message.get("author", {})
    text = replace_mentions(message.get("content", ""), message, ctx)
    out = {
        "author": display_name(author),
        "time": message["timestamp"][:16],
        "text": text[:chars],
        "link": _link(message, ctx),
    }
    if len(text) > chars:
        out["text_cut"] = True  # so a URL near the end is never passed on half-written
    if author.get("bot"):
        out["is_bot"] = True
    return out


def render_thread(messages: list[dict], ctx: RenderContext) -> list[dict]:
    """Every given message in full, with what each replies to, for opening one conversation or topic."""
    out = []
    for m in messages:
        item = brief(m, ctx, ctx.limits.text_chars)
        reply = _reply_to(m, ctx)
        if reply is not None:
            item["reply_to"] = reply
        out.append(item)
    return out
