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
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from kobo_api.iap import (
    ASSERTION_HEADER,
    EXEMPT_PATHS,
    EXEMPT_PREFIXES,
    FORWARDED_PROTO_HEADER,
    HTTPS_ONLY_PREFIXES,
    HTTPS_REQUIRED_BODY,
    IAP_ISSUER,
    UNAUTHORIZED_BODY,
    HttpsOnlyMiddleware,
    IapError,
    IapMiddleware,
    IapVerifier,
    _is_exempt,
    _requires_https,
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


# --- https-only OAuth surface (Task 2c, CYBER-1132 ride-along) --------------
# The IAP-exempt EXTERNAL roots (/oauth, /.well-known, /mcp) are reachable
# from the internet off the no-IAP ingress backend, and answered over plain
# http:// at the load balancer. `HttpsOnlyMiddleware` is the app-level half of
# the fix (the LB-level FrontendConfig redirectToHttps is a kc.yaml change):
# X-Forwarded-Proto is the discriminator -- the GCLB always sets it, and
# in-cluster direct-Service callers (the /broker lane the ledger and agents
# use, kubelet probes, the metrics scraper) never do. ABSENT means internal
# and is always left untouched, so the plain-http /broker lane is unharmed.
#
# Every test below runs with follow_redirects=False on purpose: a real client
# re-requests over https and the LB then sets X-Forwarded-Proto: https, but a
# test client replays the pinned http header on the redirect and would loop.

HTTP_PROTO = {FORWARDED_PROTO_HEADER: "http"}
HTTPS_PROTO = {FORWARDED_PROTO_HEADER: "https"}


def test_https_only_constants_match_the_brief_exactly():
    assert FORWARDED_PROTO_HEADER == "x-forwarded-proto"
    assert HTTPS_REQUIRED_BODY == {"error": "this endpoint requires https"}
    # Exactly the three EXTERNAL roots -- deliberately NOT derived from
    # EXEMPT_PREFIXES, so a future IAP exemption for another in-cluster lane
    # can never silently enroll itself into https enforcement.
    assert HTTPS_ONLY_PREFIXES == ("/mcp", "/oauth", "/.well-known")
    assert "/broker" not in HTTPS_ONLY_PREFIXES
    assert set(HTTPS_ONLY_PREFIXES) < set(EXEMPT_PREFIXES)


def test_requires_https_matches_the_external_roots_on_segment_boundaries():
    """Same path-matching discipline `_is_exempt` holds: normalize first,
    then match exactly or on a segment boundary."""
    for external in (
        "/mcp",
        "/mcp/",
        "/oauth",
        "/oauth/authorize",
        "/oauth/token",
        "/oauth/google/callback",
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource",
    ):
        assert _requires_https(external) is True, external
    for untouched in (
        # the in-cluster lanes: never enforced, they legitimately speak http
        "/broker",
        "/broker/agents/wren/github-token",
        "/metrics",
        "/api/healthz",
        # not our scope to widen -- IAP fronting owns these
        "/api/agents",
        "/",
        # lookalikes, off a segment boundary
        "/mcpevil",
        "/oauthx",
        "/oauth-something",
        "/.well-knownx",
    ):
        assert _requires_https(untouched) is False, untouched
    # a traversal that borrows an external root's prefix but resolves into
    # /api is NOT the oauth surface; one that resolves INTO it is
    assert _requires_https("/oauth/../api/agents") is False
    assert _requires_https("//mcp/../api/agents") is False
    assert _requires_https("/api/agents/../../oauth/token") is True


def test_plain_http_get_on_the_oauth_surface_is_308_with_path_and_query_kept(
    session_factory, iap_config
):
    """(a) GET over plain http on an external root: 308 to the identical URL
    with the scheme swapped -- host, path and query all preserved."""
    _, jwk = _generate_jwk()
    test_client = _client_with_fetcher(
        session_factory, iap_config, _counting_fetcher({"keys": [jwk]})
    )

    resp = test_client.get(
        "/oauth/authorize?response_type=code&client_id=abc&state=xyz%20z",
        headers=HTTP_PROTO,
        follow_redirects=False,
    )

    assert resp.status_code == 308
    assert resp.headers["location"] == (
        "https://testserver/oauth/authorize"
        "?response_type=code&client_id=abc&state=xyz%20z"
    )


def test_plain_http_get_is_308_on_every_external_root(session_factory, iap_config):
    _, jwk = _generate_jwk()
    test_client = _client_with_fetcher(
        session_factory, iap_config, _counting_fetcher({"keys": [jwk]})
    )

    for root in (
        "/oauth/authorize",
        "/oauth/google/callback",
        "/mcp",
        "/mcp/",
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource",
    ):
        resp = test_client.get(root, headers=HTTP_PROTO, follow_redirects=False)
        assert resp.status_code == 308, root
        assert resp.headers["location"] == f"https://testserver{root}", root


def test_plain_http_head_on_the_oauth_surface_is_308(session_factory, iap_config):
    _, jwk = _generate_jwk()
    test_client = _client_with_fetcher(
        session_factory, iap_config, _counting_fetcher({"keys": [jwk]})
    )

    resp = test_client.head(
        "/oauth/authorize", headers=HTTP_PROTO, follow_redirects=False
    )

    assert resp.status_code == 308
    assert resp.headers["location"] == "https://testserver/oauth/authorize"


def test_plain_http_post_on_the_oauth_surface_is_400_never_redirected(
    session_factory, iap_config
):
    """(b) A token POST's credentials must never transit plain http, and a
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


def test_plain_http_multi_hop_proto_takes_the_client_facing_value(
    session_factory, iap_config
):
    """X-Forwarded-Proto accumulates left-to-right, oldest first: the FIRST
    value is the protocol the client actually spoke. `http, https` means the
    client arrived over plain http, so it is still refused."""
    _, jwk = _generate_jwk()
    test_client = _client_with_fetcher(
        session_factory, iap_config, _counting_fetcher({"keys": [jwk]})
    )

    resp = test_client.get(
        "/oauth/authorize",
        headers={FORWARDED_PROTO_HEADER: "http, https"},
        follow_redirects=False,
    )

    assert resp.status_code == 308
    assert resp.headers["location"] == "https://testserver/oauth/authorize"


def test_broker_lane_is_untouched_by_the_https_guard(session_factory, iap_config):
    """(c) The in-cluster /broker lane must keep working over plain http even
    if some future proxy does set the header on it: never 308'd, never 400'd
    with the https copy. (These reach their real routes and fail their own
    agent-JWT check -- the point is WHICH gate answered.)"""
    _, jwk = _generate_jwk()
    test_client = _client_with_fetcher(
        session_factory, iap_config, _counting_fetcher({"keys": [jwk]})
    )

    for method, path in (
        ("get", "/broker/agents/wren/jira/search?q=x"),
        ("post", "/broker/agents/wren/github-token"),
    ):
        for headers in ({}, HTTP_PROTO, HTTPS_PROTO):
            resp = getattr(test_client, method)(
                path, headers=headers, follow_redirects=False
            )
            assert resp.status_code != 308, f"{method} {path} {headers}"
            assert resp.json() != HTTPS_REQUIRED_BODY, f"{method} {path} {headers}"


def test_non_external_paths_over_plain_http_are_untouched(session_factory, iap_config):
    """Scope discipline: IAP fronting already owns /api and the scraper owns
    /metrics. Plain http there is not this guard's business -- /api still
    answers with the IAP 401, /metrics still 200s."""
    _, jwk = _generate_jwk()
    test_client = _client_with_fetcher(
        session_factory, iap_config, _counting_fetcher({"keys": [jwk]})
    )

    api = test_client.get("/api/agents", headers=HTTP_PROTO, follow_redirects=False)
    assert api.status_code == 401
    assert api.json() == UNAUTHORIZED_BODY

    metrics = test_client.get("/metrics", headers=HTTP_PROTO, follow_redirects=False)
    assert metrics.status_code == 200


def test_absent_forwarded_proto_passes_through_untouched(session_factory, iap_config):
    """(d) No header at all means an in-cluster caller reaching the pod's
    Service directly. Pass through: this is how the MCP roots behaved before
    the guard existed (MCP flag-off here, so they 404 at routing)."""
    _, jwk = _generate_jwk()
    test_client = _client_with_fetcher(
        session_factory, iap_config, _counting_fetcher({"keys": [jwk]})
    )

    for root in ("/oauth/authorize", "/mcp", "/.well-known/oauth-protected-resource"):
        resp = test_client.get(root, follow_redirects=False)
        assert resp.status_code == 404, root

    posted = test_client.post("/oauth/token", follow_redirects=False)
    assert posted.status_code == 404
    assert posted.json() != HTTPS_REQUIRED_BODY


def test_forwarded_proto_https_passes_through_untouched(session_factory, iap_config):
    """(e) Already https: the guard has nothing to do, for any method."""
    _, jwk = _generate_jwk()
    test_client = _client_with_fetcher(
        session_factory, iap_config, _counting_fetcher({"keys": [jwk]})
    )

    for root in ("/oauth/authorize", "/mcp", "/.well-known/oauth-protected-resource"):
        resp = test_client.get(root, headers=HTTPS_PROTO, follow_redirects=False)
        assert resp.status_code == 404, root

    posted = test_client.post("/oauth/token", headers=HTTPS_PROTO, follow_redirects=False)
    assert posted.status_code == 404
    assert posted.json() != HTTPS_REQUIRED_BODY


def test_https_guard_runs_with_iap_disabled_too(client):
    """The guard is registered unconditionally, independent of
    `Config.iap_audience` -- the external roots are served off the NO-IAP
    ingress backend, so an IAP-less room is exactly the room that needs it.
    `client` (conftest.py) is built from the plain `config` fixture, whose
    `iap_audience` defaults to "" -- disabled."""
    redirected = client.get(
        "/oauth/authorize?client_id=abc", headers=HTTP_PROTO, follow_redirects=False
    )
    assert redirected.status_code == 308
    assert redirected.headers["location"] == (
        "https://testserver/oauth/authorize?client_id=abc"
    )

    refused = client.post("/oauth/token", headers=HTTP_PROTO, follow_redirects=False)
    assert refused.status_code == 400
    assert refused.json() == HTTPS_REQUIRED_BODY

    # and the in-cluster lanes stay plain-http-clean here too
    assert client.get("/api/healthz", headers=HTTP_PROTO).status_code == 200
    broker = client.post(
        "/broker/agents/wren/github-token", headers=HTTP_PROTO, follow_redirects=False
    )
    assert broker.status_code != 308
    assert broker.json() != HTTPS_REQUIRED_BODY


def test_https_guard_is_installed_outside_the_iap_gate(session_factory, iap_config):
    """The ordering create_app relies on, pinned structurally because it is
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
    """(f) The brief's inspection point. FINDING: the discovery documents do
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
            oauth_client_id="mcp-test-client",
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
