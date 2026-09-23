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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from sqlalchemy import and_, or_, update
from sqlalchemy.orm import Session, sessionmaker

from kobo_api.clients import Clients
from kobo_api.config import Config
from kobo_api.db import SQLITE_BUSY_TIMEOUT_S
from kobo_api.kinds import is_connected
from kobo_api.models import Agent, AgentStatus, AgentStep, StepStatus
from kobo_api.pairs import sync_dispatch_pairs_best_effort
from kobo_api.steps import (
    AccessStep,
    AvatarStep,
    BuildStep,
    ConnectedWireStep,
    ConnectStep,
    DeployStep,
    DirectoryStep,
    HelloStep,
    OnboardStep,
    RepoStep,
    StepSkipped,
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
            await asyncio.to_thread(sweep_interrupted, app.state.session_factory)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("interrupted sweep failed")


def sweep_interrupted(
    session_factory: sessionmaker[Session], *, now: datetime | None = None
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
                Agent.status.in_([AgentStatus.PENDING, AgentStatus.PROVISIONING]),
                or_(
                    and_(
                        Agent.run_heartbeat_at.is_not(None),
                        Agent.run_heartbeat_at < cutoff,
                    ),
                    and_(Agent.run_heartbeat_at.is_(None), Agent.created_at < cutoff),
                ),
            )
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


def pipeline_for(agent: Agent) -> list[Step]:
    """The step list this agent's kind runs. The single place the choice is
    made, so a caller never spells the comparison itself."""
    return CONNECTED_PIPELINE if is_connected(agent) else PIPELINE


# The UI's checklist vocabulary: step name -> human-readable label, sourced
# from each step's own `.label` (part of the `Step` Protocol above, alongside
# `name`/`run`) so this dict can't drift from what each step actually
# declares of itself. Built from the UNION of both lists: a name missing
# here degrades to a raw slug on the detail route, and the checklist would
# render it.
STEP_LABELS: dict[str, str] = {
    step.name: step.label for step in (*PIPELINE, *CONNECTED_PIPELINE)
}
