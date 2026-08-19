"""The provisioning step engine.

Mirrors gojira's `start_provision` shape: `run_pipeline` fires a daemon thread
that walks an ordered list of `Step`s against a fresh session of its own
(opened from `session_factory`, per the file-SQLite `check_same_thread=False`
threading pattern in `kobo_api.db`/`tests/conftest.py`) and returns
immediately, leaving the caller to poll the ledger (`AgentStep` rows) for
progress rather than block on completion.

Every step's outcome is persisted before moving to the next one, so a crash or
restart can resume at that per-step boundary: `reset_failed_steps` flips FAILED
rows back to PENDING, and the next `run_pipeline` call skips anything already
DONE or SKIPPED. That resume story only covers failures raised from inside a
step's own `run()`; engine-level failures (workdir setup, a session commit, a
missing agent row) don't get a step boundary to land on, so they're caught at
the pipeline level instead and the agent is marked FAILED directly.
Steps themselves arrived in Tasks 6-8 (`kobo_api.steps`); the engine itself
is exercised in `tests/test_engine.py` against toy steps, while `PIPELINE`
(the real, assembled step order) and `STEP_LABELS` below are exercised
end-to-end through the fake platform clients in `tests/test_api_agents.py`.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from kobo_api.clients import Clients
from kobo_api.config import Config
from kobo_api.models import Agent, AgentStatus, AgentStep, StepStatus
from kobo_api.steps import (
    AccessStep,
    BuildStep,
    DeployStep,
    DirectoryStep,
    HelloStep,
    OnboardStep,
    RepoStep,
    WireStep,
)

log = logging.getLogger(__name__)


class Step(Protocol):
    name: str
    label: str

    def run(self, ctx: Ctx) -> str:
        """Perform the step; return a human-readable detail, or raise to fail."""
        ...


@dataclass
class Ctx:
    agent: Agent
    session: Session
    config: Config
    clients: Clients
    workdir: Path


def _get_or_create_step(session: Session, agent_id: int, name: str) -> AgentStep:
    # `.one_or_none()` is safe because AgentStep enforces a UniqueConstraint on
    # (agent_id, name): this filter can never match more than one row.
    step_row = (
        session.query(AgentStep).filter_by(agent_id=agent_id, name=name).one_or_none()
    )
    if step_row is None:
        step_row = AgentStep(agent_id=agent_id, name=name, status=StepStatus.PENDING)
        session.add(step_row)
        session.flush()
    return step_row


def _run_pipeline_body(
    session_factory: sessionmaker[Session],
    agent_id: int,
    *,
    config: Config,
    clients: Clients,
    steps: list[Step],
) -> None:
    session = session_factory()
    try:
        agent = session.get(Agent, agent_id)
        if agent is None:
            # Nothing to provision and nothing to mark FAILED - log and bail
            # rather than let this masquerade as an AttributeError below.
            log.warning("run_pipeline: agent %s not found, nothing to provision", agent_id)
            return

        try:
            agent.status = AgentStatus.PROVISIONING
            session.commit()

            workdir = config.workdir / agent.name
            workdir.mkdir(parents=True, exist_ok=True)
            ctx = Ctx(
                agent=agent, session=session, config=config, clients=clients, workdir=workdir
            )

            failed_step_name: str | None = None
            for step in steps:
                step_row = _get_or_create_step(session, agent_id, step.name)

                if step_row.status in (StepStatus.DONE, StepStatus.SKIPPED):
                    continue

                if failed_step_name is not None:
                    step_row.status = StepStatus.FAILED
                    step_row.error = f"aborted: {failed_step_name}"
                    session.commit()
                    continue

                step_row.status = StepStatus.RUNNING
                step_row.started_at = datetime.now(UTC)
                session.commit()

                try:
                    step.run(ctx)
                except Exception as exc:  # noqa: BLE001 - a step's failure, not a bug here
                    log.warning(
                        "step %r failed for agent %s: %s", step.name, agent_id, exc
                    )
                    step_row.status = StepStatus.FAILED
                    step_row.error = str(exc)
                    step_row.finished_at = datetime.now(UTC)
                    agent.error = str(exc)
                    agent.status = AgentStatus.FAILED
                    session.commit()
                    failed_step_name = step.name
                    continue

                step_row.status = StepStatus.DONE
                step_row.finished_at = datetime.now(UTC)
                session.commit()

            if failed_step_name is None:
                agent.status = AgentStatus.READY
                agent.error = None
                session.commit()
        except Exception as exc:
            # Engine-level failure: anything outside a step's own run() (workdir
            # mkdir, a commit, ...). Steps have a FAILED row to resume from; this
            # doesn't, so catch it here and land the agent in FAILED directly
            # instead of leaving it wedged in PROVISIONING forever.
            log.exception("run_pipeline: engine-level failure for agent %s", agent_id)

            # The session that hit the error may itself be poisoned (e.g. a
            # failed commit leaves it unusable until rolled back), so clean it
            # up best-effort and record the failure through a fresh one.
            try:
                session.rollback()
            except Exception:  # best-effort cleanup only
                log.warning("run_pipeline: rollback of broken session failed", exc_info=True)

            recovery_session = session_factory()
            try:
                recovered_agent = recovery_session.get(Agent, agent_id)
                if recovered_agent is None:
                    log.warning(
                        "run_pipeline: agent %s not found while recording engine "
                        "failure",
                        agent_id,
                    )
                    return
                recovered_agent.status = AgentStatus.FAILED
                recovered_agent.error = str(exc)
                recovery_session.commit()
            finally:
                recovery_session.close()
    finally:
        session.close()


def run_pipeline(
    session_factory: sessionmaker[Session],
    agent_id: int,
    *,
    config: Config,
    clients: Clients,
    steps: list[Step],
) -> threading.Thread:
    """Fire the pipeline for `agent_id` on a daemon thread and return it immediately.

    The thread opens its own session from `session_factory` (never the caller's),
    so it's safe to call this from a request handler and let the request return
    before provisioning finishes.

    Not safe to call twice concurrently for the same agent_id (racing writers
    to the same AgentStep rows); callers must serialize that themselves - the
    API route does this later by gating on agent status.
    """
    thread = threading.Thread(
        target=_run_pipeline_body,
        args=(session_factory, agent_id),
        kwargs={"config": config, "clients": clients, "steps": steps},
        daemon=True,
    )
    thread.start()
    return thread


def sweep_interrupted(session_factory: sessionmaker[Session]) -> int:
    """Fail every agent left wedged in PENDING/PROVISIONING by a process restart.

    `_run_pipeline_body` is the only thing that ever moves an agent OUT of
    PENDING/PROVISIONING, and it only runs inside the daemon thread
    `run_pipeline` fires. If the API process dies (or is redeployed) while
    that thread is mid-flight, the thread is simply gone — nothing ever
    revisits the agent's row again. `retry_agent`'s gate only accepts FAILED,
    so an agent stuck in PENDING/PROVISIONING is wedged forever short of a
    manual DB edit. Call this once at process startup, before serving any
    requests (`build_default_app` — never `create_app`, so test fixtures'
    isolated agents are unaffected), to land every such agent in FAILED
    instead, where the existing retry path already knows how to pick it back
    up. Any AgentStep row a dead thread left RUNNING is swept the same way,
    so `reset_failed_steps` sees it too.

    Returns the number of agents swept; logs a summary line when nonzero.
    """
    session = session_factory()
    try:
        wedged = (
            session.query(Agent)
            .filter(Agent.status.in_([AgentStatus.PENDING, AgentStatus.PROVISIONING]))
            .all()
        )
        for agent in wedged:
            agent.status = AgentStatus.FAILED
            agent.error = "interrupted by an API restart — retry when ready"
            running_steps = (
                session.query(AgentStep)
                .filter_by(agent_id=agent.id, status=StepStatus.RUNNING)
                .all()
            )
            for step_row in running_steps:
                step_row.status = StepStatus.FAILED
                step_row.error = "interrupted"

        if wedged:
            session.commit()
            log.warning(
                "sweep_interrupted: flipped %d agent(s) wedged by a prior restart "
                "to FAILED",
                len(wedged),
            )
        return len(wedged)
    finally:
        session.close()


def reset_failed_steps(session: Session, agent_id: int) -> None:
    """Flip every FAILED step of `agent_id` back to PENDING: the retry primitive.

    A subsequent `run_pipeline` call will then skip the DONE/SKIPPED steps and
    re-run only these (and anything after them that never got created/started).
    """
    failed_steps = (
        session.query(AgentStep)
        .filter_by(agent_id=agent_id, status=StepStatus.FAILED)
        .all()
    )
    for step_row in failed_steps:
        step_row.status = StepStatus.PENDING
        step_row.error = None
        step_row.started_at = None
        step_row.finished_at = None
    session.commit()


# The assembled, one-click pipeline: compose+push a repo, onboard it onto
# gojira, grant model access, wire the app-authored manifests, wait for the
# image build and rollout, join the switchboard's directory, then say hello.
# Order is load-bearing — each step depends on state a prior one recorded on
# the agent (repo_full_name, gojira_app_id, ...).
#
# DirectoryStep now runs BEFORE HelloStep (colleagues switchboard plan, Task
# 4b — whole-arc review finding C-1, operator ruling 2026-08-19): agents
# compose LOCKED (only switchboard-signed calls accepted), so HelloStep's
# first contact needs the agent already registered in the directory before
# it can ride the switchboard's relay. This reverses Task 2's original
# placement (DirectoryStep last, right after HelloStep) — the card
# DirectoryStep graduates at this point legitimately lacks a capability
# line yet (HelloStep's own tail is what earns it, moments later), which is
# fine: a second, best-effort resync after that tail's card commit is what
# backfills the directory's copy with the capability once it exists.
# DirectoryStep is still a no-op on a room with no switchboard configured,
# so a room that doesn't opt in sees no behavior change from either its
# placement or HelloStep's own contact path (still the legacy direct one).
PIPELINE: list[Step] = [
    RepoStep(),
    OnboardStep(),
    AccessStep(),
    WireStep(),
    BuildStep(),
    DeployStep(),
    DirectoryStep(),
    HelloStep(),
]

# The UI's checklist vocabulary: step name -> human-readable label, sourced
# from each step's own `.label` (part of the `Step` Protocol above, alongside
# `name`/`run`) so this dict can't drift from what each step actually
# declares of itself.
STEP_LABELS: dict[str, str] = {step.name: step.label for step in PIPELINE}
