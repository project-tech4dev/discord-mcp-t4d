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

## 2026-11-15 — Brief layout, and Discord's rate limits (after the second real run)

- **5×5×5 brief** (maintainer's call): pending, deadlines and nudges show at
  most 5 items each, the ones most worth acting on; everything else goes to a
  new "more to check" section (one line each, with its link), then last week.
- **Rate limits were silently dropping channels.** The three brief calls run
  in parallel, each reading 8 things at once, and each request backed off on
  its own after a 429. Some ran out of retries and the whole channel was
  counted as "skipped" (a real run lost one of the busiest channels). Fixes:
  - **Shared pacing in the client:** at most 8 requests in flight across all
    calls; a 429 pauses every request on the client for Discord's
    `retry_after`; an exhausted bucket holds back only the next request to
    the same path (the data is returned at once). The client keeps paths and
    times only, never message data.
  - **Reported, not hidden:** results add `rate_limited_channels` when Discord
    refused channels for being busy, and the instructions tell Claude to say
    the answer is incomplete. A rate-limited channel overview fails with the
    "try again shortly" error instead of "not available to you".
  - **Fewer refusals:** the server checks the bot's own roles and skips
    channels the bot cannot read without asking Discord (each 403 counts
    toward Discord's invalid-request limit, which can block the bot's IP).
    These count in `skipped_channels` (the person can see them, so the count
    reveals nothing). If the bot lookup fails, channels are tried as before.
    The bot's user ID is the only thing kept between requests.
- **Rejected:** a cache of refused channel IDs with a time limit (state that
  can go stale; the role check needs none).
- **Review outcomes:**
  - A request queued for a slot checks the pause again once it has the slot
    (otherwise queued requests fired straight into a fresh 429).
  - Waits are capped: each Discord value at 60s (odd or infinite values fall
    back to 1s), and a request facing more than 30s fails at once with "try
    again shortly" rather than hanging every call.
  - Bucket waits are kept per route and channel (a trailing message or user
    ID is folded away), and expired entries are cleared on every insert.
  - `get_conversation` and opening a thread or stretch say "try again" when
    Discord was busy, instead of "not available to you"; a 429 on a private
    thread's membership lookup counts in `rate_limited_channels` (allowlist
    mode; allow-all keeps it quiet); the bot lookup runs alongside the server
    lookups instead of after them.
  - Confirmed: no new count, error or log line reveals hidden channels; the
    bot check only narrows access.
- **Deferred:** a 429 on any route pauses the whole client (simple and
  cautious; per-route pauses only for non-global 429s if this proves slow);
  private threads the bot isn't in still cost one refused request.

## 2026-11-15 — `fyi` for pings that ask nothing; leave wording is not a promise (after the third real run)

- **Context:** running the brief as two people showed noise. Banter,
  shoutouts, "cc @you" and leave notices that named someone came back as
  `needs_you`. A leave notice that named someone, and "I will not be able
  to make it", came back as `promise_to_you`.
- **Decision:**
  - New `get_my_pings` status `fyi`: the person was pinged directly, no
    ping asked anything of them, and they have not replied. A ping asks
    when it has a question or ask phrase ("can u", "lmk", "any updates",
    "gentle reminder"), a question without "?" ("who can review", "are you
    free"), an opening imperative after an optional greeting ("hey, review
    this"), or is only mentions, tags, a link or a greeting (a bare "@you"
    calls for attention). The pinger's follow-up messages count too ("Hi
    @you", then the ask). A non-asking ping after the person's reply ("cool
    thanks") no longer reopens; the row is `check` (`others_after_you`).
    `fyi` sorts after `check`, is dropped after announcement and handled
    rows when over budget, and shows the direct ping.
  - Refusals and absences are not promises: "I will not…", "I'll not…",
    and "I will be / I'll be" on leave, on PTO, on vacation, on holiday,
    OOO, out, away, off, offline, unavailable, travelling or back.
  - Deployment config (not in the repo): channels that carry only notices,
    such as a hiring channel, should be `pings = "direct_only"`.
