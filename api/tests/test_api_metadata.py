"""Tests for the metadata HTTP routes (persona-metadata plan, Task 3):
`PATCH /api/agents/{name}/metadata` and `POST
/api/agents/{name}/metadata/regenerate` — ready-gated, any signed-in user
(no operator gate), against the fakes (tests/fakes.py), no real
git/kubectl/Vertex involved.

Same harness shape as test_api_slack.py/test_api_knowledge.py: own
`session_factory`/`config` fixtures, a `TestClient` built from `create_app`.
The 502-consistency proof (a card-commit failure must leave the DB
untouched) follows test_api_slack.py's own `test_put_channels_rewire_
failure_returns_502_and_leaves_original_channels_stored` idiom exactly: a
follow-up GET through the SAME TestClient (a fresh request-scoped session
per call, via `get_session`) is the "fresh-session" proof — it can only see
what the PATCH/regenerate route actually committed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.clients import ClientError, Clients
from kobo_api.config import Config
from kobo_api.main import create_app
from kobo_api.models import Agent, AgentStatus, Base
from kobo_api.personas import seed_default_personas
from kobo_api.purpose_tags import encode_tags
from kobo_api.search import ingest_indexes
from tests.fakes import (
    FakeAgent,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
    FakeName,
    FakeStorage,
)

AGENT_NAME = "clio"
PERSONA = "helpful-generalist"
REPO = "acme/agent-clio"

SKILLS_BUCKET = "skills-bucket"
PERSONAS_BUCKET = "personas-bucket"


class _RaisingGitHub(FakeGitHub):
    """Every clone raises `ClientError` — the card-commit 502 fixture,
    modeling a transient GitHub outage without a real subprocess."""

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
    return Config(
        workdir=tmp_path / "work", skills_bucket=SKILLS_BUCKET, personas_bucket=PERSONAS_BUCKET
    )


def _make_client(session_factory, config, *, github=None, name=None, storage=None):
    if storage is None:
        storage = FakeStorage()
        seed_default_personas(storage, bucket=PERSONAS_BUCKET)
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
                storage=storage,
                name=name,
            ),
        )
    )


def _seed_agent(
    session_factory,
    config,
    *,
    status=AgentStatus.READY,
    repo_full_name=REPO,
    team="shared",
    purpose_tags=None,
    capability=None,
    skills=None,
):
    session = session_factory()
    session.add(
        Agent(
            name=AGENT_NAME,
            persona=PERSONA,
            model_id=config.models[0].id,
            status=status,
            repo_full_name=repo_full_name,
            team=team,
            purpose_tags=encode_tags(purpose_tags) if purpose_tags is not None else None,
            capability=capability,
            skills=skills or [],
        )
    )
    session.commit()
    session.close()


# --- PATCH /api/agents/{name}/metadata -------------------------------------------


def test_patch_metadata_unknown_agent_404s(session_factory, config):
    client = _make_client(session_factory, config)
    response = client.patch(f"/api/agents/{AGENT_NAME}/metadata", json={"team": "acme-team"})
    assert response.status_code == 404


def test_patch_metadata_not_ready_409s(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, status=AgentStatus.PROVISIONING)

    response = client.patch(f"/api/agents/{AGENT_NAME}/metadata", json={"team": "acme-team"})

    assert response.status_code == 409
    assert response.json() == {"error": "agent is not ready for directory changes yet"}


def test_patch_metadata_updates_all_three_fields_and_returns_sha(session_factory, config):
    github = FakeGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config, team="shared", purpose_tags=["old"])

    response = client.patch(
        f"/api/agents/{AGENT_NAME}/metadata",
        json={
            "team": "acme-team",
            "purpose_tags": ["billing", "support"],
            "capability": "Helps customers with billing.",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "team": "acme-team",
        "purpose_tags": ["billing", "support"],
        "capability": "Helps customers with billing.",
        "sha": response.json()["sha"],
    }
    assert response.json()["sha"]
    assert github.pushes[-1][1] == "kobo: directory details updated"

    detail = client.get(f"/api/agents/{AGENT_NAME}").json()
    assert detail["team"] == "acme-team"
    assert detail["purpose_tags"] == ["billing", "support"]
    assert detail["capability"] == "Helps customers with billing."


def test_patch_metadata_partial_update_only_touches_submitted_fields(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(
        session_factory, config, team="acme-team", purpose_tags=["billing"],
        capability="Original sentence.",
    )

    response = client.patch(
        f"/api/agents/{AGENT_NAME}/metadata", json={"capability": "A new sentence."}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["team"] == "acme-team"
    assert body["purpose_tags"] == ["billing"]
    assert body["capability"] == "A new sentence."


def test_patch_metadata_empty_body_leaves_everything_unchanged(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(
        session_factory, config, team="acme-team", purpose_tags=["billing"],
        capability="Original sentence.",
    )

    response = client.patch(f"/api/agents/{AGENT_NAME}/metadata", json={})

    assert response.status_code == 200
    assert response.json()["team"] == "acme-team"
    assert response.json()["purpose_tags"] == ["billing"]
    assert response.json()["capability"] == "Original sentence."


def test_patch_metadata_invalid_team_slug_rejected(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)

    response = client.patch(
        f"/api/agents/{AGENT_NAME}/metadata", json={"team": "Not A Valid Slug!"}
    )

    assert response.status_code == 422
    assert response.json() == {"error": "that doesn't look like a team name"}


def test_patch_metadata_purpose_tags_over_cap_rejected(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)

    response = client.patch(
        f"/api/agents/{AGENT_NAME}/metadata",
        json={"purpose_tags": [f"tag{i}" for i in range(9)]},
    )

    assert response.status_code == 422
    assert response.json() == {"error": "Eight tags at most."}


def test_patch_metadata_capability_over_max_len_rejected(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)

    response = client.patch(
        f"/api/agents/{AGENT_NAME}/metadata", json={"capability": "x" * 161}
    )

    assert response.status_code == 422
    assert response.json() == {
        "error": "capability must be plain text, 160 characters or fewer"
    }


def test_patch_metadata_capability_blank_after_cleanup_rejected(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)

    response = client.patch(f"/api/agents/{AGENT_NAME}/metadata", json={"capability": "   "})

    assert response.status_code == 422


def test_patch_metadata_capability_strips_em_dashes_before_storing(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)

    response = client.patch(
        f"/api/agents/{AGENT_NAME}/metadata",
        json={"capability": "Helps with billing—and returns."},
    )

    assert response.status_code == 200
    assert "—" not in response.json()["capability"]
    assert response.json()["capability"] == "Helps with billing-and returns."


def test_patch_metadata_card_commit_failure_returns_502_and_db_not_committed(
    session_factory, config
):
    client = _make_client(session_factory, config, github=_RaisingGitHub())
    _seed_agent(
        session_factory, config, team="original-team", purpose_tags=["original"],
        capability="Original sentence.",
    )

    response = client.patch(
        f"/api/agents/{AGENT_NAME}/metadata",
        json={
            "team": "new-team",
            "purpose_tags": ["new-tag"],
            "capability": "A new sentence.",
        },
    )

    assert response.status_code == 502
    assert response.json() == {
        "error": "couldn't update the directory details. Try again."
    }

    # Fresh-session proof: a follow-up GET (its own request-scoped session)
    # still shows the ORIGINAL stored values, not the submitted ones.
    detail = client.get(f"/api/agents/{AGENT_NAME}").json()
    assert detail["team"] == "original-team"
    assert detail["purpose_tags"] == ["original"]
    assert detail["capability"] == "Original sentence."


# --- POST /api/agents/{name}/metadata/regenerate ----------------------------------


def test_regenerate_metadata_unknown_agent_404s(session_factory, config):
    client = _make_client(session_factory, config)
    response = client.post(f"/api/agents/{AGENT_NAME}/metadata/regenerate")
    assert response.status_code == 404


def test_regenerate_metadata_not_ready_409s(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, status=AgentStatus.FAILED)

    response = client.post(f"/api/agents/{AGENT_NAME}/metadata/regenerate")

    assert response.status_code == 409
    assert response.json() == {"error": "agent is not ready for directory changes yet"}


def test_regenerate_metadata_uses_model_client_stores_and_commits(session_factory, config):
    github = FakeGitHub()
    fake_name = FakeName(capability="Freshly regenerated sentence.")
    client = _make_client(session_factory, config, github=github, name=fake_name)
    _seed_agent(
        session_factory, config, team="acme-team", purpose_tags=["billing"],
        capability="Stale sentence.",
    )

    response = client.post(f"/api/agents/{AGENT_NAME}/metadata/regenerate")

    assert response.status_code == 200
    assert response.json() == {
        "team": "acme-team",
        "purpose_tags": ["billing"],
        "capability": "Freshly regenerated sentence.",
        "sha": response.json()["sha"],
    }
    assert response.json()["sha"]
    assert github.pushes[-1][1] == "kobo: directory details updated"

    detail = client.get(f"/api/agents/{AGENT_NAME}").json()
    assert detail["capability"] == "Freshly regenerated sentence."


def test_regenerate_metadata_uses_current_skills_from_catalog(session_factory, config):
    storage = FakeStorage()
    seed_default_personas(storage, bucket=PERSONAS_BUCKET)
    storage.write_bytes(
        SKILLS_BUCKET,
        "shared/word-games/SKILL.md",
        (
            b"---\nname: word-games\ndescription: Plays word games with the "
            b"user.\n---\nBody.\n"
        ),
        metadata={"source-sha": "seedsha"},
        content_type="text/markdown",
    )
    storage.write_bytes(
        SKILLS_BUCKET,
        "index.json",
        json.dumps(
            {
                "entries": [
                    {
                        "id": "word-games",
                        "name": "Word games",
                        "description": "Plays word games with the user.",
                        "team": "shared",
                        "tags": ["fun"],
                        "sha": "seedsha",
                        "builtin": False,
                    }
                ]
            }
        ).encode(),
        content_type="application/json",
    )
    fake_name = FakeName(capability="Plays games and helps out.")
    client = _make_client(session_factory, config, name=fake_name, storage=storage)
    _seed_agent(
        session_factory, config,
        skills=[{"name": "word-games", "team": "shared", "sha": "seedsha"}],
    )
    # Warm the FTS catalog the same way the create route's own
    # _maybe_refresh_catalogs TTL hook does — regenerate reads it directly
    # via skill_descriptions_for_agent, it doesn't refresh it itself.
    session = session_factory()
    ingest_indexes(session.get_bind(), storage, {"skill": SKILLS_BUCKET})
    session.close()

    response = client.post(f"/api/agents/{AGENT_NAME}/metadata/regenerate")

    assert response.status_code == 200
    assert fake_name.capability_calls[0][1] == ["Plays word games with the user."]


def test_regenerate_metadata_card_commit_failure_returns_502_and_db_not_committed(
    session_factory, config
):
    client = _make_client(session_factory, config, github=_RaisingGitHub())
    _seed_agent(session_factory, config, capability="Stale sentence.")

    response = client.post(f"/api/agents/{AGENT_NAME}/metadata/regenerate")

    assert response.status_code == 502
    assert response.json() == {
        "error": "couldn't update the directory details. Try again."
    }

    detail = client.get(f"/api/agents/{AGENT_NAME}").json()
    assert detail["capability"] == "Stale sentence."


def test_regenerate_metadata_persona_lookup_failure_returns_502(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)
    # Overwrite the persona row to reference a persona that isn't in the
    # (bare) storage the client fixture was built with.
    session = session_factory()
    agent = session.query(Agent).filter_by(name=AGENT_NAME).first()
    agent.persona = "ghost-persona"
    session.commit()
    session.close()

    response = client.post(f"/api/agents/{AGENT_NAME}/metadata/regenerate")

    assert response.status_code == 502
    assert response.json() == {"error": "couldn't read this agent's persona. Try again."}
