# Channel Overview and Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `get_channel_overview` and `search` (one channel and its threads, up to 30 days), and extend `get_conversation` to open their rows.

**Architecture:** There are two new pure modules. `signals.py` spots strong and weak signals in text. `topics.py` builds topics (threads, and main-channel segments split by quiet gaps) from fetched messages, renders overview and search rows, and fits them to the budget. `digest.py` keeps doing access checks and fetching, and gains the two tools. `discord_client.py` gains a fetch cap and two single-message fetches for thread starters.

**Tech Stack:** Python 3, MCP Python SDK, httpx, pytest. Reuse `fit`-style budgeting via `conversations._size`.

**Spec:** `docs/superpowers/specs/2026-11-15-channel-overview-and-search-design.md`

## Global Constraints

- New `[limits]`: `overview_max_days = 30`, `overview_latest = 2`, `overview_key_messages = 2`, `opening_chars = 300`, `main_topic_gap_hours = 24`, `main_topic_max_hours = 72`, `search_matches_per_topic = 5`, `overview_max_messages = 3000`. `get_my_pings` keeps `max_days = 14`.
- Allowlist entries take an optional `watch` list of strings, each 2–40 characters, at most 30 per entry.
- `look_for` takes at most 20 words, each 2–40 characters. `search` `words` takes 1–10, each 2–40 characters. Both are matched literally and case-insensitively, at the start of a word, with any suffix allowed. Phrases match at word boundaries. Nothing is ever compiled from user text except through `re.escape`.
- Strong signals are month + day, numeric dates, `deadline`, `due`, `EOD`, `EOW`, document links (`docs.google.com`, `drive.google.com`, `sheets.google.com`, `notion.so`, `notion.site`), watch words and look_for words. Weak signals are weekday names, `tomorrow` and `next week`.
- Topic ids are `t:<thread_id>` and `m:<channel_id>/<first message id>`. Ping conversation ids (`<channel>/<message>`) are unchanged.
- Search words are never written to the audit log. Error text is fixed text. Both access gates apply, no message fetch happens before access passes, and forum channels are allowed.
- No real Discord data goes in tests or docs (CLAUDE.md).

## Review Focus

1. **A thread started over 30 days ago with recent activity:** the row's `opening` is the true starter, and its id is stable (`t:<id>`). Tested in Task 3.
2. **A busy channel with 40 threads:** the result stays within budget, the rows with strong signals are all kept, and `omitted` counts the rest. Tested in Task 2.
3. **`look_for` or search words containing regex characters** (`"c++"`, `"a.b"`, `"(draft"`): they match literally and never raise. Tested in Task 1.
4. **`get_conversation("t:<id>")` for a thread under a channel the person can't read, or a malformed `t:`/`m:` id:** gives `AccessDenied` or `InputError` with no message fetch. Tested in Task 4.
5. **"overdue" vs "due", "resubmit" vs "submit", "EODs":** matching is at the start of a word, so `due` doesn't match "overdue" and `submit` doesn't match "resubmit". Tested in Task 1.

---

### Task 0: Security fixes from the review of commit 21493f4

**Files:**
- Modify: `discord_mcp/audit.py` (`TOOLS`), `discord_mcp/discord_client.py` (`get_messages_since` gains `max_count`), `discord_mcp/digest.py` (`_fetch(..., max_count)`, `get_channel_messages` stops at `limit`, and `get_conversation` caps its fetch), `tests/fakes.py`
- Test: `tests/test_server.py`, `tests/test_discord_client.py`, `tests/test_digest.py`

- [ ] **Step 1: Write the failing tests.**
  - `test_every_registered_tool_audits_under_its_own_name`: for each tool from `list_tools()`, call it (or let the error path run) and assert that the audit line's `tool` equals the tool name, never `"invalid"`.
  - `test_messages_since_stops_at_max_count`: as in Task 3 Step 1.
  - `test_get_channel_messages_fetches_only_up_to_limit`: the fake records the `max_count` passed, and it equals `limit`.
  - `test_every_fetch_is_capped`: digest fetches for `get_my_pings` and `get_conversation` pass `max_count == overview_max_messages`.
