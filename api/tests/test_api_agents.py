"""Tests for the public agent routes: create/list/detail/retry — the
one-click pipeline (`kobo_api.provisioner.PIPELINE`) exercised end-to-end
through the fake platform clients (`tests/fakes.py`), the same way a real
deploy would run it, just with every backend faked.

Same no-real-sleep discipline as test_steps.py/test_steps_wire.py: the
pipeline's own poll loops (OnboardStep/WireStep's gojira poll, BuildStep's CI
poll) must never actually block, so `kobo_api.steps._sleep` is
monkeypatched to a no-op here too (this file doesn't share test_steps.py's
module, so the autouse fixture is replicated rather than imported).

The deadline-poll helper below (`_poll_until`) DOES sleep for real, briefly,
between HTTP polls: that's the test's own wall-clock wait for the pipeline's
background thread to make progress, a different clock than the one the
`_sleep` patch silences.
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.clients import Clients, StorageNotFound
from kobo_api.config import Config
from kobo_api.iap import ASSERTION_HEADER, IAP_ISSUER
from kobo_api.kinds import KIND_CONNECTED
from kobo_api.main import create_app, get_session
from kobo_api.models import Agent, AgentStatus, Base
from kobo_api.provisioner import sweep_interrupted
from tests.fakes import (
    FakeAgent,
    FakeCabal,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
)

# fake_storage: reuses conftest.py's shared fixture (seeded with both
# catalogs — the two packaged personas + the sample "shared/word-games"
# skill) rather than redefining it here; only this module's `config`/
# `clients` need their own local fixtures (full platform-client wiring the
# conftest.py `client` fixture doesn't provide).

AGENT_NAME = "clio"
PERSONA = "helpful-generalist"
TEAM = "clio-team"

# Real-IAP-JWT idiom (created_by attribution tests, Task A2 below): same
# audience/kid constants as test_web_teach.py's own copy.
AUDIENCE = "test-audience"
KID = "test-key-1"

ALL_STEP_NAMES = [
    "repo", "avatar", "onboard", "access", "wire", "build", "deploy", "directory", "hello",
]

POLL_DEADLINE_S = 5
POLL_INTERVAL_S = 0.02


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """The assembled pipeline's steps poll gojira/CI; never let those loops
    sleep for real (see module docstring re: the deadline-poll's own sleep)."""
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
    )


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
def clients(fake_github, fake_gojira, fake_agent, fake_storage):
    return Clients(
        github=fake_github,
        gojira=fake_gojira,
        cloud=FakeCloud(),
        agent=fake_agent,
        kubectl=FakeKubectl(),
        storage=fake_storage,
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


def test_pipeline_order_joins_the_directory_before_saying_hello():
    """Whole-arc review finding C-1 / operator ruling 2026-08-19 (colleagues
    switchboard plan, Task 4b): agents compose LOCKED, so `HelloStep`'s
    first contact needs the agent already registered in the switchboard's
    directory before it can ride the relay — `DirectoryStep` must run
    strictly before `HelloStep`, and every other step keeps its prior
    relative order. Asserted directly against the real, assembled
    `PIPELINE` rather than only through an end-to-end run, so this pins the
    exact order regardless of how any single step's own outcome plays out.
    """
    from kobo_api.provisioner import PIPELINE

    assert [step.name for step in PIPELINE] == ALL_STEP_NAMES


def test_create_agent_returns_201_pending_then_provisions_to_ready(client):
    response = client.post(
        "/api/agents",
        json={"name": AGENT_NAME, "persona": PERSONA, "team": TEAM, "skills": [], "model": None},
    )

    assert response.status_code == 201
    assert response.json() == {"name": AGENT_NAME, "status": "pending"}

    body = _poll_until(client, AGENT_NAME, {"ready", "failed"})
    assert body["status"] == "ready"
    assert [s["name"] for s in body["steps"]] == ALL_STEP_NAMES
    steps_by_name = {s["name"]: s for s in body["steps"]}
    # AvatarStep is best-effort: this fixture's `clients` carries no image
    # backend at all, so it SKIPS with a one-line reason instead of blocking
    # READY -- every other step still runs and completes normally.
    assert steps_by_name["avatar"]["status"] == "skipped"
    assert steps_by_name["avatar"]["error"] == "picture generation is not available"
    assert all(s["status"] == "done" for s in body["steps"] if s["name"] != "avatar")
    assert all(s["error"] is None for s in body["steps"] if s["name"] != "avatar")
    assert body["first_reply"] == "hello yourself, human"


def test_create_agent_no_model_falls_back_to_default(client, config):
    client.post("/api/agents", json={"name": AGENT_NAME, "persona": PERSONA, "team": TEAM})

    body = _poll_until(client, AGENT_NAME, {"ready", "failed"})
    assert body["model"] == config.models[0].id


def test_create_agent_duplicate_name_returns_409(client):
    payload = {"name": AGENT_NAME, "persona": PERSONA, "team": TEAM}

    first = client.post("/api/agents", json=payload)
    assert first.status_code == 201

    second = client.post("/api/agents", json=payload)
    assert second.status_code == 409
    assert "error" in second.json()


def test_create_refuses_when_the_cluster_cannot_be_asked(session_factory, config, clients):
    """Guards ownership of the agent's namespace (issue #193): when the room
    cannot tell whether the namespace exists, it does not make the agent."""
    clients.kubectl.namespace_error = "Unable to connect to the server"
    client = TestClient(create_app(session_factory, config=config, clients=clients))

    response = client.post("/api/agents", json={"name": AGENT_NAME, "persona": PERSONA, "team": TEAM})

    assert response.status_code == 503
    session = session_factory()
    assert session.query(Agent).filter_by(name=AGENT_NAME).first() is None
    session.close()


def test_create_refuses_a_name_whose_namespace_already_exists(
    session_factory, config, clients
):
    """Guards ownership of the agent's namespace (issue #193): a namespace no
    row records belongs to something else, and the platform would adopt it."""
    clients.kubectl.namespaces[AGENT_NAME] = datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None)
    client = TestClient(create_app(session_factory, config=config, clients=clients))

    response = client.post(
        "/api/agents", json={"name": AGENT_NAME, "persona": PERSONA, "team": TEAM}
    )

    assert response.status_code == 409
    session = session_factory()
    assert session.query(Agent).filter_by(name=AGENT_NAME).first() is None
    session.close()


def test_create_agent_name_toctou_maps_integrity_error_to_409(
    session_factory, config, clients
):
    """The pre-check (`SELECT ... WHERE name = ?`) is read-then-act, not a
    lock: a request that loses a real race sees the pre-check miss and only
    discovers the collision when its own insert hits the UNIQUE constraint.
    Force exactly that ordering — seed the row directly (as a concurrent
    winner would), then override the session so this request's pre-check
    still reports a miss — and assert the resulting IntegrityError is mapped
    to the same 409 the pre-check itself would return, not a raw 500.
    """
    seed_session = session_factory()
    seed_session.add(
        Agent(
            name=AGENT_NAME,
            persona=PERSONA,
            model_id=config.models[0].id,
            status=AgentStatus.PENDING,
        )
    )
    seed_session.commit()
    seed_session.close()

    class _MissingPrecheckSession:
        """Proxies a real Session but makes `.query(...).filter_by(...).first()`
        always report a miss — the TOCTOU window this test forces — while
        everything else (crucially `add`/`commit`) hits the real session, so
        the real UNIQUE constraint still fires on commit.
        """

        def __init__(self, real):
            self._real = real

        def query(self, *args, **kwargs):
            class _MissingQuery:
                def filter_by(self, **kw):
                    return self

                def first(self):
                    return None

            return _MissingQuery()

        def __getattr__(self, item):
            return getattr(self._real, item)

    def _override_get_session():
        real = session_factory()
        try:
            yield _MissingPrecheckSession(real)
        finally:
            real.close()

    app = create_app(session_factory, config=config, clients=clients)
    app.dependency_overrides[get_session] = _override_get_session
    race_client = TestClient(app)

    response = race_client.post(
        "/api/agents", json={"name": AGENT_NAME, "persona": PERSONA, "team": TEAM}
    )

    assert response.status_code == 409
    assert response.json() == {"error": f"name taken: {AGENT_NAME!r}"}


def test_create_agent_unknown_persona_returns_422(client):
    response = client.post(
        "/api/agents", json={"name": AGENT_NAME, "persona": "ghost-persona", "team": TEAM}
    )

    assert response.status_code == 422
    assert "error" in response.json()


