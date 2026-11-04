"""The gateway lane follows the permission (kobo#327, per permission since kobo#365).

With the permission service on, an agent holds permissions, not lanes: a
grant on a Datadog connection lets it borrow a scoped application key for an
hour; a grant on its GitHub repository set lets it borrow an installation
token narrowed to that set. The tool gateway still has to let the agent's
calls through to the vendor's handler and to the vendor's MCP target, and it
decides that from its own grant table: a scope per target and an allowed
handler list per persona. This module keeps the two in step, so approving
the permission is the only thing anyone grants.

Two tables. `GATEWAY_LANES` says, per vendor, which scopes and handlers an
agent needs once it holds ANY active grant on an integration of that vendor
(Datadog). `PERMISSION_LANES` says it per (vendor, permission), for a vendor
whose levels open different targets (GitHub: Read opens the read-only
`github-mcp` target, Review pull requests and Open pull requests open the
`github-mcp-write` target, the second with the `github:write` scope the
branch- and pull-request-creating tools need). `follow` recomputes the
managed part of the agent's gateway grant from its grants in the permission
service and writes it live through the gateway's admin plane, leaving
whatever else the grant carries (scopes and handlers the room did not put
there) alone, and brings the agent's own bundle allow list in step both
ways: a pattern the agent now earns is added, one it no longer earns is
withdrawn (one commit, one restart). Best effort: it never raises, it
answers one sentence for the audit row.

Import-light on purpose: it takes the clients bag and the permission-service
client it is handed, and never imports the app.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import NamedTuple

import yaml
from sqlalchemy.orm import Session

from . import delivery
from .clients import ClientError
from .composer import update_agent_yaml_allow
from .kinds import KIND_COMPOSED
from .liminal_admin import ISSUER, GatewayGrant
from .models import Agent, AgentStatus
from .repo_lock import repo_write_lock

log = logging.getLogger(__name__)

class Lane(NamedTuple):
    """What an agent granted a vendor (or one of its permissions) needs on the
    gateway (scopes, handlers) and in its own bundle (allow patterns, matched by
    the runtime with fnmatch): the gateway filters the catalog per grant, the
    runtime offers only what the bundle allows, so a permission has to open both."""

    scopes: tuple[str, ...]
    handlers: tuple[str, ...]
    allow: tuple[str, ...]


# vendor -> the lane an agent granted ANY permission of that vendor needs.
GATEWAY_LANES: dict[str, Lane] = {
    "datadog": Lane(scopes=("datadog:read",), handlers=("datadog", "datadog-mcp"), allow=("datadog.*", "datadog-mcp.*")),
    # Atlassian (2026-11-02): one shared credential on the gateway's `atlassian` target
    # (`required_scope atlassian:read`, admin_integrations.target_spec); the grant opens the
    # scope and the handler for the agent, nothing is leased.
    "atlassian": Lane(scopes=("atlassian:read",), handlers=("atlassian",), allow=("atlassian.*",)),
}
# (vendor, permission) -> the lane that one permission needs; wins over the
# vendor table for its vendor (kobo#365: GitHub's three levels).
PERMISSION_LANES: dict[tuple[str, str], Lane] = {
    ("github", "github_repos_read"): Lane(scopes=("github:read",), handlers=("github-mcp",), allow=("github-mcp.*",)),
    ("github", "github_pulls_review"): Lane(scopes=("github:review",), handlers=("github-mcp-write",),
                                            allow=("github-mcp-write.*",)),
    ("github", "github_pulls_write"): Lane(scopes=("github:review", "github:write"), handlers=("github-mcp-write",),
                                           allow=("github-mcp-write.*",)),
    # Atlassian's one writing level (2026-11-03): the target's Jira writing tools sit behind
    # `atlassian:jira-write` (admin_integrations.TARGET_EXTRAS); its read permissions keep the
    # vendor lane above. The shared credential is the outer wall, so a read-only connection
    # never offers this permission (its ceiling is the read tier).
    ("atlassian", "atlassian_jira_issues_write"): Lane(scopes=("atlassian:read", "atlassian:jira-write"),
                                                       handlers=("atlassian",), allow=("atlassian.*",)),
}
# What the room used to put there and no lane earns any more: withdrawn on the
# next `follow` (the gateway's `github` handler and the broker sidecar's tools,
# kobo#365). Listed here so `follow` keeps managing them until they are gone.
RETIRED_HANDLERS: tuple[str, ...] = ("github",)
RETIRED_ALLOW: dict[str, str] = {"github.*": "github"}
# Allow entries named one by one that the room's own tools used to need and that nothing serves
# any more: the token broker's `github.<tool>` entries (github.search_prs, github.open_pr, ...)
# answer HTTP 405 now that the broker is gone, and the room's own Jira read lane (jira.get_issue,
# jira.find_issues) retired on 2026-11-04 for the Atlassian connection. `sync_allow` withdraws any
# entry under one of these prefixes that no lane earns, on the next sync. "github-mcp." does not
# match "github.".
RETIRED_PREFIXES: tuple[str, ...] = ("github.", "jira.")

_ALL_LANES: list[tuple[str, Lane]] = [*GATEWAY_LANES.items(), *((v, lane) for (v, _), lane in PERMISSION_LANES.items())]
MANAGED_SCOPES: frozenset[str] = frozenset(s for _, lane in _ALL_LANES for s in lane.scopes)
MANAGED_HANDLERS: frozenset[str] = frozenset(h for _, lane in _ALL_LANES for h in lane.handlers) | frozenset(RETIRED_HANDLERS)
# allow pattern -> the vendor it belongs to, for the commit message.
ALLOW_VENDOR: dict[str, str] = {**{p: v for v, lane in _ALL_LANES for p in lane.allow}, **RETIRED_ALLOW}
MANAGED_ALLOW: frozenset[str] = frozenset(ALLOW_VENDOR)
ALLOW_COMMIT = "kobo: allow the {vendor} tools"
WITHDRAW_COMMIT = "kobo: withdraw the {vendor} tools"

NOT_WIRED = "the gateway admin plane is not wired; the lane did not change"
UNCHANGED = "gateway lane unchanged"
REMOVED = "gateway lane removed"


def lane_for(vendor: str, permission: str) -> Lane | None:
    """The lane one active grant earns: the permission's own when the vendor
    is per permission, else the vendor's; None for a vendor with no lane."""
    return PERMISSION_LANES.get((vendor, permission)) or GATEWAY_LANES.get(vendor)


