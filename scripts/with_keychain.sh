#!/bin/sh
# Runs a command with the bot token read from the macOS Keychain, so the token
# never appears in shell history or in Claude's config. Non-secret IDs come
# from .env.local (git-ignored).
#
# Store the token once (you will be prompted to paste it):
#   security add-generic-password -s discord-mcp-bot -a "$USER" -w
set -eu
cd "$(dirname "$0")/.."
if [ -f .env.local ]; then
  set -a
  . ./.env.local
  set +a
fi
DISCORD_BOT_TOKEN="$(security find-generic-password -s discord-mcp-bot -a "$USER" -w)"
export DISCORD_BOT_TOKEN
exec "$@"