- [ ] **Step 2: Run them and confirm they fail.** Run: `.venv/bin/pytest -q`. Expected: the new tests fail.
- [ ] **Step 3: Implement.**
  - Add every tool name to `audit.TOOLS`, including `get_channel_overview` and `search`, in advance.
  - Add `max_count` to `get_messages_since`, stopping paging once it's reached.
  - `_fetch` passes `max_count`, defaulting to `limits.overview_max_messages`.
  - `get_channel_messages` passes `limit`.
  - When a cap is hit in `get_my_pings`, report `truncated`.
- [ ] **Step 4: Run the suite and confirm it passes.** Run: `.venv/bin/pytest -q`. Expected: all pass.

Deferred to the user: the refresh token replay after a restart (Medium). It needs a decision about durable storage versus signing in again after every restart.

### Task 1: Signals and config

**Files:**
- Create: `discord_mcp/signals.py`
- Modify: `discord_mcp/config.py` (Limits, `ChannelEntry.watch`, validation), `config.toml`
- Test: `tests/test_signals.py`, `tests/test_config.py`

**Interfaces:**
- Produces:
  ```python
  class Words:  # compiled, literal word matcher
      @staticmethod
      def build(words: Iterable[str]) -> "Words"
      def find(self, text: str) -> list[str]          # the matched words, as given
  def signals(text: str, extra: Words) -> tuple[list[str], bool]
      # (matched labels like "date", "doc_link", "word:submit", "weekday"; is_strong)
  def check_words(words: object, *, max_count: int, what: str) -> list[str]   # raises ValueError(fixed text)
  ChannelEntry.watch: tuple[str, ...] = ()
  ```

- [ ] **Step 1: Write the failing tests.**
  - `test_word_matches_start_with_suffix`: `Words.build(["submit"]).find("We submitted it")` returns `["submit"]`. `find("resubmit now")` returns `[]`.
  - `test_due_not_overdue`: `signals("this is overdue", Words.build([]))[0]` contains no `"due"`. `signals("due Friday", ...)` is strong.
  - `test_phrase_matches_at_word_boundaries`: `Words.build(["next step"]).find("the next step is")` returns `["next step"]`.
  - `test_regex_characters_are_literal`: `Words.build(["c++", "a.b", "(draft"]).find("use c++ and (draft) not axb")` returns `["c++", "(draft"]`, and nothing raises.
  - `test_strong_signals`: each of `"by 17 Nov"`, `"Nov 17"`, `"17/11"`, `"2026-11-17"`, `"deadline friday"`, `"send by EOD"`, `"https://docs.google.com/document/d/x"` gives `is_strong` True. The labels include `"date"` or `"doc_link"` as appropriate.
  - `test_weak_signals_are_not_strong`: `"see you Monday"`, `"tomorrow then"` and `"next week"` give labels containing `"weekday"` or `"relative_date"`, and `is_strong` False.
  - `test_extra_words_are_strong`: `signals("final proposal attached", Words.build(["proposal"]))` gives `(["word:proposal"], True)`.
  - `test_check_words_limits`: rejects a non-list, more than `max_count` items, a 1-character word and a 41-character word. Accepts and strips valid words.
  - Config tests: `test_watch_list_loads` (`watch = ["submit","due"]` gives `entry.watch == ("submit","due")`), `test_watch_rejects_bad_words` (a 1-character word, or 31 words, raises `ConfigError`), and `test_overview_limits_defaults` (all 8 new limits with their default values).

- [ ] **Step 2: Run them and confirm they fail.** Run: `.venv/bin/pytest tests/test_signals.py tests/test_config.py -q`. Expected: ImportError, and the config tests fail.

- [ ] **Step 3: Implement.**
  - `Words` compiles one pattern from `\b(?:escaped)\w*` per single word. A phrase uses `\b(?:escaped)\b`, with spaces in the phrase matching `\s+`. Matching is case-insensitive.
  - Built-in strong patterns cover dates (month names and 3-letter abbreviations followed or preceded by 1–2 digits, `\d{1,2}/\d{1,2}`, `\d{4}-\d\d-\d\d`), the words `deadline`, `due`, `eod`, `eow` (whole word), and the doc-link domains.
  - Built-in weak patterns cover weekday names and abbreviations (`mon`…`sun`, as whole words), `tomorrow`, and `next week`.
  - Add the new `Limits` fields and the `watch` loading, validated with `check_words(..., max_count=30)`. Add the new keys to `config.toml`.

