"""Tests for kobo_api.mcp.rate_limit and kobo_api.mcp.audit (MCP server
arc, Task 6): the per-email sliding window every tool call checks before
it runs, and the `@audited` decorator that enforces it and writes a
redacted record of every call that actually reached the tool.

No FastMCP server, no real MCP wire traffic -- `audited` is exercised
directly against small `async def` stand-in "tools", the same way
`test_mcp_refresh_tokens.py` exercises its module's functions directly
against a plain session rather than through a live route. House pattern
for driving async code from a plain (non-asyncio) test function, per
`test_clients.py`'s own precedent (no pytest-asyncio in this repo):
`asyncio.run(...)`.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from kobo_api.mcp import audit, rate_limit
from kobo_api.models import MCPToolCallLog

EMAIL = "person@example.com"
OTHER_EMAIL = "someone-else@example.com"


@pytest.fixture(autouse=True)
def _reset_rate_limit_and_audit(monkeypatch):
    """Every test starts with a clean sliding window and an unconfigured
    audit layer, so one test's calls/rows never bleed into the next."""
    rate_limit.reset()
    monkeypatch.setattr(audit, "_session_factory", None)
    yield
    rate_limit.reset()


# --- stub ctx: the defensive getattr chain --------------------------------


class _StubAccessToken:
    def __init__(self, user_email):
        self.user_email = user_email


class _StubUser:
    def __init__(self, user_email):
        self.access_token = _StubAccessToken(user_email)


class _StubRequest:
    def __init__(self, user_email):
        self.user = _StubUser(user_email)


class _StubRequestContext:
    def __init__(self, user_email):
        self.request = _StubRequest(user_email)


class _StubCtx:
    """A stand-in for the real MCP `ctx` argument, shaped exactly like the
    design spec's `ctx.request_context.request.user.access_token.
    user_email` chain."""

    def __init__(self, user_email):
        self.request_context = _StubRequestContext(user_email)


def _ctx(email=EMAIL):
    return _StubCtx(email)


# --- _email_from_ctx: defensive chain --------------------------------------


def test_email_from_ctx_reads_the_full_chain():
    assert audit._email_from_ctx(_ctx(EMAIL)) == EMAIL


def test_email_from_ctx_missing_request_context_returns_empty():
    class _Empty:
        pass

    assert audit._email_from_ctx(_Empty()) == ""


def test_email_from_ctx_none_returns_empty():
    assert audit._email_from_ctx(None) == ""


def test_email_from_ctx_missing_request_returns_empty():
    class _NoRequest:
        request_context = object()

    assert audit._email_from_ctx(_NoRequest()) == ""


def test_email_from_ctx_missing_access_token_returns_empty():
    class _Ctx:
        class request_context:
            class request:
                user = object()

    assert audit._email_from_ctx(_Ctx()) == ""


def test_email_from_ctx_user_email_none_returns_empty():
    assert audit._email_from_ctx(_ctx(None)) == ""


# --- _redact ----------------------------------------------------------------


def test_redact_replaces_exactly_the_sensitive_keys():
    kwargs = {
        "note": "hello",
        "message": "hi",
        "bot_token": "xoxb-1",
        "app_token": "xapp-1",
        "secret": "s3cr3t",
        "token": "abc",
        "value": "v",
        "secret_values": {"a": "b"},
        "name": "my-agent",
        "limit": 50,
        "agent": "kobo-1",
    }

    redacted = audit._redact(kwargs)

    for key in (
        "note",
        "message",
        "bot_token",
        "app_token",
        "secret",
        "token",
        "value",
        "secret_values",
    ):
        assert redacted[key] == "***REDACTED***"
    assert redacted["name"] == "my-agent"
    assert redacted["limit"] == 50
    assert redacted["agent"] == "kobo-1"


def test_redact_is_case_insensitive_and_matches_substrings():
    kwargs = {"BOT_TOKEN": "xoxb-1", "SlackAppToken": "xapp-1", "greeting": "hi"}

    redacted = audit._redact(kwargs)

    assert redacted["BOT_TOKEN"] == "***REDACTED***"
    assert redacted["SlackAppToken"] == "***REDACTED***"
    assert redacted["greeting"] == "hi"


