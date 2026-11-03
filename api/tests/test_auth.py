"""Authentication: who the room believes a caller is.

IAP assertion verification and the middleware's coverage (everything but
the exempt paths), the https-only guard on the OAuth surface, the room's own
OAuth 2.1 authorization server (PKCE, hosted-domain check, refresh-token
families and replay), the MCP JWTs, and the two agent-facing brokers' token
checks. Folded on 2026-10-29 from test_iap.py, test_mcp_oauth.py,
test_mcp_jwt.py, test_mcp_refresh_tokens.py, test_mcp_tools_read.py,
test_pr_broker.py, test_jira_broker.py, test_config.py and
test_api_admin_mcp.py.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import time
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.admin_store import audit_events
from kobo_api.clients import Clients
from kobo_api.config import Config, GroupChoice, MCPConfig
from kobo_api.iap import (
    ASSERTION_HEADER,
    EXEMPT_PREFIXES,
    FORWARDED_PROTO_HEADER,
    HTTPS_REQUIRED_BODY,
    IAP_ISSUER,
    UNAUTHORIZED_BODY,
    HttpsOnlyMiddleware,
    IapError,
    IapMiddleware,
    IapVerifier,
    _is_exempt,
)
from kobo_api.main import create_app
from kobo_api.mcp import audit, oauth_as, rate_limit
from kobo_api.mcp import jwt as mcp_jwt
from kobo_api.mcp import refresh_tokens as rt
from kobo_api.mcp import server as mcp_server
from kobo_api.mcp.jwt import ACCESS_TTL_S, mint, verify
from kobo_api.mcp.refresh_tokens import consume, create_family
from kobo_api.mcp.server import _KoboTokenVerifier
from kobo_api.models import Base, MCPRefreshToken
from kobo_api.pr_broker import BrokerAuthError
from tests.fakes import FakeAgent, FakeCloud, FakeGitHub, FakeGojira, FakeKubectl

# --- from test_iap.py ---


AUDIENCE = "test-audience"


KID = "test-key-1"


def _generate_jwk(kid: str = KID):
    """A fresh ES256 keypair plus its public half exported as a real JWK
    (kid included) — the shape IAP's own JWKS endpoint returns."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    jwk = jwt.algorithms.ECAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk["kid"] = kid
    jwk["use"] = "sig"
    jwk["alg"] = "ES256"
    return private_key, jwk


def _sign(
    private_key,
    kid: str = KID,
    *,
    aud: str = AUDIENCE,
    iss: str = IAP_ISSUER,
    iat: int | None = None,
    exp: int | None = None,
    email: str | None = "person@example.com",
    extra_claims: dict | None = None,
) -> str:
    now = int(time.time())
    claims = {
        "aud": aud,
        "iss": iss,
        "iat": iat if iat is not None else now,
        "exp": exp if exp is not None else now + 3600,
        "sub": "accounts.google.com:1234567890",
    }
    if email is not None:
        claims["email"] = email
    if extra_claims:
        claims.update(extra_claims)
    return jwt.encode(claims, private_key, algorithm="ES256", headers={"kid": kid})


def _counting_fetcher(*responses):
    """A jwks_fetcher stub that returns `responses[0]` on the first call,
    `responses[1]` on the second, ... and repeats the last response for any
    further call. `.calls` tracks the call count for the kid-rotation test."""

    def fetcher():
        fetcher.calls += 1
        idx = min(fetcher.calls - 1, len(responses) - 1)
        return responses[idx]

    fetcher.calls = 0
    return fetcher


def _forged_claims(email: str = "person@example.com") -> dict:
    """The same claim shape `_sign` produces, built by hand so it can be fed
    to `jwt.encode` under non-ES256 algs (which `_sign` doesn't support)."""
    now = int(time.time())
    return {
        "aud": AUDIENCE,
        "iss": IAP_ISSUER,
        "iat": now,
        "exp": now + 3600,
        "sub": "accounts.google.com:1234567890",
        "email": email,
    }


@pytest.fixture
def iap_config(config):
    return config.model_copy(update={"iap_audience": AUDIENCE})


def _client_with_fetcher(session_factory, iap_config, fetcher):
    app = create_app(session_factory, config=iap_config, iap_jwks_fetcher=fetcher)

    @app.get("/probe")
    def probe(request: Request) -> dict:
        return {"email": getattr(request.state, "iap_email", None)}

    return TestClient(app)


def _make_minimal_dist(tmp_path):
    """A minimal Vite-shaped `dist/` dir, same shape as test_static_ui.py's
    own `make_dist` helper."""
    dist_dir = tmp_path / "dist"
    assets_dir = dist_dir / "assets"
    assets_dir.mkdir(parents=True)
    (dist_dir / "index.html").write_text("<html>SPA-MARKER</html>")
    (assets_dir / "app.js").write_text("console.log('app');")
    return dist_dir


HTTP_PROTO = {FORWARDED_PROTO_HEADER: "http"}


HTTPS_PROTO = {FORWARDED_PROTO_HEADER: "https"}


def test_verify_valid_token_returns_claims():
    """Gate (authentication): Verify valid token returns claims."""
    private_key, jwk = _generate_jwk()
    verifier = IapVerifier(AUDIENCE, jwks_fetcher=_counting_fetcher({"keys": [jwk]}))
    token = _sign(private_key)

    claims = verifier.verify(token)

    assert claims["aud"] == AUDIENCE
    assert claims["iss"] == IAP_ISSUER
    assert claims["email"] == "person@example.com"


def test_verify_wrong_audience_raises_iap_error():
    """Gate (authentication): Verify wrong audience raises iap error."""
    private_key, jwk = _generate_jwk()
    verifier = IapVerifier(AUDIENCE, jwks_fetcher=_counting_fetcher({"keys": [jwk]}))
    token = _sign(private_key, aud="some-other-audience")

    with pytest.raises(IapError):
        verifier.verify(token)


def test_verify_wrong_issuer_raises_iap_error():
    """Gate (authentication): Verify wrong issuer raises iap error."""
    private_key, jwk = _generate_jwk()
    verifier = IapVerifier(AUDIENCE, jwks_fetcher=_counting_fetcher({"keys": [jwk]}))
    token = _sign(private_key, iss="https://not-cloud.google.com/iap")

    with pytest.raises(IapError):
        verifier.verify(token)


def test_verify_expired_token_raises_iap_error():
    """Gate (authentication): Verify expired token raises iap error."""
    private_key, jwk = _generate_jwk()
    verifier = IapVerifier(AUDIENCE, jwks_fetcher=_counting_fetcher({"keys": [jwk]}))
    now = int(time.time())
    token = _sign(private_key, iat=now - 7200, exp=now - 3600)

    with pytest.raises(IapError):
        verifier.verify(token)


def test_verify_error_message_never_contains_the_token():
    """Gate (authentication): Verify error message never contains the token."""
    private_key, jwk = _generate_jwk()
    verifier = IapVerifier(AUDIENCE, jwks_fetcher=_counting_fetcher({"keys": [jwk]}))
    token = _sign(private_key, aud="some-other-audience")

    with pytest.raises(IapError) as excinfo:
        verifier.verify(token)

    assert token not in str(excinfo.value)


def test_verify_kid_rotation_refetches_once_and_succeeds():
    """Gate (authentication): Verify kid rotation refetches once and succeeds.

    The fetcher returns an EMPTY key set on the first call (the cache's
    only view of the world so far) and the real key on the second — the
    kid-rotation path: verify() must still succeed, and the fetcher must
    have been called exactly twice (the initial cache-empty fetch, plus one
    unconditional refetch on the kid miss — no more, no less)."""
    private_key, jwk = _generate_jwk()
    fetcher = _counting_fetcher({"keys": []}, {"keys": [jwk]})
    verifier = IapVerifier(AUDIENCE, jwks_fetcher=fetcher)
    token = _sign(private_key)

    claims = verifier.verify(token)

    assert claims["email"] == "person@example.com"
    assert fetcher.calls == 2


def test_verify_alg_none_token_raises_iap_error():
    """Gate (authentication): Verify alg none token raises iap error.

    An unsigned `alg: none` token — otherwise a valid-shaped payload
    carrying the real `kid` — must still be rejected. There is no
    signature at all here; accepting it would mean skipping verification
    entirely whenever the header merely claims `none`."""
    _, jwk = _generate_jwk()
    verifier = IapVerifier(AUDIENCE, jwks_fetcher=_counting_fetcher({"keys": [jwk]}))
    token = jwt.encode(
        _forged_claims(), key=None, algorithm="none", headers={"kid": KID}
    )

    with pytest.raises(IapError):
        verifier.verify(token)


def test_verify_hs256_substitution_raises_iap_error():
    """Gate (authentication): Verify hs256 substitution raises iap error.

    A token HMAC-signed with a plain shared secret (not IAP's ES256
    key), carrying the real `kid` in its header. Pinning
    `algorithms=["ES256"]` must reject this outright — the classic
    RS256-as-HS256 confusion attack, HS256 flavor."""
    _, jwk = _generate_jwk()
    verifier = IapVerifier(AUDIENCE, jwks_fetcher=_counting_fetcher({"keys": [jwk]}))
    token = jwt.encode(
        _forged_claims(),
        "a-plain-shared-secret-well-over-32-bytes-long",
        algorithm="HS256",
        headers={"kid": KID},
    )

    with pytest.raises(IapError):
        verifier.verify(token)


