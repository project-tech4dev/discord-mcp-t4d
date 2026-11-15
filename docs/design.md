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

## What counts as a ping

A message pings a person when Discord's own fields show a mention of them, a
mention of a role they hold, @everyone, @here, or a reply to one of their
messages. Their own messages never count. Each ping carries a reason and a
follow_up with the conversation after it.

## Tools

| Tool | Returns |
| --- | --- |
| `list_my_channels` | Allowlisted channels the person can see |
| `get_my_pings` | Pings from the last N days (default 7, max 14), newest first, with follow-up |
| `get_channel_messages` | Recent messages from one visible channel |

## Status

- Stage 1 (done): fetching, matching, tools; local stdio only.
- Stage 2: Discord sign-in and the full permission calculation.
- Stage 3: deployment as a remote connector.
