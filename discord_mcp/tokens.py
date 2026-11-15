"""Encrypted, self-contained tokens, so the server needs no token database.

Each token is AES-256-GCM encrypted with ENCRYPTION_KEY. Its kind (state,
code, access, refresh) is bound in as associated data, so one kind can never be
replayed as another. Payloads hold only IDs and timestamps, never message text
and never a Discord token. Changing ENCRYPTION_KEY signs everyone out.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import time
from collections.abc import Callable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PREFIX = "dm1."


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class TokenBox:
    def __init__(self, key: bytes, *, clock: Callable[[], float] = time.time) -> None:
        if len(key) != 32:
            raise ValueError("key must be 32 bytes")
        self._aead = AESGCM(key)
        self._clock = clock

    def seal(self, kind: str, payload: dict, *, ttl: int | None = None, exp: int | None = None) -> str:
        body = dict(payload)
        body["jti"] = secrets.token_urlsafe(16)
        body["exp"] = int(exp if exp is not None else self._clock() + (ttl or 0))
        nonce = os.urandom(12)
        ct = self._aead.encrypt(nonce, json.dumps(body, separators=(",", ":")).encode(), kind.encode())
        return PREFIX + _b64(nonce + ct)

    def open(self, kind: str, token: str) -> dict | None:
        """The payload, or None if the token is malformed, of another kind, tampered or expired."""
        if not isinstance(token, str) or not token.startswith(PREFIX):
            return None
        try:
            raw = _unb64(token[len(PREFIX) :])
            body = json.loads(self._aead.decrypt(raw[:12], raw[12:], kind.encode()))
        except (InvalidTag, ValueError, TypeError):
            return None
        if not isinstance(body, dict) or not isinstance(body.get("exp"), int) or body["exp"] < self._clock():
            return None
        return body


class ReplayGuard:
    """Remembers used token IDs until they expire, so codes and refresh tokens work once.

    Memory only, so a restart would forget which tokens were used. To keep
    "works once" true, every code and refresh token carries this process's
    random `boot` id, and tokens from an earlier process are refused: after a
    restart, everyone signs in again. Access tokens (one hour) are not affected.
    """

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._seen: dict[str, float] = {}
        self._clock = clock
        self.boot = secrets.token_urlsafe(12)

    def use(self, jti: str, exp: float) -> bool:
        now = self._clock()
        self._seen = {k: v for k, v in self._seen.items() if v >= now}
        if jti in self._seen:
            return False
        self._seen[jti] = exp
        return True
