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

from kobo_api.clients import Clients
from kobo_api.config import Config
from kobo_api.main import create_app
from kobo_api.models import Base
from kobo_api.persona_draft import (
    _fallback_name,
    _fallback_tags,
    _fallback_what,
    _fallback_who,
)
from tests.fakes import (
    FakeAgent,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
    FakeName,
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
    "who_they_are": "a calm troubleshooter for billing questions",
    "what_they_do": "Help customers troubleshoot billing issues.",
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
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
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
        "description": "a calm troubleshooter for billing questions",
        "team": "cs-team",
        # Server-derived (Persona builder v2 — the UI sends no tags at
        # all): the 6 longest non-stopword words out of name + who_they_are
        # + what_they_do, longest first. See test_personas.py's dedicated
        # `_derive_tags` tests for the algorithm itself; this just pins that
        # the route wires it through end to end.
        "tags": [
            "troubleshooter",
            "troubleshoot",
            "assistant",
            "questions",
            "customers",
            "billing",
        ],
        "builtin": False,
    }
    assert "write_bytes:personas-bucket:aria-the-assistant/persona.md" in fake_storage.calls


def test_create_persona_201_with_rules(client):
    """Rules (optional, up to 150) render into the body's Rules section — a
    regression pin that the route passes `rules` through to `create_persona`
    like every other field, not just the three required ones."""
    payload = {**VALID_PAYLOAD, "rules": ["Never share account numbers.", "Be concise."]}

    response = client.post("/api/personas", json=payload)

    assert response.status_code == 201


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


def test_create_persona_422_bad_team_shape(client):
    response = client.post("/api/personas", json={**VALID_PAYLOAD, "team": "Not Valid!"})

    assert response.status_code == 422


def test_create_persona_422_missing_who_they_are(client):
    payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "who_they_are"}

    response = client.post("/api/personas", json=payload)

    assert response.status_code == 422


def test_create_persona_422_too_many_rules(client):
    payload = {**VALID_PAYLOAD, "rules": ["ok"] * 151}

    response = client.post("/api/personas", json=payload)

    assert response.status_code == 422


def test_create_persona_422_merged_render_too_many_lines(client):
    """The route surfaces the merged-render gate's own message verbatim
    (kobo_api.personas._check_rendered_size, raised as PersonaError from
    create_persona and turned into a 422 by the existing
    `except PersonaError` handler) — not the generic field-name-only
    RequestValidationError copy."""
    payload = {**VALID_PAYLOAD, "rules": ["ok"] * 140}

    response = client.post("/api/personas", json=payload)

    assert response.status_code == 422
    assert response.json() == {
        "error": "persona renders to 151 lines; the limit is 150"
    }


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


# --- Team-optional (Persona v3 addendum): create-persona defaults to "shared" -------


def test_create_persona_omitted_team_defaults_to_shared(client):
    payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "team"}

    response = client.post("/api/personas", json=payload)

    assert response.status_code == 201
    assert response.json()["team"] == "shared"


def test_create_persona_explicit_team_still_honored(client):
    response = client.post("/api/personas", json=VALID_PAYLOAD)

    assert response.status_code == 201
    assert response.json()["team"] == VALID_PAYLOAD["team"]


# --- POST /api/personas/draft (Persona v3 addendum) ---------------------------------


DRAFT_PAYLOAD = {
    "job": "Help customers troubleshoot billing issues quickly and politely.",
}


def _draft_client(config, tmp_path, *, name=None) -> TestClient:
    clients = _minimal_clients(storage=FakeStorage(), name=name)
    return _make_client(config, clients, tmp_path)


