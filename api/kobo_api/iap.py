"""Identity-Aware Proxy assertion verification (deployability plan, Task 2).

When kobo is deployed behind Google's Identity-Aware Proxy, IAP forwards a
signed JWT on every request in the `x-goog-iap-jwt-assertion` header. This
module is the app-side guarantee that a request actually passed through that
proxy — not a misconfigured load balancer, not a direct-to-pod request, and
not a client that simply forged the header — before it ever reaches a route.
See https://cloud.google.com/iap/docs/signed-headers-howto for the header
IAP adds and the JWT it carries.

Fails closed: `IapMiddleware` (registered by `create_app` in
`kobo_api.main` only when `Config.iap_audience` is non-empty) rejects any
request without a valid assertion with a 401, for EVERY path except
`EXEMPT_PATHS` — including the built UI's static assets and SPA fallback
(Task 1's `mount_ui`), since the proxy sits in front of the whole app, not
just `/api/*`.
"""

from __future__ import annotations

import logging
import posixpath
import threading
import time
from collections.abc import Callable

import httpx
import jwt
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

log = logging.getLogger(__name__)

IAP_ISSUER = "https://cloud.google.com/iap"
IAP_JWKS_URL = "https://www.gstatic.com/iap/verify/public_key-jwk"
ASSERTION_HEADER = "x-goog-iap-jwt-assertion"
EXEMPT_PATHS = frozenset({"/api/healthz"})

# Path ROOTS the app-wide IAP assertion check never applies to, because they
# carry their OWN authentication (the MCP server arc): kobo's OAuth 2.1 server
# (/oauth/*, Google-OIDC + Workspace-domain + PKCE) and the MCP surface it
# guards (/mcp, bearer-token-verified per call), plus the public OAuth
# discovery documents (/.well-known/*, unauthenticated by RFC). The deploy's
# ingress split serves these roots off a no-IAP backend, so a real MCP client
# reaches the pod with NO IAP assertion by design -- without this exemption
# the app's own middleware would 401 them before routing, defeating the split.
# Safe when the MCP server is flag-OFF too: those roots then have no routes,
# so an exempted request simply 404s (leaks nothing) instead of 401ing.
# /broker (agent PR broker, 2026-08-27 spec) carries its own auth the same
# way: the route verifies the calling AGENT's cabal-issued JWT itself, and
# its callers are in-cluster agents reaching the pod's service directly --
# the external ingress never routes this root at all.
# Matched on a segment boundary (exact, or prefix + "/") so "/oauth" never
# exempts a lookalike like "/oauthx" -- the same boundary discipline the
# OAuth redirect-URI allowlist uses.
EXEMPT_PREFIXES = ("/mcp", "/oauth", "/.well-known", "/broker")


def _normalize(path: str) -> str:
    """Canonicalize before any exemption compare: collapse a leading run of
    slashes to one, then resolve `.` / `..` segments (`posixpath.normpath`).
    Without this, a percent-decoded traversal like `/mcp/..%2f..%2fapi/agents`
    -- which reaches the middleware as the literal, un-collapsed string
    `/mcp/../../api/agents` -- would pass a raw `startswith("/mcp/")` and be
    treated as exempt. Normalizing turns it into `/api/agents` so the prefix
    check sees the real target, making exemption independent of routing
    topology (defense in depth, per the arc's IAP-exemption review). normpath
    strips a trailing slash, so `/mcp/` canonicalizes to `/mcp`, still exempt
    by the exact-equality arm below."""
    collapsed = "/" + path.lstrip("/")
    return posixpath.normpath(collapsed)


def _is_exempt(path: str) -> bool:
    path = _normalize(path)
    if path in EXEMPT_PATHS:
        return True
    return any(path == p or path.startswith(p + "/") for p in EXEMPT_PREFIXES)

# IAP rotates its signing keys infrequently; caching for this long avoids a
# JWKS round trip on every request while still picking up a rotation well
# inside any plausible rotation window. A `kid` miss (the key genuinely
# rotated since the last fetch) is handled separately, below, by an
# immediate one-shot refetch rather than waiting out this TTL.
_JWKS_CACHE_TTL_S = 12 * 60 * 60

# Plain-language 401 body, verbatim — never any detail about *why* the
# assertion was rejected (missing header vs. bad signature vs. expired all
# look the same from outside).
UNAUTHORIZED_BODY = {"error": "you need to sign in to use this room"}


class IapError(RuntimeError):
    """The assertion is missing, malformed, or fails verification.

    The message is always a short, static, human-safe sentence — it must
    NEVER include the token text (or any fragment of it), since this
    exception's message may be logged.
    """


def _default_jwks_fetcher() -> dict:
    """GET IAP's public JWKS over the network. A JWKS fetch is a small, fast
    call — 10s is generous, not a budget users should ever wait out before
    finding out whether they're allowed in."""
    response = httpx.get(IAP_JWKS_URL, timeout=10.0)
    response.raise_for_status()
    return response.json()