def test_verify_rs256_substitution_raises_iap_error():
    """Gate (authentication): Verify rs256 substitution raises iap error.

    A token signed with a freshly generated RSA key under RS256,
    carrying the real `kid`. A different asymmetric alg than the pinned
    ES256 — must still be rejected rather than verified against whatever
    alg the header names."""
    _, jwk = _generate_jwk()
    verifier = IapVerifier(AUDIENCE, jwks_fetcher=_counting_fetcher({"keys": [jwk]}))
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt.encode(
        _forged_claims(), rsa_key, algorithm="RS256", headers={"kid": KID}
    )

    with pytest.raises(IapError):
        verifier.verify(token)


def test_disabled_iap_audience_allows_requests_without_header(client):
    # `client` (conftest.py) is built from the plain `config` fixture, whose
    # `iap_audience` defaults to "" — disabled. Every existing test suite
    # already proves every route works with no header in this state; this is
    # the one explicit test for it.
    """Gate (authentication): Disabled iap audience allows requests without header."""
    response = client.get("/api/healthz")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_enabled_no_header_401(session_factory, iap_config):
    """Gate (authentication): Enabled no header 401."""
    _, jwk = _generate_jwk()
    test_client = _client_with_fetcher(
        session_factory, iap_config, _counting_fetcher({"keys": [jwk]})
    )

    response = test_client.get("/api/agents")

    assert response.status_code == 401
    assert response.json() == UNAUTHORIZED_BODY


def test_enabled_valid_token_200_and_iap_email_set(session_factory, iap_config):
    """Gate (authentication): Enabled valid token 200 and iap email set."""
    private_key, jwk = _generate_jwk()
    test_client = _client_with_fetcher(
        session_factory, iap_config, _counting_fetcher({"keys": [jwk]})
    )
    token = _sign(private_key, email="person@example.com")

    response = test_client.get("/probe", headers={ASSERTION_HEADER: token})

    assert response.status_code == 200
    assert response.json() == {"email": "person@example.com"}


def test_enabled_healthz_exempt_with_no_header(session_factory, iap_config):
    """Gate (authentication): Enabled healthz exempt with no header."""
    _, jwk = _generate_jwk()
    test_client = _client_with_fetcher(
        session_factory, iap_config, _counting_fetcher({"keys": [jwk]})
    )

    response = test_client.get("/api/healthz")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_enabled_ui_dist_paths_require_the_header_too(session_factory, iap_config, tmp_path):
    """Gate (authentication): Enabled ui dist paths require the header too.

    The coverage rule is EVERYTHING except EXEMPT_PATHS — explicitly
    including the built UI's static/SPA paths (Task 1's `mount_ui`), since
    the identity proxy sits in front of the whole app, not just `/api/*`.
    Both the SPA fallback route ("/") and a real static asset under
    "/assets" must 401 with no header and succeed with a valid one.
    """
    private_key, jwk = _generate_jwk()
    dist_dir = _make_minimal_dist(tmp_path)
    ui_config = iap_config.model_copy(update={"ui_dist": dist_dir})
    app = create_app(
        session_factory,
        config=ui_config,
        iap_jwks_fetcher=_counting_fetcher({"keys": [jwk]}),
    )
    test_client = TestClient(app)

    root_no_header = test_client.get("/")
    assets_no_header = test_client.get("/assets/app.js")

    assert root_no_header.status_code == 401
    assert root_no_header.json() == UNAUTHORIZED_BODY
    assert assets_no_header.status_code == 401
    assert assets_no_header.json() == UNAUTHORIZED_BODY

    token = _sign(private_key)
    root_with_header = test_client.get("/", headers={ASSERTION_HEADER: token})
    assets_with_header = test_client.get(
        "/assets/app.js", headers={ASSERTION_HEADER: token}
    )

    assert root_with_header.status_code == 200
    assert root_with_header.text == "<html>SPA-MARKER</html>"
    assert assets_with_header.status_code == 200


def test_is_exempt_covers_the_mcp_roots_and_healthz_but_not_lookalikes():
    """Gate (authentication): Is exempt covers the mcp roots and healthz but not lookalikes."""
    assert EXEMPT_PREFIXES == ("/mcp", "/oauth", "/.well-known", "/broker", "/slack/relay", "/slack/oauth")
    # exact healthz (unchanged behavior)
    assert _is_exempt("/api/healthz") is True
    # the MCP roots, exact and as prefixes
    for exempt in (
        "/mcp",
        "/mcp/",
        "/oauth",
        "/oauth/authorize",
        "/oauth/token",
        "/oauth/google/callback",
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource",
    ):
        assert _is_exempt(exempt) is True, exempt
    # lookalikes on a non-segment boundary must NOT be exempt
    for guarded in (
        "/mcpevil",
        "/oauthx",
        "/oauth-something",
        "/.well-knownx",
        "/api/agents",
        "/api/agents/x/teach",
        "/",
    ):
        assert _is_exempt(guarded) is False, guarded


def test_enabled_mcp_lookalike_roots_still_401(session_factory, iap_config):
    """Gate (authentication): Enabled mcp lookalike roots still 401.

    The boundary discipline end to end: a path that merely starts with an
    exempt root's characters but not on a segment boundary is still IAP-gated."""
    _, jwk = _generate_jwk()
    test_client = _client_with_fetcher(
        session_factory, iap_config, _counting_fetcher({"keys": [jwk]})
    )

    for lookalike in ("/oauthx", "/mcpevil", "/.well-knownx"):
        resp = test_client.get(lookalike)
        assert resp.status_code == 401, f"{lookalike} should be IAP-gated"
        assert resp.json() == UNAUTHORIZED_BODY


def test_is_exempt_normalizes_before_matching_so_traversal_cannot_forge_exemption():
    """Gate (authentication): Is exempt normalizes before matching so traversal cannot forge exemption.

    Defense in depth (IAP-exemption review): a percent-decoded traversal
    reaches the middleware as a literal, un-collapsed dotted path. It must be
    canonicalized before the prefix compare, so it cannot borrow an exempt
    root's prefix to slip an /api target past IAP."""
    # decoded %2f traversals that START with an exempt root but RESOLVE to /api
    for forged in (
        "/mcp/../../api/agents",
        "/mcp/../api/agents",
        "/oauth/../api/agents",
        "/.well-known/../api/agents",
        "//mcp/../api/agents",       # leading double-slash collapsed first
    ):
        assert _is_exempt(forged) is False, forged
    # and the reverse: a dotted path that RESOLVES into an exempt root is
    # exempt (it genuinely targets the real endpoint)
    assert _is_exempt("/api/agents/../../oauth/authorize") is True
    # trailing slash still exempt after normpath strips it
    assert _is_exempt("/mcp/") is True
    assert _is_exempt("/oauth/") is True


def test_plain_http_post_on_the_oauth_surface_is_400_never_redirected(
    session_factory, iap_config
):
    """Gate (authentication): Plain http post on the oauth surface is 400 never redirected.

    (b) A token POST's credentials must never transit plain http, and a
    redirect would invite the client to resubmit them -- so it is refused
    outright, not redirected."""
    _, jwk = _generate_jwk()
    test_client = _client_with_fetcher(
        session_factory, iap_config, _counting_fetcher({"keys": [jwk]})
    )

    for method, path in (
        ("post", "/oauth/token"),
        ("post", "/oauth/register"),
        ("post", "/mcp/"),
        ("put", "/oauth/token"),
        ("delete", "/mcp/"),
        ("patch", "/oauth/token"),
    ):
        resp = getattr(test_client, method)(
            path, headers=HTTP_PROTO, follow_redirects=False
        )
        assert resp.status_code == 400, f"{method} {path}"
        assert resp.json() == HTTPS_REQUIRED_BODY, f"{method} {path}"
        assert "location" not in resp.headers, f"{method} {path}"


def test_duplicate_forwarded_proto_lines_cannot_smuggle_past_an_http_hop(
    session_factory, iap_config
):
    """Gate (authentication): Duplicate forwarded proto lines cannot smuggle past an http hop.

    Fail CLOSED against header splitting: `Headers.get` only sees the
    FIRST line a name occurs on, so a `https` line followed by a SECOND,
    separate `http` line would report clean under a `.get`-based check even
    though a real hop said http. The guard must fire on this, not pass it
    through."""
    _, jwk = _generate_jwk()
    test_client = _client_with_fetcher(
        session_factory, iap_config, _counting_fetcher({"keys": [jwk]})
    )

    headers = httpx.Headers(
        [
            (FORWARDED_PROTO_HEADER, "https"),
            (FORWARDED_PROTO_HEADER, "http"),
        ]
    )
    resp = test_client.get(
        "/oauth/authorize", headers=headers, follow_redirects=False
    )

    assert resp.status_code == 308
    assert resp.headers["location"] == "https://testserver/oauth/authorize"


