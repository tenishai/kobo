"""Tests for the two connect routes: the scan a team runs before it commits
to anything, and the connect that actually takes its repository on.

Both run the same scan server-side. A report the browser is holding is a
claim about a repository some minutes ago, and the route has to answer for
the repository now, so the connect route never trusts the form's copy of it.

The group gate is the create route's own, factored into one helper both
routes call rather than copied, so the consent branch of it stays pinned
where it already was (`test_web_visibility.py`'s create-route cases); what
these tests pin is that the connect route asks the same question at all.

Same no-real-sleep discipline as test_api_agents.py: the pipeline's poll
loops must never block, and `_poll_until` is the only place that sleeps for
real, waiting on the pipeline's daemon thread.
"""

from __future__ import annotations

import logging
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.clients import Clients
from kobo_api.config import Config, GroupChoice
from kobo_api.connect_scan import (
    ADDS,
    NO_DOCKERFILE,
    REPO_NOT_IN_ORG,
    REPO_NOT_VISIBLE,
)
from kobo_api.kinds import KIND_CONNECTED, RUN_MODE_HEADLESS, RUN_MODE_HTTP
from kobo_api.main import create_app
from kobo_api.models import Agent, AgentStatus, Base
from tests.fakes import (
    FakeAgent,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeImage,
    FakeKubectl,
)

AGENT_NAME = "clio"
ORG = "example-org"
REPO = f"{ORG}/team-chatbot"

CONNECTED_STEP_NAMES = [
    "connect", "avatar", "onboard", "access", "wire", "build", "deploy",
]

POLL_DEADLINE_S = 5
POLL_INTERVAL_S = 0.02


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    import kobo_api.steps as steps_module

    monkeypatch.setattr(steps_module, "_sleep", lambda seconds: None)


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config(tmp_path):
    return Config(
        workdir=tmp_path / "work",
        skills_bucket="skills-bucket",
        personas_bucket="personas-bucket",
        github_org=ORG,
    )


@pytest.fixture
def github():
    return FakeGitHub(existing_repos={REPO})


@pytest.fixture
def clients(github, fake_storage):
    return Clients(
        github=github,
        gojira=FakeGojira(activate_after=1),
        cloud=FakeCloud(),
        agent=FakeAgent(reply="hello yourself, human"),
        kubectl=FakeKubectl(),
        storage=fake_storage,
        image=FakeImage(),
    )


@pytest.fixture
def client(session_factory, config, clients):
    return TestClient(create_app(session_factory, config=config, clients=clients))


@pytest.fixture
def client_for(session_factory, config, fake_storage):
    """A client over one particular repository, for the cases that are about
    what the scan finds in it rather than about the route."""

    def _build(github: FakeGitHub) -> TestClient:
        clients = Clients(
            github=github,
            gojira=FakeGojira(activate_after=1),
            cloud=FakeCloud(),
            agent=FakeAgent(reply="hello yourself, human"),
            kubectl=FakeKubectl(),
            storage=fake_storage,
            image=FakeImage(),
        )
        return TestClient(create_app(session_factory, config=config, clients=clients))

    return _build


def _payload(**overrides) -> dict:
    payload = {"repo": REPO, "name": AGENT_NAME, "run_mode": RUN_MODE_HTTP}
    payload.update(overrides)
    return payload


def _row(session_factory, name=AGENT_NAME) -> Agent | None:
    session = session_factory()
    try:
        return session.query(Agent).filter_by(name=name).first()
    finally:
        session.close()


def _poll_until(client, name, statuses, deadline_s=POLL_DEADLINE_S):
    deadline = time.monotonic() + deadline_s
    body = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/agents/{name}")
        assert response.status_code == 200
        body = response.json()
        if body["status"] in statuses:
            return body
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"agent {name!r} did not reach {statuses}: {body}")


# --- POST /api/connect/scan -------------------------------------------------------


def test_scan_reports_nothing_for_a_repository_the_room_can_work_with(client):
    response = client.post("/api/connect/scan", json=_payload())

    assert response.status_code == 200
    assert response.json() == {"blockers": [], "warnings": [], "adds": ADDS}


def test_scan_reports_blockers_with_their_codes_and_their_sentences(client_for):
    client = client_for(FakeGitHub())

    response = client.post("/api/connect/scan", json=_payload())

    assert response.status_code == 200
    body = response.json()
    assert [blocker["code"] for blocker in body["blockers"]] == [REPO_NOT_VISIBLE]
    assert "could not be reached" in body["blockers"][0]["message"]


