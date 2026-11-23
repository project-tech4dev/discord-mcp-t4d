# discord-mcp

Read-only MCP server that gives each person their Discord pings, channel
overviews and search, limited to permitted channels they can already see (an
allowlist, or every channel except a deny list). It stores no message text
anywhere. See [docs/design.md](docs/design.md) for the design and
[docs/decisions.md](docs/decisions.md) for why things are the way they are.

**Status:** stage 2 of 3. Discord sign-in and the full permission check are
built, and it runs locally (with Claude Code, or with claude.ai through a
tunnel). Deployment as a hosted connector is stage 3.

## Layout

| File | Job |
| --- | --- |
| `discord_mcp/discord_client.py` | Discord REST client. GET only, paging, rate limits, errors carry status only |
| `discord_mcp/messages.py` | Shared accessors for Discord messages and channels: types, time, author, links, labels |
| `discord_mcp/matching.py` | Decides which messages ping a person |
| `discord_mcp/render.py` | Shapes messages for Claude: names, readable mentions, compact and full items |
| `discord_mcp/wording.py` | Reads intent from text: asks, promises, month names |
| `discord_mcp/budget.py` | Measures results as sent and trims them to their size budget |
| `discord_mcp/brief.py` | Server-wide brief: last week per channel, deadlines due soon, open loops (promises, asks, stalled threads), size budgets |
| `discord_mcp/deadlines.py` | Finds deadline wording in text and resolves it to a date (time zone, day/month order) |
| `discord_mcp/topics.py` | Channel overview and search: topics (threads and main-channel stretches), compact rows, size budget |
| `discord_mcp/signals.py` | Spots dates, deadline words, document links and watch/look_for words |
| `discord_mcp/conversations.py` | Groups pings into conversations, marks each needs_you / reopened / check / fyi / handled / announcement, fits the result to a size budget |
| `discord_mcp/permissions.py` | Discord's permission rules: owner, Administrator, @everyone, roles, member, private threads |
| `discord_mcp/errors.py` | The errors a tool call can end with, each fixed text |
| `discord_mcp/access.py` | Both access gates: allowlist or allow-all, deny lists, Discord permissions, drop-and-log |
| `discord_mcp/reader.py` | Reads permitted channels and threads within the message budgets (one channel, or the whole server) |
| `discord_mcp/digest.py` | Runs each tool: checks input, then asks access and reader and shapes the answer |
| `discord_mcp/oauth_provider.py` | The sign-in server Claude talks to, in front of Discord |
| `discord_mcp/discord_oauth.py` | Discord sign-in: learns the user ID, then revokes Discord's token |
| `discord_mcp/tokens.py` | Encrypted self-contained tokens; no token database |
| `discord_mcp/http_app.py` | Remote connector: Streamable HTTP with sign-in |
| `discord_mcp/server.py` | The eight MCP tools (`list_my_channels`, `get_my_pings`, `get_conversation`, `get_channel_overview`, `search`, `get_channel_messages`, `get_server_overview`, `get_open_loops`), all marked read-only, and the `brief` prompt |
| `discord_mcp/audit.py` | The only log output: user ID, tool, channel IDs, count, duration, status |
| `config.toml` | Allowlist (starts empty), allow-all mode, channel deny list, per-person deny list, limits |
| `scripts/live_check.py` | Local check against real Discord; prints IDs and reasons, never text |
| `scripts/with_keychain.sh` | Runs a command with secrets from the macOS Keychain (the bot token; in HTTP mode also the sign-in secrets) |

## Local development

Everything below runs on your own machine. Pick the mode you need:

| Mode | Use it for | Sign-in |
| --- | --- | --- |
| A. Tests only | Changing code | None |
| B. Claude Code, stdio | Quick checks as yourself | None (`DEV_USER_ID`) |
| C. Claude Code, HTTP | Testing sign-in and permissions as Claude sees them | Discord |
| D. claude.ai, through a tunnel | Testing the real connector experience | Discord |

