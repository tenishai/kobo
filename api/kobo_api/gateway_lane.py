"""The gateway lane follows the permission (kobo#327).

With the permission service on, an agent holds permissions, not lanes: a
grant on a Datadog connection lets it borrow a scoped application key for an
hour. The tool gateway still has to let the agent's calls through to the
vendor's handler and to the vendor's MCP target (`datadog-mcp`), and it
decides that from its own grant table: a scope per target and an allowed
handler list per persona. This module keeps the two in step, so approving
the permission is the only thing anyone grants.

`GATEWAY_LANES` says, per vendor, which scopes and handlers an agent needs on
the gateway once it holds ANY active grant on an integration of that vendor.
`follow` recomputes the managed part of the agent's gateway grant from its
grants in the permission service and writes it live through the gateway's
admin plane, leaving whatever else the grant carries (scopes and handlers the
room did not put there) alone. Best effort: it never raises, it answers one
sentence for the audit row.

Import-light on purpose: it takes the clients bag and the permission-service
client it is handed, and never imports the app.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import NamedTuple

from sqlalchemy.orm import Session

from .clients import ClientError
from .composer import update_agent_yaml_allow
from .kinds import KIND_COMPOSED
from .liminal_admin import ISSUER, GatewayGrant
from .models import Agent, AgentStatus
from .repo_lock import repo_write_lock

log = logging.getLogger(__name__)

class Lane(NamedTuple):
    """What an agent granted a vendor needs on the gateway (scopes, handlers)
    and in its own bundle (allow patterns, matched by the runtime with
    fnmatch): the gateway filters the catalog per grant, the runtime offers
    only what the bundle allows, so a permission has to open both."""

    scopes: tuple[str, ...]
    handlers: tuple[str, ...]
    allow: tuple[str, ...]


# vendor -> the lane an agent granted that vendor needs.
GATEWAY_LANES: dict[str, Lane] = {
    "datadog": Lane(scopes=("datadog:read",), handlers=("datadog", "datadog-mcp"), allow=("datadog.*", "datadog-mcp.*")),
    "github": Lane(scopes=("github:read",), handlers=("github", "github-mcp"), allow=("github.*", "github-mcp.*")),
}
MANAGED_SCOPES: frozenset[str] = frozenset(s for lane in GATEWAY_LANES.values() for s in lane.scopes)
MANAGED_HANDLERS: frozenset[str] = frozenset(h for lane in GATEWAY_LANES.values() for h in lane.handlers)
ALLOW_COMMIT = "kobo: allow the {vendor} tools"

NOT_WIRED = "the gateway admin plane is not wired; the lane did not change"
UNCHANGED = "gateway lane unchanged"
REMOVED = "gateway lane removed"


def desired_lane(bab, agent_name: str, actor: str) -> tuple[set[str], set[str]]:
    """The scopes and handlers the gateway owes this agent: the union over the
    vendors of every integration it holds an active grant on. A grant on an
    integration the permission service no longer lists counts for nothing."""
    scopes: set[str] = set()
    handlers: set[str] = set()
    for vendor in held_vendors(bab, agent_name, actor):
        lane = GATEWAY_LANES.get(vendor)
        if lane is None:
            continue
        scopes.update(lane.scopes)
        handlers.update(lane.handlers)
    return scopes, handlers


def held_vendors(bab, agent_name: str, actor: str) -> set[str]:
    """The vendors of every integration the agent holds an active grant on."""
    vendor_of = {i.get("integration"): i.get("vendor") for i in bab.integrations(actor)}
    return {
        str(vendor_of.get(g.get("integration")))
        for g in bab.grants(agent_name, actor)
        if g.get("state", "active") == "active" and vendor_of.get(g.get("integration"))
    }


def ensure_allow(session: Session, clients, *, agent_name: str, vendors: set[str]) -> str:
    """Make the agent's own bundle admit the gateway tools of every vendor in
    `vendors` (one commit, one restart) when it does not already. Returns a
    clause for the sentence: "" when nothing had to change or the agent is
    not the room's own, else what was pushed or why it could not be."""
    patterns = sorted({p for v in sorted(vendors) for p in (GATEWAY_LANES.get(v).allow if v in GATEWAY_LANES else ())})
    if not patterns:
        return ""
    agent = session.query(Agent).filter_by(name=agent_name).one_or_none()
    github = getattr(clients, "github", None) if clients is not None else None
    if agent is None or agent.kind != KIND_COMPOSED or not agent.repo_full_name or github is None:
        return ""
    if agent.status != AgentStatus.READY:
        return ""
    repo = agent.repo_full_name
    try:
        with repo_write_lock(repo), tempfile.TemporaryDirectory(prefix="kobo-allow-") as tmp:
            workdir = Path(tmp) / "repo"
            github.clone(repo, workdir)
            if not update_agent_yaml_allow(workdir / "bundle", entries=patterns):
                return ""
            github.commit_push(workdir, ALLOW_COMMIT.format(vendor=", ".join(sorted(vendors))))
    except (ClientError, OSError) as exc:
        log.warning("allow list for %s could not be updated: %s", agent_name, exc)
        return f"; the agent's allow list could not be updated: {exc}"
    return f"; the agent now allows {', '.join(patterns)} (it restarts once)"


