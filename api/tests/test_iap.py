"""Tests for kobo_api.iap (deployability plan, Task 2) — identity-proxy
assertion verification.

No mocking of the verify path: every test that needs a signed assertion
generates a real ES256 keypair (`cryptography`, via PyJWT[crypto]), exports
its public half as a real JWK (with a `kid`), and signs real JWTs against
the private half. An injected `jwks_fetcher` callable stands in for the
network call to IAP's public endpoint — that's the only fake here, and it
still hands back a real, verifiable key set.
"""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import Request
from fastapi.testclient import TestClient

from kobo_api.iap import (
    ASSERTION_HEADER,
    EXEMPT_PATHS,
    EXEMPT_PREFIXES,
    IAP_ISSUER,
    UNAUTHORIZED_BODY,
    IapError,
    IapVerifier,
    _is_exempt,
)
from kobo_api.main import create_app

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


# --- Constants: exact values from the brief -----------------------------


def test_constants_match_the_brief_exactly():
    assert IAP_ISSUER == "https://cloud.google.com/iap"
    assert ASSERTION_HEADER == "x-goog-iap-jwt-assertion"
    assert EXEMPT_PATHS == frozenset({"/api/healthz", "/metrics"})
    assert UNAUTHORIZED_BODY == {"error": "you need to sign in to use this room"}


# --- IapVerifier.verify(): unit-level, real crypto, injected fetcher ----


def test_verify_valid_token_returns_claims():
    private_key, jwk = _generate_jwk()
    verifier = IapVerifier(AUDIENCE, jwks_fetcher=_counting_fetcher({"keys": [jwk]}))
    token = _sign(private_key)

    claims = verifier.verify(token)

    assert claims["aud"] == AUDIENCE
    assert claims["iss"] == IAP_ISSUER
    assert claims["email"] == "person@example.com"


def test_verify_garbage_token_raises_iap_error():
    _, jwk = _generate_jwk()
    verifier = IapVerifier(AUDIENCE, jwks_fetcher=_counting_fetcher({"keys": [jwk]}))

    with pytest.raises(IapError):
        verifier.verify("not-a-jwt-at-all")


def test_verify_wrong_audience_raises_iap_error():
    private_key, jwk = _generate_jwk()
    verifier = IapVerifier(AUDIENCE, jwks_fetcher=_counting_fetcher({"keys": [jwk]}))
    token = _sign(private_key, aud="some-other-audience")

    with pytest.raises(IapError):
        verifier.verify(token)


def test_verify_wrong_issuer_raises_iap_error():
    private_key, jwk = _generate_jwk()
    verifier = IapVerifier(AUDIENCE, jwks_fetcher=_counting_fetcher({"keys": [jwk]}))
    token = _sign(private_key, iss="https://not-cloud.google.com/iap")

    with pytest.raises(IapError):
        verifier.verify(token)


def test_verify_expired_token_raises_iap_error():
    private_key, jwk = _generate_jwk()
    verifier = IapVerifier(AUDIENCE, jwks_fetcher=_counting_fetcher({"keys": [jwk]}))
    now = int(time.time())
    token = _sign(private_key, iat=now - 7200, exp=now - 3600)

    with pytest.raises(IapError):
        verifier.verify(token)


def test_verify_unknown_kid_after_refetch_raises_iap_error():
    # Both fetcher responses lack the signing kid entirely — a true
    # "we've never heard of this key" case, distinct from the rotation test
    # below where the second fetch *does* have it.
    private_key, jwk = _generate_jwk()
    other_jwk = dict(jwk, kid="a-totally-different-kid")
    verifier = IapVerifier(
        AUDIENCE, jwks_fetcher=_counting_fetcher({"keys": [other_jwk]})
    )
    token = _sign(private_key)

    with pytest.raises(IapError):
        verifier.verify(token)


def test_verify_error_message_never_contains_the_token():
    private_key, jwk = _generate_jwk()
    verifier = IapVerifier(AUDIENCE, jwks_fetcher=_counting_fetcher({"keys": [jwk]}))
    token = _sign(private_key, aud="some-other-audience")

    with pytest.raises(IapError) as excinfo:
        verifier.verify(token)

    assert token not in str(excinfo.value)