def test_draft_persona_happy_path_returns_the_three_fields(config, tmp_path):
    fake_name = FakeName(
        draft={
            "name": "billing-buddy",
            "who_they_are": "a calm troubleshooter for billing questions",
            "what_they_do": "Helps customers resolve billing issues quickly.",
            "purpose_tags": ["billing", "support"],
        }
    )
    client = _draft_client(config, tmp_path, name=fake_name)

    response = client.post("/api/personas/draft", json=DRAFT_PAYLOAD)

    assert response.status_code == 200
    assert response.json() == {
        "name": "billing-buddy",
        "who_they_are": "a calm troubleshooter for billing questions",
        "what_they_do": "Helps customers resolve billing issues quickly.",
        "purpose_tags": ["billing", "support"],
    }


def test_draft_persona_model_purpose_tags_are_normalized(config, tmp_path):
    fake_name = FakeName(
        draft={
            "name": "billing-buddy",
            "who_they_are": "a calm troubleshooter for billing questions",
            "what_they_do": "Helps customers resolve billing issues quickly.",
            "purpose_tags": ["Billing", "billing", " Support ", "REFUNDS"],
        }
    )
    client = _draft_client(config, tmp_path, name=fake_name)

    response = client.post("/api/personas/draft", json=DRAFT_PAYLOAD)

    assert response.json()["purpose_tags"] == ["billing", "support", "refunds"]


def test_draft_persona_model_purpose_tags_capped_at_five(config, tmp_path):
    fake_name = FakeName(
        draft={
            "name": "billing-buddy",
            "who_they_are": "a calm troubleshooter for billing questions",
            "what_they_do": "Helps customers resolve billing issues quickly.",
            "purpose_tags": ["one", "two", "three", "four", "five", "six", "seven"],
        }
    )
    client = _draft_client(config, tmp_path, name=fake_name)

    response = client.post("/api/personas/draft", json=DRAFT_PAYLOAD)

    assert response.json()["purpose_tags"] == ["one", "two", "three", "four", "five"]


def test_draft_persona_falls_back_to_persona_own_tags_when_model_omits_them(
    config, tmp_path
):
    fake_name = FakeName(
        draft={
            "name": "billing-buddy",
            "who_they_are": "a calm troubleshooter for billing questions",
            "what_they_do": "Helps customers resolve billing issues quickly.",
        }
    )
    client = _draft_client(config, tmp_path, name=fake_name)

    response = client.post("/api/personas/draft", json=DRAFT_PAYLOAD)

    assert response.json()["purpose_tags"] == _fallback_tags(
        "billing-buddy",
        "a calm troubleshooter for billing questions",
        "Helps customers resolve billing issues quickly.",
    )


def test_draft_persona_calls_generator_with_job_and_rules(config, tmp_path):
    fake_name = FakeName()
    client = _draft_client(config, tmp_path, name=fake_name)

    client.post(
        "/api/personas/draft",
        json={**DRAFT_PAYLOAD, "rules": ["Never share account numbers.", "Be concise."]},
    )

    assert fake_name.draft_calls == [
        (DRAFT_PAYLOAD["job"], ["Never share account numbers.", "Be concise."])
    ]


def test_draft_persona_defaults_rules_to_empty_list(config, tmp_path):
    fake_name = FakeName()
    client = _draft_client(config, tmp_path, name=fake_name)

    client.post("/api/personas/draft", json=DRAFT_PAYLOAD)

    assert fake_name.draft_calls == [(DRAFT_PAYLOAD["job"], [])]


def test_draft_persona_per_field_fallback_on_invalid_generator_name(config, tmp_path):
    fake_name = FakeName(
        draft={
            "name": "x" * 61,  # over PersonaFields.name's 60-char bound
            "who_they_are": "a calm troubleshooter for billing questions",
            "what_they_do": "Helps customers resolve billing issues quickly.",
        }
    )
    client = _draft_client(config, tmp_path, name=fake_name)

    response = client.post("/api/personas/draft", json=DRAFT_PAYLOAD)

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == _fallback_name(DRAFT_PAYLOAD["job"])
    assert body["who_they_are"] == "a calm troubleshooter for billing questions"
    assert body["what_they_do"] == "Helps customers resolve billing issues quickly."


