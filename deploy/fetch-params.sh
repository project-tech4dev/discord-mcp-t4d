#!/bin/sh
# Runs as root before every start (ExecStartPre of discord-mcp.service).
# Reads the six /discord-mcp/* parameters from SSM with the instance role and
# writes, into the tmpfs /run/discord-mcp (created mode 0700 by systemd):
#
#   secrets.env  0400, app UID  the four secrets, KEY=value lines
#   config.toml  0400, app UID  contents of the config parameter
#   compose.env  0400, root     non-secret settings for Compose, plus
#                               IMAGE_TAG and AWS_REGION
#
# Never prints a value: AWS CLI errors go to /dev/null and every message is
# fixed text, naming at most a parameter or setting name. Never use set -x.
set -eu
umask 077

PREFIX=/discord-mcp
# Overridable only so the tests can run it; systemd sets neither.
RUN_DIR=${DISCORD_MCP_RUN_DIR:-/run/discord-mcp}
IMAGE_TAG_FILE=${DISCORD_MCP_IMAGE_TAG_FILE:-/etc/discord-mcp/image-tag}
APP_UID=10001
SECRET_NAMES="DISCORD_BOT_TOKEN DISCORD_CLIENT_SECRET OAUTH_CLIENT_SECRET ENCRYPTION_KEY"
# Non-secret settings the env parameter may hold. Anything else fails the start.
ENV_KEYS="DOMAIN PUBLIC_URL DISCORD_GUILD_ID DISCORD_CLIENT_ID OAUTH_CLIENT_ID OAUTH_REDIRECT_URIS SERVER_NAME"
ENV_REQUIRED="DOMAIN PUBLIC_URL DISCORD_GUILD_ID DISCORD_CLIENT_ID OAUTH_CLIENT_ID"

fail() {
    echo "fetch-params: $1" >&2
    exit 1
}

AWS=$(command -v aws || true)
[ -n "$AWS" ] || AWS=/snap/bin/aws
[ -x "$AWS" ] || fail "aws CLI not found"
[ -d "$RUN_DIR" ] || fail "$RUN_DIR does not exist (start through systemd)"

# --- the metadata block must be in place (see imds-block.sh) -----------------
# Checked on every start, so a flushed firewall stops the server instead of
# letting the containers reach the instance role.
iptables -w -C OUTPUT -d 169.254.169.254 -m owner ! --uid-owner 0 -j REJECT 2>/dev/null \
    || fail "instance metadata block is missing (run discord-mcp-imds-block)"
if command -v ip6tables >/dev/null 2>&1; then
    ip6tables -w -C OUTPUT -d fd00:ec2::254 -m owner ! --uid-owner 0 -j REJECT 2>/dev/null \
        || fail "instance metadata block is missing for IPv6 (run discord-mcp-imds-block)"
fi

# --- image tag (on disk, so a reboot brings back the same version) ------------
[ -r "$IMAGE_TAG_FILE" ] || fail "no image tag yet: run a deploy first"
IMAGE_TAG=$(cat "$IMAGE_TAG_FILE")
printf '%s' "$IMAGE_TAG" | grep -Eqx '[0-9a-f]{7,40}' || fail "image tag file is malformed"

