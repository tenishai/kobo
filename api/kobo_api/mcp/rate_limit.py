"""Per-email sliding-window rate limiter for kobo's MCP tool surface (MCP
server arc, Task 6): every tool call checks in here, via
`kobo_api.mcp.audit.audited`, before the tool itself ever runs.

**Per-process only.** State lives in a module-level dict, never persisted
and never shared across replicas. This is deliberately not the room's
answer to distributed abuse -- it is a single-replica speed bump against a
runaway or misbehaving client. Should the MCP surface ever run more than
one replica, each replica enforces its own independent ceiling rather than
one shared limit; closing that gap would need a shared store (redis, the
room's own DB) this module does not reach for.

**Concurrency.** No `threading.Lock` guards `_calls` below, on purpose:
kobo's MCP surface runs on a single asyncio event loop in a single worker
process, so every read-modify-write on this dict happens on one thread,
and CPython's GIL makes each individual dict/deque operation atomic
against another OS thread even if one were introduced. A second worker
process would need the distributed limiter this module explicitly is not
(see above), at which point an in-process lock would not have helped
anyway.
"""

from __future__ import annotations

import collections
import time

LIMIT = 60
WINDOW_S = 60.0

# email -> deque of time.monotonic() timestamps within the last WINDOW_S,
# oldest first.
_calls: dict[str, collections.deque[float]] = collections.defaultdict(
    collections.deque
)


def check(email: str) -> bool:
    """True if `email` may make another call right now, and records this
    call toward its window; False if it is already at `LIMIT` calls within
    the last `WINDOW_S` seconds, in which case nothing is recorded -- a
    refused call never happened, so it must not count toward anything.

    Every call -- whether it returns True or False -- prunes timestamps
    older than the window first, so an email that stops calling for a
    while has its history age out on its own with no separate cleanup
    pass needed anywhere.
    """
    now = time.monotonic()
    timestamps = _calls[email]
    cutoff = now - WINDOW_S
    while timestamps and timestamps[0] < cutoff:
        timestamps.popleft()

    if len(timestamps) >= LIMIT:
        return False

    timestamps.append(now)
    return True


def reset(email: str | None = None) -> None:
    """Clear one email's call history, or every email's if `email` is
    omitted. Nothing in production code calls this today -- it exists so
    tests can start each case with a clean window rather than one that
    might still carry a previous case's timestamps.
    """
    if email is None:
        _calls.clear()
    else:
        _calls.pop(email, None)
