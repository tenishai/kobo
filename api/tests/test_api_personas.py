"""Route-level tests for POST /api/personas (Phase A Task 4): the persona
builder route, exercised through a full TestClient/create_app stack with
FakeStorage standing in for GCS — same harness shape as test_api_search.py.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from pnyx_api.clients import Clients
from pnyx_api.config import Config
from pnyx_api.main import create_app
from pnyx_api.models import Base
from tests.fakes import (
    FakeAgent,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
    FakeStorage,
)


def _index_bytes(entries: list[dict]) -> bytes:
    return json.dumps({"entries": entries}).encode()


SKILL_ENTRIES = [
    {
        "id": "code-review",
        "name": "Code review",
        "description": "Reviews pull requests",
        "team": "shared",
        "tags": ["review", "quality"],
        "sha": "abc123",
        "builtin": False,
    },
]

VALID_PAYLOAD = {
    "name": "Aria the Assistant",
    "role": "Customer support specialist",
    "purpose": "Help customers troubleshoot billing issues.",
    "tone": "warm",
    "tags": ["support"],
    "team": "cs-team",
}


def _minimal_clients(**kwargs) -> Clients:
    """A Clients with every non-storage field a bare fake — this route
    never touches github/gojira/cloud/agent/kubectl, but the Clients
    dataclass requires them (no defaults)."""
    return Clients(
        github=FakeGitHub(),
        gojira=FakeGojira(),
        cloud=FakeCloud(),
        agent=FakeAgent(),
        kubectl=FakeKubectl(),
        **kwargs,
    )


def _make_client(config: Config, clients: Clients, tmp_path) -> TestClient:
    engine = create_engine(
        f"sqlite:///{tmp_path}/pnyx.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    return TestClient(create_app(session_factory, config=config, clients=clients))


@pytest.fixture
def fake_storage():
    return FakeStorage(
        buckets={
            "skills-bucket": {"index.json": (_index_bytes(SKILL_ENTRIES), {})},
            "personas-bucket": {},
        }
    )


@pytest.fixture
def config(tmp_path):
    return Config(
        workdir=tmp_path / "work",
        skills_bucket="skills-bucket",
        personas_bucket="personas-bucket",
    )


@pytest.fixture
def clients(fake_storage):
    return _minimal_clients(storage=fake_storage)


@pytest.fixture
def client(config, clients, tmp_path):
    return _make_client(config, clients, tmp_path)


def test_create_persona_201(client, fake_storage):
    response = client.post("/api/personas", json=VALID_PAYLOAD)

    assert response.status_code == 201
    assert response.json() == {
        "id": "aria-the-assistant",
        "name": "Aria the Assistant",
        "description": "Customer support specialist",
        "team": "cs-team",
        "tags": ["support"],
        "builtin": False,
    }
    assert "write_bytes:personas-bucket:aria-the-assistant/persona.md" in fake_storage.calls


def test_create_persona_is_instantly_searchable(client):
    client.post("/api/personas", json=VALID_PAYLOAD)

    response = client.get("/api/personas/search", params={"q": "aria"})

    assert response.status_code == 200
    ids = [r["id"] for r in response.json()["results"]]
    assert "aria-the-assistant" in ids


def test_create_persona_preserves_existing_skill_entries(client):
    """Regression test for the ingest hazard (Task 4 controller decision
    #1): search.ingest_indexes does a FULL rebuild of every kind it's
    given, so a persona-create route that ingested ONLY the personas
    catalog would silently wipe the skills table clean. The route must
    call ingest_indexes with the full kind->bucket mapping
    (configured_catalogs), not just {"persona": ...}.
    """
    pre = client.get("/api/skills/search", params={"q": "revi"})
    assert pre.json()["results"], "fixture sanity: skill entry should be searchable pre-create"

    create_response = client.post("/api/personas", json=VALID_PAYLOAD)
    assert create_response.status_code == 201

    post = client.get("/api/skills/search", params={"q": "revi"})
    assert post.json()["results"] == pre.json()["results"]


def test_create_persona_duplicate_409(client):
    first = client.post("/api/personas", json=VALID_PAYLOAD)
    assert first.status_code == 201

    response = client.post("/api/personas", json=VALID_PAYLOAD)

    assert response.status_code == 409
    assert response.json() == {"error": "a persona with that name already exists"}


def test_create_persona_422_bad_tone(client):
    response = client.post("/api/personas", json={**VALID_PAYLOAD, "tone": "sarcastic"})

    assert response.status_code == 422


def test_create_persona_422_bad_team_shape(client):
    response = client.post("/api/personas", json={**VALID_PAYLOAD, "team": "Not Valid!"})

    assert response.status_code == 422


def test_create_persona_422_missing_tags(client):
    payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "tags"}

    response = client.post("/api/personas", json=payload)

    assert response.status_code == 422


def test_create_persona_422_name_does_not_slugify(client):
    """A name that passes pydantic's own length check but can't slugify to
    a valid id (PersonaError, raised from persona_id inside create_persona)
    must still surface as a 422, not an unhandled 500."""
    response = client.post("/api/personas", json={**VALID_PAYLOAD, "name": "!!!"})

    assert response.status_code == 422


def test_create_persona_503_when_storage_none(config, tmp_path):
    client = _make_client(config, _minimal_clients(), tmp_path)

    response = client.post("/api/personas", json=VALID_PAYLOAD)

    assert response.status_code == 503
    assert response.json() == {"error": "catalogs aren't set up yet"}


def test_create_persona_503_when_personas_bucket_unset(tmp_path):
    config = Config(workdir=tmp_path / "work", skills_bucket="skills-bucket")
    client = _make_client(config, _minimal_clients(storage=FakeStorage()), tmp_path)

    response = client.post("/api/personas", json=VALID_PAYLOAD)

    assert response.status_code == 503
    assert response.json() == {"error": "catalogs aren't set up yet"}


def test_create_persona_502_on_storage_error(config, tmp_path):
    erroring_storage = FakeStorage(write_error="storage is on fire")
    client = _make_client(config, _minimal_clients(storage=erroring_storage), tmp_path)

    response = client.post("/api/personas", json=VALID_PAYLOAD)

    assert response.status_code == 502