def test_broker_lane_is_untouched_by_the_https_guard(session_factory, iap_config):
    """Gate (authentication): Broker lane is untouched by the https guard.

    (c) The in-cluster /broker lane must keep working over plain http even
    if some future proxy does set the header on it: never 308'd, never 400'd
    with the https copy. (These reach their real routes and fail their own
    agent-JWT check -- the point is WHICH gate answered.)"""
    _, jwk = _generate_jwk()
    test_client = _client_with_fetcher(
        session_factory, iap_config, _counting_fetcher({"keys": [jwk]})
    )

    for method, path in (
        ("get", "/broker/agents/wren/jira/search?q=x"),
        ("get", "/broker/agents/wren/jira/issues/AREQ-1"),
    ):
        for headers in ({}, HTTP_PROTO, HTTPS_PROTO):
            resp = getattr(test_client, method)(
                path, headers=headers, follow_redirects=False
            )
            assert resp.status_code != 308, f"{method} {path} {headers}"
            assert resp.json() != HTTPS_REQUIRED_BODY, f"{method} {path} {headers}"


def test_https_guard_is_installed_outside_the_iap_gate(session_factory, iap_config):
    """Gate (authentication): Https guard is installed outside the iap gate.

    The ordering create_app relies on, pinned structurally because it is
    NOT observable in behavior: every root the guard covers is a root the IAP
    gate exempts, so both orders happen to answer a plain-http /oauth request
    the same way today. `add_middleware` inserts at index 0, so index 0 is the
    OUTERMOST layer and runs first -- the https guard must be there, ahead of
    IapMiddleware, so a plain-http request is refused before any credential
    in it is read and regardless of how the IAP gate's exemption list later
    changes."""
    app = create_app(session_factory, config=iap_config)
    classes = [m.cls for m in app.user_middleware]

    assert classes.index(HttpsOnlyMiddleware) == 0
    assert classes.index(HttpsOnlyMiddleware) < classes.index(IapMiddleware)


def test_discovery_document_urls_are_https_independent_of_the_request_scheme():
    """Gate (authentication): Discovery document urls are https independent of the request scheme.

    (f) The brief's inspection point. FINDING: the discovery documents do
    NOT derive their URLs from the request at all -- `oauth_as._issuer` /
    `_endpoint` build every URL from the configured `mcp.issuer_base_url`
    (required non-empty the moment MCP is enabled, by
    `Config._mcp_requires_an_issuer_base_url_when_enabled`). So there is
    nothing to force to https; this test PINS that staticness, so a future
    refactor to `request.base_url` (the scheme-derived shape, which survives
    today only in `_google_redirect_uri`'s unreachable flag-off fallback)
    cannot silently start publishing http:// endpoint URLs.

    Harness mirrors test_mcp_oauth.py: just the AS router and a config, no
    guard middleware -- so the XFP=http request actually REACHES the routes
    rather than being 308'd, which is the only way to observe what the
    documents would say.
    """
    from kobo_api.config import Config, MCPConfig
    from kobo_api.mcp import oauth_as

    app = FastAPI()
    app.include_router(oauth_as.router)
    app.state.config = Config(
        mcp=MCPConfig(
            allow_without_iap=True, oauth_client_id="mcp-test-client",
            workspace_domain="example.com",
            issuer_base_url="https://kobo.example.org",
        )
    )
    test_client = TestClient(app, follow_redirects=False)

    as_doc = test_client.get(
        "/.well-known/oauth-authorization-server", headers=HTTP_PROTO
    )
    pr_doc = test_client.get(
        "/.well-known/oauth-protected-resource", headers=HTTP_PROTO
    )

    assert as_doc.status_code == 200
    assert pr_doc.status_code == 200
    for url in (
        as_doc.json()["issuer"],
        as_doc.json()["authorization_endpoint"],
        as_doc.json()["token_endpoint"],
        as_doc.json()["registration_endpoint"],
        pr_doc.json()["resource"],
        pr_doc.json()["authorization_servers"][0],
    ):
        assert url.startswith("https://kobo.example.org"), url

    # byte-identical with the header absent and with it claiming https:
    # proof the documents never consult the request's scheme
    assert (
        test_client.get("/.well-known/oauth-authorization-server").json()
        == test_client.get(
            "/.well-known/oauth-authorization-server", headers=HTTPS_PROTO
        ).json()
        == as_doc.json()
    )


# --- from test_mcp_oauth.py ---


CLIENT_ID = "mcp-test-client"


WORKSPACE_DOMAIN = "example.com"


EMAIL = "person@example.com"


# fix round 1: mcp.issuer_base_url is now required the moment
# mcp.oauth_client_id is set (Config._mcp_requires_an_issuer_base_url_when_
# enabled), so the default `config` fixture below must set it too, same as
# it already sets workspace_domain. test_well_known_uses_configured_issuer_
# base_url_when_set below still separately exercises a different value to
# pin the "well-known docs use the configured issuer" behavior; this one
# just needs to be non-empty.
ISSUER_BASE_URL = "https://kobo-test.example.org"


CLAUDE_REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"


class FakeGoogleIdentity:
    """Test double for `oauth_as.GoogleIdentity`. `exchange_and_verify`
    returns canned claims keyed by the `code` presented, or raises
    `raise_on_exchange` if set -- never touches the network. `auth_url`
    just records its args (so a test can recover the minted `state_jwt`
    without parsing a fake URL) and returns a deterministic placeholder.
    """

    def __init__(self):
        self.claims_by_code: dict[str, dict] = {}
        self.auth_url_calls: list[dict] = []
        self.exchange_calls: list[dict] = []
        self.raise_on_exchange: Exception | None = None

    def set_claims(self, code: str, claims: dict) -> None:
        self.claims_by_code[code] = claims

    def auth_url(self, *, state_jwt: str, redirect_uri: str) -> str:
        self.auth_url_calls.append({"state_jwt": state_jwt, "redirect_uri": redirect_uri})
        return f"https://idp.example.com/auth?state={state_jwt}"

    def exchange_and_verify(self, *, code: str, redirect_uri: str) -> dict:
        self.exchange_calls.append({"code": code, "redirect_uri": redirect_uri})
        if self.raise_on_exchange is not None:
            raise self.raise_on_exchange
        return self.claims_by_code[code]


@pytest.fixture
def session_factory__mcp_oauth(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__mcp_oauth():
    return Config(
        mcp=MCPConfig(
            allow_without_iap=True, oauth_client_id=CLIENT_ID,
            workspace_domain=WORKSPACE_DOMAIN,
            issuer_base_url=ISSUER_BASE_URL,
        )
    )


@pytest.fixture
def fake_google():
    return FakeGoogleIdentity()


def _make_app(*, config__mcp_oauth, session_factory__mcp_oauth, google=None):
    app = FastAPI()
    app.state.config = config__mcp_oauth
    app.state.session_factory = session_factory__mcp_oauth
    if google is not None:
        app.state.mcp_google = google
    app.include_router(oauth_as.router)
    return app


@pytest.fixture
def app(config__mcp_oauth, session_factory__mcp_oauth, fake_google):
    return _make_app(config__mcp_oauth=config__mcp_oauth, session_factory__mcp_oauth=session_factory__mcp_oauth, google=fake_google)


@pytest.fixture
def client__mcp_oauth(app):
    return TestClient(app, follow_redirects=False)


def _pkce_pair() -> tuple[str, str]:
    verifier = "a-fixed-test-code-verifier-that-is-long-enough-0123456789"
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


def _authorize(
    client__mcp_oauth,
    *,
    challenge: str,
    redirect_uri: str = CLAUDE_REDIRECT_URI,
    client_id: str = CLIENT_ID,
    state: str = "client-state-1",
    code_challenge_method: str = "S256",
    response_type: str = "code",
):
    params = {
        "response_type": response_type,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": code_challenge_method,
        "state": state,
    }
    return client__mcp_oauth.get("/oauth/authorize", params=params)


def _code_from_redirect(location: str) -> str:
    return parse_qs(urlparse(location).query)["code"][0]


def _full_grant(client__mcp_oauth, fake_google, *, code: str = "google-code") -> tuple[str, str, str]:
    """Runs authorize -> callback -> token to a successful end, returning
    (access_token, refresh_token, verifier). `verifier` is handed back so
    callers that don't need it can ignore it."""
    verifier, challenge = _pkce_pair()
    authorize_resp = _authorize(client__mcp_oauth, challenge=challenge)
    assert authorize_resp.status_code == 302
    state_jwt = fake_google.auth_url_calls[-1]["state_jwt"]

    fake_google.set_claims(code, {"email": EMAIL, "hd": WORKSPACE_DOMAIN})
    callback_resp = client__mcp_oauth.get(
        "/oauth/google/callback", params={"code": code, "state": state_jwt}
    )
    assert callback_resp.status_code == 302
    code_jwt = _code_from_redirect(callback_resp.headers["location"])

    token_resp = client__mcp_oauth.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code_jwt,
            "code_verifier": verifier,
            "redirect_uri": CLAUDE_REDIRECT_URI,
            "client_id": CLIENT_ID,
        },
    )
    assert token_resp.status_code == 200
    body = token_resp.json()
    return body["access_token"], body["refresh_token"], verifier


