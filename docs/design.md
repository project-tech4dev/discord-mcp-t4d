# Design

discord-mcp is a read-only MCP server. Each person asks Claude for the Discord
messages that ping them, and the server returns only those from allowlisted
channels the person can already see in Discord.

## Flow

1. Claude calls a tool on behalf of a signed-in person.
2. The server works out the person's roles and which allowlisted channels they can see.
3. It fetches recent messages from those channels with a bot token, at the moment of the request.
4. It returns the messages that ping the person. Claude does all sorting and summarising.

There is no database and no message history. Messages exist in memory only while a request runs.

## Guarantees

- **Read-only.** The Discord client has one HTTP method, GET. No tool posts, edits, reacts or deletes.
- **No stored message text.** Nothing is written to disk, logs or outside services.
- **Minimal logs.** Each line holds only user ID, tool name, channel IDs, item count, duration and status. Every other logger is silenced, and errors are reported by type only.
- **Fail closed.** A channel that fails any lookup, returns 403, or has an unrecognised type is dropped from the result and its ID is logged.
- **Two gates.** A channel must be on the allowlist and visible to the person under Discord's permission rules.
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
  in memory, so a second worker or replica would let them be reused.
- Roles are read by the bot on every request, so losing a role or leaving the
  server takes effect immediately.

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

## Conversations

`get_my_pings` returns conversations, not raw pings. A thread is one
conversation. In a plain channel a conversation starts at a ping and follows
the people: later messages join when they reply into it or come from the
pinger, the person, or anyone the ping names, until a 12-hour quiet gap or
24 hours after it started. A message that fits several goes to the one it
replies into, else to the one with the most recent ping. @everyone
conversations take the person's messages only by reply.

Each conversation is marked `needs_you` (no reply from the person yet),
`reopened` (pinged again after their reply), `handled` (their last message
is after the latest ping) or `announcement` (only @everyone). Handled rows
carry the person's last reply so Claude can judge whether it closed the
matter. The result has a size budget (40,000 characters by default): when
it is over, oldest announcements, then oldest handled rows are dropped and
counted in `omitted`. `get_conversation` opens one conversation in full.
Design: `docs/superpowers/specs/2026-11-15-ping-conversations-design.md`.

## Tools

| Tool | Returns |
| --- | --- |
| `list_my_channels` | Allowlisted channels the person can see |
| `get_my_pings` | Conversations that ping the person from the last N days (default 7, max 14), most urgent first |
| `get_conversation` | One conversation in full, from any message link in it or its id |
| `get_channel_messages` | Recent messages from one visible channel |

## Status

- Stage 1 (done): fetching, matching, tools; local stdio only.
- Stage 2 (done): Discord sign-in and the full permission calculation.
- Stage 3: deployment as a remote connector.
