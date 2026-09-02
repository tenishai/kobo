"""Tests for POST /api/agents/name-suggestions (create flow v2 addendum):
the naming station's generator route — three name+reason suggestions ahead
of persona/team/skills, always exactly three even when the generator errors
out. Same harness shape as test_api_avatars.py/test_api_personas.py: fakes
only (`tests/fakes.py`), no real Vertex AI network involved anywhere.

persona_id/exclude/round (create-identity, Task 3) get their own section
near the bottom: server-side persona lookup feeding the generator the
persona's real body + tags instead of the client's own thin fields, an
unresolved persona_id falling back to those client fields rather than
ever 404ing, and exclude/round riding through to both the generator and
`finalize_suggestions`'s own drop-set/fallback-ordering.
"""

from __future__ import annotations

import json

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
    FakeStorage,
)

VALID_PAYLOAD = {
    "who_they_are": "a calm troubleshooter for billing questions",
    "what_they_do": "Helps customers troubleshoot billing issues.",
}

# A distinctive token planted in a seeded persona's real body — present
# only there, never in any client-submitted `what_they_do` in this file, so
# a test can prove the route fed the generator the persona's REAL text
# rather than the thin client-submitted one.
PERSONA_BODY_TOKEN = "zylophonic-quasar-archive"


def _index_bytes(entries: list[dict]) -> bytes:
    return json.dumps({"entries": entries}).encode()


def _persona_storage(
    persona_id: str = "seed-persona", *, tags: list[str] | None = None
) -> FakeStorage:
    """A FakeStorage whose personas-bucket carries one persona (index entry
    + persona.md body) — `persona_id` resolves to a body containing
    `PERSONA_BODY_TOKEN` and to `tags` (default a small canned pair)."""
    entries = [
        {
            "id": persona_id,
            "name": "Seed Persona",
            "description": "a thin catalog label",
            "team": "shared",
            "tags": tags if tags is not None else ["astral", "calm"],
            "builtin": False,
        }
    ]
    body = (
        "You are {name}, a calm guide.\n\n"
        "## What you do\n\n"
        f"You run the {PERSONA_BODY_TOKEN} archive.\n"
    )
    return FakeStorage(
        buckets={
            "personas-bucket": {
                "index.json": (_index_bytes(entries), {}),
                f"{persona_id}/persona.md": (body.encode(), {}),
            }
        }
    )


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config(tmp_path):
    # personas_bucket is set even though most tests here never seed one --
    # a persona_id lookup with no storage configured at all (clients.storage
    # is None) already short-circuits to the fallback path, so this is safe
    # for every existing test and lets the persona_id tests below just plug
    # a seeded FakeStorage into the same config.
    return Config(workdir=tmp_path / "work", personas_bucket="personas-bucket")


# Sentinel distinguishing "name kwarg not passed" (-> default FakeName())
# from an explicit `name=None` (-> Clients.name is None, the "generator
# unavailable, go straight to fallback" path) — plain `None` can't serve as
# both, same idiom as test_api_avatars.py's own `_IMAGE_UNSET`.
_NAME_UNSET = object()


def _make_client(session_factory, config, *, name=_NAME_UNSET, storage=None) -> TestClient:
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
                storage=storage,
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
        (VALID_PAYLOAD["who_they_are"], VALID_PAYLOAD["what_they_do"], None, [])
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


# --- persona_id server-side lookup (create-identity, Task 3) ------------------------


def test_persona_id_lookup_feeds_the_real_body_and_tags(session_factory, config):
    storage = _persona_storage("seed-persona", tags=["astral", "calm"])
    fake_name = FakeName()
    client = _make_client(session_factory, config, name=fake_name, storage=storage)

    response = client.post(
        "/api/agents/name-suggestions",
        json={
            "who_they_are": "a thin label",
            "what_they_do": "thin client text nobody should see reach the generator",
            "persona_id": "seed-persona",
        },
    )

    assert response.status_code == 200
    assert len(fake_name.calls) == 1
    who, what, tags, _exclude = fake_name.calls[0]
    assert who == "a thin label"
    assert PERSONA_BODY_TOKEN in what
    assert "thin client text" not in what
    assert tags == ["astral", "calm"]


def test_persona_id_lookup_truncates_body_to_2000_chars(session_factory, config):
    long_tail = "x" * 3000
    storage = FakeStorage(
        buckets={
            "personas-bucket": {
                "index.json": (
                    _index_bytes(
                        [
                            {
                                "id": "long-persona",
                                "name": "Long Persona",
                                "description": "d",
                                "team": "shared",
                                "tags": [],
                                "builtin": False,
                            }
                        ]
                    ),
                    {},
                ),
                "long-persona/persona.md": (long_tail.encode(), {}),
            }
        }
    )
    fake_name = FakeName()
    client = _make_client(session_factory, config, name=fake_name, storage=storage)

    client.post(
        "/api/agents/name-suggestions",
        json={**VALID_PAYLOAD, "persona_id": "long-persona"},
    )

    _who, what, _tags, _exclude = fake_name.calls[0]
    assert len(what) == 2000


