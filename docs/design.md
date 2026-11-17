# Design

discord-mcp is a read-only MCP server. Each person asks Claude for the Discord
messages that ping them, and the server returns only those from allowlisted
channels the person can already see in Discord.

## Flow

1. Claude calls a tool on behalf of a signed-in person.
2. The server works out the person's roles and which permitted channels they can see.
3. It fetches recent messages from those channels with a bot token, at the moment of the request.
4. It returns the messages that ping the person. Claude does all sorting and summarising.

There is no database and no message history. Messages exist in memory only while a request runs.

## Guarantees

- **Read-only.** The Discord client has one HTTP method, GET. No tool posts, edits, reacts or deletes.
- **No stored message text.** Nothing is written to disk, logs or outside services.
- **Minimal logs.** Each line holds only user ID, tool name, channel IDs, item count, duration and status. Every other logger is silenced, and errors are reported by type only.
- **Fail closed.** A channel that fails any lookup, returns 403, or has an unrecognised type is dropped from the result and its ID is logged.
  If the server's own roles or channel list cannot be read, no channel can be checked, so the whole call fails ("Discord could not be reached") rather than returning an empty result.
- **Two gates.** A channel must be permitted by configuration and visible to the person under Discord's permission rules.
  Configuration is either the allowlist (default) or, with `allow_all_channels = true`, every text,
  announcement and forum channel. Either way the channel deny list wins: a denied channel, any channel
  in a denied category, and any thread under a denied channel (or itself denied) is never read by any
  tool, for anyone. In allow-all mode, channels a person cannot see are skipped quietly and never
  counted, so results do not reveal how many hidden channels exist.
- **Allowed roles.** With `allowed_roles` set, only members holding one of
  those roles may sign in or call a tool; others are refused like non-members.
- **No requests Discord will refuse.** Discord blocks a bot that makes too many
  refused requests (401, 403, 429: 10,000 per 10 minutes). A link's channel id
  is untrusted, so `get_conversation` first looks for it in the server's channel
  list and then its active threads, and refuses a channel that configuration,
  the person or the bot cannot read without asking Discord about it. Only an id
  in neither (an archived thread, or junk) is looked up by itself. Each person
  may make `calls_per_minute` tool calls (default 30) in any 60 seconds, counted
  in memory and checked before any Discord request.
- **Secrets from the environment only.**

## Sign-in

The server is its own OAuth authorization server in front of Discord, because
Discord does not support the registration methods MCP clients use.

- One pre-registered client (Claude). Self-registration is disabled.
- Discord sign-in asks only for `identify`. The server reads the user ID,
  revokes Discord's token at once, and checks server membership with the bot.
- The Discord step is bound to the browser that started it with a short-lived
  cookie, so a sign-in link cannot be finished by someone else.
- The server issues its own encrypted tokens: access tokens last one hour, and
  a sign-in lasts 30 days from first sign-in without sliding. Codes and refresh
  tokens work once. No token database exists; changing the key signs everyone out.
- Run exactly one server process. Used codes and refresh tokens are remembered
  in memory, and codes and refresh tokens carry the process's random boot id.
  A second worker or replica refuses the first one's codes and refresh tokens,
  so sign-in breaks (it fails closed). After a restart they stop working and
  everyone signs in again (access tokens still last their hour).
- Roles are read by the bot on every request, so losing a role (including an
  allowed role) or leaving the server takes effect immediately.

## Permissions

`permissions.py` follows Discord's rules: owner first, then Administrator in
the base permissions, then the @everyone override, then all role overrides
combined, then the member override, denies before allows at each step. A
channel needs both View Channel and Read Message History. Threads use their
parent's permissions, and private threads also need membership. Unrecognised
data counts as no access.

## What counts as a ping

A message pings a person when Discord's own fields show a mention of them, a
mention of a role they hold, @everyone, @here, or a reply to one of their
messages. Their own messages never count. Each ping carries a reason.

A channel set to `pings = "direct_only"` in config (for leave notices or
social channels) turns @here and role pings there, and in its threads (not in a category
set this way), into
reason `group`, which counts like @everyone. Pings that name the person, and
replies to them, still count as direct.

## Conversations

`get_my_pings` returns conversations, not raw pings. A thread is one
conversation. In a plain channel a conversation starts at a ping and follows
the people: later messages join when they reply into it or come from the
pinger, the person, or anyone the ping names, until a 12-hour quiet gap or
24 hours after it started. A message that fits several goes to the one it
replies into, else to the one with the most recent ping. @everyone and
`group` conversations take the person's messages only by reply.

Each conversation is marked `needs_you` (no reply from the person yet),
`reopened` (pinged again after their reply), `handled` (their last message
is after the latest ping) or `announcement` (only @everyone, or `group` pings).
Only pings that ask something count toward `needs_you` and `reopened`: a
question, an ask phrase ("can u", "lmk", "any updates", "gentle reminder"),
a question without "?" ("who can review", "are you free"), an opening
imperative after an optional greeting ("hey, review this"), or a ping that
is only mentions, tags, a link or a greeting. A later message from the same
pinger counts too, so "Hi @you" followed by the ask still needs the person
(before their reply for `needs_you`, after it for `reopened`); someone
else's ask in the thread does not. A conversation whose direct pings ask
nothing (a cc, thanks, a shoutout, a shared link, a leave notice) is `fyi`
when the person has not replied; a non-asking ping after their reply leaves
it `check` (`others_after_you`) rather than `reopened`.
A conversation that would be handled is marked `check` instead when someone
other than the person (not a bot) posted after their last reply
(`others_after_you`), or their last reply is a promise such as "will", "let
me" or "EOD" (`you_promised`). Check rows carry the reasons and the newest
message after the reply in `latest`. Handled and check rows carry the
person's last reply so Claude can judge whether it closed the matter. The
result has a size budget (40,000 characters by default): when it is over,
oldest announcements, then oldest handled rows, then oldest fyi rows are
dropped and counted in `omitted`. `get_conversation` opens one conversation in full.
Design: `docs/superpowers/specs/2026-11-15-ping-conversations-design.md`.

