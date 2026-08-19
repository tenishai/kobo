"""Tests for `POST /api/agents/{name}/directory/sync` (colleagues switchboard
plan, Task 2): ready-gated, any signed-in user (no operator gate), 409 when
the switchboard isn't configured, 502 on a sync failure, `{"registered":
true}` on success — against the fakes (tests/fakes.py), no real
git/switchboard involved.

Same harness shape as test_api_metadata.py: own `session_factory`/`config`
fixtures, a `TestClient` built from `create_app`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.clients import ClientError, Clients
from kobo_api.config import Config
from kobo_api.main import create_app
from kobo_api.models import Agent, AgentStatus, Base
from tests.fakes import (
    FakeAgent,
    FakeCabal,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
)

AGENT_NAME = "clio"
PERSONA = "helpful-generalist"
REPO = "acme/agent-clio"
ENDPOINT_TEMPLATE = "http://agent-{name}.default.svc.cluster.local"


class _RaisingGitHub(FakeGitHub):
    """Every clone raises `ClientError` — models a transient GitHub outage
    without a real subprocess (mirrors test_api_metadata.py's own fixture)."""

    def clone(self, repo: str, dest: Path) -> None:
        raise ClientError("gh: rate limited")


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


def _enabled_config(config):
    return config.model_copy(
        update={"cabal_url": "http://cabal.internal", "agent_endpoint_template": ENDPOINT_TEMPLATE}
    )


def _make_client(session_factory, config, *, github=None, cabal=None):
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
                cabal=cabal,
            ),
        )
    )


def _seed_agent(session_factory, *, status=AgentStatus.READY, repo_full_name=REPO):
    session = session_factory()
    session.add(
        Agent(
            name=AGENT_NAME,
            persona=PERSONA,
            model_id="vertex_ai/claude-sonnet-4-5",
            status=status,
            repo_full_name=repo_full_name,
        )
    )
    session.commit()
    session.close()


def test_sync_directory_unknown_agent_404s(session_factory, config):
    client = _make_client(session_factory, config)
    response = client.post(f"/api/agents/{AGENT_NAME}/directory/sync")
    assert response.status_code == 404


def test_sync_directory_not_ready_409s(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, status=AgentStatus.PROVISIONING)

    response = client.post(f"/api/agents/{AGENT_NAME}/directory/sync")

    assert response.status_code == 409
    assert response.json() == {"error": "agent is not ready to join the directory yet"}


def test_sync_directory_disabled_switchboard_409s(session_factory, config):
    """The default `config` fixture carries neither cabal org key — the
    route must 409 with a plain, switchboard-specific detail rather than
    attempting anything against a None cabal client."""
    client = _make_client(session_factory, config)
    _seed_agent(session_factory)

    response = client.post(f"/api/agents/{AGENT_NAME}/directory/sync")

    assert response.status_code == 409
    assert response.json() == {"error": "the switchboard is not configured"}


def test_sync_directory_success_registers_and_returns_registered_true(
    session_factory, config, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    fake_cabal = FakeCabal()
    fake_github = FakeGitHub()
    client = _make_client(session_factory, cfg, github=fake_github, cabal=fake_cabal)
    _seed_agent(session_factory)

    response = client.post(f"/api/agents/{AGENT_NAME}/directory/sync")

    assert response.status_code == 200
    assert response.json() == {"registered": True}
    assert len(fake_cabal.graduate_calls) == 1
    assert fake_cabal.graduate_calls[0]["name"] == AGENT_NAME
    assert (
        fake_cabal.graduate_calls[0]["endpoint"]
        == f"http://agent-{AGENT_NAME}.default.svc.cluster.local"
    )


def test_sync_directory_card_fetch_failure_returns_502(session_factory, config, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    fake_cabal = FakeCabal()
    client = _make_client(session_factory, cfg, github=_RaisingGitHub(), cabal=fake_cabal)
    _seed_agent(session_factory)

    response = client.post(f"/api/agents/{AGENT_NAME}/directory/sync")

    assert response.status_code == 502
    assert response.json() == {"error": "couldn't sync the directory. Try again."}
    assert fake_cabal.graduate_calls == []


def test_sync_directory_graduate_failure_returns_502(session_factory, config, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    fake_cabal = FakeCabal(graduate_error="switchboard unreachable")
    client = _make_client(session_factory, cfg, cabal=fake_cabal)
    _seed_agent(session_factory)

    response = client.post(f"/api/agents/{AGENT_NAME}/directory/sync")

    assert response.status_code == 502
    assert response.json() == {"error": "couldn't sync the directory. Try again."}


def test_sync_directory_malformed_endpoint_template_returns_502(session_factory, config, monkeypatch):
    """A malformed `agent_endpoint_template` (Task 4b hardening) makes
    `sync_directory` raise `ClientError` instead of a bare `KeyError` —
    the route's existing `except ClientError` 502 handler covers it with
    no new branch, rather than the request 500ing."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = config.model_copy(
        update={
            "cabal_url": "http://cabal.internal",
            "agent_endpoint_template": "http://agent-{oops}.default.svc.cluster.local",
        }
    )
    fake_cabal = FakeCabal()
    client = _make_client(session_factory, cfg, cabal=fake_cabal)
    _seed_agent(session_factory)

    response = client.post(f"/api/agents/{AGENT_NAME}/directory/sync")

    assert response.status_code == 502
    assert response.json() == {"error": "couldn't sync the directory. Try again."}
    assert fake_cabal.graduate_calls == []
