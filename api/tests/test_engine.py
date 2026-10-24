"""Tests for kobo_api.provisioner: the step engine's ledger persistence,
fire-and-return threading, and resumable retry. Exercised against toy Steps
defined here — real steps arrive in Tasks 6-8.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.clients import Clients
from kobo_api.db import init_db
from kobo_api.kinds import KIND_COMPOSED, KIND_CONNECTED
from kobo_api.models import Agent, AgentStatus, AgentStep, StepStatus
from kobo_api.provisioner import (
    HEARTBEAT_THREAD_NAME,
    RUN_STALE_AFTER_S,
    reset_failed_steps,
    run_pipeline,
    sweep_interrupted,
    sweep_waiting,
)
from kobo_api.steps import StepWaiting
from tests.fakes import FakeCabal, FakeGojira

# No real backend is touched by the toy steps below, so every client slot is None.
NO_CLIENTS = Clients(github=None, gojira=None, cloud=None, agent=None, kubectl=None)

JOIN_TIMEOUT = 5


@dataclass
class RecordingStep:
    """Appends `name` to a shared `calls` list on every `run()`. Raises
    `fail_times` times (decrementing) before returning `detail`, so a test can
    force a failure and then a successful retry after `reset_failed_steps`.
    """

    name: str
    calls: list[str]
    fail_times: int = 0
    detail: str = "ok"

    def run(self, ctx) -> str:
        self.calls.append(self.name)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError(f"{self.name} boom")
        return self.detail


def _make_agent(session_factory, name: str, *, kind: str = KIND_COMPOSED) -> int:
    session = session_factory()
    agent = Agent(
        name=name,
        persona="helpful-generalist",
        model_id="vertex_ai/claude-sonnet-4-5",
        kind=kind,
    )
    session.add(agent)
    session.commit()
    agent_id = agent.id
    session.close()
    return agent_id


def _load(session_factory, agent_id: int):
    """Fetch the agent and its steps (by name) from a fresh session, then close it."""
    session = session_factory()
    agent = session.get(Agent, agent_id)
    rows = {s.name: s for s in agent.steps}
    session.close()
    return agent, rows


def _make_agent_with_group(session_factory, name: str, group_name: str) -> int:
    session = session_factory()
    agent = Agent(
        name=name,
        persona="helpful-generalist",
        model_id="vertex_ai/claude-sonnet-4-5",
        group_name=group_name,
    )
    session.add(agent)
    session.commit()
    agent_id = agent.id
    session.close()
    return agent_id


def _seed_ready_agent(session_factory, name: str, group_name: str) -> None:
    """A colleague already READY before the pipeline under test runs --
    this is the other half of the same-group pair a READY flip below
    should join."""
    session = session_factory()
    session.add(
        Agent(
            name=name,
            persona="helpful-generalist",
            model_id="vertex_ai/claude-sonnet-4-5",
            status=AgentStatus.READY,
            group_name=group_name,
        )
    )
    session.commit()
    session.close()


def test_happy_path_all_steps_done_and_agent_ready(session_factory, config):
    agent_id = _make_agent(session_factory, "agent-happy")
    calls: list[str] = []
    steps = [
        RecordingStep(name="step-a", calls=calls),
        RecordingStep(name="step-b", calls=calls),
    ]

    thread = run_pipeline(
        session_factory, agent_id, config=config, clients=NO_CLIENTS, steps=steps
    )
    thread.join(timeout=JOIN_TIMEOUT)
    assert thread.is_alive() is False

    assert calls == ["step-a", "step-b"]

    agent, rows = _load(session_factory, agent_id)
    assert agent.status == AgentStatus.READY
    assert agent.error is None
    assert rows["step-a"].status == StepStatus.DONE
    assert rows["step-b"].status == StepStatus.DONE
    assert rows["step-a"].error is None
    assert rows["step-a"].started_at is not None
    assert rows["step-a"].finished_at is not None
    assert rows["step-b"].started_at is not None
    assert rows["step-b"].finished_at is not None
    # per-agent scratch dir was created under config.workdir
    assert (config.workdir / "agent-happy").is_dir()


def test_second_step_raises_fails_agent_with_message(session_factory, config):
    agent_id = _make_agent(session_factory, "agent-fails")
    calls: list[str] = []
    steps = [
        RecordingStep(name="step-a", calls=calls),
        RecordingStep(name="step-b", calls=calls, fail_times=1),
    ]

    thread = run_pipeline(
        session_factory, agent_id, config=config, clients=NO_CLIENTS, steps=steps
    )
    thread.join(timeout=JOIN_TIMEOUT)
    assert thread.is_alive() is False

    agent, rows = _load(session_factory, agent_id)
    assert agent.status == AgentStatus.FAILED
    assert agent.error == "step-b boom"
    assert rows["step-a"].status == StepStatus.DONE
    assert rows["step-b"].status == StepStatus.FAILED
    assert rows["step-b"].error == "step-b boom"


def test_failure_marks_remaining_steps_aborted(session_factory, config):
    agent_id = _make_agent(session_factory, "agent-aborted")
    calls: list[str] = []
    steps = [
        RecordingStep(name="step-a", calls=calls, fail_times=1),
        RecordingStep(name="step-b", calls=calls),
        RecordingStep(name="step-c", calls=calls),
    ]

    thread = run_pipeline(
        session_factory, agent_id, config=config, clients=NO_CLIENTS, steps=steps
    )
    thread.join(timeout=JOIN_TIMEOUT)
    assert thread.is_alive() is False

    # step-b/c never actually ran
    assert calls == ["step-a"]

    agent, rows = _load(session_factory, agent_id)
    assert agent.status == AgentStatus.FAILED
    assert agent.error == "step-a boom"
    assert rows["step-a"].status == StepStatus.FAILED
    assert rows["step-a"].error == "step-a boom"
    assert rows["step-b"].status == StepStatus.FAILED
    assert rows["step-b"].error == "aborted: step-a"
    assert rows["step-c"].status == StepStatus.FAILED
    assert rows["step-c"].error == "aborted: step-a"


def test_resume_after_reset_only_reruns_failed_step(session_factory, config):
    agent_id = _make_agent(session_factory, "agent-resume")
    calls: list[str] = []
    step_a = RecordingStep(name="step-a", calls=calls)
    step_b = RecordingStep(name="step-b", calls=calls, fail_times=1)
    steps = [step_a, step_b]

    thread = run_pipeline(
        session_factory, agent_id, config=config, clients=NO_CLIENTS, steps=steps
    )
    thread.join(timeout=JOIN_TIMEOUT)
    assert thread.is_alive() is False
    assert calls == ["step-a", "step-b"]

    session = session_factory()
    agent = session.get(Agent, agent_id)
    assert agent.status == AgentStatus.FAILED
    reset_failed_steps(session, agent_id)
    session.close()

    thread2 = run_pipeline(
        session_factory, agent_id, config=config, clients=NO_CLIENTS, steps=steps
    )
    thread2.join(timeout=JOIN_TIMEOUT)
    assert thread2.is_alive() is False

    # step-a's row was already DONE, so resume must NOT re-execute it; step-b
    # is the only one that runs again (and this time it succeeds).
    assert calls == ["step-a", "step-b", "step-b"]

    agent, rows = _load(session_factory, agent_id)
    assert agent.status == AgentStatus.READY
    assert agent.error is None
    assert rows["step-a"].status == StepStatus.DONE
    assert rows["step-b"].status == StepStatus.DONE
    assert rows["step-b"].error is None


def test_steps_dropped_from_a_later_run_are_left_untouched(session_factory, config):
    agent_id = _make_agent(session_factory, "agent-subset")
    calls: list[str] = []
    first_run_steps = [
        RecordingStep(name="step-a", calls=calls),
        RecordingStep(name="step-old", calls=calls),
    ]

    thread = run_pipeline(
        session_factory, agent_id, config=config, clients=NO_CLIENTS, steps=first_run_steps
    )
    thread.join(timeout=JOIN_TIMEOUT)
    assert thread.is_alive() is False

    # A later pipeline definition drops step-old entirely.
    calls.clear()
    second_run_steps = [RecordingStep(name="step-a", calls=calls)]
    thread2 = run_pipeline(
        session_factory, agent_id, config=config, clients=NO_CLIENTS, steps=second_run_steps
    )
    thread2.join(timeout=JOIN_TIMEOUT)
    assert thread2.is_alive() is False

    # step-a was already DONE -> skipped, not re-run
    assert calls == []

    agent, rows = _load(session_factory, agent_id)
    assert agent.status == AgentStatus.READY
    assert rows["step-old"].status == StepStatus.DONE  # untouched leftover row
    assert rows["step-a"].status == StepStatus.DONE


def test_engine_level_failure_before_any_step_marks_agent_failed(session_factory, config):
    """A failure outside a step's run() (here: workdir creation) must still
    land the agent in FAILED - not leave it wedged in PROVISIONING with no
    FAILED step for `reset_failed_steps`/a retry route to act on.
    """
    # config.workdir points at a plain file, so `workdir.mkdir(...)` inside
    # the engine raises NotADirectoryError before any step ever gets to run.
    blocking_file = config.workdir
    blocking_file.parent.mkdir(parents=True, exist_ok=True)
    blocking_file.write_text("not a directory")

    agent_id = _make_agent(session_factory, "agent-engine-fail")
    calls: list[str] = []
    steps = [RecordingStep(name="step-a", calls=calls)]

    thread = run_pipeline(
        session_factory, agent_id, config=config, clients=NO_CLIENTS, steps=steps
    )
    thread.join(timeout=JOIN_TIMEOUT)
    assert thread.is_alive() is False

    # The failure happened before the step loop, so no step ever ran.
    assert calls == []

    agent, rows = _load(session_factory, agent_id)
    assert agent.status == AgentStatus.FAILED
    assert agent.error  # non-empty
    # No step ever got as far as RUNNING (rows may be absent or PENDING).
    assert all(row.status != StepStatus.RUNNING for row in rows.values())


def test_unknown_agent_id_completes_without_raising(session_factory, config):
    """An agent_id that doesn't exist must not crash the daemon thread with an
    AttributeError; it should just log and return, leaving no trace.
    """
    calls: list[str] = []
    steps = [RecordingStep(name="step-a", calls=calls)]
    unknown_agent_id = 999_999

    thread = run_pipeline(
        session_factory, unknown_agent_id, config=config, clients=NO_CLIENTS, steps=steps
    )
    thread.join(timeout=JOIN_TIMEOUT)
    assert thread.is_alive() is False

    assert calls == []

    session = session_factory()
    assert session.query(Agent).count() == 0
    session.close()


def test_reset_failed_steps_only_resets_failed_rows(session_factory, config):
    agent_id = _make_agent(session_factory, "agent-reset-scope")
    calls: list[str] = []
    steps = [
        RecordingStep(name="step-a", calls=calls),
        RecordingStep(name="step-b", calls=calls, fail_times=1),
    ]
    thread = run_pipeline(
        session_factory, agent_id, config=config, clients=NO_CLIENTS, steps=steps
    )
    thread.join(timeout=JOIN_TIMEOUT)
    assert thread.is_alive() is False

    session = session_factory()
    reset_failed_steps(session, agent_id)
    agent = session.get(Agent, agent_id)
    rows = {s.name: s for s in agent.steps}
    assert rows["step-a"].status == StepStatus.DONE  # never touched: wasn't FAILED
    assert rows["step-b"].status == StepStatus.PENDING
    assert rows["step-b"].error is None
    assert rows["step-b"].started_at is None
    assert rows["step-b"].finished_at is None
    session.close()


def _run_to_failure(session_factory, config, agent_id, steps):
    thread = run_pipeline(
        session_factory, agent_id, config=config, clients=NO_CLIENTS, steps=steps
    )
    thread.join(timeout=JOIN_TIMEOUT)
    assert thread.is_alive() is False


def test_reset_failed_steps_rewires_when_the_build_failed(session_factory, config):
    """A build or rollout that fails is, more often than not, wiring that did
    not land: the Secret the Deployment names is not there, so the pod never
    starts. The wiring step is idempotent, and re-running it is what
    re-delivers that Secret. So a retry after a build or deploy failure runs
    the wiring again rather than only the step that noticed."""
    agent_id = _make_agent(session_factory, "agent-rewire")
    calls: list[str] = []
    steps = [
        RecordingStep(name="wire", calls=calls),
        RecordingStep(name="build", calls=calls, fail_times=1),
    ]
    _run_to_failure(session_factory, config, agent_id, steps)

    session = session_factory()
    reset_failed_steps(session, agent_id)
    rows = {s.name: s for s in session.get(Agent, agent_id).steps}
    assert rows["wire"].status == StepStatus.PENDING
    assert rows["wire"].error is None
    assert rows["build"].status == StepStatus.PENDING
    session.close()

    _run_to_failure(session_factory, config, agent_id, steps)
    assert calls == ["wire", "build", "wire", "build"]


def test_reset_failed_steps_rewires_when_the_deploy_failed(session_factory, config):
    agent_id = _make_agent(session_factory, "agent-rewire-deploy")
    calls: list[str] = []
    steps = [
        RecordingStep(name="wire", calls=calls),
        RecordingStep(name="build", calls=calls),
        RecordingStep(name="deploy", calls=calls, fail_times=1),
    ]
    _run_to_failure(session_factory, config, agent_id, steps)

    session = session_factory()
    reset_failed_steps(session, agent_id)
    rows = {s.name: s for s in session.get(Agent, agent_id).steps}
    assert rows["wire"].status == StepStatus.PENDING
    # The build is reset with the wiring: a re-run wiring step may push a
    # commit and ask the platform to reprovision, and the CI run that starts
    # then has to be watched by somebody before the rollout is judged.
    assert rows["build"].status == StepStatus.PENDING
    assert rows["deploy"].status == StepStatus.PENDING
    session.close()


def test_reset_failed_steps_does_not_rewire_a_connected_agent(session_factory, config):
    """A connected agent's wiring step proposes a pull request in a repo the
    room does not own and delivers no Secret, so re-running it repairs
    nothing and can newly fail for reasons unrelated to the build. Its
    retry re-runs only what failed."""
    agent_id = _make_agent(session_factory, "agent-connected", kind=KIND_CONNECTED)
    calls: list[str] = []
    steps = [
        RecordingStep(name="wire", calls=calls),
        RecordingStep(name="build", calls=calls, fail_times=1),
    ]
    _run_to_failure(session_factory, config, agent_id, steps)

    session = session_factory()
    reset_failed_steps(session, agent_id)
    rows = {s.name: s for s in session.get(Agent, agent_id).steps}
    assert rows["wire"].status == StepStatus.DONE
    assert rows["build"].status == StepStatus.PENDING
    session.close()


def test_reset_failed_steps_leaves_the_wiring_alone_for_a_later_failure(session_factory, config):
    agent_id = _make_agent(session_factory, "agent-no-rewire")
    calls: list[str] = []
    steps = [
        RecordingStep(name="wire", calls=calls),
        RecordingStep(name="build", calls=calls),
        RecordingStep(name="deploy", calls=calls),
        RecordingStep(name="hello", calls=calls, fail_times=1),
    ]
    _run_to_failure(session_factory, config, agent_id, steps)

    session = session_factory()
    reset_failed_steps(session, agent_id)
    rows = {s.name: s for s in session.get(Agent, agent_id).steps}
    assert rows["wire"].status == StepStatus.DONE
    assert rows["hello"].status == StepStatus.PENDING
    session.close()


# --- sweep_interrupted -------------------------------------------------------------


def _seed_agent(
    session_factory,
    name: str,
    status: AgentStatus,
    *,
    age_s: float = RUN_STALE_AFTER_S * 2,
    heartbeat_age_s: float | None = None,
) -> int:
    """An agent row `age_s` old (default: comfortably older than the
    staleness threshold, which is what an in-flight row left behind by a
    PRIOR process looks like), whose heartbeat is `heartbeat_age_s` old --
    None meaning no heartbeat at all, the shape of every row written
    before the column existed.
    """
    now = datetime.now(UTC).replace(tzinfo=None)
    session = session_factory()
    agent = Agent(
        name=name,
        persona="helpful-generalist",
        model_id="vertex_ai/claude-sonnet-4-5",
        status=status,
        created_at=now - timedelta(seconds=age_s),
        run_heartbeat_at=(
            None if heartbeat_age_s is None else now - timedelta(seconds=heartbeat_age_s)
        ),
    )
    session.add(agent)
    session.commit()
    agent_id = agent.id
    session.close()
    return agent_id


def _seed_step(session_factory, agent_id: int, name: str, status: StepStatus) -> None:
    session = session_factory()
    session.add(AgentStep(agent_id=agent_id, name=name, status=status))
    session.commit()
    session.close()


def test_sweep_interrupted_flips_pending_and_provisioning_agents_and_running_steps(
    session_factory, config
):
    """A mixed-status fixture: PENDING/PROVISIONING agents (the ones a prior
    process death would leave wedged) alongside READY/FAILED agents that must
    be left alone, plus RUNNING/DONE steps under the wedged agents to prove
    the sweep only touches RUNNING rows, not DONE ones.
    """
    pending_id = _seed_agent(session_factory, "agent-pending", AgentStatus.PENDING)
    provisioning_id = _seed_agent(
        session_factory, "agent-provisioning", AgentStatus.PROVISIONING
    )
    ready_id = _seed_agent(session_factory, "agent-ready", AgentStatus.READY)
    failed_id = _seed_agent(session_factory, "agent-failed", AgentStatus.FAILED)

    _seed_step(session_factory, provisioning_id, "repo", StepStatus.DONE)
    _seed_step(session_factory, provisioning_id, "onboard", StepStatus.RUNNING)
    _seed_step(session_factory, pending_id, "repo", StepStatus.RUNNING)
    _seed_step(session_factory, ready_id, "repo", StepStatus.DONE)

    swept = sweep_interrupted(session_factory)

    assert swept == 2

    session = session_factory()
    pending = session.get(Agent, pending_id)
    provisioning = session.get(Agent, provisioning_id)
    ready = session.get(Agent, ready_id)
    failed = session.get(Agent, failed_id)

    assert pending.status == AgentStatus.FAILED
    assert pending.error == "interrupted by an API restart — retry when ready"
    assert provisioning.status == AgentStatus.FAILED
    assert provisioning.error == "interrupted by an API restart — retry when ready"
    # READY/FAILED agents are untouched: not among the wedged statuses.
    assert ready.status == AgentStatus.READY
    assert failed.status == AgentStatus.FAILED
    assert failed.error is None

    provisioning_steps = {s.name: s for s in provisioning.steps}
    pending_steps = {s.name: s for s in pending.steps}
    ready_steps = {s.name: s for s in ready.steps}

    assert provisioning_steps["repo"].status == StepStatus.DONE  # untouched
    assert provisioning_steps["onboard"].status == StepStatus.FAILED
    assert provisioning_steps["onboard"].error == "interrupted"
    assert pending_steps["repo"].status == StepStatus.FAILED
    assert pending_steps["repo"].error == "interrupted"
    assert ready_steps["repo"].status == StepStatus.DONE  # untouched

    session.close()


def test_sweep_interrupted_leaves_a_live_run_alone(session_factory, config):
    """The rolling-update case: a NEW pod boots while the OLD pod is still
    provisioning. A boot sweep that took every in-flight row would fail a
    provision that is alive and well, and the user's retry would then run
    the same steps in two processes at once.
    """
    live_id = _seed_agent(
        session_factory, "agent-live", AgentStatus.PROVISIONING, heartbeat_age_s=0
    )
    _seed_step(session_factory, live_id, "onboard", StepStatus.RUNNING)

    assert sweep_interrupted(session_factory) == 0

    session = session_factory()
    live = session.get(Agent, live_id)
    assert live.status == AgentStatus.PROVISIONING
    assert live.error is None
    assert {s.name: s.status for s in live.steps} == {"onboard": StepStatus.RUNNING}
    session.close()


def test_sweep_interrupted_fails_a_run_whose_heartbeat_went_stale(session_factory, config):
    """And the other half: a process that died mid-provision stops beating,
    so its row IS swept -- and, unlike a boot-only sweep, it is swept by
    whatever replica notices, not only by one that happens to start after.
    """
    dead_id = _seed_agent(
        session_factory,
        "agent-dead",
        AgentStatus.PROVISIONING,
        heartbeat_age_s=RUN_STALE_AFTER_S * 2,
    )
    _seed_step(session_factory, dead_id, "onboard", StepStatus.RUNNING)

    assert sweep_interrupted(session_factory) == 1

    session = session_factory()
    dead = session.get(Agent, dead_id)
    assert dead.status == AgentStatus.FAILED
    assert {s.name: s.status for s in dead.steps} == {"onboard": StepStatus.FAILED}
    session.close()


def test_sweep_interrupted_fails_an_old_row_that_never_had_a_heartbeat(
    session_factory, config
):
    # Every row written before this column existed. An in-flight row that
    # old was left behind by a process that is long gone.
    legacy_id = _seed_agent(
        session_factory, "agent-legacy", AgentStatus.PROVISIONING, heartbeat_age_s=None
    )

    assert sweep_interrupted(session_factory) == 1

    session = session_factory()
    assert session.get(Agent, legacy_id).status == AgentStatus.FAILED
    session.close()


def test_sweep_interrupted_leaves_a_just_created_row_without_a_heartbeat_alone(
    session_factory, config
):
    # A create that landed a moment ago: the row exists, the thread is
    # starting, the first beat has not been written yet. Sweeping it would
    # fail a provision seconds after the user asked for it.
    fresh_id = _seed_agent(
        session_factory,
        "agent-fresh",
        AgentStatus.PENDING,
        age_s=0,
        heartbeat_age_s=None,
    )

    assert sweep_interrupted(session_factory) == 0

    session = session_factory()
    assert session.get(Agent, fresh_id).status == AgentStatus.PENDING
    session.close()


def _heartbeat_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith(HEARTBEAT_THREAD_NAME)]


def test_run_pipeline_keeps_the_heartbeat_fresh_for_the_whole_run(
    session_factory, config
):
    """Including during ONE long step: the build wait is a single step that
    can run for minutes, so a heartbeat written only at step boundaries
    would go stale in the middle of a perfectly healthy provision.
    """
    seen: list[datetime | None] = []

    @dataclass
    class SlowStep:
        name: str = "slow"

        def run(self, ctx) -> str:
            for _ in range(40):
                session = session_factory()
                try:
                    beat = session.get(Agent, ctx.agent.id).run_heartbeat_at
                finally:
                    session.close()
                if beat is not None and beat not in seen:
                    seen.append(beat)
                if len(seen) >= 2:
                    break
                time.sleep(0.01)
            return "ok"

    agent_id = _make_agent(session_factory, "agent-beating")
    thread = run_pipeline(
        session_factory,
        agent_id,
        config=config,
        clients=NO_CLIENTS,
        steps=[SlowStep()],
        heartbeat_interval_s=0.01,
    )
    thread.join(timeout=JOIN_TIMEOUT)

    # At least two DISTINCT beats landed inside one step's run.
    assert len(seen) >= 2


def test_run_pipeline_stops_the_heartbeat_when_the_run_ends(session_factory, config):
    before = len(_heartbeat_threads())
    agent_id = _make_agent(session_factory, "agent-stops")
    calls: list[str] = []

    thread = run_pipeline(
        session_factory,
        agent_id,
        config=config,
        clients=NO_CLIENTS,
        steps=[RecordingStep(name="step-a", calls=calls)],
        heartbeat_interval_s=0.01,
    )
    thread.join(timeout=JOIN_TIMEOUT)

    # The ticker is joined by the run itself, so it is already gone by the
    # time the pipeline thread returns -- no per-provision thread leak.
    assert len(_heartbeat_threads()) == before

    session = session_factory()
    beat = session.get(Agent, agent_id).run_heartbeat_at
    session.close()
    time.sleep(0.05)  # several intervals: a live ticker would move it
    session = session_factory()
    assert session.get(Agent, agent_id).run_heartbeat_at == beat
    session.close()


def test_run_pipeline_stops_the_heartbeat_when_a_step_fails(session_factory, config):
    before = len(_heartbeat_threads())
    agent_id = _make_agent(session_factory, "agent-stops-on-failure")
    calls: list[str] = []

    thread = run_pipeline(
        session_factory,
        agent_id,
        config=config,
        clients=NO_CLIENTS,
        steps=[RecordingStep(name="step-a", calls=calls, fail_times=1)],
        heartbeat_interval_s=0.01,
    )
    thread.join(timeout=JOIN_TIMEOUT)

    assert len(_heartbeat_threads()) == before
    agent, _ = _load(session_factory, agent_id)
    assert agent.status == AgentStatus.FAILED


def test_a_heartbeat_that_cannot_start_does_not_cost_the_provision(
    session_factory, config, monkeypatch
):
    # A run without a heartbeat is recoverable -- the sweep fails it and
    # the user retries. A run that never happens leaves an agent wedged
    # with nothing to resume. So the ticker is allowed to fail.
    import kobo_api.provisioner as provisioner_module

    def refuse_to_start(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(provisioner_module._Heartbeat, "start", refuse_to_start)

    agent_id = _make_agent(session_factory, "agent-no-ticker")
    calls: list[str] = []
    run_pipeline(
        session_factory,
        agent_id,
        config=config,
        clients=NO_CLIENTS,
        steps=[RecordingStep(name="step-a", calls=calls)],
    ).join(timeout=JOIN_TIMEOUT)

    agent, rows = _load(session_factory, agent_id)
    assert agent.status == AgentStatus.READY
    assert calls == ["step-a"]
    assert rows["step-a"].status == StepStatus.DONE


def test_retry_after_a_sweep_still_resumes_the_pipeline(session_factory, config):
    """The whole point of the sweep: a genuinely dead run lands in FAILED,
    where the existing retry path can pick it back up.
    """
    agent_id = _make_agent(session_factory, "agent-resumed")
    calls: list[str] = []
    steps = [
        RecordingStep(name="step-a", calls=calls),
        RecordingStep(name="step-b", calls=calls),
    ]

    session = session_factory()
    agent = session.get(Agent, agent_id)
    agent.status = AgentStatus.PROVISIONING
    agent.created_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(
        seconds=RUN_STALE_AFTER_S * 2
    )
    session.add(AgentStep(agent_id=agent_id, name="step-a", status=StepStatus.RUNNING))
    session.commit()
    session.close()

    assert sweep_interrupted(session_factory) == 1

    session = session_factory()
    reset_failed_steps(session, agent_id)
    session.close()

    run_pipeline(
        session_factory, agent_id, config=config, clients=NO_CLIENTS, steps=steps
    ).join(timeout=JOIN_TIMEOUT)

    agent, rows = _load(session_factory, agent_id)
    assert agent.status == AgentStatus.READY
    assert calls == ["step-a", "step-b"]
    assert {name: row.status for name, row in rows.items()} == {
        "step-a": StepStatus.DONE,
        "step-b": StepStatus.DONE,
    }


def test_sweep_interrupted_is_a_noop_on_a_clean_table(session_factory, config):
    """No PENDING/PROVISIONING agents at all (every prior provision either
    finished or already failed on its own) - returns 0 and leaves every row
    exactly as it was.
    """
    ready_id = _seed_agent(session_factory, "agent-clean-ready", AgentStatus.READY)
    _seed_step(session_factory, ready_id, "repo", StepStatus.DONE)

    swept = sweep_interrupted(session_factory)

    assert swept == 0

    session = session_factory()
    ready = session.get(Agent, ready_id)
    assert ready.status == AgentStatus.READY
    assert {s.name: s.status for s in ready.steps} == {"repo": StepStatus.DONE}
    session.close()


# --- dispatch-pairs sync on the READY flip (Task C4: the room becomes the
# switchboard's own policy source) ---------------------------------------------


def _cabal_enabled_config(config):
    return config.model_copy(
        update={
            "cabal_url": "http://cabal.internal",
            "agent_endpoint_template": "http://agent-{name}.default.svc.cluster.local",
        }
    )


def test_ready_flip_syncs_dispatch_pairs_when_cabal_enabled(session_factory, config, monkeypatch):
    """A brand-new agent flipping to READY joins its group's implicit
    pairs -- the switchboard push right after the flip must carry both
    directions of that pair, computed fresh off the DB, not anything this
    test hands the pipeline directly."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _cabal_enabled_config(config)
    _seed_ready_agent(session_factory, "b", "growth")
    agent_id = _make_agent_with_group(session_factory, "a", "growth")
    fake_cabal = FakeCabal()
    clients = Clients(
        github=None, gojira=None, cloud=None, agent=None, kubectl=None, cabal=fake_cabal
    )

    thread = run_pipeline(
        session_factory,
        agent_id,
        config=cfg,
        clients=clients,
        steps=[RecordingStep(name="step-a", calls=[])],
    )
    thread.join(timeout=JOIN_TIMEOUT)
    assert thread.is_alive() is False

    assert fake_cabal.put_dispatch_pairs_calls == [
        sorted({("agent:a", "b"), ("agent:b", "a")})
    ]


