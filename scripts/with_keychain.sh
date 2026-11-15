#!/bin/sh
# Runs a command with secrets read from the macOS Keychain, so they never
# appear in shell history, files or Claude's config. Non-secret settings come
# from an env file (default .env.local; set ENV_FILE to choose another).
#
# Store each secret once (you will be prompted to paste it):
#   security add-generic-password -s discord-mcp-bot -a "$USER" -w             # bot token
#   security add-generic-password -s discord-mcp-discord-secret -a "$USER" -w  # Discord app client secret
#   security add-generic-password -s discord-mcp-oauth-secret -a "$USER" -w    # secret Claude uses
#   security add-generic-password -s discord-mcp-encryption-key -a "$USER" -w  # 32-byte base64url key
set -eu
cd "$(dirname "$0")/.."
ENV_FILE="${ENV_FILE:-.env.local}"
if [ -f "$ENV_FILE" ]; then
  set -a
  . "./$ENV_FILE"
  set +a
fi
secret() {
  security find-generic-password -s "$1" -a "$USER" -w 2>/dev/null || true
}
DISCORD_BOT_TOKEN="$(secret discord-mcp-bot)"
export DISCORD_BOT_TOKEN
if [ -n "${PUBLIC_URL:-}" ]; then
  DISCORD_CLIENT_SECRET="$(secret discord-mcp-discord-secret)"
  OAUTH_CLIENT_SECRET="$(secret discord-mcp-oauth-secret)"
  ENCRYPTION_KEY="$(secret discord-mcp-encryption-key)"
  export DISCORD_CLIENT_SECRET OAUTH_CLIENT_SECRET ENCRYPTION_KEY
fi
exec "$@"