- **Why:** the brief's first three sections should hold what the person
  must act on. A missed real ask costs more than a noisy row, so the ask
  test is deliberately loose and a bare mention still counts.
- **Rejected:**
  - Skipping `direct_only` channels in `get_open_loops` (tried first): it
    also dropped real promises and asks from a main team channel that is
    `direct_only` only because its team role pings are broadcasts.
    `direct_only` says how @here and role pings count, not whether a
    channel holds work; leave wording handles leave notices. (The deadline
    scan still skips `direct_only` channels; revisit if deadlines there are
    missed.)
  - A model-based classifier (cost, and message text would go to another
    service).
  - Dropping fyi rows entirely (a shared doc or date can still matter).
  - Marking every team channel `direct_only` (real role asks there would
    become announcements).
- **Accepted gaps:** social lines such as "I'll bring cake" still count as
  promises; an ask hidden in a non-asking ping ("thanks, see <link>") shows
  as `fyi` but stays listed with its link.
- **Review outcomes:**
  - Code review: a greeting ping with the ask in the next message became
    `fyi` (pinger's follow-ups are now read); common asks were missed;
    channel, emoji and timestamp tags and markdown links were not
    stripped; an fyi row could show a later @everyone; after the skip was
    removed, "back", OOO, PTO, vacation and holiday wording still read as
    promises. All fixed with tests; mutation checks confirmed the new tests
    fail without their fix.
  - Security review: nothing Critical, High or Medium. Open loops read
    every channel through both access gates, `direct_only` only filtered
    afterwards, so no new channel is read. No ReDoS (worst case under 5 ms
    on 4,000-character adversarial input). Test strings that read like
    real messages were replaced with invented ones.

## 2026-11-15 — Shared modules for the five question types (refactor, phase 1)

- **Decision:** move code every view used into four shared modules, with no
  change in behaviour: `messages.py` (message and channel accessors, links,
  thread labels), `render.py` (display names, mentions, compact and full
  message items), `wording.py` (asks, both promise patterns, month names)
  and `budget.py` (result size, binary-search fitting, message trimming).
  `matching.py` keeps ping detection and the shared request types
  (`Identity`, `RenderContext`, `ChannelMessages`). `permissions.py` keeps
  its own `PRIVATE_THREAD` so the access rules stay standalone.
- **Why:** pings, channel overview, search, the server brief and open loops
  had grown their own copies of the same helpers (three `_time`/`_author`,
  three `THREAD_TYPES`, two link formats, two full-thread renderers, two
  month regexes, two apostrophe classes), and `brief.py` and `topics.py`
  imported private helpers from `conversations.py`. The ask and promise
  rules behind needs_you, you_promised and open loops now sit in one file;
  signals and deadlines keep their own rules and share the month names.
- **Rejected:** merging `Conversation` and `Topic` (they answer different
  questions: what pinged me, and what is happening in a channel).


## 2026-11-15 — Access and reading split out of digest.py (refactor, phase 2)

- **Decision:** split the 840-line `digest.py` into `errors.py` (the tool
  errors, fixed text), `access.py` (both access gates: `view`, `threads`,
  and new `one_channel`, `one_thread` and `home_of` checks used by every
  one-channel tool and link), and `reader.py` (fetching, thread starters, the server-wide read
  and the one-channel topic read, which now returns a `ChannelRead`
  dataclass instead of an 8-tuple). `digest.py` keeps only the tool
  handlers. One `_window` check replaces `_days` and the hand-written
  `limit` check, so `limit=True` is now rejected like `days=True`. Both
  thread caps share `newest_threads`; the two message budget formulas are
  unchanged.
- **Why:** with five question types, the access rules were mixed in with
  tool code, and the "may read, then view, then visible" check was written
  out four times. Keeping the gates in one module makes them easier to
  review on their own.
- **Rejected:** one message-budget formula for both reads (they balance
  different things: one channel's threads, or every channel on the server),
  and re-exporting the errors from `digest.py` (callers now import them
  from `errors.py`).

## 2026-11-15 — One direct-ping rule, fast fitting, limits in descriptions (refactor, phase 3)

- **Decision:** four small fixes after the split.
  - `matching.py` owns which pings are direct: `channel_ping` (role and
    @here pings become "group" in a direct_only channel or its threads),
    `is_quiet` and `is_direct`. get_my_pings and get_channel_overview
    (`your_part`, `only_mine`) both use them, so they can no longer
    disagree.
  - `fit_overview` and `fit_search` use the binary search the brief and
    open-loops budgets already used (`fewest_to_drop`), so a long list is measured about
    log2(n) times instead of once per dropped row. Same result.
  - Tool descriptions are built from the server's `Limits` and the named
    defaults in `digest.py` (`SEARCH_DEFAULT_DAYS` and others), so a
    configured limit is the number Claude sees; "unless configured
    otherwise" is gone.
  - `ToolResult` carries `item_count`, set by each tool, instead of
    server.py guessing it from payload key names. Counts are unchanged.
- **Why:** each was a second copy of a rule that had already drifted once
  or could drift silently (a new payload key, a changed limit).
- **Left as is:** the brief recipe in the server instructions still names
  days=7, 14 and 30 (the defaults; a server configured with smaller maxima
  would need its own recipe), and `fit_budget` / `trim_messages` still
  drop one item at a time (their order and compact steps make a search
  less simple).
- **Rejected:** moving `RenderContext` out of `matching.py` (it is shared
  by every view and has no better home yet), and counting deadlines as well
  as channels in get_server_overview's item_count (kept as it was).

## 2026-11-17 — One always-on process is a deployment requirement (issue #3)

- **Decision:** the server runs as exactly one always-on process. The
  Stage 3 host must be a single VM, or one container or machine with a
  minimum and maximum of 1 instance. `uvicorn_config` also pins `workers=1`
  (a guard; see below). Written into the Status
  section of `docs/design.md` and the README.
- **Why:** used codes and refresh tokens are remembered in memory
  (`ReplayGuard`), and codes and refresh tokens carry the process's boot id.
  With replicas, a code issued by one instance is refused by another (each
  has its own boot id, so this fails closed but breaks sign-in); with
  scale-to-zero, everyone has to sign in again after each cold start. `uvicorn.Server.run()`
  already runs one process, so `workers=1` guards a future switch to
  `uvicorn.run()`. One process keeps the "no token database" guarantee.
- **Rejected:** a shared store (Redis, SQLite or Postgres) for used `jti`s
  with expiry, dropping the boot id. It would allow autoscaling hosts, but
  adds a stateful service to run and secure for a server with one team's
  traffic. Revisit if the host has to scale out.

## 2026-11-17 — Links never make the bot ask about channels it must not read (issue #4)

- **Decision:** `Access.home_of` looks for a link's channel id in the
  server's channel list, then in its active threads, before it calls
  `get_channel`. A channel that configuration, the deny list, the person or
  the bot rules out is refused with the usual "not available" text and no
  request about it. Only an id in neither list (an archived thread, or junk)
  is looked up by itself. What it found is passed on, so `one_thread` and
  `view` do not look the same id up again. A per-person limit of
  `calls_per_minute` tool calls (default 30) in any 60 seconds caps the rest.
  It is checked with the user ID from the sign-in token, before the bot
  looks the member up, so a refused call makes no Discord request at all.
- **Known gap:** an id in neither list is still looked up by itself. If it
  is an archived thread (or a thread hidden from the active list) under a
  channel the bot cannot read, Discord refuses it and that counts. The call
  limit bounds this to 30 a minute per person, against Discord's 10,000 per
  10 minutes. Searching every permitted channel's archived threads instead
  was rejected as many requests on every such link.
- **Why:** any signed-in member could repeat `get_conversation` on ids of
  channels the bot cannot read. Each refused request counts toward Discord's
  invalid-request limit (10,000 per 10 minutes), and past it Discord blocks
  the bot for everyone. The `channel/message` form also skipped the
  server-id check. The limit also bounds a looping client.
- **Rejected:** refusing every id not on the allowlist before any lookup
  (the allowlist holds parent channels, so thread links would stop working);
  caching the server's structure between requests (roles and permissions
  must stay current); a shared or persistent rate-limit store (the server
  is one process, see issue #3, so memory is enough; a restart resets it).

## 2026-11-17 — Optional role restriction (`allowed_roles`)

- **Decision:** a new optional setting, `allowed_roles`, lists role IDs.
  When it is not empty, only members holding one of them may sign in or
  call any tool; everyone else gets fixed text saying their account lacks
  an allowed role. It is checked with the member's roles on every call.
  Empty (the default) keeps the old rule: every server member.
- **Why:** the server has members outside the core team (partners,
  guests). Channel permissions already keep them out of private channels,
  but the team wants the connector itself to be for staff only, which is
  simpler to reason about than relying on every channel's overrides.
- **Rejected:** a list of user IDs (it drifts as people join and leave;
  roles are already maintained), and an environment variable (it belongs
  with the other access rules in the config file).

## 2026-11-17 — A failed server lookup fails the call in every mode (#6)

- **Decision:** when the server's details, roles or channel list cannot be
  read, every tool fails with "Discord could not be reached. Try again
  shortly.", in allowlist mode as in allow-all mode. Before, allowlist mode
  dropped each allowlisted channel and returned an empty result.
- **Why:** an empty `get_my_pings` reads as "nothing needs you", which is a
  wrong answer, not a safe one. Nothing extra is revealed: the call returns
  nothing either way.
- **Rejected:** keeping the per-channel `guild_*` drop lines in the audit log
  (the failed call is already logged once with its status).

## 2026-11-17 — A failed extra read is "partial", not "dropped"

- **Decision:** when a busy channel's second, budget hand-back read fails,
  its first read stays in the result. The audit line says
  `dropped:partial_<code>`, and a 429 counts in `rate_limited_channels` but
  not in `skipped_channels`. `conversation_max_messages` must be at least 2,
  since a full conversation always keeps its first message and first ping.
- **Why:** the old `dropped:<code>` line claimed a channel was left out when
  its messages were shown, and a refused extra read went uncounted, so
  Claude could not say the answer was incomplete.
- **Rejected:** discarding the first read when the second fails (it loses
  good messages for nothing), and a new audit status kind beside `dropped`
  (one more pattern for log readers to learn).

## 2026-11-17 — Discord's fields, not the text, decide a ping (#11); issues #9, #12, #13 not done

- **Decision:** a typed `<@id>` counts only when Discord also lists the
  person in `mentions`. It still marks a reply that names the person as a
  `mention`, not a `reply`. An empty `channel_ids` filter means every
  channel (#10).
- **Why:** a bot post with pings suppressed carries the code in its text,
  and the design already says pings come from Discord's own fields.
- **Rejected:** dropping the text check (replies that name the person would
  become `reply`), #9 nicknames (REST message reads carry no `member` data;
  a lookup per author costs requests), #13 private threads (see the next
  entry), and #12 attachment and
  forward text for now (under 0.5% of messages; deferred).

## 2026-11-17 — Private threads stay out of reach: no Manage Threads (#13)

- **Decision:** the bot does not get Manage Threads, so private threads it
  has not joined, and every archived private thread, are not read by any
  tool. The README says so.
- **Why:** the earlier count of none was wrong: without the permission,
  Discord hides private threads from the bot. Measured briefly with the
  permission (counts only): 4 active private threads, 35 archived (5 active
  in the last 90 days, none in the last 30), in 9 of 27 permitted channels.
  That is real but light use, and Manage Threads would let a leaked token
  archive, lock and delete threads. The code still never writes.
- **Rejected:** keeping Manage Threads (covers active private threads with
  no code change, but a much broader permission for a read-only bot), and
  listing archived private threads on every server-wide call (27 more
  requests per call for threads that were all quiet in the last 30 days).