def test_create_agent_archived_persona_returns_honest_422(client, fake_storage):
    # `fake_storage`: this module's conftest-shared FakeStorage fixture
    # (seeded with the two packaged personas + the sample skill). Append an
    # archived entry to the index, refresh the catalog so ingest drops it,
    # then pick it -- the honest 422 arm must fire, not the plain "unknown
    # persona" one, even though ingest makes the two indistinguishable in
    # the FTS table itself.
    index = json.loads(fake_storage.read_bytes("personas-bucket", "index.json"))
    index["entries"].append(
        {"id": "shelved-soul", "name": "shelved-soul", "description": "d",
         "team": "shared", "tags": [], "builtin": False, "archived": True}
    )
    fake_storage.write_bytes("personas-bucket", "index.json", json.dumps(index).encode())
    # Minor 7 fix (hardening wave review): no explicit refresh here -- the
    # `client` fixture is fresh per test (catalog_loaded_at starts None), so
    # POST /api/agents's own `_maybe_refresh_catalogs` cold-start ingest
    # already picks up the entry written just above. An extra refresh call
    # here was non-load-bearing and its status went unchecked.
    response = client.post(
        "/api/agents",
        json={"name": AGENT_NAME, "persona": "shelved-soul", "team": TEAM},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "persona 'shelved-soul' is archived"


def test_create_agent_unknown_skill_returns_422(client):
    response = client.post(
        "/api/agents",
        json={
            "name": AGENT_NAME,
            "persona": PERSONA,
            "team": TEAM,
            "skills": [{"id": "ghost-skill", "team": "shared"}],
        },
    )

    assert response.status_code == 422
    assert "error" in response.json()


def test_create_agent_unknown_model_returns_422(client):
    response = client.post(
        "/api/agents",
        json={"name": AGENT_NAME, "persona": PERSONA, "team": TEAM, "model": "not-a-real-model"},
    )

    assert response.status_code == 422
    assert "error" in response.json()


def test_create_agent_invalid_name_returns_422(client):
    response = client.post(
        "/api/agents", json={"name": "Not Valid!", "persona": PERSONA, "team": TEAM}
    )

    assert response.status_code == 422
    assert "error" in response.json()


def test_create_agent_invalid_name_never_reaches_the_pipeline(client, fake_github):
    """A 422 must reject before touching any platform client."""
    client.post("/api/agents", json={"name": "Not Valid!", "persona": PERSONA, "team": TEAM})

    assert fake_github.calls == []


@pytest.mark.parametrize(
    "name,expected_reason",
    [
        ("a" * 25, "too long (max 24)"),
        ("agent-", "must not end with a hyphen"),
        ("cortex", "reserved name"),
    ],
)
def test_create_agent_name_rule_violations_return_422_with_distinct_reason(
    client, name, expected_reason
):
    """Same distinct reasons as the availability route (`validate_name`),
    surfaced in create's 422 error message rather than a bare "invalid name"."""
    response = client.post(
        "/api/agents", json={"name": name, "persona": PERSONA, "team": TEAM}
    )

    assert response.status_code == 422
    assert expected_reason in response.json()["error"]


def test_create_agent_bad_team_slug_returns_422(client):
    response = client.post(
        "/api/agents", json={"name": AGENT_NAME, "persona": PERSONA, "team": "Not Valid!"}
    )

    assert response.status_code == 422
    assert response.json() == {"error": "that doesn't look like a team name"}


def test_create_agent_omitted_team_defaults_to_shared(client):
    """Team-optional (Persona v3 / teamless create addendum): the create
    modal drops the Team station entirely, so the API must accept a
    payload with no `team` key at all and record it as "shared" — the same
    value nearly everything in the catalogs already carries."""
    response = client.post("/api/agents", json={"name": AGENT_NAME, "persona": PERSONA})

    assert response.status_code == 201

    detail = client.get(f"/api/agents/{AGENT_NAME}")
    assert detail.status_code == 200
    assert detail.json()["team"] == "shared"


def test_create_agent_explicit_team_still_honored(client):
    response = client.post(
        "/api/agents", json={"name": AGENT_NAME, "persona": PERSONA, "team": TEAM}
    )

    assert response.status_code == 201

    detail = client.get(f"/api/agents/{AGENT_NAME}")
    assert detail.status_code == 200
    assert detail.json()["team"] == TEAM


def test_create_agent_catalogs_not_configured_returns_422(session_factory, tmp_path):
    """Controller decision #2: an unwired org (no catalog bucket named at
    all) 422s with the SAME copy the search/refresh routes' 503 uses — a
    setup problem, not a bad request — and this must happen before any Agent
    row is written."""
    unwired_config = Config(workdir=tmp_path / "work")
    client = TestClient(create_app(session_factory, config=unwired_config))

    response = client.post(
        "/api/agents", json={"name": AGENT_NAME, "persona": PERSONA, "team": TEAM}
    )

    assert response.status_code == 422
    assert response.json() == {"error": "catalogs aren't set up yet"}

    session = session_factory()
    assert session.query(Agent).filter_by(name=AGENT_NAME).first() is None
    session.close()


def test_create_agent_persists_team_and_exposes_it_in_detail_and_list(client):
    response = client.post(
        "/api/agents",
        json={"name": AGENT_NAME, "persona": PERSONA, "team": TEAM, "skills": [], "model": None},
    )

    assert response.status_code == 201

    detail = client.get(f"/api/agents/{AGENT_NAME}").json()
    assert detail["team"] == TEAM

    listing = client.get("/api/agents").json()["agents"]
    assert listing[0]["team"] == TEAM


def test_get_agent_team_is_null_for_pre_existing_rows(session_factory, config, clients):
    """Detail/list responses gain "team", but a row that predates this
    column (or was simply never given one) must render null, not 500."""
    client = TestClient(create_app(session_factory, config=config, clients=clients))
    session = session_factory()
    session.add(
        Agent(
            name=AGENT_NAME,
            persona=PERSONA,
            model_id=config.models[0].id,
            status=AgentStatus.READY,
        )
    )
    session.commit()
    session.close()

    detail = client.get(f"/api/agents/{AGENT_NAME}").json()
    assert detail["team"] is None

    listing = client.get("/api/agents").json()["agents"]
    assert listing[0]["team"] is None


# --- purpose_tags / capability (persona-metadata plan, Task 2) ---------------------


def test_create_agent_purpose_tags_normalized_and_stored(client):
    response = client.post(
        "/api/agents",
        json={
            "name": AGENT_NAME,
            "persona": PERSONA,
            "team": TEAM,
            "purpose_tags": ["Billing", "billing", " Support "],
        },
    )

    assert response.status_code == 201

    detail = client.get(f"/api/agents/{AGENT_NAME}").json()
    assert detail["purpose_tags"] == ["billing", "support"]


def test_create_agent_purpose_tags_defaults_to_empty_list(client):
    response = client.post(
        "/api/agents", json={"name": AGENT_NAME, "persona": PERSONA, "team": TEAM}
    )

    assert response.status_code == 201

    detail = client.get(f"/api/agents/{AGENT_NAME}").json()
    assert detail["purpose_tags"] == []


def test_create_agent_purpose_tags_over_cap_rejected(client):
    tags = [f"tag{i}" for i in range(9)]

    response = client.post(
        "/api/agents",
        json={"name": AGENT_NAME, "persona": PERSONA, "team": TEAM, "purpose_tags": tags},
    )

    assert response.status_code == 422
    assert response.json() == {"error": "Eight tags at most."}


def test_create_agent_purpose_tags_dedupe_brings_under_cap_accepted(client):
    # 9 repeats of the SAME tag de-dupe down to 1 — the cap must be
    # checked AFTER normalization, not against the raw submitted count.
    tags = ["billing"] * 9

    response = client.post(
        "/api/agents",
        json={"name": AGENT_NAME, "persona": PERSONA, "team": TEAM, "purpose_tags": tags},
    )

    assert response.status_code == 201

    detail = client.get(f"/api/agents/{AGENT_NAME}").json()
    assert detail["purpose_tags"] == ["billing"]


def test_get_agent_capability_is_null_until_earned(
    session_factory, config, clients, monkeypatch
):
    """`run_pipeline` is replaced with a no-op (the retry-race idiom below):
    the real daemon pipeline distills a capability shortly after the 201, so
    GETting the live app is a race this test lost on a loaded CI runner
    (2026-09-16: the distill won and `capability` came back as the persona's
    first sentence). The contract under test is only that CREATE itself
    births the row with no capability. `clients` (this FILE's fixture, with
    the persona-seeded FakeStorage) is required — without it the persona
    catalog is empty and the POST 422s before capability is ever in play."""
    import kobo_api.main as main_module

    monkeypatch.setattr(main_module, "run_pipeline", lambda *args, **kwargs: None)
    client = TestClient(create_app(session_factory, config=config, clients=clients))

    response = client.post(
        "/api/agents", json={"name": AGENT_NAME, "persona": PERSONA, "team": TEAM}
    )

    assert response.status_code == 201, response.text

    detail = client.get(f"/api/agents/{AGENT_NAME}").json()
    assert detail["capability"] is None
    assert "capability" in detail


# --- created_by / group_name (access control, Task A2) ----------------------
#
# The identity-attribution slice reuses test_web_teach.py's real-JWT idiom
# (a locally generated ES256 keypair signs the assertion, verified through
# the actual `IapMiddleware`/`IapVerifier`, not a mock) rather than the
# module's own plain `client` fixture, since that fixture's `config` never
# sets `iap_audience` -- no IapMiddleware is registered at all, so
# `request.state.iap_email` is never set. Helpers are replicated locally
# rather than imported, same no-shared-module discipline as test_web_teach.py
# itself relative to test_steps.py.


def _generate_jwk(kid: str = KID):
    private_key = ec.generate_private_key(ec.SECP256R1())
    jwk = jwt.algorithms.ECAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk["kid"] = kid
    jwk["use"] = "sig"
    jwk["alg"] = "ES256"
    return private_key, jwk


def _sign(private_key, *, email: str | None, kid: str = KID) -> str:
    now = int(time.time())
    claims = {
        "aud": AUDIENCE,
        "iss": IAP_ISSUER,
        "iat": now,
        "exp": now + 3600,
        "sub": "accounts.google.com:1234567890",
    }
    if email is not None:
        claims["email"] = email
    return jwt.encode(claims, private_key, algorithm="ES256", headers={"kid": kid})


def _fetcher(jwk):
    def fetch():
        return {"keys": [jwk]}

    return fetch


def test_create_agent_records_created_by_from_iap_email(session_factory, config, clients):
    private_key, jwk = _generate_jwk()
    iap_config = config.model_copy(update={"iap_audience": AUDIENCE})
    iap_client = TestClient(
        create_app(
            session_factory, config=iap_config, clients=clients, iap_jwks_fetcher=_fetcher(jwk)
        )
    )
    token = _sign(private_key, email="creator@example.com")

    response = iap_client.post(
        "/api/agents",
        json={"name": AGENT_NAME, "persona": PERSONA, "team": TEAM},
        headers={ASSERTION_HEADER: token},
    )

    assert response.status_code == 201

    session = session_factory()
    row = session.query(Agent).filter_by(name=AGENT_NAME).first()
    session.close()
    assert row.created_by == "creator@example.com"


def test_create_agent_without_iap_leaves_created_by_none(client, session_factory):
    # config.iap_audience is "" by default on the module's `client` fixture
    # (no IapMiddleware registered at all) -- request.state.iap_email is
    # never set, and the route must fall back to `None` rather than raising
    # on a missing attribute.
    response = client.post(
        "/api/agents", json={"name": AGENT_NAME, "persona": PERSONA, "team": TEAM}
    )

    assert response.status_code == 201

    session = session_factory()
    row = session.query(Agent).filter_by(name=AGENT_NAME).first()
    session.close()
    assert row.created_by is None


def test_create_agent_iap_on_but_no_email_claim_leaves_created_by_none(
    session_factory, config, clients
):
    private_key, jwk = _generate_jwk()
    iap_config = config.model_copy(update={"iap_audience": AUDIENCE})
    iap_client = TestClient(
        create_app(
            session_factory, config=iap_config, clients=clients, iap_jwks_fetcher=_fetcher(jwk)
        )
    )
    token = _sign(private_key, email=None)

    response = iap_client.post(
        "/api/agents",
        json={"name": AGENT_NAME, "persona": PERSONA, "team": TEAM},
        headers={ASSERTION_HEADER: token},
    )

    assert response.status_code == 201

    session = session_factory()
    row = session.query(Agent).filter_by(name=AGENT_NAME).first()
    session.close()
    assert row.created_by is None


def test_create_agent_stores_group_name_verbatim_when_provided(client, session_factory):
    response = client.post(
        "/api/agents",
        json={
            "name": AGENT_NAME,
            "persona": PERSONA,
            "team": TEAM,
            "group_name": "payments-eng",
        },
    )

    assert response.status_code == 201

    session = session_factory()
    row = session.query(Agent).filter_by(name=AGENT_NAME).first()
    session.close()
    assert row.group_name == "payments-eng"


def test_create_agent_omitted_group_name_defaults_to_none(client, session_factory):
    response = client.post(
        "/api/agents", json={"name": AGENT_NAME, "persona": PERSONA, "team": TEAM}
    )

    assert response.status_code == 201

    session = session_factory()
    row = session.query(Agent).filter_by(name=AGENT_NAME).first()
    session.close()
    assert row.group_name is None


def test_list_agents_tolerates_null_created_by_and_group_name_on_pre_existing_rows(
    client, session_factory, config
):
    """A row that predates the created_by/group_name columns (or was simply
    never given values) must read back as None, not 500 or raise -- same
    story as `test_get_agent_team_is_null_for_pre_existing_rows` above, just
    for this pair of columns instead."""
    session = session_factory()
    session.add(
        Agent(
            name=AGENT_NAME,
            persona=PERSONA,
            model_id=config.models[0].id,
            status=AgentStatus.READY,
        )
    )
    session.commit()
    session.close()

    listing = client.get("/api/agents").json()["agents"]
    assert len(listing) == 1
    assert listing[0]["created_by"] is None
    assert listing[0]["group_name"] is None

    detail = client.get(f"/api/agents/{AGENT_NAME}")
    assert detail.status_code == 200
    assert detail.json()["created_by"] is None
    assert detail.json()["group_name"] is None

    session = session_factory()
    row = session.query(Agent).filter_by(name=AGENT_NAME).first()
    session.close()
    assert row.created_by is None
    assert row.group_name is None


def test_list_agents_exposes_created_by_and_group_name(client, session_factory, config):
    """Access-control Task A3: the owner-or-operator gate and the card's
    "Made by <email>" line both need `created_by` (plus `group_name`, for
    Task B2's visibility filter) actually on the wire -- A2 stored both
    columns but deliberately left them off `GET /api/agents`."""
    session = session_factory()
    session.add(
        Agent(
            name=AGENT_NAME,
            persona=PERSONA,
            model_id=config.models[0].id,
            status=AgentStatus.READY,
            created_by="owner@example.com",
            group_name="payments-eng",
        )
    )
    session.commit()
    session.close()

    row = client.get("/api/agents").json()["agents"][0]
    assert row["created_by"] == "owner@example.com"
    assert row["group_name"] == "payments-eng"


def test_create_agent_pipeline_materializes_staged_skill_and_persona_body(client, fake_github):
    """Pipeline-to-READY, end to end: a picked skill is staged from the
    skills bucket and lands in the pushed repo's bundle, and the persona
    body is `{name}`-substituted with the agent's own name — asserted
    directly against FakeGitHub's recorded push tree (`created_from_dir`),
    the same idiom test_steps.py's RepoStep tests use.
    """
    response = client.post(
        "/api/agents",
        json={
            "name": AGENT_NAME,
            "persona": PERSONA,
            "team": TEAM,
            "skills": [{"id": "word-games", "team": "shared"}],
        },
    )
    assert response.status_code == 201

    body = _poll_until(client, AGENT_NAME, {"ready", "failed"})
    assert body["status"] == "ready"
    assert body["skills"] == [{"name": "word-games", "team": "shared", "sha": "seedsha"}]

    assert len(fake_github.created_from_dir) == 1
    _repo, pushed_src = fake_github.created_from_dir[0]

    skill_md = (pushed_src / "bundle" / "skills" / "word-games" / "SKILL.md").read_text()
    assert "word-games" in skill_md

    persona_md = (pushed_src / "bundle" / "persona.md").read_text()
    assert AGENT_NAME in persona_md
    assert "{name}" not in persona_md


# --- Roster refresh lane (fast-lane + discovery arc, Task 6) -----------------
#
# The create-READY flip end to end: once the new agent reaches READY, every
# OTHER agent that can see it (same non-shared group, or an accepted invite)
# gets a redelivered `colleagues.md` over the fast lane. The negative half is
# the one that matters (Global Constraints): a room with agents in two
# different groups must never cross-deliver between them.


def _client_with_cabal_and_storage(
    session_factory, config, fake_github, fake_gojira, fake_agent, fake_storage, cabal
):
    return TestClient(
        create_app(
            session_factory,
            config=config,
            clients=Clients(
                github=fake_github,
                gojira=fake_gojira,
                cloud=FakeCloud(),
                agent=fake_agent,
                kubectl=FakeKubectl(),
                storage=fake_storage,
                cabal=cabal,
            ),
        )
    )


def _colleagues_deliveries(fake_cabal):
    """Every relay call in `fake_cabal.relay_calls` that carries a
    `colleagues.md` knowledge system task, keyed by the target agent name
    it was relayed to."""
    return [
        call["name"]
        for call in fake_cabal.relay_calls
        if call["task_envelope"].get("metadata", {}).get("filename") == "colleagues.md"
    ]


def test_create_agent_ready_flip_redelivers_roster_without_cross_group_delivery(
    session_factory, config, fake_github, fake_gojira, fake_agent, fake_storage, monkeypatch
):
    """Global Constraints' load-bearing negative test, exercised through the
    real pipeline end to end: a room with agents in two groups must not
    cross-deliver. A same-group READY agent and an unrelated-group READY
    agent both pre-exist; once the newly created agent reaches READY, only
    the same-group agent's colleagues.md is redelivered."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_cabal_config(config)
    fake_cabal = FakeCabal(
        relay_result={"status": {"state": "completed", "message": {"parts": [{"text": "ok"}]}}}
    )
    client = _client_with_cabal_and_storage(
        session_factory, cfg, fake_github, fake_gojira, fake_agent, fake_storage, fake_cabal
    )
    session = session_factory()
    session.add(
        Agent(
            name="same-group",
            persona=PERSONA,
            model_id="vertex_ai/claude-sonnet-4-5",
            status=AgentStatus.READY,
            group_name="acme-acl",
        )
    )
    session.add(
        Agent(
            name="other-group",
            persona=PERSONA,
            model_id="vertex_ai/claude-sonnet-4-5",
            status=AgentStatus.READY,
            group_name="orbit-acl",
        )
    )
    session.commit()
    session.close()

    response = client.post(
        "/api/agents",
        json={"name": AGENT_NAME, "persona": PERSONA, "group_name": "acme-acl"},
    )
    assert response.status_code == 201

    body = _poll_until(client, AGENT_NAME, {"ready", "failed"})
    assert body["status"] == "ready"

    delivered_to = _colleagues_deliveries(fake_cabal)
    assert "same-group" in delivered_to
    assert "other-group" not in delivered_to


def test_create_agent_ready_flip_does_not_redeliver_when_switchboard_disabled(
    session_factory, config, fake_github, fake_gojira, fake_agent, fake_storage
):
    """Default config/clients (no cabal org keys, no cabal client): the
    refresh is a no-op, same as every other fast-lane caller."""
    fake_cabal = FakeCabal()
    client = _client_with_cabal_and_storage(
        session_factory, config, fake_github, fake_gojira, fake_agent, fake_storage, fake_cabal
    )
    session = session_factory()
    session.add(
        Agent(
            name="same-group",
            persona=PERSONA,
            model_id="vertex_ai/claude-sonnet-4-5",
            status=AgentStatus.READY,
            group_name="acme-acl",
        )
    )
    session.commit()
    session.close()

    response = client.post(
        "/api/agents",
        json={"name": AGENT_NAME, "persona": PERSONA, "group_name": "acme-acl"},
    )
    assert response.status_code == 201

    body = _poll_until(client, AGENT_NAME, {"ready", "failed"})
    assert body["status"] == "ready"
    assert fake_cabal.relay_calls == []


# --- GET /api/agents ---------------------------------------------------------------


def test_list_agents_returns_name_status_model_created_at(client, config):
    client.post(
        "/api/agents",
        json={"name": AGENT_NAME, "persona": PERSONA, "team": TEAM, "model": None},
    )

    response = client.get("/api/agents")

    assert response.status_code == 200
    agents = response.json()["agents"]
    assert len(agents) == 1
    row = agents[0]
    assert row["name"] == AGENT_NAME
    assert row["status"] in {"pending", "provisioning", "ready"}
    assert row["model"] == config.models[0].id
    assert isinstance(row["created_at"], str) and row["created_at"]
    # The card's description line reads these off the list, not a per-card fetch.
    assert row["persona"] == PERSONA
    assert "capability" in row


def test_list_agents_empty_when_none_created(client):
    response = client.get("/api/agents")

    assert response.status_code == 200
    assert response.json() == {"agents": [], "groups_unavailable": False}


def test_list_agents_marks_an_agent_the_room_composed(client):
    """`kind` rides the LIST as well as the detail, the same reason
    `archetype` does: the dashboard card carries the marker, and a
    per-agent fetch to learn it would be one round trip per card."""
    client.post("/api/agents", json={"name": AGENT_NAME, "persona": PERSONA, "team": TEAM})

    response = client.get("/api/agents")

    assert response.json()["agents"][0]["kind"] == "composed"


# --- GET /api/agents/{name} --------------------------------------------------------


def test_get_agent_unknown_name_returns_404(client):
    response = client.get("/api/agents/does-not-exist")

    assert response.status_code == 404
    assert "error" in response.json()


# --- Failure + retry ---------------------------------------------------------------


def _failing_clients(fake_github, fake_agent, failing_gojira, fake_storage):
    return Clients(
        github=fake_github,
        gojira=failing_gojira,
        cloud=FakeCloud(),
        agent=fake_agent,
        kubectl=FakeKubectl(),
        storage=fake_storage,
    )


def _seed_agent(session_factory, config, *, status=AgentStatus.READY, model_id=None):
    """Insert an Agent row directly, bypassing the pipeline — chat/label tests
    only care about the row's status/model_id, not how it got there."""
    session = session_factory()
    session.add(
        Agent(
            name=AGENT_NAME,
            persona=PERSONA,
            model_id=model_id if model_id is not None else config.models[0].id,
            status=status,
        )
    )
    session.commit()
    session.close()


def _client_with_agent(
    session_factory, config, fake_github, fake_gojira, fake_agent, fake_storage=None
):
    """Same shape as `_failing_clients`, but for chat tests that need their
    own specifically-configured FakeAgent (scripted replies/converse_error)
    rather than the module `fake_agent` fixture's fixed canned reply.

    `fake_storage` defaults to None: every caller here seeds the agent
    directly (bypassing create_agent's catalog validation entirely), so none
    of them need a wired catalog — unlike `_failing_clients`, whose callers
    DO go through create_agent.
    """
    return TestClient(
        create_app(
            session_factory,
            config=config,
            clients=Clients(
                github=fake_github,
                gojira=fake_gojira,
                cloud=FakeCloud(),
                agent=fake_agent,
                kubectl=FakeKubectl(),
                storage=fake_storage,
            ),
        )
    )


def test_create_agent_failure_surfaces_failing_step_error_and_aborts_the_rest(
    session_factory, config, fake_github, fake_agent, fake_storage
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
            clients=_failing_clients(fake_github, fake_agent, failing_gojira, fake_storage),
        )
    )

    response = client.post("/api/agents", json={"name": AGENT_NAME, "persona": PERSONA, "team": TEAM})
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