- [ ] **Step 4: Run the suite and confirm it passes.** Run: `.venv/bin/pytest -q`. Expected: all pass.

### Task 2: Topics, rows and budget

**Files:**
- Create: `discord_mcp/topics.py`
- Test: `tests/test_topics.py`

**Interfaces:**
- Consumes: `ChannelMessages`, `Identity`, `RenderContext`, `ping_reason`, `CONTENT_TYPES` (matching.py), `brief` and `_size` (conversations.py), `Words` and `signals` (Task 1).
- Produces:
  ```python
  @dataclass
  class Topic:
      kind: str                 # "thread" | "main"
      channel_id: str           # thread id for threads, channel id for main
      label: str
      messages: list[dict]      # oldest first, starter included for threads
      @property
      def id(self) -> str       # "t:<thread_id>" or "m:<channel_id>/<first id>"
  def build_topics(main: ChannelMessages | None, threads: list[ChannelMessages], starters: dict[str, dict], ctx: RenderContext) -> list[Topic]
      # starters: thread_id -> its true first message when older than the window
  def overview_row(t: Topic, identity: Identity, ctx: RenderContext, extra: Words, now: datetime) -> tuple[dict, bool]   # (row, has_strong)
  def fit_overview(rows: list[tuple[dict, bool]], base: dict, max_chars: int) -> dict
  def search_results(topics: list[Topic], words: Words, ctx: RenderContext) -> list[dict]
  def fit_search(results: list[dict], base: dict, max_chars: int) -> dict
  ```

- [ ] **Step 1: Write the failing tests.** Use fakes with `ME`, `OTHER` and `THIRD`.
  - `test_thread_is_one_topic_with_starter`: a thread with 3 messages plus a `starters` entry (an older message) gives 1 topic whose first message is the starter, with id `"t:<thread id>"`.
  - `test_thread_started_from_main_message_excludes_it_from_main`: main message `"100"` has `thread={"id":"100"}`, and the thread `"100"` exists. Message `"100"` appears only in the thread topic.
  - `test_main_channel_splits_at_24h_gap`: messages at h=100, 90, 60 (a 30h gap) and 55 give 2 main topics, `["m:10/<first>", ...]`.
  - `test_main_topic_cut_at_72h`: messages every 10h for 100h give topics none of which spans more than 72h.
  - `test_overview_row_fields`: the keys are exactly `{"where","kind","id","started","last_activity","quiet_days","message_count","people","your_part","opening","key_messages","latest"}`. The opening text is ≤ 300 characters, `latest` has 2 entries, and `quiet_days` is computed from `now`.
  - `test_your_part_values`: covers pinged, posted, pinged_and_posted and none.
  - `test_key_messages_are_strong_only_and_not_repeated`: an early message "due 20 Nov", a middle message "see you Monday", and the latest message "draft https://docs.google.com/d/x". `key_messages` holds only the "due" one. The latest entry carries `matched` with `"doc_link"`.
  - `test_overview_orders_signal_rows_first_then_newest`.
  - `test_busy_channel_keeps_all_signal_rows_within_budget`: 40 thread topics with 300-character texts, 10 of them with a strong signal, and a budget of 40000. All 10 signal rows are kept, the `pydantic_core.to_json(out, indent=2)` length is ≤ 40000, `omitted == 40 - len(kept)`, and `truncated` is set when `omitted > 0`.
  - `test_thirty_thread_channel_fits_budget`: 30 thread topics with realistic sizes (opening 300, latest 2×200, 1 key) give `omitted == 0`.
  - `test_search_groups_by_topic_and_matches_title`: thread "Acme proposal", whose messages don't contain "acme", plus a main topic with "acme" in a message. Searching `acme` gives 2 results. The thread result has `title_match` True and its latest message in `matches`.
  - `test_search_caps_matches_per_topic`: 8 matching messages in one topic gives 5 matches, newest first.

- [ ] **Step 2: Run them and confirm they fail.** Run: `.venv/bin/pytest tests/test_topics.py -q`. Expected: ImportError.

