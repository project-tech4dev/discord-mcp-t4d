"""Reads intent from message wording: asks, promises, and the month names dates use.

Every rule that decides from text alone lives here, so get_my_pings, get_open_loops,
signals and deadlines read words the same way. Pure functions; nothing is kept.
"""

from __future__ import annotations

import re

# Any apostrophe style: ' ‘ ’ ʼ `
APOS = "['‘’ʼ`]"
# Month names, full or short, as one alternation (no group).
MONTHS = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)

# --- asks ---------------------------------------------------------------------------

# An ask: a question or an ask phrase. Links are ignored ("?" in a query string is not a question).
ASK = re.compile(r"\?|\b(?:can you|could you|please|pls|any updates?|let me know|when will)\b", re.IGNORECASE)
_LINK = re.compile(r"(?:https?://|www\.)[^\s<>]*[^\s<>?.,!)\]]", re.IGNORECASE)
_GREETING = r"(?:hey|hi|hello|hiya|yo|good\s+(?:morning|afternoon|evening))"
# Looser phrasing that, in a ping, still asks something of the person.
_PING_ASK = re.compile(
    r"\b(?:can|could|would|do|are|will|shall)\s+(?:you|u|we)\b|\bplz\b|\bkindly\b|\brequest\s+you\b"
    r"|\bthoughts\b|\btake\s+a\s+(?:look|peek)\b|\bneed\s+(?:you|your)\b|\blmk\b|\blet\s+(?:me|us)\s+know\b"
    r"|\bwdyt\b|\basap\b|\burgent\b|\breminder\b|\bfollow(?:ing)?[\s-]?up\b|\bbump\b|\bmind\s+\w+ing\b"
    r"|\bwaiting\s+(?:on|for)\s+(?:you|your)\b|\bover\s+to\s+you\b|\byou\s+need\s+to\b"
    # A question without "?": "who can review", "anyone free".
    r"|\b(?:who|what|when|where|which|how|why)\s+(?:can|could|will|would|is|are|do|does|did|should)\b"
    r"|\bany(?:one|body)\s+(?:can|could|has|have|wants?|free)\b"
    # An opening imperative, after an optional greeting: "look at this", "hey, review", "pls check".
    rf"|^\W*(?:{_GREETING}\W+)?(?:(?:pls|please|plz)\s+)?(?:look|review|check|see|send|share|add|update"
    r"|confirm|approve|help|reply|respond|fill|sign|join|comment|read|test|fix|merge|call)\b",
    re.IGNORECASE,
)
# Mentions and other Discord tags (channels, custom emoji, timestamps), and markdown links (bounded).
_TAGS = re.compile(
    r"<@[!&]?\w+>|<#\d+>|<a?:\w+:\d+>|<t:-?\d+(?::\w)?>|@(?:here|everyone)\b"
    r"|\[[^\]\n]{0,200}\]\([^)\s]{1,500}\)"
)
_ONLY_GREETING = re.compile(rf"^\W*(?:{_GREETING}\W*)?$", re.IGNORECASE)


def asks(text: str) -> bool:
    """A question or ask phrase, ignoring links."""
    return bool(ASK.search(_LINK.sub(" ", text)))


def ping_asks(text: str) -> bool:
    """Whether a ping asks something of the person. A ping that is only mentions, a link or a
    greeting calls for attention, so it asks; "cc @you", thanks or a shoutout do not."""
    bare = _LINK.sub(" ", _TAGS.sub(" ", text)).strip()
    if not re.sub(r"[\W_]+", "", bare) or _ONLY_GREETING.match(bare):
        return True
    return asks(bare) or bool(_PING_ASK.search(bare))


# --- promises -------------------------------------------------------------------------

# A reply that promises rather than answers (get_my_pings' you_promised, last reply only).
# Whole words, any apostrophe style. "let me know" closes an answer, so it is not a promise;
# "on it" counts only at the start of the reply or as "I'm on it" ("commented on it" is not).
REPLY_PROMISE = re.compile(
    rf"\b(?:will|i{APOS}ll|let me(?! know)|later|tomorrow|next week|eod|eow|soon|shortly"
    rf"|i{APOS}?m on it|get back)\b|^\s*on it\b",
    re.IGNORECASE,
)

# A first-person promise across 30 days of messages (get_open_loops): stricter than
# REPLY_PROMISE. "will" alone ("that will work") or "later"/"soon" are not promises here, nor
# are refusals and absences ("I will not be able to", "I'll be on leave").
_NOT_A_PROMISE = (
    r"(?!\s+not\b|\s+be\s+(?:on\s+(?:leave|pto|vacation|holiday)|ooo|out|away|off|offline|unavailable"
    r"|travell?ing|back)\b)"
)
OWN_PROMISE = re.compile(
    rf"\b(?:(?:i{APOS}ll|i\s+will){_NOT_A_PROMISE}|i{APOS}?m\s+on\s+it|let\s+me(?!\s+know)|will\s+do"
    r"|will\s+(?:send|share|do|check|update|get\s+back|look|review|follow\s+up|circle\s+back|add|fix"
    r"|draft|prepare|reach\s+out|ping|call|confirm|post|upload|write|set\s+up|sort|schedule))\b"
    r"|^\s*on\s+it\b",
    re.IGNORECASE,
)


def reply_promises(text: str) -> bool:
    """The person's last reply in a ping conversation promises rather than answers."""
    return bool(REPLY_PROMISE.search(text))


def is_promise(text: str) -> bool:
    """A first-person promise; a question ("will you send it?") never is."""
    return bool(text) and not text.rstrip().endswith("?") and bool(OWN_PROMISE.search(text))
