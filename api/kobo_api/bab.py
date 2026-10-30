"""Access through bab: the room's permission service (design
`docs/superpowers/specs/2026-10-27-bab-permissions-design.md`; the contract
is bab's `docs/design-leases.md`).

bab decides; kobo is the console. An agent's owner picks permissions per
connected integration, a reviewer approves them in bulk, and the agent
borrows a one-hour credential from bab when a task needs one. kobo never
holds a vendor credential for an agent and never sees a lease's credential.

The room calls bab with one shared console token (`KOBO_BAB_TOKEN`) and
names the signed-in person in `X-Bab-Actor`; bab records both. The room also
tells bab which agents exist (`PUT /topology`), with the pod identity that is
each one, so a lease asked for by the pod `ledger/agent-ledger` lands under
the agent the room calls `ledger`.

Absent = off: no `bab_url` in the org config, or no token in the environment,
and nothing here is wired (`build_bab` returns None, every route answers 404
"access is off for this room").
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Protocol

import httpx

from .clients import ClientError
from .kinds import KIND_CONNECTED
from .models import AgentStatus
from .timefmt import stamp_strings

log = logging.getLogger(__name__)

TOKEN_ENV = "KOBO_BAB_TOKEN"
ROOM_ACTOR = "room@kobo"
"""The actor the room names when no person is behind a call (boot, destroy)."""

AGENTS_VENDOR = "agents"
"""bab's vendor for agent-to-agent talk (kobo#310, gate 1): one integration per
built agent, id = the agent's name, offering the one permission below."""
A2A_ASK = "a2a_ask"
"""The permission an asker holds on a target agent's `agents` integration. An
active grant is what the switchboard's dispatch pair follows; bab never leases
it (a lease on an `agents` integration is 409)."""


def _external_id(token: str, subject: str) -> str:
    import hmac
    from hashlib import sha256

    return hmac.new(token.encode(), subject.encode(), sha256).hexdigest()[:32]


class BabClient(Protocol):
    def health(self) -> bool: ...
    def vendors(self, actor: str) -> list[dict]: ...
    def integrations(self, actor: str) -> list[dict]: ...
    def catalogue(self, vendor: str, actor: str) -> dict: ...
    def put_topology(self, agents: list[dict], actor: str) -> dict: ...
    def request(self, agent: str, integration: str, permissions: list[str], note: str | None, actor: str) -> dict: ...
    def requests(self, status: str, agent: str | None, actor: str) -> list[dict]: ...
    def decide(self, ids: list[int], verdict: str, reason: str | None, actor: str) -> list[dict]: ...
    def grants(self, agent: str, actor: str) -> list[dict]: ...
    def revoke(self, grant_id: int, actor: str) -> dict: ...
    def leases(self, agent: str, since: datetime | None, actor: str) -> list[dict]: ...
    def all_leases(self, since: datetime | None, actor: str) -> list[dict]: ...
    def aws_identity(self, actor: str) -> dict: ...
    def probe_aws(self, role_arn: str, audience: str, actor: str, hub_role_arn: str | None = None,
                  external_id: str | None = None) -> dict: ...
    def put_integration(self, integration: str, vendor: str, spec: dict, ceiling: list[str], actor: str,
                        parent: str | None = None) -> dict: ...
    def delete_integration(self, integration: str, actor: str) -> dict: ...
    def talks_to(self, agent: str, actor: str) -> list[str]: ...
    def org_accounts(self, integration: str, actor: str) -> list[dict]: ...
    def org_hub_role_arn(self, integration: str, actor: str) -> str: ...
    def external_id_for(self, parent: str, account_id: str) -> str: ...
    def external_id_for_org(self, parent: str) -> str: ...
    def discover_accounts(self, integration: str, actor: str, role_name: str) -> dict: ...


