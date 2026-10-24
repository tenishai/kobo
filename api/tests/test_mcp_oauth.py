"""Tests for kobo_api.mcp.oauth_as (MCP server arc, Task 4) -- the OAuth
2.1 Authorization Server at `/oauth/*` and `/.well-known/*`.

No mocking of our own code: `kobo_api.mcp.jwt` and `kobo_api.mcp.
refresh_tokens` run for real throughout (every token minted here is a
real HS256 JWT, every family row a real sqlite row). The only fake is
`FakeGoogleIdentity`, standing in for the network hop to Google -- it
implements the same `GoogleIdentity` Protocol the real `_GoogleOIDC`
does, injected via `app.state.mcp_google`.

Harness: a bare `FastAPI()` app (not `kobo_api.main.create_app`) with
just this router mounted, `app.state.config`/`app.state.session_factory`
set directly, and `app.state.mcp_google` set to the fake -- mirroring
test_iap.py's own "minimal app, real session_factory, injected seam"
harness rather than pulling in the whole room's route surface for a
router that Task 9 hasn't wired into `main.py` yet.
"""

from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from kobo_api.config import Config, MCPConfig
from kobo_api.mcp import oauth_as
from kobo_api.mcp.jwt import verify
from kobo_api.models import Base, MCPRefreshToken

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
DISALLOWED_REDIRECT_URI = "https://not-on-the-allowlist.example.net/callback"


# --- Fake Google seam --------------------------------------------------


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


# --- Fixtures ------------------------------------------------------------


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config():
    return Config(
        mcp=MCPConfig(
            oauth_client_id=CLIENT_ID,
            workspace_domain=WORKSPACE_DOMAIN,
            issuer_base_url=ISSUER_BASE_URL,
        )
    )


@pytest.fixture
def fake_google():
    return FakeGoogleIdentity()


def _make_app(*, config, session_factory, google=None):
    app = FastAPI()
    app.state.config = config
    app.state.session_factory = session_factory
    if google is not None:
        app.state.mcp_google = google
    app.include_router(oauth_as.router)
    return app


@pytest.fixture
def app(config, session_factory, fake_google):
    return _make_app(config=config, session_factory=session_factory, google=fake_google)


@pytest.fixture
def client(app):
    return TestClient(app, follow_redirects=False)


# --- Helpers --------------------------------------------------------------


def _pkce_pair() -> tuple[str, str]:
    verifier = "a-fixed-test-code-verifier-that-is-long-enough-0123456789"
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


