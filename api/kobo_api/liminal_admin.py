"""The tool gateway's admin plane, as the room speaks to it (admin panel
Connections, design 2026-10-18 Arc 2; Kyle's ruling 2026-10-24: the room
holds its own gateway admin token).

The gateway keys authority on the verified (issuer, persona) of a token, and
its trust registry names `kobo-room` as an admin persona of the switchboard
issuer. So the room's token is minted by the switchboard's operator-only
mint-service with `sub` and `persona` both `kobo-room`, audience the gateway,
and it is cached in memory until a day before it expires. No token is ever
written to disk or logged.

A grant on the gateway REPLACES the persona's row (scopes and allowed
handlers), so the caller merges what the gateway already holds for that
persona before writing; `admin_connections` does that merge.

Targets go the same way from liminal 0.9.0 (liminal#222, kobo#327): `PUT
/admin/targets/<name>` with the stanza as a JSON object (no `name`) adds or
replaces a live target, `DELETE` removes it, and a target the gateway's own
config file owns answers 409. An older gateway has no such routes and
answers FastAPI's plain 404; `put_target` turns that into the sentence
`TARGETS_UNSUPPORTED`, so the caller can fall back to the pull request.

The handlers' cards come the same way from liminal 0.10.0 (`GET
/admin/cards`): every loaded handler's capability card under its name, labels
and all, which `gateway_lane` derives what each permission opens from. An
older gateway answers the plain 404, and `cards` raises `CARDS_UNSUPPORTED`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

from .clients import CabalClient, CabalError, ClientError

log = logging.getLogger(__name__)

ROOM_PERSONA = "kobo-room"
GATEWAY_AUDIENCE = "liminal"
ADMIN_SCOPE = "admin:*"
TOKEN_TTL_DAYS = 7
TOKEN_RENEW_BEFORE_S = 24 * 3600
ISSUER = "cabal-core"
# What `put_target` says when the gateway has no target routes (before liminal 0.9.0).
TARGETS_UNSUPPORTED = "the gateway's admin plane does not take targets yet (liminal 0.9.0 or later)"
# What `cards` says when the gateway does not list its handlers' cards (before liminal 0.10.0).
CARDS_UNSUPPORTED = "the gateway's admin plane does not list its handlers' cards yet (liminal 0.10.0 or later)"


class TargetNotFound(ClientError):
    """`DELETE /admin/targets/<name>` found no live target of that name."""


def targets_unsupported(exc: BaseException) -> bool:
    """Whether a `ClientError` is the admin plane lacking target routes."""
    return "does not take targets yet" in str(exc)


@dataclass(frozen=True)
class GatewayGrant:
    issuer: str
    persona: str
    scopes: tuple[str, ...]
    allowed_handlers: tuple[str, ...] | None  # None = every handler


class LiminalAdminClient(Protocol):
    def list_grants(self) -> list[GatewayGrant]: ...
    def grant(self, grant: GatewayGrant) -> None: ...
    def revoke(self, issuer: str, persona: str) -> None: ...
    def aws_identity(self, audience: str) -> dict[str, str]: ...
    def aws_probe(self, role_arn: str, audience: str) -> dict[str, str]: ...
    def audit(self, *, persona: str, since: float | None = None, limit: int = 200) -> list[dict]: ...
    def list_targets(self) -> list[dict]: ...
    def put_target(self, name: str, spec: dict) -> dict: ...
    def delete_target(self, name: str) -> dict: ...
    def cards(self) -> list[dict]: ...


class RoomAdminToken:
    """Mints and caches the room's gateway admin token through the
    switchboard. Renewed a day before expiry; a failed mint raises
    `CabalError` and the previous token, if still valid, keeps serving."""

    def __init__(self, cabal: CabalClient, *, ttl_days: int = TOKEN_TTL_DAYS, now=time.time):
        self._cabal = cabal
        self._ttl_days = ttl_days
        self._now = now
        self._token: str | None = None
        self._expires_at = 0.0

    def __call__(self) -> str:
        if self._token is None or self._now() > self._expires_at - TOKEN_RENEW_BEFORE_S:
            token = self._cabal.mint_service(
                sub=ROOM_PERSONA, persona=ROOM_PERSONA, aud=GATEWAY_AUDIENCE,
                scope=[ADMIN_SCOPE], ttl_days=self._ttl_days,
            )
            self._token = token
            self._expires_at = self._now() + self._ttl_days * 86400
            log.info("gateway admin token minted for %s (%d days)", ROOM_PERSONA, self._ttl_days)
        return self._token


class HttpxLiminalAdminClient:
    def __init__(self, base_url: str, token_provider, *, client: httpx.Client | None = None):
        self._base_url = base_url.rstrip("/")
        self._token = token_provider
        self._client = client or httpx.Client(timeout=httpx.Timeout(15.0, connect=5.0))

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token()}"}

    def _check(self, resp: httpx.Response, what: str) -> dict:
        if resp.status_code != 200:
            raise ClientError(f"gateway admin plane refused {what}: HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise ClientError(f"gateway admin plane returned no JSON for {what}") from exc

    def list_grants(self) -> list[GatewayGrant]:
        try:
            body = self._check(self._client.get(f"{self._base_url}/admin/policy", headers=self._headers()), "list")
        except httpx.HTTPError as exc:
            raise ClientError(f"gateway admin plane unreachable: {exc.__class__.__name__}") from exc
        out = []
        for r in body.get("grants", []):
            handlers = r.get("allowed_handlers")
            out.append(GatewayGrant(
                issuer=r["issuer"], persona=r["persona"], scopes=tuple(sorted(r.get("scopes", []))),
                allowed_handlers=None if handlers is None else tuple(sorted(handlers)),
            ))
        return out

    def grant(self, grant: GatewayGrant) -> None:
        payload = {
            "issuer": grant.issuer, "persona": grant.persona, "scopes": list(grant.scopes),
            "allowed_handlers": None if grant.allowed_handlers is None else list(grant.allowed_handlers),
        }
        try:
            self._check(self._client.post(f"{self._base_url}/admin/policy/grant", json=payload, headers=self._headers()), f"grant for {grant.persona}")
        except httpx.HTTPError as exc:
            raise ClientError(f"gateway admin plane unreachable: {exc.__class__.__name__}") from exc

    def revoke(self, issuer: str, persona: str) -> None:
        try:
            self._check(self._client.post(f"{self._base_url}/admin/policy/revoke", json={"issuer": issuer, "persona": persona}, headers=self._headers()), f"revoke for {persona}")
        except httpx.HTTPError as exc:
            raise ClientError(f"gateway admin plane unreachable: {exc.__class__.__name__}") from exc


    def aws_identity(self, audience: str) -> dict[str, str]:
        """The gateway's own Google identity (issuer, subject, email,
        audience): what an AWS trust policy names."""
        try:
            resp = self._client.get(f"{self._base_url}/admin/aws/identity", params={"audience": audience}, headers=self._headers())
        except httpx.HTTPError as exc:
            raise ClientError(f"gateway admin plane unreachable: {exc.__class__.__name__}") from exc
        if resp.status_code == 503:
            raise ClientError(_detail(resp, "the gateway has no cloud identity yet"))
        return {k: str(v) for k, v in self._check(resp, "aws identity").items()}

    def aws_probe(self, role_arn: str, audience: str) -> dict[str, str]:
        """Assume the role once as the room. AWS's refusal comes back as
        the sentence to show; `account` and `arn` on success."""
        try:
            resp = self._client.post(f"{self._base_url}/admin/aws/probe", json={"role_arn": role_arn, "audience": audience}, headers=self._headers())
        except httpx.HTTPError as exc:
            raise ClientError(f"gateway admin plane unreachable: {exc.__class__.__name__}") from exc
        if resp.status_code == 400:
            raise ClientError(_detail(resp, "AWS refused the role"))
        return {k: str(v) for k, v in self._check(resp, "aws probe").items()}

    def audit(self, *, persona: str, since: float | None = None, limit: int = 200) -> list[dict]:
        """The gateway's audit rows for one persona, newest first: every
        admitted call (`kind: call`) and every refusal."""
        params: dict[str, str | int | float] = {"persona": persona, "limit": limit}
        if since is not None:
            params["since"] = since
        try:
            body = self._check(self._client.get(f"{self._base_url}/admin/policy/audit", params=params, headers=self._headers()), f"audit for {persona}")
        except httpx.HTTPError as exc:
            raise ClientError(f"gateway admin plane unreachable: {exc.__class__.__name__}") from exc
        rows = body.get("audit", [])
        return [r for r in rows if isinstance(r, dict)]

    def list_targets(self) -> list[dict]:
        """Every target the gateway serves, live or from its file, with its
        status and its spec without secrets."""
        try:
            body = self._check(self._client.get(f"{self._base_url}/admin/targets", headers=self._headers()), "list targets")
        except httpx.HTTPError as exc:
            raise ClientError(f"gateway admin plane unreachable: {exc.__class__.__name__}") from exc
        return [t for t in body.get("targets", []) if isinstance(t, dict)]

    def put_target(self, name: str, spec: dict) -> dict:
        """Add or replace a live target; `{"name", "status"}` back. The
        gateway's refusal (409 file-owned, 422 invalid) is the sentence
        raised. A plain 404 is an admin plane without target routes."""
        try:
            resp = self._client.put(f"{self._base_url}/admin/targets/{name}", json=spec, headers=self._headers())
        except httpx.HTTPError as exc:
            raise ClientError(f"gateway admin plane unreachable: {exc.__class__.__name__}") from exc
        if resp.status_code == 404 and not _about_target(_detail(resp, ""), name):
            raise ClientError(TARGETS_UNSUPPORTED)
        if resp.status_code in (404, 409, 422):
            raise ClientError(_detail(resp, f"the gateway refused the {name} target: HTTP {resp.status_code}"))
        return self._check(resp, f"target {name}")

    def delete_target(self, name: str) -> dict:
        """Remove a live target; `{"removed": name}` back. `TargetNotFound`
        for a name the gateway does not serve live; the 409 sentence when its
        config file owns the target; a plain 404 is the older admin plane."""
        try:
            resp = self._client.delete(f"{self._base_url}/admin/targets/{name}", headers=self._headers())
        except httpx.HTTPError as exc:
            raise ClientError(f"gateway admin plane unreachable: {exc.__class__.__name__}") from exc
        if resp.status_code == 404:
            detail = _detail(resp, "")
            if not _about_target(detail, name):
                raise ClientError(TARGETS_UNSUPPORTED)
            raise TargetNotFound(detail)
        if resp.status_code == 409:
            raise ClientError(_detail(resp, f"the gateway refused removing the {name} target: HTTP 409"))
        return self._check(resp, f"removing target {name}")


    def cards(self) -> list[dict]:
        """Every loaded handler's capability card as `{"name", "card"}`, as
        written (labels, a tool's `permissions` and `risk`). Any failure raises
        `ClientError`, never an empty list: the caller keeps what it knew."""
        try:
            resp = self._client.get(f"{self._base_url}/admin/cards", headers=self._headers())
        except httpx.HTTPError as exc:
            raise ClientError(f"gateway admin plane unreachable: {exc.__class__.__name__}") from exc
        if resp.status_code == 404:
            # FastAPI's plain "Not Found" is a gateway without the route; liminal's own 404
            # says why (an admin plane built without the handler registry).
            detail = _detail(resp, "")
            raise ClientError(CARDS_UNSUPPORTED if detail.strip().lower() in ("", "not found") else detail)
        cards = self._check(resp, "cards").get("cards")
        if not isinstance(cards, list):
            raise ClientError("gateway admin plane answered cards without a list")
        return [c for c in cards if isinstance(c, dict)]


def _about_target(detail: str, name: str) -> bool:
    """Whether a 404's detail is the gateway talking about the target (unknown
    name) rather than FastAPI's plain `Not Found` for a route it lacks."""
    text = detail.strip()
    if not text or text.lower() == "not found":
        return False
    return "target" in text.lower() or name in text


def _detail(resp: httpx.Response, fallback: str) -> str:
    try:
        return str(resp.json().get("detail") or fallback)
    except ValueError:
        return fallback


def build_liminal_admin(config, cabal: CabalClient | None) -> LiminalAdminClient | None:
    """The real client when the org names an admin plane and the switchboard
    is wired (the token comes from it); None otherwise, and the panel says
    grants land as the pull request only."""
    if not config.liminal_admin_url or cabal is None:
        return None
    return HttpxLiminalAdminClient(config.liminal_admin_url, RoomAdminToken(cabal))


__all__ = [
    "ADMIN_SCOPE", "CARDS_UNSUPPORTED", "ISSUER", "ROOM_PERSONA", "TARGETS_UNSUPPORTED", "CabalError", "GatewayGrant", "HttpxLiminalAdminClient",
    "LiminalAdminClient", "RoomAdminToken", "TargetNotFound", "build_liminal_admin", "targets_unsupported",
]
