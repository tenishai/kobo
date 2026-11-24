"""kobo's OAuth 2.1 Authorization Server (MCP server arc, Task 4): the
`/oauth/*` and `/.well-known/*` routes that turn a Google-verified
Workspace identity into the rotating access/refresh token pairs Tasks
2-3 built (`kobo_api.mcp.jwt`, `kobo_api.mcp.refresh_tokens`). The
2026-08-22 MCP server design's "Identity and auth" section is the flow
narrative this module implements.

**Flow** (public client, PKCE S256, one pre-registered `client_id`):

1. `GET /oauth/authorize` -- validates `client_id`/`redirect_uri`/PKCE,
   mints a short-lived `state` JWT carrying the client's own
   `redirect_uri`, PKCE `code_challenge`, and opaque `state` param, then
   302s to Google.
2. `GET /oauth/google/callback` -- verifies the `state` JWT, exchanges
   the Google `code` via the injectable `GoogleIdentity` seam, hard-
   checks the verified ID token's `hd` claim against
   `config.mcp.workspace_domain` (the `hd` *hint* sent in step 1 is never
   itself trusted -- only the value Google's own signed ID token
   attests to is), mints an `authorization_code` JWT, and redirects back
   to the client's own `redirect_uri`.
3. `POST /oauth/token`, `grant_type=authorization_code` -- verifies the
   code JWT and PKCE (`hmac.compare_digest`, never `==`), then mints an
   access token and starts a brand-new refresh-token family
   (`kobo_api.mcp.refresh_tokens.create_family`).
4. `POST /oauth/token`, `grant_type=refresh_token` -- rotates via
   `kobo_api.mcp.refresh_tokens.consume`; `None` means `invalid_grant`
   (`consume` has already revoked the family internally if this was a
   replay).

**jti vs rtid (read this before touching a refresh token).**
`kobo_api.mcp.jwt.mint` generates its own `jti` claim internally and
*refuses* a caller-supplied `jti` kwarg (a smuggling guard -- see that
module's `_RESERVED_CLAIMS`). But `kobo_api.mcp.refresh_tokens.
create_family`/`consume` need to hand a specific id back to the caller
for that exact id to be the one persisted and later looked up again
(`consume`'s return value is a jti STRING it has already inserted as a
new row's primary key). Since the refresh JWT can't carry that value as
its own `jti` claim, it rides as a separate extra claim: `rtid` (refresh
token id -- the family-store row id, i.e. `MCPRefreshToken.jti`). A
refresh JWT's own `jti` claim is therefore cosmetic here -- unique per
token, never looked up, never read by this module after mint. Its
`rtid` claim is the ONLY thing ever handed to `consume()`. Every mint of
a refresh token in this module sets `rtid` explicitly; every verify of
one reads `claims["rtid"]`, never `claims["jti"]`, before calling
`consume`.

**Google seam.** `GoogleIdentity` is a two-method `Protocol`
(`auth_url`, `exchange_and_verify`). The real implementation,
`_GoogleOIDC`, is constructed lazily per request from `config.mcp` when
`app.state.mcp_google` isn't set on the FastAPI app; tests inject a fake
there instead, so the whole suite runs with no network dependency on
Google's own endpoints. `_GoogleOIDC` reads the OAuth client secret from
the env var NAMED by `config.mcp.oauth_client_secret_env` at call time
(never cached, never held past the one call that needs it), and
verifies Google's `id_token` against Google's own JWKS via a
module-level-cached `PyJWKClient` (one client for the process, since
Google's JWKS endpoint and keys don't change per request).

**Issuer.** `config.mcp.issuer_base_url` (added by this task; see its
own docstring on `kobo_api.config.MCPConfig` for why it's a new field
rather than reusing `Config.api_base_url`) is this room's externally-
reachable base URL, used to build the well-known documents' absolute
endpoint URLs and the `redirect_uri` kobo registers with Google for its
own callback. Empty (the default, matching an org that hasn't
configured it yet) means the well-known documents fall back to relative
endpoint paths and the Google callback `redirect_uri` falls back to the
incoming request's own base URL -- both fine for local dev/tests,
neither sufficient for a real deployment.

**Redirect allowlist.** Exactly `https://claude.ai/api/mcp/auth_callback`,
or any URL whose host is `localhost`/`127.0.0.1` (any port, http or
https). Checked at `/oauth/authorize` AND again at `/oauth/token`: the
value travels inside the signed `state` -> `authorization_code` JWT
chain (so it can't be forged in transit), but the token endpoint
re-validates the embedded value against the allowlist independently
rather than simply trusting a value that already passed through a JWT
once -- defense in depth against this module's own future edits, not
against the client.

**Sync routes, real house style.** Every route here is a plain `def`,
matching `kobo_api.main`'s own routes -- FastAPI runs sync path
functions in a threadpool, so the one network hop this module ever
makes (`_GoogleOIDC.exchange_and_verify`'s call to Google) uses a plain
`httpx.Client` context manager rather than an async client.

**Session dependency.** `get_session` below is a deliberate near-
duplicate of `kobo_api.main.get_session` (same body: read
`request.app.state.session_factory`, yield a session, close it) rather
than an import from `kobo_api.main` -- Task 9 wires this router INTO
`main.py`, and an import in the other direction here would create a
circular import the moment that happens. Both read the same
`app.state.session_factory` any real `create_app` sets, so the behavior
is identical either way.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import uuid
from collections.abc import Iterator
from typing import Protocol
from urllib.parse import urlencode, urlparse

import httpx
import jwt as pyjwt
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from jwt import PyJWKClient
from pydantic import BaseModel
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from kobo_api.config import Config, MCPConfig
from kobo_api.mcp.jwt import (
    ACCESS_TTL_S,
    CODE_TTL_S,
    REFRESH_TTL_S,
    STATE_TTL_S,
    TokenError,
    mint,
    signing_key,
    verify,
)
from kobo_api.mcp.refresh_tokens import consume, create_family, redeem_code

log = logging.getLogger(__name__)

router = APIRouter()

# --- Redirect allowlist (ruling: checked at /oauth/authorize AND again at
# /oauth/token) --------------------------------------------------------

_CLAUDE_REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"


def _redirect_uri_allowed(uri: str) -> bool:
    if not uri:
        return False
    if uri == _CLAUDE_REDIRECT_URI:
        return True
    try:
        parsed = urlparse(uri)
    except ValueError:
        return False
    # Restricted to http/https (fix round 1, defense in depth): hostname
    # alone isn't enough -- a file:// or custom-scheme URI can set its host
    # to "localhost" too, and that must not ride this branch. Matches this
    # module's own docstring above, which already promises "any port, http
    # or https" for this branch.
    return parsed.scheme in ("http", "https") and parsed.hostname in (
        "localhost",
        "127.0.0.1",
    )


# --- PKCE (S256 only) -------------------------------------------------------


def _pkce_matches(verifier: str, challenge: str) -> bool:
    if not verifier or not challenge:
        return False
    computed = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return hmac.compare_digest(computed, challenge)


# --- Signing key --------------------------------------------------------


def _key(cfg: Config) -> bytes:
    """The MCP signing key named by `cfg.mcp.jwt_key_env`, so an org that
    overlays a non-default env var name is honored everywhere this module
    mints or verifies a token."""
    return signing_key(cfg.mcp.jwt_key_env)


# --- Session dependency (see module docstring for why this isn't an
# import from kobo_api.main) ----------------------------------------------


def get_session(request: Request) -> Iterator[Session]:
    session_factory: sessionmaker[Session] = request.app.state.session_factory
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


# --- Google identity seam ---------------------------------------------------


class GoogleIdentity(Protocol):
    """The one seam this module has onto Google: build the URL that sends
    a browser to Google's consent screen, and turn a Google authorization
    `code` into verified ID-token claims. Real impl below (`_GoogleOIDC`);
    tests inject a fake via `app.state.mcp_google`."""

    def auth_url(self, *, state_jwt: str, redirect_uri: str) -> str: ...

    def exchange_and_verify(self, *, code: str, redirect_uri: str) -> dict: ...


class GoogleIdentityError(RuntimeError):
    """`exchange_and_verify` failed: token exchange, network, JWKS lookup,
    signature verification, or an unexpected `iss`. The callback route
    turns this into a 502 rather than letting a raw exception surface as
    an unhandled 500 -- Google being unreachable (or a real attack
    reusing a stale JWKS) is an operational fact, not a bug in this
    module."""


_GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
_GOOGLE_ISSUERS = frozenset({"accounts.google.com", "https://accounts.google.com"})

# Cached at module level (ruling: "cache the client at module level") --
# PyJWKClient does its own internal JWKS caching/refresh-on-unknown-kid, so
# one instance for the process is exactly right, same posture as
# `kobo_api.iap.IapVerifier`'s own JWKS cache, just via PyJWT's own client
# instead of a hand-rolled one.
_jwks_client: PyJWKClient | None = None


def _get_jwks_client() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = PyJWKClient(_GOOGLE_JWKS_URL)
    return _jwks_client


class _GoogleOIDC:
    """Real `GoogleIdentity`: httpx + Google's token endpoint + Google's
    JWKS. Constructed lazily per request (see `_google_identity` below)
    from `Config.mcp` alone -- it holds no long-lived network resources of
    its own beyond the module-level JWKS client cache above."""

    def __init__(self, config: MCPConfig):
        self._config = config

    def auth_url(self, *, state_jwt: str, redirect_uri: str) -> str:
        params = {
            "client_id": self._config.oauth_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email",
            "state": state_jwt,
        }
        if self._config.workspace_domain:
            # A hint only -- the callback hard-checks the verified ID
            # token's own `hd` claim, never this request-side value.
            params["hd"] = self._config.workspace_domain
        return f"{_GOOGLE_AUTH_ENDPOINT}?{urlencode(params)}"

    def exchange_and_verify(self, *, code: str, redirect_uri: str) -> dict:
        client_secret = os.environ.get(self._config.oauth_client_secret_env, "")
        try:
            with httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
                response = client.post(
                    _GOOGLE_TOKEN_ENDPOINT,
                    data={
                        "code": code,
                        "client_id": self._config.oauth_client_id,
                        "client_secret": client_secret,
                        "redirect_uri": redirect_uri,
                        "grant_type": "authorization_code",
                    },
                )
                response.raise_for_status()
            id_token = response.json()["id_token"]

            signing_key_obj = _get_jwks_client().get_signing_key_from_jwt(id_token)
            claims = pyjwt.decode(
                id_token,
                key=signing_key_obj.key,
                algorithms=["RS256"],
                audience=self._config.oauth_client_id,
                options={"require": ["exp", "iat"]},
            )
        except Exception as exc:  # network, bad JSON, bad JWKS, bad signature...
            log.warning("mcp oauth: google token exchange/verify failed: %s", exc)
            raise GoogleIdentityError(
                "google token exchange or id_token verification failed"
            ) from exc

        if claims.get("iss") not in _GOOGLE_ISSUERS:
            raise GoogleIdentityError("unexpected issuer in google id_token")

        return claims


def _google_identity(request: Request) -> GoogleIdentity:
    existing = getattr(request.app.state, "mcp_google", None)
    if existing is not None:
        return existing
    cfg: Config = request.app.state.config
    return _GoogleOIDC(cfg.mcp)


def _google_redirect_uri(request: Request, cfg: Config) -> str:
    """The redirect_uri kobo presents to Google for its OWN callback --
    distinct from the client's `redirect_uri` (Claude.ai/localhost, where
    the final authorization code goes). Must be identical between the
    initial auth request and the token exchange, so it's computed the
    same deterministic way both times rather than threaded through the
    state JWT."""
    if cfg.mcp.issuer_base_url:
        base = cfg.mcp.issuer_base_url.rstrip("/")
    else:
        base = str(request.base_url).rstrip("/")
    return f"{base}/oauth/google/callback"


# --- Well-known documents ---------------------------------------------------


def _issuer(cfg: Config) -> str:
    return cfg.mcp.issuer_base_url.rstrip("/")


def _endpoint(cfg: Config, path: str) -> str:
    base = _issuer(cfg)
    return f"{base}{path}" if base else path


@router.get("/.well-known/oauth-authorization-server")
def authorization_server_metadata(request: Request) -> dict:
    cfg: Config = request.app.state.config
    return {
        "issuer": _issuer(cfg),
        "authorization_endpoint": _endpoint(cfg, "/oauth/authorize"),
        "token_endpoint": _endpoint(cfg, "/oauth/token"),
        "registration_endpoint": _endpoint(cfg, "/oauth/register"),
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    }


@router.get("/.well-known/oauth-protected-resource")
def protected_resource_metadata(request: Request) -> dict:
    cfg: Config = request.app.state.config
    return {
        "resource": _endpoint(cfg, "/mcp"),
        "authorization_servers": [_issuer(cfg)],
    }


# --- GET /oauth/authorize ---------------------------------------------------


@router.get("/oauth/authorize")
def authorize(
    request: Request,
    response_type: str = "",
    client_id: str = "",
    redirect_uri: str = "",
    code_challenge: str = "",
    code_challenge_method: str = "",
    state: str = "",
) -> Response:
    cfg: Config = request.app.state.config

    if response_type != "code":
        return JSONResponse({"error": "unsupported_response_type"}, status_code=400)

    if client_id != cfg.mcp.oauth_client_id:
        return JSONResponse({"error": "unauthorized_client"}, status_code=400)

    if not _redirect_uri_allowed(redirect_uri):
        return JSONResponse(
            {
                "error": "invalid_request",
                "error_description": "redirect_uri is not on the allowlist",
            },
            status_code=400,
        )

    if code_challenge_method != "S256" or not code_challenge:
        return JSONResponse(
            {
                "error": "invalid_request",
                "error_description": "PKCE with code_challenge_method=S256 is required",
            },
            status_code=400,
        )

    state_jwt = mint(
        "state",
        cfg.mcp.oauth_client_id,
        cfg.mcp.oauth_client_id,
        STATE_TTL_S,
        key=_key(cfg),
        code_challenge=code_challenge,
        redirect_uri=redirect_uri,
        client_state=state,
    )

    google = _google_identity(request)
    google_redirect = _google_redirect_uri(request, cfg)
    location = google.auth_url(state_jwt=state_jwt, redirect_uri=google_redirect)
    return RedirectResponse(location, status_code=302)


# --- GET /oauth/google/callback ----------------------------------------------


@router.get("/oauth/google/callback")
def google_callback(request: Request, code: str = "", state: str = "") -> Response:
    cfg: Config = request.app.state.config

    try:
        state_claims = verify(state, "state", cfg.mcp.oauth_client_id, key=_key(cfg))
    except TokenError:
        return JSONResponse(
            {
                "error": "invalid_request",
                "error_description": "state is invalid or expired",
            },
            status_code=400,
        )

    google = _google_identity(request)
    google_redirect = _google_redirect_uri(request, cfg)
    try:
        id_claims = google.exchange_and_verify(code=code, redirect_uri=google_redirect)
    except GoogleIdentityError as exc:
        log.warning("mcp oauth: google exchange failed: %s", exc)
        return JSONResponse(
            {
                "error": "server_error",
                "error_description": "could not verify the google sign in",
            },
            status_code=502,
        )

    if id_claims.get("hd") != cfg.mcp.workspace_domain:
        return JSONResponse(
            {
                "error": "forbidden",
                "error_description": "account is outside the configured workspace",
            },
            status_code=403,
        )

    email = id_claims.get("email", "")
    code_jwt = mint(
        "authorization_code",
        email,
        cfg.mcp.oauth_client_id,
        CODE_TTL_S,
        key=_key(cfg),
        code_challenge=state_claims["code_challenge"],
        redirect_uri=state_claims["redirect_uri"],
    )

    client_redirect_uri = state_claims["redirect_uri"]
    client_state = state_claims.get("client_state", "")
    query = {"code": code_jwt}
    if client_state:
        query["state"] = client_state
    separator = "&" if urlparse(client_redirect_uri).query else "?"
    location = f"{client_redirect_uri}{separator}{urlencode(query)}"
    return RedirectResponse(location, status_code=302)


# --- POST /oauth/token -------------------------------------------------------


def _token_response(cfg: Config, *, email: str, rtid: str, family_id: str) -> dict:
    access_token = mint(
        "access_token",
        email,
        cfg.mcp.oauth_client_id,
        ACCESS_TTL_S,
        key=_key(cfg),
        scope="mcp",
    )
    refresh_token = mint(
        "refresh_token",
        email,
        cfg.mcp.oauth_client_id,
        REFRESH_TTL_S,
        key=_key(cfg),
        rtid=rtid,
        family_id=family_id,
    )
    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": ACCESS_TTL_S,
        "refresh_token": refresh_token,
        "scope": "mcp",
    }


def _grant_authorization_code(
    cfg: Config,
    session: Session,
    *,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    client_id: str,
) -> JSONResponse:
    if client_id != cfg.mcp.oauth_client_id:
        return JSONResponse({"error": "invalid_client"}, status_code=400)

    try:
        claims = verify(
            code, "authorization_code", cfg.mcp.oauth_client_id, key=_key(cfg)
        )
    except TokenError:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    embedded_redirect_uri = claims.get("redirect_uri", "")
    if not _redirect_uri_allowed(embedded_redirect_uri):
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    if redirect_uri and redirect_uri != embedded_redirect_uri:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    if not _pkce_matches(code_verifier, claims.get("code_challenge", "")):
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    email = claims["sub"]
    own_jti = uuid.uuid4().hex
    try:
        family_id = create_family(session, jti=own_jti, email=email)
        # Spend the code (issue #196). Its own jti is the key, so a second
        # exchange of the same code, sequential or concurrent, fails here
        # and takes the first exchange's family down with it.
        replayed = redeem_code(
            session, jti=claims["jti"], email=email, family_id=family_id
        )
        if replayed is not None:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        session.commit()
    except OperationalError:
        session.rollback()
        return JSONResponse({"error": "temporarily_unavailable"}, status_code=503)

    return JSONResponse(
        _token_response(cfg, email=email, rtid=own_jti, family_id=family_id)
    )


def _grant_refresh_token(
    cfg: Config, session: Session, *, refresh_token: str, client_id: str
) -> JSONResponse:
    if client_id and client_id != cfg.mcp.oauth_client_id:
        return JSONResponse({"error": "invalid_client"}, status_code=400)

    try:
        claims = verify(
            refresh_token, "refresh_token", cfg.mcp.oauth_client_id, key=_key(cfg)
        )
    except TokenError:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    email = claims["sub"]
    rtid = claims.get("rtid", "")
    try:
        new_jti = consume(session, jti=rtid, email=email)
        session.commit()
    except OperationalError:
        session.rollback()
        return JSONResponse({"error": "temporarily_unavailable"}, status_code=503)

    if new_jti is None:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    return JSONResponse(
        _token_response(
            cfg, email=email, rtid=new_jti, family_id=claims.get("family_id", "")
        )
    )


@router.post("/oauth/token")
def token(
    request: Request,
    session: Session = Depends(get_session),  # noqa: B008
    grant_type: str = Form(""),
    code: str = Form(""),
    code_verifier: str = Form(""),
    redirect_uri: str = Form(""),
    client_id: str = Form(""),
    refresh_token: str = Form(""),
) -> JSONResponse:
    cfg: Config = request.app.state.config

    if grant_type == "authorization_code":
        return _grant_authorization_code(
            cfg,
            session,
            code=code,
            code_verifier=code_verifier,
            redirect_uri=redirect_uri,
            client_id=client_id,
        )
    if grant_type == "refresh_token":
        return _grant_refresh_token(
            cfg, session, refresh_token=refresh_token, client_id=client_id
        )

    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


# --- POST /oauth/register ----------------------------------------------------


class ClientRegistrationRequest(BaseModel):
    """RFC 7591 dynamic client registration request. Only `redirect_uris`
    matters here -- kobo has exactly one pre-registered public client
    (`config.mcp.oauth_client_id`), so every other RFC 7591 field a real
    client might send (`client_name`, `grant_types`, ...) is accepted and
    ignored rather than rejected."""

    redirect_uris: list[str] = []

    model_config = {"extra": "allow"}


@router.post("/oauth/register")
def register(payload: ClientRegistrationRequest, request: Request) -> Response:
    cfg: Config = request.app.state.config

    if not all(_redirect_uri_allowed(uri) for uri in payload.redirect_uris):
        return JSONResponse(
            {
                "error": "invalid_redirect_uri",
                "error_description": "one or more redirect_uris are not allowed",
            },
            status_code=400,
        )

    return JSONResponse(
        {
            "client_id": cfg.mcp.oauth_client_id,
            "token_endpoint_auth_method": "none",
            "redirect_uris": payload.redirect_uris,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        }
    )