def _authorize(
    client,
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
    return client.get("/oauth/authorize", params=params)


def _code_from_redirect(location: str) -> str:
    return parse_qs(urlparse(location).query)["code"][0]


def _full_grant(client, fake_google, *, code: str = "google-code") -> tuple[str, str, str]:
    """Runs authorize -> callback -> token to a successful end, returning
    (access_token, refresh_token, verifier). `verifier` is handed back so
    callers that don't need it can ignore it."""
    verifier, challenge = _pkce_pair()
    authorize_resp = _authorize(client, challenge=challenge)
    assert authorize_resp.status_code == 302
    state_jwt = fake_google.auth_url_calls[-1]["state_jwt"]

    fake_google.set_claims(code, {"email": EMAIL, "hd": WORKSPACE_DOMAIN})
    callback_resp = client.get(
        "/oauth/google/callback", params={"code": code, "state": state_jwt}
    )
    assert callback_resp.status_code == 302
    code_jwt = _code_from_redirect(callback_resp.headers["location"])

    token_resp = client.post(
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


# --- Happy path end to end -------------------------------------------------


def test_happy_path_end_to_end_issues_a_verifiable_access_token(client, fake_google):
    access_token, refresh_token, _ = _full_grant(client, fake_google, code="google-code-1")

    claims = verify(access_token, "access_token", CLIENT_ID)

    assert claims["sub"] == EMAIL
    assert claims["scope"] == "mcp"
    # A refresh token was minted too, carrying the family plumbing.
    refresh_claims = verify(refresh_token, "refresh_token", CLIENT_ID)
    assert refresh_claims["sub"] == EMAIL
    assert "rtid" in refresh_claims
    assert "family_id" in refresh_claims


def test_authorize_state_jwt_carries_pkce_and_client_redirect(client, fake_google):
    _, challenge = _pkce_pair()

    resp = _authorize(client, challenge=challenge, state="round-trip-me")

    assert resp.status_code == 302
    state_jwt = fake_google.auth_url_calls[-1]["state_jwt"]
    claims = verify(state_jwt, "state", CLIENT_ID)
    assert claims["code_challenge"] == challenge
    assert claims["redirect_uri"] == CLAUDE_REDIRECT_URI
    assert claims["client_state"] == "round-trip-me"


def test_callback_redirects_to_client_with_code_and_state(client, fake_google):
    _, challenge = _pkce_pair()
    _authorize(client, challenge=challenge, state="my-opaque-state")
    state_jwt = fake_google.auth_url_calls[-1]["state_jwt"]
    fake_google.set_claims("google-code-2", {"email": EMAIL, "hd": WORKSPACE_DOMAIN})

    resp = client.get(
        "/oauth/google/callback", params={"code": "google-code-2", "state": state_jwt}
    )

    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith(CLAUDE_REDIRECT_URI)
    query = parse_qs(urlparse(location).query)
    assert query["state"] == ["my-opaque-state"]
    code_claims = verify(query["code"][0], "authorization_code", CLIENT_ID)
    assert code_claims["sub"] == EMAIL


# --- /oauth/authorize validation -------------------------------------------


def test_wrong_client_id_400(client):
    _, challenge = _pkce_pair()

    resp = _authorize(client, challenge=challenge, client_id="not-the-configured-client")

    assert resp.status_code == 400


def test_disallowed_redirect_uri_400(client):
    _, challenge = _pkce_pair()

    resp = _authorize(client, challenge=challenge, redirect_uri=DISALLOWED_REDIRECT_URI)

    assert resp.status_code == 400


def test_missing_pkce_400(client):
    resp = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": CLAUDE_REDIRECT_URI,
            "state": "s1",
        },
    )

    assert resp.status_code == 400


def test_plain_pkce_method_rejected(client):
    _, challenge = _pkce_pair()

    resp = _authorize(client, challenge=challenge, code_challenge_method="plain")

    assert resp.status_code == 400


def test_unsupported_response_type_400(client):
    _, challenge = _pkce_pair()

    resp = _authorize(client, challenge=challenge, response_type="token")

    assert resp.status_code == 400


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "http://localhost:5173/cb",
        "http://127.0.0.1:9999/cb",
        "https://127.0.0.1/cb",
        "https://localhost/cb",
    ],
)
def test_localhost_and_loopback_redirect_uris_are_allowed(client, redirect_uri):
    _, challenge = _pkce_pair()

    resp = _authorize(client, challenge=challenge, redirect_uri=redirect_uri)

    assert resp.status_code == 302


# --- localhost/127.0.0.1 branch is scheme-restricted (fix round 1) ---------
#
# `_redirect_uri_allowed` used to accept the localhost/127.0.0.1 branch on
# hostname alone, so a `file://` or custom-scheme URI with a "localhost"
# host rode the same allowlist branch a real http/https redirect does. The
# module's own docstring already promised "any port, http or https" -- this
# just makes the code match that promise.


@pytest.mark.parametrize(
    ("redirect_uri", "expected_status"),
    [
        ("file://localhost/etc/passwd", 400),
        ("custom-scheme://localhost/cb", 400),
        ("http://localhost:3000/cb", 302),
        ("https://127.0.0.1/cb", 302),
    ],
)
def test_redirect_uri_localhost_branch_requires_http_or_https_scheme(
    client, redirect_uri, expected_status
):
    _, challenge = _pkce_pair()

    resp = _authorize(client, challenge=challenge, redirect_uri=redirect_uri)

    assert resp.status_code == expected_status


# --- /oauth/google/callback: hd hard-check, tampered state -----------------


def test_hd_mismatch_in_callback_claims_403_even_with_a_valid_email(client, fake_google):
    _, challenge = _pkce_pair()
    _authorize(client, challenge=challenge)
    state_jwt = fake_google.auth_url_calls[-1]["state_jwt"]
    fake_google.set_claims("google-code-3", {"email": EMAIL, "hd": "not-" + WORKSPACE_DOMAIN})

    resp = client.get(
        "/oauth/google/callback", params={"code": "google-code-3", "state": state_jwt}
    )

    assert resp.status_code == 403


