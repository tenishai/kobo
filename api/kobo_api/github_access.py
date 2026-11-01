"""GitHub per agent (kobo#365): one repository set, then a level.

The room's GitHub connection (Admin > Integrations) is the App the permission
service mints from; the room acts as itself with it and it is never granted to
an agent. What an agent is granted is a CHILD of that connection in the
permission service, `<connection>-<agent>` with `parent = <connection>` and
`spec.repositories`: the one installation token the agent borrows is narrowed
to that set, so one set per agent, and the three catalogue rows on the
`github` vendor (`github_repos_read`, `github_pulls_review`,
`github_pulls_write`) are the levels the owner asks for on it.

This module is the room's side of that: find the connection, list the
installation's repositories through the room App, write or replace the
child, and the one-time move of the retired PR broker's `RepoAccess` rows
onto it at boot. Routes in `main.py` call it; it never imports the app.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from . import audit, gateway_lane
from .bab import ROOM_ACTOR, BabClient, all_permissions, child_id, child_repositories
from .clients import ClientError, InstallationRepo
from .models import REPO_ACCESS_APPROVED, AdminIntegration, RepoAccess

log = logging.getLogger(__name__)

GITHUB_VENDOR = "github"
REPOS_READ = "github_repos_read"
PULLS_REVIEW = "github_pulls_review"
PULLS_WRITE = "github_pulls_write"
"""The three levels, as the catalogue names them (labels Read, Review pull
requests, Open pull requests come from bab)."""

MAX_REPOSITORIES = 200
REPOSITORY_RE = re.compile(r"[\w.-]+/[\w.-]+")

NO_CONNECTION = "no GitHub connection: a room admin adds GitHub under Admin > Integrations first"
LEASES_NOTE = ("the permission service has no call to end an open lease; "
               "a token already out keeps the old set until its hour runs out")
"""bab 0.4.12 exposes POST /leases, POST /leases/for-agent and GET /leases and
nothing that ends one early (checked 2026-11-01); the audit row says so."""

MIGRATION_NOTE = "migrated from repository access (the retired pull request broker)"


@dataclass(frozen=True)
class Connection:
    """The room's GitHub connection as the child needs it: its id in the
    permission service, its label, and the installation the child copies."""

    id: str
    name: str
    installation_id: str
    account: str | None


def connection(bab: BabClient, actor: str, session: Session | None) -> Connection | None:
    """The room's GitHub connection, or None when GitHub is not connected. The
    installation id and account come from the room's own row (its identity)
    first, the service's spec otherwise; the label is the row's name."""
    rows = [i for i in bab.integrations(actor)
            if i.get("vendor") == GITHUB_VENDOR and i.get("kind") != "org" and not i.get("parent")]
    if not rows:
        return None
    row = min(rows, key=lambda i: str(i.get("integration")))
    admin_row = session.get(AdminIntegration, row["integration"]) if session is not None else None
    identity = ((admin_row.spec or {}).get("identity") or {}) if admin_row is not None else {}
    spec = row.get("spec") or {}
    return Connection(
        id=str(row["integration"]),
        name=str((admin_row.name if admin_row is not None else None) or row.get("name") or row["integration"]),
        installation_id=str(identity.get("installation_id") or spec.get("installation_id") or ""),
        account=identity.get("account") or spec.get("account") or None,
    )


def child(bab: BabClient, actor: str, connection_id: str, agent: str) -> dict | None:
    """The agent's child under the connection, as bab lists it, or None."""
    wanted = child_id(connection_id, agent)
    for row in bab.integrations(actor):
        if row.get("integration") == wanted and row.get("parent") == connection_id:
            return row
    return None


def installation_repositories(clients) -> list[InstallationRepo]:
    """Every repository the room App's installation can see, by name. Raises
    `ClientError` when the room holds no GitHub client or GitHub refused."""
    github = getattr(clients, "github", None) if clients is not None else None
    if github is None:
        raise ClientError("this room holds no GitHub client to list the installation with")
    return sorted(github.list_installation_repos(), key=lambda r: r.full_name)


def strays(repositories: list[str], available: list[InstallationRepo]) -> list[str]:
    """The names asked for that the installation does not carry."""
    known = {r.full_name for r in available}
    return sorted({r for r in repositories if r not in known})


def child_spec(conn: Connection, agent: str, repositories: list[str]) -> dict:
    """What the child carries in the permission service: the set, a name the
    Review pane shows, and the installation copied from the connection."""
    spec = {"repositories": sorted(set(repositories)), "name": f"{conn.name} for {agent}",
            "installation_id": conn.installation_id}
    if conn.account:
        spec["account"] = conn.account
    return spec