- [ ] **Step 3: Implement.**
  - For threads, if the starter isn't already present, prepend it.
  - Main-channel messages: drop the ones that started a thread present in `threads`, then segment by gap and cap.
  - `your_part` uses `ping_reason` over the topic's messages.
  - `fit_overview` orders by `(not has_strong, -last_activity)` and drops from the end using `_size`.
  - `fit_search` orders by newest match first and drops from the end.
  - Payload keys come from the spec: overview has `channel, days, topics, counts{thread,main}, omitted, truncated, look_for`; search has `channel, days, results, match_count, truncated`. `channel`, `days` and `look_for` come in through `base`.

- [ ] **Step 4: Run the suite and confirm it passes.** Run: `.venv/bin/pytest -q`. Expected: all pass.

### Task 3: Fetching and the two tools

**Files:**
- Modify: `discord_mcp/discord_client.py`, `tests/fakes.py`, `discord_mcp/digest.py`, `discord_mcp/server.py`
- Test: `tests/test_discord_client.py`, `tests/test_digest.py`, `tests/test_server.py`

**Interfaces:**
- Produces:
  ```python
  # discord_client.py
  async def get_messages_since(self, channel_id: str, cutoff: datetime, max_count: int | None = None) -> list[dict]
  async def get_message(self, channel_id: str, message_id: str) -> dict          # GET /channels/{c}/messages/{m}
  async def get_oldest_message(self, channel_id: str) -> dict | None             # GET /channels/{c}/messages?after=0&limit=1
  # digest.py
  async def get_channel_overview(self, identity, channel_id: str, days: int | None = None, look_for: list[str] | None = None, only_mine: bool = False) -> ToolResult
  async def search(self, identity, channel_id: str, words: list[str], days: int | None = None) -> ToolResult
  async def _channel_topics(self, identity, tool: str, channel_id: str, days: int) -> tuple[_View, dict, list[Topic], bool, list[str]]
      # (view, parent channel, topics, capped, fetched ids). Raises AccessDenied before any message fetch.
  ```
  The fake client implements the same three methods. `get_message` and `get_oldest_message` record `"message:<c>/<m>"` and `"oldest:<c>"` in `calls`.

- [ ] **Step 1: Write the failing tests.**
  - Client: `test_messages_since_stops_at_max_count` (a fake transport returns 3 full pages, and `max_count=150` gives 150 messages and stops paging), `test_get_message_path`, `test_get_oldest_message_uses_after_zero`.
  - Digest:
    - `test_overview_lists_threads_and_main_topics`.
    - `test_overview_thread_older_than_window_shows_true_starter`: a thread whose `id` equals parent message `"5"` at hours_ago=900. The row's `opening.link` ends with `/5`, and `"message:10/5"` is in `client.calls`.
    - `test_overview_thread_not_from_parent_uses_oldest`.
    - `test_overview_only_mine`.
    - `test_overview_watch_words_from_config_are_strong`.
    - `test_overview_rejects_channel_off_allowlist_without_fetch`.
    - `test_overview_days_above_30_rejected`.
    - `test_overview_forum_channel_threads_only` (parent type 15).
    - `test_overview_bad_look_for_is_input_error` (21 words).
    - `test_search_finds_thread_by_title`.
    - `test_search_words_not_in_audit_log`: the caplog audit lines don't contain the search word.
    - `test_search_requires_words`.
  - Server: the tool list is the 6 names, sorted; the new tools carry the read-only annotation and the `UNTRUSTED` text; the audit `item_count` for the overview is the number of topics and for search the number of results.

- [ ] **Step 2: Run them and confirm they fail.** Run: `.venv/bin/pytest -q`. Expected: failures only in the new tests.