### A. Run the tests

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
```

Tests use `tests/fakes.py` (a fake Discord client and synthetic IDs), never
real data.

### One-time setup for modes B to D

**1. A Discord bot** (Developer Portal → New Application → Bot)

1. Turn on the Message Content intent. The Server Members intent is not needed.
2. Invite the bot with only View Channels and Read Message History. Never
   Administrator.
3. Private channels: the bot reads only what its roles allow. A channel a
   person can see but the bot can't shows up as `skipped_channels`.
   - Preferred: in each private channel's permissions, give the bot's own role
     View Channel and Read Message History. It gets exactly those channels.
   - Shortcut: add the bot to a team role. It then gets every channel that role
     opens, now and later, plus everything else the role allows. In allow-all
     mode those channels are read without a per-channel decision, so deny the
     ones you don't want. Use only roles without moderation powers.
   - Never give the bot Administrator, a moderator role, or an admin role.
4. Keep the bot's own role below every role that guards a private channel.
5. Private threads are mostly not covered. Without Manage Threads, Discord
   shows the bot only private threads it has joined, and only while they are
   active. Pings in any other private thread are never seen, even by the
   thread's members. Giving the bot Manage Threads would fix this but also
   lets the token archive, lock and delete threads, so it is not granted (see
   `docs/decisions.md`, issue #13). Use a private channel when the connector
   should see the discussion.

**2. Secrets in the macOS Keychain** (each command prompts you to paste the value)

```sh
security add-generic-password -s discord-mcp-bot -a "$USER" -w             # bot token
security add-generic-password -s discord-mcp-discord-secret -a "$USER" -w  # Discord app client secret (C, D)
security add-generic-password -s discord-mcp-oauth-secret -a "$USER" -w    # client secret you give Claude (C, D)
security add-generic-password -s discord-mcp-encryption-key -a "$USER" -w  # token encryption key (C, D)
```

Generate the last two with
`python3 -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip('='))"`.
`scripts/with_keychain.sh` reads them at start-up, so they never sit in the
repo, env files or shell history. (Claude Code and claude.ai each keep their
own copy of the client secret once you give it to them.)

**3. `config.local.toml`** (git-ignored; `config.toml` explains every setting in a line)

Copy `config.toml` to `config.local.toml` and edit the values you need, so
every setting and its explanation sit in one file. Start with one test
channel:

```toml
allowlist = [ { id = "CHANNEL_ID_1", name = "test", phase = 1 } ]
```

When `config.toml` gains a setting or new explanations, bring your copy up to
date without losing your values:

```sh
.venv/bin/python -m scripts.sync_local_config          # rebuilds it; old copy in config.local.toml.bak
.venv/bin/python -m scripts.sync_local_config --check  # only says whether it is out of date
```

It marks each value that differs from the default with a
`# CUSTOM (default: ...)` line, keeps comment lines starting `# NOTE` above a
setting, and writes nothing unless the server would load the new file exactly
as it loads the old one. It never changes a value: a setting in your copy keeps
its value even when the default changes later, and is then marked `CUSTOM`.
Review the `CUSTOM` lines after each run and delete a setting to take the new
default. Each run replaces the previous `.bak`. A short file with only the
settings you change also works; anything left out uses the default.

The values a fuller setup changes, once you're ready to read more of the server:

```toml
allow_all_channels = false         # true reads every channel except deny_channels
allowed_roles = ["ROLE_ID"]        # only members with one of these roles; [] for everyone
deny_channels = [
  { id = "CATEGORY_OR_CHANNEL_ID", note = "leadership" },
]
allowlist = [
  { id = "CHANNEL_ID_1", name = "events", phase = 1, watch = ["venue", "agenda", "rsvp", "deadline"] },
  { id = "CHANNEL_ID_2", name = "leave", phase = 1, pings = "direct_only" },
]
deny = []
```

- Turn on `allow_all_channels` only with the server owner's agreement, and
  deny private spaces (leadership, HR) first.
- `deny_channels` takes channel, category or thread IDs and always wins.
- `allowed_roles` limits who may sign in and use the tools (for example
  staff, not partners). Leave it empty to let every server member in.
- Each person may make `calls_per_minute` tool calls (default 30) in any
  minute; more get "Too many requests".
- `watch` adds words that mark a message as worth a look in the overview.
- `pings = "direct_only"` is for broadcast channels (leave notices, social,
  job posts): @here and role pings there count as announcements, not "needs
  you". The channel is still read for the overview, search and open loops.
- Config is read at start-up: restart the server after changing it.

### B. Claude Code over stdio (no sign-in)

Create `.env.local` (git-ignored):

```sh
DISCORD_GUILD_ID=...
DEV_USER_ID=...            # your Discord user ID; you act as this person
DIGEST_CONFIG=config.local.toml
```

