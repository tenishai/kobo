"""PUT /api/agents/{name}/colleague-mode (issue #131): the owner's colleague
mode moves the agent's repository (allow list and colleagues.md) and the
row together, or neither."""

from __future__ import annotations

from pathlib import Path

import yaml
from fastapi.testclient import TestClient
from sqlalchemy import event

from kobo_api.clients import Clients
from kobo_api.composer import BundleSpec, compose
from kobo_api.kinds import KIND_CONNECTED
from kobo_api.main import create_app
from kobo_api.models import Agent, AgentStatus
from tests.fakes import FakeAgent, FakeCloud, FakeGitHub, FakeGojira, FakeKubectl

AGENT_NAME = "clio"
REPO = "acme/agent-clio"
GROUP = "acme-acl"
DOC = "bundle/knowledge/colleagues.md"


def _composed_tree(tmp_path: Path) -> dict[str, str]:
    """The agent's repository as the room composed it, default mode."""
    spec = BundleSpec(
        name=AGENT_NAME,
        persona_body="You are clio.\n",
        model_id="vertex_ai/claude-sonnet-4-5",
        skills=[],
        runtime_image="registry.example/agents/daedalus-runtime:v1",
        vertex_project="p",
        vertex_location="us-east5",
        cabal_url="https://cabal.example",
        broker_url="http://kobo.svc",
    )
    dest = tmp_path / "composed"
    compose(spec, dest)
    return {
        p.relative_to(dest).as_posix(): p.read_text() for p in dest.rglob("*") if p.is_file()
    }


class _CapturingGitHub(FakeGitHub):
    """Snapshots the workdir at `commit_push`, the only moment the cloned
    tree still exists."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.pushed: dict[str, str] = {}
        self.messages: list[str] = []

    def commit_push(self, workdir: Path, message: str) -> bool:
        self.pushed = {
            p.relative_to(workdir).as_posix(): p.read_text()
            for p in workdir.rglob("*")
            if p.is_file()
        }
        self.messages.append(message)
        return super().commit_push(workdir, message)


def _client(session_factory, config, github):
    return TestClient(
        create_app(
            session_factory,
            config=config,
            clients=Clients(
                github=github,
                gojira=FakeGojira(),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=FakeKubectl(),
            ),
        )
    )


def _seed(session_factory, *, kind=None):
    session = session_factory()
    for name in (AGENT_NAME, "ada"):
        agent = Agent(
            name=name,
            persona="helpful-generalist",
            model_id="vertex_ai/claude-sonnet-4-5",
            status=AgentStatus.READY,
            repo_full_name=f"acme/agent-{name}",
            group_name=GROUP,
            skills=[],
        )
        if kind is not None and name == AGENT_NAME:
            agent.kind = kind
        session.add(agent)
    session.commit()
    session.close()


def _mode_on_row(session_factory):
    session = session_factory()
    try:
        return session.query(Agent).filter_by(name=AGENT_NAME).one().colleague_mode
    finally:
        session.close()


def _put(client, mode):
    return client.put(f"/api/agents/{AGENT_NAME}/colleague-mode", json={"mode": mode})


def test_roster_only_rewrites_the_bundle_and_records_the_row(session_factory, config, tmp_path):
    github = _CapturingGitHub(clone_tree=_composed_tree(tmp_path))
    client = _client(session_factory, config, github)
    _seed(session_factory)

    response = _put(client, "roster-only")

    assert response.status_code == 200
    assert response.json()["colleague_mode"] == "roster-only"
    assert response.json()["previous_colleague_mode"] == "default"
    allow = yaml.safe_load(github.pushed["bundle/agent.yaml"])["tools"]["allow"]
    assert "colleagues.ask_colleague" not in allow and "github.open_pr" in allow
    assert "- ada" in github.pushed[DOC]
    assert "ask_colleague FIRST" not in github.pushed[DOC]
    assert github.messages == ["kobo: colleague mode is now roster-only"]
    assert _mode_on_row(session_factory) == "roster-only"
    detail = client.get(f"/api/agents/{AGENT_NAME}").json()
    assert detail["colleague_mode"] == "roster-only"


def test_none_removes_the_colleague_document(session_factory, config, tmp_path):
    github = _CapturingGitHub(clone_tree=_composed_tree(tmp_path))
    client = _client(session_factory, config, github)
    _seed(session_factory)

    assert _put(client, "none").status_code == 200

    assert DOC not in github.pushed
    allow = yaml.safe_load(github.pushed["bundle/agent.yaml"])["tools"]["allow"]
    assert not [t for t in allow if t.startswith("colleagues.")]
    assert _mode_on_row(session_factory) == "none"


def test_an_unknown_mode_is_422_and_pushes_nothing(session_factory, config, tmp_path):
    github = _CapturingGitHub(clone_tree=_composed_tree(tmp_path))
    client = _client(session_factory, config, github)
    _seed(session_factory)

    assert _put(client, "sometimes").status_code == 422
    assert github.messages == []
    assert _mode_on_row(session_factory) is None


def test_a_failed_push_leaves_the_row_where_it_was(session_factory, config):
    client = _client(session_factory, config, _CapturingGitHub(clone_error="boom"))
    _seed(session_factory)

    assert _put(client, "none").status_code == 502
    assert _mode_on_row(session_factory) is None


def test_the_row_is_written_only_after_the_push(session_factory, config, tmp_path):
    """Issue #133: no UPDATE on the agents row may reach the database before
    the push, or Postgres holds the row lock across the clone and push."""
    agent_updates: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("UPDATE AGENTS"):
            agent_updates.append(statement)

    engine = session_factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", _record)

    class _CountingGitHub(_CapturingGitHub):
        updates_at_push: int | None = None

        def commit_push(self, workdir: Path, message: str) -> bool:
            self.updates_at_push = len(agent_updates)
            return super().commit_push(workdir, message)

    github = _CountingGitHub(clone_tree=_composed_tree(tmp_path))
    client = _client(session_factory, config, github)
    _seed(session_factory)
    try:
        assert _put(client, "roster-only").status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert github.updates_at_push == 0
    assert agent_updates, "the mode never reached the row"
    assert _mode_on_row(session_factory) == "roster-only"
    # Rendered for the new mode even though the row still said default.
    assert "ask_colleague FIRST" not in github.pushed[DOC]


def test_a_connected_agent_is_refused(session_factory, config, tmp_path):
    """The room never pushes to a team's own repository."""
    github = _CapturingGitHub(clone_tree=_composed_tree(tmp_path))
    client = _client(session_factory, config, github)
    _seed(session_factory, kind=KIND_CONNECTED)

    assert _put(client, "none").status_code == 409
    assert github.messages == []
