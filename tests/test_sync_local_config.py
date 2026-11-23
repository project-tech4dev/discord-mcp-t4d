"""scripts/sync_local_config.py: rebuild a local config from config.toml, keeping its values."""

import tomllib

import pytest

from scripts.sync_local_config import SyncError, sync

TEMPLATE = """\
# Header from the template.

# Channels read. Example:
#   allowlist = [{ id = "123456789012345678", name = "x", phase = 1 }]
allowlist = []

# Time zone.
timezone = "UTC"

[limits]
default_days = 7     # days looked back
max_days = 30        # longest look-back
"""

SHORT_LOCAL = """\
timezone = "Europe/Berlin"
allowlist = [
  { id = "111111111111111111", name = "a", phase = 1 },  # first
  { id = "222222222222222222", name = "b # not a comment", phase = 2 },
]

[limits]
max_days = 14
"""


def test_keeps_every_local_value_and_takes_the_rest_from_the_template():
    out = sync(TEMPLATE, SHORT_LOCAL)
    got = tomllib.loads(out)
    assert got["timezone"] == "Europe/Berlin"
    assert [e["name"] for e in got["allowlist"]] == ["a", "b # not a comment"]
    assert got["limits"] == {"default_days": 7, "max_days": 14}


def test_marks_only_changed_values_and_keeps_template_comments():
    out = sync(TEMPLATE, SHORT_LOCAL)
    assert "# CUSTOM (default: \"UTC\")\ntimezone = \"Europe/Berlin\"" in out
    assert "# CUSTOM (default: 30)\nmax_days = 14        # longest look-back" in out
    assert "default_days = 7     # days looked back" in out
    assert out.count("# CUSTOM") == 3
    assert "#   allowlist = [{ id" in out  # the commented example is untouched
    assert "},  # first" in out  # comments inside a local array survive


def test_a_value_equal_to_the_default_is_not_marked():
    out = sync(TEMPLATE, 'timezone = "UTC"\n')
    assert "CUSTOM" not in out


def test_running_again_changes_nothing_and_keeps_notes():
    local = SHORT_LOCAL.replace('timezone = "Europe/Berlin"', '# NOTE: our office.\ntimezone = "Europe/Berlin"')
    once = sync(TEMPLATE, local)
    assert "# NOTE: our office.\n# CUSTOM (default: \"UTC\")\ntimezone" in once
    assert sync(TEMPLATE, once) == once


def test_a_setting_missing_from_the_template_is_refused():
    with pytest.raises(SyncError, match="not in the template"):
        sync(TEMPLATE, "allow_all_chanels = true\n")


def test_a_local_file_that_is_not_valid_toml_is_refused():
    with pytest.raises(SyncError, match="not valid TOML"):
        sync(TEMPLATE, "timezone = \n")


def test_a_fresh_copy_of_the_real_template_is_already_up_to_date():
    from pathlib import Path

    real = Path("config.toml").read_text()
    assert sync(real, real) == real


def test_a_single_space_before_a_template_comment_is_kept():
    template = "[limits]\nx = 50 # note\n"
    assert sync(template, template) == template


def test_a_rebuild_keeps_the_file_private(tmp_path):
    import os

    from scripts.sync_local_config import main

    (tmp_path / "t.toml").write_text(TEMPLATE)
    local = tmp_path / "l.toml"
    local.write_text('timezone = "Europe/Berlin"\n')
    local.chmod(0o600)
    old = os.umask(0o022)
    try:
        assert main(["--template", str(tmp_path / "t.toml"), "--local", str(local)]) == 0
    finally:
        os.umask(old)
    assert local.stat().st_mode & 0o777 == 0o600
    assert "CUSTOM" in local.read_text()
    assert not (tmp_path / "l.toml.new").exists()


def test_a_symlinked_local_file_is_refused(tmp_path):
    from scripts.sync_local_config import main

    (tmp_path / "t.toml").write_text(TEMPLATE)
    real = tmp_path / "real.toml"
    real.write_text('timezone = "Europe/Berlin"\n')
    (tmp_path / "l.toml").symlink_to(real)
    assert main(["--template", str(tmp_path / "t.toml"), "--local", str(tmp_path / "l.toml")]) == 1
    assert real.read_text() == 'timezone = "Europe/Berlin"\n'