def test_ready_flip_does_not_sync_when_cabal_disabled(session_factory, config):
    agent_id = _make_agent_with_group(session_factory, "a", "growth")
    fake_cabal = FakeCabal()
    clients = Clients(
        github=None, gojira=None, cloud=None, agent=None, kubectl=None, cabal=fake_cabal
    )

    thread = run_pipeline(
        session_factory,
        agent_id,
        config=config,
        clients=clients,
        steps=[RecordingStep(name="step-a", calls=[])],
    )
    thread.join(timeout=JOIN_TIMEOUT)
    assert thread.is_alive() is False

    assert fake_cabal.put_dispatch_pairs_calls == []


def test_ready_flip_sync_does_not_fire_when_the_pipeline_fails(
    session_factory, config, monkeypatch
):
    # A failed run never reaches AgentStatus.READY -- the sync trigger, tied
    # to that exact transition, must never fire for it either.
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _cabal_enabled_config(config)
    agent_id = _make_agent_with_group(session_factory, "a", "growth")
    fake_cabal = FakeCabal()
    clients = Clients(
        github=None, gojira=None, cloud=None, agent=None, kubectl=None, cabal=fake_cabal
    )

    thread = run_pipeline(
        session_factory,
        agent_id,
        config=cfg,
        clients=clients,
        steps=[RecordingStep(name="step-a", calls=[], fail_times=99)],
    )
    thread.join(timeout=JOIN_TIMEOUT)
    assert thread.is_alive() is False

    agent, _ = _load(session_factory, agent_id)
    assert agent.status == AgentStatus.FAILED
    assert fake_cabal.put_dispatch_pairs_calls == []


