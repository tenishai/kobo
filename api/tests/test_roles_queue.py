"""The API/worker split: roles, the queue claim, and the sweep that leaves
queued agents alone."""

from datetime import UTC, datetime, timedelta

import pytest

from kobo_api.models import Agent, AgentStatus
from kobo_api.provisioner import claim_queued, naive_utc, queue_tick, sweep_interrupted
from kobo_api.roles import (
    ROLE_ALL,
    ROLE_API,
    ROLE_WORKER,
    launches_inline,
    role_from_env,
    runs_pipelines,
    serves_requests,
)


def test_roles_parse_and_split_the_work():
    assert role_from_env({}) == ROLE_ALL
    assert role_from_env({"KOBO_ROLE": " Worker "}) == ROLE_WORKER
    with pytest.raises(ValueError):
        role_from_env({"KOBO_ROLE": "both"})

    assert launches_inline(ROLE_ALL) and not launches_inline(ROLE_API)
    assert serves_requests(ROLE_API) and not serves_requests(ROLE_WORKER)
    assert runs_pipelines(ROLE_WORKER) and not runs_pipelines(ROLE_API)


def _agent(session_factory, name, status, *, heartbeat=None, created_at=None):
    with session_factory() as session:
        agent = Agent(
            name=name,
            persona="helpful-generalist",
            team="shared",
            model_id="vertex_ai/claude-sonnet-5",
            status=status,
            run_heartbeat_at=heartbeat,
        )
        if created_at is not None:
            agent.created_at = created_at
        session.add(agent)
        session.commit()
        return agent.id


def test_claim_is_once_and_only_for_queued_rows(session_factory):
    queued = _agent(session_factory, "queued", AgentStatus.PENDING)
    _agent(session_factory, "running", AgentStatus.PROVISIONING, heartbeat=naive_utc(datetime.now(UTC)))
    _agent(session_factory, "done", AgentStatus.READY)

    assert claim_queued(session_factory, limit=5) == [queued]
    assert claim_queued(session_factory, limit=5) == []

    with session_factory() as session:
        assert session.get(Agent, queued).run_heartbeat_at is not None


def test_queue_tick_runs_each_queued_agent_once(session_factory, config):
    first = _agent(session_factory, "first", AgentStatus.PENDING)
    second = _agent(session_factory, "second", AgentStatus.PENDING)
    launched: list[int] = []

    def runner(sf, agent_id, **kwargs):
        launched.append(agent_id)
        assert kwargs["steps"] is not None

    assert queue_tick(session_factory, config=config, clients=None, runner=runner, slots=1) == 1
    assert queue_tick(session_factory, config=config, clients=None, runner=runner, slots=1) == 1
    assert queue_tick(session_factory, config=config, clients=None, runner=runner, slots=1) == 0
    assert launched == [first, second]


def test_sweep_in_queue_mode_leaves_queued_rows_for_the_worker(session_factory):
    old = naive_utc(datetime.now(UTC) - timedelta(minutes=10))
    queued = _agent(session_factory, "queued", AgentStatus.PENDING, created_at=old)
    dead = _agent(session_factory, "dead", AgentStatus.PROVISIONING, heartbeat=old)

    assert sweep_interrupted(session_factory, queue_mode=True) == 1
    with session_factory() as session:
        assert session.get(Agent, queued).status == AgentStatus.PENDING
        assert session.get(Agent, dead).status == AgentStatus.FAILED

    # The old shape still fails a never-started row once it is old enough.
    assert sweep_interrupted(session_factory, queue_mode=False) == 1
    with session_factory() as session:
        assert session.get(Agent, queued).status == AgentStatus.FAILED
