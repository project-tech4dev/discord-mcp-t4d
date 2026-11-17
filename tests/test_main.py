import io
import logging

from discord_mcp import audit
from discord_mcp.__main__ import uvicorn_config
from tests.test_http_app import ENV

from discord_mcp.settings import load_settings

SENTINEL = "PRIVATE-MESSAGE-TEXT-42"


async def dummy_app(scope, receive, send):
    pass


def test_uvicorn_has_no_access_log_and_adds_no_handlers():
    stream = io.StringIO()
    restore = audit.configure_logging(stream)
    try:
        before = list(logging.getLogger().handlers)
        cfg = uvicorn_config(dummy_app, load_settings(ENV))
        assert cfg.access_log is False
        assert cfg.log_config is None
        assert cfg.server_header is False
        assert logging.getLogger().handlers == before
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            logger = logging.getLogger(name)
            assert logger.handlers == []
            logger.error("GET /oauth/discord/callback?code=%s", SENTINEL)
        assert SENTINEL not in stream.getvalue()
    finally:
        restore()


def test_uvicorn_runs_one_worker_even_if_the_environment_asks_for_more(monkeypatch):
    # Sign-in state lives in this process's memory (issue #3): a second worker would
    # refuse the first one's codes and refresh tokens. WEB_CONCURRENCY must not add one.
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    cfg = uvicorn_config(dummy_app, load_settings(ENV))
    assert cfg.workers == 1
