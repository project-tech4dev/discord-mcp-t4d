"""Finds deadlines in message text and resolves them to dates.

A date counts only with deadline wording next to it ("by Friday", "due Nov 20",
"20/11 deadline") or when it is a deadline word itself (EOD, EOW). Relative
dates resolve from the day the message was posted, in the configured time
zone. Dates inside links are ignored. Pure functions; nothing is kept.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from discord_mcp.wording import MONTHS

_MONTH_NUMBER = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}  # fmt: skip
_MONTH = rf"(?:{MONTHS})"
_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_WEEKDAY = r"(?:mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:r(?:s(?:day)?)?)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)"
_ORD = r"(?:st|nd|rd|th)?"

_DATE = re.compile(
    rf"""
    (?P<iso>\b(?P<iy>\d{{4}})-(?P<im>\d{{2}})-(?P<id>\d{{2}})\b)
    | (?P<dm>\b(?P<dmd>\d{{1,2}}){_ORD}\s+(?:of\s+)?(?P<dmm>{_MONTH})\.?\b(?:,?\s+(?P<dmy>\d{{4}})\b)?)
    | (?P<md>\b(?P<mdm>{_MONTH})\.?\s+(?P<mdd>\d{{1,2}}){_ORD}\b(?:,?\s+(?P<mdy>\d{{4}})\b)?)
    | (?P<num>(?<![\d/.])(?P<n1>\d{{1,2}})/(?P<n2>\d{{1,2}})(?:/(?P<n3>\d{{4}}|\d{{2}}))?(?![\d/]))
    | (?P<eod>\beod\b)
    | (?P<eow>\b(?:eow|end\s+of\s+(?:the\s+)?week)\b)
    | (?P<rel>\b(?:today|tonight|tomorrow|tmrw)\b)
    | (?P<wd>\b{_WEEKDAY}\b)
    """,
    re.IGNORECASE | re.VERBOSE,
)
# Deadline wording must sit right before the date ("by Friday", "due: Nov 20", "submit before
# this Friday", "last date is 20 Nov"), so a cue in another clause ("by the way, I'm out Friday")
# never counts. Or a deadline word right after it ("20 Nov deadline").
_CUE_BEFORE = re.compile(
    r"\b(?:due|deadlines?|by|before|until|till|submit\w*|submission|closes?|closing|last\s+date)"
    r"(?:\s+(?:is|on|this|the|end\s+of))*[\s:\-\u2013\u2014]+$",
    re.IGNORECASE,
)
_CUE_AFTER = re.compile(r"^[\s:\-\u2013\u2014]{0,3}(?:deadlines?|due(?!\s+to\b))\b", re.IGNORECASE)
_NEXT_BEFORE = re.compile(r"\bnext\s+$", re.IGNORECASE)
_NEXT_AFTER = re.compile(r"^\s+next\s+week\b", re.IGNORECASE)
# A short weekday ("sat", "sun", "mon") followed by another word is usually that word, not a day,
# unless the word is a time of day.
_SHORT_WORD_AFTER = re.compile(r"^\s+(?!(?:morning|afternoon|evening|night|eod|noon)\b)[a-z]", re.IGNORECASE)
_URL = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_CUE_WINDOW = 40  # characters before a date that are checked for deadline wording


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _with_year(m: int, d: int, year: str | None, posted: date) -> date | None:
    if year:
        y = int(year)
        return _safe_date(y + 2000 if y < 100 else y, m, d)
    found = _safe_date(posted.year, m, d)
    if found is not None and found < posted - timedelta(days=183):
        found = _safe_date(posted.year + 1, m, d)  # "due Jan 5" written in late December
    return found


def _resolve(g: dict, posted: date, date_order: str) -> date | None:
    if g["iso"]:
        return _safe_date(int(g["iy"]), int(g["im"]), int(g["id"]))
    if g["dm"]:
        return _with_year(_MONTH_NUMBER[g["dmm"][:3].lower()], int(g["dmd"]), g["dmy"], posted)
    if g["md"]:
        return _with_year(_MONTH_NUMBER[g["mdm"][:3].lower()], int(g["mdd"]), g["mdy"], posted)
    if g["num"]:
        a, b = int(g["n1"]), int(g["n2"])
        day, month = (a, b) if date_order == "dmy" else (b, a)
        return _with_year(month, day, g["n3"], posted)
    if g["eod"]:
        return posted
    if g["eow"]:
        # Friday of that week; posted on a weekend, the coming Friday.
        return posted + timedelta(days=(4 - posted.weekday()) % 7)
    if g["rel"]:
        return posted + timedelta(days=1) if g["rel"].lower() in ("tomorrow", "tmrw") else posted
    target = _WEEKDAYS[g["wd"][:3].lower()]
    return posted + timedelta(days=(target - posted.weekday()) % 7)


def find_deadlines(text: str, posted: datetime, tz: ZoneInfo, date_order: str) -> list[tuple[date, str]]:
    """(resolved date, matched text) for each deadline in `text`, once per date, in text order."""
    if not text:
        return []
    clean = _URL.sub(lambda m: " " * len(m.group()), text)  # keeps positions, drops dates in links
    day = posted.astimezone(tz).date()
    out: list[tuple[date, str]] = []
    seen: set[date] = set()
    for m in _DATE.finditer(clean):
        g = m.groupdict()
        self_cued = bool(g["eod"] or g["eow"])
        before = clean[max(0, m.start() - _CUE_WINDOW) : m.start()]
        after = clean[m.end() : m.end() + 20]
        if g["wd"]:
            if _NEXT_BEFORE.search(before) or _NEXT_AFTER.search(after):
                continue  # "next Friday", "Friday next week": too vague to place this week
            if len(g["wd"]) <= 4 and _SHORT_WORD_AFTER.search(after):
                continue  # "sat down", "sun rise"
        if not (self_cued or _CUE_BEFORE.search(before) or _CUE_AFTER.search(after)):
            continue
        resolved = _resolve(g, day, date_order)
        if resolved is not None and resolved not in seen:
            seen.add(resolved)
            out.append((resolved, m.group().strip()))
    return out
