from pathlib import Path

import pytest

from discord_mcp.config import ConfigError, load_config

ROOT = Path(__file__).resolve().parent.parent


def test_shipped_config_starts_with_empty_allowlist_and_rollout_comment():
    text = (ROOT / "config.toml").read_text()
    cfg = load_config(ROOT / "config.toml")
    assert cfg.allowlist == {}
    assert cfg.deny == frozenset()
    lowered = text.lower()
    assert "common team channels" in lowered
    assert "leadership and hr" in lowered


def test_loads_allowlist_deny_and_limits(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(
        """
allowlist = [
  { id = "10", name = "general", phase = 1 },
  { id = "11", name = "random", phase = 1 },
]
deny = [ { user_id = "1001", channel_id = "11" } ]
[limits]
summary_chars = 150
"""
    )
    cfg = load_config(p)
    assert set(cfg.allowlist) == {"10", "11"}
    assert cfg.allowlist["10"].phase == 1
    assert cfg.deny == frozenset({("1001", "11")})
    assert cfg.limits.summary_chars == 150
    assert cfg.limits.max_days == 14  # untouched default


def test_rejects_non_numeric_channel_id(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('allowlist = [ { id = "general", phase = 1 } ]\n')
    with pytest.raises(ConfigError):
        load_config(p)


def test_new_conversation_limits_have_defaults(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("allowlist = []\n")
    limits = load_config(p).limits
    assert (
        limits.conversation_gap_hours,
        limits.conversation_max_hours,
        limits.summary_chars,
        limits.max_result_chars,
        limits.conversation_max_messages,
        limits.row_max_pings,
    ) == (12, 24, 200, 40000, 50, 5)


def test_rejects_unknown_limit(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("allowlist = []\n[limits]\nmax_itemz = 5\n")
    with pytest.raises(ConfigError):
        load_config(p)


def test_removed_follow_up_limit_is_rejected(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("allowlist = []\n[limits]\nfollow_up_count = 10\n")
    with pytest.raises(ConfigError, match="unknown limits"):
        load_config(p)


def test_watch_list_loads(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('allowlist = [ { id = "10", phase = 1, watch = ["submit", "due"] } ]\n')
    assert load_config(p).allowlist["10"].watch == ("submit", "due")


def test_watch_defaults_to_empty(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('allowlist = [ { id = "10", phase = 1 } ]\n')
    assert load_config(p).allowlist["10"].watch == ()


@pytest.mark.parametrize("watch", ['["x"]', "[" + ", ".join(f'"w{i:02d}"' for i in range(31)) + "]", '"submit"'])
def test_watch_rejects_bad_words(tmp_path, watch):
    p = tmp_path / "c.toml"
    p.write_text(f'allowlist = [ {{ id = "10", phase = 1, watch = {watch} }} ]\n')
    with pytest.raises(ConfigError):
        load_config(p)


def test_overview_limits_defaults(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("allowlist = []\n")
    lim = load_config(p).limits
    assert (
        lim.overview_max_days, lim.overview_latest, lim.overview_key_messages, lim.opening_chars,
        lim.main_topic_gap_hours, lim.main_topic_max_hours, lim.search_matches_per_topic, lim.overview_max_messages,
    ) == (30, 2, 2, 300, 24, 72, 5, 3000)


def test_allow_all_and_deny_channels_load(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('allow_all_channels = true\ndeny_channels = [ { id = "5", note = "leadership" }, { id = "6" } ]\nallowlist = []\n')
    cfg = load_config(p)
    assert cfg.allow_all_channels is True
    assert cfg.deny_channels == frozenset({"5", "6"})


def test_allow_all_defaults_off(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("allowlist = []\n")
    cfg = load_config(p)
    assert cfg.allow_all_channels is False
    assert cfg.deny_channels == frozenset()


@pytest.mark.parametrize("text", ['allow_all_channels = "yes"\n', 'deny_channels = [ { id = "abc" } ]\n', 'deny_channels = ["5"]\n'])
def test_allow_all_and_deny_channels_reject_bad_values(tmp_path, text):
    p = tmp_path / "c.toml"
    p.write_text(text + "allowlist = []\n")
    with pytest.raises(ConfigError):
        load_config(p)


def test_misspelled_top_level_key_is_rejected(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('allow_all_channels = true\ndeny_channel = [ { id = "5" } ]\nallowlist = []\n')
    with pytest.raises(ConfigError, match="unknown settings"):
        load_config(p)


def test_pings_mode_loads(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('allowlist = [ { id = "10", phase = 1, pings = "direct_only" } ]\n')
    assert load_config(p).allowlist["10"].pings == "direct_only"


def test_pings_mode_defaults_to_all(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('allowlist = [ { id = "10", phase = 1 } ]\n')
    assert load_config(p).allowlist["10"].pings == "all"


def test_unknown_pings_mode_is_rejected(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('allowlist = [ { id = "10", phase = 1, pings = "direct" } ]\n')
    with pytest.raises(ConfigError, match="pings must be one of"):
        load_config(p)


@pytest.mark.parametrize(
    "toml",
    [
        'allowlist = [ { id = "10", phase = 1, ping = "direct_only" } ]\n',
        'deny_channels = [ { id = "10", nte = "x" } ]\n',
        'deny = [ { user_id = "1", channel_id = "10", chanel = "x" } ]\n',
    ],
)
def test_unknown_keys_inside_entries_are_rejected(tmp_path, toml):
    p = tmp_path / "c.toml"
    p.write_text(toml)
    with pytest.raises(ConfigError, match=r"unknown \w+ settings: \['(ping|nte|chanel)'\]"):
        load_config(p)


def test_known_optional_keys_still_load(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(
        'allowlist = [ { id = "10", name = "x", phase = 1, watch = ["due"], pings = "direct_only" } ]\n'
        'deny_channels = [ { id = "11", note = "x" } ]\n'
        'deny = [ { user_id = "1", channel_id = "10", note = "x" } ]\n'
    )
    assert load_config(p).allowlist["10"].pings == "direct_only"


@pytest.mark.parametrize("toml", ['allowlist = ["10"]\n', 'deny = ["1"]\n', 'allowlist = "10"\n'])
def test_malformed_entries_are_config_errors(tmp_path, toml):
    p = tmp_path / "c.toml"
    p.write_text(toml)
    with pytest.raises(ConfigError):
        load_config(p)
