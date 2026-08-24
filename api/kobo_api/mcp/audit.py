"""The audit decorator every MCP tool wears (MCP server arc, Task 6):
`kobo_api.mcp.rate_limit`'s per-email sliding window, plus a redacted
record of every call that actually reached the tool.

**Order of operations**, inside `audited`'s wrapper, exactly:

1. Resolve the caller's email off the tool's `ctx` argument
   (`_email_from_ctx`).
2. Rate limit (`rate_limit.check`). Over the limit -> raise `ToolError`
   immediately, before the tool ever runs and before any row is written.
   RULING: a rate-limited call is not an audited call. It never reached
   the tool it would have logged, so there is nothing here to log --
   `rate_limit`'s own counters are the record of how many times an email
   has been refused, should that ever need surfacing.
3. Run the wrapped tool, timing it with `time.monotonic()`.
4. Write one `kobo_api.models.MCPToolCallLog` row: `"ok"` with its
   duration on success, `"error"` with the truncated exception text on
   failure -- and on failure, RE-RAISE. The tool's own return value or
   exception is always what the caller sees; the audit write happens
   around it, never in place of it.

**Write failures never break a tool call.** `configure()` hands this
module a session factory -- the same plain, non-context-manager callable
`kobo_api.main`/`kobo_api.mcp.oauth_as`'s own `get_session` already read
off `request.app.state.session_factory` (a `sessionmaker`: calling it
returns a fresh `Session` this module opens, uses, and closes itself, the
simplest shape the tool-registration layer can hand in with no wrapping
of its own). If that factory was never configured, raises when called, or
the write itself fails for any reason, `_write_log` logs the exception
(`log.exception`) and swallows it. A room that cannot reach its own
database still lets every tool call through -- the audit trail is a
nice-to-have riding alongside the tool actually working, never a gate on
it.

**Decorator order matters.** A tool is written `@mcp.tool()` on the
outside, `@audited("tool_name")` immediately under it -- `audited` must
be the INNER decorator so the SDK's own signature/docstring introspection
of the `@mcp.tool()`-wrapped function sees straight through to the real
tool. `functools.wraps` below sets `__wrapped__`, which `inspect.
signature` (what the SDK's tool registration uses to build a JSON schema)
follows by default, so the wrapper is transparent to that introspection
without this module doing anything more elaborate.
"""

from __future__ import annotations

import functools
import json
import logging
import time
from collections.abc import Callable
from typing import Any, TypeVar

from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy.orm import Session, sessionmaker

from kobo_api.mcp import rate_limit
from kobo_api.models import MCPToolCallLog

log = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

# A kwarg is redacted whole the moment its lowercased name CONTAINS any of
# these substrings -- substring, not exact match, so e.g. a `slack_bot_
# token` kwarg is caught by "bot_token" without this set needing to grow
# for every new name that happens to embed one of these words.
SENSITIVE_KEYS = {
    "note",
    "message",
    "bot_token",
    "app_token",
    "secret",
    "token",
    "value",
    "secret_values",
    "content",
}

_REDACTED = "***REDACTED***"

# Values are BOUNDED, not name-redacted: the persona wave's what_they_do
# (49152-char wall) would otherwise land whole in every audit row. Name
# redaction (SENSITIVE_KEYS) still wins first and redacts the value whole.
_ARG_TEXT_CAP = 512

# Bounding caps (hardening wave): 512 chars per string, 64 elements per
# list (with an explicit dropped-count marker), 8 levels of nesting. The
# audit path must never crash, hang, or balloon a tool call, whatever
# shape a future tool's kwargs take.
_MAX_LIST_ELEMENTS = 64
_MAX_DEPTH = 8


def _bounded(value: Any, depth: int = 0) -> Any:
    """Cap string values at _ARG_TEXT_CAP with an explicit truncation
    marker; lists are bounded per element AND per count (first
    _MAX_LIST_ELEMENTS kept, one "[+N more]" marker appended); dict
    VALUES are bounded recursively (keys are schema-shaped, not user
    text). Anything nested past _MAX_DEPTH collapses to a sentinel.
    Always returns NEW lists/dicts -- callers' kwargs are never mutated.
    Everything else passes through for json.dumps's default=str."""
    if depth >= _MAX_DEPTH:
        return "[depth capped]"
    if isinstance(value, str) and len(value) > _ARG_TEXT_CAP:
        return value[:_ARG_TEXT_CAP] + f"...[truncated {len(value) - _ARG_TEXT_CAP} chars]"
    if isinstance(value, list):
        bounded = [_bounded(item, depth + 1) for item in value[:_MAX_LIST_ELEMENTS]]
        dropped = len(value) - _MAX_LIST_ELEMENTS
        if dropped > 0:
            bounded.append(f"[+{dropped} more]")
        return bounded
    if isinstance(value, dict):
        return {key: _bounded(item, depth + 1) for key, item in value.items()}
    return value


