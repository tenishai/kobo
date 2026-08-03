"""Route-level tests for the FTS catalog endpoints (Phase A Task 3):
GET /api/skills/search, GET /api/personas/search, GET /api/catalog/teams, and
POST /api/catalogs/refresh — exercised through a full TestClient/create_app
stack with `FakeStorage` standing in for GCS, same harness shape as
test_api_agents.py's own fixtures.
"""

from __future__ import annotations

import json
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from pnyx_api.clients import Clients
from pnyx_api.config import Config
from pnyx_api.main import _maybe_refresh_catalogs, create_app
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
    return json.dumps({"generated_at": "2026-08-03T00:00:00Z", "entries": entries}).encode()


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

PERSONA_ENTRIES = [
    {
        "id": "helpful-generalist",
        "name": "Helpful Generalist",
        "description": "A friendly all-rounder",
        "team": "shared",
        "tags": [],
        "builtin": True,
    },
]


def _minimal_clients(**kwargs) -> Clients:
    """A Clients with every non-storage field a bare fake — these routes
    never touch github/gojira/cloud/agent/kubectl, but the Clients dataclass
    requires them (no defaults)."""
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
            "personas-bucket": {"index.json": (_index_bytes(PERSONA_ENTRIES), {})},
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


def test_search_skills_returns_entries(client, fake_storage):
    response = client.get("/api/skills/search", params={"q": "revi"})

    assert response.status_code == 200
    assert response.json() == {
        "results": [
            {
                "id": "code-review",
                "name": "Code review",
                "description": "Reviews pull requests",
                "team": "shared",
                "tags": ["review", "quality"],
                "sha": "abc123",
            }
        ]
    }
    # getting anything back at all means the TTL hook had to ingest first —
    # nothing pre-seeds the table.
    assert "read_bytes:skills-bucket:index.json" in fake_storage.calls


def test_search_skills_team_filter(client):
    response = client.get("/api/skills/search", params={"q": "", "team": "platform"})

    assert response.status_code == 200
    assert response.json() == {"results": []}


def test_search_personas_returns_entries_with_builtin_not_sha(client):
    response = client.get("/api/personas/search", params={"q": ""})

    assert response.status_code == 200
    body = response.json()
    assert body["results"] == [
        {
            "id": "helpful-generalist",
            "name": "Helpful Generalist",
            "description": "A friendly all-rounder",
            "team": "shared",
            "tags": [],
            "builtin": True,
        }
    ]
    assert "sha" not in body["results"][0]


def test_catalog_teams_route(client):
    response = client.get("/api/catalog/teams")

    assert response.status_code == 200
    assert response.json() == {"teams": ["shared"]}


def test_refresh_route_returns_counts(client):
    response = client.post("/api/catalogs/refresh")

    assert response.status_code == 200
    assert response.json() == {"skills": 1, "personas": 1}


def test_ttl_refresh_runs_once_for_two_immediate_calls(client, fake_storage):
    client.get("/api/catalog/teams")
    client.get("/api/catalog/teams")

    assert fake_storage.calls.count("read_bytes:skills-bucket:index.json") == 1
    assert fake_storage.calls.count("read_bytes:personas-bucket:index.json") == 1


def test_ttl_lock_serializes_concurrent_refreshes(config, tmp_path):
    """White-box concurrency check that `catalog_lock` — not just the outer
    TTL check the sequential test above already exercises — actually
    serializes two refreshes racing at the same instant: thread 1 is parked
    inside `read_bytes` (holding the lock, mid-ingest) when thread 2 reaches
    `_maybe_refresh_catalogs`; thread 2 must block on the lock and then find
    the table already fresh, rather than starting a second ingest.
    """
    entered_read = threading.Event()
    release_read = threading.Event()

    class BlockingStorage(FakeStorage):
        def read_bytes(self, bucket, key):
            if bucket == "skills-bucket":
                entered_read.set()
                release_read.wait(timeout=2)
            return super().read_bytes(bucket, key)

    storage = BlockingStorage(
        buckets={
            "skills-bucket": {"index.json": (_index_bytes(SKILL_ENTRIES), {})},
            "personas-bucket": {"index.json": (_index_bytes(PERSONA_ENTRIES), {})},
        }
    )
    engine = create_engine(
        f"sqlite:///{tmp_path}/pnyx.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    app = create_app(session_factory, config=config, clients=_minimal_clients(storage=storage))

    t1 = threading.Thread(target=_maybe_refresh_catalogs, args=(app, engine))
    t1.start()
    assert entered_read.wait(timeout=2), "thread 1 never reached read_bytes"

    # thread 1 holds catalog_lock right now (mid-ingest, blocked inside
    # read_bytes) — thread 2 must block trying to acquire it.
    t2 = threading.Thread(target=_maybe_refresh_catalogs, args=(app, engine))
    t2.start()

    release_read.set()
    t1.join(timeout=2)
    t2.join(timeout=2)

    assert storage.calls.count("read_bytes:skills-bucket:index.json") == 1
    assert storage.calls.count("read_bytes:personas-bucket:index.json") == 1


def test_search_never_500s_on_malformed_index(config, tmp_path):
    bad_storage = FakeStorage(
        buckets={
            "skills-bucket": {"index.json": (b"not json", {})},
            "personas-bucket": {},
        }
    )
    client = _make_client(config, _minimal_clients(storage=bad_storage), tmp_path)

    response = client.get("/api/skills/search", params={"q": ""})

    # the TTL hook's ingest failed and was swallowed — the (empty) table is
    # served instead of a 500.
    assert response.status_code == 200
    assert response.json() == {"results": []}


def test_refresh_route_503_when_storage_none(config, tmp_path):
    client = _make_client(config, _minimal_clients(), tmp_path)

    response = client.post("/api/catalogs/refresh")

    assert response.status_code == 503
    assert response.json() == {"error": "catalogs aren't set up yet"}


def test_refresh_route_503_when_both_buckets_unset(tmp_path):
    config = Config(workdir=tmp_path / "work")  # skills_bucket/personas_bucket default ""
    client = _make_client(config, _minimal_clients(storage=FakeStorage()), tmp_path)

    response = client.post("/api/catalogs/refresh")

    assert response.status_code == 503
    assert response.json() == {"error": "catalogs aren't set up yet"}


def test_refresh_route_502_on_storage_error(config, tmp_path):
    erroring_storage = FakeStorage(read_error="storage is on fire")
    client = _make_client(config, _minimal_clients(storage=erroring_storage), tmp_path)

    response = client.post("/api/catalogs/refresh")

    assert response.status_code == 502
