"""Tests for kobo_api.mcp.mount (MCP server arc, Task 9): the flag gate +
deferred imports, the bare-`/mcp` 308 redirect, and the lifespan chain
that makes a MOUNTED FastMCP session manager's task group actually start.
See kobo_api.mcp.mount's own module docstring for the full mechanism
these pin -- this file exists to prove it holds against the real,
installed SDK, not just to restate the docstring's argument.

Harness for the "enabled" tests: `kobo_api.main.create_app` with an
mcp-enabled `Config` (same fields test_mcp_tools_read.py/test_mcp_tools_
write.py's own fixtures use) -- `create_app` now calls `mount_mcp`
internally (this task's own wiring in `main.py`), so building the app
IS exercising the mount, unlike those two files which call `build_mcp_
server` a second time themselves for direct tool-call testing.
"""

from __future__ import annotations

import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.clients import Clients
from kobo_api.config import Config, MCPConfig
from kobo_api.main import create_app
from kobo_api.mcp import audit, rate_limit
from kobo_api.mcp.jwt import ACCESS_TTL_S, mint, signing_key
from kobo_api.mcp.mount import mount_mcp
from kobo_api.models import Base
from tests.fakes import FakeAgent, FakeCloud, FakeGitHub, FakeGojira, FakeKubectl

CLIENT_ID = "test-mcp-client"
WORKSPACE_DOMAIN = "example.com"
ISSUER_BASE_URL = "https://kobo.example.org"
EMAIL = "person@example.com"

# The exact three names the design's "Feature-flagged with deferred
# imports" ruling (docs/superpowers/specs/2026-08-22-mcp-server-design.md)
# says a flag-off room must never gain in sys.modules.
_WATCHED_MODULES = ("mcp", "kobo_api.mcp.server", "kobo_api.mcp.oauth_as")


@pytest.fixture(autouse=True)
def _reset_rate_limit_and_audit():
    """Same discipline test_mcp_tools_read.py/test_mcp_tools_write.py hold
    -- `mount_mcp` (enabled) calls `build_mcp_server`, which calls `audit.
    configure` for real, a module-level global that would otherwise leak
    across test files sharing this process."""
    rate_limit.reset()
    audit.configure(None)
    yield
    rate_limit.reset()
    audit.configure(None)


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _enabled_config(tmp_path):
    return Config(
        workdir=tmp_path / "work",
        mcp=MCPConfig(
            allow_without_iap=True, oauth_client_id=CLIENT_ID,
            workspace_domain=WORKSPACE_DOMAIN,
            issuer_base_url=ISSUER_BASE_URL,
        ),
    )


def _enabled_app(session_factory, tmp_path):
    clients = Clients(
        github=FakeGitHub(),
        gojira=FakeGojira(),
        cloud=FakeCloud(),
        agent=FakeAgent(),
        kubectl=FakeKubectl(),
    )
    return create_app(session_factory, config=_enabled_config(tmp_path), clients=clients)


# --- disabled: false, and imports nothing from the mcp surface --------------


def test_mount_mcp_disabled_returns_false_and_imports_nothing_from_the_mcp_surface():
    """`config.mcp_enabled` False (a plain default `Config()`) -- `mount_
    mcp` must return `False` having imported NONE of `mcp` (the SDK),
    `kobo_api.mcp.server`, or `kobo_api.mcp.oauth_as`. Some other test
    module collected earlier in this same pytest session (test_mcp_tools_
    read.py, test_mcp_oauth.py, ...) may already have imported all three
    -- popped here first so the before/after diff measures what THIS call
    does, not what collection already did; restored in `finally` so any
    later test in the session that expects them present is unaffected.
    """
    saved = {name: sys.modules.pop(name) for name in _WATCHED_MODULES if name in sys.modules}
    try:
        app = FastAPI()
        app.state.config = Config()  # mcp_enabled is False by default

        before = set(sys.modules)
        result = mount_mcp(app)
        after = set(sys.modules)
    finally:
        sys.modules.update(saved)

    assert result is False
    assert not (after - before) & set(_WATCHED_MODULES)


