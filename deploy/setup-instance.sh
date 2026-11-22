#!/bin/sh
# Installs or updates discord-mcp on the EC2 instance. Run as root, either by
# discord-mcp-deploy.service (output to /var/log/discord-mcp/deploy.log) or by
# the first-install command (`remote.sh install`). Idempotent: the first run
# installs everything, every later run updates.
#
#   setup-instance.sh [main | <commit sha>]
#
# Never prints a parameter value and never reads one: secrets are fetched only
# by fetch-params.sh when the service starts. Never use set -x here.
set -eu
# Code and installed files must be world-readable: git writes the checkout
# with this umask and the image keeps those modes.
umask 022

REF=${1:-main}
SRC=/opt/discord-mcp/src
ETC=/etc/discord-mcp
STATE=/var/lib/discord-mcp
LOG_DIR=/var/log/discord-mcp
SYSTEMD=/etc/systemd/system
CADDY_UID=10002
KEEP_IMAGES=3

export DEBIAN_FRONTEND=noninteractive
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin"

step=start
finish() {
    rc=$?
    if [ "$rc" -ne 0 ]; then
        echo "deploy failed: $step"
    fi
}
trap finish EXIT

log() { echo "== $(date -u +%Y-%m-%dT%H:%M:%SZ) $*"; }

[ "$(id -u)" -eq 0 ] || { step="must run as root"; exit 1; }

case "$REF" in
    main) ;;
    *) printf '%s' "$REF" | grep -Eqx '[0-9a-f]{7,40}' || { step="ref must be main or a commit sha"; exit 1; } ;;
esac

# --- 1. code: fetch, check out, then re-run from the new copy once ------------
if [ -z "${DISCORD_MCP_REEXEC:-}" ]; then
    step=code
    log "code: $REF"
    [ -d "$SRC/.git" ] || { step="code: $SRC is not a git checkout (run remote.sh install)"; exit 1; }
    # Only merged code runs: the commit must be on origin/main (see that script).
    /bin/sh "$SRC/deploy/checkout-ref.sh" "$SRC" "$REF" >/dev/null
    # A change to this script takes effect in this same deploy.
    DISCORD_MCP_REEXEC=1 exec /bin/sh "$SRC/deploy/setup-instance.sh" "$REF"
fi

cd "$SRC"
SHA=$(git rev-parse --short=12 HEAD)
TAG=$SHA

# --- 2. packages, if missing --------------------------------------------------
step=packages
log "packages"
need=""
for p in docker.io docker-compose-v2 docker-buildx git curl jq unattended-upgrades iptables; do
    dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok installed" || need="$need $p"
done
if [ -n "$need" ]; then
    apt-get update -q
    # shellcheck disable=SC2086  # a list of package names
    apt-get install -y -q --no-install-recommends $need
fi
# Automatic updates must never restart Docker underneath the server.
apt-mark hold docker.io containerd runc >/dev/null
if ! command -v aws >/dev/null 2>&1; then
    snap install aws-cli --classic
fi
systemctl enable --now docker >/dev/null

# --- 3. swap: 1 GB on the encrypted root disk ---------------------------------
step=swap
if ! swapon --show=NAME --noheadings | grep -qx /swapfile; then
    log "swap"
    [ -f /swapfile ] || { fallocate -l 1G /swapfile; chmod 600 /swapfile; mkswap /swapfile >/dev/null; }
    swapon /swapfile
fi
grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab

# --- 4. no SSH: no key pair, port 22 closed, and the daemon off ---------------
step=ssh
systemctl disable --now ssh.socket ssh.service >/dev/null 2>&1 || true

# --- 5. image -----------------------------------------------------------------
step=image
if ! docker image inspect "discord-mcp:$TAG" >/dev/null 2>&1; then
    log "image: building discord-mcp:$TAG"
    docker build --pull=false -t "discord-mcp:$TAG" "$SRC"
fi