def test_scan_reports_warnings_without_blocking(client_for):
    client = client_for(
        FakeGitHub(existing_repos={REPO}, root_entries=["Dockerfile", ".env"])
    )

    body = client.post("/api/connect/scan", json=_payload()).json()

    assert body["blockers"] == []
    assert [warning["code"] for warning in body["warnings"]] == ["env_file_committed"]


def test_scan_refuses_a_name_that_could_never_be_an_agent(client):
    response = client.post("/api/connect/scan", json=_payload(name="Not A Name"))

    assert response.status_code == 422
    assert "invalid name" in response.json()["error"]


def test_scan_refuses_a_run_mode_the_room_does_not_have(client):
    response = client.post("/api/connect/scan", json=_payload(run_mode="magic"))

    assert response.status_code == 422
    assert response.json()["error"] == (
        "pick how this agent runs: it serves HTTP, or it has no HTTP port"
    )


def test_scan_never_writes_anything(client, github, session_factory):
    client.post("/api/connect/scan", json=_payload())

    assert github.cloned == []
    assert github.pushes == []
    assert github.created_from_dir == []
    assert _row(session_factory) is None


# --- POST /api/agents/connect -----------------------------------------------------


def test_connect_returns_201_pending_then_provisions_to_ready(
    client, github, session_factory
):
    response = client.post("/api/agents/connect", json=_payload())

    assert response.status_code == 201, response.text
    assert response.json() == {"name": AGENT_NAME, "status": "pending"}

    body = _poll_until(client, AGENT_NAME, {"ready", "failed"})
    assert body["status"] == "ready", body
    assert [step["name"] for step in body["steps"]] == CONNECTED_STEP_NAMES
    assert body["kind"] == KIND_CONNECTED


def test_connect_never_creates_or_writes_to_the_repository(client, github):
    client.post("/api/agents/connect", json=_payload())
    _poll_until(client, AGENT_NAME, {"ready", "failed"})

    forbidden = ("create_repo_from_dir:", "commit_push:", "rename_repo:", "archive_repo:")
    assert [call for call in github.calls if call.startswith(forbidden)] == []


def test_connect_records_the_row_a_connected_agent_needs(client, session_factory, config):
    client.post(
        "/api/agents/connect",
        json=_payload(secret_names=["chat-tokens", "billing-api"]),
    )

    agent = _row(session_factory)
    assert agent.kind == KIND_CONNECTED
    assert agent.repo_full_name == REPO
    assert agent.run_mode == RUN_MODE_HTTP
    assert agent.secret_names == ["chat-tokens", "billing-api"]
    assert agent.skills == []
    assert agent.team == "shared"
    # NOT NULL, and a connected agent has none: one fixed value, never a
    # real persona id, so nothing ever tries to resolve it.
    assert agent.persona == "connected"
    assert agent.model_id == config.models[0].id
    assert agent.status in {AgentStatus.PENDING, AgentStatus.PROVISIONING, AgentStatus.READY}


def test_connect_says_who_connected_what(client, caplog):
    """One line per connect, at WARNING because INFO does not reach the pod
    logs here. Arbitrary code with model access is the risk this whole
    feature carries, and the record of who took it on is part of the
    answer."""
    with caplog.at_level(logging.WARNING, logger="kobo_api.main"):
        client.post("/api/agents/connect", json=_payload())

    lines = [record.getMessage() for record in caplog.records if "connect" in record.getMessage()]
    assert any(REPO in line and AGENT_NAME in line for line in lines), lines


# --- refusals ---------------------------------------------------------------------


def test_connect_refuses_an_invalid_name(client, session_factory):
    response = client.post("/api/agents/connect", json=_payload(name="Not A Name"))

    assert response.status_code == 422
    assert response.json() == {"error": "invalid name (invalid name): 'Not A Name'"}
    assert _row(session_factory, "Not A Name") is None


def test_connect_refuses_a_run_mode_the_room_does_not_have(client):
    response = client.post("/api/agents/connect", json=_payload(run_mode="magic"))

    assert response.status_code == 422


@pytest.mark.parametrize(
    "secret_name",
    ["Chat-Tokens", "chat_tokens", "-chat", "chat-", "chat tokens", "", "a" * 64],
)
def test_connect_refuses_a_secret_name_kubernetes_would_not_take(
    client, session_factory, secret_name
):
    response = client.post(
        "/api/agents/connect", json=_payload(secret_names=[secret_name])
    )

    assert response.status_code == 422, response.text
    assert "secret name" in response.json()["error"]
    assert _row(session_factory) is None


