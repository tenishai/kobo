"""The build-log viewer route (2026-08-27 feedback: parity with the org's PaaS). Drives
the same create_app client as every route test; the GitHub side is the
suite's own FakeGitHub grown two members."""

import pytest
from fastapi.testclient import TestClient

from kobo_api.clients import Clients
from kobo_api.main import create_app
from kobo_api.models import Agent, AgentStatus
from tests.fakes import FakeAgent, FakeCloud, FakeGojira, FakeKubectl, FakeStorage


class LogGitHub:
    def __init__(self, run=None, log="line1\nline2", raise_on_log=False):
        self._run = run
        self._log = log
        self._raise = raise_on_log

    def latest_run(self, repo):
        return self._run

    def run_log(self, repo, run_id, *, failed_only=True):
        if self._raise:
            from kobo_api.clients import ClientError

            raise ClientError("boom")
        self.failed_only = failed_only
        return self._log


def _client(session_factory, config, github):
    clients = Clients(
        github=github,
        gojira=FakeGojira(),
        cloud=FakeCloud(),
        agent=FakeAgent(),
        kubectl=FakeKubectl(),
        storage=FakeStorage(),
    )
    return TestClient(create_app(session_factory, config=config, clients=clients))


def _seed(session_factory, repo="acme/agent-ward"):
    session = session_factory()
    session.add(
        Agent(
            name="ward",
            persona="p",
            model_id="m",
            status=AgentStatus.READY,
            repo_full_name=repo,
        )
    )
    session.commit()
    session.close()


def test_failed_run_serves_failed_log_tail(session_factory, config):
    github = LogGitHub(
        run={"id": 7, "status": "completed", "conclusion": "failure", "head_sha": "abc"}
    )
    _seed(session_factory)
    r = _client(session_factory, config, github).get("/api/agents/ward/build-log")
    assert r.status_code == 200
    body = r.json()
    assert body["run"]["conclusion"] == "failure"
    assert body["log"] == "line1\nline2"
    assert github.failed_only is True


def test_green_run_serves_whole_log(session_factory, config):
    github = LogGitHub(
        run={"id": 7, "status": "completed", "conclusion": "success", "head_sha": "abc"}
    )
    _seed(session_factory)
    r = _client(session_factory, config, github).get("/api/agents/ward/build-log")
    assert r.status_code == 200
    assert github.failed_only is False


def test_long_log_is_tailed_with_a_trim_marker(session_factory, config):
    github = LogGitHub(
        run={"id": 7, "status": "completed", "conclusion": "failure", "head_sha": "a"},
        log="\n".join(f"line{i}" for i in range(1000)),
    )
    _seed(session_factory)
    body = _client(session_factory, config, github).get("/api/agents/ward/build-log").json()
    lines = body["log"].splitlines()
    assert len(lines) == 401  # 400 + the trim marker
    assert "600 earlier lines trimmed" in lines[0]
    assert lines[-1] == "line999"


def test_no_runs_404s_and_no_repo_409s(session_factory, config):
    _seed(session_factory)
    client = _client(session_factory, config, LogGitHub(run=None))
    assert client.get("/api/agents/ward/build-log").status_code == 404

    session = session_factory()
    session.query(Agent).update({"repo_full_name": None})
    session.commit()
    session.close()
    assert client.get("/api/agents/ward/build-log").status_code == 409


def test_unknown_agent_404s(session_factory, config):
    client = _client(session_factory, config, LogGitHub())
    assert client.get("/api/agents/nobody/build-log").status_code == 404


def test_github_failure_reads_as_502(session_factory, config):
    github = LogGitHub(
        run={"id": 7, "status": "completed", "conclusion": "failure", "head_sha": "a"},
        raise_on_log=True,
    )
    _seed(session_factory)
    r = _client(session_factory, config, github).get("/api/agents/ward/build-log")
    assert r.status_code == 502