def held_lanes(bab, agent_name: str, actor: str) -> list[Lane]:
    """The lanes of every (vendor, permission) the agent holds an active grant
    on. A grant on an integration the permission service no longer lists
    counts for nothing."""
    vendor_of = {i.get("integration"): i.get("vendor") for i in bab.integrations(actor)}
    lanes: list[Lane] = []
    for g in bab.grants(agent_name, actor):
        if g.get("state", "active") != "active":
            continue
        vendor = vendor_of.get(g.get("integration"))
        if not vendor:
            continue
        lane = lane_for(str(vendor), str(g.get("permission") or ""))
        if lane is not None and lane not in lanes:
            lanes.append(lane)
    return lanes


def desired_lane(bab, agent_name: str, actor: str) -> tuple[set[str], set[str]]:
    """The scopes and handlers the gateway owes this agent: the union over the
    lanes of every (vendor, permission) it holds an active grant on."""
    scopes: set[str] = set()
    handlers: set[str] = set()
    for lane in held_lanes(bab, agent_name, actor):
        scopes.update(lane.scopes)
        handlers.update(lane.handlers)
    return scopes, handlers


def desired_allow(lanes: list[Lane]) -> set[str]:
    """The bundle allow patterns the agent's lanes earn."""
    return {p for lane in lanes for p in lane.allow}


def held_vendors(bab, agent_name: str, actor: str) -> set[str]:
    """The vendors of every integration the agent holds an active grant on."""
    vendor_of = {i.get("integration"): i.get("vendor") for i in bab.integrations(actor)}
    return {
        str(vendor_of.get(g.get("integration")))
        for g in bab.grants(agent_name, actor)
        if g.get("state", "active") == "active" and vendor_of.get(g.get("integration"))
    }


def _vendors_of(patterns: set[str]) -> str:
    return ", ".join(sorted({ALLOW_VENDOR.get(p, p.split(".", 1)[0]) for p in patterns}))