def test_create_agent_hello_step_failure_leaves_agent_failed_with_no_reply(
    session_factory, config, fake_github, fake_gojira, fake_storage
):
    """HelloStep is now the pipeline's LAST step (Task 4b's reorder moved
    DirectoryStep ahead of it) — a failure there should look like any other
    step failure (agent FAILED, that step's row FAILED with the error)
    rather than a special case, and `first_reply` must stay None since
    `hello()` never returned successfully. Nothing runs after hello now, so
    there's no later step left to mark aborted.
    """
    failing_agent = FakeAgent(hello_error="a2a hello failed (500): boom")
    client = TestClient(
        create_app(
            session_factory,
            config=config,
            clients=Clients(
                github=fake_github,
                gojira=fake_gojira,
                cloud=FakeCloud(),
                agent=failing_agent,
                kubectl=FakeKubectl(),
                storage=fake_storage,
            ),
        )
    )

    response = client.post("/api/agents", json={"name": AGENT_NAME, "persona": PERSONA, "team": TEAM})
    assert response.status_code == 201

    body = _poll_until(client, AGENT_NAME, {"ready", "failed"})
    assert body["status"] == "failed"
    assert body["first_reply"] is None

    steps_by_name = {s["name"]: s for s in body["steps"]}
    # AvatarStep SKIPPED (best-effort, no image backend in this fixture)
    # rather than done -- everything else before hello (directory included,
    # it now runs first, as a no-op since this fixture's config carries no
    # switchboard org keys) completed fine.
    assert steps_by_name["avatar"]["status"] == "skipped"
    assert all(
        steps_by_name[n]["status"] == "done"
        for n in ALL_STEP_NAMES
        if n not in ("hello", "avatar")
    )
    assert steps_by_name["hello"]["status"] == "failed"
    assert "boom" in steps_by_name["hello"]["error"]


