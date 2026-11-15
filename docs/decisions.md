# Decision log

Major decisions in time order, newest last. Each entry gives the decision,
why it was made, and what was rejected.

## 2026-11-15 — Stage 1: read-only server, pings only

- **Decision:** The server is read-only (GET only). It stores no message text and
  logs only IDs and counts. Claude does all sorting and summarising.
- **Why:** Message content is sensitive, and the server should hold nothing.

## 2026-11-15 — Stage 2: own OAuth server in front of Discord

- **Decision:** The server issues its own encrypted tokens after a Discord
  `identify` sign-in. Roles are re-read on every request.
- **Why:** Discord does not support the client registration that MCP clients use.
- **Rejected:** Storing tokens in a database (no database at all).

## 2026-11-15 — Pings become conversations

- **Problem:** 15 pings over 14 days came to about 62K characters, which is over
  Claude Code's tool-result limit. Most of it was raw follow-ups, and in plain
  channels the "follow-up" was just the next 10 messages, whatever they were about.
- **Decision:** There are two use cases. (1) "What still needs me, and what has
  been covered" goes in `get_my_pings`, which now returns one row per
  conversation, marked needs_you / reopened / handled / announcement.
  (2) The fuller picture comes from the new `get_conversation`, which opens one
  conversation in full.
- **Grouping ("follow the people"):** A thread is one conversation. In a plain
  channel, a conversation is the reply chain plus messages from the pinger, the
  person and anyone named, until a 12-hour gap or 24 hours after it started.
- **Rejected:** Reply links only (misses answers typed without using reply). Only
  removing repeated messages (size still depends on how busy the channel is).
- **Other calls:**
  - `@here` and role pings count as direct pings, because `@here` is used in
    small private channels. `@everyone` counts as an announcement.
  - A Discord reply belongs with its target's conversation, and my reply to
    anything else never closes an unrelated conversation. Wrong guesses err
    towards needs_you.
  - The result has a 40K-character budget, measured as the transport sends it.
    Old announcements and old handled rows are dropped first.
  - `max_items` is removed. Everything is grouped before any cut.
- **Spec:** `docs/superpowers/specs/2026-11-15-ping-conversations-design.md`

## 2026-11-15 — Process: review before commit, keep this log

- **Decision:** Every commit gets a code review and a security review first, and
  major decisions are recorded here (see `CLAUDE.md`).
- **Why:** Requested by the maintainer after the conversations change was
  committed with a code review but no separate security review.

## 2026-11-15 — Next: channel and thread views (scope)

- **Decision:** The next work covers channel and thread views for any topic,
  not just grants. It serves two people: me (my own items) and a team view
  (for example, the partnerships person sees the whole value across the
  `grants` channel). A server-wide view is deferred.
- **Why:** Most topics (such as grants) are one thread each, sometimes with
  discussion in the main channel. Channel plus thread level is enough for now.

## 2026-11-15 — Channel overview and search (approved)

- **Decision:** add `get_channel_overview` (one small row per thread or
  main-channel topic, up to 30 days, with `only_mine` for a personal view) and
  `search` (by words, grouped by topic), and extend `get_conversation` to open
  any row.
- **"Needs action" signals:** an unanswered ask, a draft shared, or a deadline
  with nothing since. The server only spots strong signals (explicit dates,
  deadline words, document links, watch words) and weak ones (weekday names,
  "tomorrow", "next week"). Claude judges.
- **Learning over time:** there are three word layers: built-in, a per-channel
  `watch` list in config, and `look_for` from Claude on each question. Nothing
  is learned or stored on the server.
- **Main-channel topics** split at a 24-hour quiet gap (the user's choice), with
  each topic capped at 72 hours.
- **Rejected:** adding threads and compaction to `get_channel_messages` (size
  grows with activity). A server-wide view is deferred.
- **Spec:** `docs/superpowers/specs/2026-11-15-channel-overview-and-search-design.md`

## 2026-11-15 — Overview and search: review outcomes

- **Size:** about 25 typical threads fit in full in the shared 40K budget, not
  30. Past that, the quietest rows without strong signals are dropped and
  counted in `omitted`.
- **Bounded work per request:** at most the 50 newest threads per overview, a
  shared budget of 15,000 messages, and 3,000 per channel. Opening a topic
  fetches only that thread, or only the main channel. Per-person rate limits
  are deferred to Stage 3 (deployment).
- **Signals:** a bare `17/11` is only a weak signal, because "24/7" and "10/10"
  are common. `may` counts as a month only before a day. "deadlines" and
  "overdue" are strong.
- **Opened topics** are trimmed to the 40K budget, oldest first, always keeping
  the first message and the first ping.
- **Refresh token reuse after a restart** (from the security review of the
  stage-2 commit) is open and waiting for a decision. Resolved below: sign in
  again after every restart. The options are signing
  in again after each restart, a small token store, or deferring it to
  Stage 3.

## 2026-11-15 — Allow all channels, with a channel deny list

- **Decision:** a new `allow_all_channels = true` mode reads every text,
  announcement and forum channel, except `deny_channels`. A denied entry can be
  a channel, a category (which denies every channel inside it) or a thread. The
  deny list wins in both modes and for every tool. The allowlist stays the
  default mode and still supplies per-channel `watch` words.
- **Why:** to get a whole-server view of what needs the person, without listing
  every channel. Private spaces are excluded explicitly instead.
- **Safeguards:**
  - Discord permissions still apply to each person.
  - In allow-all mode, channels a person can't see are skipped quietly and not
    counted, so the result never reveals how many hidden channels exist.
- **Trade-off:** `get_my_pings` reads every visible channel on each call, so it
  gets slower as the server grows. Per-channel caps still apply. Add a
  total-channel cap if it becomes slow.

## 2026-11-15 — Sign in again after every restart

- **Decision:** codes and refresh tokens carry the process's random boot id and
  are refused after a restart.
- **Why:** used tokens are remembered only in memory, so a restart would
  otherwise let used refresh tokens work again for up to 30 days. This keeps
  the no-database promise.
- **Rejected:** a token store (SQLite or Redis), and deferring the fix to Stage 3.

## 2026-11-15 — Thread starters: "thread created" messages

- **Found on real data:** a thread created on its own leaves a type-18 "thread
  created" system message in the parent, with the thread's id. That message was
  taken as the starter, rejected, and the fallback never ran, so old threads
  showed a mid-thread message as their opening.
- **Fix:** if the parent message isn't a normal message, use the thread's oldest
  message. On real Discord, `after=0&limit=1` returns the oldest message.
- **Review outcomes (allow-all):**
  - In allow-all mode, a failed server lookup fails the call instead of
    returning "nothing needs you".
  - Unknown top-level config settings are rejected, so a misspelled deny list
    can't load as an empty one.
  - Private threads, and per-person denies on channels a person can't see, are
    skipped quietly.
  - Denied channels are never named in `#channel` mentions.
  - Deny-list hits have their own audit code (`dropped:deny_list`).
  - Archived-thread lookups run in parallel, and opening a link looks up only
    its own channel.
  - Deferred: a cap on total channels per `get_my_pings` call (add it if calls
    get slow), and checking the bot's own permissions (channels the bot can't
    read show up as skipped).