def test_verify_kid_rotation_refetches_once_and_succeeds():
    """The fetcher returns an EMPTY key set on the first call (the cache's
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


# --- Alg-substitution regression -----------------------------------------
#
# `verify()` pins `algorithms=["ES256"]` in the `jwt.decode` call — it never
# reads or trusts the token header's own `alg`, which an attacker fully
# controls. These three tests each forge a token that is well-formed and
# carries the REAL `kid`, but is signed (or not signed at all) under a
# different alg than ES256. If a future edit ever widened `algorithms=` or
# switched to reading `alg` from the header, these would be the tests that
# catch it — every one must still raise `IapError`.


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


def test_verify_alg_none_token_raises_iap_error():
    """An unsigned `alg: none` token — otherwise a valid-shaped payload
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
    """A token HMAC-signed with a plain shared secret (not IAP's ES256
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
    """A token signed with a freshly generated RSA key under RS256,
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


# --- Middleware: route-level via TestClient, iap_audience set -----------


@pytest.fixture
def iap_config(config):
    return config.model_copy(update={"iap_audience": AUDIENCE})


def _client_with_fetcher(session_factory, iap_config, fetcher):
    app = create_app(session_factory, config=iap_config, iap_jwks_fetcher=fetcher)

    @app.get("/probe")
    def probe(request: Request) -> dict:
        return {"email": getattr(request.state, "iap_email", None)}

    return TestClient(app)


def test_disabled_iap_audience_allows_requests_without_header(client):
    # `client` (conftest.py) is built from the plain `config` fixture, whose
    # `iap_audience` defaults to "" — disabled. Every existing test suite
    # already proves every route works with no header in this state; this is
    # the one explicit test for it.
    response = client.get("/api/healthz")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_enabled_no_header_401(session_factory, iap_config):
    _, jwk = _generate_jwk()
    test_client = _client_with_fetcher(
        session_factory, iap_config, _counting_fetcher({"keys": [jwk]})
    )

    response = test_client.get("/api/agents")

    assert response.status_code == 401
    assert response.json() == UNAUTHORIZED_BODY


def test_enabled_garbage_token_401(session_factory, iap_config):
    _, jwk = _generate_jwk()
    test_client = _client_with_fetcher(
        session_factory, iap_config, _counting_fetcher({"keys": [jwk]})
    )

    response = test_client.get(
        "/api/agents", headers={ASSERTION_HEADER: "definitely-not-a-jwt"}
    )

    assert response.status_code == 401
    assert response.json() == UNAUTHORIZED_BODY


def test_enabled_wrong_audience_401(session_factory, iap_config):
    private_key, jwk = _generate_jwk()
    test_client = _client_with_fetcher(
        session_factory, iap_config, _counting_fetcher({"keys": [jwk]})
    )
    token = _sign(private_key, aud="wrong-audience")

    response = test_client.get("/api/agents", headers={ASSERTION_HEADER: token})

    assert response.status_code == 401
    assert response.json() == UNAUTHORIZED_BODY


def test_enabled_wrong_issuer_401(session_factory, iap_config):
    private_key, jwk = _generate_jwk()
    test_client = _client_with_fetcher(
        session_factory, iap_config, _counting_fetcher({"keys": [jwk]})
    )
    token = _sign(private_key, iss="https://evil.example/iap")

    response = test_client.get("/api/agents", headers={ASSERTION_HEADER: token})

    assert response.status_code == 401
    assert response.json() == UNAUTHORIZED_BODY


def test_enabled_expired_token_401(session_factory, iap_config):
    private_key, jwk = _generate_jwk()
    test_client = _client_with_fetcher(
        session_factory, iap_config, _counting_fetcher({"keys": [jwk]})
    )
    now = int(time.time())
    token = _sign(private_key, iat=now - 7200, exp=now - 3600)

    response = test_client.get("/api/agents", headers={ASSERTION_HEADER: token})

    assert response.status_code == 401
    assert response.json() == UNAUTHORIZED_BODY


def test_enabled_valid_token_200_and_iap_email_set(session_factory, iap_config):
    private_key, jwk = _generate_jwk()
    test_client = _client_with_fetcher(
        session_factory, iap_config, _counting_fetcher({"keys": [jwk]})
    )
    token = _sign(private_key, email="person@example.com")

    response = test_client.get("/probe", headers={ASSERTION_HEADER: token})

    assert response.status_code == 200
    assert response.json() == {"email": "person@example.com"}


