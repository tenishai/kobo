"""`PATCH /api/agents/{name}/model`: a deployed agent moves onto another
catalog model. The change is `AGENT_MODEL` and `VERTEXAI_LOCATION` in the
agent's gojira.yaml (the composed agent.yaml reads `${AGENT_MODEL:-...}`),
one commit, a reprovision, and the row's `model_id`."""

from __future__ import annotations

from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from kobo_api.clients import Clients
from kobo_api.config import ModelChoice
from kobo_api.kinds import KIND_CONNECTED
from kobo_api.main import create_app
from kobo_api.models import Agent, AgentStatus
from tests.fakes import FakeAgent, FakeCloud, FakeGitHub, FakeGojira, FakeKubectl

AGENT_NAME = "clio"
REPO = "acme/agent-clio"
OLD_MODEL = "vertex_ai/claude-sonnet-4-5"
NEW_MODEL = "vertex_ai/gemini-3.5-flash"


class _CapturingGitHub(FakeGitHub):
    """Reads gojira.yaml at commit_push time, the only window in which the
    caller's cloned workdir still exists."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.captured_gojira: dict | None = None

    def commit_push(self, workdir: Path, message: str) -> bool:
        target = workdir / "gojira.yaml"
        if target.is_file():
            self.captured_gojira = yaml.safe_load(target.read_text())
        return super().commit_push(workdir, message)


def _client(session_factory, config, *, github=None, gojira=None):
    config.models = [
        ModelChoice(id=OLD_MODEL, label="Claude Affordable (Sonnet)"),
        ModelChoice(id=NEW_MODEL, label="Gemini Affordable (Flash)", location="global"),
    ]
    return TestClient(
        create_app(
            session_factory,
            config=config,
            clients=Clients(
                github=github if github is not None else _CapturingGitHub(),
                gojira=gojira if gojira is not None else FakeGojira(),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=FakeKubectl(),
            ),
        )
    )


def _seed_agent(session_factory, *, status=AgentStatus.READY, kind=None, gojira=None):
    # The fake platform only reprovisions an app it created, the way the
    # real one does; the row carries that app's id.
    app_id = (
        gojira.create_app(name=AGENT_NAME, repo_full_name=REPO, cluster="dev", namespace=AGENT_NAME)
        if gojira is not None
        else None
    )
    with session_factory() as session:
        agent = Agent(
            name=AGENT_NAME,
            persona="helpful-generalist",
            model_id=OLD_MODEL,
            status=status,
            repo_full_name=REPO,
            gojira_app_id=app_id,
        )
        if kind is not None:
            agent.kind = kind
        session.add(agent)
        session.commit()
    return app_id


def _model_on_row(session_factory) -> str:
    with session_factory() as session:
        return session.query(Agent).filter_by(name=AGENT_NAME).one().model_id


def test_set_model_writes_the_two_env_keys_reprovisions_and_records_it(session_factory, config):
    github = _CapturingGitHub()
    gojira = FakeGojira()
    client = _client(session_factory, config, github=github, gojira=gojira)
    app_id = _seed_agent(session_factory, gojira=gojira)

    response = client.patch(f"/api/agents/{AGENT_NAME}/model", json={"model": NEW_MODEL})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["model"] == NEW_MODEL
    assert body["model_label"] == "Gemini Affordable (Flash)"
    assert body["previous_model"] == OLD_MODEL
    env = (github.captured_gojira or {}).get("env") or {}
    assert env["AGENT_MODEL"] == NEW_MODEL
    assert env["VERTEXAI_LOCATION"] == "global"
    assert github.pushes[-1][1] == "kobo: model is now Gemini Affordable (Flash) [skip ci]"
    assert f"reprovision:{app_id}" in gojira.calls
    assert _model_on_row(session_factory) == NEW_MODEL


def test_set_model_not_in_the_catalog_is_refused_and_pushes_nothing(session_factory, config):
    github = _CapturingGitHub()
    client = _client(session_factory, config, github=github)
    _seed_agent(session_factory)

    response = client.patch(
        f"/api/agents/{AGENT_NAME}/model", json={"model": "vertex_ai/claude-opus-9"}
    )

    assert response.status_code == 422
    assert "no model" in response.json()["error"]
    assert github.pushes == []
    assert _model_on_row(session_factory) == OLD_MODEL


def test_set_model_on_a_connected_agent_is_409(session_factory, config):
    client = _client(session_factory, config)
    _seed_agent(session_factory, kind=KIND_CONNECTED)

    response = client.patch(f"/api/agents/{AGENT_NAME}/model", json={"model": NEW_MODEL})

    assert response.status_code == 409


def test_set_model_before_ready_is_409(session_factory, config):
    client = _client(session_factory, config)
    _seed_agent(session_factory, status=AgentStatus.PROVISIONING)

    response = client.patch(f"/api/agents/{AGENT_NAME}/model", json={"model": NEW_MODEL})

    assert response.status_code == 409
    assert response.json() == {"error": "agent is not ready for a model change yet"}