def _code_grant(client__mcp_oauth, fake_google, *, code: str = "google-code"):
    """authorize -> callback, returning (code_jwt, verifier) without exchanging."""
    verifier, challenge = _pkce_pair()
    assert _authorize(client__mcp_oauth, challenge=challenge).status_code == 302
    state_jwt = fake_google.auth_url_calls[-1]["state_jwt"]
    fake_google.set_claims(code, {"email": EMAIL, "hd": WORKSPACE_DOMAIN})
    callback = client__mcp_oauth.get("/oauth/google/callback", params={"code": code, "state": state_jwt})
    assert callback.status_code == 302
    return _code_from_redirect(callback.headers["location"]), verifier


def _exchange(client__mcp_oauth, code_jwt, verifier):
    return client__mcp_oauth.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code_jwt,
            "code_verifier": verifier,
            "redirect_uri": CLAUDE_REDIRECT_URI,
            "client_id": CLIENT_ID,
        },
    )


def test_happy_path_end_to_end_issues_a_verifiable_access_token(client__mcp_oauth, fake_google):
    """Gate (authentication): Happy path end to end issues a verifiable access token."""
    access_token, refresh_token, _ = _full_grant(client__mcp_oauth, fake_google, code="google-code-1")

    claims = verify(access_token, "access_token", CLIENT_ID)

    assert claims["sub"] == EMAIL
    assert claims["scope"] == "mcp"
    # A refresh token was minted too, carrying the family plumbing.
    refresh_claims = verify(refresh_token, "refresh_token", CLIENT_ID)
    assert refresh_claims["sub"] == EMAIL
    assert "rtid" in refresh_claims
    assert "family_id" in refresh_claims


def test_missing_pkce_400(client__mcp_oauth):
    """Gate (authentication): Missing pkce 400."""
    resp = client__mcp_oauth.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": CLAUDE_REDIRECT_URI,
            "state": "s1",
        },
    )

    assert resp.status_code == 400


def test_plain_pkce_method_rejected(client__mcp_oauth):
    """Gate (authentication): Plain pkce method rejected."""
    _, challenge = _pkce_pair()

    resp = _authorize(client__mcp_oauth, challenge=challenge, code_challenge_method="plain")

    assert resp.status_code == 400


def test_hd_mismatch_in_callback_claims_403_even_with_a_valid_email(client__mcp_oauth, fake_google):
    """Gate (authentication): Hd mismatch in callback claims 403 even with a valid email."""
    _, challenge = _pkce_pair()
    _authorize(client__mcp_oauth, challenge=challenge)
    state_jwt = fake_google.auth_url_calls[-1]["state_jwt"]
    fake_google.set_claims("google-code-3", {"email": EMAIL, "hd": "not-" + WORKSPACE_DOMAIN})

    resp = client__mcp_oauth.get(
        "/oauth/google/callback", params={"code": "google-code-3", "state": state_jwt}
    )

    assert resp.status_code == 403


def test_hd_absent_in_callback_claims_403(client__mcp_oauth, fake_google):
    """Gate (authentication): Hd absent in callback claims 403."""
    _, challenge = _pkce_pair()
    _authorize(client__mcp_oauth, challenge=challenge)
    state_jwt = fake_google.auth_url_calls[-1]["state_jwt"]
    fake_google.set_claims("google-code-4", {"email": EMAIL})  # no hd at all

    resp = client__mcp_oauth.get(
        "/oauth/google/callback", params={"code": "google-code-4", "state": state_jwt}
    )

    assert resp.status_code == 403


def test_tampered_state_400(client__mcp_oauth, fake_google):
    """Gate (authentication): Tampered state 400."""
    _, challenge = _pkce_pair()
    _authorize(client__mcp_oauth, challenge=challenge)
    state_jwt = fake_google.auth_url_calls[-1]["state_jwt"]
    # Flip a character in the PAYLOAD segment, not the token's last
    # character overall: HS256's 32-byte digest leaves the final base64url
    # symbol's low 2 bits unused (32 % 3 == 2 leftover bytes), so a flip
    # confined to the signature's own last char can occasionally decode to
    # the identical signature bytes and verify anyway (test_mcp_jwt.py's
    # own test_tampered_payload_rejected documents the same reasoning).
    # Tampering the payload always changes the signed content instead.
    header, payload, signature = state_jwt.split(".")
    tampered_char = "A" if payload[-1] != "A" else "B"
    tampered_payload = payload[:-1] + tampered_char
    tampered = f"{header}.{tampered_payload}.{signature}"

    resp = client__mcp_oauth.get("/oauth/google/callback", params={"code": "whatever", "state": tampered})

    assert resp.status_code == 400
    # Tampered state must be rejected before ever calling out to Google.
    assert fake_google.exchange_calls == []


def test_pkce_verifier_mismatch_400(client__mcp_oauth, fake_google):
    """Gate (authentication): Pkce verifier mismatch 400."""
    _, challenge = _pkce_pair()
    _authorize(client__mcp_oauth, challenge=challenge)
    state_jwt = fake_google.auth_url_calls[-1]["state_jwt"]
    fake_google.set_claims("google-code-5", {"email": EMAIL, "hd": WORKSPACE_DOMAIN})
    callback_resp = client__mcp_oauth.get(
        "/oauth/google/callback", params={"code": "google-code-5", "state": state_jwt}
    )
    code_jwt = _code_from_redirect(callback_resp.headers["location"])

    resp = client__mcp_oauth.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code_jwt,
            "code_verifier": "the-wrong-verifier-entirely",
            "redirect_uri": CLAUDE_REDIRECT_URI,
            "client_id": CLIENT_ID,
        },
    )

    assert resp.status_code == 400
    assert resp.json() == {"error": "invalid_grant"}


def test_redirect_uri_mismatch_at_token_endpoint_400(client__mcp_oauth, fake_google):
    """Gate (authentication): Redirect uri mismatch at token endpoint 400."""
    _, challenge = _pkce_pair()
    _authorize(client__mcp_oauth, challenge=challenge)
    state_jwt = fake_google.auth_url_calls[-1]["state_jwt"]
    fake_google.set_claims("google-code-8", {"email": EMAIL, "hd": WORKSPACE_DOMAIN})
    callback_resp = client__mcp_oauth.get(
        "/oauth/google/callback", params={"code": "google-code-8", "state": state_jwt}
    )
    code_jwt = _code_from_redirect(callback_resp.headers["location"])

    resp = client__mcp_oauth.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code_jwt,
            "code_verifier": "irrelevant",
            "redirect_uri": "http://localhost:1/somewhere-else",
            "client_id": CLIENT_ID,
        },
    )

    assert resp.status_code == 400
    assert resp.json() == {"error": "invalid_grant"}


def test_refresh_replay_returns_400_and_revokes_the_family(client__mcp_oauth, fake_google, session_factory__mcp_oauth):
    """Gate (authentication): Refresh replay returns 400 and revokes the family."""
    _, refresh_token, _ = _full_grant(client__mcp_oauth, fake_google, code="google-code-11")
    first = client__mcp_oauth.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": CLIENT_ID},
    )
    assert first.status_code == 200

    replay = client__mcp_oauth.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": CLIENT_ID},
    )

    assert replay.status_code == 400
    assert replay.json() == {"error": "invalid_grant"}

    claims = verify(refresh_token, "refresh_token", CLIENT_ID)
    session = session_factory__mcp_oauth()
    try:
        rows = session.query(MCPRefreshToken).filter_by(family_id=claims["family_id"]).all()
        assert len(rows) >= 2
        assert all(row.revoked_at is not None for row in rows)
    finally:
        session.close()


def test_a_code_exchanged_twice_fails_and_kills_the_first_grants_family(client__mcp_oauth, fake_google):
    """Gate (authentication): A code exchanged twice fails and kills the first grants family."""
    code_jwt, verifier = _code_grant(client__mcp_oauth, fake_google)
    first = _exchange(client__mcp_oauth, code_jwt, verifier)
    assert first.status_code == 200
    refresh_token = first.json()["refresh_token"]

    second = _exchange(client__mcp_oauth, code_jwt, verifier)
    assert second.status_code == 400
    assert second.json()["error"] == "invalid_grant"

    # The first exchange's tokens are gone too: a refresh on them fails.
    refreshed = client__mcp_oauth.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": CLIENT_ID},
    )
    assert refreshed.status_code == 400
    assert refreshed.json()["error"] == "invalid_grant"


def test_authorization_server_metadata_carries_the_right_endpoints(client__mcp_oauth):
    """Gate (authentication): Authorization server metadata carries the right endpoints."""
    resp = client__mcp_oauth.get("/.well-known/oauth-authorization-server")

    assert resp.status_code == 200
    body = resp.json()
    assert body["authorization_endpoint"].endswith("/oauth/authorize")
    assert body["token_endpoint"].endswith("/oauth/token")
    assert body["registration_endpoint"].endswith("/oauth/register")
    assert body["code_challenge_methods_supported"] == ["S256"]
    assert "authorization_code" in body["grant_types_supported"]
    assert "refresh_token" in body["grant_types_supported"]


