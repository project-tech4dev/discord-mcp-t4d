# Deploying discord-mcp on AWS (stage 3, phase 1)

This runs discord-mcp as a shared remote connector for the team, on one EC2
instance in the team's AWS account. The design, with the reasons behind it,
is in `docs/superpowers/specs/2026-11-22-aws-deployment-design.md`.

- **Phase 1 (this page):** the AWS resources are created by hand, following
  the checklist below. The instance builds and runs the image itself, and
  teammates operate it with `deploy/remote.sh` through SSM Run Command. There
  is no SSH.
- **Phase 2 (later):** Terraform and GitHub Actions. See
  [Phase 2](#phase-2-terraform-and-github-actions) at the end.

**Never commit** the region, account ID, VPC, subnet, security group or
instance IDs, the Elastic IP or the domain. Keep them in your shell and in
the git-ignored `deploy/.remote.local`.

## What runs where

```
claude.ai ──HTTPS──▶ Elastic IP :443 ──▶ Caddy (TLS, Let's Encrypt) ──▶ 127.0.0.1:8000 app
                                          caddy container, UID 10002     app container, UID 10001
```

- One `t4g.small` (Ubuntu 24.04 arm64). Docker Compose runs two containers
  on the host network: `app` (this repo's `Dockerfile`) and `caddy`.
- Both run as non-root with read-only root filesystems and all capabilities
  dropped. Caddy keeps only `NET_BIND_SERVICE`.
- Settings and secrets live in six SSM SecureString parameters. On every
  start, `fetch-params.sh` reads them by exact name into the tmpfs
  `/run/discord-mcp`. The app reads its secrets from a read-only mounted
  file, so no secret is ever in Docker's saved config or in `docker inspect`.
- An iptables rule stops every non-root process from reaching instance
  metadata, so neither container can get the instance role's credentials.
- Only the app's audit lines leave the instance (CloudWatch, 30 days).

## Files in this directory

| File | Runs | Does |
| --- | --- | --- |
| `remote.sh` | your machine | `install`, `deploy [main\|sha]`, `restart`, `status`, `logs [n]`, `caddy-logs [n]`, through SSM Run Command |
| `setup-instance.sh` | instance, root | Idempotent install and update: code, packages, swap, image, files, restart, health check, rollback |
| `checkout-ref.sh` | instance, root, called by setup | Fetches only `main` (never tags) and checks out the requested commit if, and only if, it is on `origin/main` |
| `fetch-params.sh` | instance, root, before every start | Reads the six parameters and writes `/run/discord-mcp/{secrets.env,config.toml,compose.env}` |
| `entrypoint.sh` | inside the `app` image | Loads the four secrets into its environment, then runs `python -m discord_mcp` |
| `imds-block.sh` | instance, root, at boot | Blocks instance metadata for non-root processes |
| `instance/` | instance | `compose.yaml`, `Caddyfile`, systemd units (and a Docker drop-in that requires the metadata block), logrotate and unattended-upgrades config |
| `examples/` | you, once | `discord-mcp.env.example` (the `/discord-mcp/env` parameter, every key explained) and `remote.local.example` (`deploy/.remote.local`). The channel config's example is `config.toml` at the repo root |
| `iam/` | you, once | The instance role's trust policy and permission policy (with placeholders) |

## Checklist: create the AWS resources (once)

You need someone who can create an IAM role, a security group, an instance,
an Elastic IP, a log group and an alarm. Run these from the repo root, in
order. Each step says what it creates.

### 0. Your shell

```sh
export AWS_PROFILE=<profile> AWS_REGION=<region>
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
VPC_ID=<existing vpc>        # the VPC to use
SUBNET_ID=<public subnet>    # a public subnet in it
```

Save the profile and region for `remote.sh` in the git-ignored
`deploy/.remote.local` (the format is in `examples/remote.local.example`):

```sh
printf 'AWS_PROFILE=%s\nAWS_REGION=%s\n' "$AWS_PROFILE" "$AWS_REGION" > deploy/.remote.local
```

### 1. Pick the domain

Pick a subdomain that reveals nothing: it is published in public
certificate transparency logs. It is called `<domain>` below.

### 2. SSM parameters (all SecureString, default `aws/ssm` key)

Create these first: the first SecureString creates the `aws/ssm` key that the
policy in step 3 needs. Values are never typed on the command line, so they
stay out of shell history.

| Name | Holds |
| --- | --- |
| `/discord-mcp/DISCORD_BOT_TOKEN` | bot token |
| `/discord-mcp/DISCORD_CLIENT_SECRET` | Discord app secret |
| `/discord-mcp/OAUTH_CLIENT_SECRET` | secret given to Claude (new for production) |
| `/discord-mcp/ENCRYPTION_KEY` | 32 random bytes, base64url (new for production) |
| `/discord-mcp/config` | contents of `config.local.toml` (advanced tier if over 4 KB) |
| `/discord-mcp/env` | non-secret settings, `KEY=value` lines |

The two Discord secrets, pasted when prompted:

```sh
for n in DISCORD_BOT_TOKEN DISCORD_CLIENT_SECRET; do
  printf '%s: ' "$n"; read -rs V; echo
  printf %s "$V" | aws ssm put-parameter --name "/discord-mcp/$n" \
      --type SecureString --value file:///dev/stdin >/dev/null
done; unset V
```

The two generated secrets, which never appear on screen:

```sh
for n in OAUTH_CLIENT_SECRET ENCRYPTION_KEY; do
  python3 -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip('='),end='')" \
    | aws ssm put-parameter --name "/discord-mcp/$n" --type SecureString \
        --value file:///dev/stdin >/dev/null
done
```

Give `OAUTH_CLIENT_SECRET` to the person adding the connector in claude.ai.
Copy it straight to the clipboard so it never shows on screen or in your
shell history, and never paste it into a chat or ticket:

```sh
aws ssm get-parameter --name /discord-mcp/OAUTH_CLIENT_SECRET --with-decryption \
  --query Parameter.Value --output text | pbcopy
```

The channel config, from your local file:

```sh
aws ssm put-parameter --name /discord-mcp/config --type SecureString \
    --value file://config.local.toml >/dev/null
```

The non-secret settings. Copy the example **outside the repo**, replace every
`<...>` (each line is explained in the file), then upload it:

```sh
cp deploy/examples/discord-mcp.env.example ~/discord-mcp.env
"${EDITOR:-vi}" ~/discord-mcp.env
aws ssm put-parameter --name /discord-mcp/env --type SecureString \
    --value file://$HOME/discord-mcp.env >/dev/null
```

`fetch-params.sh` accepts only `DOMAIN`, `PUBLIC_URL`, `DISCORD_GUILD_ID`,
`DISCORD_CLIENT_ID`, `OAUTH_CLIENT_ID`, `OAUTH_REDIRECT_URIS` and
`SERVER_NAME`. Values may use only letters, digits and `. _ : / , @ + -`.
`PUBLIC_URL` must be exactly `https://` plus `DOMAIN`. Anything else stops
the start, with a message naming the setting.

### 3. IAM role `discord-mcp-instance` and its policy

The role does **not** use `AmazonSSMManagedInstanceCore`: that grants
`ssm:GetParameter*` on every parameter, and the account is shared. The
custom policy in [`iam/instance-policy.json`](iam/instance-policy.json)
allows only:

| Statement | Allows | Limited to |
| --- | --- | --- |
| `SsmAgentCore` | `ssm:UpdateInstanceInformation`, `ssmmessages:*Channel`, `ec2messages:*` (listed one by one) | What the SSM agent needs for Run Command. These actions take no resource ARN |
| `ReadDiscordMcpParametersOnly` | `ssm:GetParameter`, `ssm:GetParameters` | The six parameter ARNs above, by exact name. No `GetParametersByPath`, no wildcard |
| `DecryptThoseParametersViaSsmOnly` | `kms:Decrypt` | The `aws/ssm` key, only through SSM in this region and only for those six parameters. This states the intent; the `aws/ssm` key's own policy already lets anyone in the account decrypt through SSM, so the real limit is the parameter list above |
| `WriteAuditLogsToOneGroup` | `logs:CreateLogStream`, `logs:PutLogEvents` | The `/discord-mcp/app` log group. No `CreateLogGroup`: if the group is missing, the start fails |

Fill in the placeholders into a file **outside the repo**, then create the
role:

```sh
SSM_KEY_ARN=$(aws kms describe-key --key-id alias/aws/ssm --query KeyMetadata.Arn --output text)
sed -e "s|__REGION__|$AWS_REGION|g" -e "s|__ACCOUNT_ID__|$ACCOUNT_ID|g" \
    -e "s|__SSM_KEY_ARN__|$SSM_KEY_ARN|g" deploy/iam/instance-policy.json > ~/discord-mcp-policy.json
grep -c __ ~/discord-mcp-policy.json   # must print 0

aws iam create-role --role-name discord-mcp-instance \
    --assume-role-policy-document file://deploy/iam/instance-trust.json
aws iam put-role-policy --role-name discord-mcp-instance --policy-name discord-mcp-instance \
    --policy-document file://$HOME/discord-mcp-policy.json
aws iam create-instance-profile --instance-profile-name discord-mcp-instance
aws iam add-role-to-instance-profile --instance-profile-name discord-mcp-instance \
    --role-name discord-mcp-instance
aws iam wait instance-profile-exists --instance-profile-name discord-mcp-instance
```

IAM takes a few seconds to reach EC2. If step 6 says "Invalid IAM Instance
Profile name", wait a minute and run it again.

**Adding a parameter later** means adding its ARN to both lists in the
policy (and to `fetch-params.sh`), then running `put-role-policy` again.

### 4. Security group `discord-mcp`

Inbound TCP 80 (Let's Encrypt challenge and redirect) and 443 from anywhere.
No port 22. Outbound stays at the default (all).

```sh
SG_ID=$(aws ec2 create-security-group --group-name discord-mcp \
    --description "discord-mcp: HTTP and HTTPS only" --vpc-id "$VPC_ID" --query GroupId --output text)
aws ec2 authorize-security-group-ingress --group-id "$SG_ID" --ip-permissions \
    'IpProtocol=tcp,FromPort=80,ToPort=80,IpRanges=[{CidrIp=0.0.0.0/0}]' \
    'IpProtocol=tcp,FromPort=443,ToPort=443,IpRanges=[{CidrIp=0.0.0.0/0}]'
```

If the subnet has IPv6, add the same two rules with
`Ipv6Ranges=[{CidrIpv6=::/0}]`.

### 5. CloudWatch log group `/discord-mcp/app`, 30 days

Create it before the first start: the instance can't.

```sh
aws logs create-log-group --log-group-name /discord-mcp/app
aws logs put-retention-policy --log-group-name /discord-mcp/app --retention-in-days 30
```

Give read access to operators only: it records who called which tool on
which channels.

### 6. EC2 instance

`t4g.small`, the official Ubuntu 24.04 arm64 AMI, no key pair, IMDSv2
required with hop limit 1, a 20 GB encrypted gp3 disk, termination
protection on, and no user data.

```sh
INSTANCE_ID=$(aws ec2 run-instances \
    --image-id resolve:ssm:/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id \
    --instance-type t4g.small --subnet-id "$SUBNET_ID" --security-group-ids "$SG_ID" \
    --iam-instance-profile Name=discord-mcp-instance \
    --metadata-options HttpEndpoint=enabled,HttpTokens=required,HttpPutResponseHopLimit=1 \
    --block-device-mappings 'DeviceName=/dev/sda1,Ebs={VolumeSize=20,VolumeType=gp3,Encrypted=true}' \
    --disable-api-termination \
    --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=discord-mcp}]' \
    --query 'Instances[0].InstanceId' --output text)
aws ec2 wait instance-running --instance-ids "$INSTANCE_ID"
```

The tag `Name=discord-mcp` is how `remote.sh` finds the instance. Only one
running instance may have it.

### 7. Elastic IP

```sh
ALLOC_ID=$(aws ec2 allocate-address --domain vpc --query AllocationId --output text)
aws ec2 associate-address --instance-id "$INSTANCE_ID" --allocation-id "$ALLOC_ID"
aws ec2 describe-addresses --allocation-ids "$ALLOC_ID" --query 'Addresses[0].PublicIp' --output text
```

### 8. Recover alarm

Free. On a host failure the instance moves and keeps its IP and disk.

```sh
aws cloudwatch put-metric-alarm --alarm-name discord-mcp-recover \
    --namespace AWS/EC2 --metric-name StatusCheckFailed_System \
    --dimensions Name=InstanceId,Value="$INSTANCE_ID" \
    --statistic Maximum --period 60 --evaluation-periods 2 --threshold 1 \
    --comparison-operator GreaterThanOrEqualToThreshold \
    --alarm-actions "arn:aws:automate:$AWS_REGION:ec2:recover"
```

### 9. Cloudflare DNS

An A record (and AAAA if used) for `<domain>`, pointing at the Elastic IP,
**DNS only (grey cloud)**. Caddy ends TLS on the instance, so Cloudflare
never sees the traffic.

### 10. Discord Developer Portal

OAuth2 → Redirects: add `https://<domain>/oauth/discord/callback`, beside
the existing one.

### 11. Check the SSM agent sees the instance

```sh
aws ssm describe-instance-information \
    --filters "Key=InstanceIds,Values=$INSTANCE_ID" --query 'InstanceInformationList[0].PingStatus'
```

It should print `"Online"` within a few minutes. If not, check the role is
attached and the subnet has a route to the internet.

### 12. First install

```sh
deploy/remote.sh install
```

This clones the public repo to `/opt/discord-mcp/src` and runs
`setup-instance.sh main`. It installs packages, builds the image (a few
minutes on `t4g.small`), starts the service and waits up to 3 minutes for
Caddy's certificate. It ends with `deploy ok <sha>` or
`deploy failed: <step>`.

Then delete the scratch files: `rm ~/discord-mcp-policy.json ~/discord-mcp.env`.

### 13. Add the connector in claude.ai

URL `https://<domain>/mcp`, with the `OAUTH_CLIENT_ID` and
`OAUTH_CLIENT_SECRET` from step 2. Sign in with Discord.

## Day-to-day operations

"Only merged code runs" means only commits on `main` can be deployed
(`checkout-ref.sh` checks it on the instance). That is worth exactly as much
as the protection on `main`, so keep GitHub branch protection on: changes
only through reviewed pull requests, no direct pushes, no force-pushes.
Tags are never fetched, so a pushed tag can't stand in for `main`.

| Task | Command |
| --- | --- |
| Deploy the latest `main` | `deploy/remote.sh deploy` |
| Deploy or roll back to a commit on `main` | `deploy/remote.sh deploy <sha>` |
| Pick up changed secrets or channels | `deploy/remote.sh restart` |
| Check it's up | `deploy/remote.sh status` |
| Last deploy output | `deploy/remote.sh logs 100` |
| Read audit lines | `aws logs tail /discord-mcp/app --since 1h` |
| Certificate trouble | `deploy/remote.sh caddy-logs 100` |
| Replace the instance | Steps 6–8 and 11–12 again, then move the Elastic IP. Caddy gets a new certificate |

- **Only merged code runs.** A deploy refuses any commit that isn't on
  `origin/main`.
- **A failed deploy rolls back** to the previous image and says so.
- **Every restart signs people out.** They sign in again once their current
  access token (up to an hour) expires. Announce deploys in the team channel.
- **Deploys survive a closed laptop.** The deploy is a systemd unit on the
  instance; `remote.sh status` picks it up later.
- **Reboots:** security updates install daily. If one needs a reboot, it
  happens on Sunday at 03:00 UTC, never at another time.
- **Docker updates are manual.** `docker.io`, `containerd` and `runc` are
  held so automatic updates never restart the server. Update them in a
  planned window (`apt-mark unhold`, upgrade, `remote.sh restart`, hold
  again), through a Run Command or Session Manager.

## Changing secrets and channels

- **A secret:**

  ```sh
  read -rs V && printf %s "$V" | aws ssm put-parameter --name /discord-mcp/<NAME> \
      --type SecureString --overwrite --value file:///dev/stdin >/dev/null; unset V
  deploy/remote.sh restart
  ```

  Everyone signs in again.
- **Channels:** edit `config.local.toml`, then:

  ```sh
  aws ssm put-parameter --name /discord-mcp/config --type SecureString \
      --overwrite --value file://config.local.toml >/dev/null
  deploy/remote.sh restart
  ```

  A broken config stops the server (fail closed) and `restart` reports it.
  Put the previous version back from the parameter's history.
- **The encryption key:** changing it signs everyone out at once. Do it after
  a suspected leak.

## Every log, and what it may contain

The rule: **only the audit lines leave the instance.** Everything else stays
on the instance, rotated, readable only by root. No log holds a secret,
message text, a request URL or a client IP.

| Log | Where | Contains |
| --- | --- | --- |
| App audit lines | CloudWatch `/discord-mcp/app`, 30 days | One JSON line per call: Discord user ID, tool, channel IDs, item count, duration, status. Plus `startup` and `fatal: <type or setting name>`. **This is a usage record of named people:** who called which tool on which channels, and when |
| Caddy | instance, Docker `local` driver, 3 × 10 MB | Startup and certificate events. No access log; the loggers that print requests or client IPs are excluded |
| `deploy.log` | instance, `/var/log/discord-mcp/`, 4 weeks | apt, git and build output, step names, `deploy ok <sha>` |
| journald | instance | Unit start and stop; `fetch-params` errors name a parameter or setting, never a value |
| SSM Run Command history | AWS, about 30 days | Command text (only fixed text plus a ref or number) and output (status and `deploy.log` lines) |
| CloudTrail | AWS | Who read or changed which parameter and who ran which command. SecureString values are not recorded |

The full inventory, with each safeguard, is in the design.

**Session Manager is for emergencies only.** Sessions may be recorded. In a
session, never print a parameter value, `/run/discord-mcp/*` or
`docker inspect` output.

## Who can do what

**Account admins can do everything:** deploy, open a root shell through
Session Manager, read and change the parameters (including the bot token),
read the audit logs, and stop or replace the instance. The list of account
admins is the list of people who can read the secrets. CloudTrail records
each person's actions.

A teammate who only operates the server needs:

- `ssm:SendCommand` with `AWS-RunShellScript` on the tagged instance, and
  `ssm:GetCommandInvocation`
- `ec2:DescribeInstances`
- `ssm:PutParameter` and `ssm:GetParameter` on `/discord-mcp/*` (anyone with
  this can read the bot token)
- read access to the `/discord-mcp/app` log group

Before a second teammate operates it, decide how teammates sign in to AWS
and create that narrow role instead of handing out admin access (tracked in
the Phase 2 issue).

## Verification before calling phase 1 done

1. **TLS:** `curl -v https://<domain>/.well-known/oauth-authorization-server`
   returns 200 with a Let's Encrypt certificate. Cloudflare shows the record
   as DNS only.
2. **Sign-in and calls:** add the connector in claude.ai, sign in, and run a
   brief.
3. **Logs:** run a brief, then search every log above for a word from a real
   message, part of each secret, `code=` and your IP. CloudWatch should have
   only audit lines. On the instance also search
   `/var/lib/docker/containers`. Repeat while the app is stopped (Caddy
   returns 502), while signing in, and after scanner-style traffic from a
   known IP: a TLS connection to the bare IP with no SNI, a port-80 request
   with a junk `Host`, a malformed request, and an unknown path.
4. **Background running:** reboot with no session open (the server comes
   back); kill the app container (Docker restarts it); run `remote.sh deploy`,
   close the laptop, and check `remote.sh status` later.
5. **Memory:** watch `remote.sh status` during a brief. If it nears 2 GB,
   raise the instance size.
6. **IMDS:** a process in the `app` container can't reach `169.254.169.254`.
7. **One copy:** during a deploy, at most one `app` container exists.

## Moving over from Tailscale Funnel

Keep the same Discord application and add the new redirect beside the
Funnel one. Use **new** `OAUTH_CLIENT_SECRET` and `ENCRYPTION_KEY` values
(step 2 generates them). Once production works, stop the laptop server and
remove the Funnel redirect.

## Cost (approximate, on-demand)

About $17–18 a month: `t4g.small` ~$12, the Elastic IP ~$3.60, 20 GB gp3
~$1.60, CloudWatch Logs and SSM under $1.

## Phase 2: Terraform and GitHub Actions

Not built yet. Phase 2 imports these resources into Terraform (state in a
private S3 bucket, SSM parameters kept outside it), builds the image in
GitHub Actions and pushes it to ECR, and adds an approved plan-then-apply
flow with OIDC roles. Because the repo is public, every account-specific
value stays an Actions secret and no plan or full output is published.

Top-level design and open questions:
[issue #21](https://github.com/project-tech4dev/discord-mcp-t4d/issues/21).
