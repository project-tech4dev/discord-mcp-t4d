# AWS deployment (stage 3) — design

Status: phase 1 built (see `deploy/README.md`); phase 2 planned in issue
#21. Date: 2026-11-22.

**As built, differences from this design:** the IMDS rule is re-applied by a
boot unit instead of `iptables-persistent`; the Sunday reboot is a systemd
timer that reboots only when an update needs it (unattended-upgrades never
reboots itself); the AWS CLI comes from the snap; the instance policy reads
the six parameters by exact ARN (not `discord-mcp/*`); the health check uses
`curl --resolve` to 127.0.0.1; the code step runs first, so the newest script
installs the packages; `remote.sh install` does the first install; and
`.dockerignore` is an allowlist (`requirements.txt`, `discord_mcp/`,
`deploy/entrypoint.sh`) rather than a list of exclusions. See
`docs/decisions.md`.

## Intent

Run discord-mcp as a shared remote connector for the team, hosted in the
team's existing AWS account. People add it in claude.ai and stop depending on
one laptop behind Tailscale Funnel. The team runs it, not one person.

The work comes in two phases:

- **Phase 1 (this design, in detail):**
  - The AWS resources are created by hand, following a checklist.
  - One idempotent script on the instance, `deploy/setup-instance.sh`,
    installs, updates and restarts the server.
  - Teammates trigger it from their own machines with one command, which
    runs in the background through SSM Run Command.
  - The goal is to learn how running and changing it really goes before
    automating anything.
- **Phase 2 (later, outlined at the end):** Terraform and GitHub Actions
  replace the hand steps and the trigger command.

Everything in phase 1 that is about security, rather than automation, is
built properly from day one.

## Goals

- One always-on server process on one EC2 instance, at
  `https://<subdomain>.<team domain>` (DNS in Cloudflare).
- Keep every guarantee in `docs/design.md`: read-only, no stored message text,
  minimal audit logs, fail closed, both access gates, secrets from the
  environment only.
- Encryption ends on the instance, as it does with Funnel today.
- Everything on the instance runs as systemd services. Nothing needs an
  open SSM session or a logged-in person: the server, its restarts, its
  start after a reboot, and deploys.
- A teammate deploys, restarts, or changes channels or secrets from their own
  terminal with one command, without opening a session.
- Every log is inventoried and contains no message text, no secrets and no
  request URLs. Only the audit lines leave the instance.
- Costs about $17–18 a month.

## Non-goals

- High availability. One instance is a hard requirement (sign-in state lives
  in memory, decision 2026-11-17).
- Zero-downtime deploys. Every restart signs everyone out. That is accepted.
- Terraform, GitHub Actions, ECR, or a staging environment in phase 1.
- External uptime monitoring or paging beyond the CloudWatch recovery alarm.

## Anti-goals (a failure even if it works)

- Traffic decrypted by a third party: no proxied (orange-cloud) Cloudflare
  record and no Cloudflare tunnel.
- A secret or the real channel list in git, in the Docker image, in Docker's
  saved container config, in any log, in shell history, or in SSM command
  history.
- Request URLs, client IPs or request headers in any log that leaves the
  instance. Sign-in URLs carry one-time codes in the query string.
- More than one copy of the server running at the same time, even briefly.
- SSH keys or an open port 22.

## Constraints (from the code and earlier decisions)

- `uvicorn_config` pins one worker. `forwarded_allow_ips="*"` is safe only
  while uvicorn listens on loopback, behind a proxy on the same host.
  `HOST` must stay a loopback address.
- `TransportSecuritySettings.allowed_hosts` is the `PUBLIC_URL` host, so the
  proxy must pass the original `Host` header through. Caddy does this by
  default.
- The HTTP app is stateless and returns plain JSON responses, with no SSE. A
  big brief can take over a minute while it waits out Discord's rate limits.
  Caddy's `reverse_proxy` has no response timeout by default.
- **Secrets:**
  - `DISCORD_BOT_TOKEN`
  - `DISCORD_CLIENT_SECRET`
  - `OAUTH_CLIENT_SECRET`
  - `ENCRYPTION_KEY`
- **Non-secret settings:**
  - `DISCORD_GUILD_ID`
  - `PUBLIC_URL`
  - `DISCORD_CLIENT_ID`
  - `OAUTH_CLIENT_ID`
  - `OAUTH_REDIRECT_URIS`
  - `SERVER_NAME`
  - `DIGEST_CONFIG`
