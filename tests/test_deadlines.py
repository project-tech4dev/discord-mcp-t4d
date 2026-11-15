from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from discord_mcp.deadlines import find_deadlines

UTC_TZ = ZoneInfo("UTC")
BERLIN = ZoneInfo("Europe/Berlin")
# Wednesday 2026-11-11, 10:00 UTC
WED = datetime(2026, 11, 11, 10, 0, tzinfo=UTC)


def dates(text, posted=WED, tz=UTC_TZ, order="dmy"):
    return [d for d, _ in find_deadlines(text, posted, tz, order)]


def test_a_date_without_deadline_wording_is_not_a_deadline():
    assert dates("I'm out Friday") == []
    assert dates("event on Nov 20, see you there") == []


@pytest.mark.parametrize(
    "text,expected",
    [
        ("please send it by Friday", date(2026, 11, 13)),
        ("submission closes fri", date(2026, 11, 13)),
        ("due wednesday", date(2026, 11, 11)),  # the same weekday as the post counts
        ("due Nov 20", date(2026, 11, 20)),
        ("deadline: 20th November", date(2026, 11, 20)),
        ("20 Nov deadline for the report", date(2026, 11, 20)),
        ("submit before 2026-11-20", date(2026, 11, 20)),
        ("last date is 20 Nov 2027", date(2027, 11, 20)),
        ("needed by today", date(2026, 11, 11)),
        ("due tonight", date(2026, 11, 11)),
        ("due tomorrow please", date(2026, 11, 12)),
        ("can you do it by EOD", date(2026, 11, 11)),
        ("EOD works", date(2026, 11, 11)),
        ("EOW is fine", date(2026, 11, 13)),
        ("until end of week", date(2026, 11, 13)),
    ],
)
def test_deadline_phrases_resolve(text, expected):
    assert dates(text) == [expected]


def test_phrase_is_the_matched_date_text():
    assert find_deadlines("please send it by Friday", WED, UTC_TZ, "dmy") == [(date(2026, 11, 13), "Friday")]


def test_next_week_is_too_vague():
    assert dates("due next week") == []


def test_numeric_dates_follow_the_date_order():
    assert dates("submit by 20/11") == [date(2026, 11, 20)]
    assert dates("submit by 11/20", order="mdy") == [date(2026, 11, 20)]
    assert dates("submit by 11/20") == []  # month 20 does not exist in dmy


def test_invalid_dates_are_skipped():
    assert dates("due 31/02") == []


def test_dates_inside_urls_are_ignored():
    assert dates("due: see https://example.org/report/2026-11-20/by-friday?d=20/11") == []


def test_a_date_long_past_rolls_into_next_year():
    posted = datetime(2026, 12, 28, 10, 0, tzinfo=UTC)
    assert dates("due Jan 5", posted) == [date(2027, 1, 5)]


def test_a_recent_past_date_keeps_the_year():
    assert dates("was due Nov 2") == [date(2026, 11, 2)]


def test_time_zone_moves_the_post_date():
    late = datetime(2026, 11, 11, 23, 30, tzinfo=UTC)  # Thursday 00:30 in Berlin
    assert dates("due today", late, UTC_TZ) == [date(2026, 11, 11)]
    assert dates("due today", late, BERLIN) == [date(2026, 11, 12)]


def test_each_date_listed_once_per_message():
    assert dates("due Friday, yes by Nov 13 at the latest") == [date(2026, 11, 13)]


def test_several_deadlines_in_one_message():
    assert dates("draft due Friday, final due Nov 20") == [date(2026, 11, 13), date(2026, 11, 20)]


def test_may_needs_a_day_after_it():
    assert dates("you may submit by friday") == [date(2026, 11, 13)]


# --- review findings: the cue must sit right before the date (or a deadline word right after)

SUN = datetime(2026, 11, 15, 10, 0, tzinfo=UTC)
SAT = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "text",
    [
        "by the way, I'm out Friday",
        "Great work by Asha today",
        "posted by Ravi on Monday",
        "the latest news from Friday",
        "I'm out Friday, due to travel",
    ],
)
def test_wording_away_from_the_date_is_not_a_cue(text):
    assert dates(text, SUN) == []


def test_only_the_cued_date_counts():
    assert dates("by Friday, and I'm out Monday", SUN) == [date(2026, 11, 20)]


@pytest.mark.parametrize("text", ["due Friday next week", "by next Friday", "submit next friday"])
def test_a_weekday_in_next_week_is_not_resolved(text):
    assert dates(text, SUN) == []


def test_this_weekday_still_counts():
    assert dates("due this Friday", SUN) == [date(2026, 11, 20)]


@pytest.mark.parametrize("posted", [SAT, SUN])
def test_eow_on_a_weekend_means_next_friday(posted):
    assert dates("submit by EOW", posted) == [date(2026, 11, 20)]


@pytest.mark.parametrize("text", ["submit before I sat down", "done by sun rise"])
def test_short_weekday_followed_by_a_word_is_not_a_weekday(text):
    assert dates(text, SUN) == []


def test_short_weekday_at_the_end_or_before_a_time_counts():
    assert dates("closes fri", SUN) == [date(2026, 11, 20)]
    assert dates("due fri 5pm", SUN) == [date(2026, 11, 20)]
    assert dates("due mon morning", SUN) == [date(2026, 11, 16)]
