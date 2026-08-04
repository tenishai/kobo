"""Tests for kobo_api.github_app (deployability plan, Task 3) —
InstallationTokenProvider: App JWT minting + installation-token exchange +
caching.

No mocking of the signing path: every test generates a real RS256 keypair
(`cryptography`, via PyJWT[crypto]), writes the private half to a temp PEM
file (exactly how a deployed room would have it mounted), and verifies the
App JWT the provider produces against the public half. An injected
`http_client` (httpx.MockTransport) stands in for the network call to
GitHub's API, and an injected `now` stands in for the wall clock — both are
the only fakes here; the JWT itself is always real, verifiable cryptography.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from kobo_api.clients import ClientError
from kobo_api.github_app import InstallationTokenProvider

APP_ID = "app-123"
INSTALLATION_ID = "installation-456"


def _generate_rsa_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _write_pem(tmp_path, private_key, name: str = "app.pem"):
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path = tmp_path / name
    path.write_bytes(pem)
    return path


def _iso_expiry(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _recording_transport(captured: list, responses):
    """A MockTransport handler that appends every request to `captured` and
    replays `responses[0]`, `responses[1]`, ... in order (repeating the last
    for any call past the end)."""

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        idx = min(len(captured) - 1, len(responses) - 1)
        return responses[idx]

    return handler


# --- App JWT: exact claims/headers, verified against the real public key ----


def test_token_sends_correctly_signed_app_jwt_and_returns_installation_token(tmp_path):
    private_key, public_key = _generate_rsa_keypair()
    key_path = _write_pem(tmp_path, private_key)
    captured: list[httpx.Request] = []
    fixed_now = 1_700_000_000.0
    response = httpx.Response(
        201,
        json={"token": "ghs_abc123", "expires_at": _iso_expiry(fixed_now + 3600)},
    )
    http_client = httpx.Client(
        transport=httpx.MockTransport(_recording_transport(captured, [response]))
    )
    provider = InstallationTokenProvider(
        APP_ID, INSTALLATION_ID, key_path, http_client=http_client, now=lambda: fixed_now
    )

    token = provider.token()

    assert token == "ghs_abc123"
    assert len(captured) == 1
    request = captured[0]
    assert request.method == "POST"
    assert str(request.url) == (
        f"https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens"
    )
    assert request.headers["accept"] == "application/vnd.github+json"

    auth_header = request.headers["authorization"]
    assert auth_header.startswith("Bearer ")
    app_jwt = auth_header.removeprefix("Bearer ")

    # `fixed_now` is a fixed test epoch, not the real wall clock, so exp
    # validation against real "now" is irrelevant here — the claim VALUES
    # (not their freshness relative to the real clock) are what this test
    # checks; the signature check (the load-bearing part) still runs fully.
    claims = jwt.decode(
        app_jwt, key=public_key, algorithms=["RS256"], options={"verify_exp": False}
    )
    assert claims["iss"] == APP_ID
    assert claims["iat"] == int(fixed_now) - 60
    assert claims["exp"] == int(fixed_now) + 9 * 60


def test_token_app_jwt_rejected_under_wrong_public_key(tmp_path):
    # Sanity check on the test helper itself: a JWT signed by the provider's
    # own private key must NOT verify against an unrelated keypair's public
    # half — proves the assertion above is a real signature check, not a
    # tautology.
    private_key, _ = _generate_rsa_keypair()
    _, other_public_key = _generate_rsa_keypair()
    key_path = _write_pem(tmp_path, private_key)
    captured: list[httpx.Request] = []
    response = httpx.Response(
        201, json={"token": "ghs_abc123", "expires_at": _iso_expiry(time.time() + 3600)}
    )
    http_client = httpx.Client(
        transport=httpx.MockTransport(_recording_transport(captured, [response]))
    )
    provider = InstallationTokenProvider(
        APP_ID, INSTALLATION_ID, key_path, http_client=http_client
    )
    provider.token()
    app_jwt = captured[0].headers["authorization"].removeprefix("Bearer ")

    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(app_jwt, key=other_public_key, algorithms=["RS256"])


# --- Caching ------------------------------------------------------------------


def test_token_is_cached_one_http_call_for_two_token_calls(tmp_path):
    private_key, _ = _generate_rsa_keypair()
    key_path = _write_pem(tmp_path, private_key)
    captured: list[httpx.Request] = []
    fixed_now = 1_700_000_000.0
    response = httpx.Response(
        201, json={"token": "ghs_cached", "expires_at": _iso_expiry(fixed_now + 3600)}
    )
    http_client = httpx.Client(
        transport=httpx.MockTransport(_recording_transport(captured, [response]))
    )
    provider = InstallationTokenProvider(
        APP_ID, INSTALLATION_ID, key_path, http_client=http_client, now=lambda: fixed_now
    )

    first = provider.token()
    second = provider.token()

    assert first == "ghs_cached"
    assert second == "ghs_cached"
    assert len(captured) == 1


def test_token_refreshes_after_simulated_expiry_via_injected_now(tmp_path):
    private_key, _ = _generate_rsa_keypair()
    key_path = _write_pem(tmp_path, private_key)
    captured: list[httpx.Request] = []
    start = 1_700_000_000.0
    expires_at = start + 3600  # GitHub's real installation-token lifetime
    responses = [
        httpx.Response(
            201, json={"token": "ghs_first", "expires_at": _iso_expiry(expires_at)}
        ),
        httpx.Response(
            201,
            json={
                "token": "ghs_second",
                "expires_at": _iso_expiry(expires_at + 3600),
            },
        ),
    ]
    http_client = httpx.Client(
        transport=httpx.MockTransport(_recording_transport(captured, responses))
    )
    clock = {"now": start}
    provider = InstallationTokenProvider(
        APP_ID, INSTALLATION_ID, key_path, http_client=http_client, now=lambda: clock["now"]
    )

    first = provider.token()
    assert first == "ghs_first"
    assert len(captured) == 1

    # Still within the cached token's lifetime and outside the 5-minute
    # refresh margin -> no second HTTP call.
    clock["now"] = expires_at - 10 * 60
    still_cached = provider.token()
    assert still_cached == "ghs_first"
    assert len(captured) == 1

    # Now within 5 minutes of the cached token's expiry -> must refresh.
    clock["now"] = expires_at - 4 * 60
    refreshed = provider.token()
    assert refreshed == "ghs_second"
    assert len(captured) == 2


# --- Non-201: status code only, never the response body ----------------------


def test_non_201_raises_client_error_with_status_code_only_never_body(tmp_path, caplog):
    private_key, _ = _generate_rsa_keypair()
    key_path = _write_pem(tmp_path, private_key)
    secret_marker = "SECRET_INSTALLATION_CONTEXT_MARKER_9f3a"
    response = httpx.Response(500, text=f"internal error: {secret_marker}")
    http_client = httpx.Client(
        transport=httpx.MockTransport(_recording_transport([], [response]))
    )
    provider = InstallationTokenProvider(
        APP_ID, INSTALLATION_ID, key_path, http_client=http_client
    )

    with caplog.at_level("DEBUG", logger="kobo_api.github_app"), pytest.raises(ClientError) as exc_info:
        provider.token()

    assert "500" in str(exc_info.value)
    assert secret_marker not in str(exc_info.value)
    # Full detail (including the body) IS available server-side, at debug.
    assert secret_marker in caplog.text


def test_non_201_error_message_never_contains_the_app_jwt_or_any_token(tmp_path):
    private_key, _ = _generate_rsa_keypair()
    key_path = _write_pem(tmp_path, private_key)
    captured: list[httpx.Request] = []
    response = httpx.Response(403, text="Resource not accessible by integration")
    http_client = httpx.Client(
        transport=httpx.MockTransport(_recording_transport(captured, [response]))
    )
    provider = InstallationTokenProvider(
        APP_ID, INSTALLATION_ID, key_path, http_client=http_client
    )

    with pytest.raises(ClientError) as exc_info:
        provider.token()

    app_jwt = captured[0].headers["authorization"].removeprefix("Bearer ")
    assert app_jwt not in str(exc_info.value)


# --- Private key handling ------------------------------------------------------


def test_missing_private_key_file_raises_client_error_naming_only_the_path(tmp_path):
    missing_path = tmp_path / "does-not-exist.pem"
    provider = InstallationTokenProvider(APP_ID, INSTALLATION_ID, missing_path)

    with pytest.raises(ClientError) as exc_info:
        provider.token()

    assert str(missing_path) in str(exc_info.value)
