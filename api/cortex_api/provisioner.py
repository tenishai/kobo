"""The provisioning step engine.

Mirrors gojira's `start_provision` shape: `run_pipeline` fires a daemon thread
that walks an ordered list of `Step`s against a fresh session of its own
(opened from `session_factory`, per the file-SQLite `check_same_thread=False`
threading pattern in `cortex_api.db`/`tests/conftest.py`) and returns
immediately, leaving the caller to poll the ledger (`AgentStep` rows) for
progress rather than block on completion.

Every step's outcome is persisted before moving to the next one, so a crash or
restart can resume: `reset_failed_steps` flips FAILED rows back to PENDING,
and the next `run_pipeline` call skips anything already DONE or SKIPPED.
Steps themselves arrive in Tasks 6-8 — this module only ships the engine,
exercised in `tests/test_engine.py` against toy steps.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from cortex_api.clients import Clients
from cortex_api.config import Config
from cortex_api.models import Agent, AgentStatus, AgentStep, StepStatus

log = logging.getLogger(__name__)


class Step(Protocol):
    name: str

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
                log.warning("step %r failed for agent %s: %s", step.name, agent_id, exc)
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
    """
    thread = threading.Thread(
        target=_run_pipeline_body,
        args=(session_factory, agent_id),
        kwargs={"config": config, "clients": clients, "steps": steps},
        daemon=True,
    )
    thread.start()
    return thread


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
