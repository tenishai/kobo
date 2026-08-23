"""HS256 JWT mint/verify for kobo's MCP server (MCP server arc, Task 2) --
the one signing module every later task's OAuth AS, refresh-token
rotation, and tool-call verification builds on.

Four token types share this module (state, authorization_code,
access_token, refresh_token -- see
docs/superpowers/specs/2026-08-22-mcp-server-design.md's "Token
mechanics"), each just a `typ` claim value stamped through `mint`/`verify`
below. There is no per-type code and no per-type key: one HS256 secret
signs all four, and `typ` is the only thing that tells them apart.

Key source: `signing_key(env_var)` base64-decodes the named env var
(default `KOBO_MCP_JWT_KEY`, matching `Config.mcp.jwt_key_env`'s own
default -- `kobo_api.config.MCPConfig`). An unset or empty env var falls
back to a random key generated once per process (dev only -- a deployed
kobo always sets the real env var; see `_ephemeral_key` below).

This module's own name shadows the `jwt` package (PyJWT) it wraps --
`import jwt as pyjwt` below, rather than a bare `import jwt`, avoids the
self-import a plain `jwt.encode` reference would otherwise resolve to
inside this file.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import secrets
import time
import uuid

import jwt as pyjwt

log = logging.getLogger(__name__)

STATE_TTL_S = 300
CODE_TTL_S = 60
ACCESS_TTL_S = 900
REFRESH_TTL_S = 30 * 24 * 3600

_ALGORITHM = "HS256"
_MIN_KEY_BYTES = 32
_RESERVED_CLAIMS = frozenset({"typ", "sub", "aud", "iat", "exp", "jti"})

# Process-lifetime fallback key, generated at most once, the first time
# `signing_key` is asked for a signing env var that turns out to be unset
# or empty. `None` is both "never needed yet" and the guard that keeps the
# one-time warning below to exactly one log line per process, however many
# times signing_key() is subsequently called.
_ephemeral_key: bytes | None = None


class TokenError(Exception):
    """A token failed to mint or verify: wrong typ, wrong aud, expired,
    tampered, wrong key, or simply malformed. Every failure mode below
    raises this one type, never PyJWT's own exception hierarchy, so a
    caller only ever needs to catch one thing."""


def signing_key(env_var: str = "KOBO_MCP_JWT_KEY") -> bytes:
    """The base64-decoded signing key held in the env var named `env_var`,
    or a cached random 32-byte key when that env var is unset or empty.

    The fallback is cached process-wide (not per env var name): whichever
    env var was asked for first and found missing, every later call that
    also finds its own env var missing reuses that same random key rather
    than minting a fresh one per name. A deployed kobo always sets the
    real env var, so this path only ever runs in dev.

    Raises `TokenError` if the env var is set but isn't valid base64, or
    decodes to fewer than `_MIN_KEY_BYTES` bytes -- a key that short is a
    misconfiguration, not something to sign tokens with.
    """
    global _ephemeral_key

    raw = os.environ.get(env_var, "")
    if raw:
        try:
            decoded = base64.b64decode(raw, validate=False)
        except binascii.Error as exc:
            raise TokenError(
                f"{env_var} is not valid base64: {exc}"
            ) from exc

        if len(decoded) < _MIN_KEY_BYTES:
            raise TokenError(
                f"{env_var} decodes to {len(decoded)} bytes, need at least "
                f"{_MIN_KEY_BYTES}"
            )

        return decoded

    if _ephemeral_key is None:
        _ephemeral_key = secrets.token_bytes(32)
        log.warning(
            "%s is not set; signing MCP tokens with a random per-process "
            "key. Tokens will not survive a restart. Dev only.",
            env_var,
        )
    return _ephemeral_key


def mint(
    typ: str, sub: str, aud: str, ttl_s: int, *, key: bytes | None = None, **extra
) -> str:
    """Mint an HS256 JWT of type `typ` for subject `sub`, audience `aud`,
    expiring `ttl_s` seconds from now. `key` defaults to `signing_key()`;
    pass an explicit key to sign against a different one (a caller that
    resolved its own env var via `signing_key(some_env_var)`, or a test).
    Any `extra` keyword claims (a code challenge, a family id, ...) are
    stamped in verbatim alongside the standard ones. Raises `TokenError`
    if `extra` tries to pass one of the standard claim names (`typ`,
    `sub`, `aud`, `iat`, `exp`, `jti`) -- a caller reaching for `exp=` is
    a bug that needs to surface loudly, not one whose value quietly wins
    against the real expiry below.
    """
    smuggled = extra.keys() & _RESERVED_CLAIMS
    if smuggled:
        raise TokenError(
            f"extra claims collide with reserved claim names: "
            f"{sorted(smuggled)}"
        )

    now = int(time.time())
    claims = {
        "typ": typ,
        "sub": sub,
        "aud": aud,
        "iat": now,
        "exp": now + ttl_s,
        "jti": uuid.uuid4().hex,
        **extra,
    }
    return pyjwt.encode(
        claims, key if key is not None else signing_key(), algorithm=_ALGORITHM
    )


def verify(token: str, expected_typ: str, aud: str, *, key: bytes | None = None) -> dict:
    """Decode and verify `token`, returning its claims. `key` defaults to
    `signing_key()`, same override rule as `mint` above.

    PyJWT itself checks the signature (pinned to HS256 -- never the
    token's own `alg` header), `aud`, and `exp`. `typ` is our own claim,
    not something PyJWT knows about, so it's checked explicitly here,
    after decode. Every failure mode raises `TokenError`, never a bare
    PyJWT exception -- the except clause below is `PyJWTError`, not the
    narrower `InvalidTokenError`, because `InvalidKeyError` and its
    siblings are `PyJWTError`s that don't subclass `InvalidTokenError`.
    """
    try:
        claims = pyjwt.decode(
            token,
            key=key if key is not None else signing_key(),
            algorithms=[_ALGORITHM],
            audience=aud,
        )
    except pyjwt.PyJWTError as exc:
        raise TokenError(f"token failed verification: {exc}") from exc

    actual_typ = claims.get("typ")
    if actual_typ != expected_typ:
        raise TokenError(
            f"expected token type {expected_typ!r}, got {actual_typ!r}"
        )

    return claims