def test_draft_persona_generator_client_error_falls_back_still_200(config, tmp_path, caplog):
    client = _draft_client(
        config, tmp_path, name=FakeName(draft_error="model unavailable")
    )

    response = client.post("/api/personas/draft", json=DRAFT_PAYLOAD)

    assert response.status_code == 200
    job = DRAFT_PAYLOAD["job"]
    assert response.json() == {
        "name": _fallback_name(job),
        "who_they_are": _fallback_who(job),
        "what_they_do": _fallback_what(job),
        "purpose_tags": _fallback_tags(
            _fallback_name(job), _fallback_who(job), _fallback_what(job)
        ),
    }
    assert any("persona draft" in record.message.lower() for record in caplog.records)


def test_draft_persona_no_name_client_configured_falls_back_still_200(config, tmp_path):
    client = _draft_client(config, tmp_path, name=None)

    response = client.post("/api/personas/draft", json=DRAFT_PAYLOAD)

    assert response.status_code == 200
    job = DRAFT_PAYLOAD["job"]
    assert response.json() == {
        "name": _fallback_name(job),
        "who_they_are": _fallback_who(job),
        "what_they_do": _fallback_what(job),
        "purpose_tags": _fallback_tags(
            _fallback_name(job), _fallback_who(job), _fallback_what(job)
        ),
    }


def test_draft_persona_422_empty_job(config, tmp_path):
    client = _draft_client(config, tmp_path)

    response = client.post("/api/personas/draft", json={"job": ""})

    assert response.status_code == 422


def test_draft_persona_422_missing_job(config, tmp_path):
    client = _draft_client(config, tmp_path)

    response = client.post("/api/personas/draft", json={})

    assert response.status_code == 422


def test_draft_persona_422_job_too_long(config, tmp_path):
    client = _draft_client(config, tmp_path)

    response = client.post("/api/personas/draft", json={"job": "a" * 501})

    assert response.status_code == 422


def test_draft_persona_422_too_many_rules(config, tmp_path):
    client = _draft_client(config, tmp_path)

    response = client.post(
        "/api/personas/draft", json={**DRAFT_PAYLOAD, "rules": ["ok"] * 151}
    )

    assert response.status_code == 422


def test_draft_persona_200_with_150_rule_hints(config, tmp_path):
    """Upload/limits addendum: PersonaDraftRequest.rules mirrors
    PersonaFields.rules' cap move from 8 to 150 items."""
    client = _draft_client(config, tmp_path)

    response = client.post(
        "/api/personas/draft", json={**DRAFT_PAYLOAD, "rules": ["ok"] * 150}
    )

    assert response.status_code == 200


def test_draft_persona_422_bad_rule_item_length(config, tmp_path):
    client = _draft_client(config, tmp_path)

    response = client.post(
        "/api/personas/draft", json={**DRAFT_PAYLOAD, "rules": [""]}
    )

    assert response.status_code == 422


def test_draft_persona_invalid_payload_never_reaches_the_generator(config, tmp_path):
    fake_name = FakeName()
    client = _draft_client(config, tmp_path, name=fake_name)

    client.post("/api/personas/draft", json={"job": ""})

    assert fake_name.draft_calls == []


def test_draft_persona_result_feeds_create_persona_unchanged(config, tmp_path):
    """The addendum's core promise: the drafted fields land in the SAME v2
    fields, with create_persona's own contract untouched — a drafted
    response, posted straight to /api/personas (plus a team), creates
    cleanly."""
    fake_name = FakeName(
        draft={
            "name": "billing-buddy",
            "who_they_are": "a calm troubleshooter for billing questions",
            "what_they_do": "Helps customers resolve billing issues quickly.",
        }
    )
    client = _draft_client(config, tmp_path, name=fake_name)

    draft_response = client.post("/api/personas/draft", json=DRAFT_PAYLOAD)
    draft = draft_response.json()

    create_response = client.post("/api/personas", json={**draft, "team": "shared"})

    assert create_response.status_code == 201
    assert create_response.json()["name"] == "billing-buddy"