## Tools

| Tool | Returns |
| --- | --- |
| `list_my_channels` | Allowlisted channels the person can see |
| `get_my_pings` | Conversations that ping the person from the last N days (default 7, max 30), most urgent first |
| `get_conversation` | One conversation or topic in full, from any message link in it or its id (`t:`/`m:` for overview rows) |
| `get_channel_overview` | One compact row per topic (thread or main-channel stretch) in one channel, up to 30 days |
| `search` | Topics in one channel matching words, grouped by topic, up to 30 days |
| `get_channel_messages` | Recent messages from one visible channel |
| `get_server_overview` | Last week's discussion per channel, and deadlines due this week, across every readable channel |
| `get_open_loops` | The person's open promises, and asks, promises and threads where someone else owes the next move |

## Channel overview and search

For briefings and questions about a whole channel ("which grants still need
action"), not only what pinged the person. A topic is a thread (with its true
first message, even when older than the window) or a stretch of main-channel
messages split at a 24-hour quiet gap and capped at 72 hours. Each row carries
the opening message, the latest 2, up to 2 key messages, `quiet_days` and
`your_part` ("pinged" means a direct ping: not @everyone, and not @here or a role
ping in a `direct_only` channel); `only_mine` keeps the person's own topics.

The server spots signals mechanically and never decides "needs action":
strong signals (explicit dates, deadline/due/EOD/EOW, document links, the
channel's `watch` words from config, and `look_for` words from Claude) and weak
ones (weekday names, tomorrow, next week). Words are matched literally at the
start of a word. Nothing is learned or stored; "learning" means people add
`watch` words or tell their Claude what to look for. Rows with strong signals
are kept first under the size budget; about 25 typical threads fit in full.
Every fetch is capped at 3000 messages per channel or thread.
Design: `docs/superpowers/specs/2026-11-15-channel-overview-and-search-design.md`.

## Server-wide brief

A morning or Monday brief comes from three calls Claude makes in parallel
(the recipe is in the server instructions and in the `brief` MCP prompt):
`get_server_overview(days=7)` for last week and deadlines,
`get_my_pings(days=14)` for pending items, and `get_open_loops(days=30)` for
promises and nudges. Each call reads every readable channel and thread once,
within a per-request message budget (`server_request_messages`), and keeps
its own 40K budget. Nothing is kept between calls.

- **Last week:** one entry per active channel (message, topic and people
  counts) with its top 3 topics, strong signals first. Quiet channels are
  counted, never listed.
- **Deadlines:** a date counts only with deadline wording next to it ("by
  Friday", "due Nov 20", "20/11 deadline") or as EOD/EOW. Dates resolve from
  the day the message was posted, in the configured `timezone`; numeric dates
  follow `date_order`; dates in links are ignored; "next week" is too vague.
  Deadlines due from today to `upcoming_days` ahead are listed from messages
  of the last `deadline_days` (default 30). Broadcast (`direct_only`) channels
  are not scanned.
- **Open loops:** `your_promise` (a promise of the person's with no later
  message from them in that thread or stretch, and no reply to it),
  `unanswered_ask` (the person asked someone named directly, by mention or
  reply, and that person has posted nothing since anywhere in the channel or
  its threads), `promise_to_you` (someone promised the person and has posted
  nothing since), `stalled` (threads only: quiet for 3 days, ending on a
  question, ask or strong signal). Roles, @here and bots never owe a reply.
  Broadcast (`direct_only`) channels still give loops: real work happens in
  some of them, and leave notices are caught by wording instead. Refusals
  and absences ("I will not be able to", "I'll be on leave", "I'll be OOO",
  "I'll be back on Monday") are not promises.
- **Size:** `get_my_pings` has its own budget, `pings_max_chars` (default
  40,000, at most 200,000). When over it, announcement, handled and fyi rows go
  first; then the least urgent rows are shortened, one at a time and only as
  far as needed: texts cut (row `compact: 1`), then only the newest ping
  (`compact: 2`). Rows are dropped only when every kept row is as short as it
  gets, and are counted in `omitted`. Other tools keep the 40,000 budget.

Design: `docs/superpowers/specs/2026-11-15-server-wide-brief-design.md`.

## Status

- Stage 1 (done): fetching, matching, tools; local stdio only.
- Stage 2 (done): Discord sign-in and the full permission calculation.
- Stage 3: deployment as a remote connector. **Host requirement:** one
  always-on process: a single VM, or one container or machine with a minimum
  and maximum of 1 instance. No load-balanced replicas and no scale-to-zero
  (for example Cloud Run unless min = max = 1, or serverless functions); the
  reasons are under Sign-in.