def test_retry_when_not_failed_returns_409(client):
    client.post("/api/agents", json={"name": AGENT_NAME, "persona": PERSONA, "team": TEAM})
    _poll_until(client, AGENT_NAME, {"ready", "failed"})

    response = client.post(f"/api/agents/{AGENT_NAME}/retry")

    assert response.status_code == 409
    assert "error" in response.json()


def test_retry_unknown_agent_returns_404(client):
    response = client.post("/api/agents/does-not-exist/retry")

    assert response.status_code == 404
    assert "error" in response.json()


def test_retry_after_fixing_failure_reaches_ready_without_rerunning_done_steps(
    session_factory, config, fake_github, fake_agent, fake_storage
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
            clients=_failing_clients(fake_github, fake_agent, failing_gojira, fake_storage),
        )
    )

    client.post("/api/agents", json={"name": AGENT_NAME, "persona": PERSONA, "team": TEAM})
    first_body = _poll_until(client, AGENT_NAME, {"ready", "failed"})
    assert first_body["status"] == "failed"

    # RepoStep is the only step that actually creates a repo before onboard
    # fails (AvatarStep also runs in between, and immediately SKIPS, since
    # this fixture's clients carry no image backend, but that's not a repo
    # creation): the counting fake proof that retry doesn't re-run RepoStep.
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
    # AvatarStep SKIPPED (best-effort, no image backend in this fixture)
    # back on the first run, before onboard ever failed -- the resume-skip
    # rule (DONE/SKIPPED steps never re-run) keeps it skipped on retry too.
    steps_by_name = {s["name"]: s for s in second_body["steps"]}
    assert steps_by_name["avatar"]["status"] == "skipped"
    assert all(
        s["status"] == "done" for s in second_body["steps"] if s["name"] != "avatar"
    )
    assert second_body["first_reply"] == fake_agent._reply

    # Still only the one create_repo_from_dir call across both runs: RepoStep's
    # idempotent-resume path (repo_exists + matching recorded repo_full_name)
    # took over instead of recomposing/recreating.
    create_calls_after_retry = [
        c for c in fake_github.calls if c.startswith("create_repo_from_dir")
    ]
    assert len(create_calls_after_retry) == 1


def test_retry_agent_race_second_call_409s_and_pipeline_fires_once(
    session_factory, config, clients, monkeypatch
):
    """The FAILED gate (`if agent.status != AgentStatus.FAILED`) is
    read-then-act: without a synchronous status flip before `run_pipeline`,
    two retries fired back-to-back both read FAILED and both pass the gate,
    each launching its own pipeline thread against the same AgentStep rows.
    Replace `run_pipeline` with a recording no-op (so this test doesn't
    depend on real pipeline timing) and fire two retries immediately after
    each other: the first should flip the agent to PROVISIONING (closing the
    gate) and fire the pipeline exactly once; the second must see
    PROVISIONING and 409 without ever touching the pipeline again.
    """
    import kobo_api.main as main_module

    seed_session = session_factory()
    seed_session.add(
        Agent(
            name=AGENT_NAME,
            persona=PERSONA,
            model_id=config.models[0].id,
            status=AgentStatus.FAILED,
            error="boom",
        )
    )
    seed_session.commit()
    seed_session.close()

    recorded_agent_ids: list[int] = []

    def _recording_run_pipeline(session_factory, agent_id, *, config, clients, steps):
        recorded_agent_ids.append(agent_id)

    monkeypatch.setattr(main_module, "run_pipeline", _recording_run_pipeline)

    race_client = TestClient(create_app(session_factory, config=config, clients=clients))

    first = race_client.post(f"/api/agents/{AGENT_NAME}/retry")
    second = race_client.post(f"/api/agents/{AGENT_NAME}/retry")

    assert first.status_code == 202
    assert first.json()["status"] == "provisioning"
    assert second.status_code == 409
    assert "error" in second.json()
    assert len(recorded_agent_ids) == 1


# --- The heartbeat stamp on the way in flight (two-replica safety) -----------
#
# `sweep_interrupted` (kobo_api.provisioner) fails an in-flight row whose
# heartbeat is stale. A row is in flight the moment the ROUTE commits it,
# which is before the pipeline thread exists -- so the route has to stamp
# the heartbeat in that same commit. A retry is the sharp case: the row
# already carries the PREVIOUS run's heartbeat, which is long past the
# cutoff, and the first beat of the new run is a write that is allowed to
# fail (it is logged and skipped, never raised). Without the stamp, one
# failed first beat leaves a live retry sweepable on the very next tick.


def _sweeping_pipeline_is_replaced(monkeypatch):
    """No pipeline at all: no thread, and therefore no heartbeat. What is
    left is exactly what the ROUTE committed, which is what these tests
    are about."""
    import kobo_api.main as main_module

    monkeypatch.setattr(main_module, "run_pipeline", lambda *args, **kwargs: None)


def test_retry_stamps_the_heartbeat_so_a_live_retry_is_not_swept(
    session_factory, config, clients, monkeypatch
):
    _sweeping_pipeline_is_replaced(monkeypatch)
    seed_session = session_factory()
    seed_session.add(
        Agent(
            name=AGENT_NAME,
            persona=PERSONA,
            model_id=config.models[0].id,
            status=AgentStatus.FAILED,
            error="boom",
            # A row that has been sitting failed for days: its heartbeat is
            # the last run's, far outside any cutoff.
            created_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=3),
            run_heartbeat_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=3),
        )
    )
    seed_session.commit()
    seed_session.close()

    retry_client = TestClient(create_app(session_factory, config=config, clients=clients))
    assert retry_client.post(f"/api/agents/{AGENT_NAME}/retry").status_code == 202

    # Another replica sweeps right now, before the new run's first beat.
    assert sweep_interrupted(session_factory) == 0

    session = session_factory()
    agent = session.query(Agent).filter_by(name=AGENT_NAME).one()
    assert agent.status == AgentStatus.PROVISIONING
    assert agent.error is None
    session.close()


def test_retry_resumes_an_agent_parked_on_its_setup_pull_request(
    session_factory, config, clients, monkeypatch
):
    """WAITING is a resume by hand: the person who just merged the setup PR
    does not have to wait for the sweep. Same 202, same primitive."""
    _sweeping_pipeline_is_replaced(monkeypatch)
    seed_session = session_factory()
    seed_session.add(
        Agent(
            name=AGENT_NAME,
            persona=PERSONA,
            model_id=config.models[0].id,
            kind=KIND_CONNECTED,
            status=AgentStatus.WAITING,
            error="Waiting for someone on your team to review and merge the setup pull request: https://github.com/o/r/pull/3",
            gojira_app_id=4,
        )
    )
    seed_session.commit()
    seed_session.close()

    resume_client = TestClient(create_app(session_factory, config=config, clients=clients))
    assert resume_client.post(f"/api/agents/{AGENT_NAME}/retry").status_code == 202

    session = session_factory()
    agent = session.query(Agent).filter_by(name=AGENT_NAME).one()
    assert agent.status == AgentStatus.PROVISIONING
    assert agent.error is None
    session.close()


def test_create_stamps_the_heartbeat_so_a_new_agent_is_not_swept(
    session_factory, config, clients, monkeypatch
):
    # Create gets a grace period from its own fresh created_at, but only
    # for as long as the cutoff -- and it costs nothing to be explicit
    # rather than to lean on a NULL branch meant for legacy rows.
    _sweeping_pipeline_is_replaced(monkeypatch)
    create_client = TestClient(create_app(session_factory, config=config, clients=clients))

    response = create_client.post(
        "/api/agents", json={"name": AGENT_NAME, "persona": PERSONA, "team": TEAM}
    )
    assert response.status_code == 201, response.text

    session = session_factory()
    agent = session.query(Agent).filter_by(name=AGENT_NAME).one()
    assert agent.run_heartbeat_at is not None
    # Pretend the row itself is old enough to be swept on the NULL branch:
    # the stamp, not created_at, is what keeps it alive.
    agent.created_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=3)
    session.commit()
    session.close()

    assert sweep_interrupted(session_factory) == 0


# --- Launch guard (run_pipeline/Thread.start() failing post-commit) ---------------


