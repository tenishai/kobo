"""Tests for the public agent routes: create/list/detail/retry — the
one-click pipeline (`cortex_api.provisioner.PIPELINE`) exercised end-to-end
through the fake platform clients (`tests/fakes.py`), the same way a real
deploy would run it, just with every backend faked.

Same no-real-sleep discipline as test_steps.py/test_steps_wire.py: the
pipeline's own poll loops (OnboardStep/WireStep's gojira poll, BuildStep's CI
poll) must never actually block, so `cortex_api.steps._sleep` is
monkeypatched to a no-op here too (this file doesn't share test_steps.py's
module, so the autouse fixture is replicated rather than imported).

The deadline-poll helper below (`_poll_until`) DOES sleep for real, briefly,
between HTTP polls: that's the test's own wall-clock wait for the pipeline's
background thread to make progress, a different clock than the one the
`_sleep` patch silences.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cortex_api.clients import Clients
from cortex_api.config import Config
from cortex_api.main import create_app
from cortex_api.models import Base
from tests.fakes import FakeAgent, FakeCloud, FakeGitHub, FakeGojira, FakeKubectl

AGENT_NAME = "clio"
PERSONA = "helpful-generalist"

ALL_STEP_NAMES = ["repo", "onboard", "access", "wire", "build", "deploy", "hello"]

POLL_DEADLINE_S = 5
POLL_INTERVAL_S = 0.02


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """The assembled pipeline's steps poll gojira/CI; never let those loops
    sleep for real (see module docstring re: the deadline-poll's own sleep)."""
    import cortex_api.steps as steps_module

    monkeypatch.setattr(steps_module, "_sleep", lambda seconds: None)


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/cortex.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config(tmp_path):
    return Config(workdir=tmp_path / "work")


@pytest.fixture
def fake_github():
    return FakeGitHub()


@pytest.fixture
def fake_gojira():
    return FakeGojira(activate_after=1)


@pytest.fixture
def fake_agent():
    return FakeAgent(reply="hello yourself, human")


@pytest.fixture
def clients(fake_github, fake_gojira, fake_agent):
    return Clients(
        github=fake_github,
        gojira=fake_gojira,
        cloud=FakeCloud(),
        agent=fake_agent,
        kubectl=FakeKubectl(),
    )


@pytest.fixture
def client(session_factory, config, clients):
    return TestClient(create_app(session_factory, config=config, clients=clients))


def _poll_until(client, name, statuses, deadline_s=POLL_DEADLINE_S):
    """Poll GET /api/agents/{name} until its status lands in `statuses`;
    fail with a clear message if `deadline_s` real (wall-clock) seconds pass
    before that happens — the pipeline itself never really sleeps (see
    `_no_real_sleep` above), so on a healthy run this returns almost
    immediately.
    """
    deadline = time.monotonic() + deadline_s
    body = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/agents/{name}")
        assert response.status_code == 200
        body = response.json()
        if body["status"] in statuses:
            return body
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"agent {name!r} did not reach {statuses} within {deadline_s}s: {body}")


# --- POST /api/agents -------------------------------------------------------------


def test_create_agent_returns_201_pending_then_provisions_to_ready(client):
    response = client.post(
        "/api/agents",
        json={"name": AGENT_NAME, "persona": PERSONA, "skills": [], "model": None},
    )

    assert response.status_code == 201
    assert response.json() == {"name": AGENT_NAME, "status": "pending"}

    body = _poll_until(client, AGENT_NAME, {"ready", "failed"})
    assert body["status"] == "ready"
    assert [s["name"] for s in body["steps"]] == ALL_STEP_NAMES
    assert all(s["status"] == "done" for s in body["steps"])
    assert all(s["error"] is None for s in body["steps"])
    assert body["first_reply"] == "hello yourself, human"


def test_create_agent_step_labels_match_ui_checklist_vocabulary(client):
    client.post("/api/agents", json={"name": AGENT_NAME, "persona": PERSONA})

    body = _poll_until(client, AGENT_NAME, {"ready", "failed"})

    labels = {s["name"]: s["label"] for s in body["steps"]}
    assert labels == {
        "repo": "Repository",
        "onboard": "Platform onboarding",
        "access": "Model access",
        "wire": "Deploy wiring",
        "build": "Image build",
        "deploy": "Deploy",
        "hello": "First contact",
    }


def test_create_agent_no_model_falls_back_to_default(client, config):
    client.post("/api/agents", json={"name": AGENT_NAME, "persona": PERSONA})

    body = _poll_until(client, AGENT_NAME, {"ready", "failed"})
    assert body["model"] == config.models[0].id


def test_create_agent_duplicate_name_returns_409(client):
    payload = {"name": AGENT_NAME, "persona": PERSONA}

    first = client.post("/api/agents", json=payload)
    assert first.status_code == 201

    second = client.post("/api/agents", json=payload)
    assert second.status_code == 409
    assert "error" in second.json()


def test_create_agent_unknown_persona_returns_422(client):
    response = client.post(
        "/api/agents", json={"name": AGENT_NAME, "persona": "ghost-persona"}
    )

    assert response.status_code == 422
    assert "error" in response.json()


def test_create_agent_unknown_skill_returns_422(client):
    response = client.post(
        "/api/agents",
        json={"name": AGENT_NAME, "persona": PERSONA, "skills": ["ghost-skill"]},
    )

    assert response.status_code == 422
    assert "error" in response.json()


def test_create_agent_unknown_model_returns_422(client):
    response = client.post(
        "/api/agents",
        json={"name": AGENT_NAME, "persona": PERSONA, "model": "not-a-real-model"},
    )

    assert response.status_code == 422
    assert "error" in response.json()