# --- from test_mcp_jwt.py ---


AUD = "test-client"


SUB = "person@example.com"


TYPES = ["state", "authorization_code", "access_token", "refresh_token"]


@pytest.fixture(autouse=True)
def _clean_key_state(monkeypatch):
    """Every test starts with no signing env var set and no cached
    ephemeral key -- the module-level `_ephemeral_key` cache would
    otherwise leak the "already warned" state (and the key itself) from
    one test into the next."""
    monkeypatch.delenv("KOBO_MCP_JWT_KEY", raising=False)
    monkeypatch.setattr(mcp_jwt, "_ephemeral_key", None)


def _key() -> bytes:
    return b"0" * 32


@pytest.mark.parametrize("typ", TYPES)
def test_round_trip_each_typ(typ):
    """Gate (authentication): Round trip each typ."""
    token = mcp_jwt.mint(typ, SUB, AUD, 60, key=_key())

    claims = mcp_jwt.verify(token, typ, AUD, key=_key())

    assert claims["typ"] == typ
    assert claims["sub"] == SUB
    assert claims["aud"] == AUD
    assert "iat" in claims
    assert "exp" in claims
    assert "jti" in claims


@pytest.mark.parametrize(
    "reserved,value",
    [
        ("iat", 0),
        ("exp", 9999999999),
        ("jti", "smuggled-jti"),
    ],
)
def test_mint_rejects_reserved_extra_claims(reserved, value):
    """Gate (authentication): Mint rejects reserved extra claims."""
    with pytest.raises(mcp_jwt.TokenError):
        mcp_jwt.mint(
            "access_token", SUB, AUD, 60, key=_key(), **{reserved: value}
        )


def test_wrong_aud_rejected():
    """Gate (authentication): Wrong aud rejected."""
    token = mcp_jwt.mint("access_token", SUB, "aud-a", 60, key=_key())

    with pytest.raises(mcp_jwt.TokenError):
        mcp_jwt.verify(token, "access_token", "aud-b", key=_key())


def test_tampered_payload_rejected():
    """Gate (authentication): Tampered payload rejected."""
    token = mcp_jwt.mint("access_token", SUB, AUD, 60, key=_key())
    header, payload, signature = token.split(".")
    # Flip a character in the payload segment -- signature no longer covers
    # what's decoded, so this must fail as a bad signature, not silently
    # decode into a different (but validly-signed) set of claims.
    tampered_char = "A" if payload[-1] != "A" else "B"
    tampered_payload = payload[:-1] + tampered_char
    tampered_token = f"{header}.{tampered_payload}.{signature}"

    with pytest.raises(mcp_jwt.TokenError):
        mcp_jwt.verify(tampered_token, "access_token", AUD, key=_key())


def test_none_algorithm_rejected():
    """Gate (authentication): None algorithm rejected.

    A well-shaped but unsigned `alg: none` token must never verify --
    accepting it would mean HS256 wasn't actually pinned on decode."""
    now_claims = {"typ": "access_token", "sub": SUB, "aud": AUD, "jti": "x"}
    token = pyjwt.encode(now_claims, key=None, algorithm="none")

    with pytest.raises(mcp_jwt.TokenError):
        mcp_jwt.verify(token, "access_token", AUD, key=_key())


def test_signing_key_too_short_raises_token_error(monkeypatch):
    """Gate (authentication): Signing key too short raises token error."""
    monkeypatch.setenv(
        "KOBO_MCP_JWT_KEY", base64.b64encode(b"a" * 8).decode()
    )

    with pytest.raises(mcp_jwt.TokenError):
        mcp_jwt.signing_key()


# --- from test_mcp_refresh_tokens.py ---


EMAIL = "person@example.com"


OTHER_EMAIL = "someone-else@example.com"