def test_create_agent_launch_failure_returns_503_and_agent_lands_failed(
    session_factory, config, clients, monkeypatch
):
    """A `run_pipeline` launch failure (e.g. `Thread.start()` itself raising)
    happens after the agent row is already committed as PENDING; without a
    guard the agent would be wedged there identically to the restart-mid-
    provision case `sweep_interrupted` covers. Force that failure directly
    (no real thread-exhaustion scenario needed) and assert the route reports
    a caller-actionable 503 instead of a raw 500, and the agent lands FAILED
    (i.e. retryable) rather than stuck in PENDING.
    """
    import kobo_api.main as main_module

    def _raising_run_pipeline(session_factory, agent_id, *, config, clients, steps):
        raise RuntimeError("no more threads")

    monkeypatch.setattr(main_module, "run_pipeline", _raising_run_pipeline)

    race_client = TestClient(create_app(session_factory, config=config, clients=clients))

    response = race_client.post(
        "/api/agents", json={"name": AGENT_NAME, "persona": PERSONA, "team": TEAM}
    )

    assert response.status_code == 503
    assert response.json() == {"error": "failed to start provisioning: no more threads"}

    session = session_factory()
    agent = session.query(Agent).filter_by(name=AGENT_NAME).first()
    assert agent.status == AgentStatus.FAILED
    assert agent.error == "failed to start provisioning: no more threads"
    session.close()


# --- POST /api/agents/{name}/chat --------------------------------------------------


def test_chat_happy_path_returns_reply_and_forwards_exact_messages(
    client, session_factory, config, fake_agent
):
    _seed_agent(session_factory, config)
    messages = [
        {"role": "user", "text": "hi"},
        {"role": "agent", "text": "hello yourself, human"},
        {"role": "user", "text": "how are you?"},
    ]

    response = client.post(f"/api/agents/{AGENT_NAME}/chat", json={"messages": messages})

    assert response.status_code == 200
    assert response.json() == {"reply": "hello yourself, human"}
    assert len(fake_agent.converse_calls) == 1
    namespace, service, recorded = fake_agent.converse_calls[0]
    assert namespace == AGENT_NAME
    assert service == AGENT_NAME
    assert recorded == messages


def test_chat_multi_turn_each_call_forwards_its_own_full_transcript(
    session_factory, config, fake_github, fake_gojira
):
    """The server is stateless: each POST carries the whole transcript so
    far, and each converse() call must record exactly that call's messages —
    never a merge with, or carryover from, a prior call."""
    fake_agent = FakeAgent(replies=["hi there!", "still doing fine"])
    client = _client_with_agent(session_factory, config, fake_github, fake_gojira, fake_agent)
    _seed_agent(session_factory, config)

    first_messages = [{"role": "user", "text": "hi"}]
    first = client.post(f"/api/agents/{AGENT_NAME}/chat", json={"messages": first_messages})
    assert first.status_code == 200
    assert first.json() == {"reply": "hi there!"}

    second_messages = first_messages + [
        {"role": "agent", "text": "hi there!"},
        {"role": "user", "text": "how are you?"},
    ]
    second = client.post(f"/api/agents/{AGENT_NAME}/chat", json={"messages": second_messages})
    assert second.status_code == 200
    assert second.json() == {"reply": "still doing fine"}

    assert len(fake_agent.converse_calls) == 2
    assert fake_agent.converse_calls[0] == (AGENT_NAME, AGENT_NAME, first_messages)
    assert fake_agent.converse_calls[1] == (AGENT_NAME, AGENT_NAME, second_messages)


def test_chat_unknown_agent_returns_404(client):
    response = client.post(
        "/api/agents/does-not-exist/chat", json={"messages": [{"role": "user", "text": "hi"}]}
    )

    assert response.status_code == 404
    assert "error" in response.json()


@pytest.mark.parametrize(
    "status", [AgentStatus.PENDING, AgentStatus.PROVISIONING, AgentStatus.FAILED]
)
def test_chat_non_ready_agent_returns_409(client, session_factory, config, status):
    _seed_agent(session_factory, config, status=status)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/chat", json={"messages": [{"role": "user", "text": "hi"}]}
    )

    assert response.status_code == 409
    assert response.json() == {"error": "agent is not ready to chat"}


def test_chat_empty_messages_returns_422(client, session_factory, config):
    _seed_agent(session_factory, config)

    response = client.post(f"/api/agents/{AGENT_NAME}/chat", json={"messages": []})

    assert response.status_code == 422
    assert "error" in response.json()


def test_chat_last_message_not_user_returns_422(client, session_factory, config):
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/chat",
        json={"messages": [{"role": "user", "text": "hi"}, {"role": "agent", "text": "yo"}]},
    )

    assert response.status_code == 422
    assert "error" in response.json()


def test_chat_empty_text_returns_422(client, session_factory, config):
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/chat", json={"messages": [{"role": "user", "text": ""}]}
    )

    assert response.status_code == 422
    assert "error" in response.json()


def test_chat_text_over_4000_chars_returns_422(client, session_factory, config):
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/chat",
        json={"messages": [{"role": "user", "text": "a" * 4001}]},
    )

    assert response.status_code == 422
    assert "error" in response.json()


def test_chat_unknown_role_returns_422(client, session_factory, config):
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/chat",
        json={"messages": [{"role": "system", "text": "hi"}]},
    )

    assert response.status_code == 422
    assert "error" in response.json()


def test_chat_converse_error_returns_502_with_client_error_message(
    session_factory, config, fake_github, fake_gojira
):
    fake_agent = FakeAgent(converse_error="a2a request failed (500): boom")
    client = _client_with_agent(session_factory, config, fake_github, fake_gojira, fake_agent)
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/chat", json={"messages": [{"role": "user", "text": "hi"}]}
    )

    assert response.status_code == 502
    assert response.json() == {"error": "agent unreachable: a2a request failed (500): boom"}


# --- Chat via the switchboard relay (Task 3) ----------------------------------------
#
# Selection between the relay and the legacy direct path is `cabal_enabled(config)
# and clients.cabal is not None` — every combination of those two independent
# gates is pinned below, alongside envelope byte-parity with the direct path's
# own `_build_task_envelope` (kobo_api.clients), the relayed reply's tolerant
# extraction, and the relay-failure 502's fixed, plain copy.

CABAL_ENDPOINT_TEMPLATE = "http://agent-{name}.default.svc.cluster.local"


def _enabled_cabal_config(config):
    return config.model_copy(
        update={"cabal_url": "http://cabal.internal", "agent_endpoint_template": CABAL_ENDPOINT_TEMPLATE}
    )


def _client_with_cabal(
    session_factory, config, fake_github, fake_gojira, fake_agent, cabal, *, kubectl=None
):
    return TestClient(
        create_app(
            session_factory,
            config=config,
            clients=Clients(
                github=fake_github,
                gojira=fake_gojira,
                cloud=FakeCloud(),
                agent=fake_agent,
                kubectl=kubectl if kubectl is not None else FakeKubectl(),
                cabal=cabal,
            ),
        )
    )


def test_chat_relays_through_the_switchboard_when_cabal_is_enabled(
    session_factory, config, fake_github, fake_gojira, fake_agent, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_cabal_config(config)
    fake_cabal = FakeCabal(
        relay_result={"status": {"message": {"parts": [{"text": "hi from the switchboard"}]}}}
    )
    client = _client_with_cabal(
        session_factory, cfg, fake_github, fake_gojira, fake_agent, fake_cabal
    )
    _seed_agent(session_factory, cfg)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/chat", json={"messages": [{"role": "user", "text": "hi"}]}
    )

    assert response.status_code == 200
    assert response.json() == {"reply": "hi from the switchboard"}
    assert len(fake_cabal.relay_calls) == 1
    assert fake_cabal.relay_calls[0]["name"] == AGENT_NAME
    # the legacy direct path never runs when the relay handles the chat
    assert fake_agent.converse_calls == []


def test_chat_uses_direct_path_when_switchboard_not_configured(
    session_factory, config, fake_github, fake_gojira, fake_agent
):
    """Default config/clients (no cabal org keys, no cabal client) — the
    legacy direct path, completely unchanged."""
    client = _client_with_cabal(
        session_factory, config, fake_github, fake_gojira, fake_agent, None
    )
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/chat", json={"messages": [{"role": "user", "text": "hi"}]}
    )

    assert response.status_code == 200
    assert response.json() == {"reply": fake_agent._reply}
    assert len(fake_agent.converse_calls) == 1


def test_chat_falls_back_to_direct_path_when_cabal_client_missing_even_if_config_enabled(
    session_factory, config, fake_github, fake_gojira, fake_agent, monkeypatch
):
    """`cabal_enabled(config)` true but `clients.cabal` is None (e.g. wired
    without a cabal client despite the org config being set) must not crash —
    falls back to the direct path exactly like an unwired room."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_cabal_config(config)
    client = _client_with_cabal(session_factory, cfg, fake_github, fake_gojira, fake_agent, None)
    _seed_agent(session_factory, cfg)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/chat", json={"messages": [{"role": "user", "text": "hi"}]}
    )

    assert response.status_code == 200
    assert response.json() == {"reply": fake_agent._reply}
    assert len(fake_agent.converse_calls) == 1


def test_chat_ignores_cabal_client_when_switchboard_not_enabled(
    session_factory, config, fake_github, fake_gojira, fake_agent
):
    """A cabal client present on `Clients` but `cabal_enabled(config)` false
    (e.g. the operator token env var unset) must still take the direct
    path — the client's mere presence is not the selection signal."""
    fake_cabal = FakeCabal()
    client = _client_with_cabal(
        session_factory, config, fake_github, fake_gojira, fake_agent, fake_cabal
    )
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/chat", json={"messages": [{"role": "user", "text": "hi"}]}
    )

    assert response.status_code == 200
    assert response.json() == {"reply": fake_agent._reply}
    assert fake_cabal.relay_calls == []
    assert len(fake_agent.converse_calls) == 1


def test_chat_relay_envelope_matches_the_shared_builder_verbatim(
    session_factory, config, fake_github, fake_gojira, fake_agent, monkeypatch
):
    """The relay path must reuse `_build_task_envelope` (kobo_api.clients)
    verbatim rather than fork the shape. Pinned by freezing `uuid4` (the
    envelope's only non-deterministic field) and comparing the envelope the
    relay actually receives against calling that same builder directly with
    the same messages.

    The route deliberately does NOT stamp a contextId (operator ruling
    2026-08-26): the runtime already shares one context across every hop of
    a turn, and a derived per-conversation id would merge a whole
    conversation into one guard session, refusing a third identical reply
    within ten minutes.
    """
    import uuid

    from kobo_api.clients import _build_task_envelope

    monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID(int=0))
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_cabal_config(config)
    fake_cabal = FakeCabal(relay_result={"status": {"message": {"parts": [{"text": "ok"}]}}})
    client = _client_with_cabal(
        session_factory, cfg, fake_github, fake_gojira, fake_agent, fake_cabal
    )
    _seed_agent(session_factory, cfg)
    messages = [
        {"role": "user", "text": "hi"},
        {"role": "agent", "text": "hello yourself"},
        {"role": "user", "text": "how are you?"},
    ]

    client.post(f"/api/agents/{AGENT_NAME}/chat", json={"messages": messages})

    expected_envelope = _build_task_envelope(messages)
    assert "contextId" not in expected_envelope
    assert fake_cabal.relay_calls[0]["task_envelope"] == expected_envelope