# --- enabled: mounted, 308, well-known, lifespan chain -----------------------


def test_mount_mcp_enabled_returns_true(session_factory, tmp_path):
    # create_app already ran mount_mcp once (Task 9's own wiring); calling
    # it again here directly exercises the return value itself, same as
    # every enabled behavior below is exercised through create_app's own
    # internal call, not a second one.
    app = _enabled_app(session_factory, tmp_path)
    assert mount_mcp(app) is True


def test_app_with_mcp_enabled_boots_cleanly_under_the_testclient_lifespan(
    session_factory, tmp_path
):
    """Entering `TestClient` as a context manager drives the real ASGI
    `lifespan.startup`/`lifespan.shutdown` handshake -- the one place a
    broken `FastAPI(lifespan=...)` wiring would surface, since the
    lifespan argument is fixed at the `FastAPI(...)` constructor call and
    merely building the app (without entering it) never runs it."""
    app = _enabled_app(session_factory, tmp_path)

    with TestClient(app):
        pass


def test_bare_mcp_post_get_delete_all_redirect_308_to_trailing_slash(
    session_factory, tmp_path
):
    """The 308 gotcha: `app.mount("/mcp", asgi)` compiles to a route that
    only matches `/mcp/...` (see kobo_api.mcp.mount's own docstring), so a
    real client's bare `/mcp` needs the explicit redirect route this
    pins. 308, not 301/302/307, because it is the one status that
    preserves both method and body across the hop -- checked here across
    all three methods `mount_mcp` registers the route for."""
    app = _enabled_app(session_factory, tmp_path)

    with TestClient(app, follow_redirects=False) as client:
        for method in ("get", "post", "delete"):
            response = getattr(client, method)("/mcp")
            assert response.status_code == 308
            assert response.headers["location"].endswith("/mcp/")


def test_well_known_oauth_authorization_server_is_reachable(session_factory, tmp_path):
    """Proves `mount_mcp` actually wired `oauth_as.router` onto the real
    app via `create_app`, not just that the router object exists."""
    app = _enabled_app(session_factory, tmp_path)

    with TestClient(app) as client:
        response = client.get("/.well-known/oauth-authorization-server")

    assert response.status_code == 200


def test_oauth_authorize_route_exists(session_factory, tmp_path):
    app = _enabled_app(session_factory, tmp_path)

    with TestClient(app, follow_redirects=False) as client:
        # No query params -- this 400s on missing response_type rather
        # than 404ing, which is exactly the proof the route is mounted at
        # all (a genuinely missing route would 404, not 400).
        response = client.get("/oauth/authorize")

    assert response.status_code == 400


def test_real_initialize_request_over_the_rooms_real_hostname_is_not_421(
    session_factory, tmp_path
):
    """Rollout-blocker regression (Task 9 review, reproduced live): the
    design's ingress serves `/mcp` on the room's REAL hostname (same host
    as the UI) -- never on `localhost`/`127.0.0.1`, which is all
    `test_real_initialize_request_does_not_hit_the_uninitialized_session_
    manager` above ever drives through. `build_mcp_server` used to
    construct `FastMCP(...)` with no `host=`/`transport_security=` at
    all, so the SDK defaulted to `host="127.0.0.1"`, which auto-enables
    `TransportSecuritySettings(allowed_hosts=["127.0.0.1:*",
    "localhost:*", "[::1]:*"])` -- a request whose `Host` header is the
    room's real hostname then fails `TransportSecurityMiddleware.
    _validate_host` and the SDK answers 421 "Invalid Host header" before
    the request ever reaches the session manager or a tool.

    `base_url="https://kobo.example.org"` with NO explicit port -- the
    exact shape a real HTTPS-on-443 client uses, and the exact shape the
    reviewer's live probe used -- reproduces that 421 pre-fix. This test
    is the reproduction turned around to assert the FIXED behavior: 200,
    never 421.
    """
    app = _enabled_app(session_factory, tmp_path)
    cfg = app.state.config
    token = mint(
        "access_token",
        EMAIL,
        CLIENT_ID,
        ACCESS_TTL_S,
        key=signing_key(cfg.mcp.jwt_key_env),
        scope="mcp",
    )

    with TestClient(app, base_url=ISSUER_BASE_URL) as client:
        response = client.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test-real-host-smoke", "version": "0.0.1"},
                },
            },
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )

    assert response.status_code != 421
    assert response.status_code == 200
    assert '"protocolVersion"' in response.text


