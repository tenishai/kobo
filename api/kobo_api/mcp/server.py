"""kobo's MCP server object (MCP server arc, Task 7): the `FastMCP`
instance every kobo tool call reaches through, its `TokenVerifier` (the
seam that turns a bearer access token into the caller's verified email),
and the ONE place a tool reads that email back off its `ctx` argument.

**How identity actually reaches a tool, confirmed against the installed
mcp 1.29.0 SDK (venv/lib/python3.12/site-packages/mcp):**

1. `BearerAuthBackend.authenticate` (`mcp.server.auth.middleware.
   bearer_auth`) lifts the bearer token off the `Authorization` header on
   every request into the streamable-HTTP ASGI app, and awaits
   `token_verifier.verify_token(token)` -- the ONE call site that ever
   invokes `_KoboTokenVerifier` below. `None` back means "unauthenticated"
   to the SDK (a plain 401), never an exception -- which is why
   `verify_token` below returns `None` on `TokenError` rather than letting
   it propagate.
2. A non-`None` `AccessToken` is wrapped in `AuthenticatedUser(SimpleUser)`
   (`mcp.server.auth.middleware.auth_context`), which stamps
   `self.access_token = auth_info` and is what Starlette's own
   `AuthenticationMiddleware` stores as `request.user` for the rest of
   that request.
3. The streamable-HTTP transport (`mcp.server.streamable_http`) carries
   that same Starlette `Request` all the way into the low-level server's
   `RequestContext.request` (`ServerMessageMetadata(request_context=
   request)`, `mcp.server.lowlevel.server._handle_request`).
4. `FastMCP.get_context()` wraps that `RequestContext` in the `Context`
   object a tool's `ctx` parameter receives (`mcp.server.fastmcp.server.
   Context`).

So the reference pattern's accessor shape holds exactly as documented:
`ctx.request_context.request.user.access_token.user_email` -- CONFIRMED
against 1.29's actual classes, not just plausible-looking names: `request`
is the real Starlette `Request` (its `.user` property is Starlette's own,
reading `scope["user"]`), `.user` is `AuthenticatedUser`, `.access_token`
is whatever `_KoboAccessToken` instance `verify_token` returned, and
`.user_email` is the one field this module adds beyond the SDK's own
`AccessToken` (which carries no notion of email at all -- `token`,
`client_id`, `scopes`, `expires_at`, `resource`, `subject`, `claims`).
`get_user_email` below is the one place any tool reads that chain.

**Context injection survives the `@mcp.tool()` / `@audited(...)` decorator
split across modules, verified experimentally against 1.29 (see this
task's report for the reproduction):** `Tool.from_function` locates the
`ctx` parameter via `find_context_parameter`, which calls `typing.
get_type_hints` on whatever callable was handed to `@mcp.tool()` -- here,
the `functools.wraps`-wrapped closure `audited()` returns. `get_type_hints`
resolves a wrapped callable's annotations against the ORIGINAL function's
`__globals__` (it walks `__wrapped__` before reading `__globals__` --
CPython's `typing.get_type_hints`, not an SDK behavior), and `functools.
wraps` copies `__annotations__` (a `Context` type-hint string, this
module's `from __future__ import annotations`) onto the wrapper alongside
`__wrapped__`. The net effect: even though `audited`'s wrapper is defined
in `kobo_api.mcp.audit` (which never imports `Context`), the SDK still
resolves the hint using `kobo_api.mcp.tools.read`'s own globals (which
does), and finds `ctx` correctly. This is exactly why the design's
decorator order -- `@mcp.tool()` outermost, `@audited(...)` immediately
under it -- is load-bearing, not stylistic.
"""

from __future__ import annotations

import time
from datetime import UTC
from typing import Any
from urllib.parse import urlparse

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import TransportSecuritySettings
from pydantic import AnyHttpUrl

from kobo_api.config import Config
from kobo_api.mcp import audit
from kobo_api.mcp.jwt import TokenError, verify
from kobo_api.mcp.refresh_tokens import email_revoked_at


class _KoboAccessToken(AccessToken):
    """The SDK's own `AccessToken` (a plain pydantic `BaseModel`) carries
    no notion of who a caller is -- `token`/`client_id`/`scopes`/
    `expires_at`/`resource`/`subject`/`claims`, nothing named `email`.
    This subclass adds the ONE fact every kobo tool actually needs:
    `user_email`, lifted off the access token's own `sub` claim by
    `_KoboTokenVerifier.verify_token` below. Pydantic subclassing a
    `BaseModel` with an extra required field is ordinary and supported;
    the SDK itself documents this exact move ("OK to add fields to
    subclasses which should not be exposed externally", `mcp.server.auth.
    provider`).
    """

    user_email: str


