"""Tests for kobo_api.mcp.jwt (MCP server arc, Task 2) -- the one signing
key every later task's OAuth AS, refresh-token rotation, and tool-call
verification builds on.

Four token types share this module (state, authorization_code,
access_token, refresh_token -- docs/superpowers/specs/2026-08-22-mcp-server-
design.md's "Token mechanics"), each just a `typ` claim value round-tripped
through `mint`/`verify` below. No mocking of PyJWT -- every test signs and
verifies a real HS256 JWT.
"""

from __future__ import annotations

import base64
import logging

import jwt as pyjwt
import pytest

from kobo_api.mcp import jwt as mcp_jwt

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


# --- Constants: exact values from the brief -----------------------------


def test_constants_match_the_brief_exactly():
    assert mcp_jwt.STATE_TTL_S == 300
    assert mcp_jwt.CODE_TTL_S == 60
    assert mcp_jwt.ACCESS_TTL_S == 900
    assert mcp_jwt.REFRESH_TTL_S == 30 * 24 * 3600


# --- mint / verify: round trip -------------------------------------------


@pytest.mark.parametrize("typ", TYPES)
def test_round_trip_each_typ(typ):
    token = mcp_jwt.mint(typ, SUB, AUD, 60, key=_key())

    claims = mcp_jwt.verify(token, typ, AUD, key=_key())

    assert claims["typ"] == typ
    assert claims["sub"] == SUB
    assert claims["aud"] == AUD
    assert "iat" in claims
    assert "exp" in claims
    assert "jti" in claims


def test_mint_passes_through_extra_claims():
    token = mcp_jwt.mint(
        "authorization_code", SUB, AUD, 60, key=_key(), code_challenge="abc123"
    )

    claims = mcp_jwt.verify(token, "authorization_code", AUD, key=_key())

    assert claims["code_challenge"] == "abc123"


# --- Rejections -----------------------------------------------------------


def test_wrong_typ_rejected():
    token = mcp_jwt.mint("access_token", SUB, AUD, 60, key=_key())

    with pytest.raises(mcp_jwt.TokenError):
        mcp_jwt.verify(token, "refresh_token", AUD, key=_key())


def test_wrong_aud_rejected():
    token = mcp_jwt.mint("access_token", SUB, "aud-a", 60, key=_key())

    with pytest.raises(mcp_jwt.TokenError):
        mcp_jwt.verify(token, "access_token", "aud-b", key=_key())


def test_expired_rejected():
    token = mcp_jwt.mint("access_token", SUB, AUD, -1, key=_key())

    with pytest.raises(mcp_jwt.TokenError):
        mcp_jwt.verify(token, "access_token", AUD, key=_key())


def test_tampered_payload_rejected():
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


def test_wrong_key_rejected():
    token = mcp_jwt.mint("access_token", SUB, AUD, 60, key=_key())

    with pytest.raises(mcp_jwt.TokenError):
        mcp_jwt.verify(token, "access_token", AUD, key=b"1" * 32)


def test_malformed_token_rejected():
    with pytest.raises(mcp_jwt.TokenError):
        mcp_jwt.verify("not-a-jwt-at-all", "access_token", AUD, key=_key())


def test_none_algorithm_rejected():
    """A well-shaped but unsigned `alg: none` token must never verify --
    accepting it would mean HS256 wasn't actually pinned on decode."""
    now_claims = {"typ": "access_token", "sub": SUB, "aud": AUD, "jti": "x"}
    token = pyjwt.encode(now_claims, key=None, algorithm="none")

    with pytest.raises(mcp_jwt.TokenError):
        mcp_jwt.verify(token, "access_token", AUD, key=_key())


# --- jti uniqueness ---------------------------------------------------------


def test_jti_unique_across_two_mints():
    first = mcp_jwt.mint("access_token", SUB, AUD, 60, key=_key())
    second = mcp_jwt.mint("access_token", SUB, AUD, 60, key=_key())

    first_claims = mcp_jwt.verify(first, "access_token", AUD, key=_key())
    second_claims = mcp_jwt.verify(second, "access_token", AUD, key=_key())

    assert first_claims["jti"] != second_claims["jti"]


# --- signing_key(): env var vs ephemeral fallback --------------------------


def test_signing_key_reads_named_env_var_base64_decoded(monkeypatch):
    raw = b"a" * 32
    monkeypatch.setenv("MY_CUSTOM_KEY_ENV", base64.b64encode(raw).decode())

    assert mcp_jwt.signing_key("MY_CUSTOM_KEY_ENV") == raw


def test_signing_key_default_env_var_matches_config_default(monkeypatch):
    """`Config.mcp.jwt_key_env` defaults to "KOBO_MCP_JWT_KEY" (Task 1) --
    `signing_key()`'s own default must name that same env var so a caller
    who never overlays `mcp.jwt_key_env` still resolves the right one."""
    raw = b"b" * 32
    monkeypatch.setenv("KOBO_MCP_JWT_KEY", base64.b64encode(raw).decode())

    assert mcp_jwt.signing_key() == raw


def test_signing_key_unset_env_var_returns_cached_ephemeral_key():
    first = mcp_jwt.signing_key("KOBO_MCP_JWT_KEY")
    second = mcp_jwt.signing_key("KOBO_MCP_JWT_KEY")

    assert first == second
    assert len(first) == 32


def test_signing_key_empty_env_var_also_falls_back():
    import os

    os.environ["KOBO_MCP_JWT_KEY"] = ""

    key = mcp_jwt.signing_key("KOBO_MCP_JWT_KEY")

    assert len(key) == 32


def test_ephemeral_key_path_warns_once_and_round_trips(caplog):
    with caplog.at_level(logging.WARNING, logger="kobo_api.mcp.jwt"):
        token = mcp_jwt.mint("access_token", SUB, AUD, 60)
        claims = mcp_jwt.verify(token, "access_token", AUD)
        # A second mint/verify pair in the same process must NOT warn again
        # -- the cache being populated is the guard.
        token2 = mcp_jwt.mint("state", SUB, AUD, 60)
        mcp_jwt.verify(token2, "state", AUD)

    assert claims["sub"] == SUB
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "restart" in warnings[0].message.lower()