def test_hd_absent_in_callback_claims_403(client, fake_google):
    _, challenge = _pkce_pair()
    _authorize(client, challenge=challenge)
    state_jwt = fake_google.auth_url_calls[-1]["state_jwt"]
    fake_google.set_claims("google-code-4", {"email": EMAIL})  # no hd at all

    resp = client.get(
        "/oauth/google/callback", params={"code": "google-code-4", "state": state_jwt}
    )

    assert resp.status_code == 403


def test_tampered_state_400(client, fake_google):
    _, challenge = _pkce_pair()
    _authorize(client, challenge=challenge)
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

    resp = client.get("/oauth/google/callback", params={"code": "whatever", "state": tampered})

    assert resp.status_code == 400
    # Tampered state must be rejected before ever calling out to Google.
    assert fake_google.exchange_calls == []


def test_google_exchange_failure_returns_502(client, fake_google):
    _, challenge = _pkce_pair()
    _authorize(client, challenge=challenge)
    state_jwt = fake_google.auth_url_calls[-1]["state_jwt"]
    fake_google.raise_on_exchange = oauth_as.GoogleIdentityError("boom")

    resp = client.get(
        "/oauth/google/callback", params={"code": "whatever", "state": state_jwt}
    )

    assert resp.status_code == 502


# --- /oauth/token: authorization_code grant --------------------------------


def test_pkce_verifier_mismatch_400(client, fake_google):
    _, challenge = _pkce_pair()
    _authorize(client, challenge=challenge)
    state_jwt = fake_google.auth_url_calls[-1]["state_jwt"]
    fake_google.set_claims("google-code-5", {"email": EMAIL, "hd": WORKSPACE_DOMAIN})
    callback_resp = client.get(
        "/oauth/google/callback", params={"code": "google-code-5", "state": state_jwt}
    )
    code_jwt = _code_from_redirect(callback_resp.headers["location"])

    resp = client.post(
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


def test_missing_code_verifier_400(client, fake_google):
    _, challenge = _pkce_pair()
    _authorize(client, challenge=challenge)
    state_jwt = fake_google.auth_url_calls[-1]["state_jwt"]
    fake_google.set_claims("google-code-6", {"email": EMAIL, "hd": WORKSPACE_DOMAIN})
    callback_resp = client.get(
        "/oauth/google/callback", params={"code": "google-code-6", "state": state_jwt}
    )
    code_jwt = _code_from_redirect(callback_resp.headers["location"])

    resp = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code_jwt,
            "redirect_uri": CLAUDE_REDIRECT_URI,
            "client_id": CLIENT_ID,
        },
    )

    assert resp.status_code == 400
    assert resp.json() == {"error": "invalid_grant"}


def test_wrong_client_id_at_token_endpoint_400(client, fake_google):
    _, challenge = _pkce_pair()
    _authorize(client, challenge=challenge)
    state_jwt = fake_google.auth_url_calls[-1]["state_jwt"]
    fake_google.set_claims("google-code-7", {"email": EMAIL, "hd": WORKSPACE_DOMAIN})
    callback_resp = client.get(
        "/oauth/google/callback", params={"code": "google-code-7", "state": state_jwt}
    )
    code_jwt = _code_from_redirect(callback_resp.headers["location"])

    resp = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code_jwt,
            "code_verifier": "irrelevant",
            "redirect_uri": CLAUDE_REDIRECT_URI,
            "client_id": "some-other-client",
        },
    )

    assert resp.status_code == 400