- The real config (`config.local.toml`) is structured TOML. It stays a file,
  is git-ignored, and reaches the host through SSM.
- This is a public repo. Region, account ID, VPC and subnet IDs, instance ID
  and the domain are never committed. Scripts take them from the operator's
  environment or a git-ignored local file.

## Approaches considered

**Hosting**

- **A. One EC2 instance, Docker, Caddy (chosen).** Matches the one-process
  rule exactly. The Elastic IP is stable, so the Cloudflare record is set
  once. TLS ends on the box.
- B. ECS Fargate, one task. The public IP changes on each deploy. Fixing that
  needs an ALB or a DNS update on every deploy. A rolling deploy also runs
  two tasks for a moment, which breaks sign-in.
- C. EC2 with a venv and no containers. Fewer layers, but Python, its
  dependencies and the OS get tangled together, and the move to phase 2's
  image pipeline gets harder.

**TLS in front of the server**

- **Caddy on the instance (chosen).** $0 extra. Only the instance sees
  plaintext. Automatic Let's Encrypt certificates. About 20–40 MB of memory.
- ALB with ACM. About $25 a month extra. Takes the instance off the public
  internet and allows WAF. Rejected on cost against the benefit. It also
  doesn't fix the single point of failure.
- Cloudflare proxied. Rejected: Cloudflare would see every response,
  including message text. The free plan's 100-second limit would also cut
  off long briefs, and its bot protection may block claude.ai.

**Order of work**

- **Hand-built first, automate later (chosen by the user).** Problems with
  the certificate, memory and restarts show up before they are locked into
  automation. There are no public Actions logs to protect yet. The risk is
  drift (nobody remembers how the setup was made). The checklist and the
  idempotent script cover that.
- Terraform and Actions from the start. The full earlier design is kept as
  phase 2.

## Phase 1 design

### AWS resources, created by hand (checklist in `deploy/README.md`)

`deploy/README.md` gives each step with a copyable AWS CLI command, plus the
console path.

1. **IAM role `discord-mcp-instance`** with an instance profile. It has a
   custom policy, **not** `AmazonSSMManagedInstanceCore`, which grants
   `ssm:GetParameter*` on `*` and would let the instance read every
   SecureString in the account. The policy allows:
   - only what the SSM agent needs (`ssm:UpdateInstanceInformation`,
     `ssmmessages:*`, `ec2messages:*`, and similar)
   - `ssm:GetParameter(s)` on `parameter/discord-mcp/*`, plus `kms:Decrypt`
     through SSM
   - `logs:CreateLogStream` and `logs:PutLogEvents` on the
     `/discord-mcp/app` log group only
2. **Security group `discord-mcp`:**
   - Inbound: TCP 80 and 443 from anywhere, IPv4 (and IPv6 if the subnet
     has it). Port 80 is for the Let's Encrypt challenge and the redirect.
   - No port 22.
   - Outbound: all.
3. **EC2 instance:**
   - t4g.small, Ubuntu 26.04 LTS arm64 (official Canonical AMI)
   - an existing VPC's public subnet, or a dedicated VPC with one public
     subnet (recommended when the account runs other workloads)
   - no key pair
   - role `discord-mcp-instance`
   - IMDSv2 required, hop limit 1
   - 20 GB gp3 disk, encrypted
   - tag `Name=discord-mcp`, termination protection on
   - no user data: all setup is done by `setup-instance.sh`
4. **Elastic IP**, associated with the instance.
5. **CloudWatch log group `/discord-mcp/app`**, kept for 30 days (*my call*:
   long enough to look into a problem, short enough not to build a history of
   who read which channel). Read access only for operators.
6. **CloudWatch alarm** on `StatusCheckFailed_System` with the EC2 `recover`
   action. It's free, and the instance keeps its IP and disk.
7. **SSM parameters** (all SecureString, default `aws/ssm` key), created
   from the operator's terminal without the values entering shell history
   (see "Changing secrets and channels"):

   | Name | Holds |
   | --- | --- |
   | `/discord-mcp/DISCORD_BOT_TOKEN` | bot token |
   | `/discord-mcp/DISCORD_CLIENT_SECRET` | Discord app secret |
   | `/discord-mcp/OAUTH_CLIENT_SECRET` | secret given to Claude |
   | `/discord-mcp/ENCRYPTION_KEY` | 32-byte base64url key |
   | `/discord-mcp/config` | contents of `config.local.toml` (advanced tier if over 4 KB) |
   | `/discord-mcp/env` | non-secret settings as `KEY=value` lines, plus `DOMAIN` for Caddy |

   `env` is SecureString too. It isn't secret, but SecureString keeps its
   value out of CloudTrail and out of console screenshots.
