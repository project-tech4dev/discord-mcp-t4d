"""Spots cheap, mechanical signals in message text: dates, deadline words, document links, chosen words.

The server never decides whether something "needs action"; it only points at the
messages Claude should read. Words from config or from Claude are matched
literally (escaped), case-insensitively, at the start of a word with any suffix.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from discord_mcp.wording import MONTHS

_DAY = r"\d{1,2}(?:st|nd|rd|th)?"
_NUM_EDGE_L = r"(?<![\d./:])"  # not part of a version number, URL path or time
_NUM_EDGE_R = r"(?![\d./:])"
assert "|may|" in MONTHS  # "may" alone is too common a word to count as a month
_MONTHS_NOT_MAY = MONTHS.replace("|may|", "|")

# (label, pattern, strong)
_BUILT_IN: list[tuple[str, re.Pattern[str], bool]] = [
    (
        "date",
        re.compile(
            # "17 Nov", "Nov 17", "May 5" (but not "1 may work": "may" counts only before a day)
            rf"\b(?:{_DAY}\s+(?:{_MONTHS_NOT_MAY})|(?:{MONTHS})\s+{_DAY})\b"
            # 17/11/2026 and 2026-11-17, not inside versions or URLs
            rf"|{_NUM_EDGE_L}\d{{1,2}}/\d{{1,2}}/(?:\d{{4}}|\d{{2}}){_NUM_EDGE_R}"
            rf"|{_NUM_EDGE_L}\d{{4}}-\d\d-\d\d{_NUM_EDGE_R}",
            re.IGNORECASE,
        ),
        True,
    ),
    ("deadline", re.compile(r"\b(?:deadline\w*|due(?!\s+to\b(?!\s+be\b))|overdue|eod|eow)\b", re.IGNORECASE), True),
    ("doc_link", re.compile(r"(?:docs|drive|sheets)\.google\.com|notion\.(?:so|site)", re.IGNORECASE), True),
    (
        "weekday",
        re.compile(
            r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|tues|wed|thu|thurs|fri)\b",
            re.IGNORECASE,
        ),
        False,
    ),
    ("relative_date", re.compile(r"\b(?:tomorrow|next\s+week)\b", re.IGNORECASE), False),
    # "17/11" alone is too often a ratio or score ("24/7", "10/10") to count as a deadline
    ("numeric_date", re.compile(rf"{_NUM_EDGE_L}\d{{1,2}}/\d{{1,2}}{_NUM_EDGE_R}"), False),
]


def _pattern(word: str) -> re.Pattern[str]:
    parts = word.split()
    if len(parts) > 1:
        return re.compile(r"(?<!\w)" + r"\s+".join(map(re.escape, parts)) + r"(?!\w)", re.IGNORECASE)
    return re.compile(r"(?<!\w)" + re.escape(word) + r"\w*", re.IGNORECASE)


class Words:
    """A literal word matcher built from config or tool input."""

    def __init__(self, words: list[tuple[str, re.Pattern[str]]]) -> None:
        self._words = words

    @staticmethod
    def build(words: Iterable[str]) -> Words:
        seen: dict[str, re.Pattern[str]] = {}
        for w in words:
            w = w.strip()
            if w and w.lower() not in {k.lower() for k in seen}:
                seen[w] = _pattern(w)
        return Words(list(seen.items()))

    def find(self, text: str) -> list[str]:
        return [w for w, p in self._words if p.search(text)]


def signals(text: str, extra: Words) -> tuple[list[str], bool]:
    """Labels for what the text contains, and whether any of them is a strong signal."""
    labels: list[str] = []
    strong = False
    for label, pattern, is_strong in _BUILT_IN[:3]:
        if pattern.search(text):
            labels.append(label)
            strong = strong or is_strong
    for w in extra.find(text):
        labels.append(f"word:{w}")
        strong = True
    for label, pattern, _ in _BUILT_IN[3:]:
        if pattern.search(text):
            labels.append(label)
    return labels, strong


def check_words(words: object, *, max_count: int, what: str) -> list[str]:
    """Validate a list of words from config or a tool call. Raises ValueError with fixed text."""
    message = f"{what} must be a list of at most {max_count} words, each 2 to 40 characters"
    if not isinstance(words, list) or len(words) > max_count:
        raise ValueError(message)
    out: list[str] = []
    for w in words:
        if not isinstance(w, str) or not 2 <= len(w.strip()) <= 40:
            raise ValueError(message)
        out.append(w.strip())
    return out
