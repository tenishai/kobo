"""The agent PR broker (2026-08-27 spec): scoped GitHub App token minting.

The room-side half of the spec's two-wall token model. Wall 1 is the
PR App installation's own repository list (org-admin curated,
GitHub-enforced). Wall 2 is here: every token this module mints carries the
`repositories` parameter pinned to exactly the requesting agent's APPROVED
`RepoAccess` rows and `permissions` pinned to the PR-only set -- GitHub then
refuses that token everywhere else, no matter what the caller asks it for.

Two deliberate differences from `kobo_api.github_app.
InstallationTokenProvider` (the room's own repo-management app client):

- NO CACHE. The provider caches because its scope never varies; a broker
  token's scope varies per agent per call, and a cache keyed wide enough to
  be correct would hold live tokens for every agent in room memory. Minted
  on demand, handed back, forgotten.
- A REQUEST BODY. An unscoped mint POSTs empty; the scoping IS this module.

Caller identity: the broker's route authenticates the AGENT, not a human --
the cabal-issued colleague JWT every deployed agent already holds (sub
"agent:<name>", issuer "cabal-core"), verified against the switchboard's
own JWKS: the same issuer/jwks convention the composer bakes into every
bundle's a2a auth block, walked in reverse. `verify_agent_token` pins
issuer and the exact expected sub. The accepted audience is cabal's own
("cabal-core"): that is the audience colleague tokens are minted with
today. The confused-deputy tradeoff (a token aimed at cabal also opens
this one kobo route) is accepted and recorded here; a dedicated
kobo-audience mint on the cabal side is the hardening path if this route
ever grows past token brokering.

JWT conventions for the App JWT itself (iat -60s, exp +9m) match
`kobo_api.github_app` and the family's other GitHub App clients.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import httpx
import jwt as pyjwt
from jwt import PyJWKClient

from kobo_api.clients import ClientError

log = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"
_ACCEPT = "application/vnd.github+json"
_JWT_BACKDATE_S = 60
_JWT_LIFETIME_S = 9 * 60

_CABAL_ISSUER = "cabal-core"
_CABAL_AUDIENCE = "cabal-core"
_JWKS_PATH = "/.well-known/jwks.json"

# PR-only (2026-08-27 ruling 2): contents:write exists solely so the PR's
# own head branch can be created; there is no path to merging here.
PR_PERMISSIONS = {"contents": "write", "pull_requests": "write", "metadata": "read"}


class BrokerAuthError(Exception):
    """The presented bearer is not the named agent's live colleague token."""


def verify_agent_token(cabal_url: str, bearer: str, agent_name: str) -> None:
    """Verify `bearer` is cabal's own signature over `agent:<agent_name>`.

    Raises BrokerAuthError on ANY failure -- expired, wrong issuer, wrong
    audience, wrong sub, unreachable JWKS -- with a caller-safe message;
    the specific cause is logged server-side only, matching the room's
    generic-error discipline.
    """
    try:
        key = PyJWKClient(
            f"{cabal_url.rstrip('/')}{_JWKS_PATH}"
        ).get_signing_key_from_jwt(bearer)
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


def mint_scoped_token(
    *,
    app_id: str,
    installation_id: str,
    private_key_path: Path,
    repositories: list[str],
    http_client: httpx.Client | None = None,
    now=time.time,
) -> dict:
    """Mint one installation access token pinned to `repositories`.

    `repositories` entries arrive as "owner/name" (RepoAccess's own shape);
    GitHub's mint API wants bare repo names -- the owner is implied by the
    installation -- so the owner segment is split off here, at the wall,
    rather than trusted from storage. Returns
    `{"token": ..., "expires_at": ..., "repositories": [...]}` with the
    ORIGINAL owner/name shapes echoed back. Never logs the token.
    """
    if not repositories:
        raise ClientError("refusing to mint a token scoped to nothing")

    epoch = int(now())
    app_jwt = pyjwt.encode(
        {
            "iat": epoch - _JWT_BACKDATE_S,
            "exp": epoch + _JWT_LIFETIME_S,
            "iss": app_id,
        },
        private_key_path.read_text(),
        algorithm="RS256",
    )

    client = http_client or httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0))
    try:
        response = client.post(
            f"{_GITHUB_API}/app/installations/{installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {app_jwt}", "Accept": _ACCEPT},
            json={
                "repositories": [r.split("/", 1)[-1] for r in repositories],
                "permissions": PR_PERMISSIONS,
            },
        )
    except httpx.HTTPError as exc:
        log.debug("scoped token request failed: %r", exc)
        raise ClientError("failed to reach GitHub to mint a scoped token") from exc
    finally:
        if http_client is None:
            client.close()

    if response.status_code != 201:
        log.warning(
            "scoped token mint refused: %s %s",
            response.status_code,
            response.text[:200],
        )
        raise ClientError("GitHub refused the scoped token mint")

    body = response.json()
    return {
        "token": body["token"],
        "expires_at": body.get("expires_at", ""),
        "repositories": list(repositories),
    }
