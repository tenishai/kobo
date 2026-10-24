"""Connections: an agent's lanes to integrations, applied to the tool gateway
(design 2026-10-18 Arc 2 step 3; the room holds its own gateway admin token,
Kyle 2026-10-24).

A lane is a row in `admin_grants` (the truth). Applying it means two
projections, each reported back on the row and its audit event:

1. **Live, through the gateway's admin plane**, when the org names one: the
   persona's whole grant is recomputed from its non-revoked rows (scopes
   `<integration>:<access>`, allowed handlers the integration ids) and merged
   with the handlers the gateway already holds for that persona (the room's
   own birthright handlers, `scratchpad` and friends), then written. One
   write per agent, never per lane, because a grant replaces the row.
2. **Durable, as a pull request on the gateway's config repository**,
   rewriting that persona's `[[grant]]` block in `grants.toml` so the next
   image carries it. Absent a repository the row says so.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

from sqlalchemy.orm import Session

from . import admin_store
from .admin_integrations import _gateway_branch_pr
from .clients import ClientError, Clients
from .config import Config
from .liminal_admin import ISSUER, GatewayGrant, LiminalAdminClient
from .models import AdminGrant

log = logging.getLogger(__name__)

GRANTS_FILE = "grants.toml"
BRANCH_PREFIX = "kobo/lane-"


def desired_grant(session: Session, agent_name: str) -> tuple[list[str], list[str]]:
    """(scopes, handlers) from the agent's live lanes, sorted."""
    rows = [g for g in admin_store.list_grants(session, agent_name=agent_name) if g.state != admin_store.STATE_REVOKED]
    scopes = sorted({f"{g.integration_id}:{g.access}" for g in rows})
    handlers = sorted({g.integration_id for g in rows})
    return scopes, handlers


def merged_grant(existing: GatewayGrant | None, scopes: list[str], handlers: list[str], integrations: set[str]) -> GatewayGrant:
    """Keep every scope and handler the gateway holds that is not about an
    integration the room manages, then set the room's lanes over it."""
    keep_scopes = [s for s in (existing.scopes if existing else ()) if s.split(":", 1)[0] not in integrations]
    keep_handlers = [h for h in (existing.allowed_handlers or ()) if h not in integrations] if existing and existing.allowed_handlers is not None else []
    all_scopes = sorted(set(keep_scopes) | set(scopes))
    all_handlers = sorted(set(keep_handlers) | set(handlers))
    return GatewayGrant(issuer=ISSUER, persona=existing.persona if existing else "", scopes=tuple(all_scopes), allowed_handlers=tuple(all_handlers))


def grants_toml_with(text: str, persona: str, scopes: list[str], handlers: list[str]) -> str:
    """`grants.toml` with this persona's block replaced (or appended, or
    removed when it has no lanes left and the room wrote the block)."""
    block_re = re.compile(r'\n?\[\[grant\]\]\nissuer = "[^"]*"\npersona = "' + re.escape(persona) + r'"\n(?:[a-z_]+ = [^\n]*\n)*')
    stripped = block_re.sub("", text)
    if not scopes and not handlers:
        return stripped
    block = (
        f'\n[[grant]]\nissuer = "{ISSUER}"\npersona = "{persona}"\n'
        f'scopes = [{", ".join(chr(34) + s + chr(34) for s in scopes)}]\n'
        f'allowed_handlers = [{", ".join(chr(34) + h + chr(34) for h in handlers)}]\n'
    )
    return stripped.rstrip("\n") + "\n" + block


def apply_agent_lanes(session: Session, clients: Clients | None, config: Config, *, agent_name: str, actor: str) -> dict[str, str]:
    """Push the agent's lanes to both projections and report each as a
    sentence; the rows' states follow. Never raises past a logged failure."""
    scopes, handlers = desired_grant(session, agent_name)
    integrations = {i.id for i in admin_store.list_integrations(session)}
    outcome = {"live": "no admin plane configured; lanes land with the pull request", "file": "no gateway config repository configured"}
    live_ok = None

    admin: LiminalAdminClient | None = getattr(clients, "liminal_admin", None) if clients is not None else None
    if admin is not None:
        try:
            existing = next((g for g in admin.list_grants() if g.issuer == ISSUER and g.persona == agent_name), None)
            merged = merged_grant(existing, scopes, handlers, integrations)
            merged = GatewayGrant(issuer=ISSUER, persona=agent_name, scopes=merged.scopes, allowed_handlers=merged.allowed_handlers)
            if merged.scopes or merged.allowed_handlers:
                admin.grant(merged)
            elif existing is not None:
                admin.revoke(ISSUER, agent_name)
            outcome["live"] = "live at the gateway"
            live_ok = True
        except ClientError as exc:
            outcome["live"] = f"the gateway admin plane refused: {exc}"
            live_ok = False

    if clients is not None and config.gateway_config_repo:
        try:
            # One branch per distinct grant state: a later change gets its own
            # pull request rather than silently reusing the one already open
            # for an earlier state. The gateway owner merges the newest.
            state_hash = hashlib.sha256(f"{scopes}|{handlers}".encode()).hexdigest()[:8]
            pr = _gateway_branch_pr(
                clients.github, config,
                branch=f"{BRANCH_PREFIX}{agent_name}-{state_hash}",
                title=f"kobo: {agent_name}'s lanes",
                body=f"Opened by the room's admin panel for {actor}. Sets {agent_name}'s grant: scopes {scopes or 'none'}, handlers {handlers or 'none'}.",
                edit=lambda workdir: _edit_grants(workdir, agent_name, scopes, handlers),
            )
            outcome["file"] = f"pull request open on the gateway: {pr}" if pr else "grants file already carries these lanes"
        except (ClientError, OSError) as exc:
            outcome["file"] = f"the gateway pull request failed: {exc}"

    for g in admin_store.list_grants(session, agent_name=agent_name):
        if g.state == admin_store.STATE_REVOKED:
            continue
        state = admin_store.STATE_APPLIED if live_ok else (admin_store.STATE_FAILED if live_ok is False else admin_store.STATE_PENDING)
        admin_store.set_grant_state(session, g.id, state=state, detail=f"{outcome['live']}; {outcome['file']}", spec={"pull_request": outcome["file"]} if "pull request open" in outcome["file"] else None)
    return outcome


def _edit_grants(workdir: Path, persona: str, scopes: list[str], handlers: list[str]) -> bool:
    target = workdir / GRANTS_FILE
    current = target.read_text() if target.exists() else ""
    updated = grants_toml_with(current, persona, scopes, handlers)
    if updated == current:
        return False
    target.write_text(updated)
    return True


def connections_view(session: Session, agents: list) -> list[dict]:
    """Every agent, every lane: the pane's rows. `agents` are the room's
    Agent rows (name, group); 'no lane' is a visible state, not an absence."""
    lanes_by_agent: dict[str, list[AdminGrant]] = {}
    for g in admin_store.list_grants(session):
        lanes_by_agent.setdefault(g.agent_name, []).append(g)
    return [
        {
            "agent": a.name,
            "group": getattr(a, "group_name", None) or getattr(a, "team", None),
            "lanes": [admin_store.grant_dict(g) for g in lanes_by_agent.get(a.name, [])],
        }
        for a in agents
    ]
