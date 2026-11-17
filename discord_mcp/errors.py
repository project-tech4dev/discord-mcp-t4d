"""Errors a tool call can end with. Each message is fixed text, safe to show to Claude."""

from __future__ import annotations

from collections.abc import Iterable

from discord_mcp.discord_client import DiscordError


class InputError(ValueError):
    """Bad tool input. The message is fixed text and safe to show."""


class AccessDenied(PermissionError):
    """The channel is not permitted by configuration, is denied, or is not visible to the person."""


class NotAMember(PermissionError):
    """The person is not (or no longer) a member of the Discord server."""


class RoleNotAllowed(NotAMember):
    """A member holding none of the roles in `allowed_roles`. Refused wherever a non-member is."""


class RateLimited(PermissionError):
    """The person made more tool calls in the last minute than `calls_per_minute` allows."""


def not_available() -> AccessDenied:
    return AccessDenied("channel is not available")


def rate_limited_count(codes: Iterable[str]) -> int:
    """How many failures were Discord's rate limit (HTTP 429), not access or errors."""
    return sum(1 for c in codes if c.endswith("http_429"))


def busy_or_denied(codes: Iterable[str]) -> Exception:
    """The error for a channel that could not be read: "try again" if Discord was busy, else not available."""
    return DiscordError(429) if rate_limited_count(codes) else not_available()
