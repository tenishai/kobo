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
LEASES_NOTE = ("the next credential the agent borrows is for the new set "
               "(a permission service older than 0.4.22 hands out the old one until its hour runs out)")
"""bab 0.4.22 drops the credential it cached for a set when the set changes
under grants, so the agent's next call is narrowed to the new one; the audit
row says so."""

REPOSITORIES_KIND = "repositories"
"""bab 0.4.22: the kind of a request that would change a repository set
already carrying grants (`proposal: {added, removed}`), decided in Review."""

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


def installation_repositories(clients, index=None) -> list[InstallationRepo]:
    """Every repository the room App's installation can see, by name.

    `index` is the room's warm listing (`repo_search.RepoIndex`, the five-minute
    cache the connect form searches, filled by the warmer at boot and every
    TTL). When the room keeps one it is the answer (kobo#368): the picker and
    the save used to walk the installation a page at a time on every call.
    Only a room without one (no GitHub client, or a test that built none) walks
    the client itself. Raises `ClientError` when the room holds no GitHub
    client or GitHub refused."""
    if index is not None:
        return sorted(index.listing(), key=lambda r: r.full_name)
    github = getattr(clients, "github", None) if clients is not None else None
    if github is None:
        raise ClientError("this room holds no GitHub client to list the installation with")
    return sorted(github.list_installation_repos(), key=lambda r: r.full_name)


def strays(repositories: list[str], available: list[InstallationRepo]) -> list[str]:
    """The names asked for that the installation does not carry."""
    known = {r.full_name for r in available}
    return sorted({r for r in repositories if r not in known})


@dataclass(frozen=True)
class Saved:
    """What a write of an agent's set came to: the child's id, whether the set
    in force changed, the set in force, and the pick left waiting for a
    reviewer (None when the whole pick landed)."""

    integration: str
    changed: bool
    repositories: list[str]
    waiting: dict | None = None


def waiting_pick(request: dict | None) -> dict | None:
    """A request of kind `repositories` as the room shows it: what the owner's
    pick would add to (and take from) the set once a reviewer allows it."""
    if not request or request.get("kind") != REPOSITORIES_KIND:
        return None
    change = request.get("proposal") or {}
    return {"id": request.get("id"), "added": sorted(change.get("added") or []),
            "removed": sorted(change.get("removed") or []), "requested_by": request.get("requested_by"),
            "created_at": request.get("created_at")}


def waiting_on(bab: BabClient, actor: str, agent: str, integration: str) -> dict | None:
    """The pick waiting on the agent's set, or None."""
    for row in bab.requests("pending", agent, actor):
        if row.get("integration") == integration and row.get("kind") == REPOSITORIES_KIND:
            return waiting_pick(row)
    return None


def child_spec(conn: Connection, agent: str, repositories: list[str]) -> dict:
    """What the child carries in the permission service: the set, a name the
    Review pane shows, and the installation copied from the connection."""
    spec = {"repositories": sorted(set(repositories)), "name": f"{conn.name} for {agent}",
            "installation_id": conn.installation_id}
    if conn.account:
        spec["account"] = conn.account
    return spec


def write_child(bab: BabClient, actor: str, conn: Connection, agent: str, repositories: list[str]) -> Saved:
    """Write or replace the agent's child with `repositories`. The ceiling is
    every github catalogue permission: the levels are the owner's to ask and
    Review's to decide, not a wall the room adds.

    Once the set carries grants, an owner's pick is the permission service's
    to split (bab 0.4.22): what it removes lands, what it adds waits for a
    reviewer, and the answer names the set in force and the request under
    `pending`. A reviewer's own write lands whole."""
    existing = child(bab, actor, conn.id, agent)
    before = sorted(child_repositories(existing))
    spec = child_spec(conn, agent, repositories)
    cid = child_id(conn.id, agent)
    answer = bab.put_integration(cid, GITHUB_VENDOR, spec, all_permissions(bab, GITHUB_VENDOR, actor), actor,
                                 parent=conn.id) or {}
    waiting = waiting_pick(answer.get("pending"))
    if waiting is not None:
        in_force = sorted(answer.get("repositories") or before)
        return Saved(cid, in_force != before, in_force, waiting)
    return Saved(cid, existing is None or before != spec["repositories"], spec["repositories"])


def active_grants(bab: BabClient, actor: str, agent: str, integration: str) -> list[dict]:
    return [g for g in bab.grants(agent, actor)
            if g.get("integration") == integration and g.get("state", "active") == "active"]


def end_open_leases(bab: BabClient, actor: str, agent: str, integration: str) -> str:
    """What happened to the leases out on the child after its set changed:
    the permission service drops the credential it cached (see `LEASES_NOTE`),
    so the sentence says so and the audit row carries it."""
    return LEASES_NOTE


# ---- the default: an agent reads its own repository (owner ruling 2026-11-02) --


class DefaultWaits(ClientError):
    """The room asked for the default and could not allow it itself: the
    request is filed and waits for a reviewer."""


def own_repository_default(bab: BabClient, conn: Connection, agent: str, repository: str, note: str) -> str | None:
    """Read on the agent's own repository, asked for and allowed by the room.

    The room writes the agent's set as its one repository, files a Read request
    on it as itself and approves it. The permission service lets the room decide
    exactly that since 0.4.23 (kobo#476): a request the room raised, for a level
    its catalogue marks `room_default`, on that agent's own set. Before that the
    decision was refused (the room is on no reviewing team), so the default never
    landed anywhere and left a request waiting; that is `DefaultWaits` here.

    Safe to run again. A set that is still exactly this repository with the
    room's own request still waiting on it is approved now. Anything else that
    already exists stands: a set a person picked, a default a reviewer decided,
    a Read already held. The room never asks twice.

    Returns the set's id when the agent holds Read on it because of this call
    (the caller brings the gateway lane in step), else None."""
    actor = ROOM_ACTOR
    cid = child_id(conn.id, agent)
    existing = child(bab, actor, conn.id, agent)
    if existing is None:
        write_child(bab, actor, conn, agent, [repository])
        if any(g.get("permission") == REPOS_READ for g in active_grants(bab, actor, agent, cid)):
            return cid
        asked = bab.request(agent, cid, [REPOS_READ], note, actor)
    else:
        if child_repositories(existing) != [repository]:
            return None
        asked = next((r for r in bab.requests("pending", agent, actor)
                      if r.get("integration") == cid and r.get("requested_by") == actor
                      and r.get("kind") != REPOSITORIES_KIND and r.get("permissions") == [REPOS_READ]), None)
        if asked is None:
            return None
    try:
        results = bab.decide([asked["id"]], "approve", None, actor)
    except ClientError as exc:
        raise DefaultWaits(str(exc)) from exc
    if not any(r.get("id") == asked["id"] and r.get("status") == "approved" for r in results):
        raise DefaultWaits(f"the permission service did not approve request {asked['id']}")
    return cid


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
                cid = write_child(bab, actor, conn, agent_name, repositories).integration
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
    "REPOSITORIES_KIND",
    "REPOSITORY_RE",
    "REPOS_READ",
    "Connection",
    "DefaultWaits",
    "Saved",
    "active_grants",
    "child",
    "child_spec",
    "connection",
    "end_open_leases",
    "installation_repositories",
    "migrate_repo_access",
    "own_repository_default",
    "strays",
    "waiting_on",
    "waiting_pick",
    "write_child",
]