@pytest.mark.parametrize(
    "relay_result, expected_reply",
    [
        pytest.param(
            {"status": {"message": {"parts": [{"text": "bare task reply"}]}}},
            "bare task reply",
            id="bare-task-status-message",
        ),
        pytest.param(
            {"task": {"status": {"message": {"parts": [{"text": "wrapped in task"}]}}}},
            "wrapped in task",
            id="wrapped-in-task-key",
        ),
        pytest.param(
            {"result": {"status": {"message": {"parts": [{"text": "wrapped in result"}]}}}},
            "wrapped in result",
            id="wrapped-in-result-key",
        ),
        pytest.param(
            {"status": {"message": {"parts": [{"text": "first"}, {"text": "last"}]}}},
            "first\nlast",
            id="multiple-parts-join-in-order",
        ),
        pytest.param(
            {
                "status": {"message": {"parts": [{"other_key": "value"}]}},
                "history": [
                    {"role": "agent", "parts": [{"text": "from history"}]},
                ],
            },
            "from history",
            id="skips-source-when-no-part-carries-text",
        ),
        pytest.param(
            {
                "history": [
                    {"role": "user", "parts": [{"text": "hi"}]},
                    {"role": "agent", "parts": [{"text": "from history"}]},
                ]
            },
            "from history",
            id="falls-back-to-historys-last-turn",
        ),
        pytest.param(
            {"artifacts": [{"parts": [{"text": "from artifacts"}]}]},
            "from artifacts",
            id="falls-back-to-artifacts-last-entry",
        ),
    ],
)
def test_chat_relay_reply_extraction_variants(
    session_factory,
    config,
    fake_github,
    fake_gojira,
    fake_agent,
    monkeypatch,
    relay_result,
    expected_reply,
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_cabal_config(config)
    fake_cabal = FakeCabal(relay_result=relay_result)
    client = _client_with_cabal(
        session_factory, cfg, fake_github, fake_gojira, fake_agent, fake_cabal
    )
    _seed_agent(session_factory, cfg)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/chat", json={"messages": [{"role": "user", "text": "hi"}]}
    )

    assert response.status_code == 200
    assert response.json() == {"reply": expected_reply}


def test_chat_relay_cabal_error_returns_502_with_fixed_plain_copy(
    session_factory, config, fake_github, fake_gojira, fake_agent, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_cabal_config(config)
    fake_cabal = FakeCabal(relay_error="switchboard unreachable")
    client = _client_with_cabal(
        session_factory, cfg, fake_github, fake_gojira, fake_agent, fake_cabal
    )
    _seed_agent(session_factory, cfg)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/chat", json={"messages": [{"role": "user", "text": "hi"}]}
    )

    assert response.status_code == 502
    assert response.json() == {"error": "The switchboard did not answer. Try again shortly."}


