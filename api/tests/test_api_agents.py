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

import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.clients import Clients
from kobo_api.config import Config
from kobo_api.main import create_app, get_session
from kobo_api.models import Agent, AgentStatus, Base
from tests.fakes import FakeAgent, FakeCloud, FakeGitHub, FakeGojira, FakeKubectl

# fake_storage: reuses conftest.py's shared fixture (seeded with both
# catalogs — the two packaged personas + the sample "shared/word-games"
# skill) rather than redefining it here; only this module's `config`/
# `clients` need their own local fixtures (full platform-client wiring the
# conftest.py `client` fixture doesn't provide).

AGENT_NAME = "clio"
PERSONA = "helpful-generalist"
TEAM = "clio-team"

ALL_STEP_NAMES = ["repo", "onboard", "access", "wire", "build", "deploy", "hello"]

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
    assert all(s["status"] == "done" for s in body["steps"])
    assert all(s["error"] is None for s in body["steps"])
    assert body["first_reply"] == "hello yourself, human"


def test_create_agent_step_labels_match_ui_checklist_vocabulary(client):
    client.post("/api/agents", json={"name": AGENT_NAME, "persona": PERSONA, "team": TEAM})

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
    assert "error" in response.json()


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
    """HelloStep is the pipeline's last step; a failure there should look
    like any other step failure (agent FAILED, that step's row FAILED with
    the error) rather than a special case — and `first_reply` must stay
    None since `hello()` never returned successfully.
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
    # every step before hello (the last one) completed fine
    assert all(steps_by_name[n]["status"] == "done" for n in ALL_STEP_NAMES if n != "hello")
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


# --- model_label fold-in ------------------------------------------------------------


def test_list_agents_includes_model_label_from_catalog(client, session_factory, config):
    _seed_agent(session_factory, config)

    response = client.get("/api/agents")

    row = response.json()["agents"][0]
    assert row["model"] == config.models[0].id
    assert row["model_label"] == config.models[0].label


def test_get_agent_includes_model_label_from_catalog(client, session_factory, config):
    _seed_agent(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}")

    body = response.json()
    assert body["model"] == config.models[0].id
    assert body["model_label"] == config.models[0].label


def test_list_agents_model_label_falls_back_to_raw_id_when_not_in_catalog(
    client, session_factory, config
):
    _seed_agent(session_factory, config, model_id="ancient-model-no-longer-offered")

    response = client.get("/api/agents")

    row = response.json()["agents"][0]
    assert row["model"] == "ancient-model-no-longer-offered"
    assert row["model_label"] == "ancient-model-no-longer-offered"


def test_get_agent_model_label_falls_back_to_raw_id_when_not_in_catalog(
    client, session_factory, config
):
    _seed_agent(session_factory, config, model_id="ancient-model-no-longer-offered")

    response = client.get(f"/api/agents/{AGENT_NAME}")

    body = response.json()
    assert body["model"] == "ancient-model-no-longer-offered"
    assert body["model_label"] == "ancient-model-no-longer-offered"


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