class IapVerifier:
    """Verifies a `x-goog-iap-jwt-assertion` token: signature (ES256 only,
    pinned — never taken from the token's own `alg` header, which an
    attacker fully controls), `aud` == `audience`, `iss` == `IAP_ISSUER`,
    and that `exp`/`iat` are present.

    `jwks_fetcher` defaults to `_default_jwks_fetcher` (a real network call
    to Google's public endpoint); tests inject one that returns a real,
    locally-generated JWK set instead, so the verify path below always runs
    against genuine cryptography, never a mock.
    """

    def __init__(self, audience: str, jwks_fetcher: Callable[[], dict] | None = None):
        self._audience = audience
        self._fetch = jwks_fetcher or _default_jwks_fetcher
        self._lock = threading.Lock()
        self._keys: dict[str, dict] = {}
        self._fetched_at: float | None = None

    def _refresh_locked(self) -> None:
        """Unconditionally re-fetch and replace the cached kid->JWK map.
        Caller must hold `self._lock`. A malformed/unreachable JWKS response
        propagates to the caller as-is — `verify` below is the one place
        that turns it into an `IapError`."""
        jwks = self._fetch()
        self._keys = {
            key["kid"]: key for key in jwks.get("keys", []) if key.get("kid")
        }
        self._fetched_at = time.monotonic()

    def _key_for(self, kid: str) -> dict | None:
        """The JWK for `kid`. Refreshes the cache first if it's stale (past
        `_JWKS_CACHE_TTL_S`) or has never been populated. If `kid` still
        isn't found after that lookup, refetches ONCE more before giving up
        — an unknown kid on an otherwise-fresh cache means a key rotation,
        not a cache-expiry problem, and deserves its own unconditional
        refetch rather than waiting out the TTL.
        """
        with self._lock:
            stale = (
                self._fetched_at is None
                or time.monotonic() - self._fetched_at >= _JWKS_CACHE_TTL_S
            )
            if stale:
                self._refresh_locked()

            key = self._keys.get(kid)
            if key is None:
                self._refresh_locked()
                key = self._keys.get(kid)
            return key

    def verify(self, token: str) -> dict:
        """Return the verified claims, or raise `IapError`. Never raises
        anything else — every failure mode (bad header, unreachable JWKS,
        bad signature, wrong audience/issuer, expired, missing claims) is
        normalized to `IapError` with a plain, token-free message; full
        exception detail goes to the log only, never into the message a
        caller might surface to an HTTP response.
        """
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as exc:
            log.info("iap: malformed assertion header: %s", exc)
            raise IapError("malformed assertion") from exc

        kid = header.get("kid")
        if not kid:
            log.info("iap: assertion header has no kid")
            raise IapError("assertion missing key id")

        try:
            jwk = self._key_for(kid)
        except Exception as exc:  # JWKS fetch/parse failure — never the token
            log.warning("iap: jwks fetch failed: %s", exc)
            raise IapError("could not verify assertion") from exc

        if jwk is None:
            log.info("iap: no signing key found for kid %s", kid)
            raise IapError("unknown signing key")

        try:
            pyjwk = jwt.PyJWK.from_dict(jwk)
        except Exception as exc:  # broad: PyJWKError isn't an InvalidKeyError
            log.warning("iap: malformed jwk for kid %s: %s", kid, exc)
            raise IapError("could not verify assertion") from exc

        try:
            return jwt.decode(
                token,
                key=pyjwk.key,
                algorithms=["ES256"],
                audience=self._audience,
                issuer=IAP_ISSUER,
                options={"require": ["exp", "iat"]},
            )
        except jwt.InvalidTokenError as exc:
            log.info("iap: assertion failed verification: %s", exc)
            raise IapError("invalid assertion") from exc


class IapMiddleware(BaseHTTPMiddleware):
    """Rejects, with a 401, any request whose path isn't exempt (`_is_exempt`:
    `EXEMPT_PATHS` exactly, or under an `EXEMPT_PREFIXES` root that carries its
    own auth) and doesn't carry a valid `ASSERTION_HEADER` — registered by `create_app`
    (`kobo_api.main`) only when `Config.iap_audience` is non-empty. On
    success, stashes the verified `email` claim at `request.state.iap_email`
    for downstream use (future team mapping).

    Coverage caveat: `BaseHTTPMiddleware` only sees ASGI `http` scopes. Any
    future `@app.websocket(...)` route runs over the `websocket` scope and
    would NOT pass through this middleware at all — it would need its own
    IAP verification. No websocket routes exist today, but this is not
    something to discover the hard way later.
    """

    def __init__(self, app: ASGIApp, *, verifier: IapVerifier):
        super().__init__(app)
        self._verifier = verifier

    async def dispatch(self, request: Request, call_next):
        if _is_exempt(request.url.path):
            return await call_next(request)

        token = request.headers.get(ASSERTION_HEADER)
        if not token:
            return JSONResponse(UNAUTHORIZED_BODY, status_code=401)

        try:
            claims = self._verifier.verify(token)
        except IapError as exc:
            log.info("iap: rejected request to %s: %s", request.url.path, exc)
            return JSONResponse(UNAUTHORIZED_BODY, status_code=401)

        request.state.iap_email = claims.get("email")
        return await call_next(request)