def sync_allow(session: Session, clients, *, agent_name: str, patterns: set[str]) -> str:
    """Make the agent's own bundle admit exactly the managed patterns in
    `patterns` (one commit, one restart): the ones it lacks are added, the
    managed ones it carries and no longer earns are withdrawn; entries the
    room never put there stay. Returns a clause for the sentence: "" when
    nothing had to change or the agent is not the room's own, else what was
    pushed or why it could not be."""
    agent = session.query(Agent).filter_by(name=agent_name).one_or_none()
    github = getattr(clients, "github", None) if clients is not None else None
    if agent is None or agent.kind != KIND_COMPOSED or not agent.repo_full_name or github is None:
        return ""
    if agent.status != AgentStatus.READY:
        return ""
    wanted = sorted(patterns & MANAGED_ALLOW)
    withdraw = sorted(MANAGED_ALLOW - patterns)
    repo = agent.repo_full_name
    try:
        with repo_write_lock(repo), tempfile.TemporaryDirectory(prefix="kobo-allow-") as tmp:
            workdir = Path(tmp) / "repo"
            github.clone(repo, workdir)
            before = _allow_entries(workdir / "bundle")
            withdraw = sorted(set(withdraw) | {p for p in before if p.startswith(RETIRED_PREFIXES) and p not in patterns})
            if not update_agent_yaml_allow(workdir / "bundle", entries=wanted, remove=withdraw):
                return ""
            added = [p for p in wanted if p not in before]
            gone = [p for p in withdraw if p in before]
            delivery.push(github, workdir, _commit_message(added, gone))
    except (ClientError, OSError) as exc:
        log.warning("allow list for %s could not be updated: %s", agent_name, exc)
        return f"; the agent's allow list could not be updated: {exc}"
    said = []
    if added:
        said.append(f"now allows {', '.join(added)}")
    if gone:
        said.append(f"no longer allows {', '.join(gone)}")
    return f"; the agent {' and '.join(said)} (it restarts once)"


def _commit_message(added: list[str], gone: list[str]) -> str:
    """One commit for both halves: "kobo: allow the github tools",
    "kobo: withdraw the github tools", or both joined when a level change
    opens one target and closes another."""
    parts = []
    if added:
        parts.append(ALLOW_COMMIT.format(vendor=_vendors_of(set(added))))
    if gone:
        parts.append(WITHDRAW_COMMIT.format(vendor=_vendors_of(set(gone))) if not added
                     else f"withdraw the {_vendors_of(set(gone))} tools")
    return "; ".join(parts)


def _allow_entries(bundle_dir: Path) -> list[str]:
    """The bundle's allow list as it stands, [] when it cannot be read (the
    editor raises its own `ClientError` for the sentence)."""
    try:
        data = yaml.safe_load((bundle_dir / "agent.yaml").read_text())
        return [str(p) for p in ((data or {}).get("tools") or {}).get("allow") or []]
    except (OSError, yaml.YAMLError, AttributeError):
        return []


def _owed_grant(existing: GatewayGrant | None, lanes: list[Lane]):
    """(old scopes, old handlers, new scopes, new handlers) for one agent:
    the managed part recomputed from its lanes, everything else kept. A
    grant that allows every handler (None) is never narrowed."""
    scopes = {s for lane in lanes for s in lane.scopes}
    handlers = {h for lane in lanes for h in lane.handlers}
    old_scopes = set(existing.scopes) if existing is not None else set()
    new_scopes = (old_scopes - MANAGED_SCOPES) | scopes
    old_handlers: set[str] | None
    new_handlers: set[str] | None
    if existing is not None and existing.allowed_handlers is None:
        old_handlers = new_handlers = None
    else:
        old_handlers = set(existing.allowed_handlers) if existing is not None else set()
        new_handlers = (old_handlers - MANAGED_HANDLERS) | handlers
    return old_scopes, old_handlers, new_scopes, new_handlers


def follow(session: Session, clients, bab, *, agent_name: str, actor: str) -> str:
    """Bring the agent's gateway grant in step with its permissions, and its
    bundle's allow list with the lanes it holds, and say what happened in
    one sentence. Scopes and handlers the room does not manage stay; a grant
    that allows every handler (None) is never narrowed; allow entries the
    room manages are added and withdrawn as the lanes say, others stay."""
    admin = getattr(clients, "liminal_admin", None) if clients is not None else None
    if admin is None:
        return NOT_WIRED
    try:
        lanes = held_lanes(bab, agent_name, actor)
        allowed = sync_allow(session, clients, agent_name=agent_name, patterns=desired_allow(lanes))
        existing = next((g for g in admin.list_grants() if g.issuer == ISSUER and g.persona == agent_name), None)
        old_scopes, old_handlers, new_scopes, new_handlers = _owed_grant(existing, lanes)
        if new_scopes == old_scopes and new_handlers == old_handlers:
            return UNCHANGED + allowed
        if not new_scopes and new_handlers is not None and not new_handlers:
            admin.revoke(ISSUER, agent_name)
            return REMOVED + allowed
        admin.grant(GatewayGrant(
            issuer=ISSUER, persona=agent_name, scopes=tuple(sorted(new_scopes)),
            allowed_handlers=None if new_handlers is None else tuple(sorted(new_handlers)),
        ))
        shown_handlers = "every handler" if new_handlers is None else (", ".join(sorted(new_handlers)) or "none")
        return f"gateway lane set: scopes {', '.join(sorted(new_scopes)) or 'none'}; handlers {shown_handlers}{allowed}"
    except ClientError as exc:
        log.warning("gateway lane for %s did not follow its permissions: %s", agent_name, exc)
        return f"the gateway admin plane refused: {exc}"