class _KoboTokenVerifier(TokenVerifier):
    """Bridges kobo's own `kobo_api.mcp.jwt` access tokens to the SDK's
    `TokenVerifier` protocol (`mcp.server.auth.provider.TokenVerifier`):
    `verify_token` is the ONE seam `BearerAuthBackend` calls, once per
    request, on the bearer token lifted straight off the `Authorization`
    header. No new verification logic lives here -- this is a pure
    adapter onto `kobo_api.mcp.jwt.verify`, the same access-token
    verification `kobo_api.mcp.oauth_as` already trusts.

    `app` is the real FastAPI app, carried (not just its config captured
    once at construction) so the audience this checks against always
    tracks `app.state.config` live, and so a later task's tools reach
    `app.state.clients`/`app.state.session_factory` off the identical
    `app` reference every other MCP module already threads through
    (`kobo_api.mcp.authz.room_request`, `kobo_api.mcp.audit.configure`).
    """

    def __init__(self, app: Any):
        self._app = app

    async def verify_token(self, token: str) -> AccessToken | None:
        cfg: Config = self._app.state.config
        try:
            claims = verify(token, "access_token", aud=cfg.mcp.oauth_client_id)
        except TokenError:
            return None

        email = claims.get("sub")
        if not email:
            return None
        if self._revoked_since(email, claims.get("iat")):
            return None

        return _KoboAccessToken(
            token=token,
            client_id=cfg.mcp.oauth_client_id,
            scopes=[],
            expires_at=claims.get("exp"),
            subject=email,
            user_email=email,
        )

    def _revoked_since(self, email: str, issued_at: Any) -> bool:
        """True when `email` was cut off (issue #197) after this token was
        issued. Access tokens are stateless, so this is the one read that
        lets a revocation land before the token's own expiry. Behind a short
        per-process cache: one row read per email per `_REVOCATION_CACHE_S`,
        not per tool call. A room without a session factory (a test mount)
        has nothing to read and refuses nothing."""
        factory = getattr(self._app.state, "session_factory", None)
        if factory is None:
            return False
        now = time.monotonic()
        cached = _revocation_cache.get(email)
        if cached is None or now - cached[0] > _REVOCATION_CACHE_S:
            with factory() as session:
                revoked_at = email_revoked_at(session, email=email)
            _revocation_cache[email] = (now, revoked_at)
        else:
            revoked_at = cached[1]
        if revoked_at is None:
            return False
        issued = float(issued_at or 0)
        return issued <= revoked_at.replace(tzinfo=UTC).timestamp()


# email -> (checked at, revoked_at or None). Per process, like every other
# in-memory cache in the room; the bound is `_REVOCATION_CACHE_S`.
_revocation_cache: dict[str, tuple[float, Any]] = {}
_REVOCATION_CACHE_S = 30.0


def forget_revocation_cache() -> None:
    """Drop the cache so the next verification reads the table. The revoke
    route calls this in its own process; other replicas catch up within
    `_REVOCATION_CACHE_S`."""
    _revocation_cache.clear()


def get_user_email(ctx: Any) -> str:
    """THE one place any MCP tool reads its caller's verified email:
    `ctx.request_context.request.user.access_token.user_email` -- see this
    module's own docstring for exactly why that chain is what 1.29 hands a
    tool. A defensive `getattr` chain, not a straight-line attribute
    access, on purpose: a malformed or stubbed `ctx` (a test double, SDK
    version drift) must never raise here and crash a tool call before the
    tool itself even runs -- it resolves to `""`, the same "caller
    unknown" value a genuinely unauthenticated request would produce.

    `kobo_api.mcp.audit._email_from_ctx` holds its OWN private copy of
    this exact chain, deliberately -- Task 6 shipped before this module
    existed, and the two are kept independently defensive rather than one
    importing the other, so a change to either one can never silently
    regress the other's fail-open-to-empty-string safety net.
    """
    request_context = getattr(ctx, "request_context", None)
    request = getattr(request_context, "request", None)
    user = getattr(request, "user", None)
    access_token = getattr(user, "access_token", None)
    email = getattr(access_token, "user_email", None)
    return email or ""