def test_unresolved_persona_id_falls_back_to_client_fields(session_factory, config):
    storage = FakeStorage(buckets={"personas-bucket": {"index.json": (_index_bytes([]), {})}})
    fake_name = FakeName()
    client = _make_client(session_factory, config, name=fake_name, storage=storage)

    response = client.post(
        "/api/agents/name-suggestions",
        json={**VALID_PAYLOAD, "persona_id": "ghost"},
    )

    assert response.status_code == 200
    suggestions = response.json()["suggestions"]
    assert len(suggestions) == SUGGESTION_COUNT
    _who, what, tags, _exclude = fake_name.calls[0]
    assert what == VALID_PAYLOAD["what_they_do"]
    assert tags is None


def test_unresolved_persona_id_with_no_storage_configured_still_200(session_factory, config):
    # No FakeStorage at all -- clients.storage stays None. Same "never
    # 404s, never even errors" contract as an id the index simply lacks.
    client = _make_client(session_factory, config, name=None)

    response = client.post(
        "/api/agents/name-suggestions",
        json={**VALID_PAYLOAD, "persona_id": "whatever"},
    )

    assert response.status_code == 200
    assert len(response.json()["suggestions"]) == SUGGESTION_COUNT


# --- exclude / round (create-identity, Task 3) --------------------------------------


def test_exclude_and_round_reach_the_generator_and_finalizer(session_factory, config):
    client = _make_client(session_factory, config, name=None)

    first = client.post("/api/agents/name-suggestions", json=VALID_PAYLOAD)
    first_names = [s["name"] for s in first.json()["suggestions"]]

    second = client.post(
        "/api/agents/name-suggestions",
        json={**VALID_PAYLOAD, "exclude": first_names, "round": 1},
    )
    second_names = [s["name"] for s in second.json()["suggestions"]]

    assert not (set(first_names) & set(second_names))
    assert len(second_names) == SUGGESTION_COUNT


def test_exclude_is_lowercased_before_reaching_the_generator(session_factory, config):
    fake_name = FakeName()
    client = _make_client(session_factory, config, name=fake_name)

    client.post(
        "/api/agents/name-suggestions",
        json={**VALID_PAYLOAD, "exclude": ["Ember", "CEDAR"]},
    )

    _who, _what, _tags, exclude = fake_name.calls[0]
    assert exclude == ["cedar", "ember"]


def test_name_suggestions_422_exclude_over_cap(session_factory, config):
    client = _make_client(session_factory, config)

    response = client.post(
        "/api/agents/name-suggestions",
        json={**VALID_PAYLOAD, "exclude": [f"name{i}" for i in range(21)]},
    )

    assert response.status_code == 422


def test_name_suggestions_422_round_negative(session_factory, config):
    client = _make_client(session_factory, config)

    response = client.post(
        "/api/agents/name-suggestions", json={**VALID_PAYLOAD, "round": -1}
    )

    assert response.status_code == 422


def test_name_suggestions_422_round_over_cap(session_factory, config):
    client = _make_client(session_factory, config)

    response = client.post(
        "/api/agents/name-suggestions", json={**VALID_PAYLOAD, "round": 21}
    )

    assert response.status_code == 422


def test_name_suggestions_422_what_they_do_over_2000_chars(session_factory, config):
    client = _make_client(session_factory, config)

    response = client.post(
        "/api/agents/name-suggestions",
        json={"who_they_are": "a helper", "what_they_do": "x" * 2001},
    )

    assert response.status_code == 422


def test_name_suggestions_what_they_do_up_to_2000_chars_is_allowed(session_factory, config):
    client = _make_client(session_factory, config)

    response = client.post(
        "/api/agents/name-suggestions",
        json={"who_they_are": "a helper", "what_they_do": "x" * 2000},
    )

    assert response.status_code == 200


def test_returns_exactly_three(session_factory, config):
    # Exercises persona_id + exclude + round together -- the full new
    # field combination in one call -- and still lands on exactly
    # SUGGESTION_COUNT, same contract as every other path through this
    # route.
    storage = _persona_storage("seed-persona")
    client = _make_client(session_factory, config, name=None, storage=storage)

    response = client.post(
        "/api/agents/name-suggestions",
        json={
            **VALID_PAYLOAD,
            "persona_id": "seed-persona",
            "exclude": ["ember"],
            "round": 2,
        },
    )

    assert response.status_code == 200
    assert len(response.json()["suggestions"]) == SUGGESTION_COUNT