# How often the room checks the gateway still holds what it granted. Something
# other than the room can rewrite a grant: the gateway's own boot seed
# (grants.toml) re-applies a file stanza over the live row on every restart,
# which took ledger's GitHub lane away for a day (2026-11-04).
HEAL_INTERVAL_S = 300.0


def heal(clients, bab, agent_names: list[str], actor: str) -> list[str]:
    """Set each agent's gateway grant back to what its permissions owe when
    something else has changed it, and name the agents it set back. Only the
    gateway grant: the bundle allow list lives in the agent's repository,
    which nothing but the room writes. An agent holding no permission is
    left alone, so an agent whose grant lives only in the gateway's own
    file keeps it. Best effort: one agent's failure never stops the rest."""
    admin = getattr(clients, "liminal_admin", None) if clients is not None else None
    if admin is None or bab is None:
        return []
    try:
        live = {g.persona: g for g in admin.list_grants() if g.issuer == ISSUER}
    except ClientError as exc:
        log.warning("gateway grants could not be read for the heal pass: %s", exc)
        return []
    healed: list[str] = []
    for name in agent_names:
        try:
            lanes = held_lanes(bab, name, actor)
            if not lanes:
                continue
            existing = live.get(name)
            old_scopes, old_handlers, new_scopes, new_handlers = _owed_grant(existing, lanes)
            if new_scopes == old_scopes and new_handlers == old_handlers:
                continue
            admin.grant(GatewayGrant(
                issuer=ISSUER, persona=name, scopes=tuple(sorted(new_scopes)),
                allowed_handlers=None if new_handlers is None else tuple(sorted(new_handlers)),
            ))
        except ClientError as exc:
            log.warning("gateway grant for %s could not be checked or set back: %s", name, exc)
            continue
        log.warning(
            "gateway grant for %s had drifted from its permissions (scopes %s, handlers %s); set back to scopes %s, handlers %s",
            name, sorted(old_scopes), "all" if old_handlers is None else sorted(old_handlers),
            sorted(new_scopes), "all" if new_handlers is None else sorted(new_handlers),
        )
        healed.append(name)
    return healed


def _heal_ready_agents(app) -> list[str]:
    from .bab import ROOM_ACTOR

    with app.state.session_factory() as session:
        names = [a.name for a in session.query(Agent).filter(Agent.status == AgentStatus.READY).all()]
    return heal(app.state.clients, getattr(app.state.clients, "bab", None), names, ROOM_ACTOR)


async def heal_loop(app, *, interval_s: float = HEAL_INTERVAL_S) -> None:
    """`heal` over every ready agent, every `interval_s` for the life of the
    process. Sleeps first: the gateway was in step at boot a minute ago as
    often as not, and a restart of either side is caught one interval later."""
    import asyncio

    while True:
        await asyncio.sleep(interval_s)
        try:
            await asyncio.to_thread(_heal_ready_agents, app)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("gateway grant heal pass failed")


def tally(sentence: str) -> str:
    """Which bucket a `follow` sentence falls in: `set`, `unchanged` or `failed`."""
    if sentence.startswith(("gateway lane set", REMOVED)):
        return "set"
    if "could not be updated" in sentence:
        return "failed"
    if sentence == NOT_WIRED or sentence.startswith(UNCHANGED):
        return "unchanged"
    return "failed"


__all__ = [
    "GATEWAY_LANES", "HEAL_INTERVAL_S", "MANAGED_ALLOW", "MANAGED_HANDLERS", "MANAGED_SCOPES", "PERMISSION_LANES", "RETIRED_HANDLERS",
    "RETIRED_PREFIXES", "Lane", "desired_allow", "desired_lane", "follow", "heal", "heal_loop", "held_lanes", "held_vendors", "lane_for",
    "sync_allow", "tally",
]