# --- 6. files -----------------------------------------------------------------
# Installs everything from the checkout in $SRC. Run again from the previous
# commit on rollback, so the old image never runs with the new files.
install_files() {
    install -d -m 0755 "$ETC"
    install -d -m 0700 "$LOG_DIR"
    install -d -m 0755 "$STATE" "$STATE/caddy"
    install -d -m 0700 -o "$CADDY_UID" -g "$CADDY_UID" "$STATE/caddy/data" "$STATE/caddy/config"
    install -m 0644 "$SRC/deploy/instance/compose.yaml" "$SRC/deploy/instance/Caddyfile" "$ETC/"
    for u in discord-mcp.service discord-mcp-deploy.service discord-mcp-imds.service \
             discord-mcp-reboot.service discord-mcp-reboot.timer; do
        install -m 0644 "$SRC/deploy/instance/$u" "$SYSTEMD/$u"
    done
    install -m 0700 "$SRC/deploy/fetch-params.sh" /usr/local/sbin/discord-mcp-fetch-params
    install -m 0700 "$SRC/deploy/imds-block.sh" /usr/local/sbin/discord-mcp-imds-block
    install -d -m 0755 /etc/systemd/system/docker.service.d
    install -m 0644 "$SRC/deploy/instance/docker-requires-imds.conf" /etc/systemd/system/docker.service.d/discord-mcp-imds.conf
    install -m 0644 "$SRC/deploy/instance/logrotate-discord-mcp" /etc/logrotate.d/discord-mcp
    install -m 0644 "$SRC/deploy/instance/52discord-mcp-unattended-upgrades" \
        /etc/apt/apt.conf.d/52discord-mcp-unattended-upgrades
    /usr/local/sbin/discord-mcp-imds-block
    systemctl daemon-reload
    systemctl enable discord-mcp.service discord-mcp-imds.service discord-mcp-reboot.timer >/dev/null 2>&1
    systemctl start discord-mcp-imds.service discord-mcp-reboot.timer
}

step=files
log "files"
install_files

# --- 7. restart on the new image ----------------------------------------------
step=restart
current=""
[ -f "$ETC/image-tag" ] && current=$(cat "$ETC/image-tag")
printf '%s' "$current" | grep -Eqx '[0-9a-f]{7,40}' || current=""
if [ -n "$current" ] && [ "$current" != "$TAG" ]; then
    cp "$ETC/image-tag" "$ETC/image-tag.prev"
fi
printf '%s\n' "$TAG" > "$ETC/image-tag"
log "restart: discord-mcp:$TAG"

healthy() {  # Caddy and the app answer over TLS, checked on this host
    domain=$(grep -E '^DOMAIN=' /run/discord-mcp/compose.env 2>/dev/null | cut -d= -f2-)
    [ -n "$domain" ] || return 1
    until_s=$(( $(date +%s) + $1 ))
    while [ "$(date +%s)" -lt "$until_s" ]; do
        if curl -sf -o /dev/null --max-time 5 --resolve "$domain:443:127.0.0.1" \
            "https://$domain/.well-known/oauth-authorization-server"; then
            return 0
        fi
        sleep 5
    done
    return 1
}

# The first start may need longer: Caddy has to get a certificate.
wait_s=60
[ -f "$STATE/.started-once" ] || wait_s=180

if systemctl restart discord-mcp && healthy "$wait_s"; then
    touch "$STATE/.started-once"
else
    step="health check failed for $TAG"
    # Roll back to what was running before this deploy: its commit (the tag is
    # the short sha), its files and its image. Nothing to roll back to on the
    # first install or when this deploy re-ran the same version.
    if [ -n "$current" ] && [ "$current" != "$TAG" ]; then
        log "rolling back to $current"
        set +e
        (
            set -e
            git -C "$SRC" checkout --quiet --force --detach "$current"  # was on main when deployed
            install_files
            printf '%s\n' "$current" > "$ETC/image-tag"
            systemctl restart discord-mcp
        )
        rc=$?
        set -e
        if [ "$rc" -eq 0 ] && healthy 60; then
            step="health check failed for $TAG; rolled back to $current, which is running"
        else
            step="health check failed for $TAG; rollback to $current also failed"
        fi
    fi
    exit 1
fi

# --- 8. keep the last few images ----------------------------------------------
step=prune
keep="$TAG"
[ -f "$ETC/image-tag.prev" ] && keep="$keep $(cat "$ETC/image-tag.prev")"
n=0
for t in $(docker image ls discord-mcp --format '{{.Tag}}'); do  # newest first
    n=$((n + 1))
    case " $keep " in *" $t "*) continue ;; esac
    [ "$n" -le "$KEEP_IMAGES" ] && continue
    docker image rm "discord-mcp:$t" >/dev/null 2>&1 || true
done
docker image prune -f >/dev/null 2>&1 || true

step=finished
echo "deploy ok $SHA"