@pytest.fixture
def session_factory__mcp_refresh_tokens(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def session(session_factory__mcp_refresh_tokens):
    s = session_factory__mcp_refresh_tokens()
    yield s
    s.close()


def _row(session, jti):
    return session.get(MCPRefreshToken, jti)


def test_consume_replay_revokes_every_row_in_the_family(session):
    """Gate (authentication): Consume replay revokes every row in the family."""
    create_family(session, jti="jti-0", email=EMAIL)
    session.commit()
    new_jti = consume(session, jti="jti-0", email=EMAIL)
    session.commit()

    consume(session, jti="jti-0", email=EMAIL)
    session.commit()

    old_row = _row(session, "jti-0")
    new_row = _row(session, new_jti)
    assert old_row.revoked_at is not None
    assert old_row.revoked_reason == "replay_detected"
    assert new_row.revoked_at is not None
    assert new_row.revoked_reason == "replay_detected"


def test_a_lost_update_race_revokes_the_family_as_replay(session_factory__mcp_refresh_tokens, monkeypatch):
    """Gate (authentication): A lost update race revokes the family as replay.

    The rowcount-guarded UPDATE exists for exactly this: a caller whose
    own pre-read looked rotatable, but who loses the race to another
    caller that rotates the same jti first. Two Session objects share one
    file-backed sqlite engine (via `session_factory`) so each can hold its
    own view of the same row.

    Session B takes its pre-read before A ever touches the row, then A
    runs a full rotation (rotate + insert child + commit). B then calls
    `consume()` for the same jti. To deterministically land B in the
    FALLBACK branch (rowcount 0 after a clean-looking pre-check) rather
    than the pre-check branch, B's own `session.get` is monkeypatched to
    keep handing back its stale pre-read for this jti, no matter how many
    times `consume()` calls it internally.

    Note: the brief's preferred approach was relying on SQLAlchemy's
    identity map alone (seed it with a pre-read, let `consume()`'s own
    `session.get()` hit the cache). That was tried first and found to be
    genuinely flaky here: across repeated runs of the identical test body,
    B's internal `session.get()` sometimes returned the stale cached row
    (landing in the fallback, as intended) and sometimes returned a fresh
    row reflecting A's committed rotation (landing in the pre-check
    instead) -- apparently timing-dependent on how the shared engine's
    connection pool hands out and reclaims connections between the two
    sessions. A test that only sometimes exercises the branch it exists to
    prove is worse than not having it, so this uses the explicitly
    sanctioned fallback: monkeypatching the lookup seam directly.
    """
    session_a = session_factory__mcp_refresh_tokens()
    session_b = session_factory__mcp_refresh_tokens()
    try:
        create_family(session_a, jti="jti-0", email=EMAIL)
        session_a.commit()

        # B's clean-looking pre-read, taken before A rotates anything.
        # Detached so it can't be silently refreshed by anything B's
        # session does later.
        stale_row = session_b.get(MCPRefreshToken, "jti-0")
        session_b.expunge(stale_row)

        # A completes a full rotation and commits it to disk.
        new_jti = consume(session_a, jti="jti-0", email=EMAIL)
        session_a.commit()
        assert new_jti is not None

        # Pin B's session.get for this jti to always return the stale,
        # pre-rotation snapshot -- deterministically reproducing "B's
        # pre-check saw an unrotated row" regardless of identity-map or
        # connection-pool timing. Every other call (there are none here)
        # falls through to the real session.get.
        real_get = session_b.get

        def _stale_get(model, pk):
            if model is MCPRefreshToken and pk == "jti-0":
                return stale_row
            return real_get(model, pk)

        monkeypatch.setattr(session_b, "get", _stale_get)

        # B now runs consume() for the same jti. Its internal session.get
        # returns the pinned stale row, so B sails past the "already
        # rotated" pre-check and reaches the guarded UPDATE, which runs
        # for real against the database and matches zero rows (A already
        # rotated jti-0 on disk) -- the fallback branch, not the
        # pre-check, is what fires here.
        result = consume(session_b, jti="jti-0", email=EMAIL)
        session_b.commit()

        assert result is None

        session_a.expire_all()
        original_row = session_a.get(MCPRefreshToken, "jti-0")
        child_row = session_a.get(MCPRefreshToken, new_jti)
        assert original_row.revoked_at is not None
        assert original_row.revoked_reason == "replay_detected"
        assert child_row.revoked_at is not None
        assert child_row.revoked_reason == "replay_detected"
    finally:
        session_a.close()
        session_b.close()


def test_consume_email_mismatch_returns_none(session):
    """Gate (authentication): Consume email mismatch returns none."""
    create_family(session, jti="jti-0", email=EMAIL)
    session.commit()

    result = consume(session, jti="jti-0", email=OTHER_EMAIL)

    assert result is None


def test_a_family_older_than_the_bound_is_revoked_on_its_next_refresh(session_factory__mcp_refresh_tokens):
    """Gate (authentication): A family older than the bound is revoked on its next refresh."""
    from datetime import timedelta

    from kobo_api.mcp import refresh_tokens as rt
    from kobo_api.models import MCPRefreshToken

    with session_factory__mcp_refresh_tokens() as session:
        family_id = rt.create_family(session, jti="old-jti", email="p@example.com")
        session.commit()
        row = session.get(MCPRefreshToken, "old-jti")
        row.issued_at = row.issued_at - rt.FAMILY_MAX_AGE - timedelta(days=1)
        session.commit()

    with session_factory__mcp_refresh_tokens() as session:
        assert rt.consume(session, jti="old-jti", email="p@example.com") is None
        session.commit()
        row = session.get(MCPRefreshToken, "old-jti")
        assert row.revoked_at is not None
        assert row.revoked_reason == rt.EXPIRED_REASON
        assert row.family_id == family_id


# --- from test_mcp_tools_read.py ---


GROUP_A = "growth"


GROUP_B = "design-team"


ADMIN_GROUP = "infra-engineering"


AUDIENCE__mcp_tools_read = "test-iap-audience"


CLIENT_ID__mcp_tools_read = "test-mcp-client"


WORKSPACE_DOMAIN = "example.com"


ISSUER_BASE_URL__mcp_tools_read = "https://kobo.example.org"


OWNER_A_EMAIL = "owner-a@example.com"


OWNER_B_EMAIL = "owner-b@example.com"


MEMBER_A_EMAIL = "member-a@example.com"


OPERATOR_EMAIL = "ops@example.com"


ADMIN_MEMBER_EMAIL = "admin-member@example.com"


class FakeGroupResolver:
    """The plainest possible `GroupResolver` stand-in, borrowed from
    `tests/test_mcp_authz.py`: a bare object exposing `groups_for(email)
    -> list[str] | None`, mapping-driven, or always-`None` ("the
    directory is down") when `down=True`."""

    def __init__(self, mapping: dict[str, list[str]] | None = None, *, down: bool = False):
        self._mapping = dict(mapping) if mapping else {}
        self._down = down

    def groups_for(self, email: str) -> list[str] | None:
        if self._down:
            return None
        return self._mapping.get(email.lower(), [])


def _resolver():
    return FakeGroupResolver(
        {
            MEMBER_A_EMAIL: [GROUP_A],
            OWNER_A_EMAIL: [GROUP_A],
            OWNER_B_EMAIL: [GROUP_B],
            ADMIN_MEMBER_EMAIL: [ADMIN_GROUP],
        }
    )


@pytest.fixture(autouse=True)
def _reset_rate_limit_and_audit():
    """Every test starts with a clean sliding window and an unconfigured
    audit layer -- same discipline `test_mcp_audit.py` holds, needed here
    too since `build_mcp_server` calls `audit.configure` for real."""
    rate_limit.reset()
    audit.configure(None)
    yield
    rate_limit.reset()
    audit.configure(None)


@pytest.fixture
def session_factory__mcp_tools_read(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__mcp_tools_read(tmp_path):
    return Config(
        workdir=tmp_path / "work",
        operators=[OPERATOR_EMAIL],
        admin_groups=[ADMIN_GROUP],
        groups=[
            GroupChoice(name=GROUP_A, okta_group="okta-growth"),
            GroupChoice(name=GROUP_B, okta_group="okta-design-team"),
            GroupChoice(name=ADMIN_GROUP, okta_group="okta-infra-engineering"),
        ],
        iap_audience=AUDIENCE__mcp_tools_read,
        mcp=MCPConfig(
            allow_without_iap=True, oauth_client_id=CLIENT_ID__mcp_tools_read,
            workspace_domain=WORKSPACE_DOMAIN,
            issuer_base_url=ISSUER_BASE_URL__mcp_tools_read,
        ),
    )


def _app(session_factory__mcp_tools_read, config__mcp_tools_read, *, groups_resolver=None, cabal=None, storage=None, github=None):
    clients = Clients(
        github=github if github is not None else FakeGitHub(),
        gojira=FakeGojira(),
        cloud=FakeCloud(),
        agent=FakeAgent(),
        kubectl=FakeKubectl(),
        groups=groups_resolver,
        cabal=cabal,
        storage=storage,
    )
    return create_app(session_factory__mcp_tools_read, config=config__mcp_tools_read, clients=clients)


def test_token_verifier_returns_none_for_the_wrong_token_type(session_factory__mcp_tools_read, config__mcp_tools_read):
    """Gate (authentication): Token verifier returns none for the wrong token type."""
    app = _app(session_factory__mcp_tools_read, config__mcp_tools_read, groups_resolver=_resolver())
    verifier = _KoboTokenVerifier(app)
    # A refresh_token minted for the same subject/audience -- verify_token
    # must reject it: an access token check is typ-specific.
    token = mint("refresh_token", OWNER_A_EMAIL, CLIENT_ID__mcp_tools_read, 900)

    assert asyncio.run(verifier.verify_token(token)) is None


# --- from test_api_org_status.py ---


WHOAMI_AUDIENCE = "test-audience"


# --- from test_api_org_status.py ---


WHOAMI_KID = "test-key-1"


def _generate_jwk__api_org_status(kid: str = WHOAMI_KID):
    private_key = ec.generate_private_key(ec.SECP256R1())
    jwk = jwt.algorithms.ECAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk["kid"] = kid
    jwk["use"] = "sig"
    jwk["alg"] = "ES256"
    return private_key, jwk


# --- from test_api_org_status.py ---


def _fetcher(jwk):
    def fetch():
        return {"keys": [jwk]}

    return fetch


def _iap_client(config: Config, tmp_path, *, jwk) -> TestClient:
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    return TestClient(
        create_app(
            session_factory,
            config=config,
            iap_jwks_fetcher=_fetcher(jwk),
        )
    )


# --- from test_api_org_status.py ---


def _sign_whoami(private_key, *, email: str | None, kid: str = WHOAMI_KID) -> str:
    now = int(time.time())
    claims = {
        "aud": WHOAMI_AUDIENCE,
        "iss": IAP_ISSUER,
        "iat": now,
        "exp": now + 3600,
        "sub": "accounts.google.com:1234567890",
    }
    if email is not None:
        claims["email"] = email
    return jwt.encode(claims, private_key, algorithm="ES256", headers={"kid": kid})


# --- from test_api_admin_mcp.py ---


KEY = b"k" * 32


def _client(tmp_path):
    values = {
        "workdir": tmp_path / "work",
        "iap_audience": WHOAMI_AUDIENCE,
        "operators": ["ops@example.com"],
        "admin_panel": True,
        "admin_groups": [],
        "mcp": MCPConfig(
            oauth_client_id="cid",
            workspace_domain="example.com",
            issuer_base_url="https://room.example.com",
        ),
    }
    private_key, jwk = _generate_jwk__api_org_status()
    client = _iap_client(Config(**values), tmp_path, jwk=jwk)
    return client, {
        ASSERTION_HEADER: _sign_whoami(private_key, email="ops@example.com")
    }


def _claims(token: str) -> dict:
    import jwt as pyjwt

    return pyjwt.decode(token, KEY, algorithms=["HS256"], audience="cid")


def test_revoke_cuts_the_families_logs_the_action_and_the_verifier_refuses_old_tokens(
    tmp_path, monkeypatch
):
    """Gate (authentication): Revoke cuts the families logs the action and the verifier refuses old tokens."""
    client, headers = _client(tmp_path)
    factory = client.app.state.session_factory
    with factory() as session:
        rt.create_family(session, jti="j1", email="gone@example.com")
        session.commit()
    monkeypatch.setattr(
        "kobo_api.mcp.server.verify", lambda token, typ, aud: _claims(token)
    )
    monkeypatch.setattr("kobo_api.mcp.jwt.signing_key", lambda *a, **k: KEY)
    verifier = mcp_server._KoboTokenVerifier(client.app)
    old_token = mint(
        "access_token", "gone@example.com", "cid", ACCESS_TTL_S, key=KEY, scope="mcp"
    )
    mcp_server.forget_revocation_cache()
    assert asyncio.run(verifier.verify_token(old_token)) is not None

    time.sleep(
        1.1
    )  # the revocation instant must be after the token's iat (second precision)
    resp = client.post(
        "/api/admin/mcp/revocations",
        json={"email": "Gone@example.com", "reason": "left"},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json() == {"email": "gone@example.com", "refresh_tokens_revoked": 1}

    assert asyncio.run(verifier.verify_token(old_token)) is None
    listed = client.get("/api/admin/mcp/revocations", headers=headers).json()[
        "revocations"
    ]
    assert [r["email"] for r in listed] == ["gone@example.com"]
    with factory() as session:
        actions = [(e.action, e.target_id) for e in audit_events(session)]
    assert ("mcp.revoke", "gone@example.com") in actions


# --- from test_pr_broker.py (the token verification the jira lane kept, kobo#365) ---


def test_verify_refuses_a_pass_issued_before_the_last_mint(monkeypatch):
    """Gate (authentication): Verify refuses a pass issued before the last mint."""
    from datetime import UTC, datetime

    from kobo_api import pr_broker

    class _Jwks:
        def get_signing_key_from_jwt(self, bearer):
            class _K:
                key = "k"

            return _K()

    monkeypatch.setattr(pr_broker, "_jwks_client", lambda url: _Jwks())
    monkeypatch.setattr(pr_broker.pyjwt, "decode", lambda *a, **k: {"sub": "agent:ward", "iat": 1_000})

    assert pr_broker.verify_agent_token("http://cabal", "t", "ward")["iat"] == 1_000
    assert pr_broker.verify_agent_token(
        "http://cabal", "t", "ward", not_before=datetime.fromtimestamp(900, UTC)
    )
    with pytest.raises(pr_broker.BrokerAuthError):
        pr_broker.verify_agent_token(
            "http://cabal", "t", "ward", not_before=datetime.fromtimestamp(1_100, UTC)
        )
    # Naive timestamps (the DateTime column's shape) are read as UTC.
    with pytest.raises(pr_broker.BrokerAuthError):
        pr_broker.verify_agent_token(
            "http://cabal", "t", "ward", not_before=datetime.fromtimestamp(1_100, UTC).replace(tzinfo=None)
        )


# --- from test_jira_broker.py ---


@pytest.fixture
def jira_config(tmp_path):
    pat = tmp_path / "jira-pat"
    pat.write_text("not-a-real-token\n")
    return Config(
        workdir=tmp_path / "work",
        cabal_url="http://cabal.test",
        jira_domain="tracker.test",
        jira_email="reader@acme.io",
        jira_pat_path=pat,
        jira_projects=["AREQ"],
        jira_read_agents=["ledger"],
    )


@pytest.fixture
def jira_client(session_factory, jira_config):
    return TestClient(create_app(session_factory, config=jira_config))


def _authed(monkeypatch):
    import kobo_api.main as main_mod

    monkeypatch.setattr(main_mod, "verify_agent_token", lambda *a, **k: None)
    return main_mod


@pytest.fixture
def alias_client(session_factory, jira_config):
    config = jira_config.model_copy(update={"jira_alias_projects": ["IE"]})
    return TestClient(create_app(session_factory, config=config))


def test_rejected_token_is_401(jira_client, monkeypatch):
    """Gate (authentication): Rejected token is 401."""
    import kobo_api.main as main_mod

    def refuse(*a, **k):
        raise BrokerAuthError("no")

    monkeypatch.setattr(main_mod, "verify_agent_token", refuse)
    r = jira_client.get(
        "/broker/agents/ledger/jira/issues/AREQ-1",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 401


def _tracker(routes):
    """A fake Jira: `routes` maps a path to (status, json body, headers)."""
    def handle(request: httpx.Request) -> httpx.Response:
        status, body, headers = routes.get(request.url.path, (500, {}, {}))
        return httpx.Response(status, json=body, headers=headers)
    return httpx.Client(transport=httpx.MockTransport(handle))


def test_a_rejected_tracker_credential_is_not_reported_as_a_missing_issue(jira_config):
    """Gate (integrity): Jira Cloud treats a Basic pair it cannot verify as anonymous,
    so a wrong token reads every issue as 404 and every search as empty. The lane
    asks the tracker who the credential is before it calls an issue missing."""
    from kobo_api import jira_broker
    from kobo_api.clients import ClientError

    args = dict(domain=jira_config.jira_domain, email=jira_config.jira_email, pat_path=jira_config.jira_pat_path)
    rejected = {"/rest/api/3/issue/AREQ-275": (404, {"errorMessages": ["Issue does not exist"]}, {}),
                "/rest/api/3/search/jql": (200, {"issues": []}, {}),
                "/rest/api/3/myself": (401, {"message": "Client must be authenticated"}, {"X-Seraph-LoginReason": "AUTHENTICATED_FAILED"})}
    with pytest.raises(ClientError, match="rejected the room's credential"):
        jira_broker.fetch_issue(**args, key="AREQ-275", http_client=_tracker(rejected))
    with pytest.raises(ClientError, match="rejected the room's credential"):
        jira_broker.search_issues(**args, project="AREQ", text="access token", limit=3, http_client=_tracker(rejected))

    # With a credential the tracker knows, a 404 is a missing issue and an empty search is empty.
    good = dict(rejected); good["/rest/api/3/myself"] = (200, {"emailAddress": "reader@acme.io"}, {})
    with pytest.raises(ClientError, match="no such issue"):
        jira_broker.fetch_issue(**args, key="AREQ-275", http_client=_tracker(good))
    assert jira_broker.search_issues(**args, project="AREQ", text="access token", limit=3, http_client=_tracker(good))["issues"] == []


def test_a_rejected_tracker_credential_reaches_the_agent_as_the_operators_problem(jira_client, monkeypatch):
    """Gate (integrity): the route answers 502 with the credential sentence, never a 404,
    because the runtime reads every 404 on this lane as "the room has not granted me access"."""
    from kobo_api.clients import ClientError
    from kobo_api.jira_broker import CREDENTIAL_REJECTED

    main_mod = _authed(monkeypatch)

    def rejected(**kwargs):
        raise ClientError(CREDENTIAL_REJECTED)

    monkeypatch.setattr(main_mod, "jira_fetch_issue", rejected)
    monkeypatch.setattr(main_mod, "jira_search_issues", rejected)
    r = jira_client.get("/broker/agents/ledger/jira/issues/AREQ-275", headers={"Authorization": "Bearer x"})
    assert r.status_code == 502 and "rejected the room's credential" in r.json()["error"] and "operator" in r.json()["error"]
    r = jira_client.get("/broker/agents/ledger/jira/search", params={"text": "access token"}, headers={"Authorization": "Bearer x"})
    assert r.status_code == 502 and "rejected the room's credential" in r.json()["error"]


def test_agent_off_the_allowlist_reads_as_404(jira_client, monkeypatch):
    """Gate (authentication): Agent off the allowlist reads as 404."""
    _authed(monkeypatch)
    r = jira_client.get(
        "/broker/agents/intruder/jira/issues/AREQ-1",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 404
    assert "no such agent" in r.json()["error"]


def test_key_outside_pinned_projects_is_refused_before_any_call(
    jira_client, monkeypatch
):
    """Gate (authentication): Key outside pinned projects is refused before any call."""
    main_mod = _authed(monkeypatch)

    def explode(**kwargs):
        raise AssertionError("the tracker must not be called for an off-lane key")

    monkeypatch.setattr(main_mod, "jira_fetch_issue", explode)
    for key in ("SECRETPROJ-9", "AREQ"):
        r = jira_client.get(
            f"/broker/agents/ledger/jira/issues/{key}",
            headers={"Authorization": "Bearer x"},
        )
        assert r.status_code == 403
        assert "AREQ" in r.json()["error"]


def test_alias_key_resolving_elsewhere_is_refused_after_the_fetch(
    alias_client, monkeypatch
):
    """Gate (authentication): Alias key resolving elsewhere is refused after the fetch."""
    main_mod = _authed(monkeypatch)

    def fake_fetch(**kwargs):
        return {"issue": {"key": "SECRETPROJ-9"}, "comments": [], "comments_truncated": False}

    monkeypatch.setattr(main_mod, "jira_fetch_issue", fake_fetch)
    r = alias_client.get(
        "/broker/agents/ledger/jira/issues/IE-1",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 403
    assert "AREQ" in r.json()["error"]


# --- from test_config.py ---


def test_mcp_enabled_requires_workspace_domain():
    """Gate (authentication): Mcp enabled requires workspace domain.

    An AS with no domain check must refuse to boot, fail-closed -- same
    "fail loudly at construction" posture as `_admin_groups_must_be_
    configured_groups` above."""
    with pytest.raises(ValidationError) as exc_info:
        Config(mcp=MCPConfig(allow_without_iap=True, oauth_client_id="some-client-id"))

    message = str(exc_info.value)
    assert (
        "mcp.oauth_client_id is set but mcp.workspace_domain is empty; "
        "the domain check is the org gate on the MCP path"
    ) in message


def test_mcp_issuer_base_url_over_plain_http_is_rejected():
    """Gate (authentication): Mcp issuer base url over plain http is rejected.

    The well-known documents echo `issuer_base_url` verbatim into
    `issuer`, `authorization_endpoint`, etc -- an http:// value would be the
    one in-repo way left to advertise a plain-http OAuth surface, so
    construction fails loudly instead of booting with it."""
    with pytest.raises(ValidationError) as exc_info:
        Config(mcp=MCPConfig(issuer_base_url="http://example.com"))

    message = str(exc_info.value)
    assert (
        "mcp.issuer_base_url must be https:// -- the OAuth discovery "
        "documents echo it verbatim"
    ) in message


def test_mcp_on_an_open_room_is_refused_unless_chosen():
    """Gate (authentication): Mcp on an open room is refused unless chosen."""
    mcp = {"oauth_client_id": "cid", "workspace_domain": "example.com", "issuer_base_url": "https://r.example.com"}
    with pytest.raises(ValidationError, match="allow_without_iap"):
        Config(mcp=mcp)
    Config(mcp={**mcp, "allow_without_iap": True})
    Config(mcp=mcp, iap_audience="/projects/1/global/backendServices/2")


def test_auth_regime_names_each_accidental_opening():
    """Gate (authentication): Auth regime names each accidental opening."""
    line, problems = Config().auth_regime(resolver_configured=False)
    assert "NO perimeter" in line
    assert any("allow_unauthenticated" in p for p in problems)

    _, none = Config(allow_unauthenticated=True).auth_regime(resolver_configured=False)
    assert none == []

    aud = "/projects/1/global/backendServices/2"
    _, open_admins = Config(iap_audience=aud).auth_regime(resolver_configured=False)
    assert any("destroy any agent" in p for p in open_admins)

    groups = [{"name": "SRE", "kind": "engineering", "okta_group": "sre"}]
    _, no_resolver = Config(iap_audience=aud, operators=["o@example.com"], groups=groups).auth_regime(
        resolver_configured=False
    )
    assert any("no directory resolver" in p for p in no_resolver)
    line, clean = Config(iap_audience=aud, operators=["o@example.com"], groups=groups).auth_regime(
        resolver_configured=True
    )
    assert clean == [] and "resolver on" in line



# --- the Slack relay (issue #371): Slack's signature is the credential ----------

RELAY_AGENT = "clio"
RELAY_ID = "relay-id-for-clio-0000"
RELAY_SECRET = "fake-relay-secret"
SIGNING_SECRET = "fake-signing-secret"


def _relay_room(tmp_path, *, signing_secret=SIGNING_SECRET, slack_relay=True):
    """A room with the relay on, one agent and its relay row; the row has
    no signing secret yet when `signing_secret` is None (the window between
    minting the id and Slack answering the create)."""
    from kobo_api.models import Agent, AgentStatus, SlackRelayApp

    engine = create_engine(f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as s:
        s.add(Agent(name=RELAY_AGENT, persona="helpful-generalist", model_id="vertex_ai/claude-sonnet-4-5",
                    status=AgentStatus.READY, repo_full_name="example-org/agent-clio", created_by="ops@example.com"))
        s.add(SlackRelayApp(relay_id=RELAY_ID, agent_name=RELAY_AGENT, app_id="A0FAKE", client_id="fake-client-id",
                            client_secret="fake-client-secret", signing_secret=signing_secret,
                            relay_secret=RELAY_SECRET, created_at=__import__("datetime").datetime(2026, 11, 1)))
        s.commit()
    config = Config(workdir=tmp_path / "work", slack_relay=slack_relay,
                    agent_endpoint_template="http://agent-{name}.default.svc.cluster.local")
    clients = Clients(github=FakeGitHub(), gojira=FakeGojira(), cloud=FakeCloud(), agent=FakeAgent(), kubectl=FakeKubectl())
    return TestClient(create_app(session_factory, config=config, clients=clients))


def _signed(body: bytes, *, secret: str = SIGNING_SECRET, ts: int | None = None) -> dict[str, str]:
    import hmac as _hmac

    stamp = str(ts if ts is not None else int(time.time()))
    digest = _hmac.new(secret.encode(), b"v0:" + stamp.encode() + b":" + body, hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": stamp, "X-Slack-Signature": f"v0={digest}", "Content-Type": "application/json"}


def _event(event_id: str = "Ev0001") -> bytes:
    import json as _json

    return _json.dumps({"type": "event_callback", "event_id": event_id, "team_id": "T0FAKE",
                        "event": {"type": "app_mention", "text": "hi"}}).encode()


@pytest.fixture
def relay_forwards(monkeypatch):
    """Capture what the relay posts to the agent instead of opening a socket."""
    from kobo_api import slack_relay

    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"accepted": True})

    monkeypatch.setattr(slack_relay, "_http_client", httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(slack_relay, "dedupe", slack_relay.EventDedupe())
    monkeypatch.setattr(slack_relay, "bucket", slack_relay.TokenBucket())
    return calls


@pytest.mark.parametrize(
    ("relay_id", "headers", "status"),
    [
        ("no-such-relay-id", "signed", 404),
        (RELAY_ID, "wrong-secret", 401),
        (RELAY_ID, "stale", 401),
        (RELAY_ID, "unsigned", 401),
    ],
    ids=["unknown-id", "wrong-secret", "stale-timestamp", "unsigned"],
)
def test_relay_refuses_without_detail(tmp_path, relay_forwards, relay_id, headers, status):
    """Authentication (relay): an unknown relay id is a bare 404 and a bad
    signature a bare 401; neither body says why and nothing is forwarded."""
    client = _relay_room(tmp_path)
    body = _event()
    sent = {
        "signed": _signed(body),
        "wrong-secret": _signed(body, secret="not-the-signing-secret"),
        "stale": _signed(body, ts=int(time.time()) - 3600),
        "unsigned": {"Content-Type": "application/json"},
    }[headers]

    response = client.post(f"/slack/relay/{relay_id}", content=body, headers=sent)

    assert response.status_code == status
    assert response.content == b""
    assert relay_forwards == []


def test_relay_keeps_an_event_through_an_agent_restart_and_drops_it_on_a_refusal(monkeypatch):
    """Wire shape (relay): an agent mid-restart answers 404 from its old pod or
    nothing at all; the relay sends the same event again on its schedule and
    stops at the first success. A refusal (a wrong relay secret, 403) is final,
    and after the whole schedule the event is dropped with one warning."""
    from kobo_api import slack_relay

    def room(answers):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            answer = answers.pop(0)
            if answer == "down":
                raise httpx.ConnectError("connection refused")
            return httpx.Response(answer, json={"accepted": answer == 200})

        naps: list[float] = []
        monkeypatch.setattr(slack_relay, "_http_client", httpx.Client(transport=httpx.MockTransport(handler)))
        monkeypatch.setattr(slack_relay, "_sleep", naps.append)
        return seen, naps

    seen, naps = room(["down", 404, 200])
    slack_relay.forward_event("http://agent.test/slack/events", "s3cret", b"{}", agent_name="rae")
    assert len(seen) == 3 and naps == [5.0, 15.0]
    assert all(r.headers["X-Kobo-Relay-Secret"] == "s3cret" for r in seen)

    seen, naps = room([403])
    slack_relay.forward_event("http://agent.test/slack/events", "s3cret", b"{}", agent_name="rae")
    assert len(seen) == 1 and naps == []

    seen, naps = room([404] * (len(slack_relay.RETRY_DELAYS_S) + 1))
    slack_relay.forward_event("http://agent.test/slack/events", "s3cret", b"{}", agent_name="rae")
    assert len(seen) == len(slack_relay.RETRY_DELAYS_S) + 1 and naps == list(slack_relay.RETRY_DELAYS_S)


def test_relay_answers_the_challenge_for_a_pending_id_then_demands_a_signature(tmp_path, relay_forwards):
    """Authentication (relay): Slack verifies the request URL during create,
    before the signing secret exists, so a pending row answers the challenge;
    once the secret is stored an unsigned challenge is refused."""
    import json as _json

    from kobo_api.models import SlackRelayApp

    client = _relay_room(tmp_path, signing_secret=None)
    challenge = _json.dumps({"type": "url_verification", "challenge": "abc123", "token": "ignored"}).encode()

    pending = client.post(f"/slack/relay/{RELAY_ID}", content=challenge, headers={"Content-Type": "application/json"})
    assert pending.status_code == 200
    assert pending.json() == {"challenge": "abc123"}

    with client.app.state.session_factory() as s:
        s.get(SlackRelayApp, RELAY_ID).signing_secret = SIGNING_SECRET
        s.commit()
    assert client.post(f"/slack/relay/{RELAY_ID}", content=challenge, headers={"Content-Type": "application/json"}).status_code == 401
    assert client.post(f"/slack/relay/{RELAY_ID}", content=challenge, headers=_signed(challenge)).json() == {"challenge": "abc123"}


def test_relay_forwards_a_signed_event_once_with_the_agent_header(tmp_path, relay_forwards):
    """Authentication (relay): a signed event is acked at once and forwarded
    to the agent's /slack/events with X-Kobo-Relay-Secret; Slack's retry of
    the same event_id is acked and not forwarded again."""
    client = _relay_room(tmp_path)
    body = _event("Ev0042")

    first = client.post(f"/slack/relay/{RELAY_ID}", content=body, headers=_signed(body))
    retry = client.post(f"/slack/relay/{RELAY_ID}", content=body, headers=_signed(body))

    assert first.json() == {"accepted": True}
    assert retry.json() == {"accepted": True}
    assert len(relay_forwards) == 1
    forwarded = relay_forwards[0]
    assert str(forwarded.url) == "http://agent-clio.default.svc.cluster.local/slack/events"
    assert forwarded.headers["X-Kobo-Relay-Secret"] == RELAY_SECRET
    assert forwarded.content == body


def test_relay_is_absent_while_the_switch_is_off(tmp_path, relay_forwards):
    """Authentication (relay): with `slack_relay` off a known relay id and the
    install callback are both a bare 404, so a room that has not turned the
    flow on exposes nothing on its public paths."""
    client = _relay_room(tmp_path, slack_relay=False)
    body = _event()

    assert client.post(f"/slack/relay/{RELAY_ID}", content=body, headers=_signed(body)).status_code == 404
    assert client.get("/slack/oauth/callback?code=x&state=y", follow_redirects=False).status_code == 404
    assert relay_forwards == []
