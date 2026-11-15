# Server-wide brief (design)

## Intent

The maintainer wants one morning or Monday brief across the whole Discord
server, not one channel at a time. In their words, the brief covers:

1. **What was discussed last week** across the server.
2. **My pending items and promises over the last 30 days.**
3. **Key items with a deadline this week.**
4. **Nudges to team members** in threads I've been involved in: my asks that
   went unanswered, promises made to me that went quiet, and stalled threads.

Success: Claude can produce that brief from a short, fixed recipe of tool
calls, fast enough for claude.ai and Claude Code, with a link on every item.

## Goals, non-goals, anti-goals

- **Goals:** the four sections above; each building block also useful on its
  own ("who owes me what?", "what's due this week?").
- **Non-goals:** scheduling or emailing the brief (a scheduled Claude agent can
  call these tools later); any write to Discord; summarising on the server.
- **Anti-goals (a failure even if it works):**
  - storing message text, or a brief, anywhere;
  - revealing channels or threads the person can't see (by row, count or log);
  - a call slow enough that claude.ai gives up (keep each call well under a
    minute on a server of a few dozen channels);
  - a result over the 40K-character budget.

## Constraints (unchanged guarantees)

Read-only (GET only), no stored message text, audit lines with IDs and counts
only, fail closed, both access gates (config plus the person's own Discord
permissions), allow-all quiet skips, the deny lists, `direct_only` channels,
fixed error text, message text treated as untrusted, a link on every item.
Everything goes through the existing `_view`, `_threads`, `_fetch` path.

## Approaches

- **A. One `get_my_brief` call** returning all four sections. Rejected:
  30 days across every channel in one call risks claude.ai timing out, and
  one 40K budget split four ways leaves each section thin.
- **B. Three focused tools that Claude combines (chosen by the maintainer).**
  Each keeps its own budget, they can run in parallel, and each is useful
  alone. Cost: some Discord reads are repeated across calls.
- **C. Precompute the brief and store it.** Rejected: stores message text.

## Design

### 1. `get_my_pings` reaches 30 days

`max_days` goes from 14 to 30 (default stays 7). Sections 2 of the brief use
`days=30`: rows marked `needs_you`, `reopened` and `check` (including
`you_promised`). Nothing else changes.

### 2. New tool `get_server_overview(days=7, deadline_days=30, upcoming_days=7)`

Reads every channel the person may read (both gates, deny lists) and their
threads active in the window. `days` (the discussion window) 1-30,
`deadline_days` (how far back to look for deadlines; decided by the
maintainer: 30) 1-30, `upcoming_days` 1-14. The call reads
max(`days`, `deadline_days`) of history.

Returns two sections:

- **`channels`** (last week's discussion): one entry per channel with
  activity in the window, newest activity first:
  `channel`, `message_count`, `topic_count`, `people` (count),
  `last_activity`, and `topics`: up to 3 topics (threads or main-channel
  stretches, as in `get_channel_overview`), chosen by strong signals first,
  then most messages. Each topic: `id`, `where`, `message_count`,
  `last_activity`, `opening` (first message, short), `matched` signal labels.
  Channels with no activity are left out and counted in `quiet_channels`.
- **`deadlines`** (this week): messages from the last `deadline_days` whose
  date resolves to a day from today to today + `upcoming_days`, and that use
  **deadline wording** (decided by the maintainer): the date sits next to a
  cue such as due, deadline, by, before, until, submit/submission, closes,
  last date, or is EOD/EOW itself. "by Friday" and "due Nov 20" count; "I'm
  out Friday" and "event on Nov 20" don't. `direct_only` channels (leave and
  social) are not scanned for deadlines. The list is capped (builder's
  number, about 50) with the rest counted in `omitted_deadlines`. Each item: `due` (YYYY-MM-DD),
  `phrase` (the matched text, e.g. "Friday", "Nov 20", "EOD"), `where`, the
  message `brief` (author, time, text, link), and the topic `id`. Sorted by
  `due`, then time.

**Resolving dates** (relative to the message's own date, in the configured
time zone):

