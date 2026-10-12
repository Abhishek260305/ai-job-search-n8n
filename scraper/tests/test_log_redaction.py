"""The Telegram Bot API puts the bot token in the URL path, and httpx logs every request URL at INFO.
The service must keep that out of its logs (they end up in `docker compose logs`, bug reports and chat)."""
import logging
import sys
import types

import pytest

pytest.importorskip("fastapi")
sys.modules.setdefault("jobspy", types.SimpleNamespace(scrape_jobs=lambda **kw: None))

import httpx  # noqa: E402

import app as A  # noqa: E402,F401  (importing the service configures its logging)


def test_a_token_in_a_request_url_never_reaches_the_logs(caplog):
    caplog.set_level(logging.INFO)
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True})))
    client.get("https://api.telegram.org/bot123456:SECRET-TOKEN-VALUE/getUpdates")
    assert "SECRET-TOKEN-VALUE" not in caplog.text


def test_http_client_loggers_are_quiet_but_warnings_still_show():
    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING
