import pytest

from discord_mcp.signals import Words, check_words, signals

NONE = Words.build([])


def test_word_matches_start_with_suffix():
    assert Words.build(["submit"]).find("We submitted it") == ["submit"]
    assert Words.build(["submit"]).find("resubmit now") == []


def test_due_not_overdue():
    assert "due" not in " ".join(signals("this is overdue", NONE)[0])
    labels, strong = signals("due Friday", NONE)
    assert strong is True
    assert "deadline" in labels


def test_phrase_matches_at_word_boundaries():
    assert Words.build(["next step"]).find("the next step is") == ["next step"]
    assert Words.build(["next step"]).find("the next steps") == []


def test_regex_characters_are_literal():
    found = Words.build(["c++", "a.b", "(draft"]).find("use c++ and (draft) not axb")
    assert sorted(found) == ["(draft", "c++"]


@pytest.mark.parametrize(
    "text,label",
    [
        ("send it by 17 Nov", "date"),
        ("Nov 17 works", "date"),
        ("2026-11-17 is the date", "date"),
        ("the deadline is friday", "deadline"),
        ("send by EOD", "deadline"),
        ("draft at https://docs.google.com/document/d/x", "doc_link"),
        ("https://drive.google.com/folder", "doc_link"),
    ],
)
def test_strong_signals(text, label):
    labels, strong = signals(text, NONE)
    assert strong is True
    assert label in labels


@pytest.mark.parametrize("text,label", [("see you Monday", "weekday"), ("tomorrow then", "relative_date"), ("next week ok", "relative_date")])
def test_weak_signals_are_not_strong(text, label):
    labels, strong = signals(text, NONE)
    assert label in labels
    assert strong is False


def test_plain_text_has_no_signals():
    assert signals("sounds good, thanks", NONE) == ([], False)


def test_extra_words_are_strong():
    assert signals("final proposal attached", Words.build(["proposal"])) == (["word:proposal"], True)


def test_check_words_limits():
    assert check_words([" submit ", "due"], max_count=3, what="look_for") == ["submit", "due"]
    for bad in ["submit", ["a"], ["x" * 41], ["a1", "b2", "c3", "d4"], [3]]:
        with pytest.raises(ValueError):
            check_words(bad, max_count=3, what="look_for")


# --- final review fixes -----------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["Option 1 may work better", "plan 2 may slip", "50/50 split", "Python 3.12/3.13", "v1.2/1.3",
     "see example.com/2024/11/05/post", "we run 24/7"],
)
def test_numbers_that_are_not_deadlines_are_not_strong(text):
    assert signals(text, NONE)[1] is False


@pytest.mark.parametrize("text", ["May 5 works", "send by 5 June", "on 17/11/2026", "the deadlines moved", "report is overdue"])
def test_real_deadlines_stay_strong(text):
    assert signals(text, NONE)[1] is True


def test_short_numeric_date_is_weak():
    labels, strong = signals("call on 17/11", NONE)
    assert "numeric_date" in labels
    assert strong is False


def test_due_to_is_not_a_deadline():
    assert signals("delayed due to the rain", NONE) == ([], False)


def test_due_with_a_date_is_still_a_deadline():
    labels, strong = signals("due friday", NONE)
    assert "deadline" in labels and strong


def test_due_to_be_is_still_a_deadline():
    labels, strong = signals("due to be submitted friday", NONE)
    assert "deadline" in labels and strong
