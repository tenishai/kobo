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

Two processes, one database (resilience arc, Task 13): this module's own
`run_pipeline` docstring already warned that calling it twice concurrently
for the same `agent_id` races the same `AgentStep` rows -- true even within
one process, but a hard requirement once more than one API replica can be
running (the retry route's own agent-status flip is a check-then-act read
that only serializes threads sharing one interpreter, not two OS processes
reading the DB at nearly the same instant). `_claim_step` below is the fix:
before a step actually runs, the engine flips its row PENDING -> RUNNING with
one conditional `UPDATE ... WHERE status = 'pending'`, checking the rowcount
rather than trusting an ORM attribute set. Exactly one of two racing
processes ever sees rowcount > 0; the other sees 0 and returns without
executing the step (or touching the agent's status) a second time -- the
first process's own run carries the pipeline through to completion. Plain
SQL, not an engine-specific advisory lock: the same conditional UPDATE is
valid on SQLite and Postgres, so no branch is needed for either backend
(see `tests/test_engine.py`'s two-real-process test for the proof, run
against a shared SQLite file -- SQLite's own file locking is sufficient to
prove the claim's SQL is correct under real concurrent writers; nothing here
is SQLite-specific).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from kobo_api.clients import Clients
from kobo_api.config import Config
from kobo_api.db import SQLITE_BUSY_TIMEOUT_S
from kobo_api.kinds import is_connected, is_directory
from kobo_api.models import Agent, AgentStatus, AgentStep, StepStatus
from kobo_api.pairs import sync_dispatch_pairs_best_effort
from kobo_api.steps import (
    AccessStep,
    AvatarStep,
    BuildStep,
    ComposeDirectoryStep,
    ConnectedWireStep,
    ConnectStep,
    DeployStep,
    DirectoryStep,
    HelloStep,
    LoadStep,
    OnboardStep,
    RepoStep,
    StepSkipped,
    StepWaiting,
    WireStep,
    refresh_colleague_rosters,
)

log = logging.getLogger(__name__)

# How often a running pipeline says it is still alive, and how long a row
# may go unheard from before another process may declare it interrupted.
# The threshold is several intervals, not one: a beat is a small write that
# can be delayed by a slow database, a GC pause, or a busy pod, and the
# cost of being wrong here is failing a healthy provision.
RUN_HEARTBEAT_INTERVAL_S = 10.0
RUN_STALE_AFTER_S = 60.0
# How often every replica re-runs the sweep. Boot alone is not enough: with
# rolling updates a pipeline killed a minute before the last boot would
# never be swept by anyone, and a room that is not redeployed for a week
# would leave it wedged for a week.
INTERRUPTED_SWEEP_INTERVAL_S = 60.0
# Prefix for the ticker threads, so a leak is greppable in a stack dump
# (and assertable in a test).
HEARTBEAT_THREAD_NAME = "kobo-heartbeat"
# How long `stop()` waits for the ticker to notice. Strictly greater than a
# beat's worst case, which is one interval of waiting plus a database write
# that may itself sit on SQLite's busy timeout while the pipeline thread
# commits (`kobo_api.db.SQLITE_BUSY_TIMEOUT_S`).
HEARTBEAT_JOIN_TIMEOUT_S = RUN_HEARTBEAT_INTERVAL_S + SQLITE_BUSY_TIMEOUT_S + 10.0


def naive_utc(moment: datetime) -> datetime:
    """UTC with the tzinfo dropped, matching what the DB actually stores:
    these columns are plain `DateTime`, and SQLAlchemy strips tzinfo on
    both engines this room runs (the same normalization
    `kobo_api.learnings._naive_utc` does for its own comparisons).

    Public, and the ONE spelling of this for everything that touches
    `run_heartbeat_at`: the routes that put a row in flight
    (`kobo_api.main`'s create and retry) stamp the same column this
    module's heartbeat and sweep compare, and a second spelling of "now"
    among them is exactly how a 60-second cutoff goes quietly wrong.
    """
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(UTC).replace(tzinfo=None)


class _Heartbeat:
    """Says, on a short interval, that this process is still running
    `agent_id`'s pipeline.

    Its own thread and its own session per beat, deliberately: the pipeline
    session belongs to the pipeline thread (SQLAlchemy sessions are not
    thread-safe), and a beat must keep landing during a SINGLE long step --
    the build wait runs for minutes, and a heartbeat written only at step
    boundaries would go stale in the middle of a healthy provision.

    A failed beat is logged and skipped, never raised: the database being
    briefly unavailable is not a reason to fail a provision, and the worst
    case of several missed beats is another replica sweeping this run --
    which is exactly the same outcome as this process having died, the
    thing the sweep is allowed to be wrong about in only that direction.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        agent_id: int,
        *,
        interval_s: float = RUN_HEARTBEAT_INTERVAL_S,
        now: Callable[[], datetime] | None = None,
    ):
        self._session_factory = session_factory
        self._agent_id = agent_id
        self._interval_s = interval_s
        self._now = now or (lambda: datetime.now(UTC))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        # One beat inline before the thread exists, so the row is marked
        # live the moment the run begins rather than one interval later.
        self._beat()
        self._thread = threading.Thread(
            target=self._loop,
            name=f"{HEARTBEAT_THREAD_NAME}-{self._agent_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            # Joined, not merely signalled: the run's own thread must not
            # return while a beat for it is still in flight, or a caller
            # that saw the run finish could still watch the row's heartbeat
            # move afterward. The timeout is sized ABOVE a beat's worst
            # case -- a beat can sit on SQLite's busy timeout waiting for
            # the pipeline's own commit -- so reaching it means something
            # is genuinely wrong rather than merely slow, and the thread is
            # a daemon so a stuck one can never hold up the process.
            self._thread.join(timeout=HEARTBEAT_JOIN_TIMEOUT_S)
            if self._thread.is_alive():
                log.warning(
                    "heartbeat thread for agent %s did not stop within %ss",
                    self._agent_id,
                    HEARTBEAT_JOIN_TIMEOUT_S,
                )
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self._interval_s):
            self._beat()

    def _beat(self) -> None:
        session = self._session_factory()
        try:
            session.execute(
                update(Agent)
                .where(Agent.id == self._agent_id)
                .values(run_heartbeat_at=naive_utc(self._now()))
            )
            session.commit()
        except Exception:
            log.warning(
                "heartbeat write failed for agent %s", self._agent_id, exc_info=True
            )
            try:
                session.rollback()
            except Exception:  # best-effort cleanup only
                log.warning("heartbeat rollback failed", exc_info=True)
        finally:
            session.close()


class Step(Protocol):
    name: str
    label: str

    def run(self, ctx: Ctx) -> str:
        """Perform the step; return a human-readable detail, raise
        `kobo_api.steps.StepSkipped` to mark this step SKIPPED without
        failing anything after it (best-effort steps, e.g. `AvatarStep`,
        whose own outcome must never block the agent's path to READY), or
        raise any other exception to fail the step (and abort every step
        after it -- see `_run_pipeline_body` below)."""
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


def _claim_step(session: Session, step_id: int) -> bool:
    """Atomically flip AgentStep `step_id` from PENDING to RUNNING with one
    conditional `UPDATE ... WHERE status = 'pending'`, rather than an ORM
    attribute set followed by a plain commit (the row-level claim Task 13
    needs: two processes racing to run the same agent's pipeline must never
    both execute the same step).

    Returns True iff THIS call's UPDATE actually matched (and thus claimed)
    the row. False means the row was not PENDING by the time this ran --
    already RUNNING (another runner got there first) or already terminal --
    and the caller must NOT execute the step itself.

    Plain SQL, valid identically on SQLite and Postgres (no dialect-specific
    locking primitive): whichever of two concurrent callers' UPDATE actually
    lands first wins the row, deterministically, regardless of the two
    processes' relative timing -- the database's own write serialization is
    what makes this safe, not anything held in either process's memory.
    """
    result = session.execute(
        update(AgentStep)
        .where(AgentStep.id == step_id, AgentStep.status == StepStatus.PENDING)
        .values(status=StepStatus.RUNNING, started_at=datetime.now(UTC))
    )
    session.commit()
    return result.rowcount > 0


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
            # In the same commit that puts the row in flight, always: a row
            # is sweepable from the instant its status says a run is
            # happening, and the routes that flip it (create, retry) stamp
            # this for the same reason. Redundant with the ticker's first
            # beat and deliberately so -- a beat is a write that is allowed
            # to fail, and this one is not on its own.
            agent.run_heartbeat_at = naive_utc(datetime.now(UTC))
            session.commit()

            workdir = config.workdir / agent.name
            workdir.mkdir(parents=True, exist_ok=True)
            ctx = Ctx(
                agent=agent, session=session, config=config, clients=clients, workdir=workdir
            )

            failed_step_name: str | None = None
            failed_error: str | None = None
            for step in steps:
                step_row = _get_or_create_step(session, agent_id, step.name)

                if step_row.status in (StepStatus.DONE, StepStatus.SKIPPED):
                    continue

                if failed_step_name is not None:
                    step_row.status = StepStatus.FAILED
                    step_row.error = f"aborted: {failed_step_name}"
                    session.commit()
                    continue

                if not _claim_step(session, step_row.id):
                    # Another runner (a second process, most likely a
                    # double-retry racing the agent-status check in the API
                    # route -- see this module's own docstring) already
                    # claimed this step. Back off without executing it a
                    # second time or touching the agent's status any
                    # further; whichever runner actually won the claim is
                    # the one that carries this pipeline through to
                    # completion.
                    log.info(
                        "run_pipeline: step %r for agent %s was already "
                        "claimed by another runner -- stopping this run",
                        step.name, agent_id,
                    )
                    return

                try:
                    step.run(ctx)
                except StepWaiting as exc:
                    # Parked on a person. The step and the agent both say
                    # so, the steps after it stay pending (nothing is
                    # aborted, nothing failed), and this run ends here. The
                    # waiting sweep, or a Retry, runs the pipeline again;
                    # `reset_failed_steps` turns the WAITING row back to
                    # PENDING and the step re-decides against the platform.
                    log.info("step %r is waiting for agent %s: %s", step.name, agent_id, exc)
                    step_row.status = StepStatus.WAITING
                    step_row.error = str(exc)
                    session.commit()
                    agent.status = AgentStatus.WAITING
                    agent.error = str(exc)
                    session.commit()
                    return
                except StepSkipped as exc:
                    # Best-effort step (AvatarStep today): SKIPPED is not a
                    # failure -- unlike the FAILED branch below, this never
                    # touches `agent.status`/`agent.error` or sets
                    # `failed_step_name`, so every step after this one still
                    # runs normally and the agent can still reach READY.
                    # `_get_or_create_step`'s own DONE/SKIPPED short-circuit
                    # (above) makes this permanent across a retry too -- a
                    # skipped step never gets a second chance to run.
                    log.info(
                        "step %r skipped for agent %s: %s", step.name, agent_id, exc
                    )
                    step_row.status = StepStatus.SKIPPED
                    step_row.error = str(exc)
                    step_row.finished_at = datetime.now(UTC)
                    session.commit()
                    continue
                except Exception as exc:  # noqa: BLE001 - a step's failure, not a bug here
                    log.warning(
                        "step %r failed for agent %s: %s", step.name, agent_id, exc
                    )
                    step_row.status = StepStatus.FAILED
                    step_row.error = str(exc)
                    step_row.finished_at = datetime.now(UTC)
                    session.commit()
                    # The agent's own status flips to FAILED only after the
                    # loop has marked every remaining step aborted (below).
                    # Flipping it here let a reader see "failed" while later
                    # steps still read "pending", which a checklist renders as
                    # a contradiction and a poller can catch on a slow machine.
                    failed_step_name = step.name
                    failed_error = str(exc)
                    continue

                step_row.status = StepStatus.DONE
                step_row.finished_at = datetime.now(UTC)
                session.commit()

            if failed_step_name is not None:
                agent.error = failed_error
                agent.status = AgentStatus.FAILED
                session.commit()
            else:
                agent.status = AgentStatus.READY
                agent.error = None
                session.commit()
            if failed_step_name is None and not is_connected(agent):
                # Both hooks below are about colleagues, and a connected
                # agent has none: no switchboard identity, no pass, nothing
                # to deliver a roster to and nobody to pair it with. Running
                # them anyway would be harmless (both are gated and
                # fail-soft) but would also be a lie about what the room
                # just did.
                #
                # Access-control arc, Task C4: a newly-READY agent joins its
                # group's implicit pairs (kobo_api.pairs.allowed_pairs,
                # recomputed fresh right here) -- push the switchboard's
                # policy now rather than waiting on some other trigger to
                # notice. Fail-soft and skip-when-unwired, same as every
                # other trigger of this shared helper.
                sync_dispatch_pairs_best_effort(clients, config, session)
                # Roster refresh lane (Task 6): redeliver colleagues.md to
                # every agent that can now see this newly-READY agent --
                # never to every agent in the room (spec 3.3's scope
                # discipline). Best-effort and switchboard-gated internally
                # (kobo_api.steps.refresh_colleague_rosters); this outer
                # try/except is belt-and-suspenders so a bug in the refresh
                # path can never turn an otherwise-successful provision
                # into a failure this late.
                try:
                    refresh_colleague_rosters(
                        clients, config, session, name=agent.name, group_name=agent.group_name
                    )
                except Exception:
                    log.warning(
                        "roster refresh failed after agent %s went READY",
                        agent.name,
                        exc_info=True,
                    )
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


def _run_pipeline_with_heartbeat(
    session_factory: sessionmaker[Session],
    agent_id: int,
    *,
    config: Config,
    clients: Clients,
    steps: list[Step],
    heartbeat_interval_s: float,
    now: Callable[[], datetime] | None,
) -> None:
    """`_run_pipeline_body` with a heartbeat running alongside it.

    Wrapped here rather than inside the body so the liveness signal covers
    the run from its very first moment to its very last, engine-level
    recovery included, and so every existing direct caller of
    `_run_pipeline_body` (the engine's own tests) is untouched.
    """
    beat = _Heartbeat(
        session_factory, agent_id, interval_s=heartbeat_interval_s, now=now
    )
    try:
        try:
            beat.start()
        except Exception:
            # Inside the try, so a ticker that cannot start does not cost
            # the provision. The two failures are not comparable: a run
            # without a heartbeat is recoverable (the sweep fails it, the
            # user retries), while a run that never happens at all leaves
            # an agent wedged with nothing to resume.
            log.warning(
                "could not start the heartbeat for agent %s; the run continues "
                "but may be swept as interrupted",
                agent_id,
                exc_info=True,
            )
        _run_pipeline_body(
            session_factory, agent_id, config=config, clients=clients, steps=steps
        )
    finally:
        # The run is over: stop claiming it is alive. In a `finally` so a
        # crash inside the body can never leave a ticker beating for a
        # pipeline that is not running -- which would make the row
        # permanently unsweepable.
        beat.stop()


def run_pipeline(
    session_factory: sessionmaker[Session],
    agent_id: int,
    *,
    config: Config,
    clients: Clients,
    steps: list[Step],
    heartbeat_interval_s: float = RUN_HEARTBEAT_INTERVAL_S,
    now: Callable[[], datetime] | None = None,
) -> threading.Thread:
    """Fire the pipeline for `agent_id` on a daemon thread and return it immediately.

    The thread opens its own session from `session_factory` (never the caller's),
    so it's safe to call this from a request handler and let the request return
    before provisioning finishes.

    For as long as that thread runs, a second (also daemon) thread refreshes
    `Agent.run_heartbeat_at` every `heartbeat_interval_s` — that is what
    tells `sweep_interrupted` in ANOTHER replica that this run is alive and
    must not be failed out from under it. Both the interval and the clock
    are injectable for tests; nothing in the room passes either.

    Not safe to call twice concurrently for the same agent_id (racing writers
    to the same AgentStep rows); callers must serialize that themselves - the
    API route does this later by gating on agent status.
    """
    thread = threading.Thread(
        target=_run_pipeline_with_heartbeat,
        args=(session_factory, agent_id),
        kwargs={
            "config": config,
            "clients": clients,
            "steps": steps,
            "heartbeat_interval_s": heartbeat_interval_s,
            "now": now,
        },
        daemon=True,
    )
    thread.start()
    return thread


async def interrupted_sweep_loop(
    app, *, interval_s: float = INTERRUPTED_SWEEP_INTERVAL_S
) -> None:
    """Re-run `sweep_interrupted` every `interval_s`, for the life of the
    process (the lifespan owns it, same shape as the learnings nightly
    loop).

    Sleeps FIRST: `build_default_app` already sweeps synchronously at boot,
    so starting with another one would just repeat it. Unguarded by any
    lock, unlike the learnings sweep: this one is idempotent and touches
    only rows nobody is running, so two replicas doing it at the same
    moment write the same FAILED twice and agree. Cancellation passes
    through untouched; everything else is logged and the loop lives on.
    """
    import asyncio

    while True:
        await asyncio.sleep(interval_s)
        try:
            await asyncio.to_thread(
                sweep_interrupted,
                app.state.session_factory,
                queue_mode=getattr(app.state, "role", "all") != "all",
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("interrupted sweep failed")


def _stale_conditions(cutoff: datetime, *, queue_mode: bool) -> list:
    """A heartbeat older than the cutoff is a dead run in any mode. A row
    that never had a heartbeat is a never-started launch in the old shape
    (the thread died between the create landing and its first beat), but
    in queue mode it is simply waiting for a worker to claim it -- the
    claim is what writes the first heartbeat -- so it is left alone."""
    conditions = [
        and_(Agent.run_heartbeat_at.is_not(None), Agent.run_heartbeat_at < cutoff),
    ]
    if not queue_mode:
        conditions.append(and_(Agent.run_heartbeat_at.is_(None), Agent.created_at < cutoff))
    return conditions


# --- The worker's queue (API/worker split, 2026-10-01) ------------------------
#
# An API replica leaves a new or retried agent PENDING with no heartbeat.
# A worker claims it with one conditional UPDATE -- the claim IS the first
# heartbeat, so two workers can never both run it and the interrupted sweep
# can tell "queued" from "dead" -- and runs the pipeline exactly as the API
# used to, on a thread of its own, heartbeat and all.

QUEUE_POLL_INTERVAL_S = 3.0
QUEUE_MAX_CONCURRENT = 4
# How long a worker may keep running pipelines after being told to stop:
# a roll waits for in-flight work rather than failing it (the deployment's
# terminationGracePeriodSeconds must be at least this).
QUEUE_DRAIN_TIMEOUT_S = 1500.0


def claim_queued(
    session_factory: sessionmaker[Session],
    *,
    limit: int = 1,
    now: datetime | None = None,
) -> list[int]:
    """Claim up to `limit` queued agents, oldest first, and return their ids.
    Queued means PENDING with no heartbeat. The claim is
    `UPDATE ... WHERE status = 'pending' AND run_heartbeat_at IS NULL`, so
    whichever worker's UPDATE lands first owns the row, the same shape as
    `_claim_step`."""
    if limit <= 0:
        return []
    stamp = naive_utc(now or datetime.now(UTC))
    claimed: list[int] = []
    with session_factory() as session:
        candidates = session.scalars(
            select(Agent.id)
            .where(Agent.status == AgentStatus.PENDING, Agent.run_heartbeat_at.is_(None))
            .order_by(Agent.created_at, Agent.id)
            .limit(limit * 4)
        ).all()
        for agent_id in candidates:
            result = session.execute(
                update(Agent)
                .where(
                    Agent.id == agent_id,
                    Agent.status == AgentStatus.PENDING,
                    Agent.run_heartbeat_at.is_(None),
                )
                .values(run_heartbeat_at=stamp)
            )
            session.commit()
            if result.rowcount == 1:
                claimed.append(agent_id)
                if len(claimed) >= limit:
                    break
    return claimed


def queue_tick(
    session_factory: sessionmaker[Session],
    *,
    config: Config,
    clients: Clients | None,
    runner: Callable[..., object] | None = None,
    slots: int = QUEUE_MAX_CONCURRENT,
) -> int:
    """Claim and launch up to `slots` queued agents. Returns how many were
    launched. `runner` is `run_pipeline` (a test passes a recorder). A
    claim whose launch cannot even start is failed in the row, the same
    way the create route fails a launch it could not start."""
    launch = runner or run_pipeline
    launched = 0
    for agent_id in claim_queued(session_factory, limit=slots):
        with session_factory() as session:
            agent = session.get(Agent, agent_id)
            if agent is None:
                continue
            try:
                steps = pipeline_for(agent)
            except Exception as exc:
                log.exception("queue: cannot build the pipeline for agent %s", agent_id)
                agent.status = AgentStatus.FAILED
                agent.error = f"could not start provisioning: {exc}"
                session.commit()
                continue
        try:
            launch(session_factory, agent_id, config=config, clients=clients, steps=steps)
        except Exception as exc:
            log.exception("queue: failed to start provisioning for agent %s", agent_id)
            with session_factory() as session:
                agent = session.get(Agent, agent_id)
                if agent is not None:
                    agent.status = AgentStatus.FAILED
                    agent.error = f"could not start provisioning: {exc}"
                    session.commit()
            continue
        launched += 1
    return launched


def _live_pipeline_threads(app) -> list[threading.Thread]:
    threads = [t for t in getattr(app.state, "pipeline_threads", []) if t.is_alive()]
    app.state.pipeline_threads = threads
    return threads


async def queue_loop(
    app,
    *,
    interval_s: float = QUEUE_POLL_INTERVAL_S,
    max_concurrent: int = QUEUE_MAX_CONCURRENT,
) -> None:
    """For the life of a worker: every `interval_s`, fill the free slots
    from the queue. The launched threads are kept on `app.state` so a
    shutdown can wait for them (`drain_pipelines`)."""
    import asyncio

    def tick() -> None:
        free = max_concurrent - len(_live_pipeline_threads(app))
        if free <= 0:
            return
        before = set(threading.enumerate())
        queue_tick(
            app.state.session_factory,
            config=app.state.config,
            clients=app.state.clients,
            slots=free,
        )
        for t in threading.enumerate():
            if t not in before and t.daemon and t.name.startswith("Thread-"):
                app.state.pipeline_threads.append(t)

    app.state.pipeline_threads = []
    while True:
        await asyncio.sleep(interval_s)
        try:
            await asyncio.to_thread(tick)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("queue tick failed")


def drain_pipelines(app, *, timeout_s: float = QUEUE_DRAIN_TIMEOUT_S) -> int:
    """Wait for in-flight pipelines to finish, up to `timeout_s`. Returns
    how many were still running when time ran out (those will be swept as
    interrupted by the next worker)."""
    deadline = time.monotonic() + timeout_s
    for t in _live_pipeline_threads(app):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        t.join(timeout=remaining)
    left = len(_live_pipeline_threads(app))
    if left:
        log.warning("shutdown with %d pipelines still running", left)
    return left


def sweep_interrupted(
    session_factory: sessionmaker[Session],
    *,
    now: datetime | None = None,
    queue_mode: bool = False,
) -> int:
    """Fail every agent left wedged in PENDING/PROVISIONING by a process
    that is no longer running its pipeline.

    `_run_pipeline_body` is the only thing that ever moves an agent OUT of
    PENDING/PROVISIONING, and it only runs inside the daemon thread
    `run_pipeline` fires. If the API process dies (or is redeployed) while
    that thread is mid-flight, the thread is simply gone — nothing ever
    revisits the agent's row again. `retry_agent`'s gate only accepts FAILED,
    so an agent stuck in PENDING/PROVISIONING is wedged forever short of a
    manual DB edit. This lands every such agent in FAILED instead, where the
    existing retry path already knows how to pick it back up. Any AgentStep
    row a dead thread left RUNNING is swept the same way, so
    `reset_failed_steps` sees it too.

    Liveness, not "in flight": a rolling update starts a new pod WHILE the
    old one is still provisioning, so a sweep that failed every in-flight
    row would kill live runs, and the user's retry could then run the same
    steps in two processes at once. A row is only swept when its heartbeat
    (`Agent.run_heartbeat_at`, refreshed throughout a run by `_Heartbeat`)
    is older than `RUN_STALE_AFTER_S` — or, for a row that has never had a
    heartbeat at all (every row predating the column, and the seconds
    between a create landing and its thread's first beat), when the ROW
    itself is that old.

    Safe to run from anywhere, any number of times, in any number of
    replicas: it is idempotent, and two replicas sweeping the same dead row
    simply write the same FAILED twice. It runs at boot
    (`build_default_app`) and then periodically in every replica
    (`interrupted_sweep_loop`) — boot alone would never sweep a pipeline
    killed a minute after the last boot.

    Returns the number of agents swept; logs a summary line when nonzero.
    """
    cutoff = naive_utc(now or datetime.now(UTC)) - timedelta(seconds=RUN_STALE_AFTER_S)
    session = session_factory()
    try:
        wedged = (
            session.query(Agent)
            .filter(
                Agent.status.in_(
                    [AgentStatus.PENDING, AgentStatus.PROVISIONING, AgentStatus.DESTROYING]
                ),
                or_(*_stale_conditions(cutoff, queue_mode=queue_mode)),
            )
            .all()
        )
        for agent in wedged:
            if agent.status == AgentStatus.DESTROYING:
                # A destroy beats too (issue #203); one that stopped is dead.
                # DESTROY_FAILED is resumable: the next DELETE picks up at
                # the first incomplete step.
                agent.status = AgentStatus.DESTROY_FAILED
                agent.error = "interrupted by an API restart — destroy again when ready"
            else:
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


# The steps whose failure a retry answers by running the wiring again. A build
# that never concludes, or a rollout that never becomes ready, is in practice
# a pod that cannot start, and the reason is nearly always wiring that did
# not land: the Secret the Deployment's envFrom names is not there (live,
# 2026-09-24: a colleague pass the secrets platform never delivered). The
# wiring step is idempotent by construction (it re-delivers the pass, pushes
# nothing when nothing changed, and still confirms the platform is active),
# so re-running it is the repair, and this is how Retry becomes that repair
# rather than a second wait on the same missing Secret. A failure after the
# rollout (directory, hello) says nothing about the wiring, so it is left as
# it was.
#
# Composed agents only. A connected agent's wiring step proposes a pull
# request in a repository the room does not own and delivers no Secret, so
# re-running it repairs nothing and can newly fail (a service account the
# owner has since set, a stale branch) for reasons unrelated to the build.
#
# The build is reset along with the wiring, even when only the deploy
# failed: a re-run wiring step may push a commit and ask the platform to
# reprovision, and the CI run that starts then has to be watched by
# BuildStep before DeployStep judges the rollout. When nothing changed,
# BuildStep finds the same head's completed run and its one self-heal rerun
# rebuilds, which is the same rebuild a human would ask for.
# How often every replica asks the platform whether a parked connected
# agent's setup pull request has been merged. A minute is the pace of a
# person merging a PR; the agent's page tells them the room resumes on its
# own, and a minute is "on its own".
WAITING_SWEEP_INTERVAL_S = 60.0


async def waiting_sweep_loop(app, *, interval_s: float = WAITING_SWEEP_INTERVAL_S) -> None:
    """Re-run `sweep_waiting` every `interval_s` for the life of the process,
    the same shape as `interrupted_sweep_loop`. Sleeps first: nothing is
    parked at boot that was not parked a minute ago."""
    import asyncio

    while True:
        await asyncio.sleep(interval_s)
        try:
            await asyncio.to_thread(
                sweep_waiting,
                app.state.session_factory,
                config=app.state.config,
                clients=app.state.clients,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("waiting sweep failed")


def _claim_waiting(session: Session, agent_id: int) -> bool:
    """Move one WAITING row to PROVISIONING, conditionally (issue #203):
    `UPDATE ... WHERE status = 'waiting'`, so a row a DELETE or another
    replica already moved is left alone. The heartbeat is stamped with the
    claim so the interrupted sweep does not read the fresh run as wedged."""
    result = session.execute(
        update(Agent)
        .where(Agent.id == agent_id)
        .where(Agent.status == AgentStatus.WAITING)
        .values(
            status=AgentStatus.PROVISIONING,
            error=None,
            run_heartbeat_at=naive_utc(datetime.now(UTC)),
        )
    )
    return result.rowcount == 1


def sweep_waiting(
    session_factory: sessionmaker[Session],
    *,
    config: Config,
    clients: Clients | None,
    runner: Callable[..., None] | None = None,
) -> int:
    """Resume every WAITING agent whose setup pull request the platform now
    reports merged; fail the ones whose platform run failed while parked.

    The one question is the platform's: `app_status` for the agent's app,
    asked after `sync_app` so the platform looks at the pull request itself.
    Without that, an install with no GitHub webhook never learns of the
    merge and the agent waits forever (issue #118); with one, the sync is a
    no-op. A failed sync is logged and the status is read anyway.
    `active` means the merge landed and deploys flow, so the parked step is
    reset and the pipeline runs again from it (`runner`, `run_pipeline` by
    default; a test passes a recorder). `failed` means the platform gave up
    on its side while nobody was watching, and the agent should say so
    instead of waiting forever. Anything else (still `awaiting_pr`, or a
    re-run in progress) is left exactly as it is.

    Idempotent across replicas the way `sweep_interrupted` is: resuming
    flips the agent to PROVISIONING before the pipeline starts, so a second
    replica's sweep a moment later no longer sees it WAITING. Returns the
    number of agents resumed or failed.
    """
    if clients is None or clients.gojira is None:
        return 0
    run = runner or run_pipeline
    session = session_factory()
    touched = 0
    try:
        parked = session.query(Agent).filter(Agent.status == AgentStatus.WAITING).all()
        for agent in parked:
            if agent.gojira_app_id is None:
                continue
            try:
                clients.gojira.sync_app(agent.gojira_app_id)
            except Exception:  # the status read below still answers on a webhook install
                log.warning(
                    "sweep_waiting: could not ask the platform to sync app %s for agent %s",
                    agent.gojira_app_id, agent.name, exc_info=True,
                )
            try:
                status_payload = clients.gojira.app_status(agent.gojira_app_id)
            except Exception:
                log.warning(
                    "sweep_waiting: could not read platform status for agent %s",
                    agent.name, exc_info=True,
                )
                continue
            status = status_payload.get("status")
            if status == "active":
                if not _claim_waiting(session, agent.id):
                    # Someone else moved the row since this sweep read it: a
                    # DELETE made it DESTROYING, or another replica resumed it
                    # (issue #203). Never run the pipeline over that.
                    session.rollback()
                    continue
                reset_failed_steps(session, agent.id)
                session.commit()
                log.info("sweep_waiting: setup PR merged, resuming agent %s", agent.name)
                run(
                    session_factory,
                    agent.id,
                    config=config,
                    clients=clients,
                    steps=pipeline_for(agent),
                )
                touched += 1
            elif status == "failed":
                names = ", ".join(
                    step["name"]
                    for step in status_payload.get("steps", [])
                    if step.get("status") == "failed"
                ) or "unknown"
                message = (
                    f"gojira provisioning failed for app {agent.gojira_app_id} while "
                    f"waiting on the setup pull request: failed steps: {names}"
                )
                waiting_rows = (
                    session.query(AgentStep)
                    .filter_by(agent_id=agent.id, status=StepStatus.WAITING)
                    .all()
                )
                for step_row in waiting_rows:
                    step_row.status = StepStatus.FAILED
                    step_row.error = message
                agent.status = AgentStatus.FAILED
                agent.error = message
                session.commit()
                touched += 1
        return touched
    finally:
        session.close()


REWIRE_AFTER_FAILED = frozenset({"build", "deploy"})
REWIRE_RESETS = frozenset({"wire", "build"})


def reset_failed_steps(session: Session, agent_id: int) -> None:
    """Flip every FAILED (or WAITING) step of `agent_id` back to PENDING: the
    retry primitive, and the resume primitive for a parked run.

    A subsequent `run_pipeline` call will then skip the DONE/SKIPPED steps and
    re-run only these (and anything after them that never got created/started).

    One exception to "only the failed ones": when the build or the deploy of
    a composed agent is what failed, the wiring and build steps are reset
    too, so the retry re-runs them first. See `REWIRE_AFTER_FAILED`.
    """
    rows = session.query(AgentStep).filter_by(agent_id=agent_id).all()
    failed_names = {row.name for row in rows if row.status == StepStatus.FAILED}
    agent = session.get(Agent, agent_id)
    rewire = (
        bool(failed_names & REWIRE_AFTER_FAILED)
        and agent is not None
        and not is_connected(agent)
    )
    for step_row in rows:
        if step_row.status in (StepStatus.FAILED, StepStatus.WAITING) or (
            rewire and step_row.name in REWIRE_RESETS and step_row.status == StepStatus.DONE
        ):
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
#
# AvatarStep (a face at birth) slots in right after RepoStep: it needs
# `agent.repo_full_name` (set by RepoStep) to push the generated picture to
# the bundle, and putting it this early means the agent already has a face
# for the rest of the checklist to render against. It is the one step in
# this list whose own failure is never a pipeline failure -- every path
# through it that can't produce a picture raises `StepSkipped` instead
# (see its own docstring), so a room with no image backend configured, or a
# generation hiccup, never blocks an agent's path to READY.
PIPELINE: list[Step] = [
    RepoStep(),
    AvatarStep(),
    OnboardStep(),
    AccessStep(),
    WireStep(),
    BuildStep(),
    DeployStep(),
    DirectoryStep(),
    HelloStep(),
]

# The shorter list a team's own repo runs through (connected-agents design
# 2026-09-21). Four steps are literally the same objects as above -- what
# the room grants an agent, and what it watches, does not depend on who
# wrote the code. Three differ, and each difference is the same rule said
# three ways: the room does not own this repository. `ConnectStep` verifies
# one that already exists instead of creating one; `AvatarStep` reads the
# agent's kind and keeps the picture in the room rather than committing it;
# `ConnectedWireStep` proposes the wiring as a pull request rather than
# pushing it.
#
# Two steps are absent rather than skipped. A connected agent has no
# switchboard identity to list in the directory, and nothing the room knows
# how to say hello to -- an empty step would be a checklist line promising
# something that is not coming.
CONNECTED_PIPELINE: list[Step] = [
    ConnectStep(),
    AvatarStep(),
    OnboardStep(),
    AccessStep(),
    ConnectedWireStep(),
    BuildStep(),
    DeployStep(),
]


# The directory backend (issue #207): compose into a folder, picture, list
# in the switchboard directory when one is wired, tell the running set. No
# repository, platform, access grant, build or deploy, and no hello: the
# room has no address for a local process to say it to.
DIRECTORY_PIPELINE: list[Step] = [
    ComposeDirectoryStep(),
    AvatarStep(),
    DirectoryStep(),
    LoadStep(),
]


def pipeline_for(agent: Agent) -> list[Step]:
    """The step list this agent's kind runs. The single place the choice is
    made, so a caller never spells the comparison itself."""
    if is_directory(agent):
        return DIRECTORY_PIPELINE
    return CONNECTED_PIPELINE if is_connected(agent) else PIPELINE


# The UI's checklist vocabulary: step name -> human-readable label, sourced
# from each step's own `.label` (part of the `Step` Protocol above, alongside
# `name`/`run`) so this dict can't drift from what each step actually
# declares of itself. Built from the UNION of both lists: a name missing
# here degrades to a raw slug on the detail route, and the checklist would
# render it.
STEP_LABELS: dict[str, str] = {
    step.name: step.label for step in (*PIPELINE, *CONNECTED_PIPELINE, *DIRECTORY_PIPELINE)
}
