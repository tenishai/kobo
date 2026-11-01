"""Agent token verification for the room's broker lanes.

The `/broker/agents/{name}/jira/*` routes authenticate the AGENT, not a
human: the cabal-issued colleague JWT every deployed agent already holds
(sub "agent:<name>", issuer "cabal-core"), verified against the
switchboard's own JWKS, the same issuer/jwks convention the composer bakes
into every bundle's a2a auth block, walked in reverse. `verify_agent_token`
pins issuer and the exact expected sub. The accepted audience is cabal's
own ("cabal-core"): that is the audience colleague tokens are minted with
today. The confused-deputy tradeoff (a token aimed at cabal also opens these
kobo routes) is accepted and recorded here; a dedicated kobo-audience mint
on the cabal side is the hardening path if the lane ever grows.

History: this module was the agent PR broker (2026-08-27 spec) and minted
installation tokens scoped to an agent's approved `RepoAccess` rows. That
lane retired on 2026-11-01 (kobo#365): an agent reaches GitHub through the
gateway's `github-mcp` targets with a token the permission service leases
it, narrowed to its own repository set, and the module keeps only the
verification the Jira lane shares.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime

import jwt as pyjwt
from jwt import PyJWKClient

log = logging.getLogger(__name__)

_CABAL_ISSUER = "cabal-core"
_CABAL_AUDIENCE = "cabal-core"
_JWKS_PATH = "/.well-known/jwks.json"


class BrokerAuthError(Exception):
    """The presented bearer is not the named agent's live colleague token."""


# One JWKS client per switchboard URL, for the life of the process (issue
# #199). PyJWKClient caches the key set itself, so building a new one per call
# meant one JWKS fetch per broker call and turned every cabal hiccup into a 401.
_jwks_clients: dict[str, PyJWKClient] = {}
_jwks_lock = threading.Lock()


def _jwks_client(cabal_url: str) -> PyJWKClient:
    url = f"{cabal_url.rstrip('/')}{_JWKS_PATH}"
    with _jwks_lock:
        client = _jwks_clients.get(url)
        if client is None:
            client = _jwks_clients[url] = PyJWKClient(url, cache_keys=True, lifespan=600)
    return client


def verify_agent_token(
    cabal_url: str, bearer: str, agent_name: str, *, not_before: datetime | None = None
) -> dict:
    """Verify `bearer` is cabal's own signature over `agent:<agent_name>`,
    issued no earlier than `not_before`, and return its claims.

    `not_before` is the instant the room last minted this agent's pass
    (`Agent.pass_minted_at`, issue #199). A re-wire revokes the earlier passes
    at the switchboard, but this broker is not a switchboard site, so without
    this cut a pass that leaked from a pod kept minting GitHub tokens for the
    rest of its year. Naive or aware UTC; None means no cut.

    Raises BrokerAuthError on ANY failure -- expired, wrong issuer, wrong
    audience, wrong sub, older than the cut, unreachable JWKS -- with a
    caller-safe message; the specific cause is logged server-side only,
    matching the room's generic-error discipline.
    """
    try:
        key = _jwks_client(cabal_url).get_signing_key_from_jwt(bearer)
        claims = pyjwt.decode(
            bearer,
            key.key,
            algorithms=["RS256"],
            issuer=_CABAL_ISSUER,
            audience=_CABAL_AUDIENCE,
        )
    except Exception as exc:
        log.info("broker token verification failed for %s: %r", agent_name, exc)
        raise BrokerAuthError("agent token was not accepted") from exc

    if claims.get("sub") != f"agent:{agent_name}":
        log.info(
            "broker token sub mismatch: presented %r for agent %r",
            claims.get("sub"),
            agent_name,
        )
        raise BrokerAuthError("agent token was not accepted")
    if not_before is not None:
        cut = not_before if not_before.tzinfo is not None else not_before.replace(tzinfo=UTC)
        issued = float(claims.get("iat") or 0)
        if issued < cut.timestamp():
            log.info(
                "broker token for %s was issued at %s, before the pass minted at %s; refusing",
                agent_name, datetime.fromtimestamp(issued, UTC).isoformat(), cut.isoformat(),
            )
            raise BrokerAuthError("agent token was not accepted")
    return claims
