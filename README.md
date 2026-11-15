# discord-mcp

Read-only MCP server that gives each person their Discord pings from the last
7 days, limited to allowlisted channels they can already see. It stores no
message text anywhere. See [docs/design.md](docs/design.md).

**Status:** stage 2 of 3. Discord sign-in and the full permission check are
built. Runs locally; deployment as a remote connector is stage 3.

## Layout

| File | Job |
| --- | --- |
| `discord_mcp/discord_client.py` | Discord REST client. GET only, paging, rate limits, errors carry status only |
| `discord_mcp/matching.py` | Decides which messages ping a person, renders them |
| `discord_mcp/conversations.py` | Groups pings into conversations, marks each needs_you / reopened / handled / announcement, fits the result to a size budget |
| `discord_mcp/permissions.py` | Discord's permission rules: owner, Administrator, @everyone, roles, member, private threads |
| `discord_mcp/digest.py` | Runs each tool: allowlist, deny list, permissions, fetch, drop-and-log on any failure |
| `discord_mcp/oauth_provider.py` | The sign-in server Claude talks to, in front of Discord |
| `discord_mcp/discord_oauth.py` | Discord sign-in: learns the user ID, then revokes Discord's token |
| `discord_mcp/tokens.py` | Encrypted self-contained tokens; no token database |
| `discord_mcp/http_app.py` | Remote connector: Streamable HTTP with sign-in |
| `discord_mcp/server.py` | The four MCP tools (`list_my_channels`, `get_my_pings`, `get_conversation`, `get_channel_messages`), all marked read-only |
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

| Variable | Mode | Meaning |
| --- | --- | --- |
| `DISCORD_BOT_TOKEN` | both | Bot token (secret) |
| `DISCORD_GUILD_ID` | both | Your Discord server ID |
| `DIGEST_CONFIG` | both | Config file path (default `config.toml`) |
| `SERVER_NAME` | both | Name shown in Claude (default `discord-mcp`) |
| `DEV_USER_ID` | stdio | Local testing as this Discord user, no sign-in. Refused with `PUBLIC_URL` |
| `PUBLIC_URL` | http | The server's public address; https unless localhost |
| `DISCORD_CLIENT_ID` | http | Discord application ID (for sign-in) |
| `DISCORD_CLIENT_SECRET` | http | Discord application client secret (secret) |
| `OAUTH_CLIENT_ID` | http | The client ID you give Claude |
| `OAUTH_CLIENT_SECRET` | http | The client secret you give Claude (secret) |
| `OAUTH_REDIRECT_URIS` | http | Comma-separated; default `https://claude.ai/api/mcp/auth_callback` |
| `ENCRYPTION_KEY` | http | 32 random bytes, base64url (secret). Changing it signs everyone out |
| `HOST`, `PORT` | http | Listen address (default `127.0.0.1:8000`) |

Generate a key or client secret with:
`python3 -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip('='))"`

## Bot setup

1. In the Discord Developer Portal, create an application with a bot.
2. Turn on the Message Content intent. The Server Members intent is not needed.
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

## Local sign-in test (HTTP + Claude Code, macOS)

1. Discord Developer Portal → OAuth2 → Redirects: add `http://localhost:8000/oauth/discord/callback`.
2. Store the Discord client secret, a generated `OAUTH_CLIENT_SECRET` and a generated `ENCRYPTION_KEY` in the Keychain (see `scripts/with_keychain.sh`).
3. Create `.env.http.local` (git-ignored):
   ```sh
   DISCORD_GUILD_ID=...
   DIGEST_CONFIG=config.local.toml
   PUBLIC_URL=http://localhost:8000
   DISCORD_CLIENT_ID=...
   OAUTH_CLIENT_ID=claude-code-local
   OAUTH_REDIRECT_URIS=http://localhost:3118/callback
   ```
4. Start the server: `ENV_FILE=.env.http.local scripts/with_keychain.sh .venv/bin/python -m discord_mcp`
5. Add it to Claude Code (prompts for the client secret):
   `claude mcp add --transport http discord-mcp-http http://localhost:8000/mcp --client-id claude-code-local --client-secret --callback-port 3118`
6. In Claude Code, run `/mcp`, authenticate, and sign in with Discord.

## Use it in Claude Code (local, stdio)

```sh
claude mcp add discord-mcp -- "$PWD/scripts/with_keychain.sh" "$PWD/.venv/bin/python" -m discord_mcp
```

Claude's config stores only this command, not the token.

## License

MIT
