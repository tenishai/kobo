"""Is the agent actually running right now?

An agent's `status` says how far its setup got: `ready` means every
provisioning step finished. It says nothing about the pod today. A ready
agent whose container has crashed for four days still wore a green Ready
on the dashboard (lumbot, 2026-09-30), which is the one thing a status
badge must never do.

So the room asks the cluster, once a minute, for every ready agent: is
its Deployment available, and if not, why not. The answer is kept in
memory on `app.state.health` (per replica; it is derived, cheap and
re-derived within a minute of a restart, so it earns no table) and rides
along on the list and detail payloads as `health`. The UI shows `Down`
in place of `Ready` when the cluster says so, with the reason on the
hover.

Only ready agents are probed: a provisioning one has steps that already
tell the story, and a failed one is already red.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kobo_api.clients import KubectlClient
from kobo_api.models import Agent, AgentStatus

log = logging.getLogger(__name__)

HEALTH_SWEEP_INTERVAL_S = 60.0
# The first probe runs soon after boot, so a freshly rolled room is not
# blind for a whole minute.
HEALTH_FIRST_DELAY_S = 5.0

UP = "up"
DOWN = "down"


@dataclass(frozen=True)
class AgentHealth:
    state: str
    reason: str | None
    checked_at: str

    def body(self) -> dict:
        return asdict(self)


def _pod_trouble(pods: list[dict]) -> str | None:
    """One clause about the worst-looking container, or None when the pods
    look fine. Restart counts and waiting reasons are the two facts a
    person acts on; anything else is for the Observability link."""
    worst: tuple[int, str | None] | None = None
    for pod in pods:
        statuses = (pod.get("status") or {}).get("containerStatuses") or []
        for c in statuses:
            if not isinstance(c, dict):
                continue
            restarts = c.get("restartCount") or 0
            waiting = ((c.get("state") or {}).get("waiting") or {}).get("reason")
            if worst is None or restarts > worst[0] or (waiting and not worst[1]):
                worst = (restarts, waiting)
    if worst is None:
        return None
    restarts, waiting = worst
    parts = []
    if restarts:
        parts.append(f"restarted {restarts} times")
    if waiting:
        parts.append(waiting)
    return ", ".join(parts) if parts else None


def probe_health(kubectl: KubectlClient, name: str, *, now: datetime | None = None) -> AgentHealth:
    """The agent's Deployment is in its own namespace under its own name
    (the same convention every step in the pipeline uses). Available
    replicas short of spec is down; the reason names the count and, when
    the pods can be read, the worst container's restarts and wait reason.

    A cluster that cannot be asked is `up` with no reason: the room refuses
    on facts, and "I could not check" is not a fact about the agent.
    """
    checked_at = (now or datetime.now(UTC)).isoformat()
    try:
        dep = kubectl.deployment_status_json(name, name)
    except Exception:
        log.info("health probe could not read deployment for %s", name)
        return AgentHealth(UP, None, checked_at)
    spec = dep.get("spec") or {}
    status = dep.get("status") or {}
    wanted = spec.get("replicas", 1) or 0
    available = status.get("availableReplicas") or 0
    if wanted == 0 or available >= wanted:
        return AgentHealth(UP, None, checked_at)

    reason = f"{available} of {wanted} running"
    try:
        trouble = _pod_trouble(kubectl.pods_json(name))
    except Exception:
        trouble = None
    if trouble:
        reason = f"{reason}; {trouble}"
    return AgentHealth(DOWN, reason, checked_at)


def sweep_health(
    session_factory: sessionmaker[Session],
    kubectl: KubectlClient,
    store: dict[str, AgentHealth],
) -> int:
    """Probe every ready agent and rewrite `store` to exactly that set, so
    a destroyed or re-provisioning agent drops out rather than keeping a
    stale verdict. Returns how many were probed."""
    with session_factory() as session:
        names = list(
            session.scalars(select(Agent.name).where(Agent.status == AgentStatus.READY))
        )
    fresh = {name: probe_health(kubectl, name) for name in names}
    store.clear()
    store.update(fresh)
    return len(fresh)


async def health_sweep_loop(
    app,
    *,
    interval_s: float = HEALTH_SWEEP_INTERVAL_S,
    first_delay_s: float = HEALTH_FIRST_DELAY_S,
) -> None:
    """Same shape as `waiting_sweep_loop`: for the life of the process, on
    a thread because kubectl blocks."""
    delay = first_delay_s
    while True:
        await asyncio.sleep(delay)
        delay = interval_s
        clients = app.state.clients
        if clients is None:
            continue
        try:
            await asyncio.to_thread(
                sweep_health, app.state.session_factory, clients.kubectl, app.state.health
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("health sweep failed")