def write_child(bab: BabClient, actor: str, conn: Connection, agent: str, repositories: list[str]) -> tuple[str, bool]:
    """Write or replace the agent's child with `repositories`. Returns the
    child's id and whether the set changed. The ceiling is every github
    catalogue permission: the levels are the owner's to ask and Review's to
    decide, not a wall the room adds."""
    existing = child(bab, actor, conn.id, agent)
    before = child_repositories(existing)
    spec = child_spec(conn, agent, repositories)
    changed = existing is None or before != spec["repositories"]
    cid = child_id(conn.id, agent)
    bab.put_integration(cid, GITHUB_VENDOR, spec, all_permissions(bab, GITHUB_VENDOR, actor), actor, parent=conn.id)
    return cid, changed


def active_grants(bab: BabClient, actor: str, agent: str, integration: str) -> list[dict]:
    return [g for g in bab.grants(agent, actor)
            if g.get("integration") == integration and g.get("state", "active") == "active"]


def end_open_leases(bab: BabClient, actor: str, agent: str, integration: str) -> str:
    """What happened to the leases out on the child after its set changed:
    nothing can be done from here (see `LEASES_NOTE`), so the sentence says
    so and the audit row carries it."""
    return LEASES_NOTE


# ---- the one-time move of RepoAccess rows (kobo#365) -------------------------


def migrate_repo_access(session: Session, clients, bab: BabClient, *, actor: str = ROOM_ACTOR,
                        now: datetime | None = None) -> dict:
    """Every agent with approved `RepoAccess` rows not yet stamped `migrated_at`
    gets a child under the GitHub connection with those repositories and an
    approved `github_pulls_write` grant (what the broker's token let it do),
    its gateway lane brought in step, the rows stamped, one audit row per
    agent. Idempotent: a stamped row is never read again; an agent that
    already picked a set keeps it (its rows are stamped, nothing is granted).
    Without a GitHub connection the rows wait for the next boot. Never raises
    past a single agent: a refusal is logged and that agent's rows stay for
    the next boot."""
    rows = (session.query(RepoAccess)
            .filter(RepoAccess.status == REPO_ACCESS_APPROVED, RepoAccess.migrated_at.is_(None))
            .order_by(RepoAccess.agent_name, RepoAccess.repository).all())
    if not rows:
        return {"agents": 0, "migrated": 0, "kept": 0, "waiting": False}
    try:
        conn = connection(bab, actor, session)
    except ClientError as exc:
        log.warning("repository access migration: the permission service is unreachable: %s", exc)
        return {"agents": 0, "migrated": 0, "kept": 0, "waiting": True}
    if conn is None:
        log.info("repository access migration: %d approved row(s) wait for a GitHub connection", len(rows))
        return {"agents": 0, "migrated": 0, "kept": 0, "waiting": True}
    stamp = now or datetime.now(UTC)
    by_agent: dict[str, list[RepoAccess]] = {}
    for row in rows:
        by_agent.setdefault(row.agent_name, []).append(row)
    out = {"agents": len(by_agent), "migrated": 0, "kept": 0, "waiting": False}
    for agent_name, agent_rows in by_agent.items():
        repositories = sorted({r.repository for r in agent_rows})
        try:
            existing = child(bab, actor, conn.id, agent_name)
            if existing is not None:
                kept = child_repositories(existing)
                detail = {"integration": existing["integration"], "repositories": kept, "rows": repositories}
                summary = (f"the room kept {agent_name}'s repository set ({len(kept)} repositories) and retired "
                           f"its {len(repositories)} approved broker row(s)")
                out["kept"] += 1
            else:
                cid, _ = write_child(bab, actor, conn, agent_name, repositories)
                if not any(g.get("permission") == PULLS_WRITE for g in active_grants(bab, actor, agent_name, cid)):
                    asked = bab.request(agent_name, cid, [PULLS_WRITE], MIGRATION_NOTE, actor)
                    bab.decide([asked["id"]], "approve", None, actor)
                lane = gateway_lane.follow(session, clients, bab, agent_name=agent_name, actor=actor)
                detail = {"integration": cid, "repositories": repositories, "permission": PULLS_WRITE, "gateway_lane": lane}
                summary = (f"the room moved {agent_name}'s repository access onto GitHub: {len(repositories)} "
                           f"repositories, Open pull requests")
                out["migrated"] += 1
        except ClientError as exc:
            log.warning("repository access migration for %s failed; its rows wait for the next boot: %s", agent_name, exc)
            continue
        for row in agent_rows:
            row.migrated_at = stamp
        audit.record(session, action="access.migrate", target_kind="agent", target_id=agent_name, agent_name=agent_name,
                     actor=audit.ROOM_ACTOR, source=audit.SOURCE_ROOM, detail=detail, summary=summary)
        session.commit()
    return out


__all__ = [
    "GITHUB_VENDOR",
    "LEASES_NOTE",
    "MAX_REPOSITORIES",
    "NO_CONNECTION",
    "PULLS_REVIEW",
    "PULLS_WRITE",
    "REPOSITORY_RE",
    "REPOS_READ",
    "Connection",
    "active_grants",
    "child",
    "child_spec",
    "connection",
    "end_open_leases",
    "installation_repositories",
    "migrate_repo_access",
    "strays",
    "write_child",
]
