import pytest

from discord_mcp.settings import SettingsError, load_settings

BASE = {"DISCORD_BOT_TOKEN": "secret-token-value", "DISCORD_GUILD_ID": "900"}


def test_loads_from_environment():
    s = load_settings({**BASE, "DEV_USER_ID": "1001"})
    assert s.bot_token == "secret-token-value"
    assert s.guild_id == "900"
    assert s.dev_user_id == "1001"
    assert s.public_url is None


def test_missing_token_fails_without_echoing_values():
    with pytest.raises(SettingsError) as e:
        load_settings({"DISCORD_GUILD_ID": "900"})
    assert "DISCORD_BOT_TOKEN" in str(e.value)


def test_dev_user_with_public_url_is_refused():
    with pytest.raises(SettingsError):
        load_settings({**BASE, "DEV_USER_ID": "1001", "PUBLIC_URL": "https://digest.example.org"})


def test_error_never_contains_secret():
    with pytest.raises(SettingsError) as e:
        load_settings({**BASE, "DISCORD_GUILD_ID": "not-a-number"})
    assert "secret-token-value" not in str(e.value)
    assert "not-a-number" not in str(e.value)


def test_server_name_defaults_and_can_be_set():
    assert load_settings(BASE).server_name == "discord-mcp"
    assert load_settings({**BASE, "SERVER_NAME": "Example Assistant"}).server_name == "Example Assistant"