# --- region from instance metadata (root only: see the IMDS rule) -------------
IMDS_TOKEN=$(curl -sf --max-time 5 -X PUT http://169.254.169.254/latest/api/token \
    -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' 2>/dev/null) || fail "instance metadata unreachable"
AWS_REGION=$(curl -sf --max-time 5 -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" \
    http://169.254.169.254/latest/meta-data/placement/region 2>/dev/null) || fail "region unknown"
unset IMDS_TOKEN
printf '%s' "$AWS_REGION" | grep -Eqx '[a-z]{2}(-[a-z]+)+-[0-9]' || fail "region is malformed"

# --- the six parameters, by exact name (the role refuses by-path reads) -------
NAMES=""
for n in $SECRET_NAMES config env; do NAMES="$NAMES $PREFIX/$n"; done
# shellcheck disable=SC2086  # NAMES is a fixed list of parameter names
PARAMS=$("$AWS" ssm get-parameters --region "$AWS_REGION" --with-decryption \
    --names $NAMES --output json 2>/dev/null) || fail "could not read parameters from SSM"

missing=$(printf '%s' "$PARAMS" | jq -r '.InvalidParameters | join(" ")') || fail "unreadable SSM response"
[ -z "$missing" ] || fail "missing parameters: $missing"

value_of() {  # prints one parameter's value, never to a terminal or log
    printf '%s' "$PARAMS" | jq -r --arg n "$PREFIX/$1" '.Parameters[] | select(.Name == $n) | .Value'
}

STAGE=$(mktemp -d "$RUN_DIR/.stage.XXXXXX")
trap 'rm -rf "$STAGE"' EXIT

# --- secrets.env: four single-line, non-empty values --------------------------
: > "$STAGE/secrets.env"
for n in $SECRET_NAMES; do
    printf '%s' "$PARAMS" | jq -e --arg n "$PREFIX/$n" \
        '.Parameters[] | select(.Name == $n) | .Value | (length > 0) and (test("[\r\n]") | not)' \
        >/dev/null || fail "parameter $PREFIX/$n is empty or has more than one line"
    printf '%s=%s\n' "$n" "$(value_of "$n")" >> "$STAGE/secrets.env"
done

# --- config.toml ---------------------------------------------------------------
value_of config > "$STAGE/config.toml"
[ -s "$STAGE/config.toml" ] || fail "parameter $PREFIX/config is empty"

# --- compose.env: allowlisted KEY=value lines, values from a safe character set -
value_of env > "$STAGE/env.raw"
: > "$STAGE/compose.env"
seen=" "
lineno=0
while IFS= read -r line || [ -n "$line" ]; do
    lineno=$((lineno + 1))
    case "$line" in '' | '#'*) continue ;; esac
    key=${line%%=*}
    val=${line#*=}
    [ "$key" != "$line" ] || fail "env parameter line $lineno is not KEY=value"
    printf '%s' "$key" | grep -Eqx '[A-Z_]+' || fail "env parameter line $lineno has an unknown name"
    case " $ENV_KEYS " in *" $key "*) ;; *) fail "env parameter line $lineno has an unknown name" ;; esac
    case "$seen" in *" $key "*) fail "env parameter sets $key twice" ;; esac
    # No quotes, $, #, backslashes or spaces: Compose would interpret them.
    printf '%s' "$val" | grep -Eqx '[A-Za-z0-9._:/,@+-]+' || fail "env parameter $key has an empty or unsafe value"
    seen="$seen$key "
    printf '%s=%s\n' "$key" "$val" >> "$STAGE/compose.env"
done < "$STAGE/env.raw"
rm -f "$STAGE/env.raw"

for k in $ENV_REQUIRED; do
    case "$seen" in *" $k "*) ;; *) fail "env parameter is missing $k" ;; esac
done
domain=$(grep -E '^DOMAIN=' "$STAGE/compose.env" | cut -d= -f2-)
public_url=$(grep -E '^PUBLIC_URL=' "$STAGE/compose.env" | cut -d= -f2-)
printf '%s' "$domain" | grep -Eqx '[a-z0-9]([a-z0-9.-]*[a-z0-9])?' || fail "DOMAIN must be a lower-case host name"
[ "$public_url" = "https://$domain" ] || fail "PUBLIC_URL must be https://\$DOMAIN"
printf 'IMAGE_TAG=%s\nAWS_REGION=%s\n' "$IMAGE_TAG" "$AWS_REGION" >> "$STAGE/compose.env"

# --- install atomically -------------------------------------------------------
chown "$APP_UID:$APP_UID" "$STAGE/secrets.env" "$STAGE/config.toml"
chmod 0400 "$STAGE/secrets.env" "$STAGE/config.toml" "$STAGE/compose.env"
mv -f "$STAGE/secrets.env" "$STAGE/config.toml" "$STAGE/compose.env" "$RUN_DIR/"
unset PARAMS
echo "fetch-params: ok"
