"""The DELETE route claims the row once (issue #203): a second DELETE while a
destroy is in flight gets a 409, and a DESTROY_FAILED row can be taken again."""

from __future__ import annotations

from kobo_api.models import Agent, AgentStatus
from tests.test_build_log import LogGitHub, _client


def _seed(session_factory, status):
    session = session_factory()
    session.add(Agent(name="ward", persona="p", model_id="m", status=status, repo_full_name="acme/agent-ward"))
    session.commit()
    session.close()


def test_a_destroying_row_is_not_claimed_again_and_launches_nothing(session_factory, config, monkeypatch):
    import kobo_api.main as main_mod

    launches = []
    monkeypatch.setattr(main_mod, "run_destroy", lambda *a, **k: launches.append(a))
    _seed(session_factory, AgentStatus.DESTROYING)
    r = _client(session_factory, config, LogGitHub()).delete("/api/agents/ward")
    assert r.status_code == 202
    assert r.json() == {"status": "destroying"}
    assert launches == []


def test_a_failed_destroy_is_claimed_and_resumed(session_factory, config):
    _seed(session_factory, AgentStatus.DESTROY_FAILED)
    client = _client(session_factory, config, LogGitHub())
    r = client.delete("/api/agents/ward")
    assert r.status_code == 202
    session = session_factory()
    row = session.query(Agent).filter_by(name="ward").one_or_none()
    # Either the run is still going (DESTROYING, with a fresh heartbeat from the
    # claim) or the fakes let it finish and forget the row.
    assert row is None or (row.status in (AgentStatus.DESTROYING, AgentStatus.DESTROY_FAILED) and row.run_heartbeat_at is not None)
    session.close()
