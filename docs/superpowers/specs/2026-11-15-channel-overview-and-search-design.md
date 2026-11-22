# Channel overview and search

Date: 2026-11-15 · Status: approved 2026-11-15

## Why

`get_my_pings` answers "what needs me". It cannot answer questions about a
channel's topics as a whole, for example "which grants still need action,
going back 30 days" or "where are we on the X proposal":

- It only sees conversations that pinged you.
- The look-back is capped at 14 days.
- `get_channel_messages` never reads threads, and it returns raw messages, so
  its size grows with activity.
- There is no search.
- `get_conversation` only opens ping conversations.

Two people use this view:

- **Me:** my items in a channel (`only_mine`).
- **A team role**, for example someone who owns a programme area: the whole value across
  a channel such as `grants`, whether or not they were pinged.

Both use it in two ways: a daily or weekly briefing, and pointed questions.

## Scope

- One channel (plus its threads) per call, any topic. Grants is only the first
  example. A server-wide view is out of scope (later).
- In channels like `grants`, a topic is mostly one thread, with some
  discussion in the main channel. Both must show up.

## What "needs action" looks like (from the user)

- A question or follow-up request that nobody answered.
- A draft or proposal shared for review.
- A deadline mentioned, with nothing posted since.

The server never decides "needs action". It gives Claude the messages and the
cheap mechanical signals it needs, and Claude judges.

## Goals

- `get_channel_overview` returns one compact row per topic (thread or
  main-channel conversation) over up to 30 days, with the signals above.
- `search` finds topics and messages in one channel by words.
- `get_conversation` opens any overview or search row, not only ping
  conversations.
- Results stay under the same 40,000-character budget, measured as sent.
- Test target: a channel with 30 active threads over 30 days fits the budget,
  and every row with a key message is kept.

## Non-goals

- A server-wide view or a search across channels.
- Summarising on the server. Claude summarises.
- Storing anything between requests, including anything "learned".

## Anti-goals

- **Weakening any guarantee in `docs/design.md`**: read-only, no stored message
  text, minimal logs (search words are never logged), fail closed, and both
  access gates. A team role's "full view" is limited to what
  their own Discord roles can read.
- **Dropping a row that has a key message** (a date, deadline or document
  link) to save space while quiet rows with nothing flagged are kept.
- **Treating Claude's words as patterns.** `look_for` and `search` words are
  matched literally, never as regular expressions.

## Approaches considered

- **A. A channel-overview tool (chosen).** It returns one row per topic, and
  `get_conversation` opens rows. Size depends on the number of topics, not
  on activity.
- **B. Add threads and compaction to `get_channel_messages`.** Rejected: size
  grows with activity, and Claude would still have to piece topics together.
- **C. A search tool (chosen as well).** It is precise for pointed questions
  but cannot drive a briefing on its own. The user chose A and C.

## Key messages: how signals are spotted

There are three layers, all case-insensitive and never treated as patterns.
Words match at the start of a word, and the word may carry a suffix:
`submit` matches "submit", "submitted" and "submits", but not "resubmit".
Phrases (more than one word) match as written, at word boundaries. The
server learns nothing. "Learning over time" means people add words to
config or to their own Claude instructions.

There are two strengths of signal:
- **Strong signals** protect a row from being dropped and put it first:
  explicit dates (a month with a day, numeric dates), `deadline`, `due`,
  `EOD`, `EOW`, document links, `watch` words and `look_for` words.
- **Weak signals** are shown in `matched` but don't protect the row:
  weekday names, `tomorrow`, `next week`. These turn up in most busy
  channels and would otherwise protect everything.

1. **Built in, for every channel:**
   - **Strong:** month names and abbreviations with a day number, numeric
     dates (`17/11`, `2026-11-17`), `deadline`, `due`, `EOD`, `EOW`.
   - **Weak:** weekday names, `tomorrow`, `next week`.
   - Document links: `docs.google.com`, `drive.google.com`,
     `sheets.google.com`, `notion.so`, `notion.site`.
2. **Per channel, in `config.toml`:** an optional `watch` list on each
   allowlist entry, for example
   `{ id = "...", name = "events", phase = 1, watch = ["venue", "agenda", "rsvp", "deadline"] }`.
   It holds words only, never message data.
3. **Per question, from Claude:** an optional `look_for` list on the tool call
   (at most 20 words, each 2–40 characters).