def _transport_security_for(issuer_base_url: str) -> TransportSecuritySettings | None:
    """The rollout-blocking gap a live 421 probe caught (Task 9's review):
    `FastMCP(...)` built with no `host=`/`transport_security=` at all
    defaults to `host="127.0.0.1"`, which auto-enables the SDK's own
    `TransportSecuritySettings(allowed_hosts=["127.0.0.1:*",
    "localhost:*", "[::1]:*"])` (`mcp.server.fastmcp.server.Settings`'s
    own defaults). The design's ingress serves `/mcp` on the room's REAL
    hostname (same host as the UI, per the spec's ingress section) -- a
    live request arrives with `Host: <room-host>`, and `mcp.server.
    transport_security.TransportSecurityMiddleware._validate_host`
    rejects it with a 421 "Invalid Host header" before the request ever
    reaches a tool. This builds the `TransportSecuritySettings` that
    replaces the SDK's loopback-only default with the room's own
    hostname, parsed straight off `issuer_base_url` -- the one config
    field that already carries this room's externally-reachable identity
    (`MCPConfig.issuer_base_url`'s own docstring).

    **Both the bare hostname AND the `host:*` wildcard are required --
    proven against the installed SDK, not asserted on faith.**
    `_validate_host`'s wildcard branch only matches when the incoming
    `Host` header already contains a colon and port
    (`host.startswith(base_host + ":")`); a standard HTTPS request on
    443 (or HTTP on 80) arrives with a BARE `Host` header, no port at
    all, which only an exact `allowed_hosts` entry matches. An
    `allowed_hosts` of just `["kobo.example.org:*"]` would STILL 421 a
    bare `Host: kobo.example.org` -- reproduced by hand while fixing
    this, and pinned by `test_bare_host_header_https_request_is_not_421`
    / `test_allowed_hosts_carries_both_the_bare_hostname_and_the_
    wildcard_port_form` in `tests/test_mcp_mount.py`. Both entries are
    listed for `allowed_origins` too, for the same reason applied to the
    `Origin` header `_validate_origin` checks.

    Empty `issuer_base_url` (MCP enabled with no issuer -- forbidden by
    `Config._mcp_requires_an_issuer_base_url_when_enabled` the moment
    `oauth_client_id` is set, so this should never actually happen for a
    server that gets this far) returns `None`, which leaves `FastMCP`'s
    own default (loopback-only `TransportSecuritySettings`) in place:
    a server with no known public host has no safe non-loopback host to
    add to `allowed_hosts`, and loopback-only is the safe default for
    local dev, not an open one.
    """
    if not issuer_base_url:
        return None

    parsed = urlparse(issuer_base_url)
    room_host = parsed.hostname
    if not room_host:
        return None
    if parsed.port:
        room_host = f"{room_host}:{parsed.port}"
    scheme = parsed.scheme or "https"

    return TransportSecuritySettings(
        allowed_hosts=[room_host, f"{room_host}:*"],
        allowed_origins=[f"{scheme}://{room_host}", f"{scheme}://{room_host}:*"],
    )


def build_mcp_server(app: Any) -> FastMCP:
    """Constructs kobo's MCP server: stateless streamable-HTTP FastMCP
    (`stateless_http=True`, `streamable_http_path="/"` -- the mounting
    app, a later task, puts this whole sub-app at `/mcp`), `_KoboToken
    Verifier` wired to `app`, `AuthSettings` pointed at this room's own
    issuer/resource URLs, `transport_security` widened past the SDK's own
    loopback-only default to this room's real hostname (see `_transport_
    security_for` above -- a rollout-blocking gap Task 9's review caught
    via a live 421 probe), `audit.configure`d with `app`'s own session
    factory so every tool call's audit row lands in the room's real DB,
    then the read tools and write tools registered by their own modules
    (`kobo_api.mcp.tools.read`/`kobo_api.mcp.tools.write`) -- see each
    module's own docstring for its current tool count and roster, never
    duplicated here where a later wave would have to remember to update
    both places in lockstep.

    **Import deliberately deferred, not just habit:** `kobo_api.mcp.tools.
    read` itself imports `get_user_email` straight off this module
    (`from kobo_api.mcp.server import get_user_email`) -- a plain
    module-level import there is only safe once THIS module has finished
    defining it, which a top-level `import ... read` up above would not
    yet have done (Python would still be partway through executing this
    file's own import block). Importing here, inside the function body,
    guarantees `kobo_api.mcp.server` is already fully loaded in
    `sys.modules` by the time `read.py`'s own import runs.
    """
    from kobo_api.mcp.tools import read as read_tools
    from kobo_api.mcp.tools import write as write_tools

    cfg: Config = app.state.config

    mcp = FastMCP(
        name="kobo",
        stateless_http=True,
        streamable_http_path="/",
        token_verifier=_KoboTokenVerifier(app),
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(cfg.mcp.issuer_base_url),
            resource_server_url=AnyHttpUrl(
                f"{cfg.mcp.issuer_base_url.rstrip('/')}/mcp"
            ),
        ),
        transport_security=_transport_security_for(cfg.mcp.issuer_base_url),
    )

    audit.configure(app.state.session_factory)

    read_tools.register(mcp, app)
    write_tools.register(mcp, app)

    return mcp
