"""Checks on the deployment files in deploy/, Dockerfile and .dockerignore.

These run the shell scripts locally with stub commands on PATH; nothing here
talks to AWS or Docker.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
SCRIPTS = sorted(DEPLOY.glob("*.sh"))
SECRET_NAMES = ["DISCORD_BOT_TOKEN", "DISCORD_CLIENT_SECRET", "OAUTH_CLIENT_SECRET", "ENCRYPTION_KEY"]
PARAMETERS = SECRET_NAMES + ["config", "env"]
PRIVATE = "PRIVATE-SECRET-VALUE-42"
# dash rejects bash-only syntax; macOS's sh is bash and would not.
POSIX_SH = shutil.which("dash") or "sh"


# --- the image can never contain secrets ---------------------------------------


def test_dockerignore_is_an_allowlist_of_only_what_the_image_needs():
    lines = [ln.strip() for ln in (ROOT / ".dockerignore").read_text().splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("#")]
    assert lines[0] == "*"
    allowed = {ln for ln in lines if ln.startswith("!")}
    assert allowed == {"!requirements.txt", "!discord_mcp/", "!deploy/entrypoint.sh"}


def test_dockerfile_pins_the_base_image_and_runs_as_non_root():
    text = (ROOT / "Dockerfile").read_text()
    assert re.search(r"^FROM python:3\.12[^\s]*@sha256:[0-9a-f]{64}$", text, re.M)
    assert re.search(r"^USER 10001:10001$", text, re.M)
    assert "COPY . " not in text


def test_image_code_is_readable_by_the_app_whatever_the_host_file_modes():
    # git writes the checkout with the deploying process's umask, and COPY
    # keeps those modes; the app (UID 10001) must still be able to read it.
    text = (ROOT / "Dockerfile").read_text()
    assert "chmod -R a+rX,go-w /app" in text


def test_setup_and_deploy_unit_use_a_umask_that_leaves_code_readable():
    assert re.search(r"^umask 022$", (DEPLOY / "setup-instance.sh").read_text(), re.M)
    assert "UMask=0077" not in (DEPLOY / "instance" / "discord-mcp-deploy.service").read_text()


# --- scripts -------------------------------------------------------------------


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.name)
def test_scripts_are_strict_posix_sh_and_never_trace(path):
    text = path.read_text()
    assert text.startswith("#!/bin/sh\n")
    assert re.search(r"^set -eu$", text, re.M)
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    assert not re.search(r"set\s+-[a-z]*x|set\s+-o\s+xtrace|sh\s+-x", code)
    assert path.stat().st_mode & stat.S_IXUSR
    subprocess.run([POSIX_SH, "-n", str(path)], check=True)


@pytest.mark.parametrize("path", sorted((DEPLOY / "instance").glob("*.service")), ids=lambda p: p.name)
def test_units_never_trace(path):
    assert not re.search(r"sh\s+-[a-z]*x", path.read_text())


def test_compose_never_puts_secrets_in_docker_config():
    text = (DEPLOY / "instance" / "compose.yaml").read_text()
    assert "env_file" not in text
    for name in SECRET_NAMES:
        assert name not in text
    # A missing file must fail the start, not be created as an empty directory.
    assert text.count("type: bind") == text.count("create_host_path: false") == 5


def test_app_listens_on_loopback_only():
    text = (DEPLOY / "instance" / "compose.yaml").read_text()
    assert re.search(r"^\s+HOST: 127\.0\.0\.1\b", text, re.M)


def test_caddy_writes_no_access_log():
    text = (DEPLOY / "instance" / "Caddyfile").read_text()
    site = text.split("{$DOMAIN}", 1)[1]
    assert "log" not in site
    assert "exclude http.log.error http.handlers.reverse_proxy http.stdlib" in text


# --- IAM policy matches what fetch-params reads ----------------------------------


def _policy():
    return json.loads((DEPLOY / "iam" / "instance-policy.json").read_text())


def test_policy_reads_exactly_the_six_parameters_by_name():
    stmts = {s["Sid"]: s for s in _policy()["Statement"]}
    expected = [f"arn:aws:ssm:__REGION__:__ACCOUNT_ID__:parameter/discord-mcp/{n}" for n in PARAMETERS]
    read = stmts["ReadDiscordMcpParametersOnly"]
    assert sorted(read["Action"]) == ["ssm:GetParameter", "ssm:GetParameters"]
    assert read["Resource"] == expected
    kms = stmts["DecryptThoseParametersViaSsmOnly"]
    assert kms["Condition"]["StringEquals"]["kms:EncryptionContext:PARAMETER_ARN"] == expected
    assert kms["Condition"]["StringEquals"]["kms:ViaService"] == "ssm.__REGION__.amazonaws.com"


def test_policy_has_no_broad_parameter_or_log_access():
    text = json.dumps(_policy())
    assert "GetParametersByPath" not in text
    assert "ssm:*" not in text and "ssm:GetParameter*" not in text
    assert "logs:CreateLogGroup" not in text
    for s in _policy()["Statement"]:
        if s["Resource"] == "*":
            assert all(a.startswith(("ssmmessages:", "ec2messages:")) or a == "ssm:UpdateInstanceInformation" for a in s["Action"])


def test_fetch_params_reads_the_same_parameters_as_the_policy():
    text = (DEPLOY / "fetch-params.sh").read_text()
    secrets = re.search(r'^SECRET_NAMES="([^"]+)"', text, re.M).group(1).split()
    assert secrets == SECRET_NAMES
    assert "for n in $SECRET_NAMES config env" in text
    assert "get-parameters-by-path" not in text


# --- entrypoint ----------------------------------------------------------------


def _stub(tmp_path: Path, name: str, body: str) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    p = bin_dir / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)
    return bin_dir


def _entrypoint(tmp_path: Path, secrets: str):
    f = tmp_path / "secrets.env"
    f.write_text(secrets)
    bin_dir = _stub(tmp_path, "python", 'env | grep -E "^(DISCORD|OAUTH|ENCRYPTION)_" | sort\necho "args=$*"\n')
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "DISCORD_MCP_SECRETS": str(f)}
    return subprocess.run(["sh", str(DEPLOY / "entrypoint.sh")], env=env, capture_output=True, text=True)


def test_entrypoint_exports_values_literally_and_runs_the_server(tmp_path):
    tricky = "a $HOME `id` $(id) b=c 'q' \"dq\""
    out = _entrypoint(
        tmp_path,
        f"DISCORD_BOT_TOKEN={tricky}\nDISCORD_CLIENT_SECRET=s2\nOAUTH_CLIENT_SECRET=s3\nENCRYPTION_KEY=s4\n",
    )
    assert out.returncode == 0, out.stderr
    assert f"DISCORD_BOT_TOKEN={tricky}\n" in out.stdout
    assert "args=-m discord_mcp" in out.stdout


@pytest.mark.parametrize(
    "secrets",
    [f"PATH={PRIVATE}\n", f"LD_PRELOAD={PRIVATE}\n", f"{PRIVATE}\n"],
    ids=["path", "ld_preload", "no-equals"],
)
def test_entrypoint_refuses_other_names_without_printing_the_value(tmp_path, secrets):
    out = _entrypoint(tmp_path, secrets)
    assert out.returncode == 2
    assert PRIVATE not in out.stdout + out.stderr
    assert "args=" not in out.stdout


def test_entrypoint_fails_closed_without_the_file(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "DISCORD_MCP_SECRETS": str(tmp_path / "missing")}
    out = subprocess.run(["sh", str(DEPLOY / "entrypoint.sh")], env=env, capture_output=True, text=True)
    assert out.returncode == 2
    assert out.stderr == "fatal: secrets file is missing\n"


# --- fetch-params.sh -----------------------------------------------------------

GOOD_ENV = (
    "DOMAIN=mcp.example.org\nPUBLIC_URL=https://mcp.example.org\nDISCORD_GUILD_ID=1\n"
    "DISCORD_CLIENT_ID=2\nOAUTH_CLIENT_ID=claude\n"
)


def _fetch(tmp_path: Path, values: dict[str, str], invalid: list[str] | None = None, no_block: bool = False):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    tag = tmp_path / "image-tag"
    tag.write_text("0123456789ab\n")
    response = {
        "Parameters": [{"Name": f"/discord-mcp/{k}", "Value": v} for k, v in values.items()],
        "InvalidParameters": invalid or [],
    }
    (tmp_path / "response.json").write_text(json.dumps(response))
    bin_dir = _stub(tmp_path, "aws", f'echo "$*" > "{tmp_path}/aws-args"\ncat "{tmp_path}/response.json"\n')
    _stub(tmp_path, "curl", 'case "$*" in *api/token*) echo token ;; *) echo us-west-2 ;; esac\n')
    _stub(tmp_path, "chown", 'echo "$*" >> "$CHOWN_LOG"\n')
    _stub(tmp_path, "iptables", f'echo "$*" >> "{tmp_path}/fw"\nexit {1 if no_block else 0}\n')
    _stub(tmp_path, "ip6tables", f'echo "$*" >> "{tmp_path}/fw6"\nexit 0\n')
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "DISCORD_MCP_RUN_DIR": str(run_dir),
        "DISCORD_MCP_IMAGE_TAG_FILE": str(tag),
        "CHOWN_LOG": str(tmp_path / "chown.log"),
    }
    out = subprocess.run([POSIX_SH, str(DEPLOY / "fetch-params.sh")], env=env, capture_output=True, text=True)
    return out, run_dir


def _good(**overrides):
    values = {n: f"{PRIVATE}-{n}" for n in SECRET_NAMES}
    values["config"] = '[digest]\nchannels = ["x"]\n'
    values["env"] = GOOD_ENV
    values.update(overrides)
    return values


def test_fetch_params_writes_the_three_files_with_tight_modes(tmp_path):
    out, run_dir = _fetch(tmp_path, _good())
    assert out.returncode == 0, out.stderr
    assert PRIVATE not in out.stdout + out.stderr
    secrets = (run_dir / "secrets.env").read_text()
    assert secrets == "".join(f"{n}={PRIVATE}-{n}\n" for n in SECRET_NAMES)
    compose = (run_dir / "compose.env").read_text()
    assert compose == GOOD_ENV + "IMAGE_TAG=0123456789ab\nAWS_REGION=us-west-2\n"
    assert PRIVATE not in compose
    for name in ("secrets.env", "config.toml", "compose.env"):
        assert stat.S_IMODE((run_dir / name).stat().st_mode) == 0o400
    chowned = (tmp_path / "chown.log").read_text()
    assert chowned.startswith("10001:10001 ") and "secrets.env" in chowned and "compose.env" not in chowned
    assert [p.name for p in run_dir.iterdir() if p.name.startswith(".stage")] == []
    args = (tmp_path / "aws-args").read_text()
    assert "get-parameters " in args and "--with-decryption" in args
    for n in PARAMETERS:
        assert f"/discord-mcp/{n}" in args


@pytest.mark.parametrize(
    "overrides,invalid,message",
    [
        ({}, ["/discord-mcp/ENCRYPTION_KEY"], "missing parameters: /discord-mcp/ENCRYPTION_KEY"),
        ({"DISCORD_BOT_TOKEN": f"{PRIVATE}\nsecond"}, None, "/discord-mcp/DISCORD_BOT_TOKEN is empty or has more than one line"),
        ({"DISCORD_BOT_TOKEN": ""}, None, "/discord-mcp/DISCORD_BOT_TOKEN is empty"),
        ({"env": GOOD_ENV + f"LD_PRELOAD={PRIVATE}\n"}, None, "unknown name"),
        ({"env": GOOD_ENV + f"SERVER_NAME=a ${PRIVATE}\n"}, None, "SERVER_NAME has an empty or unsafe value"),
        ({"env": GOOD_ENV + "DOMAIN=other.example.org\n"}, None, "sets DOMAIN twice"),
        ({"env": GOOD_ENV.replace("OAUTH_CLIENT_ID=claude\n", "")}, None, "missing OAUTH_CLIENT_ID"),
        ({"env": GOOD_ENV.replace("https://mcp", "https://evil")}, None, "PUBLIC_URL must be"),
        ({"env": GOOD_ENV.replace("DOMAIN=mcp", "DOMAIN=MCP").replace("https://mcp", "https://MCP")}, None, "lower-case"),
        ({"env": GOOD_ENV + "PUBLIC_URL DISCORD_GUILD_ID=x\n"}, None, "unknown name"),
    ],
    ids=["missing", "multiline", "empty", "unknown-key", "unsafe-value", "twice", "required", "url", "case", "two-keys"],
)
def test_fetch_params_fails_closed_without_printing_values(tmp_path, overrides, invalid, message):
    out, run_dir = _fetch(tmp_path, _good(**overrides), invalid)
    assert out.returncode == 1
    assert message in out.stderr
    assert PRIVATE not in out.stdout + out.stderr
    assert sorted(p.name for p in run_dir.iterdir()) == []


def test_fetch_params_refuses_to_start_without_the_metadata_block(tmp_path):
    out, run_dir = _fetch(tmp_path, _good(), no_block=True)
    assert out.returncode == 1
    assert "metadata block is missing" in out.stderr
    assert not (tmp_path / "aws-args").exists()  # stopped before reading any parameter
    rule = "-w -C OUTPUT -d 169.254.169.254 -m owner ! --uid-owner 0 -j REJECT"
    assert rule in (tmp_path / "fw").read_text()


def test_fetch_params_checks_both_metadata_rules(tmp_path):
    out, _ = _fetch(tmp_path, _good())
    assert out.returncode == 0, out.stderr
    assert "-w -C OUTPUT -d fd00:ec2::254 -m owner ! --uid-owner 0 -j REJECT" in (tmp_path / "fw6").read_text()


def test_server_requires_the_metadata_block():
    unit = (DEPLOY / "instance" / "discord-mcp.service").read_text()
    assert re.search(r"^Requires=.*\bdiscord-mcp-imds\.service\b", unit, re.M)


# --- checkout-ref.sh: only commits on origin/main ever run ---------------------


def _git(cwd: Path, *args: str) -> str:
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "HOME": str(cwd),
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.org",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repos(tmp_path):
    """An origin with two commits on main and an unmerged commit tagged
    "origin/main", and a checkout that already has that tag."""
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "commit", "-q", "--allow-empty", "-m", "old")
    old = _git(work, "rev-parse", "HEAD")
    _git(work, "commit", "-q", "--allow-empty", "-m", "good")
    good = _git(work, "rev-parse", "HEAD")
    _git(work, "checkout", "-q", "-b", "attacker")
    _git(work, "commit", "-q", "--allow-empty", "-m", "evil")
    evil = _git(work, "rev-parse", "HEAD")
    _git(work, "tag", "origin/main", evil)
    _git(work, "checkout", "-q", "main")  # the origin's default branch stays main
    origin = tmp_path / "origin.git"
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(origin))
    src = tmp_path / "src"
    _git(tmp_path, "clone", "-q", str(origin), str(src))  # an old clone: fetched the tag
    _git(src, "fetch", "-q", "--tags", "origin")
    return src, {"old": old, "good": good, "evil": evil}


def _checkout(src: Path, ref: str):
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(src), "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run([POSIX_SH, str(DEPLOY / "checkout-ref.sh"), str(src), ref], env=env, capture_output=True, text=True)


def test_checkout_main_ignores_a_tag_named_origin_main(repos):
    src, sha = repos
    out = _checkout(src, "main")
    assert out.returncode == 0, out.stderr
    assert _git(src, "rev-parse", "HEAD") == sha["good"]
    assert _git(src, "tag", "-l") == ""


def test_checkout_refuses_a_commit_not_on_main(repos):
    src, sha = repos
    out = _checkout(src, sha["evil"][:12])
    assert out.returncode == 1
    assert "not on origin/main" in out.stderr
    assert _git(src, "rev-parse", "HEAD") != sha["evil"]


def test_checkout_an_older_commit_on_main(repos):
    src, sha = repos
    out = _checkout(src, sha["old"][:7])
    assert out.returncode == 0, out.stderr
    assert _git(src, "rev-parse", "HEAD") == sha["old"]


def test_checkout_rejects_anything_but_main_or_a_sha(repos):
    src, _ = repos
    for ref in ["origin/main", "attacker", "HEAD", "main~1"]:
        assert _checkout(src, ref).returncode == 1


def test_setup_instance_uses_checkout_ref_and_install_clones_without_tags():
    assert "deploy/checkout-ref.sh" in (DEPLOY / "setup-instance.sh").read_text()
    assert "git clone --quiet --no-tags --single-branch --branch main" in (DEPLOY / "remote.sh").read_text()


# --- remote.sh -----------------------------------------------------------------

AWS_STUB = """\
echo "$*" >> "$AWS_CALLS"
case "$*" in
  *describe-instances*) echo i-0123456789abcdef0 ;;
  *send-command*) echo cmd-1 ;;
  *"--query Status"*) echo Success ;;
  *StandardOutputContent*) echo "deploy ok abc" ;;
  *StandardErrorContent*) echo "" ;;
