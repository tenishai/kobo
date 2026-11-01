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

The same reading says whether the agent is mid-restart (`rolling`,
2026-11-01): agents deploy with strategy Recreate and one replica, so a
change the owner asked for (a Wakes on save, a model change) takes the
agent down for a minute by design. That minute is "restarting", not an
outage, and the Slack row's Live waits for a reading that is up AND not
rolling, taken after the save.

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
from kobo_api.timefmt import rfc3339

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
    # Mid-restart: the controller has not caught up with the latest spec,
    # or the new pods are not all up yet. `state` keeps its own meaning
    # (down is down), so a rolling agent can be either.
    rolling: bool = False
    # When the deployment last became available (its Available condition's
    # transition time), None while it is not. The newest pod's own ready
    # time would need a pod read per agent; this comes free with the
    # deployment and says the same thing for a one-replica agent.
    ready_since: str | None = None

    def body(self) -> dict:
        return asdict(self)


# The deployment controller's Progressing reasons while a rollout is still
# on its way. Once it finishes the reason is NewReplicaSetAvailable and
# stays there even when the pod later crashes, and when it gives up the
# reason is ProgressDeadlineExceeded; neither of those is a restart.
_PROGRESSING = {"ReplicaSetUpdated", "NewReplicaSetCreated", "FoundNewReplicaSet"}


def _condition(status: dict, kind: str) -> dict | None:
    for cond in status.get("conditions") or []:
        if isinstance(cond, dict) and cond.get("type") == kind:
            return cond
    return None


def _rollout(dep: dict) -> tuple[bool, str | None]:
    """(`rolling`, `ready_since`) from the deployment's own status. Rolling
    when the controller has not observed the latest generation, when fewer
    pods than wanted are on the new spec, when more pods than wanted exist
    (old ones still going), or when the new pods are not all ready and the
    controller itself says the rollout is still progressing. That last
    clause needs the Progressing condition: without it, a pod that is not
    ready is simply down, never assumed to be restarting (lumbot's four-day
    crash loop looked exactly like "updated 1, ready 0")."""
    spec = dep.get("spec") or {}
    status = dep.get("status") or {}
    wanted = spec.get("replicas", 1) or 0
    generation = (dep.get("metadata") or {}).get("generation") or 0
    observed = status.get("observedGeneration") or 0
    updated = status.get("updatedReplicas") or 0
    ready = status.get("readyReplicas") or 0
    total = status.get("replicas") or 0
    progressing = _condition(status, "Progressing") or {}
    unfinished = progressing.get("status") == "True" and progressing.get("reason") in _PROGRESSING
    rolling = (
        observed < generation
        or updated < wanted
        or total > wanted
        or (ready < wanted and updated > 0 and unfinished)
    )
    available = _condition(status, "Available") or {}
    ready_since = available.get("lastTransitionTime") if available.get("status") == "True" else None
    return rolling, ready_since


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
    `rolling` (see `_rollout`) rides along either way.

    A cluster that cannot be asked is `up` with no reason: the room refuses
    on facts, and "I could not check" is not a fact about the agent.
    """
    checked_at = rfc3339(now or datetime.now(UTC))
    try:
        dep = kubectl.deployment_status_json(name, name)
    except Exception:  # noqa: BLE001
        log.info("health probe could not read deployment for %s", name)
        return AgentHealth(UP, None, checked_at)
    spec = dep.get("spec") or {}
    status = dep.get("status") or {}
    wanted = spec.get("replicas", 1) or 0
    available = status.get("availableReplicas") or 0
    rolling, ready_since = _rollout(dep)
    if wanted == 0 or available >= wanted:
        return AgentHealth(UP, None, checked_at, rolling, ready_since)

    reason = f"{available} of {wanted} running"
    try:
        trouble = _pod_trouble(kubectl.pods_json(name))
    except Exception:  # noqa: BLE001
        trouble = None
    if trouble:
        reason = f"{reason}; {trouble}"
    return AgentHealth(DOWN, reason, checked_at, rolling, ready_since)


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
