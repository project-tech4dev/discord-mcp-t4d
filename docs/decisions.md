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

## 2026-11-15 — Broadcast channels: `pings = "direct_only"`

- **Problem:** once the bot could read every team channel, leave notices
  ("@team out today") and blog shares filled `needs_you`, and most of those
  rows were not asks.
- **Decision:** a per-channel config setting, `pings = "direct_only"`. In those
  channels and their threads, @here and role pings become reason `group` and
  count as announcements. Pings that name the person, and replies to them,
  still need the person. The channel stays readable for the overview, search
  and messages, so "who is out next week?" still works.
- **Rejected:** denying the channels (loses the leave and social questions);
  treating every bot message as an announcement (people post their own
  "@team I'm out" notices).
- **Review outcomes:** a channel's category is not matched (only the channel
  itself, or a thread's parent). Deferred: unknown keys inside an allowlist
  entry are still ignored (a typo fails safe, as `all`), and the overview's
  `your_part` still counts a `group` ping as `pinged`.

## 2026-11-15 — Links on every item; shared URLs passed on with care

- **Decision:** the instructions to Claude ask for the Discord link on every
  message or conversation it mentions. URLs that people share (blogs, docs,
  forms) may be passed on exactly as written, credited to their author, never
  changed, hidden behind other text or presented as recommended. Message text
  that the server cut short is marked `text_cut`, and URLs are only passed on
  from text that was not cut.
- **Why:** summaries without links were much less useful. Shared URLs come
  from untrusted message text, so a phishing link must not read as an action
  item, and a URL cut at the length limit must not be passed on half-written.
- **Rejected:** a separate server-side list of URLs per message with their
  hosts (possible later hardening).

## 2026-11-15 — Testing with claude.ai through Tailscale Funnel

- **Decision:** to test the connector in claude.ai before deployment, run the
  server locally behind a Tailscale Funnel, with a separate git-ignored
  `.env.funnel.local`. The README documents every local mode (tests, stdio,
  HTTP with Claude Code, claude.ai through the tunnel).
- **Why:** claude.ai connects from Anthropic's cloud, so `localhost` can't be
  reached. Funnel ends the encryption on the local machine, so the tunnel
  provider can't read message text, and it gives a stable address.
- **Rejected:** ngrok and Cloudflare tunnels (they decrypt traffic at their
  edge); deploying early only to test.
- **Caveat:** the open tunnel exposes the server before per-person rate limits
  exist (stage 3), so it stays open only while testing.

## 2026-11-15 — A `check` status for loose ends

- **Problem:** a conversation counted as handled as soon as the person
  replied, hiding two loose ends: someone carried on after the reply without
  pinging again, or the reply was a promise ("will send the draft") rather than
  an answer. Handled rows showed nothing after the reply.
- **Decision:** a handled conversation becomes `check` when someone other
  than the person posted after their last reply (`others_after_you`), or their
  last reply contains a promise phrase (`you_promised`: will, I'll, let me,
  later, tomorrow, next week, EOD, EOW, soon, shortly, I'm on it, get back).
  "let me know" and "commented on it" don't count (review finding: they close
  answers). Known gaps: "ill" without an apostrophe, "we'll", "gonna". Check
  rows show the reasons and the newest message after the reply, and sort
  between reopened and handled, so they outlast handled rows in the size cut.
- **Maintainer's calls:** the person's own promises count. Bots posting after
  the reply don't (leave calendars and similar would be noise).
- **Rejected:** only adding the newest message to handled rows (nothing
  sorted up front, and those rows are cut first); a model on the server
  judging each reply (puts a model and message-text processing in the
  server). False alarms such as "thanks!" are left to Claude.
- **Spec:** `docs/superpowers/specs/2026-11-15-check-status-design.md`

## 2026-11-15 — Clean-up batch from earlier reviews

- **Overview `your_part`:** "pinged" now means a direct ping, as in
  `get_my_pings`. @everyone, and @here or role pings in a `direct_only`
  channel, no longer count.
- **Config typos inside entries** (allowlist, `deny_channels`, `deny`) are
  rejected, like unknown top-level settings, so `ping = "direct_only"` can't
  load silently as the default.
- **Overview and search report `skipped_channels`:** the main channel or
  threads in the window that could not be read, which used to vanish silently.
- **Allow-all mode:** a failed membership lookup on a private thread is now
  quiet, like `no_access` (security review: a transient Discord error could
  otherwise reveal that a private thread exists).
- **Search** counts and shows only matches inside the window (the same cutoff
  as the fetch). A thread's older first message is still shown in the overview
  for context, and still counts toward `your_part`.
- **"due to"** is excluded, but "due to be ..." still counts. Trade-off: "due
  to the funder by 5pm" is missed; Claude still sees the date.
- **Config:** `deny` entries accept `note`, and malformed entries (not tables)
  are config errors instead of crashes.
- **Still deferred:** starter-lookup failures are not audited (the topic still
  works without its starter), the duplicated grouping code, and the " › " title
  split.

## 2026-11-15 — Server-wide brief: three tools Claude combines

