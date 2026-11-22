#!/bin/sh
# Installed as /usr/local/sbin/discord-mcp-imds-block; run by
# discord-mcp-imds.service at boot and by setup-instance.sh. Idempotent.
# Rejects connections to the instance metadata service from any process that
# is not root.
set -eu

add() {  # $1 = iptables or ip6tables, $2 = metadata address
    if ! "$1" -w -C OUTPUT -d "$2" -m owner ! --uid-owner 0 -j REJECT 2>/dev/null; then
        "$1" -w -I OUTPUT 1 -d "$2" -m owner ! --uid-owner 0 -j REJECT
    fi
}

add iptables 169.254.169.254
if command -v ip6tables >/dev/null 2>&1; then
    add ip6tables fd00:ec2::254
fi
