# Server-wide brief Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Three tools Claude combines into a morning/Monday brief: `get_my_pings` up to 30 days, new `get_server_overview`, new `get_open_loops`.

**Architecture:** One shared server-wide read in `digest.py` (view → threads → bounded fetch) feeds pure functions in two new modules: `deadlines.py` (find and resolve deadline dates) and `brief.py` (server overview rows and open loops, each with a budget fitter). `server.py` registers the tools, the recipe and an MCP prompt.

**Tech Stack:** Python 3.15, MCP SDK `MCPServer`, `zoneinfo`, pytest with `tests/fakes.py`.

**Spec:** `docs/superpowers/specs/2026-11-15-server-wide-brief-design.md`

## Global Constraints

- Read-only (GET only); no message text stored or logged; audit lines carry IDs and counts only.
- Both access gates and the deny lists apply through `_view` / `_threads` / `_fetch`; allow-all skips stay quiet.
- Every result ≤ `max_result_chars` (40,000) measured with `pydantic_core.to_json(indent=2)`.
- Errors shown to Claude are fixed text; config errors never echo values.
- Tests use synthetic data from `tests/fakes.py` only.

## Review Focus

1. A deadline phrase with no cue ("I'm out Friday") must not be listed; "by Friday" must.
2. A date inside a URL (`/2026-11-20/`, `?d=20/11`) must not be listed.
3. An ask answered the next day in a new main-channel stretch (or in a thread of the same channel) must not be an `unanswered_ask`.
4. A private thread the person can't see must never appear in any section or count (allow-all).
5. Busy server: results stay under budget and set `truncated`, never error.

---

### Task 1: Config — time zone, date order, limits, 30-day pings

**Files:** Modify `discord_mcp/config.py`, `config.toml`. Test `tests/test_config.py`.

**Interfaces — Produces:** `Config.timezone: str = "UTC"`, `Config.date_order: str = "dmy"`; `Limits.max_days = 30`, `nudge_ask_days = 1`, `nudge_promise_days = 2`, `nudge_stall_days = 3`, `brief_topics_per_channel = 3`, `brief_max_deadlines = 50`, `server_request_messages` (measured in Task 7).

- [ ] Tests: `test_timezone_and_date_order_load`, `test_bad_timezone_is_rejected` (match "timezone must be"), `test_bad_date_order_is_rejected`, `test_defaults_are_utc_and_dmy`; update `max_days == 30`.
- [ ] Watch fail; implement (validate with `zoneinfo.ZoneInfo`, fixed error text); green.

### Task 2: `deadlines.py`

**Interfaces — Produces:** `find_deadlines(text: str, posted: datetime, tz: ZoneInfo, date_order: str) -> list[tuple[date, str]]` (resolved date, matched phrase), deduped by date, in text order.

- [ ] Tests (`tests/test_deadlines.py`): cue required (`"I'm out Friday"` → `[]`, `"by Friday"` → next Friday on/after the post date); same-day weekday counts; `"due Nov 20"`, `"20th November deadline"`, `"2026-11-20"` with a cue; `"submit by 20/11"` dmy vs mdy (`11/20`); `"today"`/`"tonight"` with cue → post date; `"EOD"` alone → post date; `"tomorrow"` with cue → +1; `"EOW"` alone → Friday of the post's week; `"next week"` → `[]`; URL dates ignored; year rollover (posted Dec 28, "due Jan 5" → next year); explicit year kept; invalid `31/02` skipped; time zone moves the post date (23:30 UTC = next day in Europe/Berlin).
- [ ] Watch fail; implement; green.

### Task 3: Shared server-wide read; request budget for `get_my_pings`

**Files:** Modify `discord_mcp/digest.py`. Test `tests/test_digest.py`.

**Interfaces — Produces:** `Digest._read_server(identity, tool, cutoff) -> _ServerRead(view, ctx, fetched: list[ChannelMessages], skipped: int, capped: bool)`; per-target cap = `min(overview_max_messages, max(100, server_request_messages // n_targets))`.

- [ ] Tests: `test_pings_accept_30_days`, `test_pings_reject_31_days`, `test_pings_share_a_request_budget` (many channels → per-channel `max_counts` shrink, `truncated` when a cap is hit).
- [ ] Watch fail; refactor `get_my_pings` onto `_read_server`; green.

### Task 4: `get_server_overview`

**Files:** Create `discord_mcp/brief.py`; modify `digest.py`. Tests `tests/test_brief.py`, `tests/test_digest.py`.

**Interfaces — Produces:** `brief.channel_entry(...)`, `brief.deadline_items(...)`, `brief.fit_server_overview(channels, deadlines, base, max_chars, topics_per_channel) -> dict`; `Digest.get_server_overview(identity, days=7, deadline_days=30, upcoming_days=7) -> ToolResult`.

- [ ] Tests: discussion only counts messages inside `days`; up to 3 topics per channel, strong signals first, then most messages; `quiet_channels` counts readable channels with no activity; deadlines in [today, today+upcoming] only, sorted by due; `direct_only` channels give no deadlines; `brief_max_deadlines` cap with `omitted_deadlines`; budget order: deadlines kept, topics trimmed 3→2→1, then quietest channels dropped (`omitted_channels`); allow-all hidden channel/private thread never counted or shown; deny list respected; `days`/`deadline_days`/`upcoming_days` bounds → `InputError`.
- [ ] Watch fail; implement; green.

### Task 5: `get_open_loops`

**Files:** `brief.py`, `digest.py`. Tests `tests/test_brief.py`, `tests/test_digest.py`.

**Interfaces — Produces:** `brief.open_loops(groups, identity, ctx, now, extra_by_channel) -> list[dict]`, `brief.fit_loops(items, base, max_chars) -> dict`; `Digest.get_open_loops(identity, days=30) -> ToolResult`.

- [ ] Tests: `unanswered_ask` (direct mention or reply only; roles/@here ignored; answer anywhere later in the channel or its threads clears it; < 1 day old not listed); `promise_to_you` (names or replies to me + promise phrase, author silent since, ≥ 2 days); `stalled` (threads only, quiet ≥ 3 days, last message ask/"?" or strong signal; not repeated if listed under another kind; main-channel stretches never stalled); `your_promise` (my promise ≥ 1 day; cleared by my later message in the same thread, a reply to it, or my later message in the same main-channel stretch); order `your_promise, unanswered_ask, promise_to_you, stalled`, oldest first; cuts drop `stalled` first; `counts`/`omitted` by kind; bots never named or owing.
- [ ] Watch fail; implement; green.

### Task 6: Server wiring

**Files:** `server.py`, `audit.py`. Test `tests/test_server.py`.

- [ ] Tests: exactly eight read-only tools; new descriptions carry `UNTRUSTED`; audit item counts for the new payloads; `INSTRUCTIONS` contains the recipe; prompt `brief` listed and its text names all three tools.
- [ ] Watch fail; implement; green.

### Task 7: Measure, set caps, docs

- [ ] Scratchpad timing script on the real server (timings, counts, sizes only). Set `server_request_messages` and adjust caps so each call is well under 30 s and within budget.
- [ ] Update `docs/design.md`, `README.md`, `config.toml`; add the `docs/decisions.md` entry.
- [ ] Full suite; real-data scan of the diff.
