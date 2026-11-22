import io
import json
import logging

import pytest

from discord_mcp import audit

SENTINEL = "PRIVATE-MESSAGE-TEXT-42"
ALLOWED = {"user_id", "tool", "channel_ids", "item_count", "duration_ms", "status"}


@pytest.fixture
def stream():
    s = io.StringIO()
    restore = audit.configure_logging(s)
    yield s
    restore()


def test_audit_line_has_only_allowed_fields(stream):
    audit.record(user_id="1001", tool="get_my_pings", channel_ids=["10", "11"], item_count=3, duration_ms=12, status="ok")
    line = json.loads(stream.getvalue().strip())
    assert set(line) == ALLOWED
    assert line["channel_ids"] == ["10", "11"]


def test_non_numeric_ids_are_not_written(stream):
    audit.record(user_id=SENTINEL, tool="get_my_pings", channel_ids=[SENTINEL], item_count=0, duration_ms=0, status="ok")
    out = stream.getvalue()
    assert SENTINEL not in out
    assert '"invalid"' in out


def test_unknown_tool_or_status_text_is_not_written(stream):
    audit.record(user_id="1", tool=SENTINEL, channel_ids=[], item_count=0, duration_ms=0, status=SENTINEL)
    assert SENTINEL not in stream.getvalue()


def test_other_loggers_are_silenced_including_exceptions(stream):
    logging.getLogger("mcp.server").error("tool failed: %s", SENTINEL)
    logging.getLogger("httpx").warning(SENTINEL)
    try:
        raise RuntimeError(SENTINEL)
    except RuntimeError:
        logging.getLogger("mcp.server.mcpserver").exception("Tool raised")
        logging.getLogger("discord_mcp").exception("boom")
    logging.getLogger().critical(SENTINEL)
    assert stream.getvalue() == ""


# --- uncaught errors outside the main thread print only their type ----------------

import gc  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402

PRIVATE = "PRIVATE-MESSAGE-TEXT-42"


def test_thread_errors_print_only_the_type(monkeypatch, capsys):
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    audit.install_hooks()

    def boom():
        raise ValueError(PRIVATE)

    t = threading.Thread(target=boom)
    t.start()
    t.join()
    err = capsys.readouterr().err
    assert err == "fatal: ValueError\n"
    assert PRIVATE not in err


def test_unraisable_errors_print_only_the_type(monkeypatch, capsys):
    monkeypatch.setattr(sys, "unraisablehook", sys.unraisablehook)
    audit.install_hooks()

    class Leaky:
        def __del__(self):
            raise RuntimeError(PRIVATE)

    Leaky()
    gc.collect()
    err = capsys.readouterr().err
    assert err == "fatal: RuntimeError\n"
    assert PRIVATE not in err


def test_install_hooks_covers_the_main_thread_too(monkeypatch):
    for mod, name in ((sys, "excepthook"), (sys, "unraisablehook"), (threading, "excepthook")):
        monkeypatch.setattr(mod, name, getattr(mod, name))
    audit.install_hooks()
    assert sys.excepthook is audit.safe_excepthook
    assert sys.unraisablehook is audit.safe_unraisablehook
    assert threading.excepthook is audit.safe_threading_excepthook
