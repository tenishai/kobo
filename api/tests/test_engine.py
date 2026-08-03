"""Tests for cortex_api.provisioner: the step engine's ledger persistence,
fire-and-return threading, and resumable retry. Exercised against toy Steps
defined here — real steps arrive in Tasks 6-8.
"""

from __future__ import annotations

from dataclasses import dataclass

from cortex_api.clients import Clients
from cortex_api.models import Agent, AgentStatus, AgentStep, StepStatus
from cortex_api.provisioner import reset_failed_steps, run_pipeline, sweep_interrupted

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


def _make_agent(session_factory, name: str) -> int:
    session = session_factory()
    agent = Agent(
        name=name, persona="helpful-generalist", model_id="vertex_ai/claude-sonnet-4-5"
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


# --- sweep_interrupted -------------------------------------------------------------


def _seed_agent(session_factory, name: str, status: AgentStatus) -> int:
    session = session_factory()
    agent = Agent(
        name=name,
        persona="helpful-generalist",
        model_id="vertex_ai/claude-sonnet-4-5",
        status=status,
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
