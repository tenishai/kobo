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

from .clients import ClientError
from .liminal_admin import ISSUER, GatewayGrant

log = logging.getLogger(__name__)

# vendor -> (scopes, handlers) the gateway needs for an agent granted that vendor.
GATEWAY_LANES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "datadog": (("datadog:read",), ("datadog", "datadog-mcp")),
}
MANAGED_SCOPES: frozenset[str] = frozenset(s for scopes, _ in GATEWAY_LANES.values() for s in scopes)
MANAGED_HANDLERS: frozenset[str] = frozenset(h for _, handlers in GATEWAY_LANES.values() for h in handlers)

NOT_WIRED = "the gateway admin plane is not wired; the lane did not change"
UNCHANGED = "gateway lane unchanged"
REMOVED = "gateway lane removed"


def desired_lane(bab, agent_name: str, actor: str) -> tuple[set[str], set[str]]:
    """The scopes and handlers the gateway owes this agent: the union over the
    vendors of every integration it holds an active grant on. A grant on an
    integration the permission service no longer lists counts for nothing."""
    vendor_of = {i.get("integration"): i.get("vendor") for i in bab.integrations(actor)}
    held = {
        vendor_of.get(g.get("integration"))
        for g in bab.grants(agent_name, actor)
        if g.get("state", "active") == "active"
    }
    scopes: set[str] = set()
    handlers: set[str] = set()
    for vendor in held:
        lane = GATEWAY_LANES.get(vendor or "")
        if lane is None:
            continue
        scopes.update(lane[0])
        handlers.update(lane[1])
    return scopes, handlers


def follow(clients, bab, *, agent_name: str, actor: str) -> str:
    """Bring the agent's gateway grant in step with its permissions and say
    what happened, in one sentence. Scopes and handlers the room does not
    manage stay; a grant that allows every handler (None) is never narrowed."""
    admin = getattr(clients, "liminal_admin", None) if clients is not None else None
    if admin is None:
        return NOT_WIRED
    try:
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
            return UNCHANGED
        if not new_scopes and new_handlers is not None and not new_handlers:
            admin.revoke(ISSUER, agent_name)
            return REMOVED
        admin.grant(GatewayGrant(
            issuer=ISSUER, persona=agent_name, scopes=tuple(sorted(new_scopes)),
            allowed_handlers=None if new_handlers is None else tuple(sorted(new_handlers)),
        ))
        shown_handlers = "every handler" if new_handlers is None else (", ".join(sorted(new_handlers)) or "none")
        return f"gateway lane set: scopes {', '.join(sorted(new_scopes)) or 'none'}; handlers {shown_handlers}"
    except ClientError as exc:
        log.warning("gateway lane for %s did not follow its permissions: %s", agent_name, exc)
        return f"the gateway admin plane refused: {exc}"


def tally(sentence: str) -> str:
    """Which bucket a `follow` sentence falls in: `set`, `unchanged` or `failed`."""
    if sentence.startswith("gateway lane set") or sentence == REMOVED:
        return "set"
    if sentence in (UNCHANGED, NOT_WIRED):
        return "unchanged"
    return "failed"


__all__ = ["GATEWAY_LANES", "MANAGED_HANDLERS", "MANAGED_SCOPES", "desired_lane", "follow", "tally"]