- **Decision:** a morning or Monday brief comes from three calls made in
  parallel: `get_server_overview` (new: last week per channel, and deadlines
  due this week), `get_my_pings` (now up to 30 days) and `get_open_loops`
  (new: the person's promises, unanswered asks, promises to them, stalled
  threads). The server instructions and a `brief` MCP prompt give Claude the
  recipe. Each call reads every readable channel within a per-request message
  budget and keeps its own 40K budget.
- **Why:** one call over 30 days of every channel risks claude.ai timing out,
  and one 40K budget split four ways leaves each section thin. Each tool is
  also useful alone ("who owes me what?", "what's due this week?").
- **Rejected:** one `get_my_brief` call (A); precomputing and storing the brief
  (C: stores message text).
- **Maintainer's calls:** a date counts as a deadline only with deadline
  wording next to it ("by Friday", "due Nov 20", EOD/EOW), so "I'm out Friday"
  doesn't; deadlines are looked for over the last 30 days; the person's own
  promises are included, pinged or not (`your_promise`).
- **Builder-check calls:** "stalled" applies to threads only (a main-channel
  stretch always looks quiet once the channel moves on); only people named
  directly (mention or reply) owe a reply, never roles, @here or bots; an
  answer anywhere later in the same channel or its threads closes an ask, so a
  next-day reply in a new stretch isn't missed; no cache between calls, so the
  three calls each read Discord; broadcast (`direct_only`) channels are not
  scanned for deadlines.
- **Measured** on a server of about 100 channels and threads: each call
  11-16 s, all three in parallel about 22 s; `get_server_overview(7, 30)` about 32K characters,
  `get_open_loops(30)` about 21K. `server_request_messages = 30000`.
- **Size:** `get_my_pings` over 30 days had about 120 rows. It now shortens
  texts before dropping rows (`compact_level` 1, then 2: one short ping per
  row with its link). With 14 days every urgent row fits; with 30 days
  `needs_you` and `reopened` fit and the oldest `check` rows can be cut
  (counted in `omitted`).
- **Spec:** `docs/superpowers/specs/2026-11-15-server-wide-brief-design.md`
- **Brief recipe uses `get_my_pings(days=14)`** (maintainer's call after
  measuring): 30 days of pings came to about 120 rows and cut most `check`
  rows to fit the budget, while 14 days keeps every urgent and check row.
  The person's own promises still go back 30 days through `get_open_loops`.
- **Review outcomes (code and security reviews, fixed before commit):**
  - Deadline wording must sit right before the date ("by Friday", "due: Nov
    20", "submit before this Friday") or be a deadline word right after it, so
    "by the way, I'm out Friday", "posted by Ravi on Monday" and "the latest
    news from Friday" no longer count. A weekday with "next" before it or "next
    week" after it is not resolved. EOW posted on a weekend means the coming
    Friday. A short weekday followed by another word ("sat down", "sun rise")
    is not a day, unless the word is a time of day ("mon morning").
  - Open loops merge everyone owing in a topic into `who`, so a second person
    asked in the same stretch is not lost. A question to someone is an
    `unanswered_ask`, never also `your_promise`.
  - Promises in open loops use a stricter first-person rule (I'll, I will,
    I'm on it, let me, will send/share/do/check/...) and never a question;
    "that will work" or "see you later" no longer count. `get_my_pings`'
    `you_promised` keeps its broader rule, since it only reads the last reply
    in a ping conversation.
  - `fit_budget` drops handled and announcement rows before shortening any
    urgent row; shortening is the last resort.
  - Server-wide reads: one hard message budget per request (no 100-message
    floor), with unused share handed back to busy channels in a second pass,
    and at most `server_max_threads` (200) newest threads per request.
  - Open loops run in linear time (one pass per channel group), and result
    fitting uses a binary search instead of re-measuring after every cut.
  - The `brief` prompt carries the untrusted-text warning; the deadline cap
    sets `truncated`; within a kind, open loops drop the oldest first.
- **Deferred:** a per-person deny on a thread's own id is not checked (only
  its parent channel); one error text names the configured maximum days.

## 2026-11-15 — Brief: section order, size and asks (after the first real run)

- **Section order** (maintainer's call): pending items and promises,
  deadlines this week, nudges, then last week.
- **Pings may go past 40K:** a new `pings_max_chars` limit (default 40K) lets
  busy people, mostly senior leadership, get every urgent row whole; Claude
  Code then needs a higher `MAX_MCP_OUTPUT_TOKENS`. When rows must still be
  shortened, the least urgent are shortened first and only as far as needed,
  so `needs_you` rows keep their full text.
- **A "?" inside a link is not a question** (found on real data: a shared
  URL with a query string showed up as an unanswered ask).
- **Rejected:** raising the 40K budget for every tool (the original Claude
  Code failure was a 62K result).
- **Review outcomes:** shortened rows carry their own `compact` level so
  Claude knows which rows to open; a row is dropped only when every kept row
  is already as short as it gets (the size estimate is corrected exactly);
  "did you see <link>?" is still a question; `pings_max_chars` is capped at
  200,000.
