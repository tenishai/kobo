"""Every fast-lane commit carries the directory resync (issue #149).

PR #148 (issue #147) wired `on_repin=_directory_resync(...)` into each
fast-lane call site in `main.py`: when a commit moves the clone's `FROM`
line to the recorded runtime, the repo card now names a new runtime, and the
switchboard's copy must follow or it goes stale. The per-writer unit tests
prove each writer calls the hook it is given; nothing proved that `main.py`
passes one in at every site, so deleting the kwarg at, say, the Slack route
brought back the round-1 defect with a green suite.

This table is the proof. The clone `FakeGitHub` materializes carries an
older image (`FAKE_DOCKERFILE_OLD_IMAGE`), so every commit below re-pins
`FROM`, and with the switchboard enabled each route must call cabal's
graduate exactly once. Remove `on_repin=` at any one site and its row
fails. The table is written by hand, like the connected-guard table: a new
fast-lane route is added here by someone who thought about it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.clients import Clients
from kobo_api.config import Config
from kobo_api.main import create_app
from kobo_api.models import Agent, AgentLearning, AgentStatus, Base
from kobo_api.slack_state import encode_channels
from tests.fakes import (
    FakeAgent,
    FakeCabal,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeImage,
    FakeKubectl,
    FakeSlack,
)

AGENT_NAME = "clio"
REPO = "example-org/agent-clio"
SOURCE_REPO = "example-org/tools-source"
LEARNING_ID = 1
# A tool file must define pre_task or post_draft to be accepted.
VALID_PY = "def post_draft(draft):\n    return draft\n"

# (route, body): the synchronous intake upload, teach (its archive is a
# background task, which TestClient runs before returning), the tool upload
# and import (same shape), Slack wiring, and the learning-approve archive.
FAST_LANE_ROUTES = [
    (f"/api/agents/{AGENT_NAME}/intake", {"filename": "note.md", "content": "hello"}),
    (f"/api/agents/{AGENT_NAME}/teach", {"text": "remember this"}),
    (f"/api/agents/{AGENT_NAME}/tooling", {"filename": "helper.py", "content": VALID_PY}),
    (f"/api/agents/{AGENT_NAME}/tooling/import", {"repo": SOURCE_REPO, "path": "tools"}),
    (
        f"/api/agents/{AGENT_NAME}/slack/connect",
        {"app_id": "A01234567", "bot_token": "xoxb-placeholder", "app_token": "xapp-placeholder"},
    ),
    (f"/api/agents/{AGENT_NAME}/learnings/{LEARNING_ID}/approve", {}),
]


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config(tmp_path, fake_storage):
    # The switchboard's two org keys; the operator token is the third leg.
    return Config(
        workdir=tmp_path / "work",
        skills_bucket="skills-bucket",
        personas_bucket="personas-bucket",
        cabal_url="http://cabal.internal",
        agent_endpoint_template="http://agent-{name}.default.svc.cluster.local",
    )


def _onboarded_gojira() -> tuple[FakeGojira, int]:
    """One already-onboarded app, as a READY agent has; Slack wiring asks
    Gojira to reprovision it after the commit (`activate_after=1` is the
    cadence `wait_active` expects)."""
    gojira = FakeGojira(activate_after=1)
    app_id = gojira.create_app(
        name=AGENT_NAME, repo_full_name=REPO, cluster="example-cluster", namespace=AGENT_NAME
    )
    return gojira, app_id


def _seed(session_factory, *, gojira_app_id: int) -> None:
    session = session_factory()
    session.add(
        Agent(
            name=AGENT_NAME,
            persona="helpful-generalist",
            model_id="vertex_ai/claude-sonnet-4-5",
            skills=[],
            status=AgentStatus.READY,
            repo_full_name=REPO,
            gojira_app_id=gojira_app_id,
            slack_app_id="A01234567",
            # Slack wiring refuses until a channel is saved.
            slack_channels=encode_channels(["C01234567"]),
        )
    )
    session.add(
        AgentLearning(
            id=LEARNING_ID,
            agent_name=AGENT_NAME,
            day="2026-09-21",
            content="the billing export runs at 02:00",
            kind="fact",
            source="Slack, Sep 21",
            # Attributed, so approve needs no second confirmation.
            said_by="Ada",
            evidence="quote",
            content_hash="a" * 64,
        )
    )
    session.commit()
    session.close()


@pytest.mark.parametrize(
    ("path", "body"), FAST_LANE_ROUTES, ids=[path for path, _ in FAST_LANE_ROUTES]
)
def test_every_fast_lane_commit_resyncs_the_directory_once(
    session_factory, config, fake_storage, monkeypatch, path, body
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    github = FakeGitHub(
        existing_repos={REPO},
        tooling_source_files={SOURCE_REPO: {"tools/good.py": VALID_PY}},
        # The clone carries a bundle/agent.yaml: Slack wiring edits its transports.
        agent_yaml_transports=[{"kind": "a2a", "host": "0.0.0.0", "port": 8080}],
    )
    cabal = FakeCabal()
    gojira, gojira_app_id = _onboarded_gojira()
    client = TestClient(
        create_app(
            session_factory,
            config=config,
            clients=Clients(
                github=github,
                gojira=gojira,
                cloud=FakeCloud(),
                agent=FakeAgent(reply="sure"),
                kubectl=FakeKubectl(),
                storage=fake_storage,
                slack=FakeSlack(),
                cabal=cabal,
                image=FakeImage(),
            ),
        )
    )
    _seed(session_factory, gojira_app_id=gojira_app_id)

    response = client.post(path, json=body)

    assert response.status_code == 200, response.text
    assert github.pushes, "the route committed nothing, so the table row is wrong"
    assert [call["name"] for call in cabal.graduate_calls] == [AGENT_NAME], (
        "the commit moved FROM, so the directory must be resynced exactly once"
    )