def test_create_agent_invalid_name_returns_422(client):
    response = client.post(
        "/api/agents", json={"name": "Not Valid!", "persona": PERSONA}
    )

    assert response.status_code == 422
    assert "error" in response.json()


def test_create_agent_invalid_name_never_reaches_the_pipeline(client, fake_github):
    """A 422 must reject before touching any platform client."""
    client.post("/api/agents", json={"name": "Not Valid!", "persona": PERSONA})

    assert fake_github.calls == []


# --- GET /api/agents ---------------------------------------------------------------


def test_list_agents_returns_name_status_model_created_at(client, config):
    client.post("/api/agents", json={"name": AGENT_NAME, "persona": PERSONA, "model": None})

    response = client.get("/api/agents")

    assert response.status_code == 200
    agents = response.json()["agents"]
    assert len(agents) == 1
    row = agents[0]
    assert row["name"] == AGENT_NAME
    assert row["status"] in {"pending", "provisioning", "ready"}
    assert row["model"] == config.models[0].id
    assert isinstance(row["created_at"], str) and row["created_at"]


def test_list_agents_empty_when_none_created(client):
    response = client.get("/api/agents")

    assert response.status_code == 200
    assert response.json() == {"agents": []}


# --- GET /api/agents/{name} --------------------------------------------------------


def test_get_agent_unknown_name_returns_404(client):
    response = client.get("/api/agents/does-not-exist")

    assert response.status_code == 404
    assert "error" in response.json()


# --- Failure + retry ---------------------------------------------------------------


def _failing_clients(fake_github, fake_agent, failing_gojira):
    return Clients(
        github=fake_github,
        gojira=failing_gojira,
        cloud=FakeCloud(),
        agent=fake_agent,
        kubectl=FakeKubectl(),
    )


def test_create_agent_failure_surfaces_failing_step_error_and_aborts_the_rest(
    session_factory, config, fake_github, fake_agent
):
    failing_gojira = FakeGojira(
        activate_after=1,
        end_status="failed",
        failed_steps=[{"name": "provision", "status": "failed", "error": "boom"}],
    )
    client = TestClient(
        create_app(
            session_factory,
            config=config,
            clients=_failing_clients(fake_github, fake_agent, failing_gojira),
        )
    )

    response = client.post("/api/agents", json={"name": AGENT_NAME, "persona": PERSONA})
    assert response.status_code == 201

    body = _poll_until(client, AGENT_NAME, {"ready", "failed"})
    assert body["status"] == "failed"
    assert body["error"]

    steps_by_name = {s["name"]: s for s in body["steps"]}
    assert steps_by_name["repo"]["status"] == "done"
    assert steps_by_name["onboard"]["status"] == "failed"
    assert "provision" in steps_by_name["onboard"]["error"]
    # everything after the failing step never ran, so it's marked aborted
    # rather than done
    assert steps_by_name["access"]["status"] == "failed"
    assert "aborted" in steps_by_name["access"]["error"]
    # HelloStep never ran, so no reply to show yet
    assert body["first_reply"] is None


def test_retry_when_not_failed_returns_409(client):
    client.post("/api/agents", json={"name": AGENT_NAME, "persona": PERSONA})
    _poll_until(client, AGENT_NAME, {"ready", "failed"})

    response = client.post(f"/api/agents/{AGENT_NAME}/retry")

    assert response.status_code == 409
    assert "error" in response.json()


def test_retry_unknown_agent_returns_404(client):
    response = client.post("/api/agents/does-not-exist/retry")

    assert response.status_code == 404
    assert "error" in response.json()


def test_retry_after_fixing_failure_reaches_ready_without_rerunning_done_steps(
    session_factory, config, fake_github, fake_agent
):
    failing_gojira = FakeGojira(
        activate_after=1,
        end_status="failed",
        failed_steps=[{"name": "provision", "status": "failed", "error": "boom"}],
    )
    client = TestClient(
        create_app(
            session_factory,
            config=config,
            clients=_failing_clients(fake_github, fake_agent, failing_gojira),
        )
    )

    client.post("/api/agents", json={"name": AGENT_NAME, "persona": PERSONA})
    first_body = _poll_until(client, AGENT_NAME, {"ready", "failed"})
    assert first_body["status"] == "failed"

    # RepoStep is the only step that actually ran (and completed) before
    # onboard failed — the counting fake proof that retry doesn't re-run it.
    create_calls_before_retry = [
        c for c in fake_github.calls if c.startswith("create_repo_from_dir")
    ]
    assert len(create_calls_before_retry) == 1

    # "Fix" gojira: the retry's OnboardStep call (a fresh create_app, since
    # this fake doesn't simulate a 409 conflict) now activates cleanly.
    failing_gojira._end_status = "active"

    retry_response = client.post(f"/api/agents/{AGENT_NAME}/retry")
    assert retry_response.status_code == 202
    assert retry_response.json()["status"] == "provisioning"

    second_body = _poll_until(client, AGENT_NAME, {"ready", "failed"})
    assert second_body["status"] == "ready"
    assert all(s["status"] == "done" for s in second_body["steps"])
    assert second_body["first_reply"] == fake_agent._reply

    # Still only the one create_repo_from_dir call across both runs: RepoStep's
    # idempotent-resume path (repo_exists + matching recorded repo_full_name)
    # took over instead of recomposing/recreating.
    create_calls_after_retry = [
        c for c in fake_github.calls if c.startswith("create_repo_from_dir")
    ]
    assert len(create_calls_after_retry) == 1