def test_enabled_valid_token_reaches_a_real_api_route(session_factory, iap_config):
    # Same as above, but through a real /api/* route (not just the test
    # probe) — proves the middleware doesn't only work for the probe's
    # exact wiring.
    private_key, jwk = _generate_jwk()
    test_client = _client_with_fetcher(
        session_factory, iap_config, _counting_fetcher({"keys": [jwk]})
    )
    token = _sign(private_key)

    response = test_client.get("/api/agents", headers={ASSERTION_HEADER: token})

    assert response.status_code == 200
    assert response.json() == {"agents": [], "groups_unavailable": False}


def test_enabled_no_header_401_teams_route(session_factory, iap_config):
    # GET /api/teams (persona-metadata plan Task 1) gets no gate beyond the
    # app-wide IAP middleware every other /api/* route already has — same
    # idiom as test_enabled_no_header_401 above, against the new route.
    _, jwk = _generate_jwk()
    test_client = _client_with_fetcher(
        session_factory, iap_config, _counting_fetcher({"keys": [jwk]})
    )

    response = test_client.get("/api/teams")

    assert response.status_code == 401
    assert response.json() == UNAUTHORIZED_BODY


def test_enabled_healthz_exempt_with_no_header(session_factory, iap_config):
    _, jwk = _generate_jwk()
    test_client = _client_with_fetcher(
        session_factory, iap_config, _counting_fetcher({"keys": [jwk]})
    )

    response = test_client.get("/api/healthz")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_enabled_metrics_exempt_with_no_header(session_factory, iap_config):
    # The in-cluster scraper carries no IAP assertion (it reaches the pod's
    # service directly, below the ingress) - /metrics must answer anyway.
    _, jwk = _generate_jwk()
    test_client = _client_with_fetcher(
        session_factory, iap_config, _counting_fetcher({"keys": [jwk]})
    )

    response = test_client.get("/metrics", follow_redirects=True)

    assert response.status_code == 200
    assert "http_request_duration_seconds" in response.text


def _make_minimal_dist(tmp_path):
    """A minimal Vite-shaped `dist/` dir, same shape as test_static_ui.py's
    own `make_dist` helper."""
    dist_dir = tmp_path / "dist"
    assets_dir = dist_dir / "assets"
    assets_dir.mkdir(parents=True)
    (dist_dir / "index.html").write_text("<html>SPA-MARKER</html>")
    (assets_dir / "app.js").write_text("console.log('app');")
    return dist_dir


def test_enabled_ui_dist_paths_require_the_header_too(session_factory, iap_config, tmp_path):
    """The coverage rule is EVERYTHING except EXEMPT_PATHS — explicitly
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


# --- MCP-surface IAP exemption (MCP server arc rollout) --------------------
# The /mcp, /oauth/*, /.well-known/* roots carry their OWN auth (kobo's OAuth
# 2.1 server + the MCP bearer-token verifier), served off a no-IAP ingress
# backend. The app-wide IAP middleware must let them through, or a real MCP
# client -- arriving with no IAP assertion by design -- is 401'd by kobo
# itself before routing. Boundary-matched so a lookalike root is NOT exempt.


def test_is_exempt_covers_the_mcp_roots_and_healthz_but_not_lookalikes():
    assert EXEMPT_PREFIXES == ("/mcp", "/oauth", "/.well-known", "/broker")
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


def test_enabled_mcp_roots_are_not_401_without_a_header(session_factory, iap_config):
    """With IAP enabled and NO assertion header, the MCP-surface roots pass
    the middleware (reach routing) instead of 401ing. MCP is flag-off in this
    config, so they 404 at routing -- the point is they are not blocked by the
    identity proxy, unlike every /api route."""
    _, jwk = _generate_jwk()
    test_client = _client_with_fetcher(
        session_factory, iap_config, _counting_fetcher({"keys": [jwk]})
    )

    for root in (
        "/oauth/authorize",
        "/mcp",
        "/.well-known/oauth-authorization-server",
    ):
        resp = test_client.get(root)
        assert resp.status_code != 401, f"{root} was 401'd by IAP"


def test_enabled_mcp_lookalike_roots_still_401(session_factory, iap_config):
    """The boundary discipline end to end: a path that merely starts with an
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
    """Defense in depth (IAP-exemption review): a percent-decoded traversal
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
