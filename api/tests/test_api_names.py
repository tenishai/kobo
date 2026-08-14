"""Tests for POST /api/agents/name-suggestions (create flow v2 addendum):
the naming station's generator route — five name+reason suggestions ahead
of persona/team/skills, always exactly five even when the generator errors
out. Same harness shape as test_api_avatars.py/test_api_personas.py: fakes
only (`tests/fakes.py`), no real Vertex AI network involved anywhere.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.catalog import RESERVED_NAMES
from kobo_api.clients import Clients
from kobo_api.config import Config
from kobo_api.main import create_app
from kobo_api.models import Agent, AgentStatus, Base
from kobo_api.namegen import _FALLBACK_NAMES, SUGGESTION_COUNT
from tests.fakes import (
    FakeAgent,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
    FakeName,
)

VALID_PAYLOAD = {
    "who_they_are": "a calm troubleshooter for billing questions",
    "what_they_do": "Helps customers troubleshoot billing issues.",
}


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


# Sentinel distinguishing "name kwarg not passed" (-> default FakeName())
# from an explicit `name=None` (-> Clients.name is None, the "generator
# unavailable, go straight to fallback" path) — plain `None` can't serve as
# both, same idiom as test_api_avatars.py's own `_IMAGE_UNSET`.
_NAME_UNSET = object()


def _make_client(session_factory, config, *, name=_NAME_UNSET) -> TestClient:
    return TestClient(
        create_app(
            session_factory,
            config=config,
            clients=Clients(
                github=FakeGitHub(),
                gojira=FakeGojira(),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=FakeKubectl(),
                name=FakeName() if name is _NAME_UNSET else name,
            ),
        )
    )


def _seed_agent(session_factory, config, name: str) -> None:
    session = session_factory()
    session.add(
        Agent(
            name=name,
            persona="helpful-generalist",
            model_id=config.models[0].id,
            status=AgentStatus.READY,
        )
    )
    session.commit()
    session.close()


# --- Happy path -------------------------------------------------------------------


def test_name_suggestions_happy_path_returns_five_shaped_suggestions(session_factory, config):
    client = _make_client(session_factory, config)

    response = client.post("/api/agents/name-suggestions", json=VALID_PAYLOAD)

    assert response.status_code == 200
    body = response.json()
    assert list(body.keys()) == ["suggestions"]
    suggestions = body["suggestions"]
    assert len(suggestions) == SUGGESTION_COUNT
    for suggestion in suggestions:
        assert set(suggestion.keys()) == {"name", "reason"}
        assert isinstance(suggestion["name"], str)
        assert isinstance(suggestion["reason"], str)


def test_name_suggestions_calls_generator_with_the_submitted_fields(session_factory, config):
    fake_name = FakeName()
    client = _make_client(session_factory, config, name=fake_name)

    client.post("/api/agents/name-suggestions", json=VALID_PAYLOAD)

    assert fake_name.calls == [
        (VALID_PAYLOAD["who_they_are"], VALID_PAYLOAD["what_they_do"])
    ]


# --- 422 guards --------------------------------------------------------------------


def test_name_suggestions_422_missing_who_they_are(session_factory, config):
    client = _make_client(session_factory, config)

    response = client.post(
        "/api/agents/name-suggestions", json={"what_they_do": "does things"}
    )

    assert response.status_code == 422
    assert "error" in response.json()


def test_name_suggestions_422_missing_what_they_do(session_factory, config):
    client = _make_client(session_factory, config)

    response = client.post(
        "/api/agents/name-suggestions", json={"who_they_are": "a helper"}
    )

    assert response.status_code == 422
    assert "error" in response.json()


def test_name_suggestions_422_empty_who_they_are(session_factory, config):
    client = _make_client(session_factory, config)

    response = client.post(
        "/api/agents/name-suggestions",
        json={"who_they_are": "", "what_they_do": "does things"},
    )

    assert response.status_code == 422


def test_name_suggestions_422_empty_what_they_do(session_factory, config):
    client = _make_client(session_factory, config)

    response = client.post(
        "/api/agents/name-suggestions",
        json={"who_they_are": "a helper", "what_they_do": ""},
    )

    assert response.status_code == 422


def test_name_suggestions_invalid_payload_never_reaches_the_generator(session_factory, config):
    fake_name = FakeName()
    client = _make_client(session_factory, config, name=fake_name)

    client.post("/api/agents/name-suggestions", json={"who_they_are": ""})

    assert fake_name.calls == []


# --- Fallback path: generator down --------------------------------------------------


def test_name_suggestions_generator_client_error_falls_back_still_200_with_five(
    session_factory, config, caplog
):
    client = _make_client(
        session_factory, config, name=FakeName(suggest_error="model unavailable")
    )

    response = client.post("/api/agents/name-suggestions", json=VALID_PAYLOAD)

    assert response.status_code == 200
    suggestions = response.json()["suggestions"]
    assert len(suggestions) == SUGGESTION_COUNT
    for suggestion in suggestions:
        assert suggestion["name"] in _FALLBACK_NAMES
    assert any(
        "name suggestion" in record.message.lower() for record in caplog.records
    )


def test_name_suggestions_no_name_client_configured_falls_back_still_200_with_five(
    session_factory, config
):
    client = _make_client(session_factory, config, name=None)

    response = client.post("/api/agents/name-suggestions", json=VALID_PAYLOAD)

    assert response.status_code == 200
    suggestions = response.json()["suggestions"]
    assert len(suggestions) == SUGGESTION_COUNT
    for suggestion in suggestions:
        assert suggestion["name"] in _FALLBACK_NAMES


# --- Taken/reserved filtering, reading from the DB ----------------------------------


def test_name_suggestions_filters_out_taken_agent_names_from_the_db(session_factory, config):
    taken_pool_name = _FALLBACK_NAMES[0]
    _seed_agent(session_factory, config, taken_pool_name)
    client = _make_client(session_factory, config, name=None)

    response = client.post("/api/agents/name-suggestions", json=VALID_PAYLOAD)

    assert response.status_code == 200
    names = [s["name"] for s in response.json()["suggestions"]]
    assert taken_pool_name not in names
    assert len(names) == SUGGESTION_COUNT


def test_name_suggestions_filters_out_generator_candidate_matching_taken_name(
    session_factory, config
):
    _seed_agent(session_factory, config, "juniper")
    fake_name = FakeName(candidates=[{"name": "juniper", "reason": "already exists"}])
    client = _make_client(session_factory, config, name=fake_name)

    response = client.post("/api/agents/name-suggestions", json=VALID_PAYLOAD)

    assert response.status_code == 200
    names = [s["name"] for s in response.json()["suggestions"]]
    assert "juniper" not in names
    assert len(names) == SUGGESTION_COUNT


def test_name_suggestions_filters_out_reserved_name_candidate(session_factory, config):
    fake_name = FakeName(candidates=[{"name": "cortex", "reason": "an old platform name"}])
    client = _make_client(session_factory, config, name=fake_name)

    response = client.post("/api/agents/name-suggestions", json=VALID_PAYLOAD)

    assert response.status_code == 200
    names = [s["name"] for s in response.json()["suggestions"]]
    assert "cortex" not in names
    assert "cortex" in RESERVED_NAMES  # sanity: this is genuinely reserved
    assert len(names) == SUGGESTION_COUNT


# --- Determinism (fallback path) ----------------------------------------------------


def test_name_suggestions_same_persona_text_yields_same_five_on_fallback(session_factory, config):
    client = _make_client(session_factory, config, name=None)

    first = client.post("/api/agents/name-suggestions", json=VALID_PAYLOAD)
    second = client.post("/api/agents/name-suggestions", json=VALID_PAYLOAD)

    assert first.json() == second.json()


def test_name_suggestions_different_persona_text_yields_different_five_on_fallback(
    session_factory, config
):
    client = _make_client(session_factory, config, name=None)

    first = client.post("/api/agents/name-suggestions", json=VALID_PAYLOAD)
    second = client.post(
        "/api/agents/name-suggestions",
        json={
            "who_they_are": "a wildly different persona entirely",
            "what_they_do": "does something completely unrelated",
        },
    )

    assert first.json() != second.json()


# --- Fallback pool exhaustion (the "effectively never" 502) -------------------------


def test_name_suggestions_502_when_fallback_pool_cannot_produce_five(session_factory, config):
    # Seed enough agents named after the fallback pool to leave fewer than
    # five untaken slots — the only way finalize_suggestions can come back
    # short, per the addendum's "effectively never" 502 guard.
    for pool_name in _FALLBACK_NAMES[:-2]:
        _seed_agent(session_factory, config, pool_name)
    client = _make_client(session_factory, config, name=None)

    response = client.post("/api/agents/name-suggestions", json=VALID_PAYLOAD)

    assert response.status_code == 502
    assert "error" in response.json()