esac
"""


def _remote(tmp_path: Path, *args: str):
    calls = tmp_path / "calls"
    calls.write_text("")
    bin_dir = _stub(tmp_path, "aws", AWS_STUB)
    _stub(tmp_path, "sleep", "exit 0\n")
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "AWS_CALLS": str(calls), "HOME": str(tmp_path)}
    out = subprocess.run(["sh", str(DEPLOY / "remote.sh"), *args], env=env, capture_output=True, text=True)
    return out, calls.read_text()


@pytest.mark.parametrize(
    "args",
    [
        ["deploy", "main; cat /run/discord-mcp/secrets.env"],
        ["deploy", "origin/main"],
        ["deploy", "ABCDEF1"],
        ["deploy", "abc12"],
        ["deploy", "a" * 41],
        ["deploy", "main", "extra"],
        ["logs", "1001"],
        ["logs", "0"],
        ["logs", "-5"],
        ["logs", "10; id"],
        ["caddy-logs", "99999"],
        ["restart", "now"],
        ["status", "x"],
        ["install", "x"],
        ["unknown"],
        [],
    ],
    ids=lambda a: " ".join(a) or "none",
)
def test_remote_rejects_bad_input_before_calling_aws(tmp_path, args):
    out, calls = _remote(tmp_path, *args)
    assert out.returncode == 2
    assert calls == ""


def _sent_command(calls: str) -> str:
    line = next(ln for ln in calls.splitlines() if "send-command" in ln)
    params = json.loads(line.split("--parameters ", 1)[1].split(" --query", 1)[0])
    return params["commands"][0]


def test_remote_deploy_sends_only_the_validated_sha(tmp_path):
    out, calls = _remote(tmp_path, "deploy", "0123abc")
    assert out.returncode == 0, out.stderr
    cmd = _sent_command(calls)
    assert "; echo 0123abc > /etc/discord-mcp/deploy-ref && systemctl start discord-mcp-deploy;" in cmd
    assert cmd.count("0123abc") == 1
    assert "deploy ok abc" in out.stdout


def test_remote_deploy_refuses_to_join_a_running_deploy(tmp_path):
    out, calls = _remote(tmp_path, "deploy")
    cmd = _sent_command(calls)
    assert cmd.startswith("if systemctl is-active --quiet discord-mcp-deploy; then echo 'a deploy is already running'; exit 1; fi; ")


def test_remote_keeps_waiting_while_cancelling_and_gives_up_eventually(tmp_path):
    text = (DEPLOY / "remote.sh").read_text()
    assert "Pending | InProgress | Delayed | Cancelling) " in text
    assert "deadline" in text


def test_remote_logs_strips_leading_zeros(tmp_path):
    out, calls = _remote(tmp_path, "logs", "010")
    assert out.returncode == 0, out.stderr
    assert _sent_command(calls) == "tail -n 10 /var/log/discord-mcp/deploy.log"


def test_remote_fails_when_the_command_fails(tmp_path):
    calls = tmp_path / "calls"
    calls.write_text("")
    bin_dir = _stub(tmp_path, "aws", AWS_STUB.replace("echo Success", "echo Failed"))
    _stub(tmp_path, "sleep", "exit 0\n")
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "AWS_CALLS": str(calls), "HOME": str(tmp_path)}
    out = subprocess.run(["sh", str(DEPLOY / "remote.sh"), "restart"], env=env, capture_output=True, text=True)
    assert out.returncode != 0
    assert "restart: Failed" in out.stderr


def test_remote_local_settings_file_is_git_ignored():
    out = subprocess.run(
        ["git", "check-ignore", "-q", "deploy/.remote.local"], cwd=ROOT, capture_output=True
    )
    assert out.returncode == 0


def test_no_deploy_file_is_git_ignored():
    # A broad "instance/" rule once hid deploy/instance/ from git.
    files = [str(p.relative_to(ROOT)) for p in DEPLOY.rglob("*") if p.is_file() and p.name != ".remote.local"]
    out = subprocess.run(["git", "check-ignore", *files], cwd=ROOT, capture_output=True, text=True)
    assert out.stdout == ""


def test_no_account_specific_values_in_deploy_files():
    for path in DEPLOY.rglob("*"):
        if path.is_file() and path.name != ".remote.local":
            text = path.read_text()
            assert not re.search(r"\b\d{12}\b", text), path  # AWS account IDs
            assert not re.search(r"\bi-[0-9a-f]{8,17}\b", text), path  # instance IDs
            assert not re.search(r"\b(vpc|subnet|sg|eipalloc)-[0-9a-f]{8,17}\b", text), path


if os.name == "nt":  # pragma: no cover
    pytestmark = pytest.mark.skip("POSIX shell tests")


def test_metadata_rule_checked_at_start_is_the_one_imds_block_inserts():
    # iptables -C matches only an identical spec, so any drift between the two
    # scripts would stop the server from starting.
    block = (DEPLOY / "imds-block.sh").read_text()
    spec = re.search(r'-w -C OUTPUT -d "\$2" (.+?) 2>/dev/null', block).group(1)
    assert f'-w -I OUTPUT 1 -d "$2" {spec}\n' in block
    fetch = (DEPLOY / "fetch-params.sh").read_text()
    assert f"iptables -w -C OUTPUT -d 169.254.169.254 {spec} 2>/dev/null" in fetch
    assert f"ip6tables -w -C OUTPUT -d fd00:ec2::254 {spec} 2>/dev/null" in fetch
    assert "add iptables 169.254.169.254" in block and "add ip6tables fd00:ec2::254" in block


def test_docker_itself_requires_the_metadata_block():
    # Containers have restart: unless-stopped, so after an unclean stop dockerd
    # brings them back by itself, without discord-mcp.service's checks.
    dropin = (DEPLOY / "instance" / "docker-requires-imds.conf").read_text()
    assert "[Unit]" in dropin
    assert re.search(r"^Requires=discord-mcp-imds\.service$", dropin, re.M)
    assert re.search(r"^After=discord-mcp-imds\.service$", dropin, re.M)
    setup = (DEPLOY / "setup-instance.sh").read_text()
    assert '"$SRC/deploy/instance/docker-requires-imds.conf" /etc/systemd/system/docker.service.d/discord-mcp-imds.conf' in setup


EXAMPLES = DEPLOY / "examples"


def test_env_example_lists_exactly_the_allowed_keys():
    allowed = re.search(r'^ENV_KEYS="([^"]+)"', (DEPLOY / "fetch-params.sh").read_text(), re.M).group(1).split()
    text = (EXAMPLES / "discord-mcp.env.example").read_text()
    keys = re.findall(r"^#? ?([A-Z_]+)=", text, re.M)
    assert sorted(keys) == sorted(allowed)
    assert len(keys) == len(set(keys))


def test_unfilled_env_example_is_refused(tmp_path):
    values = _good()
    values["env"] = (EXAMPLES / "discord-mcp.env.example").read_text()
    out, _ = _fetch(tmp_path, values)
    assert out.returncode != 0
    assert "unsafe value" in out.stderr


def test_filled_env_example_is_accepted(tmp_path):
    text = (EXAMPLES / "discord-mcp.env.example").read_text()
    filled = re.sub(r"<[^>]+>", lambda m: {"<domain>": "mcp.example.org"}.get(m.group(0), "x1"), text)
    values = _good()
    values["env"] = filled
    out, _ = _fetch(tmp_path, values)
    assert out.returncode == 0, out.stderr


def test_remote_local_example_sets_profile_and_region_only():
    text = (EXAMPLES / "remote.local.example").read_text()
    keys = re.findall(r"^([A-Z_]+)=", text, re.M)
    assert keys == ["AWS_PROFILE", "AWS_REGION"]
    assert "<" in text  # placeholders, not real values