| Text | Resolves to |
| --- | --- |
| "20 Nov", "Nov 20", "20th November", "2026-11-20" | that date (year: the message's, or the next year if that date is over 6 months before the message) |
| "20/11" (numeric) | day/month by default (config `date_order`, "dmy" or "mdy") |
| "today", "EOD", "tonight" | the message date |
| "tomorrow" | message date + 1 |
| "Friday", "by Fri" | the next such weekday on or after the message date |
| "EOW", "end of week" | the Friday of the message's week |
| "next week" | not resolved (too vague); not listed |

A message can yield several deadlines; each date is listed once per message.
Dates inside URLs are ignored.

**Budget:** `deadlines` are kept first. Then channel topics are trimmed (3
per channel, then 2, then 1), then the quietest channels are dropped and
counted in `omitted_channels`. `truncated` is set when anything was cut or a
fetch cap was hit. `skipped_channels` counts channels or threads that could
not be read, with the same quiet rules as `get_my_pings`.

**Bounded work:** a per-channel message cap and a request-wide message
budget, like the channel overview (exact numbers left to the builder after
measuring on the real server; target: `days=7` well under 30 seconds).

### 3. New tool `get_open_loops(days=30)`

Open loops across every readable channel where the person posted in the
window. `days` 1-30. Three kinds where someone else owes the next move
(the nudges), and one where the person does:

- **`unanswered_ask`:** a message from the person that names someone
  directly (a user mention or a reply to their message; roles and @here
  don't count) and asks something (contains "?" or an ask phrase: can you,
  could you, please, pls, any update, let me know, when will), at least 1
  day old, where none of those people posted anything afterwards in the same
  channel or its threads. (Builder's-check call: answers are looked for
  across the channel, not only the same main-channel stretch, so a reply the
  next day isn't missed.)
- **`promise_to_you`:** a message from someone else that names or replies to
  the person and contains a promise phrase (the same rule as `you_promised`),
  at least 2 days old, where its author posted nothing afterwards in the same
  channel or its threads.
- **`stalled`** (threads only, a builder's-check call: a main-channel stretch
  always looks quiet once the channel moves on): a thread the person posted
  in, quiet for at least 3 days, whose **last** message contains "?" or an ask
  phrase, or a strong signal (date, deadline, document link, watch word).
  Threads already listed under another kind are not listed again.
- **`your_promise`** (the person owes; decided by the maintainer): a message
  from the person containing a promise phrase, anywhere (pinged or not), at
  least 1 day old, with no later message from the person in the same thread,
  or replying to it, or (in a main channel) in the same stretch. Items already
  shown as `check` rows by `get_my_pings` may repeat; the recipe tells Claude
  to show each link once.

Each item: `kind`, `where`, `id` (topic), `who` (display names owing the next
move; for stalled, the other people in the topic), `since` (time of the ask,
promise or last message), `quiet_days`, `message` (the ask or promise, or the
last message for stalled: brief with link), and `latest` (the newest message
in the topic, if different).

Order: `your_promise`, `unanswered_ask`, `promise_to_you`, `stalled`; oldest
first within a kind. Budget cuts drop `stalled` first, then
`promise_to_you`, `unanswered_ask`, `your_promise`, counted in `omitted` by
kind. Thresholds (1, 2, 3 days) are config limits.

### 4. Putting the brief together

- The server instructions gain a recipe: for a brief, call in parallel
  `get_server_overview(days=7)`, `get_my_pings(days=14)` and
  `get_open_loops(days=30)`, then write four sections, each item with its
  link and each link shown once:
  1. last week (overview `channels`);
  2. your pending items and promises (pings `needs_you`, `reopened`, `check`,
     plus open loops `your_promise`);
  3. deadlines this week (overview `deadlines`);
  4. nudges (open loops `unanswered_ask`, `promise_to_you`, `stalled`).
- The server also offers an MCP prompt named `brief` with that recipe, so
  claude.ai users can pick it from the prompt menu.

### 5. Config

- `timezone` (IANA name, default "UTC"), used to resolve dates and "today".
- `date_order` ("dmy" default, or "mdy").
- New limits: `nudge_ask_days = 1`, `nudge_promise_days = 2`,
  `nudge_stall_days = 3`, `brief_topics_per_channel = 3`, plus the server-wide
  fetch caps.
- Unknown keys are rejected, as today.

### 6. Bounded work for all three calls

Each call has a per-channel message cap and a request-wide message budget,
and sets `truncated` when it hits one. `get_my_pings` gains the request-wide
budget too. Nothing is kept between calls (no cache), so the three parallel
calls each read Discord; the caps keep them within time. (Builder's-check
call.)

## What's left to the builder

- Module layout (a new `brief.py` or `deadlines.py` next to `topics.py` is
  fine), shared helpers with `topics.py` and `conversations.py`.
- The exact date regexes, within the table above.
- Server-wide fetch caps and concurrency, from a timing run on the real server
  (printing timings and counts only, never text).
- Test names and fixtures (synthetic, from `tests/fakes.py`).