class HttpxBabClient:
    def __init__(self, base_url: str, token: str, *, client: httpx.Client | None = None):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._client = client or httpx.Client(timeout=httpx.Timeout(15.0, connect=5.0))

    def _headers(self, actor: str) -> dict[str, str]:
        return {"X-Bab-Console-Token": self._token, "X-Bab-Actor": actor}

    def _call(self, method: str, path: str, actor: str, *, json=None, params=None, what: str) -> dict:
        try:
            resp = self._client.request(method, f"{self._base_url}{path}", headers=self._headers(actor),
                                        json=json, params=params)
        except httpx.HTTPError as exc:
            raise ClientError(f"the permission service is unreachable: {exc.__class__.__name__}") from exc
        if resp.status_code >= 400:
            raise ClientError(f"the permission service refused {what}: {_detail(resp)}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise ClientError(f"the permission service returned no JSON for {what}") from exc
        # The service writes naive UTC; every `*_at` it hands back leaves here with its Z (#306).
        return stamp_strings(body)

    def health(self) -> bool:
        try:
            return self._client.get(f"{self._base_url}/healthz").status_code == 200
        except httpx.HTTPError:
            return False

    def vendors(self, actor: str) -> list[dict]:
        """The catalogue's vendors with their kind. An older bab (before 0.3.1) has no
        such route; the caller derives vendors from the integrations instead."""
        try:
            return self._call("GET", "/catalogue", actor, what="the vendors").get("vendors", [])
        except ClientError as exc:
            if "404" in str(exc) or "Not Found" in str(exc):
                return []
            raise

    def integrations(self, actor: str) -> list[dict]:
        return self._call("GET", "/integrations", actor, what="the integrations").get("integrations", [])

    def catalogue(self, vendor: str, actor: str) -> dict:
        return self._call("GET", f"/catalogue/{vendor}", actor, what=f"the {vendor} catalogue")

    def put_topology(self, agents: list[dict], actor: str) -> dict:
        return self._call("PUT", "/topology", actor, json={"agents": agents}, what="the topology")

    def request(self, agent: str, integration: str, permissions: list[str], note: str | None, actor: str) -> dict:
        body = {"agent": agent, "integration": integration, "permissions": permissions, "note": note}
        return self._call("POST", "/requests", actor, json=body, what=f"a request for {agent}")

    def requests(self, status: str, agent: str | None, actor: str) -> list[dict]:
        params = {"status": status}
        if agent:
            params["agent"] = agent
        return self._call("GET", "/requests", actor, params=params, what="the requests").get("requests", [])

    def decide(self, ids: list[int], verdict: str, reason: str | None, actor: str) -> list[dict]:
        body = {"ids": ids, "verdict": verdict, "reason": reason}
        return self._call("POST", "/requests/decide", actor, json=body, what="the decision").get("results", [])

    def grants(self, agent: str, actor: str) -> list[dict]:
        return self._call("GET", f"/agents/{agent}/grants", actor, what=f"{agent}'s grants").get("grants", [])

    def revoke(self, grant_id: int, actor: str) -> dict:
        return self._call("DELETE", f"/grants/{grant_id}", actor, what=f"revoking grant {grant_id}")

    def leases(self, agent: str, since: datetime | None, actor: str) -> list[dict]:
        params: dict[str, str] = {"agent": agent}
        if since is not None:
            params["since"] = since.isoformat()
        return self._call("GET", "/leases", actor, params=params, what=f"{agent}'s leases").get("leases", [])

    def all_leases(self, since: datetime | None, actor: str) -> list[dict]:
        params: dict[str, str] = {"limit": "500"}
        if since is not None:
            params["since"] = since.isoformat()
        return self._call("GET", "/leases", actor, params=params, what="the leases").get("leases", [])

    def aws_identity(self, actor: str) -> dict:
        return self._call("GET", "/identity/aws", actor, what="its identity")

    def probe_aws(self, role_arn: str, audience: str, actor: str, hub_role_arn: str | None = None,
                  external_id: str | None = None) -> dict:
        body = {"role_arn": role_arn, "audience": audience, "hub_role_arn": hub_role_arn, "external_id": external_id}
        return self._call("POST", "/probe/aws", actor, json=body, what=f"the role {role_arn}")

    def put_integration(self, integration: str, vendor: str, spec: dict, ceiling: list[str], actor: str,
                        parent: str | None = None) -> dict:
        return self._call("PUT", f"/integrations/{integration}", actor,
                          json={"vendor": vendor, "spec": spec, "ceiling": ceiling, "parent": parent},
                          what=f"connecting {integration}")

    def org_accounts(self, integration: str, actor: str) -> list[dict]:
        return self._call("GET", f"/integrations/{integration}/accounts", actor,
                          what=f"the accounts of {integration}").get("accounts", [])

    def org_hub_role_arn(self, integration: str, actor: str) -> str:
        for i in self.integrations(actor):
            if i["integration"] == integration:
                return str(i.get("hub_role_arn") or "")
        return ""

    def external_id_for(self, parent: str, account_id: str) -> str:
        """The per-account external id of the first hub-and-spoke shape, kept so an account
        connected under it keeps leasing. New accounts use the organization's (#275)."""
        return _external_id(self._token, f"{parent}:{account_id}")

    def external_id_for_org(self, parent: str) -> str:
        """One external id per organization (#275): every member account's spoke role
        trusts the hub with the same id, so one template deploys the role everywhere
        and the hub finds the accounts that have it. Derived from the room's console
        token, so the set-up page and the connect agree before anything is stored."""
        return _external_id(self._token, parent)

    def discover_accounts(self, integration: str, actor: str, role_name: str) -> dict:
        return self._call("POST", f"/integrations/{integration}/discover", actor, json={"role_name": role_name},
                          what=f"the accounts of {integration}")

    def delete_integration(self, integration: str, actor: str) -> dict:
        return self._call("DELETE", f"/integrations/{integration}", actor, what=f"disconnecting {integration}")

    def talks_to(self, agent: str, actor: str) -> list[str]:
        """The agents `agent` holds an active `a2a_ask` grant on (bab's
        `GET /agents/{asker}/talks-to` -> {"agent", "talks_to": [names]})."""
        return list(self._call("GET", f"/agents/{agent}/talks-to", actor,
                               what=f"whom {agent} may ask").get("talks_to", []))


def _detail(resp: httpx.Response) -> str:
    """bab's `detail` as a sentence; a dict detail (a lease refusal) is summarised."""
    try:
        detail = resp.json().get("detail")
    except ValueError:
        return f"HTTP {resp.status_code}"
    if isinstance(detail, dict):
        return "; ".join(f"{k}: {v}" for k, v in detail.items() if v) or f"HTTP {resp.status_code}"
    return str(detail or f"HTTP {resp.status_code}")


def build_bab(config) -> BabClient | None:
    """The real client when the org names `bab_url` and the environment holds
    the console token; None otherwise, and Access is off for the room."""
    token = os.environ.get(TOKEN_ENV, "")
    if not config.bab_url or not token:
        return None
    return HttpxBabClient(config.bab_url, token)


def actor_for(email: str | None) -> str:
    """Whom to name in `X-Bab-Actor`: the signed-in person, or the room itself."""
    return (email or "").strip().lower() or ROOM_ACTOR


# ---- the topology the room pushes -------------------------------------------


def topology_agent(agent) -> dict:
    """One agent as bab's topology names it: the team it belongs to and the pod
    identity that is it. A composed agent runs as `agent-<name>` in namespace
    `<name>`; a connected agent runs as the deployment name itself, which is
    the agent's name (connected-repo identity ruling, 2026-09-30)."""
    kind = getattr(agent, "kind", None)
    sa = agent.name if kind == KIND_CONNECTED else f"agent-{agent.name}"
    return {"name": agent.name, "team": agent.team or "shared", "namespace": agent.name, "service_account": sa}


def push_topology(bab: BabClient, agents, actor: str) -> dict | None:
    """Tell bab which agents exist. Never raises: the push is a projection of
    the room's own state and a failure here must not fail a create or a
    destroy; the next push (boot, create, destroy, the Review pane's Sync
    button) carries the same facts. The same push makes every READY built
    agent askable (`ensure_agent_integrations`), so a room that upgrades
    into kobo#310 has its agents in bab's `agents` vendor after one boot."""
    try:
        out = bab.put_topology([topology_agent(a) for a in agents], actor)
    except ClientError as exc:
        log.warning("bab topology push failed: %s", exc)
        return None
    log.info("bab topology pushed: %s", out)
    ensure_agent_integrations(bab, agents, actor)
    return out


def _askable(agent) -> bool:
    """A READY built agent: the only kind the switchboard can carry a pair to
    (`kobo_api.pairs.allowed_pairs` names no other)."""
    return agent.status == AgentStatus.READY and getattr(agent, "kind", None) != KIND_CONNECTED


def ensure_agent_integrations(bab: BabClient, agents, actor: str) -> list[str]:
    """Every READY built agent is an `agents` integration in bab (kobo#310,
    gate 1): `PUT /integrations/{agent}` {vendor: "agents", spec: {name},
    ceiling: ["a2a_ask"]} for each one bab does not hold yet. Returns the
    names put. Never raises; a miss is logged and the next push tries again."""
    try:
        held = {i["integration"] for i in bab.integrations(actor) if i.get("vendor") == AGENTS_VENDOR}
    except ClientError as exc:
        log.warning("bab integrations unreadable; agents left as they are: %s", exc)
        return []
    put: list[str] = []
    for agent in agents:
        if not _askable(agent) or agent.name in held:
            continue
        try:
            bab.put_integration(agent.name, AGENTS_VENDOR, {"name": agent.name}, [A2A_ASK], actor)
        except ClientError as exc:
            log.warning("bab would not take %s as an agents integration: %s", agent.name, exc)
            continue
        put.append(agent.name)
    return put


def remove_agent_integration(bab: BabClient, name: str, actor: str) -> bool:
    """`DELETE /integrations/{agent}` on destroy; bab drops the grants on it.
    True when it is gone (deleted now, or already absent), False when bab
    refused or could not be reached."""
    try:
        bab.delete_integration(name, actor)
    except ClientError as exc:
        if "no integration" in str(exc):
            return True
        log.warning("bab kept the agents integration for %s: %s", name, exc)
        return False
    return True


# ---- the picker ------------------------------------------------------------

CLOUD_LABELS = {"aws": "AWS", "gcp": "Google Cloud", "azure": "Azure"}
ACCOUNT_NOUN = {"aws": "account", "gcp": "project", "azure": "subscription"}


def _add_account(accounts: list[dict], iid: str, name: str, row: dict | None, org: str | None,
                 account_id: str | None = None) -> None:
    if any(a["id"] == iid for a in accounts):
        return
    accounts.append({"id": iid, "name": name, "connected": row is not None,
                     "ceiling": sorted(row.get("ceiling") or []) if row else [],
                     "organization": org, "account_id": account_id or (row or {}).get("account_id")})


def picker(bab: BabClient, actor: str) -> list[dict]:
    """The tree the Permissions section walks: vendor, then (for a cloud) the account,
    project or subscription, then the vendor's permissions grouped its way with the tier
    as a column. A cloud's accounts are the rows connected in bab, with their ceiling and
    under their organization when there is one; nothing else (#301): an account the hub
    can see but nobody connected, or one an org file merely named, is not offered, since
    an agent cannot be granted anything there. An organization row itself is never
    offered: a lease is for an account. A service vendor has one "account": its
    connection, or a placeholder when none is connected yet, so the vendor stays
    discoverable. Nothing above a connection's ceiling is hidden; the UI greys it with
    the reason."""
    integrations = bab.integrations(actor)
    known = {v["vendor"]: v for v in bab.vendors(actor)}
    names: list[str] = list(known)
    for i in integrations:
        if i["vendor"] not in names:
            names.append(i["vendor"])
    out = []
    for vendor in names:
        if vendor == AGENTS_VENDOR:
            # Asking another agent is requested from the Access tab's Other agents
            # section (kobo#310), not from the Permissions picker.
            continue
        meta = known.get(vendor) or {"kind": "cloud" if vendor in CLOUD_LABELS else "service",
                                      "label": CLOUD_LABELS.get(vendor, vendor), "account": ACCOUNT_NOUN.get(vendor)}
        try:
            tree = bab.catalogue(vendor, actor)
        except ClientError as exc:
            log.warning("bab catalogue for %s unavailable: %s", vendor, exc)
            tree = {"groups": [], "tiers": []}
        rows = [i for i in integrations if i["vendor"] == vendor]
        orgs = [i for i in rows if i.get("kind") == "org"]
        connected = {i["integration"]: i for i in rows if i.get("kind") != "org"}
        accounts: list[dict] = []
        if meta["kind"] == "cloud":
            for org in orgs:
                for i in rows:
                    if i.get("parent") == org["integration"]:
                        _add_account(accounts, i["integration"], i.get("name") or i["integration"], i, org["integration"])
            for iid, row in connected.items():
                if not row.get("parent"):
                    _add_account(accounts, iid, row.get("name") or iid, row, None)
        else:
            for iid, row in connected.items():
                _add_account(accounts, iid, iid, row, None)
            if not accounts:
                accounts.append({"id": vendor, "name": meta["label"], "connected": False, "ceiling": [],
                                 "organization": None, "account_id": None})
        out.append({"id": vendor, "kind": meta["kind"], "label": meta.get("label") or vendor,
                    "account_noun": meta.get("account") or ("account" if meta["kind"] == "cloud" else None),
                    "organizations": [{"id": o["integration"], "name": o.get("name") or o["integration"]} for o in orgs],
                    "accounts": accounts, "groups": tree.get("groups", []), "tiers": tree.get("tiers", [])})
    return out


def read_permissions(bab: BabClient, vendor: str, actor: str) -> list[str]:
    """Every read-tier permission the catalogue offers for a vendor: the ceiling a
    read-only connection gets (the connection's own policy is the outer wall)."""
    try:
        tree = bab.catalogue(vendor, actor)
    except ClientError:
        return []
    return sorted(p["name"] for g in tree.get("groups", []) for p in g.get("permissions", []) if p.get("tier") == "read")


def active_leases(bab: BabClient, actor: str, now: datetime | None = None) -> list[dict]:
    """Leases whose hour has not run out, newest first."""
    now = now or datetime.now(UTC)
    out = []
    for lease in bab.all_leases(now - timedelta(hours=13), actor):
        try:
            exp = datetime.fromisoformat(str(lease.get("expires_at", "")))
        except ValueError:
            continue
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=UTC)
        if exp > now:
            out.append(lease)
    return sorted(out, key=lambda x: str(x.get("issued_at")), reverse=True)


def since_days(days: int, now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)) - timedelta(days=max(1, min(int(days), 90)))


__all__ = [
    "A2A_ASK",
    "AGENTS_VENDOR",
    "ROOM_ACTOR",
    "TOKEN_ENV",
    "BabClient",
    "HttpxBabClient",
    "actor_for",
    "build_bab",
    "ensure_agent_integrations",
    "picker",
    "push_topology",
    "remove_agent_integration",
    "since_days",
    "topology_agent",
]