8. **Cloudflare:**
   - An A record (and AAAA if used) for the subdomain, pointing at the
     Elastic IP, **DNS only (grey cloud)**.
   - The name is published in public certificate transparency logs, so pick
     one that reveals nothing.
9. **Discord Developer Portal:** add the redirect
   `https://<domain>/oauth/discord/callback`.

### Layout on the instance

```
/opt/discord-mcp/src/          git checkout of this repo (read-only to the app)
/etc/discord-mcp/              Caddyfile, compose.yaml (copied from deploy/instance/ by the script);
                               image-tag and image-tag.prev (which image runs, and the one before)
/run/discord-mcp/              tmpfs, mode 0700: secrets.env, config.toml, compose.env (rebuilt every start)
/var/lib/discord-mcp/caddy/    Caddy's certificates and ACME account (persistent)
/var/log/discord-mcp/          deploy.log: last setup-instance.sh runs, rotated
```

### Services (all run in the background; no session needed)

| Unit | Type | Does |
| --- | --- | --- |
| `discord-mcp.service` | oneshot, `RemainAfterExit=yes`, enabled | `ExecStartPre`: `fetch-params.sh` writes `/run/discord-mcp/*` from SSM. `ExecStart`: `docker compose up -d --wait`. `ExecStop`: `docker compose down`. Starts at boot after `docker.service` and `network-online.target` |
| `discord-mcp-deploy.service` | oneshot, not enabled | Runs `setup-instance.sh` with the ref from `/etc/discord-mcp/deploy-ref`. Triggered by the remote command. Output goes to `/var/log/discord-mcp/deploy.log` |

- Both containers use `restart: unless-stopped`, so Docker restarts a
  crashed container without systemd.
- Compose runs detached (`up -d`), so container output **never** goes to
  journald. It goes only to each container's Docker log driver (see Logs).

### `deploy/setup-instance.sh`

An idempotent POSIX sh script, run as root on the instance. The first run
installs everything, and every later run updates. It never prints a
parameter value and never uses `set -x`.

1. **Packages, if missing:**
   - `docker.io`, `docker-compose-v2`, `git`, `curl`, `jq`, AWS CLI v2
   - Hold `docker.io`, `containerd` and `runc` (`apt-mark hold`), so
     automatic updates never restart Docker
2. **Swap:** 1 GB swap file on the encrypted disk, if missing.
3. **Automatic updates:**
   - `unattended-upgrades` for security updates.
   - `Automatic-Reboot "true"` with `Automatic-Reboot-Time "03:00"`, so a
     needed reboot happens only then. The script sets a systemd timer, or
     the time zone, so the reboot lands on Sunday at a fixed time,
     unless the team prefers any night.
4. **Code:**
   - `git fetch` in `/opt/discord-mcp/src`, then check out the requested ref
     (default `origin/main`).
   - Refuses a ref that isn't reachable from `origin/main`, so only merged
     code runs.
   - Then it re-runs itself **once** from the freshly checked-out copy,
     guarded by an environment flag, so a change to the script takes effect
     in the same deploy.
5. **Image:** `docker build -t discord-mcp:<short sha> .`
   - Native arm64 build, a few minutes on t4g.small.
   - Keeps the last 3 images and prunes the rest.
6. **Files:**
   - Copies `deploy/instance/` to `/etc/discord-mcp/` and the systemd units
     to `/etc/systemd/system/`.
   - Installs `fetch-params.sh`.
   - Installs the IMDS firewall rule (see Hardening).
7. **Restart:**
   - Moves the current `image-tag` to `image-tag.prev` and writes the new
     one.
   - `systemctl daemon-reload`, `systemctl enable discord-mcp`, then
     `systemctl restart discord-mcp`.
   - `restart` runs `compose down` before `up`, so two copies never run.
8. **Health check:**
   - Waits up to 60 seconds for
     `https://$DOMAIN/.well-known/oauth-authorization-server` to answer 200.
   - Prints `deploy ok <sha>` or `deploy failed: <step>`.
   - If the new image fails, it puts `image-tag.prev` back, restarts, and
     says so.

