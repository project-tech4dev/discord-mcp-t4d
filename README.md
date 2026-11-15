# discord-mcp

Read-only MCP server that gives each person their Discord pings from the last
7 days, limited to allowlisted channels they can already see. It stores no
message text anywhere. See [docs/design.md](docs/design.md).

**Status:** stage 1 of 3. Runs locally over stdio as the user in `DEV_USER_ID`.
Sign-in and the full permission check arrive in stage 2.

## Layout

| File | Job |
| --- | --- |
| `discord_mcp/discord_client.py` | Discord REST client. GET only, paging, rate limits, errors carry status only |
| `discord_mcp/matching.py` | Decides which messages ping a person, renders them |
| `discord_mcp/digest.py` | Runs each tool: channels, threads, fetch, drop-and-log on any failure |
| `discord_mcp/server.py` | The three MCP tools, all marked read-only |
| `discord_mcp/audit.py` | The only log output: user ID, tool, channel IDs, count, duration, status |
| `config.toml` | Allowlist (starts empty), deny list, limits |
| `scripts/live_check.py` | Local check against real Discord; prints IDs and reasons, never text |
| `scripts/with_keychain.sh` | Runs a command with the bot token from the macOS Keychain |

## Run the tests

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

## Settings (environment variables only)

| Variable | Meaning |
| --- | --- |
| `DISCORD_BOT_TOKEN` | Bot token (keep it in a secret store, not a file) |
| `DISCORD_GUILD_ID` | Your Discord server ID |
| `DEV_USER_ID` | Stage 1 only: your Discord user ID. Refused if `PUBLIC_URL` is set |
| `DIGEST_CONFIG` | Config file path (default `config.toml`) |
| `SERVER_NAME` | Name shown in Claude (default `discord-mcp`) |

## Bot setup

1. In the Discord Developer Portal, create an application with a bot.
2. Turn on the Message Content intent.
3. Invite the bot with only View Channels and Read Message History. Never Administrator.
4. Place the bot's role below every role that guards a private channel.
5. Give the bot role access to each allowlisted private channel.

## Local live check (macOS)

1. Store the token once: `security add-generic-password -s discord-mcp-bot -a "$USER" -w`
2. Create `.env.local` (git-ignored) with `DISCORD_GUILD_ID`, `DEV_USER_ID`, `DIGEST_CONFIG=config.local.toml` and optionally `SERVER_NAME`.
3. Create `config.local.toml` (git-ignored) with your test channel:
   ```toml
   allowlist = [ { id = "CHANNEL_ID", name = "test", phase = 1 } ]
   deny = []
   ```
4. Have someone else ping you there. Your own messages never count.
5. Run `scripts/with_keychain.sh .venv/bin/python -m scripts.live_check`

## Use it in Claude Code (local, stdio)

```sh
claude mcp add discord-mcp -- "$PWD/scripts/with_keychain.sh" "$PWD/.venv/bin/python" -m discord_mcp
```

Claude's config stores only this command, not the token.

## License

MIT