# Stashed by configure() -- see module docstring for why this is a plain
# sessionmaker rather than a context manager. None until a real
# registration (kobo_api.mcp.server, Task 7) or a test calls configure().
_session_factory: sessionmaker[Session] | None = None


def configure(session_factory: sessionmaker[Session] | None) -> None:
    """Stash the session factory `audited` writes log rows through. Called
    once at MCP server registration time; tests call it directly with
    their own sqlite-backed factory, or with `None` to simulate an
    unconfigured (or torn-down) audit layer.
    """
    global _session_factory
    _session_factory = session_factory


def _email_from_ctx(ctx: Any) -> str:
    """The verified caller email off an MCP tool's `ctx` argument, per the
    MCP server design's `ctx.request_context.request.user.access_token.
    user_email` -- read as a defensive getattr chain, not a straight-line
    attribute access, because this module must never let a malformed or
    stubbed `ctx` (a test double, SDK version drift) raise here and crash
    a tool call before the tool itself even runs. Missing anything along
    the chain resolves to `""`, the same "caller unknown" value an actual
    accessor (a later task's own `get_user_email`) would need to handle
    from a genuinely anonymous ctx anyway.
    """
    request_context = getattr(ctx, "request_context", None)
    request = getattr(request_context, "request", None)
    user = getattr(request, "user", None)
    access_token = getattr(user, "access_token", None)
    email = getattr(access_token, "user_email", None)
    return email or ""


def _redact(kwargs: dict[str, Any]) -> dict[str, Any]:
    """A copy of `kwargs` with every key whose lowercased name contains any
    `SENSITIVE_KEYS` substring replaced by `_REDACTED`; every other key's
    value is bounded (`_bounded`), never passed through unchanged -- a long
    string is capped at `_ARG_TEXT_CAP` with a truncation marker, a list is
    bounded per element and per count, a dict per value, and everything
    else passes straight to `json.dumps`'s own `default=str`.
    """
    redacted: dict[str, Any] = {}
    for key, value in kwargs.items():
        lowered = key.lower()
        if any(sensitive in lowered for sensitive in SENSITIVE_KEYS):
            redacted[key] = _REDACTED
        else:
            redacted[key] = _bounded(value)
    return redacted


def _write_log(
    *,
    email: str,
    tool: str,
    status: str,
    duration_ms: int,
    kwargs: dict[str, Any],
    error: str | None,
) -> None:
    """Write one audit row, or give up quietly. See module docstring:
    nothing raised here is allowed to escape and take a tool call down
    with it.
    """
    if _session_factory is None:
        return
    try:
        redacted_args = json.dumps(_redact(kwargs), default=str)
        session = _session_factory()
        try:
            session.add(
                MCPToolCallLog(
                    email=email,
                    tool=tool,
                    status=status,
                    duration_ms=duration_ms,
                    redacted_args=redacted_args,
                    error=error,
                )
            )
            session.commit()
        finally:
            session.close()
    except Exception:
        log.exception("failed to write MCP tool call audit row for tool=%s", tool)


def audited(tool_name: str) -> Callable[[F], F]:
    """The decorator every MCP tool wears -- see module docstring for the
    exact order of operations and why it is that order. `tool_name` is the
    name recorded on every `MCPToolCallLog` row this call produces (a
    tool's own registered MCP name, not necessarily its Python function
    name).
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(ctx: Any, *args: Any, **kwargs: Any) -> Any:
            email = _email_from_ctx(ctx)

            if not rate_limit.check(email):
                raise ToolError(
                    f"Rate limit exceeded: {rate_limit.LIMIT} requests per minute."
                )

            start = time.monotonic()
            try:
                result = await func(ctx, *args, **kwargs)
            except Exception as exc:
                duration_ms = int((time.monotonic() - start) * 1000)
                _write_log(
                    email=email,
                    tool=tool_name,
                    status="error",
                    duration_ms=duration_ms,
                    kwargs=kwargs,
                    error=str(exc)[:500],
                )
                raise

            duration_ms = int((time.monotonic() - start) * 1000)
            _write_log(
                email=email,
                tool=tool_name,
                status="ok",
                duration_ms=duration_ms,
                kwargs=kwargs,
                error=None,
            )
            return result

        return wrapper  # type: ignore[return-value]

    return decorator
