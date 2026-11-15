import base64

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
    dev = {**BASE, "DEV_USER_ID": "1001"}
    assert load_settings(dev).server_name == "discord-mcp"
    assert load_settings({**dev, "SERVER_NAME": "Example Assistant"}).server_name == "Example Assistant"


KEY = "A" * 43  # base64url for 32 bytes
HTTP = {
    **BASE,
    "PUBLIC_URL": "https://mcp.example.org/",
    "DISCORD_CLIENT_ID": "123",
    "DISCORD_CLIENT_SECRET": "discord-secret-value",
    "OAUTH_CLIENT_ID": "claude-client",
    "OAUTH_CLIENT_SECRET": "oauth-secret-value",
    "ENCRYPTION_KEY": KEY,
}


def test_http_mode_loads_and_strips_trailing_slash():
    s = load_settings(HTTP)
    assert s.mode == "http"
    assert s.public_url == "https://mcp.example.org"
    assert s.encryption_key == base64.urlsafe_b64decode(KEY + "=")
    assert s.oauth_redirect_uris == ("https://claude.ai/api/mcp/auth_callback",)
    assert s.host == "127.0.0.1" and s.port == 8000


def test_stdio_mode_requires_dev_user():
    assert load_settings({**BASE, "DEV_USER_ID": "1001"}).mode == "stdio"
    with pytest.raises(SettingsError):
        load_settings(BASE)


@pytest.mark.parametrize(
    "missing", ["DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "OAUTH_CLIENT_ID", "OAUTH_CLIENT_SECRET", "ENCRYPTION_KEY"]
)
def test_http_mode_requires_every_secret(missing):
    env = {k: v for k, v in HTTP.items() if k != missing}
    with pytest.raises(SettingsError) as e:
        load_settings(env)
    assert missing in str(e.value)


def test_encryption_key_must_be_32_bytes_and_is_never_echoed():
    with pytest.raises(SettingsError) as e:
        load_settings({**HTTP, "ENCRYPTION_KEY": "c2hvcnQ"})
    assert "c2hvcnQ" not in str(e.value)


def test_public_url_must_be_https_except_localhost():
    with pytest.raises(SettingsError):
        load_settings({**HTTP, "PUBLIC_URL": "http://mcp.example.org"})
    assert load_settings({**HTTP, "PUBLIC_URL": "http://localhost:8000"}).public_url == "http://localhost:8000"


def test_redirect_uris_and_listen_address_are_configurable():
    s = load_settings(
        {
            **HTTP,
            "OAUTH_REDIRECT_URIS": "https://claude.ai/api/mcp/auth_callback, http://localhost:3118/callback",
            "HOST": "0.0.0.0",
            "PORT": "9000",
        }
    )
    assert s.oauth_redirect_uris == ("https://claude.ai/api/mcp/auth_callback", "http://localhost:3118/callback")
    assert (s.host, s.port) == ("0.0.0.0", 9000)


def test_settings_repr_hides_secrets():
    text = repr(load_settings(HTTP))
    for secret in ("secret-token-value", "discord-secret-value", "oauth-secret-value", KEY):
        assert secret not in text