def test_ready_flip_completes_even_when_dispatch_pairs_sync_fails(
    session_factory, config, monkeypatch
):
    """Fail-soft: a CabalError from put_dispatch_pairs must never leave the
    agent stuck out of READY or crash the pipeline thread."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _cabal_enabled_config(config)
    agent_id = _make_agent_with_group(session_factory, "a", "growth")
    fake_cabal = FakeCabal(put_dispatch_pairs_error="switchboard unreachable")
    clients = Clients(
        github=None, gojira=None, cloud=None, agent=None, kubectl=None, cabal=fake_cabal
    )

    thread = run_pipeline(
        session_factory,
        agent_id,
        config=cfg,
        clients=clients,
        steps=[RecordingStep(name="step-a", calls=[])],
    )
    thread.join(timeout=JOIN_TIMEOUT)
    assert thread.is_alive() is False

    agent, _ = _load(session_factory, agent_id)
    assert agent.status == AgentStatus.READY


# --- Two processes, one database (Task 13: the step engine under two
# processes) --------------------------------------------------------------
#
# `run_pipeline`'s own docstring already admits it: "Not safe to call twice
# concurrently for the same agent_id ... callers must serialize that
# themselves." The retry route (kobo_api.main.retry_agent) tries to close
# that window by flipping the agent to PROVISIONING synchronously before
# firing the pipeline thread -- but that flip is itself a check-then-act
# read of `agent.status` followed by a plain write, which only serializes
# two THREADS in the same process. Two REPLICA PODS (two OS processes, one
# SQLite file or one shared Postgres) can both read FAILED before either
# commits PROVISIONING, and both go on to call `run_pipeline` for the same
# agent_id -- exactly the scenario this test spawns for real, as two actual
# subprocesses, rather than two threads sharing one interpreter (which would
# prove nothing about cross-process safety: threads already serialize on the
# GIL for anything short of real I/O).
#
# The worker script below is standalone (not a pytest module) precisely so
# it can be launched via `subprocess.Popen([sys.executable, ...])` as a
# genuinely separate process with its own interpreter, its own SQLAlchemy
# engine, and its own connection to the one shared SQLite file -- proving
# whatever claim/lock kobo_api.provisioner uses is enforced by the DATABASE,
# not by anything held in a single process's memory.

_CLAIM_RACE_WORKER = '''\
import sys
import time
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.clients import Clients
from kobo_api.config import Config
from kobo_api.provisioner import run_pipeline


class DoneAlreadyStep:
    """Stands in for a step this agent already completed before either
    worker process starts -- if either process's engine ever re-executes
    this (a step-loop bug, not the claim bug this test targets), raising
    surfaces it immediately as a FAILED agent the parent test's own
    assertions catch."""

    name = "step-a"
    label = "step-a"

    def run(self, ctx):
        raise AssertionError("step-a is already DONE and must never run again")


class SlowMarkerStep:
    """The contested step: records that it started (so the parent test can
    wait for worker A to have actually claimed it before starting worker B),
    then optionally sleeps (worker A only) to hold the claim open across
    worker B's own attempt, then appends one line to a marker file every
    successful run is expected to write EXACTLY ONCE across both workers."""

    name = "step-b"
    label = "step-b"

    def __init__(self, marker_path, started_path, sleep_s):
        self.marker_path = marker_path
        self.started_path = started_path
        self.sleep_s = sleep_s

    def run(self, ctx):
        import os

        Path(self.started_path).write_text("started\\n")
        if self.sleep_s:
            time.sleep(self.sleep_s)
        with open(self.marker_path, "a") as f:
            f.write(f"ran:{os.getpid()}\\n")
        return "ok"


def main() -> None:
    db_path, agent_id, marker_path, started_path, sleep_s, workdir = sys.argv[1:7]
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    config = Config(workdir=Path(workdir))
    clients = Clients(github=None, gojira=None, cloud=None, agent=None, kubectl=None)
    steps = [DoneAlreadyStep(), SlowMarkerStep(marker_path, started_path, float(sleep_s))]

    thread = run_pipeline(
        session_factory, int(agent_id), config=config, clients=clients, steps=steps
    )
    thread.join(timeout=30)


if __name__ == "__main__":
    main()
'''


def _wait_for_file(path: Path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path} to appear")


def test_two_processes_racing_a_failed_steps_resume_run_it_exactly_once(tmp_path):
    """Two real OS processes, one shared SQLite file, both calling
    `run_pipeline` for the SAME already-reset agent at (nearly) the same
    time: the contested step must execute EXACTLY ONCE, and both processes
    must exit cleanly with the agent landing in a single consistent final
    state (READY, the step DONE) -- never both racing to run it, and never
    one of them crashing or wedging the agent.

    Deterministic without hair-trigger timing: worker A's step body writes
    its own "started" marker BEFORE sleeping, and worker B is only launched
    once that marker exists -- so by the time worker B's own pipeline loop
    reads the step's row, it is already RUNNING (claimed by A), not PENDING.
    Against the current (pre-fix) engine, RUNNING is not in the loop's
    skip-set (only DONE/SKIPPED are), so worker B re-executes the step
    anyway -- a second `ran:<pid>` line in the marker file, the double-
    execution bug this test is written to catch. The fix must make worker
    B's own claim attempt fail (rowcount 0) and back off instead.
    """
    db_path = tmp_path / "kobo.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    init_db(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    session = session_factory()
    agent = Agent(
        name="agent-two-proc", persona="helpful-generalist",
        model_id="vertex_ai/claude-sonnet-4-5",
    )
    session.add(agent)
    session.commit()
    agent_id = agent.id
    # Pre-seed the ledger as if a prior run already finished step-a and
    # failed on step-b, then `reset_failed_steps` already ran (the retry
    # route's own sequence) -- step-b is PENDING, exactly the state both
    # worker processes below race to claim.
    session.add(AgentStep(agent_id=agent_id, name="step-a", status=StepStatus.DONE))
    session.add(AgentStep(agent_id=agent_id, name="step-b", status=StepStatus.PENDING))
    session.commit()
    session.close()

    worker_script = tmp_path / "claim_race_worker.py"
    worker_script.write_text(_CLAIM_RACE_WORKER)
    marker_path = tmp_path / "marker.txt"
    started_a = tmp_path / "started-a.txt"
    started_b = tmp_path / "started-b.txt"
    workdir_a = tmp_path / "work-a"
    workdir_b = tmp_path / "work-b"

    proc_b = None
    proc_a = subprocess.Popen(
        [
            sys.executable, str(worker_script), str(db_path), str(agent_id),
            str(marker_path), str(started_a), "1.5", str(workdir_a),
        ],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        # Wait until A has actually claimed step-b (its row is RUNNING in
        # the DB) before starting B -- the deterministic ordering the
        # docstring above explains.
        _wait_for_file(started_a, timeout=10)

        proc_b = subprocess.Popen(
            [
                sys.executable, str(worker_script), str(db_path), str(agent_id),
                str(marker_path), str(started_b), "0", str(workdir_b),
            ],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        out_b, _ = proc_b.communicate(timeout=30)
        out_a, _ = proc_a.communicate(timeout=30)
    finally:
        for p in (proc_a, proc_b):
            if p is not None and p.poll() is None:
                p.kill()

    assert proc_a.returncode == 0, f"worker A crashed:\n{out_a}"
    assert proc_b.returncode == 0, f"worker B crashed:\n{out_b}"

    marker_lines = marker_path.read_text().splitlines() if marker_path.exists() else []
    assert len(marker_lines) == 1, (
        f"step-b ran {len(marker_lines)} time(s) across the two processes "
        f"(expected exactly 1): {marker_lines}\nworker A output:\n{out_a}\n"
        f"worker B output:\n{out_b}"
    )

    session = session_factory()
    agent = session.get(Agent, agent_id)
    rows = {s.name: s for s in agent.steps}
    assert agent.status == AgentStatus.READY
    assert agent.error is None
    assert rows["step-a"].status == StepStatus.DONE
    assert rows["step-b"].status == StepStatus.DONE
    session.close()


# --- waiting: a step parked on a person (Ember's review 2026-09-29, point 2) ----


@dataclass
class WaitingStep:
    """Raises StepWaiting `wait_times` times (decrementing), then returns."""

    name: str
    calls: list[str]
    wait_times: int = 1
    message: str = "Waiting for someone to merge: https://github.com/o/r/pull/9"

    def run(self, ctx) -> str:
        self.calls.append(self.name)
        if self.wait_times > 0:
            self.wait_times -= 1
            raise StepWaiting(self.message)
        return "ok"


def _run(session_factory, agent_id, config, steps, clients=NO_CLIENTS):
    thread = run_pipeline(session_factory, agent_id, config=config, clients=clients, steps=steps)
    thread.join(timeout=JOIN_TIMEOUT)
    assert thread.is_alive() is False


def test_a_waiting_step_parks_the_run_without_failing_or_aborting_anything(
    session_factory, config
):
    """Not a failure: the step and the agent both say WAITING and carry the
    reason (the PR link), the steps after it are left untouched rather than
    aborted, and nothing is marked failed."""
    agent_id = _make_agent(session_factory, "agent-parks", kind=KIND_CONNECTED)
    calls: list[str] = []
    steps = [
        RecordingStep(name="step-a", calls=calls),
        WaitingStep(name="step-b", calls=calls),
        RecordingStep(name="step-c", calls=calls),
    ]

    _run(session_factory, agent_id, config, steps)

    agent, rows = _load(session_factory, agent_id)
    assert agent.status == AgentStatus.WAITING
    assert "pull/9" in (agent.error or "")
    assert rows["step-a"].status == StepStatus.DONE
    assert rows["step-b"].status == StepStatus.WAITING
    assert "pull/9" in rows["step-b"].error
    assert "step-c" not in rows  # never reached, never aborted
    assert calls == ["step-a", "step-b"]


def test_reset_turns_a_waiting_step_back_to_pending_and_the_rerun_finishes(
    session_factory, config
):
    agent_id = _make_agent(session_factory, "agent-resumes", kind=KIND_CONNECTED)
    calls: list[str] = []
    waiting = WaitingStep(name="step-b", calls=calls)
    steps = [RecordingStep(name="step-a", calls=calls), waiting, RecordingStep(name="step-c", calls=calls)]
    _run(session_factory, agent_id, config, steps)

    session = session_factory()
    reset_failed_steps(session, agent_id)
    rows = {s.name: s.status for s in session.get(Agent, agent_id).steps}
    session.close()
    assert rows["step-b"] == StepStatus.PENDING  # the resume primitive
    assert rows["step-a"] == StepStatus.DONE

    _run(session_factory, agent_id, config, steps)  # the wait is over: waiting.wait_times is 0

    agent, rows = _load(session_factory, agent_id)
    assert agent.status == AgentStatus.READY
    assert agent.error is None
    assert [rows[n].status for n in ("step-a", "step-b", "step-c")] == [StepStatus.DONE] * 3
    assert calls == ["step-a", "step-b", "step-b", "step-c"]


def _park(session_factory, name: str, app_id: int) -> int:
    """An agent already parked on the platform's app `app_id`, its onboard
    step WAITING, the way `_run_pipeline_body` leaves it."""
    session = session_factory()
    agent = Agent(
        name=name,
        persona="connected",
        model_id="vertex_ai/claude-sonnet-4-5",
        kind=KIND_CONNECTED,
        status=AgentStatus.WAITING,
        error="Waiting for someone to merge: https://github.com/o/r/pull/9",
        gojira_app_id=app_id,
    )
    session.add(agent)
    session.commit()
    session.add(AgentStep(agent_id=agent.id, name="onboard", status=StepStatus.WAITING, error=agent.error))
    session.commit()
    agent_id = agent.id
    session.close()
    return agent_id


def _gojira_clients(gojira) -> Clients:
    return Clients(github=None, gojira=gojira, cloud=None, agent=None, kubectl=None)


def test_sweep_waiting_resumes_an_agent_whose_setup_pr_was_merged(session_factory, config):
    gojira = FakeGojira()
    gojira.seed_app(7, status="active")
    agent_id = _park(session_factory, "agent-merged", 7)
    launched: list[int] = []

    def recorder(sf, aid, *, config, clients, steps):
        launched.append(aid)

    touched = sweep_waiting(
        session_factory, config=config, clients=_gojira_clients(gojira), runner=recorder
    )

    assert touched == 1
    assert launched == [agent_id]
    agent, rows = _load(session_factory, agent_id)
    assert agent.status == AgentStatus.PROVISIONING  # flipped BEFORE the run, so a second replica skips it
    assert agent.error is None
    assert agent.run_heartbeat_at is not None
    assert rows["onboard"].status == StepStatus.PENDING


def test_sweep_waiting_leaves_an_agent_still_waiting_alone(session_factory, config):
    gojira = FakeGojira()
    gojira.seed_app(7, status="awaiting_pr")
    agent_id = _park(session_factory, "agent-still-waits", 7)
    launched: list[int] = []

    touched = sweep_waiting(
        session_factory, config=config, clients=_gojira_clients(gojira),
        runner=lambda *a, **k: launched.append(1),
    )

    assert touched == 0 and launched == []
    agent, rows = _load(session_factory, agent_id)
    assert agent.status == AgentStatus.WAITING
    assert rows["onboard"].status == StepStatus.WAITING


def test_sweep_waiting_fails_an_agent_whose_platform_run_failed_while_parked(
    session_factory, config
):
    gojira = FakeGojira(failed_steps=[{"name": "generate_artifacts", "status": "failed"}])
    gojira.seed_app(7, status="failed")
    agent_id = _park(session_factory, "agent-broke-waiting", 7)

    touched = sweep_waiting(
        session_factory, config=config, clients=_gojira_clients(gojira),
        runner=lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")),
    )

    assert touched == 1
    agent, rows = _load(session_factory, agent_id)
    assert agent.status == AgentStatus.FAILED
    assert "generate_artifacts" in agent.error
    assert rows["onboard"].status == StepStatus.FAILED
    assert "generate_artifacts" in rows["onboard"].error


def test_sweep_waiting_syncs_so_a_merge_without_a_webhook_resumes(session_factory, config):
    # Issue #118: with no webhook the platform's row says awaiting_pr until
    # someone asks it to look. The sweep asks before it reads.
    gojira = FakeGojira()
    gojira.seed_app(7, status="awaiting_pr")
    gojira.merged_setup_prs = {7}
    agent_id = _park(session_factory, "agent-no-webhook", 7)
    launched: list[int] = []

    touched = sweep_waiting(
        session_factory, config=config, clients=_gojira_clients(gojira),
        runner=lambda sf, aid, **k: launched.append(aid),
    )

    assert touched == 1 and launched == [agent_id]
    assert gojira.calls.index("sync_app:7") < gojira.calls.index("app_status:7")


def test_sweep_waiting_reads_status_when_the_sync_call_fails(session_factory, config):
    gojira = FakeGojira()
    gojira.seed_app(7, status="active")
    gojira.sync_error = "sync_app(7) failed (500): boom"
    agent_id = _park(session_factory, "agent-sync-fails", 7)
    launched: list[int] = []

    touched = sweep_waiting(
        session_factory, config=config, clients=_gojira_clients(gojira),
        runner=lambda sf, aid, **k: launched.append(aid),
    )

    assert touched == 1 and launched == [agent_id]


def test_sweep_waiting_is_a_noop_without_a_platform_client(session_factory, config):
    _park(session_factory, "agent-no-platform", 7)
    assert sweep_waiting(session_factory, config=config, clients=None) == 0
    assert sweep_waiting(session_factory, config=config, clients=NO_CLIENTS) == 0


# --- issue #203: destroys beat too, and the waiting sweep claims --------------


def test_sweep_interrupted_flips_a_dead_destroy_to_destroy_failed(session_factory, config):
    """A destroy that stopped beating is dead; DESTROY_FAILED is resumable by
    the next DELETE, where DESTROYING was a dead end nobody noticed."""
    dead_id = _seed_agent(
        session_factory, "agent-dead-destroy", AgentStatus.DESTROYING,
        heartbeat_age_s=RUN_STALE_AFTER_S * 2,
    )
    _seed_step(session_factory, dead_id, "destroy-platform", StepStatus.RUNNING)
    live_id = _seed_agent(
        session_factory, "agent-live-destroy", AgentStatus.DESTROYING, heartbeat_age_s=1
    )

    assert sweep_interrupted(session_factory) == 1

    session = session_factory()
    dead = session.get(Agent, dead_id)
    assert dead.status == AgentStatus.DESTROY_FAILED
    assert "destroy again" in dead.error
    assert {s.name: s.status for s in dead.steps} == {"destroy-platform": StepStatus.FAILED}
    assert session.get(Agent, live_id).status == AgentStatus.DESTROYING
    session.close()


def test_claim_waiting_moves_a_waiting_row_once_and_leaves_other_statuses_alone(session_factory):
    from kobo_api.provisioner import _claim_waiting

    waiting_id = _seed_agent(session_factory, "agent-waiting", AgentStatus.WAITING)
    destroying_id = _seed_agent(session_factory, "agent-going", AgentStatus.DESTROYING)

    session = session_factory()
    assert _claim_waiting(session, waiting_id) is True
    session.commit()
    assert session.get(Agent, waiting_id).status == AgentStatus.PROVISIONING
    assert session.get(Agent, waiting_id).run_heartbeat_at is not None
    # Claimed once: a second sweep (or replica) finds nothing to take.
    assert _claim_waiting(session, waiting_id) is False
    # A row a DELETE moved first is never flipped back.
    assert _claim_waiting(session, destroying_id) is False
    assert session.get(Agent, destroying_id).status == AgentStatus.DESTROYING
    session.close()