def test_chat_relay_malformed_reply_returns_502_with_fixed_plain_copy(
    session_factory, config, fake_github, fake_gojira, fake_agent, monkeypatch
):
    """A relay response with no text part anywhere (empty parts, no
    status/history/artifacts at all, ...) must 502 the same fixed way a
    genuine relay failure does, never an unhandled 500."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_cabal_config(config)
    fake_cabal = FakeCabal(relay_result={"status": {"message": {"parts": []}}})
    client = _client_with_cabal(
        session_factory, cfg, fake_github, fake_gojira, fake_agent, fake_cabal
    )
    _seed_agent(session_factory, cfg)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/chat", json={"messages": [{"role": "user", "text": "hi"}]}
    )

    assert response.status_code == 502
    assert response.json() == {"error": "The switchboard did not answer. Try again shortly."}


def test_chat_relay_connection_error_returns_502_with_fixed_plain_copy(
    session_factory, config, fake_github, fake_gojira, fake_agent, monkeypatch
):
    """`CabalClient.relay` itself never wraps httpx transport errors (unlike
    the direct path's `_post_task`) — a real `CabalClient` wired against a
    transport that raises a connection error must still surface as this
    same fixed 502, not an unhandled 500."""
    import httpx

    from kobo_api.clients import CabalClient

    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_cabal_config(config)

    def _raise_connect_error(request):
        raise httpx.ConnectError("connection refused")

    real_cabal = CabalClient(
        "http://cabal.internal",
        "op-token",
        client=httpx.Client(transport=httpx.MockTransport(_raise_connect_error)),
    )
    client = _client_with_cabal(
        session_factory, cfg, fake_github, fake_gojira, fake_agent, real_cabal
    )
    _seed_agent(session_factory, cfg)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/chat", json={"messages": [{"role": "user", "text": "hi"}]}
    )

    assert response.status_code == 502
    assert response.json() == {"error": "The switchboard did not answer. Try again shortly."}


# --- chat relay failure + delivery-aware copy (Task 6) -----------------------


def _mid_rollout_status():
    # observedGeneration behind metadata.generation -> the controller
    # hasn't seen the latest spec yet, the clearest "mid-rollout" signal.
    return {
        "metadata": {"generation": 2},
        "spec": {"replicas": 1},
        "status": {"observedGeneration": 1, "updatedReplicas": 1, "availableReplicas": 1},
    }


def test_chat_relay_failure_during_rollout_says_delivery(
    session_factory, config, fake_github, fake_gojira, fake_agent, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_cabal_config(config)
    fake_cabal = FakeCabal(relay_error="switchboard unreachable")
    fake_kubectl = FakeKubectl(
        deployment_status={(AGENT_NAME, AGENT_NAME): _mid_rollout_status()}
    )
    client = _client_with_cabal(
        session_factory, cfg, fake_github, fake_gojira, fake_agent, fake_cabal,
        kubectl=fake_kubectl,
    )
    _seed_agent(session_factory, cfg)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/chat", json={"messages": [{"role": "user", "text": "hi"}]}
    )

    assert response.status_code == 503
    assert response.json() == {
        "error": "Your agent is receiving a delivery right now. Try again in a minute."
    }


def test_chat_relay_failure_when_settled_keeps_switchboard_copy(
    session_factory, config, fake_github, fake_gojira, fake_agent, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_cabal_config(config)
    fake_cabal = FakeCabal(relay_error="switchboard unreachable")
    # FakeKubectl()'s default deployment_status_json is already a fully
    # settled rollout (tests/fakes.py FAKE_DEPLOYMENT_STATUS_SETTLED) — no
    # configuration needed to model "not mid-rollout" here.
    fake_kubectl = FakeKubectl()
    client = _client_with_cabal(
        session_factory, cfg, fake_github, fake_gojira, fake_agent, fake_cabal,
        kubectl=fake_kubectl,
    )
    _seed_agent(session_factory, cfg)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/chat", json={"messages": [{"role": "user", "text": "hi"}]}
    )

    assert response.status_code == 502
    assert response.json() == {"error": "The switchboard did not answer. Try again shortly."}


def test_chat_relay_failure_with_probe_error_keeps_switchboard_copy(
    session_factory, config, fake_github, fake_gojira, fake_agent, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_cabal_config(config)
    fake_cabal = FakeCabal(relay_error="switchboard unreachable")
    fake_kubectl = FakeKubectl(deployment_status_error="kubectl down")
    client = _client_with_cabal(
        session_factory, cfg, fake_github, fake_gojira, fake_agent, fake_cabal,
        kubectl=fake_kubectl,
    )
    _seed_agent(session_factory, cfg)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/chat", json={"messages": [{"role": "user", "text": "hi"}]}
    )

    assert response.status_code == 502
    assert response.json() == {"error": "The switchboard did not answer. Try again shortly."}


def test_chat_relay_failure_logs_exception_type_and_elapsed_seconds(
    session_factory, config, fake_github, fake_gojira, fake_agent, monkeypatch, caplog
):
    """Task 8 (502 instrumentation): the root cause of the intermittent
    502 isn't established yet, so the relay-failure log line must carry
    enough to characterize the next live one -- the exception TYPE and the
    elapsed wall time of the relay call -- without logging message content,
    which stays out of this path on purpose."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_cabal_config(config)
    fake_cabal = FakeCabal(relay_error="switchboard unreachable")
    client = _client_with_cabal(
        session_factory, cfg, fake_github, fake_gojira, fake_agent, fake_cabal
    )
    _seed_agent(session_factory, cfg)

    # Deliberately NOT patching time.monotonic: `main_module.time` IS the
    # stdlib module, so patching its attribute swaps the clock for every
    # consumer in the process (httpx, threading, pytest itself) and hangs
    # the run. Assert the SHAPE of the duration instead, which is what the
    # instrumentation actually promises.
    with caplog.at_level("WARNING"):
        response = client.post(
            f"/api/agents/{AGENT_NAME}/chat",
            json={"messages": [{"role": "user", "text": "do not log this text"}]},
        )

    assert response.status_code == 502
    assert "CabalError" in caplog.text
    assert re.search(r"after \d+\.\d{3}s", caplog.text), caplog.text
    assert "do not log this text" not in caplog.text


# --- model_label fold-in ------------------------------------------------------------


def test_list_agents_includes_model_label_from_catalog(client, session_factory, config):
    _seed_agent(session_factory, config)

    response = client.get("/api/agents")

    row = response.json()["agents"][0]
    assert row["model"] == config.models[0].id
    assert row["model_label"] == config.models[0].label


def test_list_agents_model_label_falls_back_to_raw_id_when_not_in_catalog(
    client, session_factory, config
):
    _seed_agent(session_factory, config, model_id="ancient-model-no-longer-offered")

    response = client.get("/api/agents")

    row = response.json()["agents"][0]
    assert row["model"] == "ancient-model-no-longer-offered"
    assert row["model_label"] == "ancient-model-no-longer-offered"


def test_model_label_for_a_retired_id_is_its_kin_in_the_catalog(
    client, session_factory, config
):
    """The name outlives the id: an agent still on claude-sonnet-4-5 reads
    as the catalog's Sonnet, with the raw id on the hover (`model`)."""
    from kobo_api.config import ModelChoice

    config.models = [
        ModelChoice(id="vertex_ai/claude-sonnet-5", label="Claude Affordable (Sonnet)"),
        ModelChoice(id="vertex_ai/gemini-3.5-flash", label="Gemini Affordable (Flash)"),
    ]
    _seed_agent(session_factory, config, model_id="vertex_ai/claude-sonnet-4-5")

    row = client.get("/api/agents").json()["agents"][0]
    assert row["model"] == "vertex_ai/claude-sonnet-4-5"
    assert row["model_label"] == "Claude Affordable (Sonnet)"


def test_retry_agent_launch_failure_returns_503_and_a_later_retry_still_succeeds(
    session_factory, config, clients, monkeypatch
):
    """Same launch-failure guard as create_agent's, but on the retry route: a
    `run_pipeline` failure there must still leave the agent FAILED (not
    wedged in PROVISIONING past the FAILED-only retry gate), so a follow-up
    retry against a working pipeline can succeed. Reuses the recording-no-op
    pattern from the race test above for that follow-up.
    """
    import kobo_api.main as main_module

    seed_session = session_factory()
    seed_session.add(
        Agent(
            name=AGENT_NAME,
            persona=PERSONA,
            model_id=config.models[0].id,
            status=AgentStatus.FAILED,
            error="boom",
        )
    )
    seed_session.commit()
    seed_session.close()

    def _raising_run_pipeline(session_factory, agent_id, *, config, clients, steps):
        raise RuntimeError("no more threads")

    monkeypatch.setattr(main_module, "run_pipeline", _raising_run_pipeline)

    race_client = TestClient(create_app(session_factory, config=config, clients=clients))

    first = race_client.post(f"/api/agents/{AGENT_NAME}/retry")

    assert first.status_code == 503
    assert first.json() == {"error": "failed to start provisioning: no more threads"}

    session = session_factory()
    agent = session.query(Agent).filter_by(name=AGENT_NAME).first()
    assert agent.status == AgentStatus.FAILED
    assert agent.error == "failed to start provisioning: no more threads"
    session.close()

    recorded_agent_ids: list[int] = []

    def _recording_run_pipeline(session_factory, agent_id, *, config, clients, steps):
        recorded_agent_ids.append(agent_id)

    monkeypatch.setattr(main_module, "run_pipeline", _recording_run_pipeline)

    second = race_client.post(f"/api/agents/{AGENT_NAME}/retry")

    assert second.status_code == 202
    assert second.json()["status"] == "provisioning"
    assert len(recorded_agent_ids) == 1

    session = session_factory()
    agent = session.query(Agent).filter_by(name=AGENT_NAME).first()
    assert agent.status == AgentStatus.PROVISIONING
    assert agent.error is None
    session.close()


class _PoisonedAfterRollbackSession:
    """Proxies a real Session, but once `.rollback()` is called, every
    further use of the session (crucially `.commit()`) raises too.

    Models a genuinely dead connection: a real one routinely fails BOTH the
    rollback attempt AND any statement run afterward on that same session,
    not just the rollback in isolation. Used to force the exact scenario
    `_fail_launch` exists for — the launch guard's own session going bad
    right when it tries to record the failure — without needing a real
    dropped connection.
    """

    def __init__(self, real):
        self._real = real
        self._poisoned = False

    def rollback(self):
        self._poisoned = True
        raise RuntimeError("connection already closed")

    def commit(self):
        if self._poisoned:
            raise RuntimeError("connection already closed")
        return self._real.commit()

    def __getattr__(self, item):
        return getattr(self._real, item)


def test_create_agent_launch_failure_with_poisoned_session_still_returns_503_and_agent_lands_failed(
    session_factory, config, clients, monkeypatch
):
    """Same launch failure as the guard test above, but with the request's
    own session poisoned too (its `.rollback()` raises, and — modeling a
    truly dead connection — so does any further `.commit()` on that same
    session). Recording FAILED on that same poisoned session would raise
    unhandled, turning what should be a 503 into a raw 500 AND leaving the
    agent wedged in PENDING: exactly the failure mode `_fail_launch`'s
    fresh-session recovery (mirroring `_run_pipeline_body`'s
    `recovery_session = session_factory()`) exists to prevent. Assert the
    response is still the caller-actionable 503 JSON, and the agent still
    lands FAILED — via the fresh session, since the original is unusable.
    """
    import kobo_api.main as main_module

    def _raising_run_pipeline(session_factory, agent_id, *, config, clients, steps):
        raise RuntimeError("no more threads")

    monkeypatch.setattr(main_module, "run_pipeline", _raising_run_pipeline)

    def _override_get_session():
        real = session_factory()
        try:
            yield _PoisonedAfterRollbackSession(real)
        finally:
            real.close()

    app = create_app(session_factory, config=config, clients=clients)
    app.dependency_overrides[get_session] = _override_get_session
    race_client = TestClient(app)

    response = race_client.post(
        "/api/agents", json={"name": AGENT_NAME, "persona": PERSONA, "team": TEAM}
    )

    assert response.status_code == 503
    assert response.json() == {"error": "failed to start provisioning: no more threads"}

    session = session_factory()
    agent = session.query(Agent).filter_by(name=AGENT_NAME).first()
    assert agent.status == AgentStatus.FAILED
    assert agent.error == "failed to start provisioning: no more threads"
    session.close()


class _PoisonedCloseSession:
    """Proxies a real Session, but `.close()` always raises — models a
    connection that's dead enough to fail even on release back to the pool,
    while rollback/commit on it still work fine. Used to exercise
    `_fail_launch`'s guard around its own `session.close()` (P1c review
    fold-in): without that guard, a raising close() would propagate out of
    `_fail_launch` before it ever reached the fresh-session FAILED write,
    turning the caller-actionable 503 into a raw 500.
    """

    def __init__(self, real):
        self._real = real

    def close(self):
        raise RuntimeError("connection already closed")

    def __getattr__(self, item):
        return getattr(self._real, item)


def test_create_agent_launch_failure_with_session_close_raising_still_returns_503_and_agent_lands_failed(
    session_factory, config, clients, monkeypatch
):
    """Same launch failure as the guard tests above, but this time it's the
    request's own session's `.close()` (not its `.rollback()`) that raises.
    `_fail_launch`'s `finally: session.close()` on that session must be
    guarded the same best-effort way as its rollback beside it — the
    response must still be the caller-actionable 503, and the agent must
    still land FAILED via the fresh recovery session.
    """
    import kobo_api.main as main_module

    def _raising_run_pipeline(session_factory, agent_id, *, config, clients, steps):
        raise RuntimeError("no more threads")

    monkeypatch.setattr(main_module, "run_pipeline", _raising_run_pipeline)

    def _override_get_session():
        real = session_factory()
        try:
            yield _PoisonedCloseSession(real)
        finally:
            real.close()

    app = create_app(session_factory, config=config, clients=clients)
    app.dependency_overrides[get_session] = _override_get_session
    race_client = TestClient(app)

    response = race_client.post(
        "/api/agents", json={"name": AGENT_NAME, "persona": PERSONA, "team": TEAM}
    )

    assert response.status_code == 503
    assert response.json() == {"error": "failed to start provisioning: no more threads"}

    session = session_factory()
    agent = session.query(Agent).filter_by(name=AGENT_NAME).first()
    assert agent.status == AgentStatus.FAILED
    assert agent.error == "failed to start provisioning: no more threads"
    session.close()


# --- every agent builds on the one runtime -----------------------------------


def test_create_leaves_the_runtime_column_null(client, session_factory):
    """A NULL column keeps the agent following the org pin as it moves;
    only `PATCH /api/agents/{name}/runtime` ever fills it."""
    r = client.post("/api/agents", json={
        "name": "sdk-three", "persona": "helpful-generalist",
    })
    assert r.status_code in (200, 201, 202), r.text
    from kobo_api.models import Agent

    session = session_factory()
    row = session.query(Agent).filter_by(name="sdk-three").first()
    assert row is not None and row.runtime_image is None
    session.close()


def test_create_ignores_a_runtime_image_an_older_client_still_sends(client, session_factory):
    """A browser tab or MCP client loaded before the field was retired
    keeps sending it. Ignored silently (`CreateAgentRequest`'s own
    `extra="ignore"`) -- never a 422, and never a 500 -- because the caller
    is asking for exactly the runtime it now gets anyway."""
    r = client.post("/api/agents", json={
        "name": "sdk-four", "persona": "helpful-generalist",
        "runtime_image": "docker.io/evil/runtime:latest",
    })

    assert r.status_code in (200, 201, 202), r.text
    from kobo_api.models import Agent

    session = session_factory()
    row = session.query(Agent).filter_by(name="sdk-four").first()
    assert row is not None and row.runtime_image is None
    session.close()


def test_runtimes_catalog_route_is_gone(client):
    assert client.get("/api/catalog/runtimes").status_code == 404


def test_observability_catalog_empty_without_config(client):
    r = client.get("/api/catalog/observability")
    assert r.status_code == 200
    assert r.json() == {"url": None, "project": None}


def test_observability_catalog_returns_configured_values(client):
    cfg = client.app.state.config
    old = (cfg.noto_url, cfg.noto_project)
    cfg.noto_url, cfg.noto_project = "https://noto.example", "ai-agents-dev"
    try:
        assert client.get("/api/catalog/observability").json() == {
            "url": "https://noto.example",
            "project": "ai-agents-dev",
        }
    finally:
        cfg.noto_url, cfg.noto_project = old


def test_observability_catalog_strips_trailing_slash_from_url(client):
    """A trailing slash on the configured url would double up with the
    `/projects/...` path the UI appends, producing a broken `//projects`
    link -- the route normalizes it away."""
    cfg = client.app.state.config
    old = (cfg.noto_url, cfg.noto_project)
    cfg.noto_url, cfg.noto_project = "https://noto.example/", "ai-agents-dev"
    try:
        assert client.get("/api/catalog/observability").json() == {
            "url": "https://noto.example",
            "project": "ai-agents-dev",
        }
    finally:
        cfg.noto_url, cfg.noto_project = old


def test_observability_catalog_both_or_nothing_url_only(client):
    """A half-configured pair (only one of url/project set) must surface as
    fully absent, same as the composer's other both-or-nothing config pairs
    (e.g. broker_url's github block) -- a partial value is not actionable."""
    cfg = client.app.state.config
    old = (cfg.noto_url, cfg.noto_project)
    cfg.noto_url, cfg.noto_project = "https://noto.example", None
    try:
        assert client.get("/api/catalog/observability").json() == {
            "url": None,
            "project": None,
        }
    finally:
        cfg.noto_url, cfg.noto_project = old


# --- team-guide archetype (spec 2026-09-14) ---------------------------------
#
# The create route's archetype lane FORCES the bundle surface (persona,
# skills, tags, ACL namespace) and ignores whatever the client sent
# for those — "cannot act" is a property of the composed bundle, not of a UI
# default. This module's `config` fixture names no groups, so `group_name`
# reaches the route unvalidated (exactly as the pre-groups tests above rely
# on) and the archetype branch is the only thing that rewrites it.


def _create_guide(client, name="atlas", group="sre", voice=None, **extra):
    payload = {
        "name": name,
        "persona": PERSONA,  # ignored by the archetype path
        "archetype": "team_guide",
        "group_name": group,
        **({"voice": voice} if voice else {}),
        **extra,
    }
    return client.post("/api/agents", json=payload)


def test_archetype_forces_tags_persona_and_commons_namespace(client):
    r = _create_guide(client)
    assert r.status_code == 201, r.text

    agent = client.get(f"/api/agents/{r.json()['name']}").json()
    assert "team-guide" in agent["purpose_tags"] and "sre" in agent["purpose_tags"]
    assert agent["persona"] == "team-guide-sre"
    # shared visibility, mechanically: the guide lives in the commons
    assert agent["group_name"] == "shared"


def test_archetype_ignores_client_supplied_skills(client, session_factory):
    r = _create_guide(client, skills=[{"id": "word-games", "team": "shared"}])
    assert r.status_code == 201, r.text

    agent = client.get(f"/api/agents/{r.json()['name']}").json()
    # The marketplace hasn't published `team-guide` in this fixture's
    # catalog, so the forced pick list is empty — and the client's own pick
    # is gone either way.
    assert agent["skills"] == []
    session = session_factory()
    row = session.query(Agent).filter_by(name="atlas").first()
    assert row is not None and row.runtime_image is None
    session.close()


def test_archetype_duplicate_guard_409_names_existing(client):
    assert _create_guide(client, name="atlas").status_code == 201

    r = _create_guide(client, name="beacon")
    assert r.status_code == 409
    assert r.json()["existing"] == "atlas"
    # A different team may still have its own guide.
    assert _create_guide(client, name="beacon", group="qa").status_code == 201


def test_archetype_voice_lands_in_stored_persona(client, fake_storage):
    assert _create_guide(client, voice="Anthony Bourdain").status_code == 201

    body = fake_storage.read_bytes("personas-bucket", "team-guide-sre/persona.md").decode()
    assert "Anthony Bourdain" in body and "Never act." in body


def test_archetype_persona_index_team_is_the_slug_not_the_raw_group(client, fake_storage):
    """The persona catalog's `team` axis must only ever carry slugs -- this
    is the one write path (`upsert_persona` in the archetype branch) that
    used to file the RAW picked group instead, which could put a non-slug
    string (mixed case, internal spaces) into the teams axis. `group` (raw)
    still belongs in the rendered persona prose/name/description, just not
    here."""
    r = _create_guide(client, name="atlas", group="Data Eng")
    assert r.status_code == 201, r.text

    index = json.loads(fake_storage.read_bytes("personas-bucket", "index.json"))
    entry = next(e for e in index["entries"] if e["id"] == "team-guide-data-eng")
    assert entry["team"] == "data-eng"
    # The raw group survives only in the rendered prose, not the index.
    assert entry["name"] == "Data Eng team guide"


def test_archetype_rerender_overwrites_an_existing_guide_persona(client, fake_storage):
    """Re-creating a guide after a destroy re-renders its persona: the
    persona id is derived from the group, so the write must overwrite rather
    than collide the way POST /api/personas' 409 would."""
    assert _create_guide(client, name="atlas").status_code == 201
    first = fake_storage.read_bytes("personas-bucket", "team-guide-sre/persona.md").decode()
    assert "Anthony Bourdain" not in first

    # Stand in for a destroy: let the pipeline settle, then drop the row that
    # holds the team-guide tag pair, freeing the group for a fresh guide.
    _poll_until(client, "atlas", {"ready", "failed"})
    session = client.app.state.session_factory()
    row = session.query(Agent).filter_by(name="atlas").first()
    for step in list(row.steps):
        session.delete(step)
    session.delete(row)
    session.commit()
    session.close()

    assert _create_guide(client, name="beacon", voice="Anthony Bourdain").status_code == 201
    second = fake_storage.read_bytes("personas-bucket", "team-guide-sre/persona.md").decode()
    assert "Anthony Bourdain" in second


def test_unknown_archetype_is_422(client):
    r = client.post(
        "/api/agents",
        json={"name": "atlas", "persona": PERSONA, "archetype": "team_overlord"},
    )
    assert r.status_code == 422
    assert "archetype" in r.json()["error"]


def test_archetype_voice_must_be_a_name_not_a_biography(client):
    r = _create_guide(client, voice="x" * 121)
    assert r.status_code == 422
    assert "voice" in r.json()["error"]


# --- team-guide review fix wave (C1/M1/I3/I4/M2/M3) -------------------------


def test_archetype_duplicate_guard_normalizes_group_case(client):
    """C1: the guard used to compare the RAW picked group against normalized
    tags, so "SRE" and "sre" (or "Data Eng"/"data-eng") minted two guides
    with divergent persona ids instead of colliding. Both must now resolve
    to the same slug -- and therefore the same guide."""
    first = _create_guide(client, name="atlas", group="SRE")
    assert first.status_code == 201, first.text
    agent = client.get("/api/agents/atlas").json()
    assert agent["persona"] == "team-guide-sre"

    r = _create_guide(client, name="beacon", group="sre")
    assert r.status_code == 409, r.text
    assert r.json()["existing"] == "atlas"


def test_archetype_rejects_a_group_that_does_not_slugify_to_a_persona_id(client):
    """M1: a picked group that folds to something shaped unlike a persona id
    (PERSONA_ID_RE) must never reach `upsert_persona` as a storage key. This
    module's `client` fixture configures no groups at all, so nothing
    upstream of the archetype branch validates `group_name` first."""
    r = _create_guide(client, group="../../pwned")
    assert r.status_code == 422
    assert "group name" in r.json()["error"]


def test_ordinary_agent_sharing_both_tags_does_not_block_the_real_guide(client):
    """I3: the tag-pair guard alone false-positives on any ordinary agent
    that happens to carry both "team-guide" and the group tag -- the guard
    must also require that agent's persona actually be the guide persona."""
    impostor = client.post(
        "/api/agents",
        json={
            "name": "impostor",
            "persona": PERSONA,
            "purpose_tags": ["team-guide", "sre"],
        },
    )
    assert impostor.status_code == 201, impostor.text

    r = _create_guide(client, name="atlas", group="sre")
    assert r.status_code == 201, r.text


def test_archetype_name_collision_is_checked_before_any_persona_write(client, fake_storage):
    """I4: the name-taken 409 must be checked BEFORE the persona write, so a
    collision on the most likely user error (re-using an existing agent's
    name) never orphans a team-guide-<slug> entry in the personas catalog."""
    taken = client.post(
        "/api/agents", json={"name": "atlas", "persona": PERSONA, "team": TEAM}
    )
    assert taken.status_code == 201, taken.text

    r = _create_guide(client, name="atlas", group="sre")
    assert r.status_code == 409, r.text

    with pytest.raises(StorageNotFound):
        fake_storage.read_bytes("personas-bucket", "team-guide-sre/persona.md")


def test_archetype_forced_skill_pick_prefers_the_shared_team_deterministically(
    client, fake_storage
):
    """M2: a skill id is only unique WITHIN a team (SkillPick's own
    docstring), so a bare `team=None` lookup over two teams that each
    published their own "team-guide" skill would otherwise depend on
    whatever order an unordered SQL scan happens to return. The forced pick
    must deterministically prefer the shared/marketplace team's entry."""
    fake_storage.write_bytes(
        "skills-bucket",
        "index.json",
        json.dumps(
            {
                "entries": [
                    {
                        "id": "team-guide",
                        "name": "Some other team's guide skill",
                        "description": "not the packaged one",
                        "team": "not-shared",
                        "tags": [],
                        "sha": "othersha",
                        "builtin": False,
                    },
                    {
                        "id": "team-guide",
                        "name": "Team guide",
                        "description": "The packaged guide skill.",
                        "team": "shared",
                        "tags": [],
                        "sha": "sharedsha",
                        "builtin": False,
                    },
                ]
            }
        ).encode(),
        content_type="application/json",
    )

    r = _create_guide(client, name="atlas", group="sre")
    assert r.status_code == 201, r.text

    agent = client.get("/api/agents/atlas").json()
    assert agent["skills"] == [{"name": "team-guide", "team": "shared", "sha": "sharedsha"}]


def test_archetype_marker_lands_on_the_agent_row(client, session_factory):
    """Task 2b: the row carries the archetype so the composer can suppress
    the action-tool blocks org config would otherwise hand every bundle."""
    assert _create_guide(client, name="atlas", group="sre").status_code == 201

    session = session_factory()
    row = session.query(Agent).filter_by(name="atlas").first()
    assert row is not None and row.archetype == "team_guide"
    session.close()

    # Task 2b: the detail response carries the same marker -- the UI can
    # then detect a guide from this explicit field instead of the defeasible
    # purpose-tags pair.
    agent = client.get("/api/agents/atlas").json()
    assert agent["archetype"] == "team_guide"


def test_plain_create_leaves_the_archetype_marker_null(client, session_factory):
    r = client.post("/api/agents", json={"name": "beacon", "persona": PERSONA})
    assert r.status_code == 201, r.text

    session = session_factory()
    row = session.query(Agent).filter_by(name="beacon").first()
    assert row is not None and row.archetype is None
    session.close()

    agent = client.get("/api/agents/beacon").json()
    assert agent["archetype"] is None


def test_archetype_forces_baseline_and_default_model(client, session_factory):
    """M3: the kiosk always ships the baseline guardrail tools and the
    org-default model -- a client-supplied opt-out or model override must
    never reach an archetype agent."""
    r = _create_guide(client, name="atlas", group="sre", skip_baseline=True, model="nonexistent")
    assert r.status_code == 201, r.text

    session = session_factory()
    row = session.query(Agent).filter_by(name="atlas").first()
    assert row is not None
    assert row.skip_baseline is False
    session.close()


def test_an_agent_created_under_role_api_is_claimable_by_a_worker(session_factory, config, clients):
    """Guards a contract the worker depends on: under KOBO_ROLE=api the create
    and connect routes used to stamp a heartbeat, and `claim_queued` takes only
    rows without one, so no worker ever provisioned the agent (issue #195)."""
    from kobo_api.provisioner import claim_queued
    from kobo_api.roles import ROLE_API

    api_client = TestClient(create_app(session_factory, config=config, clients=clients, role=ROLE_API))
    response = api_client.post("/api/agents", json={"name": "clio", "persona": PERSONA, "team": TEAM})

    assert response.status_code == 201
    with session_factory() as session:
        clio = session.query(Agent).filter_by(name="clio").one().id
    assert claim_queued(session_factory, limit=5) == [clio]


def test_create_registers_its_pipeline_thread_for_the_drain(client):
    """Issue #202: an inline launch is registered like a queue launch, so
    `drain_pipelines` waits for it at shutdown instead of killing the build."""
    before = len(client.app.state.pipeline_threads)
    client.post("/api/agents", json={"name": AGENT_NAME, "persona": PERSONA, "team": TEAM})
    threads = client.app.state.pipeline_threads
    assert len(threads) == before + 1
    _poll_until(client, AGENT_NAME, {"ready", "failed"})
    threads[-1].join(timeout=5)
    assert not threads[-1].is_alive()