def test_connect_refuses_more_secret_names_than_a_pod_should_carry(client):
    response = client.post(
        "/api/agents/connect",
        json=_payload(secret_names=[f"secret-{index}" for index in range(9)]),
    )

    assert response.status_code == 422
    assert response.json()["error"] == "this room takes at most 8 secret names"


def test_connect_keeps_one_of_each_secret_name_in_the_order_they_were_given(
    client, session_factory
):
    client.post(
        "/api/agents/connect",
        json=_payload(secret_names=["billing-api", "chat-tokens", "billing-api"]),
    )

    assert _row(session_factory).secret_names == ["billing-api", "chat-tokens"]


def test_connect_refuses_with_the_blockers_and_their_sentences(
    client_for, session_factory
):
    """The refusal carries what the form would have shown, so the UI never
    has to scan a second time to say why."""
    client = client_for(
        FakeGitHub(existing_repos={REPO}, root_entries=["README.md"])
    )

    response = client.post("/api/agents/connect", json=_payload())

    assert response.status_code == 422
    body = response.json()
    assert [blocker["code"] for blocker in body["blockers"]] == [NO_DOCKERFILE]
    assert body["blockers"][0]["message"].startswith("There is no Dockerfile")
    assert body["error"]
    assert _row(session_factory) is None


def test_connect_refuses_a_repository_in_another_organisation(client, session_factory):
    response = client.post(
        "/api/agents/connect", json=_payload(repo="elsewhere/team-chatbot")
    )

    assert response.status_code == 422
    assert [b["code"] for b in response.json()["blockers"]] == [REPO_NOT_IN_ORG]
    assert _row(session_factory) is None


def test_connect_refuses_headless_until_the_room_has_a_class_for_it(
    client, session_factory
):
    response = client.post("/api/agents/connect", json=_payload(run_mode=RUN_MODE_HEADLESS))

    assert response.status_code == 422
    assert [b["code"] for b in response.json()["blockers"]] == ["headless_unavailable"]
    assert response.json()["blockers"][0]["message"] == (
        "agents with no HTTP port are not available in this room yet"
    )
    assert _row(session_factory) is None


def test_connect_takes_headless_once_the_deploy_platform_names_a_class(
    session_factory, clients, tmp_path, fake_storage
):
    config = Config(
        workdir=tmp_path / "work",
        skills_bucket="skills-bucket",
        personas_bucket="personas-bucket",
        github_org=ORG,
        connected_headless_class="agent-headless",
    )
    client = TestClient(create_app(session_factory, config=config, clients=clients))

    response = client.post("/api/agents/connect", json=_payload(run_mode=RUN_MODE_HEADLESS))

    assert response.status_code == 201, response.text
    assert _row(session_factory).run_mode == RUN_MODE_HEADLESS


def test_connect_refuses_a_name_already_in_the_room(client, session_factory):
    session = session_factory()
    session.add(
        Agent(name=AGENT_NAME, persona="helpful-generalist", model_id="m", skills=[])
    )
    session.commit()
    session.close()

    response = client.post("/api/agents/connect", json=_payload())

    assert response.status_code == 409
    assert response.json() == {"error": f"name taken: {AGENT_NAME!r}"}


# --- the group gate ---------------------------------------------------------------


@pytest.fixture
def grouped_client(session_factory, clients, tmp_path, fake_storage):
    config = Config(
        workdir=tmp_path / "work",
        skills_bucket="skills-bucket",
        personas_bucket="personas-bucket",
        github_org=ORG,
        groups=[GroupChoice(name="payments-eng", okta_group="okta-payments")],
    )
    return TestClient(create_app(session_factory, config=config, clients=clients))


def test_connect_asks_for_a_group_once_the_room_has_named_some(
    grouped_client, session_factory
):
    response = grouped_client.post("/api/agents/connect", json=_payload())

    assert response.status_code == 422
    assert response.json() == {"error": "pick a group for this agent"}
    assert _row(session_factory) is None


def test_connect_refuses_a_group_this_room_has_never_heard_of(grouped_client):
    response = grouped_client.post(
        "/api/agents/connect", json=_payload(group_name="not-a-group")
    )

    assert response.status_code == 422
    assert response.json() == {"error": "unknown group: 'not-a-group'"}


def test_connect_takes_a_group_the_room_has_named(grouped_client, session_factory):
    response = grouped_client.post(
        "/api/agents/connect", json=_payload(group_name="payments-eng")
    )

    assert response.status_code == 201, response.text
    assert _row(session_factory).group_name == "payments-eng"
