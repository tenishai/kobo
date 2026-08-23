"""Mounts kobo's MCP surface onto the real app (MCP server arc, Task 9):
the `/mcp` streamable-HTTP sub-app and the `/oauth` + `/.well-known` AS
router built by Tasks 4-8, flag-gated on `config.mcp_enabled`, plus the
one piece of ASGI plumbing that makes a MOUNTED FastMCP app actually work:
its session manager's lifespan.

**Why `mount_mcp` alone isn't enough -- the lifespan gotcha, verified
against the installed SDK, not just the design doc's prose.**
`mcp.server.fastmcp.server.FastMCP.streamable_http_app` (venv/lib/
python3.12/site-packages/mcp/server/fastmcp/server.py, ~line 1044) builds
its returned `Starlette` app with `lifespan=lambda app: self.session_
manager.run()`. `StreamableHTTPSessionManager.run` (mcp/server/
streamable_http_manager.py, ~line 113) is an `@asynccontextmanager`
whose body opens an `anyio` task group, stashes it on `self._task_group`,
`yield`s, and tears the task group down on exit -- and `_handle_request`
(same file, ~line 179) raises `RuntimeError("Task group is not
initialized. Make sure to use run().")` the instant a real request
arrives and `self._task_group` is still `None`. That `run()` context
manager is exactly what the FastMCP app's OWN lifespan runs when Starlette
drives it as a top-level ASGI app (`uvicorn app` on the sub-app alone,
or any bare `TestClient(mcp.streamable_http_app())`).

Starlette does NOT propagate a `Mount`-ed sub-app's lifespan (confirmed
against `starlette/routing.py`'s `Mount` -- it has no `lifespan`
handling at all, only `matches`/`handle` for ordinary requests; the ASGI
`lifespan` scope type is handled solely by the OUTERMOST app's own
`Router.lifespan`, `starlette/routing.py` ~line 629). Mounting kobo's MCP
ASGI app onto the FastAPI app with a plain `app.mount("/mcp", asgi)` and
nothing else therefore leaves `asgi`'s own lifespan -- the one thing that
starts `session_manager.run()` -- NEVER ENTERED. The first real POST to
`/mcp/` then raises exactly the `RuntimeError` above, from inside a live
request, not at boot. Reproduced by hand while building this module
(manual mount, no lifespan chaining, real bearer token, real `initialize`
POST): the traceback bottoms out at `streamable_http_manager.py`'s
`_handle_request`, precisely the "not initialized" branch.

**The fix -- nest the sub-app's own lifespan_context inside the host
app's, the reference architecture's own pattern.** Every Starlette app
(the FastMCP-built ASGI app included) exposes the exact async context
manager its lifespan runs as `app.router.lifespan_context` (`Starlette.
__init__` hands its `lifespan=` kwarg straight to `Router(...,
lifespan=...)`, which stores it there un-wrapped when it's already a
plain callable returning an async context manager -- which `lambda app:
self.session_manager.run()` is, since `run` is itself `@asynccontext
manager`-decorated; see `starlette/routing.py` `Router.__init__`'s
`else: self.lifespan_context = lifespan` branch, ~line 599). So
`asgi.router.lifespan_context(asgi)` returns the SAME async context
manager Starlette would enter if `asgi` were ever run as a top-level ASGI
app. `mcp_lifespan` below is a plain `@asynccontextmanager` that FastAPI's
own `lifespan=` construction argument accepts (FastAPI just forwards it
to Starlette the same way), and it enters exactly that context manager,
which starts the real `session_manager.run()`'s task group for the
lifetime of the FastAPI process -- one task group, shared by every `/mcp`
request, same as a real (never-mounted) FastMCP deployment gets from its
own `uvicorn` run.

**Why a holder on `app.state`, not a closure over a captured value.**
`FastAPI(lifespan=mcp_lifespan)` must be constructed BEFORE the rest of
`create_app` runs (it's a constructor argument, fixed at the `FastAPI(...)`
call itself -- there is no `app.lifespan = ...` setter to add one after
the fact). But the actual MCP ASGI app doesn't exist yet at that point:
`mount_mcp(app)` builds it later, near the end of `create_app`, after
every `/api` route is defined and just before the SPA static mount. The
two events -- `FastAPI(lifespan=mcp_lifespan)` (constructor) and `mcp_
lifespan` actually RUNNING (ASGI startup, driven by whatever server or
test harness enters the app) -- are separated in time by the rest of
`create_app`'s body, and ASGI startup always happens strictly after
`create_app` has returned a fully-built `app` (`uvicorn`/`TestClient.
__enter__` send the `lifespan.startup` message only once the app object
they were handed exists). So by the time `mcp_lifespan` actually executes,
`app.state.mcp_asgi_app` -- set by `mount_mcp` earlier in the SAME
`create_app` call -- is already populated. `mcp_lifespan` reads it off
`app.state` at startup time rather than closing over a value captured at
`FastAPI(...)` construction time, because at construction time there is
nothing yet to capture.

**Why this is byte-identical for a flag-off room.** `mount_mcp` returns
`False` without ever setting `app.state.mcp_asgi_app` when `config.
mcp_enabled` is `False` (the default) -- the deferred-import branch below.
`mcp_lifespan` then finds nothing on `app.state` and immediately `yield`s
with no side effect at all: no task group, no sub-app, nothing entered.
A FastAPI app already runs an ASGI lifespan handshake even with NO
`lifespan=` argument (Starlette's own `_DefaultLifespan`, `starlette/
routing.py` ~line 583, itself a no-op) -- so a no-op custom lifespan
changes nothing observable about a flag-off room's boot: same startup
messages exchanged, same nothing done in response to them, same behavior
before this task as after it.

**The 308 gotcha (also load-bearing, orthogonal to the lifespan one).**
`app.mount("/mcp", asgi)` compiles to a regex requiring `/mcp/<anything>`
(`starlette.routing.Mount.__init__` does `compile_path(self.path +
"/{path:path}")`) -- it does NOT match the bare path `/mcp` with no
trailing slash and nothing after it at all, so a client that POSTs to
bare `/mcp` (every real MCP client does) gets a plain 404, never routed
into the mount. The explicit `@app.api_route("/mcp", methods=[...])`
below exists solely to answer that exact request with a 308 redirect to
`/mcp/` -- 308 (not 307, not a plain 301) because it is the one redirect
status that both preserves the HTTP method AND the request body across
the hop, which a POST with a JSON-RPC payload needs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi.responses import RedirectResponse


def mount_mcp(app: Any) -> bool:
    """Wires kobo's MCP surface onto `app` iff `app.state.config.mcp_
    enabled`. Returns whether it did.

    Flag off (the default -- `config.mcp.oauth_client_id` unset): returns
    `False` immediately, having imported nothing beyond this module's own
    top-level imports (plain FastAPI/Starlette pieces the rest of `kobo_
    api.main` already imports anyway). In particular, `kobo_api.mcp.
    server`, `kobo_api.mcp.oauth_as`, and the `mcp` SDK package itself are
    NEVER imported on this path -- the three `import` statements below
    live inside this `if` branch's body, not at module scope, specifically
    so a flag-off room's `sys.modules` never gains any of them (pinned by
    this task's own `sys.modules` diff test). A room that never configured
    MCP pays nothing for it: no extra import time, no extra memory, no
    extra attack surface from a dependency it never asked to run.

    Flag on: mounts the OAuth AS router (`kobo_api.mcp.oauth_as.router`,
    Task 4's `/oauth/*` + `/.well-known/*` routes) directly onto `app`
    (same origin, no separate app needed -- the design's ingress split
    happens at the load balancer, not here); builds the real MCP server
    (`kobo_api.mcp.server.build_mcp_server`, Tasks 6-8's tools + audit +
    token verifier); gets its streamable-HTTP ASGI app; stashes that ASGI
    app on `app.state.mcp_asgi_app` (the ONE thing `mcp_lifespan` below
    reads, at startup time -- see this module's own docstring for why a
    state holder rather than a closure); mounts it at `/mcp`; and adds the
    bare-`/mcp` 308 redirect route (also documented at length above).
    """
    config = app.state.config
    if not config.mcp_enabled:
        return False

    from kobo_api.mcp import oauth_as
    from kobo_api.mcp.server import build_mcp_server

    app.include_router(oauth_as.router)

    mcp = build_mcp_server(app)
    mcp_asgi_app = mcp.streamable_http_app()
    app.state.mcp_asgi_app = mcp_asgi_app
    app.mount("/mcp", mcp_asgi_app)

    @app.api_route("/mcp", methods=["GET", "POST", "DELETE"])
    def _redirect_bare_mcp_to_trailing_slash() -> RedirectResponse:
        return RedirectResponse(url="/mcp/", status_code=308)

    return True


@asynccontextmanager
async def mcp_lifespan(app: Any) -> AsyncIterator[None]:
    """The async context manager `kobo_api.main.create_app` passes as
    `FastAPI(lifespan=mcp_lifespan)`. Enters `app.state.mcp_asgi_app`'s own
    `router.lifespan_context` -- which is exactly the async context
    manager that starts (and, on shutdown, stops) the MCP session
    manager's task group -- for the lifetime of the host app, iff
    `mount_mcp` populated that state during `create_app`. A flag-off room
    (or, defensively, any app that never ran `mount_mcp` at all) finds
    nothing there and just `yield`s: no sub-app, no task group, no
    observable difference from having no lifespan at all. See this
    module's own docstring for the full correctness argument against the
    installed SDK, including the exact `RuntimeError` this exists to
    prevent and the reproduction that confirmed it.
    """
    mcp_asgi_app = getattr(app.state, "mcp_asgi_app", None)
    if mcp_asgi_app is None:
        yield
        return

    async with mcp_asgi_app.router.lifespan_context(mcp_asgi_app):
        yield