- [ ] **Step 3: Implement.**
  - `_channel_topics`:
    1. Check the allowlist, then `_view(identity, tool, [channel_id])`. The parent must be in `view.visible` (types `TEXT_TYPES | FORUM_TYPES`). Otherwise raise `AccessDenied`.
    2. Call `_threads(...)` filtered to that parent.
    3. Fetch with the `overview_max_messages` cap. `capped` is true if any fetch returned exactly the cap.
    4. For each thread whose earliest fetched message is not its first: try `get_message(parent, thread_id)`, and on 404 fall back to `get_oldest_message(thread_id)`. Skip quietly on `DiscordError`.
    5. Call `build_topics`.
  - Extend `_fetch` to pass `max_count`.
  - Validate `look_for` and `words` with `check_words` (20 and 10), and re-raise as `InputError`.
  - Server tools:
    - `get_channel_overview(channel_id: str, days: int | None = None, look_for: list[str] | None = None, only_mine: bool = False)`, titled "Get a channel overview".
    - `search(channel_id: str, words: list[str], days: int | None = None)`, titled "Search a channel".
    - Descriptions as in the spec, plus `UNTRUSTED`.
  - Extend the audit `items` lookup with `"topics"` and `"results"`.
  - Add the INSTRUCTIONS sentences from the spec.

- [ ] **Step 4: Run the suite and confirm it passes.** Run: `.venv/bin/pytest -q`. Expected: all pass.

### Task 4: `get_conversation` opens topic rows

**Files:**
- Modify: `discord_mcp/digest.py` (`get_conversation`), `discord_mcp/topics.py` (`render_topic(t, ctx) -> dict`, the same shape as `render_full`)
- Test: `tests/test_digest.py`, `tests/test_topics.py`

- [ ] **Step 1: Write the failing tests.**
  - `test_get_conversation_thread_id_returns_thread_with_starter`.
  - `test_get_conversation_main_topic_id`.
  - `test_get_conversation_ping_link_unchanged`: the existing ping-conversation tests still pass untouched.
  - `test_get_conversation_link_not_in_ping_conversation_falls_back_to_thread_then_main`.
  - `test_get_conversation_topic_id_under_hidden_channel_denied_without_fetch`: covers both a `t:` id for a thread whose parent is not allowlisted and an `m:` id for channel 20. Each raises `AccessDenied`, and no `messages:` call is made for them.
  - `test_get_conversation_malformed_topic_ids`: `"t:"`, `"t:abc"`, `"m:10"`, `"m:10/x"` and `"x:1"` each raise `InputError`.
  - `test_render_topic_capped_keeps_first_and_newest`.

- [ ] **Step 2: Run them and confirm they fail.** Run: `.venv/bin/pytest -q`. Expected: failures only in the new tests.

- [ ] **Step 3: Implement.**
  - Parse `t:(\d+)` and `m:(\d+)/(\d+)` strictly.
  - For `t:`, find the thread's parent via `_view` and `_threads` (the same access path as today). If it's not readable, raise `AccessDenied`. Otherwise use `_channel_topics(parent, overview_max_days)` and pick the topic whose `channel_id` matches.
  - For `m:`, call `_channel_topics(channel)` and pick the main topic that contains the message.
  - For links and ping ids, keep today's path. When the message isn't found there, fall back to `_channel_topics(...)` over 30 days and pick the topic that contains the message.

- [ ] **Step 4: Run the suite and confirm it passes.** Run: `.venv/bin/pytest -q`. Expected: all pass.

### Task 5: Docs, reviews, live check, commit

- [ ] **Step 1: Update the docs.**
  - `docs/design.md`: the Tools table (6 tools) and a short "Channel overview and search" section.
  - README: the tool list and a `watch` example in the config section.
  - `config.toml`: a commented `watch` example.
  - The decision log is already updated.
- [ ] **Step 2: Run the whole suite.** Run: `.venv/bin/pytest -q`. Expected: all pass.
- [ ] **Step 3: Code review and security review (CLAUDE.md).** Get a fresh code reviewer and a fresh security reviewer on the most capable model, then fix Critical and Important findings with tests that fail first.
- [ ] **Step 4: Scan for real data.** Run `grep` for real names and IDs over the files to be committed. Expected: no hits.
- [ ] **Step 5: Check live.** After a server restart:
  - `get_channel_overview(<channel>, days=30, look_for=[<a few words>])` gives a result inline, under 40K characters.
  - `search(grants, ["acme"])` finds the thread by title.
  - `get_conversation("t:<id>")` opens a thread.
- [ ] **Step 6: Commit** with a message that lists the reviews run and what they found.
