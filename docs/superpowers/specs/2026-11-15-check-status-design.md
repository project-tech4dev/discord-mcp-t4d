# A "check" status for loose ends (short design)

## Why

`get_my_pings` marks a conversation `handled` as soon as the person replies
after the last ping. Two kinds of loose end hide there: someone carried on
after the reply without pinging again (a question back, "any update on this?"),
and the reply was a promise rather than an answer ("will send the draft", "let
me check"). Handled rows also show nothing after the reply, so Claude can't
spot either. The goal: the "what needs me" list catches these instead of
filing them as done.

## Approaches

- **A. New `check` status from simple signals (chosen).** Server-side rules,
  no model, Claude judges the result.
- B. No new status; add the newest message to handled rows. Least code, but
  nothing is sorted up front and these rows are cut first on busy weeks.
- C. A model on the server judges each reply. Rejected: puts a model and
  message-text processing in the server.

## Design

- A conversation that would be `handled` becomes `check` when either:
  - **`others_after_you`**: someone other than the person, and not a bot,
    posted in the conversation after the person's last reply; or
  - **`you_promised`**: the person's last reply contains a promise phrase
    (decided by the maintainer: their own promises count). Phrases: will,
    I'll, let me, later, tomorrow, next week, EOD, EOW, soon, shortly, on it,
    get back. Matched as whole words, case-insensitive, straight or curly
    apostrophe.
- `reopened` and `needs_you` are unchanged (a new ping still wins).
- A `check` row is a handled row plus `check` (the list of reasons, in the
  order above) and `latest` (the newest message from someone else after the
  person's last reply, or null).
- Order and size cuts: needs_you, reopened, **check**, handled, announcement.
  `counts` and `omitted` gain `check`.
- The server instructions and the `get_my_pings` description explain `check`:
  read `latest` and `your_last_reply` and say whether it is really closed.

## Non-goals

- No judgement of meaning on the server; false alarms ("thanks!", a "will do"
  already done) are expected and left to Claude.
- No change to the channel overview or search.

## Calls left to the builder

- Exact regex and where it lives (next to the other word rules is fine).
- Test names and fixtures (synthetic, from `tests/fakes.py`).