def follow(session: Session, clients, bab, *, agent_name: str, actor: str) -> str:
    """Bring the agent's gateway grant in step with its permissions, and its
    bundle's allow list with the vendors it holds, and say what happened in
    one sentence. Scopes and handlers the room does not manage stay; a grant
    that allows every handler (None) is never narrowed; allow entries are
    only ever added (the gateway's grant is what takes a tool away)."""
    admin = getattr(clients, "liminal_admin", None) if clients is not None else None
    if admin is None:
        return NOT_WIRED
    try:
        vendors = held_vendors(bab, agent_name, actor)
        allowed = ensure_allow(session, clients, agent_name=agent_name, vendors=vendors) if vendors else ""
        scopes, handlers = desired_lane(bab, agent_name, actor)
        existing = next((g for g in admin.list_grants() if g.issuer == ISSUER and g.persona == agent_name), None)
        old_scopes = set(existing.scopes) if existing is not None else set()
        new_scopes = (old_scopes - MANAGED_SCOPES) | scopes
        old_handlers: set[str] | None
        new_handlers: set[str] | None
        if existing is not None and existing.allowed_handlers is None:
            old_handlers = new_handlers = None
        else:
            old_handlers = set(existing.allowed_handlers) if existing is not None else set()
            new_handlers = (old_handlers - MANAGED_HANDLERS) | handlers
        if new_scopes == old_scopes and new_handlers == old_handlers:
            return UNCHANGED + allowed
        if not new_scopes and new_handlers is not None and not new_handlers:
            admin.revoke(ISSUER, agent_name)
            return REMOVED
        admin.grant(GatewayGrant(
            issuer=ISSUER, persona=agent_name, scopes=tuple(sorted(new_scopes)),
            allowed_handlers=None if new_handlers is None else tuple(sorted(new_handlers)),
        ))
        shown_handlers = "every handler" if new_handlers is None else (", ".join(sorted(new_handlers)) or "none")
        return f"gateway lane set: scopes {', '.join(sorted(new_scopes)) or 'none'}; handlers {shown_handlers}{allowed}"
    except ClientError as exc:
        log.warning("gateway lane for %s did not follow its permissions: %s", agent_name, exc)
        return f"the gateway admin plane refused: {exc}"


def tally(sentence: str) -> str:
    """Which bucket a `follow` sentence falls in: `set`, `unchanged` or `failed`."""
    if sentence.startswith("gateway lane set") or sentence == REMOVED:
        return "set"
    if "could not be updated" in sentence:
        return "failed"
    if sentence == NOT_WIRED or sentence.startswith(UNCHANGED):
        return "unchanged"
    return "failed"


__all__ = ["GATEWAY_LANES", "MANAGED_HANDLERS", "MANAGED_SCOPES", "Lane", "desired_lane", "ensure_allow", "follow", "held_vendors", "tally"]