def test_redirect_uri_mismatch_at_token_endpoint_400(client, fake_google):
    _, challenge = _pkce_pair()
    _authorize(client, challenge=challenge)
    state_jwt = fake_google.auth_url_calls[-1]["state_jwt"]
    fake_google.set_claims("google-code-8", {"email": EMAIL, "hd": WORKSPACE_DOMAIN})
    callback_resp = client.get(
        "/oauth/google/callback", params={"code": "google-code-8", "state": state_jwt}
    )
    code_jwt = _code_from_redirect(callback_resp.headers["location"])

    resp = client.post(
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


def test_authorization_code_grant_db_locked_returns_503(client, fake_google, monkeypatch):
    _, challenge = _pkce_pair()
    _authorize(client, challenge=challenge)
    state_jwt = fake_google.auth_url_calls[-1]["state_jwt"]
    fake_google.set_claims("google-code-9", {"email": EMAIL, "hd": WORKSPACE_DOMAIN})
    callback_resp = client.get(
        "/oauth/google/callback", params={"code": "google-code-9", "state": state_jwt}
    )
    code_jwt = _code_from_redirect(callback_resp.headers["location"])

    verifier, _ = _pkce_pair()

    def _boom(*args, **kwargs):
        raise OperationalError("insert", {}, Exception("database is locked"))

    monkeypatch.setattr(oauth_as, "create_family", _boom)

    resp = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code_jwt,
            "code_verifier": verifier,
            "redirect_uri": CLAUDE_REDIRECT_URI,
            "client_id": CLIENT_ID,
        },
    )

    assert resp.status_code == 503
    assert resp.json() == {"error": "temporarily_unavailable"}


def test_unsupported_grant_type_400(client):
    resp = client.post("/oauth/token", data={"grant_type": "client_credentials"})

    assert resp.status_code == 400


# --- /oauth/token: refresh_token grant --------------------------------------


def test_refresh_happy_path_rotates_and_keeps_family(client, fake_google):
    _, refresh_token, _ = _full_grant(client, fake_google, code="google-code-10")

    resp = client.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": CLIENT_ID},
    )

    assert resp.status_code == 200
    body = resp.json()
    old_claims = verify(refresh_token, "refresh_token", CLIENT_ID)
    new_claims = verify(body["refresh_token"], "refresh_token", CLIENT_ID)
    assert new_claims["family_id"] == old_claims["family_id"]
    assert new_claims["rtid"] != old_claims["rtid"]
    assert new_claims["sub"] == EMAIL
    new_access_claims = verify(body["access_token"], "access_token", CLIENT_ID)
    assert new_access_claims["sub"] == EMAIL


def test_refresh_replay_returns_400_and_revokes_the_family(client, fake_google, session_factory):
    _, refresh_token, _ = _full_grant(client, fake_google, code="google-code-11")
    first = client.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": CLIENT_ID},
    )
    assert first.status_code == 200

    replay = client.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": CLIENT_ID},
    )

    assert replay.status_code == 400
    assert replay.json() == {"error": "invalid_grant"}

    claims = verify(refresh_token, "refresh_token", CLIENT_ID)
    session = session_factory()
    try:
        rows = session.query(MCPRefreshToken).filter_by(family_id=claims["family_id"]).all()
        assert len(rows) >= 2
        assert all(row.revoked_at is not None for row in rows)
    finally:
        session.close()


def test_refresh_with_invalid_jwt_400(client):
    resp = client.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": "not-a-jwt-at-all"},
    )

    assert resp.status_code == 400
    assert resp.json() == {"error": "invalid_grant"}


def test_refresh_grant_db_locked_returns_503(client, fake_google, monkeypatch):
    _, refresh_token, _ = _full_grant(client, fake_google, code="google-code-12")

    def _boom(*args, **kwargs):
        raise OperationalError("update", {}, Exception("database is locked"))

    monkeypatch.setattr(oauth_as, "consume", _boom)

    resp = client.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": CLIENT_ID},
    )

    assert resp.status_code == 503
    assert resp.json() == {"error": "temporarily_unavailable"}


# --- /oauth/register ---------------------------------------------------------