def test_allowed_hosts_carries_both_the_bare_hostname_and_the_wildcard_port_form(
    session_factory, tmp_path
):
    """Pins the nuance the review called out as the one most likely to
    regress silently: `TransportSecurityMiddleware._validate_host`'s
    wildcard branch (`mcp.server.transport_security`) only matches when
    the incoming `Host` header already contains a colon and port
    (`host.startswith(base_host + ":")`) -- a standard HTTPS request on
    443 arrives with a BARE `Host` header, no port at all, which only an
    exact bare-hostname entry in `allowed_hosts` matches. An
    `allowed_hosts` of just `["kobo.example.org:*"]` would STILL 421 a
    bare `Host: kobo.example.org` -- both the bare entry and the `:*`
    wildcard entry are required, not just the wildcard one. Checked
    directly against the constructed `FastMCP`'s own settings (not only
    indirectly through one HTTP probe's Host-header shape) so a future
    "simplification" down to a single-entry list fails this test even if
    it happened to still pass some other request shape.
    """
    from kobo_api.mcp.server import build_mcp_server

    app = _enabled_app(session_factory, tmp_path)
    mcp = build_mcp_server(app)

    allowed_hosts = mcp.settings.transport_security.allowed_hosts
    assert "kobo.example.org" in allowed_hosts
    assert "kobo.example.org:*" in allowed_hosts

    allowed_origins = mcp.settings.transport_security.allowed_origins
    assert "https://kobo.example.org" in allowed_origins
    assert "https://kobo.example.org:*" in allowed_origins


def test_bare_host_header_https_request_is_not_421(session_factory, tmp_path):
    """The live-request half of the bare-vs-wildcard proof above: a
    request whose `Host` header genuinely has no port (confirmed against
    `TestClient`'s own request construction -- `base_url` with no
    explicit port, or an explicit default port for the scheme, both
    produce a bare `Host` header with no `:<port>` suffix at all) must
    still be accepted. This is the exact request shape that an
    `allowed_hosts` containing only the `"kobo.example.org:*"` wildcard
    entry would reject.
    """
    app = _enabled_app(session_factory, tmp_path)
    cfg = app.state.config
    token = mint(
        "access_token",
        EMAIL,
        CLIENT_ID,
        ACCESS_TTL_S,
        key=signing_key(cfg.mcp.jwt_key_env),
        scope="mcp",
    )

    with TestClient(app, base_url="https://kobo.example.org:443") as client:
        response = client.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test-bare-host-smoke", "version": "0.0.1"},
                },
            },
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )

    assert response.status_code == 200
    assert '"protocolVersion"' in response.text


