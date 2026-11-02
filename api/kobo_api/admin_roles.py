"""Roles for the admin panel (design 2026-10-18, Arc 2 step 3): a named set
of permissions a group holds. The lists a role may hold (capabilities, admin
permissions) live here as the catalogue the pane renders, so a new one is a
line in this file and a body change, never a migration. Four built-ins are
seeded the first time the pane asks, from the org keys that already say who
runs the room; custom roles are made from the same lists.

Phase 1: kobo stores and shows roles and enforces what it already enforces
from them. Phase 2 mirrors them into bab as tuples (tenishai/bab#8).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from . import admin_store
from .config import Config, ModelChoice
from .models import AdminIntegration

CAPABILITIES: list[dict[str, str]] = [
    {"id": "create_agents", "label": "Create agents", "desc": "Build an agent from the catalogs."},
    {"id": "connect_repos", "label": "Connect repositories", "desc": "Bring a repository the team already owns."},
    {"id": "chat_teach", "label": "Chat and teach", "desc": "Talk to visible agents and put facts in their memory."},
    {"id": "manage_secrets", "label": "Manage secrets", "desc": "Set an agent's secrets and environment."},
    {"id": "wire_slack", "label": "Wire Slack", "desc": "Create and attach an agent's Slack app."},
    {"id": "invite_colleagues", "label": "Invite colleagues", "desc": "Accept cross-group colleague invites."},
    {"id": "destroy_agents", "label": "Destroy agents", "desc": "Tear an agent down."},
]

ADMIN_PERMISSIONS: list[dict[str, str]] = [
    {"id": "manage_integrations", "label": "Manage integrations", "desc": "Connect and disconnect vendors."},
    {"id": "approve_connections", "label": "Approve connections", "desc": "Grant an agent a lane to a vendor."},
    {"id": "manage_roles", "label": "Manage roles", "desc": "Create roles and assign groups."},
    {"id": "manage_people", "label": "Manage people", "desc": "Admins, operators, readers, group requests."},
    {"id": "view_audit", "label": "View audit", "desc": "Read who changed what."},
]

BUILTIN_IDS = ("admin", "operator", "member", "reader")


def catalogue(config: Config, integrations: list[AdminIntegration], models: list[ModelChoice] | None = None) -> dict[str, Any]:
    """Everything the role editor offers: the two permission lists, the
    room's models (`models`: every model the room knows, from
    `room_models.known_entries`, kobo#405; the org list when not given),
    its groups, and the connected integrations a ceiling can name."""
    return {
        "capabilities": CAPABILITIES,
        "admin": ADMIN_PERMISSIONS,
        "models": [{"id": m.id, "label": m.label} for m in (models if models is not None else config.models)],
        "groups": sorted({g.name for g in config.groups} | set(config.admin_groups) | set(config.reader_groups)),
        "connectors": [{"id": i.id, "name": i.name, "base_access": i.base_access} for i in integrations],
    }


def seed_builtin_roles(session: Session, config: Config, *, actor: str = "room") -> int:
    """Create the four built-ins that do not exist yet, from the org keys
    that already name who runs the room. Idempotent; returns how many were
    made. Never touches a role that exists, so an admin's edits stand."""
    # Local import: room_models imports this package's admin_store too.
    from kobo_api.room_models import known_entries

    existing = {r.id for r in admin_store.list_roles(session)}
    all_caps = [c["id"] for c in CAPABILITIES]
    all_admin = [p["id"] for p in ADMIN_PERMISSIONS]
    all_models = [m.id for m in known_entries(session, config)]
    seeds = {
        "admin": ("Admin", "Runs the room: every capability and every admin permission.", list(config.admin_groups),
                  {"capabilities": all_caps, "admin": all_admin, "models": all_models, "connectors": []}),
        "operator": ("Operator", "Builds and runs agents for a team; may approve its own team's lanes.", [],
                     {"capabilities": all_caps, "admin": ["approve_connections", "view_audit"], "models": all_models, "connectors": []}),
        "member": ("Member", "Creates and teaches agents in their own group.", [],
                   {"capabilities": ["create_agents", "chat_teach", "manage_secrets", "wire_slack", "invite_colleagues"],
                    "admin": [], "models": all_models, "connectors": []}),
        "reader": ("Reader", "Sees every agent, changes nothing.", list(config.reader_groups),
                   {"capabilities": [], "admin": ["view_audit"], "models": [], "connectors": []}),
    }
    made = 0
    for role_id in BUILTIN_IDS:
        if role_id in existing:
            continue
        name, description, groups, spec = seeds[role_id]
        admin_store.put_role(session, name=name, description=description, groups=groups, spec=spec,
                             actor=actor, role_id=role_id, builtin=True)
        made += 1
    return made