A message is a key message when it matches any layer. Each key message
carries `matched`, the list of things it matched, for example `["date"]`,
`["doc_link"]` or `["word:submit"]`, so Claude can see why it was picked.

## Design

### Topics

Within the window (`days`, default 7, maximum `overview_max_days` = 30):

- **A thread is one topic.** It includes threads whose last message falls
  inside the window, with all their messages inside the window, plus the
  thread's true first message even when it is older. That starter names the
  topic. Fetching it costs one extra call per thread whose start is outside
  the window: the parent-channel message whose id equals the thread id, if
  the thread was started from one, or otherwise the thread's oldest message.
- **A thread started from a main-channel message** includes that starting
  message, which then does not appear in a main-channel topic.
- **Main-channel topics:** main-channel messages are split into topics at
  every quiet gap of `main_topic_gap_hours` (24) or more, the user's choice.
  A topic is also cut at `main_topic_max_hours` (72), so a channel that never
  goes quiet for a day does not become one month-long topic. A Discord reply to a message in
  an earlier topic stays in its own time-based topic, but it keeps its
  `reply_to` text. This is approximate and accepted.

Topic ids are kept separate from ping conversation ids (`<channel>/<message>`),
so each tool's rows open exactly as shown:

- **A thread topic:** `t:<thread_id>`. It is stable as the window moves.
- **A main-channel topic:** `m:<channel_id>/<first message id in the topic>`.

### `get_channel_overview(channel_id, days=7, look_for=[], only_mine=false)`

Each row:

```json
{
  "where": "grants › partner-proposal",
  "kind": "thread",
  "id": "t:<thread_id>",
  "started": "2026-11-04T20:54",
  "last_activity": "2026-11-13T10:08",
  "quiet_days": 2,
  "message_count": 4,
  "people": ["alice", "bob"],
  "your_part": "pinged",
  "opening": { "author": "...", "time": "...", "text": "...", "link": "..." },
  "key_messages": [ { "author": "...", "time": "...", "text": "...", "link": "...", "matched": ["date"] } ],
  "latest": [ { "author": "...", "time": "...", "text": "...", "link": "..." } ]
}
```

- **`kind`** is `"thread"` or `"main"`.
- **`where`** for a main-channel topic is the channel name.
- **`quiet_days`** is the whole days from the last message to now.
- **`people`** is other authors in order of appearance. It includes you only
  when `only_mine` is false and you posted.
- **`your_part`** is `"pinged"`, `"posted"`, `"pinged_and_posted"` or
  `"none"`. Pings use the existing `ping_reason`, so `@here`, role and
  `@everyone` all count as pinged.
- **`opening`** is the first message, with text up to `opening_chars` (300).
- **`latest`** is the newest `overview_latest` (2) messages that are not the
  opening, oldest first, with text up to `summary_chars` (200).
- **`key_messages`** is up to `overview_key_messages` (2) messages with a
  strong signal that are not already in `opening` or `latest`, newest first,
  with text up to `summary_chars`. Messages in `opening` and `latest` that
  match get a `matched` field instead of being repeated.
- **Rows are kept small on purpose.** Claude opens a row with
  `get_conversation` for detail. The size target is a 30-thread channel in
  under 40,000 characters, measured as sent.
- **`only_mine`** keeps only rows where `your_part` is not `"none"`.
- **`is_bot`** appears only when true. Times are trimmed to minutes. Links stay
  complete.

Payload:

```json
{
  "channel": "grants",
  "days": 30,
  "topics": [ ... ],
  "counts": { "thread": 12, "main": 5 },
  "omitted": 0,
  "truncated": false,
  "look_for": ["submit", "due"]
}
```

- **Order:** rows with any strong signal first, then the others. Within each group, the newest activity first.
- **Budget:** when the result would exceed `max_result_chars` (measured as
  sent), rows are dropped from the end, so the oldest rows without strong
  signals go first. `omitted` counts them and `truncated` is set.
- **Errors:** a channel not on the allowlist, or not readable by the person,
  gives "That channel is not available to you." The tool fetches no messages
  before that check passes. Forum channels are allowed, and their topics are
  their threads (a forum has no main channel).

### `search(channel_id, words, days=14)`

- **`words`:** 1–10 words or short phrases, each 2–40 characters, matched
  literally and case-insensitively against message text and thread names.
- **`days`:** maximum `overview_max_days`.
- **Result:** grouped by topic, using the same topics as the overview:

```json
{
  "channel": "grants",
  "days": 30,
  "results": [
    {
      "where": "grants › partner-proposal",
      "kind": "thread",
      "id": "...",
      "last_activity": "...",
      "title_match": true,
      "matches": [ { "author": "...", "time": "...", "text": "...", "link": "..." } ]
    }
  ],
  "match_count": 7,
  "truncated": false
}
```

- A topic whose title matches is included even with no matching messages.
  Its `matches` then holds its latest message.
- Each topic has at most `search_matches_per_topic` (5) matches, newest
  first, with text up to `summary_chars`.
- Topics are ordered by newest match first. The same budget applies, with
  `truncated` set when topics are dropped.
- Search words are not written to the audit log.

### `get_conversation` (extended)

- **`t:<thread_id>`:** returns that thread over `overview_max_days` (30), plus
  its true first message. It is capped like today: the first message, then
  the newest messages.
- **`m:<channel>/<message>`:** returns that main-channel topic, rebuilt over
  30 days.
- **A ping conversation id or a plain message link:** keeps today's
  behaviour and its 14-day regrouping, so `get_my_pings` rows open exactly
  as before. If the message is in no ping conversation, it falls back to its
  thread, then its main-channel topic, over 30 days.
- **Access checks are unchanged.** Prefixed ids are parsed strictly, and
  anything else is an `InputError`.

### Fetching and limits

- A 30-day window can mean many messages. Each channel or thread fetch stops
  after `overview_max_messages` (default 3000) messages. When that happens,
  `truncated` is set, and the topics that were cut are the oldest ones.
- `get_my_pings` keeps its 14-day maximum (`max_days`). Only the overview,
  search and `get_conversation` use `overview_max_days`.

### Config

- **New `[limits]` keys:** `overview_max_days = 30`, `overview_latest = 2`,
  `overview_key_messages = 2`, `opening_chars = 300`, `main_topic_gap_hours = 24`, `main_topic_max_hours = 72`,
  `search_matches_per_topic = 5`, `overview_max_messages = 3000`.
- **New optional `watch` list** on allowlist entries. It must be a list of
  strings, each 2–40 characters, with at most 30 per channel.

### Server instructions

Add guidance to the instructions:

- Use `get_channel_overview` for briefings and "what's pending in #channel".
- Use `search` for a named topic.
- Use `get_conversation` to read one topic in full.
- For pending items, look for unanswered asks in `latest`, and for drafts and
  deadlines in `key_messages`, comparing dates to today and to `quiet_days`.
- Pass `look_for` words that fit the question.

Keep the warning that message text is untrusted.

### Docs

- `docs/design.md`: update the Tools table and add a short "Channel overview"
  section.
- README: the tool list.
- `docs/decisions.md`: add an entry.
- `CLAUDE.md`: no change.

## Decided by the user

- Channel and thread level, any topic. Server-wide is later.
- Both a personal view and a team view.
- The "needs action" signals: an unanswered question or follow-up, a draft
  shared, a deadline with nothing since.
- Channel-specific words. The word layers (built-in,
  per channel, per question) are the way to learn over time, with no
  learning on the server.
- Approaches A and C.
- Main-channel topics split at a 24-hour quiet gap.

## My calls (correct if wrong)

- The 72-hour cap on a main-channel topic. (The 24-hour quiet gap is the
  user's choice.) There is no "follow the people" rule, because there is no
  ping to start from.
- Defaults: 7 days (maximum 30), 2 latest messages, 2 key messages, a
  300-character opening, 5 matches per search topic, and 3000 messages per
  fetch. Rows are small so 30 threads fit, and Claude opens a row for detail.
- Strong and weak signals: weekday names, "tomorrow" and "next week" are
  shown but don't protect a row.
- Words match at the start of a word with any suffix (`submit` matches
  "submitted"). They are never treated as patterns.
- Topic ids are `t:` and `m:`, separate from ping ids, so every tool's rows
  open exactly as shown.
- A thread row always shows its true first message, even when it is older
  than the window. That costs one extra call per such thread.
- Forum channels are supported.
- Search uses the same word matching, and covers one channel per call.
- The row field names and order above.

## Left to the builder

- Exact date patterns (keep them conservative and test them on English
  examples).
- Whether to share topic building with `conversations.py` or put it in a new
  `topics.py`.
- Test fixtures. They must not use real data (see `CLAUDE.md`).