def test_redact_does_not_mutate_the_input():
    kwargs = {"token": "abc"}
    audit._redact(kwargs)
    assert kwargs["token"] == "abc"


# --- rate_limit.check: window + limit ---------------------------------------


def test_check_allows_up_to_limit_calls():
    for _ in range(rate_limit.LIMIT):
        assert rate_limit.check(EMAIL) is True


def test_check_refuses_the_call_over_the_limit():
    for _ in range(rate_limit.LIMIT):
        assert rate_limit.check(EMAIL) is True

    assert rate_limit.check(EMAIL) is False


def test_check_is_per_email():
    for _ in range(rate_limit.LIMIT):
        assert rate_limit.check(EMAIL) is True

    assert rate_limit.check(EMAIL) is False
    # A different email has its own, untouched window.
    assert rate_limit.check(OTHER_EMAIL) is True


def test_check_window_expiry_via_monkeypatched_time(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(rate_limit.time, "monotonic", lambda: now[0])

    for _ in range(rate_limit.LIMIT):
        assert rate_limit.check(EMAIL) is True
    assert rate_limit.check(EMAIL) is False

    # Advance past the window: every old timestamp should be pruned, so a
    # fresh full quota is available again.
    now[0] += rate_limit.WINDOW_S + 0.001

    assert rate_limit.check(EMAIL) is True


def test_check_window_partial_expiry_frees_exactly_the_expired_slots(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(rate_limit.time, "monotonic", lambda: now[0])

    # Use up the whole window's quota at t=1000.
    for _ in range(rate_limit.LIMIT):
        assert rate_limit.check(EMAIL) is True
    assert rate_limit.check(EMAIL) is False

    # Move to just past the window's edge -- every one of those timestamps
    # is now older than WINDOW_S and should prune away.
    now[0] = 1000.0 + rate_limit.WINDOW_S + 0.001
    assert rate_limit.check(EMAIL) is True

    # But the quota consumed by the calls made AT the new time (1 so far)
    # is real: exhausting the rest of LIMIT-1 more calls at this same
    # instant should still hit the ceiling.
    for _ in range(rate_limit.LIMIT - 1):
        assert rate_limit.check(EMAIL) is True
    assert rate_limit.check(EMAIL) is False


def test_reset_one_email_clears_only_that_email():
    for _ in range(rate_limit.LIMIT):
        rate_limit.check(EMAIL)
    for _ in range(rate_limit.LIMIT):
        rate_limit.check(OTHER_EMAIL)

    rate_limit.reset(EMAIL)

    assert rate_limit.check(EMAIL) is True
    assert rate_limit.check(OTHER_EMAIL) is False


def test_reset_all_clears_every_email():
    for _ in range(rate_limit.LIMIT):
        rate_limit.check(EMAIL)
    for _ in range(rate_limit.LIMIT):
        rate_limit.check(OTHER_EMAIL)

    rate_limit.reset()

    assert rate_limit.check(EMAIL) is True
    assert rate_limit.check(OTHER_EMAIL) is True


# --- audited(): rate limiting integrates with the decorator -----------------


def test_audited_60_calls_pass_61st_raises_tool_error():
    @audit.audited("kobo_test_tool")
    async def tool(ctx, **kwargs):
        return "ok"

    async def _run():
        for _ in range(rate_limit.LIMIT):
            assert await tool(_ctx()) == "ok"
        with pytest.raises(ToolError, match="Rate limit exceeded"):
            await tool(_ctx())

    asyncio.run(_run())


def test_audited_rate_limited_call_writes_no_row(session_factory):
    audit.configure(session_factory)

    @audit.audited("kobo_test_tool")
    async def tool(ctx, **kwargs):
        return "ok"

    async def _run():
        for _ in range(rate_limit.LIMIT):
            await tool(_ctx())
        with pytest.raises(ToolError):
            await tool(_ctx())

    asyncio.run(_run())

    session = session_factory()
    try:
        rows = session.query(MCPToolCallLog).all()
    finally:
        session.close()
    # LIMIT successful calls logged, the refused 61st logs nothing.
    assert len(rows) == rate_limit.LIMIT


def test_audited_rate_limit_message_is_exact():
    @audit.audited("kobo_test_tool")
    async def tool(ctx, **kwargs):
        return "ok"

    async def _run():
        for _ in range(rate_limit.LIMIT):
            await tool(_ctx())
        try:
            await tool(_ctx())
        except ToolError as exc:
            return str(exc)
        raise AssertionError("expected ToolError")

    message = asyncio.run(_run())
    assert message == "Rate limit exceeded: 60 requests per minute."


# --- audited(): the ok row -----------------------------------------------


def test_audited_ok_call_writes_a_row_with_status_and_duration(session_factory):
    audit.configure(session_factory)

    @audit.audited("kobo_whoami")
    async def tool(ctx, **kwargs):
        return {"email": EMAIL}

    result = asyncio.run(tool(_ctx()))
    assert result == {"email": EMAIL}

    session = session_factory()
    try:
        rows = session.query(MCPToolCallLog).all()
    finally:
        session.close()

    assert len(rows) == 1
    row = rows[0]
    assert row.email == EMAIL
    assert row.tool == "kobo_whoami"
    assert row.status == "ok"
    assert isinstance(row.duration_ms, int)
    assert row.duration_ms >= 0
    assert row.error is None


def test_audited_ok_call_redacts_the_logged_kwargs(session_factory):
    audit.configure(session_factory)

    @audit.audited("kobo_teach")
    async def tool(ctx, agent, note):
        return "done"

    asyncio.run(tool(_ctx(), agent="kobo-1", note="a secret note"))

    session = session_factory()
    try:
        row = session.query(MCPToolCallLog).one()
    finally:
        session.close()

    logged = json.loads(row.redacted_args)
    assert logged["agent"] == "kobo-1"
    assert logged["note"] == "***REDACTED***"


# --- audited(): the error row + re-raise ------------------------------------


def test_audited_raising_tool_writes_error_row_and_reraises(session_factory):
    audit.configure(session_factory)

    long_message = "boom " * 200  # well over 500 chars

    @audit.audited("kobo_broken")
    async def tool(ctx, **kwargs):
        raise ValueError(long_message)

    with pytest.raises(ValueError, match="boom"):
        asyncio.run(tool(_ctx()))

    session = session_factory()
    try:
        row = session.query(MCPToolCallLog).one()
    finally:
        session.close()

    assert row.status == "error"
    assert row.error is not None
    assert len(row.error) <= 500
    assert row.error == str(ValueError(long_message))[:500]


# --- audited(): a broken session factory never breaks the call ------------


def test_audited_broken_session_factory_does_not_break_the_call(caplog):
    def _broken_factory():
        raise RuntimeError("db is down")

    audit.configure(_broken_factory)

    @audit.audited("kobo_test_tool")
    async def tool(ctx, **kwargs):
        return "still works"

    with caplog.at_level(logging.ERROR, logger="kobo_api.mcp.audit"):
        result = asyncio.run(tool(_ctx()))

    assert result == "still works"
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_audited_unconfigured_session_factory_does_not_break_the_call():
    # No audit.configure() call at all in this test -- _session_factory
    # stays None (reset by the autouse fixture).
    @audit.audited("kobo_test_tool")
    async def tool(ctx, **kwargs):
        return "still works"

    assert asyncio.run(tool(_ctx())) == "still works"


def test_audited_broken_session_factory_still_lets_a_raising_tool_reraise():
    def _broken_factory():
        raise RuntimeError("db is down")

    audit.configure(_broken_factory)

    @audit.audited("kobo_broken")
    async def tool(ctx, **kwargs):
        raise ValueError("tool broke too")

    with pytest.raises(ValueError, match="tool broke too"):
        asyncio.run(tool(_ctx()))


# --- audited(): functools.wraps preserves the wrapped function -------------


def test_audited_preserves_function_name_and_wrapped():
    @audit.audited("kobo_named_tool")
    async def my_tool(ctx, **kwargs):
        return None

    assert my_tool.__name__ == "my_tool"
    assert my_tool.__wrapped__ is not None