**First install.** From the operator's terminal, one Run Command clones the
public repo to `/opt/discord-mcp/src` and runs the script. After that, every
deploy goes through `deploy/remote.sh`.

### `deploy/remote.sh` (runs on a teammate's machine)

A thin wrapper around `aws ssm send-command` (document `AWS-RunShellScript`,
which runs as root in the background) and `get-command-invocation`.

- It finds the instance by tag `Name=discord-mcp`, so no instance ID is in
  the repo.
- It reads the AWS profile and region from the operator's environment or a
  git-ignored `deploy/.remote.local`.

| Command | Runs on the instance |
| --- | --- |
| `remote.sh deploy [ref]` | Writes the ref to `/etc/discord-mcp/deploy-ref`, then `systemctl start discord-mcp-deploy`. Waits for it to finish and prints the last lines of `deploy.log` |
| `remote.sh restart` | `systemctl restart discord-mcp`. Re-reads SSM, so use it after changing secrets or channels |
| `remote.sh status` | `systemctl is-active` for both units, the running image's SHA, uptime, memory |
| `remote.sh logs [n]` | Last *n* lines of `deploy.log`. Audit lines are read in CloudWatch, not here |
| `remote.sh caddy-logs [n]` | Last *n* lines of Caddy's log, for certificate problems |

- **Command text is safe to store.** Every command contains only a ref or a
  number, never a value, because SSM keeps command text and output in its
  history. `remote.sh` enforces this before sending anything:
  - the ref must match `^[0-9a-f]{7,40}$` or be `main`
  - *n* must be digits only, at most 1000
- **Output is safe to store.** Output is limited to the lines above, which
  hold no values either.
- **No session is opened.** If a laptop disconnects, the deploy keeps running
  because it is a systemd unit. `remote.sh status` picks it up later.

### Secrets and config on the host

- **`fetch-params.sh`** runs as `ExecStartPre` and reads `/discord-mcp/*`
  with the instance role. It writes:
  - `/run/discord-mcp/secrets.env` (mode 0400, owned by the app's UID): the
    four secrets
  - `/run/discord-mcp/config.toml` (mode 0400, app UID)
  - `/run/discord-mcp/compose.env` (mode 0400, root): the non-secret
    settings that Compose substitutes. The domain comes from the `env`
    parameter. The image tag comes from `/etc/discord-mcp/image-tag` on
    disk, so after a reboot the same version comes back without any deploy.
- **How they reach the app.** `secrets.env` and `config.toml` are mounted
  read-only into the `app` container. Secrets are **never** passed with
  Compose `env_file` or `environment`, because Docker copies those into
  `/var/lib/docker/containers/*/config.v2.json` on disk and shows them in
  `docker inspect`.
- **Image entrypoint** (`deploy/entrypoint.sh`, POSIX sh): reads
  `secrets.env` into its own environment and `exec`s
  `python -m discord_mcp`. Secret values exist only in tmpfs and process
  memory.
- **Non-secret settings** (`PUBLIC_URL`, client IDs, `HOST=127.0.0.1`,
  `PORT=8000`, `DIGEST_CONFIG=/run/config/config.toml`) may go in Compose
  `environment`.
- **On disk:** no plaintext secret file is written. Memory can page out to
  the swap file, which is on the encrypted disk.
- **After a reboot**, `/run` is empty again and the next start fetches fresh
  values.

### Containers

- **`app`:**
  - Built from the repo's `Dockerfile`: Python 3.12 slim base pinned by
    digest, `requirements.txt` only.
  - Non-root UID 10001, read-only root filesystem, `cap_drop: [ALL]`,
    `no-new-privileges`, `network_mode: host`.
  - Listens on `127.0.0.1:8000`, so the loopback rule holds without a code
    change.
  - `PYTHONWARNINGS=ignore`.
- **`caddy`:**
  - Official image pinned by digest.
  - Runs as non-root UID 10002, read-only root filesystem,
    `network_mode: host`, `cap_drop: [ALL]` plus
    `cap_add: [NET_BIND_SERVICE]`.
  - **No `no-new-privileges`:** binding 443 as non-root relies on the
    binary's file capability, and `no-new-privileges` would silently turn it
    off.
  - `/data` and `/config` are mounted from `/var/lib/discord-mcp/caddy/`,
    owned by UID 10002.
- **Docker log driver for `app`:**
  - `awslogs` with `mode: non-blocking`, `max-buffer-size: 4m` and
    `cache-disabled: "true"`.
  - Non-blocking means a CloudWatch outage drops audit lines instead of
    stalling the app.
- **`.dockerignore`** excludes `.env*`, `config.local.toml`, `PRD*`,
  `.superpowers`, `.venv`, `.git`, `tests` and `deploy/.remote.local`.

**Caddyfile:**

```
{
    admin off
    log default {
        output stderr
        format json
        level INFO
        exclude http.log.error http.handlers.reverse_proxy http.stdlib
    }
}
{$DOMAIN} {
    reverse_proxy 127.0.0.1:8000
}
```

- **No access log:** there is no site-level `log` directive.
- **The excluded loggers:**
  - `http.log.error` and `http.handlers.reverse_proxy` write the request
    (URI, client IP, headers) when the upstream fails.
  - `http.stdlib` carries Go's own server errors, such as TLS handshake
    failures from internet scanners, which include `from <ip>:<port>`.
- **Still to confirm:** whether other Caddy loggers ever include an IP.
  Verification step 3 checks this.

### Hardening

- **IMDS firewall.** An iptables rule (owner match) blocks the instance
  metadata address for every non-root process. Both containers run as
  non-root on the host network, so neither can get the instance role's
  credentials. Docker (`awslogs`), the SSM agent and `fetch-params.sh` run
  as root and still can. The rule is persisted with
  `iptables-persistent`.
- **No SSH.** No key pair, port 22 closed, `ssh` disabled.
- **Cloudflare stays grey-cloud.** It sees DNS only.

### Every log, and what it may contain

The rule: **only the audit lines leave the instance.** Everything else stays
on the instance, rotated, readable only by root, and holds no secrets, no
message text, no request URLs and no client IPs.

| Log | Where | Contains | Safeguard |
| --- | --- | --- | --- |
| App audit lines | `app` stderr → Docker `awslogs` → CloudWatch `/discord-mcp/app`, 30 days | One JSON line per call: Discord user ID, tool name, channel IDs, item count, duration, status (`ok`/`error:<Type>`/`dropped:<reason>`). Plus `startup` and `fatal: <message>`. Checked: settings and config errors name a setting or key, never its value (`check_words` uses fixed text, and file read and TOML errors give only the error type) | `audit.py` checks each value against a strict pattern. Every other logger is filtered out. The `awslogs` local cache is turned off (`cache-disabled: "true"`), so no copy stays on disk |
| What the audit lines still reveal | CloudWatch | **Who called which tool on which channels, and when.** No text and no search words, but it is a usage record of a named person | 30-day retention. Read access only for operators. Called out in the README so the team knows |
| Python output outside logging | same stream | Uncaught errors in the main thread print only the type (`safe_excepthook`) | **Gap, fixed in phase 1:** `threading.excepthook` and `sys.unraisablehook` still print full messages and tracebacks. They get the same type-only hook (tests first). Warnings are turned off with `PYTHONWARNINGS=ignore` |
| uvicorn | — | Nothing: access log off, its loggers fall to the filtered root handler (checked: only `uvicorn.access`, which has no handler and doesn't propagate) | `log_config=None`, `access_log=False`, `server_header=False` |
| Caddy | `caddy` stderr → Docker `local` driver, 3 × 10 MB, on the instance only | Startup, certificate issuing and renewal (domain name, Let's Encrypt URLs), config errors | Access log off. `http.log.error`, `http.handlers.reverse_proxy` and `http.stdlib` excluded. Never shipped. **Checked during phase 1 setup** (see Verification) |
| `deploy.log` | `/var/log/discord-mcp/`, logrotate 4 weeks, root only | apt, git and docker build output, step names, `deploy ok <sha>` | The script never prints values and never uses `set -x`. The build context has no secrets (`.dockerignore`) |
| journald | instance, Ubuntu defaults | Unit start and stop, Docker daemon events, `fetch-params.sh` errors (parameter **name** only) | Compose runs detached, so no container output reaches journald. `fetch-params.sh` sends AWS CLI errors to `/dev/null` and prints its own fixed text |
| Docker's saved container config | `/var/lib/docker/containers/*/config.v2.json` | Image, mounts, non-secret settings | Secrets are never in `environment` or `env_file` |
| cloud-init | `/var/log/cloud-init*.log` | Nothing of ours | No user data |
| SSM agent | `/var/log/amazon/ssm/` | The command text of each Run Command | Command text is only a ref or a number |
| SSM Run Command history | AWS, about 30 days; also S3/CloudWatch if the account turned that on | Command text and the first 24,000 characters of output | `remote.sh` output is limited to status lines and `deploy.log` lines |
| SSM Session Manager | AWS; S3/CloudWatch if the account records sessions | Everything typed and shown in a session | Sessions are for emergencies only. The runbook rule: never print a parameter value, `/run/discord-mcp/*` or `docker inspect` in a session |
| CloudTrail | AWS account | API calls: who read or changed which parameter, who ran which command | SecureString values are not recorded. All our parameters are SecureString |
| Operator's shell history | teammate's laptop | Commands typed | Values are entered with `read -rs` or `file://`, never on the command line (see below) |
| Let's Encrypt / CT logs | public | The domain name | Choose a subdomain that reveals nothing |
| apt / unattended-upgrades | instance | Package names | — |

### Changing secrets and channels (runbook)

- **A secret:**

  ```
  read -rs V && printf %s "$V" | aws ssm put-parameter --name /discord-mcp/<NAME> \
      --type SecureString --overwrite --value file:///dev/stdin; unset V
  ```

  Then run `remote.sh restart`. Everyone signs in again.
- **Channels:** edit `config.local.toml` locally, then:

  ```
  aws ssm put-parameter --name /discord-mcp/config --type SecureString \
      --overwrite --value file://config.local.toml
  ```

  Then run `remote.sh restart`. A broken config stops the server (fail
  closed). `remote.sh restart` reports that, and the fix is to put back the
  previous version (SSM keeps parameter history).
- **The encryption key:** changing it signs everyone out. Do it when rotating
  after a suspected leak.

### Day-to-day operations

| Task | Command |
| --- | --- |
| Deploy the latest `main` | `deploy/remote.sh deploy` |
| Deploy or roll back to a commit | `deploy/remote.sh deploy <sha>` |
| Pick up changed secrets or channels | `deploy/remote.sh restart` |
| Check it's up | `deploy/remote.sh status` |
| Read audit lines | CloudWatch console, or `aws logs tail /discord-mcp/app --since 1h` |
| Certificate trouble | `deploy/remote.sh caddy-logs 100` |
| Replace the instance | Launch a new one from the checklist, move the Elastic IP, run the first install. Caddy gets a new certificate |

**What every restart means for people:** they sign in again. Announce
deploys in the team channel. Kernel reboots happen at most once a week, at
the fixed time.

### Who can do what

Teammates need their own AWS sign-in with these permissions (the exact
mechanism is the open question below):

- **Operator:**
  - `ssm:SendCommand` (`AWS-RunShellScript`, the tagged instance only) and
    `ssm:GetCommandInvocation`
  - `ssm:PutParameter`/`GetParameter` on `/discord-mcp/*` (anyone who can
    read these can read the bot token)
  - Read the log group
  - `ec2:DescribeInstances`
- **Setting up the AWS resources by hand** needs someone who can create an
  IAM role, a security group, an instance, an Elastic IP, a log group and an
  alarm.

### Moving over from Funnel

- Keep the same Discord application, and add the new redirect beside the
  Funnel one.
- Generate **new** `OAUTH_CLIENT_SECRET` and `ENCRYPTION_KEY` values for
  production.
- Once production works, retire the laptop server and remove the Funnel
  redirect.

### Verification before calling phase 1 done

1. **TLS:** `curl -v https://<domain>/.well-known/oauth-authorization-server`
   returns 200 with a Let's Encrypt certificate. Cloudflare shows the record
   as DNS only.
2. **Sign-in and calls:** add the connector in claude.ai, sign in, and run a
   brief.
3. **Logs:**
   - Run a brief, then search every log in the table for a word from a real
     message, a part of each secret, `code=`, and a client IP. CloudWatch
     should have only audit lines. On the instance, search `deploy.log`,
     journald, Caddy's log and `/var/lib/docker/containers`.
   - Repeat while the app is stopped, so Caddy returns 502s, and while
     signing in.
   - Repeat with scanner-style traffic from a known IP, then search Caddy's
     log for that IP:
     - a TLS connection to the Elastic IP with no SNI
     - a port-80 request with a junk `Host`
     - a malformed request
     - a request to an unknown path
4. **Background running:**
   - Reboot the instance with no session open. The server comes back, and
     people have to sign in again.
   - Kill the app container. Docker restarts it.
   - Run `remote.sh deploy`, close the laptop, then check `remote.sh status`
     later.
5. **Memory:** watch memory during a brief (`remote.sh status`). If it gets
   close to 2 GB, raise the instance size.
6. **IMDS:** a process inside the `app` container can't reach
   `169.254.169.254`.
7. **One copy:** during a deploy, at most one `app` container exists at any
   moment.

### Repo changes in phase 1

- `Dockerfile`, `.dockerignore`, `deploy/entrypoint.sh`
- `deploy/setup-instance.sh`, `deploy/fetch-params.sh`, `deploy/remote.sh`
- `deploy/instance/`: `compose.yaml`, `Caddyfile`, `discord-mcp.service`,
  `discord-mcp-deploy.service`, logrotate config, unattended-upgrades config
- `deploy/README.md`: AWS checklist, first install, runbook, log inventory
- `.gitignore`: `deploy/.remote.local`
- `discord_mcp/audit.py` and `__main__.py`: type-only `threading.excepthook`
  and `sys.unraisablehook`, tests first
- README: the Deployment section points to `deploy/README.md`
- `docs/design.md`: Status, stage 3 phase 1
- `docs/decisions.md`: an entry for this design

### Cost (approximate, on-demand)

| Item | Per month |
| --- | --- |
| t4g.small | ~$12 |
| Elastic IP (public IPv4) | ~$3.60 |
| 20 GB gp3 | ~$1.60 |
| CloudWatch Logs, SSM | < $1 |
| **Total** | **~$17–18** |

## Phase 2 (later): Terraform and GitHub Actions

Not designed in detail yet. The review of the first draft already
established these points:

- **Terraform:**
  - Import the phase 1 resources.
  - State goes in a private S3 bucket.
  - SSM parameters stay **outside** Terraform: `aws_ssm_parameter` stores
    decrypted values in state, even with `ignore_changes`.
- **Image builds:** Actions builds the image on GitHub's Arm runner and
  pushes it to ECR. The instance pulls it instead of building, and the tag to
  run moves to `/discord-mcp/image_tag`.
- **The repo is public, so Actions logs and artifacts are public:**
  - Every account-specific value is an environment **secret**, not a
    variable.
  - Derived values (registry host, instance ID, IP) are masked.
  - Terraform output is summarised.
  - Full output on failure goes to the private bucket.
  - No plan file is uploaded as an artifact.
- **Approval happens only before a job starts:**
  - A plan job runs in a reviewer-free environment with a read-only role.
    It saves the plan to the private bucket.
  - An apply job runs in an approved environment and applies that exact
    plan.
- **AWS login:** OIDC roles trust only
  `repo:<org>/<repo>:environment:<name>`. Actions are pinned to SHAs.
- **Questions to answer then:**
  - The account's rules: SCPs, permission boundaries, tags, regions,
    an existing OIDC provider.
  - Whether the person who starts a deploy may approve it.

## Deferred: teammate access

At the start, one person runs phase 1 with their existing AWS access.

**Account admins keep full access.** Access goes through IAM, not SSH keys,
so anyone with administrator rights on the account can:

- deploy with `remote.sh`
- open a root-capable shell through Session Manager
- read and change the SSM parameters (including the secrets)
- read the audit logs
- stop or replace the instance

CloudTrail records each person's actions. In short, the list of account
admins is the list of people who can read the secrets. If the SSM agent
breaks, the fallback is to replace the instance from the checklist. Before
a second teammate operates it, decide how teammates sign in to AWS (IAM
Identity Center or IAM users). Then create a narrow operator role or
permission set with only the operator permissions listed above, rather than
giving out admin access.

## Left to the builder

- Exact IAM policy JSON, within the limits above.
- How `fetch-params.sh` and the entrypoint parse parameters, as long as no
  value reaches a log or Docker's saved config.
- How AWS CLI v2 is installed (official zip or snap).
- The mechanism for the Sunday-only reboot window.
- Exact output format of `remote.sh`.
- Tests that make sense for the new files, for example that `.dockerignore`
  excludes secret files and that the scripts have no `set -x`.
