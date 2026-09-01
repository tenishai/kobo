"""Tests for the Learnings HTTP surface (Task 7, daily-learnings spec):
`GET /api/agents/{name}/learnings`, `POST .../learnings/{id}/approve`,
`POST .../learnings/{id}/deny`, `POST .../learnings/bulk`, plus the two
`pending_learnings`/`can_decide_learnings` fields on `GET /api/agents/
{name}`. Same harness shape as test_web_teach.py (own `session_factory`/
`config` fixtures, a `TestClient` built from `create_app`, a
`_CapturingGitHub` that snapshots `bundle/intake/*` content the moment
`commit_push` fires) -- all against the fakes, no real git/kubectl
involved. Approve rides the teach lane byte-for-byte, so its archive-lane
assertions reuse that same fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.clients import Clients
from kobo_api.config import Config
from kobo_api.main import create_app
from kobo_api.models import (
    LEARNING_APPROVED,
    LEARNING_DENIED,
    LEARNING_PENDING,
    Agent,
    AgentLearning,
    AgentStatus,
    Base,
)
from tests.fakes import (
    FAKE_LOCAL_HEAD_DEFAULT,
    FakeAgent,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
    FakeStorage,
)

AGENT_NAME = "clio"
PERSONA = "helpful-generalist"
REPO = "acme/agent-clio"

SHA = FAKE_LOCAL_HEAD_DEFAULT


class _CapturingGitHub(FakeGitHub):
    """Same snapshot-on-commit_push trick as test_web_intake.py's own
    `_CapturingGitHub`, replicated locally -- captures `bundle/intake/*`
    content the moment `commit_push` is called, the only window in which
    the caller's cloned workdir still exists before `kobo_api.intake`
    discards it."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.captured_intake: dict[str, str] = {}
        self.captured_dockerfile = ""

    def commit_push(self, workdir: Path, message: str) -> bool:
        intake_dir = workdir / "bundle" / "intake"
        if intake_dir.is_dir():
            self.captured_intake = {
                path.name: path.read_text()
                for path in intake_dir.iterdir()
                if path.is_file()
            }
        self.captured_dockerfile = (workdir / "Dockerfile").read_text()
        return super().commit_push(workdir, message)

    @property
    def intake_contents(self) -> list[str]:
        """The brief's own name for what these tests want to scan: the
        plain note bodies captured at commit time, adapted from
        `captured_intake` (a filename -> content dict, same shape
        test_web_teach.py's own `_CapturingGitHub` exposes)."""
        return list(self.captured_intake.values())


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config(tmp_path):
    return Config(workdir=tmp_path / "work")


def _make_client(session_factory, config, *, github=None, iap_jwks_fetcher=None, cabal=None):
    return TestClient(
        create_app(
            session_factory,
            config=config,
            clients=Clients(
                github=github if github is not None else FakeGitHub(),
                gojira=FakeGojira(),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=FakeKubectl(),
                storage=FakeStorage(),
                cabal=cabal,
            ),
            iap_jwks_fetcher=iap_jwks_fetcher,
        )
    )


def _seed_agent(session_factory, config, *, status=AgentStatus.READY, repo_full_name=REPO):
    session = session_factory()
    session.add(
        Agent(
            name=AGENT_NAME,
            persona=PERSONA,
            model_id=config.models[0].id,
            status=status,
            repo_full_name=repo_full_name,
        )
    )
    session.commit()
    session.close()


def _seed_learning(session_factory, agent_name=AGENT_NAME, **overrides):
    session = session_factory()
    try:
        defaults = {
            "agent_name": agent_name,
            "day": "2026-09-01",
            "content": "the canary gate is manual",
            "kind": "fact",
            "source": "Slack, Sep 1",
            "evidence": "quote",
            "content_hash": "a" * 64,
        }
        defaults.update(overrides)
        row = AgentLearning(**defaults)
        session.add(row)
        session.commit()
        return row.id
    finally:
        session.close()


def test_list_learnings_empty(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}/learnings")

    assert response.status_code == 200
    assert response.json() == {"learnings": []}


def test_list_learnings_returns_rows_and_validates_status(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)
    _seed_learning(session_factory)

    listed = client.get(f"/api/agents/{AGENT_NAME}/learnings")
    assert listed.status_code == 200
    body = listed.json()["learnings"]
    assert len(body) == 1
    assert body[0]["content"] == "the canary gate is manual"
    assert body[0]["status"] == LEARNING_PENDING

    bogus = client.get(f"/api/agents/{AGENT_NAME}/learnings?status=nonsense")
    assert bogus.status_code == 422


def test_list_learnings_unknown_agent_404(session_factory, config):
    client = _make_client(session_factory, config)

    assert client.get("/api/agents/ghost/learnings").status_code == 404


def test_deny_flips_status_and_second_deny_conflicts(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)
    learning_id = _seed_learning(session_factory)

    first = client.post(f"/api/agents/{AGENT_NAME}/learnings/{learning_id}/deny")
    assert first.status_code == 200
    assert first.json()["status"] == LEARNING_DENIED

    second = client.post(f"/api/agents/{AGENT_NAME}/learnings/{learning_id}/deny")
    assert second.status_code == 409


def test_approve_rides_teach_lane_archive_fallback(session_factory, config):
    github = _CapturingGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)
    learning_id = _seed_learning(session_factory)

    response = client.post(f"/api/agents/{AGENT_NAME}/learnings/{learning_id}/approve")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == LEARNING_APPROVED
    assert body["mode"] == "archive"
    assert body["filename"].startswith("learning-")
    # The note landed in bundle/intake with the taught content.
    assert any("the canary gate is manual" in c for c in github.intake_contents)


def test_approve_unknown_id_404(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)

    response = client.post(f"/api/agents/{AGENT_NAME}/learnings/999/approve")

    assert response.status_code == 404


def test_bulk_deny(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)
    first = _seed_learning(session_factory, content_hash="a" * 64)
    second = _seed_learning(session_factory, content="another fact", content_hash="b" * 64)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/learnings/bulk",
        json={"ids": [first, second], "action": "deny"},
    )

    assert response.status_code == 200
    results = response.json()["results"]
    assert [r["status_code"] for r in results] == [200, 200]


def test_agent_detail_carries_learnings_fields(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)
    _seed_learning(session_factory)

    body = client.get(f"/api/agents/{AGENT_NAME}").json()

    assert body["pending_learnings"] == 1
    assert body["can_decide_learnings"] is True  # IAP disabled: gate passes