def test_mcp_and_oauth_routes_beat_the_spa_catch_all_in_a_fully_wired_room(
    session_factory, tmp_path
):
    """Review's minor coverage gap: every other test in this module builds
    an mcp-enabled app with `ui_dist` unset, so nothing here previously
    proved that a room wired with BOTH the built UI (Task 1's SPA
    catch-all, `kobo_api.static_ui.mount_ui`) AND MCP (this task's
    `mount_mcp`) still routes `/mcp` and `/.well-known/*` to their real
    handlers rather than to the SPA's `index.html` fallback.
    `kobo_api.main.create_app` mounts MCP before the UI (see its own
    comment there), so registration order alone should make this true --
    this test exists to prove that ordering holds against the real app,
    not just to restate it.
    """
    dist_dir = tmp_path / "dist"
    (dist_dir / "assets").mkdir(parents=True)
    (dist_dir / "index.html").write_text("<html><body>SPA-INDEX-MARKER</body></html>")
    (dist_dir / "assets" / "app.js").write_text("console.log('app');")

    config = _enabled_config(tmp_path).model_copy(update={"ui_dist": dist_dir})
    clients = Clients(
        github=FakeGitHub(),
        gojira=FakeGojira(),
        cloud=FakeCloud(),
        agent=FakeAgent(),
        kubectl=FakeKubectl(),
    )
    app = create_app(session_factory, config=config, clients=clients)

    with TestClient(app, base_url=ISSUER_BASE_URL, follow_redirects=False) as client:
        well_known = client.get("/.well-known/oauth-authorization-server")
        mcp_bare = client.get("/mcp")
        spa_route = client.get("/agents/whatever-arbitrary-path")

    assert well_known.status_code == 200
    assert mcp_bare.status_code == 308
    assert spa_route.status_code == 200
    assert "SPA-INDEX-MARKER" in spa_route.text


def test_real_initialize_request_does_not_hit_the_uninitialized_session_manager(
    session_factory, tmp_path
):
    """The load-bearing proof, against the real SDK, not just a boot-
    without-raising check: a genuine bearer-authenticated `initialize`
    POST to `/mcp/` must not 500 with the SDK's own `RuntimeError("Task
    group is not initialized. Make sure to use run().")` -- exactly the
    failure a mounted FastMCP app produces when its own lifespan (the one
    that starts `StreamableHTTPSessionManager.run()`'s task group) never
    ran. Reproduced by hand while building kobo_api.mcp.mount (a manual
    `app.mount(...)` with no lifespan chaining raises precisely this, from
    inside the request, not at boot) -- this test is that reproduction
    turned around to assert the FIXED behavior instead.

    An unauthenticated request can't stand in for this proof: the SDK's
    own `RequireAuthMiddleware` answers 401 BEFORE ever reaching the
    session manager, so it would pass even with a completely broken
    lifespan chain. Hence minting a real access token here rather than
    reusing the simpler well-known/oauth checks above.

    `base_url` is the room's own configured `ISSUER_BASE_URL`
    (`https://kobo.example.org`), not `localhost`, per the rollout-blocker
    fix in `_transport_security_for` (`kobo_api.mcp.server`): `build_mcp_
    server` now points `FastMCP`'s `transport_security` at this room's
    real hostname (parsed off `config.mcp.issuer_base_url`) instead of
    leaving the SDK's own loopback-only default (`host="127.0.0.1"` ->
    `allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"]`) in place --
    a `localhost` Host header no longer matches at all post-fix, exactly
    as it shouldn't for a room whose real ingress never serves `/mcp` on
    loopback. See `test_real_initialize_request_over_the_rooms_real_
    hostname_is_not_421` below for the fix's own dedicated regression
    test, reproduced RED against the pre-fix 421 and GREEN after.
    """
    app = _enabled_app(session_factory, tmp_path)
    cfg = app.state.config
    token = mint(
        "access_token",
        EMAIL,
        CLIENT_ID,
        ACCESS_TTL_S,
        key=signing_key(cfg.mcp.jwt_key_env),
        scope="mcp",
    )

    with TestClient(app, base_url=ISSUER_BASE_URL) as client:
        response = client.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test-mount-smoke", "version": "0.0.1"},
                },
            },
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )

    assert response.status_code == 200
    assert "Task group is not initialized" not in response.text
    assert '"protocolVersion"' in response.text