def test_register_echoes_the_public_client(client):
    resp = client.post(
        "/oauth/register",
        json={"redirect_uris": [CLAUDE_REDIRECT_URI, "http://localhost:9999/cb"]},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["client_id"] == CLIENT_ID
    assert body["token_endpoint_auth_method"] == "none"
    assert body["redirect_uris"] == [CLAUDE_REDIRECT_URI, "http://localhost:9999/cb"]
    assert "authorization_code" in body["grant_types"]
    assert "refresh_token" in body["grant_types"]


def test_register_rejects_a_disallowed_redirect_uri(client):
    resp = client.post("/oauth/register", json={"redirect_uris": [DISALLOWED_REDIRECT_URI]})

    assert resp.status_code == 400


def test_register_with_no_redirect_uris_is_fine(client):
    resp = client.post("/oauth/register", json={})

    assert resp.status_code == 200
    assert resp.json()["redirect_uris"] == []


# --- well-known documents ---------------------------------------------------


def test_authorization_server_metadata_carries_the_right_endpoints(client):
    resp = client.get("/.well-known/oauth-authorization-server")

    assert resp.status_code == 200
    body = resp.json()
    assert body["authorization_endpoint"].endswith("/oauth/authorize")
    assert body["token_endpoint"].endswith("/oauth/token")
    assert body["registration_endpoint"].endswith("/oauth/register")
    assert body["code_challenge_methods_supported"] == ["S256"]
    assert "authorization_code" in body["grant_types_supported"]
    assert "refresh_token" in body["grant_types_supported"]


def test_protected_resource_metadata_points_at_mcp_and_issuer(client, config):
    resp = client.get("/.well-known/oauth-protected-resource")

    assert resp.status_code == 200
    body = resp.json()
    assert body["resource"].endswith("/mcp")
    assert body["authorization_servers"] == [config.mcp.issuer_base_url]


def test_well_known_uses_configured_issuer_base_url_when_set(session_factory, fake_google):
    issuer_config = Config(
        mcp=MCPConfig(
            oauth_client_id=CLIENT_ID,
            workspace_domain=WORKSPACE_DOMAIN,
            issuer_base_url="https://kobo.example.org",
        )
    )
    test_client = TestClient(
        _make_app(config=issuer_config, session_factory=session_factory, google=fake_google),
        follow_redirects=False,
    )

    resp = test_client.get("/.well-known/oauth-authorization-server")

    body = resp.json()
    assert body["issuer"] == "https://kobo.example.org"
    assert body["authorization_endpoint"] == "https://kobo.example.org/oauth/authorize"

    resource_resp = test_client.get("/.well-known/oauth-protected-resource")
    resource_body = resource_resp.json()
    assert resource_body["resource"] == "https://kobo.example.org/mcp"
    assert resource_body["authorization_servers"] == ["https://kobo.example.org"]


# --- Google seam: lazy construction when app.state.mcp_google is absent ----


def test_lazy_google_identity_is_constructed_when_app_state_has_none(session_factory, config):
    test_client = TestClient(
        _make_app(config=config, session_factory=session_factory, google=None),
        follow_redirects=False,
    )
    _, challenge = _pkce_pair()

    resp = test_client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": CLAUDE_REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "s1",
        },
    )

    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    query = parse_qs(urlparse(location).query)
    assert query["client_id"] == [CLIENT_ID]
    assert query["hd"] == [WORKSPACE_DOMAIN]
    state_jwt = query["state"][0]
    claims = verify(state_jwt, "state", CLIENT_ID)
    assert claims["redirect_uri"] == CLAUDE_REDIRECT_URI


# --- /oauth/token: a code is spent once (issue #196) --------------------------


def _code_grant(client, fake_google, *, code: str = "google-code"):
    """authorize -> callback, returning (code_jwt, verifier) without exchanging."""
    verifier, challenge = _pkce_pair()
    assert _authorize(client, challenge=challenge).status_code == 302
    state_jwt = fake_google.auth_url_calls[-1]["state_jwt"]
    fake_google.set_claims(code, {"email": EMAIL, "hd": WORKSPACE_DOMAIN})
    callback = client.get("/oauth/google/callback", params={"code": code, "state": state_jwt})
    assert callback.status_code == 302
    return _code_from_redirect(callback.headers["location"]), verifier


def _exchange(client, code_jwt, verifier):
    return client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code_jwt,
            "code_verifier": verifier,
            "redirect_uri": CLAUDE_REDIRECT_URI,
            "client_id": CLIENT_ID,
        },
    )


def test_a_code_exchanged_twice_fails_and_kills_the_first_grants_family(client, fake_google):
    code_jwt, verifier = _code_grant(client, fake_google)
    first = _exchange(client, code_jwt, verifier)
    assert first.status_code == 200
    refresh_token = first.json()["refresh_token"]

    second = _exchange(client, code_jwt, verifier)
    assert second.status_code == 400
    assert second.json()["error"] == "invalid_grant"

    # The first exchange's tokens are gone too: a refresh on them fails.
    refreshed = client.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": CLIENT_ID},
    )
    assert refreshed.status_code == 400
    assert refreshed.json()["error"] == "invalid_grant"