Then add it to Claude Code (Claude's config stores only this command, not the token):

```sh
claude mcp add discord-mcp -- "$PWD/scripts/with_keychain.sh" "$PWD/.venv/bin/python" -m discord_mcp
```

For a quick check without Claude, `scripts/with_keychain.sh .venv/bin/python -m scripts.live_check`
prints the IDs and reasons of your pings, never message text.

### C. Claude Code over HTTP, with Discord sign-in

1. Discord Developer Portal → OAuth2 → Redirects: add
   `http://localhost:8000/oauth/discord/callback`.
2. Create `.env.http.local` (git-ignored):
   ```sh
   DISCORD_GUILD_ID=...
   DIGEST_CONFIG=config.local.toml
   PUBLIC_URL=http://localhost:8000
   DISCORD_CLIENT_ID=...
   OAUTH_CLIENT_ID=claude-code-local
   OAUTH_REDIRECT_URIS=http://localhost:3118/callback
   ```
   The server listens on `127.0.0.1` by default. If Claude Code reports
   `ECONNREFUSED` while the server is running, your `localhost` resolves to
   IPv6: add `HOST=::1` (IPv6 loopback). Never use `HOST=::` or `0.0.0.0`,
   which listen on every network interface and expose the server over plain
   HTTP to your network.
3. Start the server (leave it running; its terminal shows the audit lines):
   ```sh
   ENV_FILE=.env.http.local scripts/with_keychain.sh .venv/bin/python -m discord_mcp
   ```
4. Add it to Claude Code (prompts for the client secret):
   ```sh
   claude mcp add --transport http discord-mcp-http http://localhost:8000/mcp --client-id claude-code-local --client-secret --callback-port 3118
   ```
5. In Claude Code, run `/mcp`, authenticate, and sign in with Discord.

Run exactly one server process, always on: no replicas and no
scale-to-zero hosting (see "Status" in `docs/design.md`).

After a restart, sign-in codes and refresh tokens from before stop working,
so Claude asks you to sign in again once its current access token (up to an
hour) expires. A restart is not a way to cut access off immediately; changing
`ENCRYPTION_KEY` is.

### D. claude.ai through a Tailscale Funnel

claude.ai connects to MCP servers from Anthropic's cloud, not from your
browser, so `localhost` is not reachable. A tunnel gives your machine a public
HTTPS address. Tailscale Funnel is recommended because the encryption ends on
your machine: Tailscale cannot read the traffic, which carries message text.
(ngrok and Cloudflare tunnels also work but decrypt traffic at their edge.)

One-time:

1. Install the Tailscale app and sign in. In the admin console, rename your
   machine to something neutral (for example `discord-mcp`): the name is part
   of the public address, and Funnel addresses appear in public certificate
   logs.
   The tailnet part of the address is published too, so avoid a tailnet name
   that identifies you.
2. Run `tailscale funnel 8000` once. If Funnel is off for your tailnet, it
   prints a link: open it and enable Funnel (and HTTPS certificates). Once it
   is enabled the command starts the tunnel in the foreground: stop it with
   Ctrl-C for now.
3. Note your address, `https://<machine>.<tailnet>.ts.net`.
4. Discord Developer Portal → OAuth2 → Redirects: add
   `https://<machine>.<tailnet>.ts.net/oauth/discord/callback`.
5. Copy `.env.http.local` to `.env.funnel.local` and change two lines:
   ```sh
   PUBLIC_URL=https://<machine>.<tailnet>.ts.net
   OAUTH_REDIRECT_URIS=https://claude.ai/api/mcp/auth_callback,https://claude.com/api/mcp/auth_callback
   ```
   (`ENV_FILE` is a path relative to the repo.)

Each session, in one terminal, keep the Mac awake (on battery `-s` does not
apply, so stay plugged in) and start the server:

```sh
caffeinate -dis &
ENV_FILE=.env.funnel.local scripts/with_keychain.sh .venv/bin/python -m discord_mcp
```

In a second terminal, open the tunnel:

```sh
tailscale funnel --bg 8000            # or: tailscale funnel --bg "http://[::1]:8000" if you set HOST=::1
```

Funnel must point at the address the server listens on; a mismatch gives a
502.

In claude.ai: Settings → Connectors → Add custom connector, URL
`https://<machine>.<tailnet>.ts.net/mcp`. Authentication is detected as OAuth.
Under the advanced settings give the client ID and client secret. The client
ID is not secret: `grep '^OAUTH_CLIENT_ID=' .env.funnel.local`. Copy the
secret without showing it on screen, paste it, then clear the clipboard:

```sh
security find-generic-password -s discord-mcp-oauth-secret -a "$USER" -w | tr -d '\n' | pbcopy
# paste into claude.ai, then:
pbcopy </dev/null
```

Then connect and sign in with Discord.

When you're done:

```sh
tailscale funnel --https=443 off      # closes the public address
# stop the server with Ctrl-C, and the caffeinate job with `kill %1`
```

While the tunnel is open the server is reachable from the internet. Everyone
must still sign in with Discord and sees only what their own Discord access
allows, but per-person rate limits are not built yet (stage 3), so keep the
tunnel open only while testing. Closing the tunnel stops new requests; access
tokens already issued stay valid for up to an hour if the server comes back
with the same `ENCRYPTION_KEY`.

### Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| Claude Code: `ECONNREFUSED` | Server not running, a typo in `PUBLIC_URL`, or `localhost` resolving to IPv6 (set `HOST=::1`) |
| `/mcp` asks you to sign in again | The server restarted; this is expected |
| Claude Code: tool result too large | Results above about 25,000 tokens are refused. Raise `MAX_MCP_OUTPUT_TOKENS` in Claude Code's environment (for example 50000), or lower `pings_max_chars`. Larger results put more untrusted message text into Claude's context |
| `skipped_channels` above 0 | Channels or threads you can see but the bot couldn't read (no access, or a Discord error): give the bot's role access, deny them, or retry |
| `rate_limited_channels` in a result | Discord was busy and refused that many channels even after waiting. Most are also in `skipped_channels` (audit: `dropped:http_429`); a busy channel whose extra read was refused is shown in part and is not (audit: `dropped:partial_http_429`). Ask again in a minute |
| claude.ai: "Couldn't reach this address" | Funnel not running, pointing at a different address than the server listens on (502), or a brand-new address still spreading through DNS (wait about 10 minutes) |
| claude.ai: connector stops responding | The tunnel dropped (sleep, Wi-Fi change). Check `tailscale funnel status` and whether calls appear in the server's audit lines |
| Sign-in fails after Discord | The redirect for this `PUBLIC_URL` is missing in the Discord portal, or `OAUTH_REDIRECT_URIS` lacks the caller's callback |

The server's only log output is one JSON audit line per call: user ID, tool,
channel IDs, item count, duration and status. No message text. A call that
never shows up there never reached the server.

## Settings (environment variables only)

| Variable | Mode | Meaning |
| --- | --- | --- |
| `DISCORD_BOT_TOKEN` | stdio + http | Bot token (secret) |
| `DISCORD_GUILD_ID` | stdio + http | Your Discord server ID |
| `DIGEST_CONFIG` | stdio + http | Config file path (default `config.toml`) |
| `SERVER_NAME` | stdio + http | Name shown in Claude (default `discord-mcp`) |
| `DEV_USER_ID` | stdio | Local testing as this Discord user, no sign-in. Refused with `PUBLIC_URL` |
| `PUBLIC_URL` | http | The server's public address; https unless localhost |
| `DISCORD_CLIENT_ID` | http | Discord application ID (for sign-in) |
| `DISCORD_CLIENT_SECRET` | http | Discord application client secret (secret) |
| `OAUTH_CLIENT_ID` | http | The client ID you give Claude |
| `OAUTH_CLIENT_SECRET` | http | The client secret you give Claude (secret) |
| `OAUTH_REDIRECT_URIS` | http | Comma-separated; default `https://claude.ai/api/mcp/auth_callback` |
| `ENCRYPTION_KEY` | http | 32 random bytes, base64url (secret). Changing it signs everyone out |
| `HOST`, `PORT` | http | Listen address (default `127.0.0.1:8000`; `::1` if `localhost` resolves to IPv6). Keep it a loopback address |

## Contributing

Read [CLAUDE.md](CLAUDE.md) first. In short:

- Write the test first (red, then green), using `tests/fakes.py`.
- Before every commit: the full suite passes, a code review and a security
  review have run, their Critical and Important findings are fixed, and the
  commit message says what they found.
- Record major decisions in `docs/decisions.md` in the same commit.
- This is a public repo: never commit real names, channel or guild IDs,
  message text, `.env*` files or `config.local.toml`.
- Error text shown to Claude is fixed text, never message content.

## Deployment

**Phase 1 (ready):** the team's shared connector runs on one EC2 instance in
the team's AWS account, behind Caddy with a Let's Encrypt certificate.
[`deploy/README.md`](deploy/README.md) has the checklist for creating the AWS
resources by hand (including the instance role's IAM policy, which can read
only the six `/discord-mcp/*` parameters), the first install, the runbook and
every log the deployment keeps. Teammates deploy merged code with
`deploy/remote.sh` through SSM Run Command; there is no SSH.

The audit lines in CloudWatch record who called which tool on which
channels, and when (no message text), for 30 days.

**Phase 2 (planned):** Terraform and GitHub Actions, with images built in CI.
Top-level design and open questions:
[issue #21](https://github.com/project-tech4dev/discord-mcp-t4d/issues/21).

Design: `docs/superpowers/specs/2026-11-22-aws-deployment-design.md`.

## License

MIT
