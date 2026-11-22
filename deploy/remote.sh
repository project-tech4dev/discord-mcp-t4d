#!/bin/sh
# Runs on a teammate's machine. Operates the discord-mcp instance through SSM
# Run Command (AWS-RunShellScript, root, in the background): no SSH and no
# session.
#
#   remote.sh install             first install: clone the repo, run setup
#   remote.sh deploy [main|<sha>] deploy merged code (default: main)
#   remote.sh restart             restart, re-reading SSM (secrets, channels)
#   remote.sh status              unit states, running image, uptime, memory
#   remote.sh logs [n]            last n lines of deploy.log (default 50)
#   remote.sh caddy-logs [n]      last n lines of Caddy's log (default 50)
#
# AWS profile and region come from the environment or from the git-ignored
# deploy/.remote.local (AWS_PROFILE=..., AWS_REGION=...). The instance is
# found by its tag Name=discord-mcp, so no instance ID is in the repo.
#
# SSM keeps every command's text and output in its history, so a command may
# contain only fixed text plus a validated ref or number, never a value. That
# is checked here before anything is sent.
set -eu

REPO_URL=https://github.com/project-tech4dev/discord-mcp-t4d.git
INSTANCE_TAG=discord-mcp
LOG=/var/log/discord-mcp/deploy.log

die() {
    echo "remote.sh: $1" >&2
    exit 2
}

usage() {
    sed -n '6,11p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 2
}

here=$(cd "$(dirname "$0")" && pwd)
if [ -f "$here/.remote.local" ]; then
    # shellcheck disable=SC1091  # optional, git-ignored
    . "$here/.remote.local"
    export AWS_PROFILE AWS_REGION 2>/dev/null || true
fi

lines() {  # validates a line count: digits only, 1 to 1000
    n=${1:-50}
    case "$n" in '' | *[!0-9]*) die "n must be a number from 1 to 1000" ;; esac
    [ "${#n}" -le 4 ] || die "n must be a number from 1 to 1000"
    n=$(printf '%s' "$n" | sed 's/^0*//')  # no leading zeros (sh reads them as octal)
    if [ -z "$n" ] || [ "$n" -gt 1000 ]; then die "n must be a number from 1 to 1000"; fi
    echo "$n"
}

cmd=${1:-}
[ $# -gt 0 ] && shift
timeout=600
case "$cmd" in
    install)
        [ $# -eq 0 ] || usage
        timeout=3600
        remote="test -d /opt/discord-mcp/src/.git || git clone --quiet --no-tags --single-branch --branch main $REPO_URL /opt/discord-mcp/src; mkdir -p -m 0700 /var/log/discord-mcp; /bin/sh /opt/discord-mcp/src/deploy/setup-instance.sh main >>$LOG 2>&1; rc=\$?; tail -n 30 $LOG; exit \$rc"
        ;;
    deploy)
        [ $# -le 1 ] || usage
        ref=${1:-main}
        case "$ref" in
            main) ;;
            *) printf '%s' "$ref" | grep -Eqx '[0-9a-f]{7,40}' || die "ref must be main or a commit sha (7-40 hex characters)" ;;
        esac
        timeout=3600
        # systemctl start would silently join a deploy already running, and
        # report its result as this one's.
        remote="if systemctl is-active --quiet discord-mcp-deploy; then echo 'a deploy is already running'; exit 1; fi; echo $ref > /etc/discord-mcp/deploy-ref && systemctl start discord-mcp-deploy; rc=\$?; tail -n 30 $LOG; exit \$rc"
        ;;
    restart)
        [ $# -eq 0 ] || usage
        remote="systemctl restart discord-mcp; rc=\$?; systemctl is-active discord-mcp; journalctl -u discord-mcp -n 20 --no-pager -o cat; exit \$rc"
        ;;
    status)
        [ $# -eq 0 ] || usage
        remote="for u in discord-mcp discord-mcp-deploy discord-mcp-reboot.timer; do echo \"\$u: \$(systemctl is-active \$u)\"; done; echo \"image: \$(cat /etc/discord-mcp/image-tag 2>/dev/null)\"; docker ps --format '{{.Names}}: {{.Status}}'; uptime; free -m"
        ;;
    logs)
        [ $# -le 1 ] || usage
        n=$(lines "${1:-}")
        remote="tail -n $n $LOG"
        ;;
    caddy-logs)
        [ $# -le 1 ] || usage
        n=$(lines "${1:-}")
        remote="docker logs --tail $n discord-mcp-caddy 2>&1"
        ;;
    *)
        usage
        ;;
esac

command -v aws >/dev/null 2>&1 || die "the AWS CLI is not installed"

instances=$(aws ec2 describe-instances \
    --filters "Name=tag:Name,Values=$INSTANCE_TAG" "Name=instance-state-name,Values=running" \
    --query 'Reservations[].Instances[].InstanceId' --output text) || die "could not list instances"
# shellcheck disable=SC2086  # split the instance IDs into words
set -- $instances
[ $# -eq 1 ] || die "expected one running instance tagged Name=$INSTANCE_TAG, found $#"
instance=$1

# The command as JSON. It contains only fixed text and the validated ref or n,
# so it needs no escaping beyond quotes and backslashes.
json_cmd=$(printf '%s' "$remote" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')
params=$(printf '{"commands":["%s"],"executionTimeout":["%s"]}' "$json_cmd" "$timeout")

command_id=$(aws ssm send-command --instance-ids "$instance" \
    --document-name AWS-RunShellScript --comment "discord-mcp $cmd" \
    --timeout-seconds 600 --parameters "$params" \
    --query Command.CommandId --output text) || die "could not send the command"
echo "sent $cmd ($command_id); waiting..." >&2

# Give up a little after the command's own timeout, so a broken connection
# can't keep this loop running forever.
deadline=$(( $(date +%s) + timeout + 120 ))
while :; do
    sleep 5
    status=$(aws ssm get-command-invocation --command-id "$command_id" --instance-id "$instance" \
        --query Status --output text 2>/dev/null) || status=Pending
    case "$status" in
        Pending | InProgress | Delayed | Cancelling) ;;
        *) break ;;
    esac
    if [ "$(date +%s)" -gt "$deadline" ]; then
        echo "$cmd: no result after ${timeout}s; check with remote.sh status" >&2
        exit 1
    fi
done

aws ssm get-command-invocation --command-id "$command_id" --instance-id "$instance" \
    --query StandardOutputContent --output text
err=$(aws ssm get-command-invocation --command-id "$command_id" --instance-id "$instance" \
    --query StandardErrorContent --output text)
[ -z "$err" ] || printf '%s\n' "$err" >&2
echo "$cmd: $status" >&2
[ "$status" = Success ]
