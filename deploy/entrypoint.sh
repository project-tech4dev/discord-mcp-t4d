#!/bin/sh
# Image entrypoint. Reads the four secrets from the read-only file mounted by
# compose.yaml into this process's environment, then runs the server.
# Secrets are never passed with Compose environment or env_file, because Docker
# saves those in its container config on disk. Values are read literally
# (never sourced or eval'd), and no value is ever printed.
set -eu

SECRETS=${DISCORD_MCP_SECRETS:-/run/secrets/discord-mcp.env}

if [ ! -r "$SECRETS" ]; then
    echo "fatal: secrets file is missing" >&2
    exit 2
fi

while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
        '' | '#'*) continue ;;
        *=*) ;;
        *) echo "fatal: malformed line in secrets file" >&2; exit 2 ;;
    esac
    key=${line%%=*}
    value=${line#*=}
    case "$key" in
        DISCORD_BOT_TOKEN | DISCORD_CLIENT_SECRET | OAUTH_CLIENT_SECRET | ENCRYPTION_KEY)
            export "$key=$value"
            ;;
        *)
            echo "fatal: unexpected name in secrets file" >&2
            exit 2
            ;;
    esac
done < "$SECRETS"
unset line key value

exec python -m discord_mcp
