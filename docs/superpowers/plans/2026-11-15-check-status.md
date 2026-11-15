# Check status Implementation Plan

**Goal:** mark handled conversations with a loose end as `check`.
**Spec:** `docs/superpowers/specs/2026-11-15-check-status-design.md`

## Task 1: status and row (discord_mcp/conversations.py)

- `PROMISE` regex: whole words, case-insensitive, `'` or `’`:
  will, i'll, let me, later, tomorrow, next week, eod, eow, soon, shortly, on it, get back.
- `check_reasons(conv, identity) -> list[str]`: `others_after_you` (a non-bot, non-me
  message after my last reply), `you_promised` (PROMISE in my last reply), in that order.
- `status_of`: `handled` becomes `check` when `check_reasons` is non-empty.
- `STATUS_ORDER = ("needs_you", "reopened", "check", "handled", "announcement")`.
- `render_row`: a `check` row is a handled row plus `check` (reasons) and `latest`
  (newest non-me message after my last reply, or null).

Tests (tests/test_conversations.py): test_human_after_my_reply_is_check,
test_bot_after_my_reply_stays_handled, test_promise_words_make_check (parametrized:
will, I’ll curly, let me, EOD), test_plain_reply_stays_handled, test_both_reasons,
test_new_ping_after_reply_is_still_reopened, test_check_row_shows_latest_after_reply,
test_check_sorts_between_reopened_and_handled, test_budget_cuts_handled_before_check,
existing counts/omitted assertions gain `check`.

## Task 2: instructions and docs

- server.py INSTRUCTIONS and get_my_pings description explain `check`.
  Test: test_instructions_explain_check (tests/test_server.py).
- docs/design.md statuses paragraph; docs/decisions.md entry.
