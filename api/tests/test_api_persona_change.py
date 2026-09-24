"""PUT /api/agents/{name}/persona: an existing agent takes a different persona.

The persona is what `bundle/persona.md` says, rendered from the catalog
body with the agent's own name in place of `{name}`, exactly as the
pipeline composes it at birth. Changing it is a commit to that one file
(the generated CI rebuilds and rolls the pod) plus the row's `persona`.
Nothing else about the agent moves: not its group, its skills or its
name.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kobo_api.clients import Clients
from kobo_api.kinds import KIND_CONNECTED
from kobo_api.main import create_app
from kobo_api.models import Agent, AgentStatus
from kobo_api.personas import set_persona_archived
from tests.fakes import (
    FAKE_LOCAL_HEAD_DEFAULT,
    FakeAgent,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
)

AGENT_NAME = "clio"
REPO = "acme/agent-clio"
OLD_PERSONA = "helpful-generalist"
NEW_PERSONA = "marketing-strategist"


class _CapturingGitHub(FakeGitHub):
    """Captures `bundle/persona.md` the moment `commit_push` is called, the
    only window in which the caller's cloned workdir still exists."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.captured_persona: str | None = None

    def commit_push(self, workdir: Path, message: str) -> bool:
        target = workdir / "bundle" / "persona.md"
        if target.is_file():
            self.captured_persona = target.read_text()
        return super().commit_push(workdir, message)


def _client(session_factory, config, fake_storage, *, github=None):
    return TestClient(
        create_app(
            session_factory,
            config=config,
            clients=Clients(
                github=github if github is not None else _CapturingGitHub(),
                gojira=FakeGojira(),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=FakeKubectl(),
                storage=fake_storage,
            ),
        )
    )


def _seed_agent(session_factory, *, status=AgentStatus.READY, kind=None):
    session = session_factory()
    agent = Agent(
        name=AGENT_NAME,
        persona=OLD_PERSONA,
        model_id="vertex_ai/claude-sonnet-4-5",
        status=status,
        repo_full_name=REPO,
    )
    if kind is not None:
        agent.kind = kind
    session.add(agent)
    session.commit()
    session.close()


def _persona_on_row(session_factory) -> str:
    session = session_factory()
    try:
        return session.query(Agent).filter_by(name=AGENT_NAME).one().persona
    finally:
        session.close()


def test_set_persona_rewrites_the_bundle_persona_and_records_it(
    session_factory, config, fake_storage
):
    github = _CapturingGitHub()
    client = _client(session_factory, config, fake_storage, github=github)
    _seed_agent(session_factory)

    response = client.put(f"/api/agents/{AGENT_NAME}/persona", json={"persona": NEW_PERSONA})

    assert response.status_code == 200
    assert response.json() == {"persona": NEW_PERSONA, "sha": FAKE_LOCAL_HEAD_DEFAULT}
    body = github.captured_persona
    assert body is not None
    # Rendered the way the pipeline renders it at birth: the agent's own
    # name in place of the placeholder, the catalog header line gone.
    assert body.startswith(f"You are {AGENT_NAME}, a marketing strategist")
    assert "{name}" not in body
    assert "<!-- description:" not in body
    assert github.pushes[-1][1] == f"kobo: persona is now {NEW_PERSONA}"
    assert _persona_on_row(session_factory) == NEW_PERSONA


def test_set_persona_that_is_already_the_agents_is_a_quiet_success(
    session_factory, config, fake_storage
):
    """Nothing to commit is still the truthful answer with the current head:
    the bundle already says this, so the live image already matches."""
    github = _CapturingGitHub(commit_push_results=[False])
    client = _client(session_factory, config, fake_storage, github=github)
    _seed_agent(session_factory)

    response = client.put(f"/api/agents/{AGENT_NAME}/persona", json={"persona": OLD_PERSONA})

    assert response.status_code == 200
    assert response.json()["persona"] == OLD_PERSONA
    assert _persona_on_row(session_factory) == OLD_PERSONA


def test_set_persona_unknown_persona_is_404_and_pushes_nothing(
    session_factory, config, fake_storage
):
    github = _CapturingGitHub()
    client = _client(session_factory, config, fake_storage, github=github)
    _seed_agent(session_factory)

    response = client.put(f"/api/agents/{AGENT_NAME}/persona", json={"persona": "nobody"})

    assert response.status_code == 404
    assert "nobody" in response.json()["error"]
    assert github.pushes == []
    assert _persona_on_row(session_factory) == OLD_PERSONA


def test_set_persona_archived_persona_is_404(session_factory, config, fake_storage):
    set_persona_archived(
        fake_storage, bucket=config.personas_bucket, persona_id=NEW_PERSONA, archived=True
    )
    github = _CapturingGitHub()
    client = _client(session_factory, config, fake_storage, github=github)
    _seed_agent(session_factory)

    response = client.put(f"/api/agents/{AGENT_NAME}/persona", json={"persona": NEW_PERSONA})

    assert response.status_code == 404
    assert github.pushes == []


def test_set_persona_refuses_a_connected_agent(session_factory, config, fake_storage):
    """The room never pushes to a team's own repository, and a connected
    agent's persona is not the room's to write."""
    github = _CapturingGitHub()
    client = _client(session_factory, config, fake_storage, github=github)
    _seed_agent(session_factory, kind=KIND_CONNECTED)

    response = client.put(f"/api/agents/{AGENT_NAME}/persona", json={"persona": NEW_PERSONA})

    assert response.status_code == 409
    assert github.pushes == []


@pytest.mark.parametrize("status", [AgentStatus.PENDING, AgentStatus.PROVISIONING, AgentStatus.FAILED])
def test_set_persona_needs_a_ready_agent(session_factory, config, fake_storage, status):
    client = _client(session_factory, config, fake_storage)
    _seed_agent(session_factory, status=status)

    response = client.put(f"/api/agents/{AGENT_NAME}/persona", json={"persona": NEW_PERSONA})

    assert response.status_code == 409


def test_set_persona_unknown_agent_is_404(session_factory, config, fake_storage):
    client = _client(session_factory, config, fake_storage)
    response = client.put("/api/agents/ghost/persona", json={"persona": NEW_PERSONA})
    assert response.status_code == 404


def test_set_persona_rejects_a_malformed_body(session_factory, config, fake_storage):
    client = _client(session_factory, config, fake_storage)
    _seed_agent(session_factory)
    assert client.put(f"/api/agents/{AGENT_NAME}/persona", json={}).status_code == 422
