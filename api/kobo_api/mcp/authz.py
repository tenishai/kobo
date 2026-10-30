"""The authz shim for kobo's MCP server (MCP server arc, Task 5): the
bridge that lets every MCP tool reuse the room's EXISTING access-control
gates -- `_visible_agents`, `_visible_agent_or_404`, `_owner_or_operator_
or_403`, `_is_admin`, all in `kobo_api.main` -- by turning a verified
email into a synthetic room `Request`. Zero new authz logic lives here;
this module only calls those four functions and translates their answers
into shapes an MCP tool can use.

Why a synthetic request at all: every one of those four gates was written
against a real HTTP `Request` (they read `request.app.state.config`,
`request.app.state.clients`, and `request.state.iap_email`, the exact
attribute `kobo_api.iap.IapMiddleware` stamps from a verified assertion's
`email` claim once IAP passes a request through). An MCP tool call never
goes through IAP at all -- the caller's identity was already verified once,
during the OAuth hop (`kobo_api.mcp.oauth_as`), and travels from then on
as the `sub` claim on a `kobo_api.mcp.jwt` access token. `room_request`
below is the one place that gap gets bridged: it builds the same shape of
object those gates already know how to read, stamped with that verified
email, so the gates themselves never have to know or care that no real
HTTP request produced it.
"""

from __future__ import annotations

from fastapi.responses import JSONResponse
from starlette.requests import Request


class NotFound(Exception):
    """Raised by the shim wherever the underlying room gate answered
    "not visible" (`visible_agent_or_not_found`) or "visible, but not
    yours to change" (`require_owner_or_operator`) -- an MCP tool catches
    this exactly once and translates it to a plain "not found" `ToolError`,
    never a distinct "forbidden" shape. That collapse is the whole security
    property of this module (see `require_owner_or_operator` below): a
    caller who can see an agent but doesn't own it must get the identical
    answer as a caller asking about an agent that doesn't exist at all, so
    neither existence nor ownership leaks through a status code an MCP
    client could branch on.
    """


def room_request(app, email: str) -> Request:
    """The ONLY place in the MCP surface that constructs a synthetic room
    `Request`. `app` must be the real FastAPI app (its `.state.config` and
    `.state.clients` are exactly what `_visible_agents`/`_owner_or_operator_
    or_403`/`_is_admin` read off `request.app.state`); `email` is the
    already-verified caller, stamped at `request.state.iap_email` -- the
    identical attribute name and shape (a plain string, no wrapping object)
    that `kobo_api.iap.IapMiddleware` stamps from a real IAP assertion's
    `email` claim, so every gate's `getattr(request.state, "iap_email",
    None)` read works unchanged whether the request came from IAP or from
    here.

    The rest of the scope is inert filler: none of the four gates this
    bridges to reads method/path/headers/query_string, but `Request` still
    needs a well-formed ASGI `http` scope to construct at all, and `router`
    is set to the real app's router so anything that resolves `request.
    app.router` (nothing in the four gates does today, but a future gate
    might) sees the real routing table rather than nothing.
    """
    req = Request(
        scope={
            "type": "http",
            "app": app,
            "headers": [],
            "method": "GET",
            "path": "/",
            "query_string": b"",
            "router": app.router,
        }
    )
    req.state.iap_email = email
    # The audit log (issue #316) reads this to say the action came through
    # an assistant's connection rather than the room's own pages.
    req.state.audit_source = "mcp"
    return req


def visible_agents(session, app, email):
    """Every agent `email` may see, per `kobo_api.main._visible_agents` --
    same ownership/grant/group/commons union that route already computes,
    reached through `room_request` instead of a real HTTP request. The
    `groups_degraded` half of that function's return is dropped here: no
    MCP tool in this arc surfaces it, and adding it back is a pure
    additive change to this signature whenever one does.
    """
    from kobo_api.main import _visible_agents

    agents, _groups_degraded = _visible_agents(session, room_request(app, email))
    return agents


def visible_agent_or_not_found(session, app, email, name):
    """The agent named `name`, if `email` may see it -- otherwise `NotFound`,
    never the underlying `JSONResponse`. `kobo_api.main._visible_agent_or_
    404` already collapses "doesn't exist" and "exists but invisible to
    this caller" into one answer (a JSONResponse, whatever its status);
    this function collapses that JSONResponse into `NotFound` so a tool
    never has to know or branch on which case actually happened.
    """
    from kobo_api.main import _visible_agent_or_404

    result = _visible_agent_or_404(session, room_request(app, email), name)
    if isinstance(result, JSONResponse):
        raise NotFound(f"no such agent: {name!r}")
    return result


def require_owner_or_operator(agent, app, email) -> None:
    """Raises `NotFound` unless `email` may mutate `agent` (owner or
    operator, per `kobo_api.main._owner_or_operator_or_403`); returns
    `None` when allowed.

    The 403-to-404 normalization is the security property this whole
    module exists for: `_owner_or_operator_or_403` answers a 403-shaped
    `JSONResponse` for a caller who can SEE `agent` (they already passed
    `visible_agent_or_not_found` above) but doesn't own it and isn't an
    operator. Handing that 403 straight to an MCP client would itself leak
    information a stranger to this agent must not get for free -- "this
    exists and isn't yours" is strictly more than "not found" tells them.
    So every denial from the underlying gate, whatever its status code,
    surfaces here as the identical `NotFound` a genuinely missing agent
    raises from `visible_agent_or_not_found` -- one exception type, one
    shape, no way for a caller to tell the two apart.
    """
    from kobo_api.main import _owner_or_operator_or_403

    result = _owner_or_operator_or_403(agent, room_request(app, email))
    if isinstance(result, JSONResponse):
        raise NotFound(f"no such agent: {agent.name!r}")


def is_admin(app, email) -> bool:
    """Whether `email` holds room-wide operator status, per
    `kobo_api.main._is_admin` -- plain email operator or admin-group
    member, the identical answer that function gives a real request."""
    from kobo_api.main import _is_admin

    return _is_admin(room_request(app, email))
