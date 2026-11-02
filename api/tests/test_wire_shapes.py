"""Wire shapes the UI, assistants over MCP and other rooms read from the API.

Agent list and detail, the create flow's statuses, whoami, the colleague
feed, the picker, the Slack block and manifest, the admin integration rows
and the gateway files they land in, the MCP tool payloads and annotations,
the Activity table, metrics. If a field here changes, something outside
this repository breaks. Folded on 2026-10-29.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import jwt
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from mcp.server.fastmcp import Context
from mcp.shared.context import RequestContext
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api import admin_store, bootstrap, model_providers
from kobo_api import audit as room_audit
from kobo_api.bootstrap import render_org_yaml
from kobo_api.bundle_publish import bundle_reference
from kobo_api.clients import ClientError, Clients, _build_learnings_envelope
from kobo_api.config import Config, GroupChoice, MCPConfig, ModelChoice
from kobo_api.directory import sync_directory
from kobo_api.health import probe_health
from kobo_api.iap import ASSERTION_HEADER, IAP_ISSUER
from kobo_api.kinds import KIND_COMPOSED
from kobo_api.liminal_admin import GatewayGrant
from kobo_api.main import create_app
from kobo_api.mcp import audit, authz, rate_limit
from kobo_api.mcp.jwt import ACCESS_TTL_S, mint, signing_key
from kobo_api.mcp.server import build_mcp_server
from kobo_api.mcp.tools.write import _endpoint
from kobo_api.model_providers import (
    REASON_GATEWAY_DOWN,
    REASON_NOT_CONNECTED,
)
from kobo_api.models import A2AInvite, Agent, AgentLearning, AgentStatus, Base
from kobo_api.org import ORG_KEYS, load_org_config
from kobo_api.personas import (
    PersonaFields,
    create_persona,
    render_persona,
    seed_default_personas,
)
from kobo_api.purpose_tags import encode_tags
from kobo_api.repo_search import RepoIndex
from kobo_api.slack_state import encode_channels
from kobo_api.steps import (
    _colleague_roster,
    _render_colleagues_md,
    refresh_colleague_rosters,
)
from kobo_api.tooling import list_tooling
from tests.fakes import (
    FAKE_DEPLOYMENT_STATUS_SETTLED,
    FAKE_LOCAL_HEAD_DEFAULT,
    FakeAgent,
    FakeBab,
    FakeCabal,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
    FakeModelsGateway,
    FakeName,
    FakePeopleSearch,
    FakeSero,
    FakeSlack,
    FakeStorage,
)

# --- from test_api_agents.py ---


AGENT_NAME = "clio"


PERSONA = "helpful-generalist"


TEAM = "clio-team"


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
def session_factory__api_agents(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__api_agents(tmp_path):
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
def client__api_agents(session_factory__api_agents, config__api_agents, clients):
    return TestClient(create_app(session_factory__api_agents, config=config__api_agents, clients=clients))


def _poll_until(client__api_agents, name, statuses, deadline_s=POLL_DEADLINE_S):
    """Poll GET /api/agents/{name} until its status lands in `statuses`;
    fail with a clear message if `deadline_s` real (wall-clock) seconds pass
    before that happens — the pipeline itself never really sleeps (see
    `_no_real_sleep` above), so on a healthy run this returns almost
    immediately.
    """
    deadline = time.monotonic() + deadline_s
    body = None
    while time.monotonic() < deadline:
        response = client__api_agents.get(f"/api/agents/{name}")
        assert response.status_code == 200
        body = response.json()
        if body["status"] in statuses:
            return body
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"agent {name!r} did not reach {statuses} within {deadline_s}s: {body}")


def _seed_agent(session_factory__api_agents, config__api_agents, *, status=AgentStatus.READY, model_id=None):
    """Insert an Agent row directly, bypassing the pipeline — chat/label tests
    only care about the row's status/model_id, not how it got there."""
    session = session_factory__api_agents()
    session.add(
        Agent(
            name=AGENT_NAME,
            persona=PERSONA,
            model_id=model_id if model_id is not None else config__api_agents.models[0].id,
            status=status,
        )
    )
    session.commit()
    session.close()


CABAL_ENDPOINT_TEMPLATE = "http://agent-{name}.default.svc.cluster.local"


def _enabled_cabal_config(config__api_agents):
    return config__api_agents.model_copy(
        update={"cabal_url": "http://cabal.internal", "agent_endpoint_template": CABAL_ENDPOINT_TEMPLATE}
    )


def _client_with_cabal(
    session_factory__api_agents, config__api_agents, fake_github, fake_gojira, fake_agent, cabal, *, kubectl=None
):
    return TestClient(
        create_app(
            session_factory__api_agents,
            config=config__api_agents,
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


def test_create_agent_returns_201_pending_then_provisions_to_ready(client__api_agents):
    """Wire shape: Create agent returns 201 pending then provisions to ready."""
    response = client__api_agents.post(
        "/api/agents",
        json={"name": AGENT_NAME, "persona": PERSONA, "team": TEAM, "skills": [], "model": None},
    )

    assert response.status_code == 201
    assert response.json() == {"name": AGENT_NAME, "status": "pending"}

    body = _poll_until(client__api_agents, AGENT_NAME, {"ready", "failed"})
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


def test_list_agents_returns_name_status_model_created_at(client__api_agents, config__api_agents):
    """Wire shape: List agents returns name status model created at."""
    client__api_agents.post(
        "/api/agents",
        json={"name": AGENT_NAME, "persona": PERSONA, "team": TEAM, "model": None},
    )

    response = client__api_agents.get("/api/agents")

    assert response.status_code == 200
    agents = response.json()["agents"]
    assert len(agents) == 1
    row = agents[0]
    assert row["name"] == AGENT_NAME
    assert row["status"] in {"pending", "provisioning", "ready"}
    assert row["model"] == config__api_agents.models[0].id
    assert isinstance(row["created_at"], str) and row["created_at"]
    # The card's description line reads these off the list, not a per-card fetch.
    assert row["persona"] == PERSONA
    assert "capability" in row


def test_timestamps_leave_the_api_as_utc_with_a_z(client__api_agents, session_factory__api_agents, config__api_agents):
    """Wire shape: a naive UTC datetime in a row reaches the UI as RFC 3339 with a Z (issue #306)."""
    session = session_factory__api_agents()
    session.add(Agent(name=AGENT_NAME, persona=PERSONA, model_id=config__api_agents.models[0].id,
                      status=AgentStatus.READY, created_at=datetime(2026, 10, 29, 21, 7)))
    session.commit()
    session.close()

    row = client__api_agents.get("/api/agents").json()["agents"][0]

    assert row["created_at"] == "2026-10-29T21:07:00Z"


def test_list_agents_marks_an_agent_the_room_composed(client__api_agents):
    """Wire shape: List agents marks an agent the room composed.

    `kind` rides the LIST as well as the detail, the same reason
    `archetype` does: the dashboard card carries the marker, and a
    per-agent fetch to learn it would be one round trip per card."""
    client__api_agents.post("/api/agents", json={"name": AGENT_NAME, "persona": PERSONA, "team": TEAM})

    response = client__api_agents.get("/api/agents")

    assert response.json()["agents"][0]["kind"] == "composed"


def test_list_agents_exposes_created_by_and_group_name(client__api_agents, session_factory__api_agents, config__api_agents):
    """Wire shape: List agents exposes created by and group name.

    Access-control Task A3: the owner-or-operator gate and the card's
    "Made by <email>" line both need `created_by` (plus `group_name`, for
    Task B2's visibility filter) actually on the wire -- A2 stored both
    columns but deliberately left them off `GET /api/agents`."""
    session = session_factory__api_agents()
    session.add(
        Agent(
            name=AGENT_NAME,
            persona=PERSONA,
            model_id=config__api_agents.models[0].id,
            status=AgentStatus.READY,
            created_by="owner@example.com",
            group_name="payments-eng",
        )
    )
    session.commit()
    session.close()

    row = client__api_agents.get("/api/agents").json()["agents"][0]
    assert row["created_by"] == "owner@example.com"
    assert row["group_name"] == "payments-eng"


def test_chat_happy_path_returns_reply_and_forwards_exact_messages(
    client__api_agents, session_factory__api_agents, config__api_agents, fake_agent
):
    """Wire shape: Chat happy path returns reply and forwards exact messages."""
    _seed_agent(session_factory__api_agents, config__api_agents)
    messages = [
        {"role": "user", "text": "hi"},
        {"role": "agent", "text": "hello yourself, human"},
        {"role": "user", "text": "how are you?"},
    ]

    response = client__api_agents.post(f"/api/agents/{AGENT_NAME}/chat", json={"messages": messages})

    assert response.status_code == 200
    assert response.json() == {"reply": "hello yourself, human"}
    assert len(fake_agent.converse_calls) == 1
    namespace, service, recorded = fake_agent.converse_calls[0]
    assert namespace == AGENT_NAME
    assert service == AGENT_NAME
    assert recorded == messages


def test_chat_relay_envelope_matches_the_shared_builder_verbatim(
    session_factory__api_agents, config__api_agents, fake_github, fake_gojira, fake_agent, monkeypatch
):
    """Wire shape: Chat relay envelope matches the shared builder verbatim.

    The relay path must reuse `_build_task_envelope` (kobo_api.clients)
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
    cfg = _enabled_cabal_config(config__api_agents)
    fake_cabal = FakeCabal(relay_result={"status": {"message": {"parts": [{"text": "ok"}]}}})
    client__api_agents = _client_with_cabal(
        session_factory__api_agents, cfg, fake_github, fake_gojira, fake_agent, fake_cabal
    )
    _seed_agent(session_factory__api_agents, cfg)
    messages = [
        {"role": "user", "text": "hi"},
        {"role": "agent", "text": "hello yourself"},
        {"role": "user", "text": "how are you?"},
    ]

    client__api_agents.post(f"/api/agents/{AGENT_NAME}/chat", json={"messages": messages})

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
    session_factory__api_agents,
    config__api_agents,
    fake_github,
    fake_gojira,
    fake_agent,
    monkeypatch,
    relay_result,
    expected_reply,
):
    """Wire shape: Chat relay reply extraction variants."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_cabal_config(config__api_agents)
    fake_cabal = FakeCabal(relay_result=relay_result)
    client__api_agents = _client_with_cabal(
        session_factory__api_agents, cfg, fake_github, fake_gojira, fake_agent, fake_cabal
    )
    _seed_agent(session_factory__api_agents, cfg)

    response = client__api_agents.post(
        f"/api/agents/{AGENT_NAME}/chat", json={"messages": [{"role": "user", "text": "hi"}]}
    )

    assert response.status_code == 200
    assert response.json() == {"reply": expected_reply}


# --- from test_api_runtime.py ---


AGENT_NAME = "clio"


PERSONA = "helpful-generalist"


REPO = "acme/agent-clio"


PERSONAS_BUCKET = "personas-bucket"


# A second ref under the org default's own registry root — the per-agent
# pin this route exists to set. Derived from the default rather than
# written out, because the root is the whole rule.
OTHER_RUNTIME = Config().runtime_image.rsplit("/", 1)[0] + "/daedalus-runtime:v9"


@pytest.fixture
def session_factory__api_runtime(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__api_runtime(tmp_path):
    return Config(workdir=tmp_path / "work", personas_bucket=PERSONAS_BUCKET)


def _make_client(session_factory__api_runtime, config__api_runtime, *, github=None, cabal=None):
    storage = FakeStorage()
    seed_default_personas(storage, bucket=PERSONAS_BUCKET)
    return TestClient(
        create_app(
            session_factory__api_runtime,
            config=config__api_runtime,
            clients=Clients(
                github=github if github is not None else FakeGitHub(),
                gojira=FakeGojira(),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=FakeKubectl(),
                storage=storage,
                name=FakeName(),
                cabal=cabal if cabal is not None else FakeCabal(),
            ),
        )
    )


def _seed_agent__api_runtime(session_factory__api_runtime, config__api_runtime, *, status=AgentStatus.READY, runtime_image=None):
    session = session_factory__api_runtime()
    session.add(
        Agent(
            name=AGENT_NAME,
            persona=PERSONA,
            model_id=config__api_runtime.models[0].id,
            status=status,
            repo_full_name=REPO,
            team="shared",
            skills=[],
            runtime_image=runtime_image,
        )
    )
    session.commit()
    session.close()


def test_get_agent_reports_its_own_runtime_image_and_the_room_s(session_factory__api_runtime, config__api_runtime):
    """Wire shape: Get agent reports its own runtime image and the room s."""
    client = _make_client(session_factory__api_runtime, config__api_runtime)
    _seed_agent__api_runtime(session_factory__api_runtime, config__api_runtime, runtime_image=OTHER_RUNTIME)

    body = client.get(f"/api/agents/{AGENT_NAME}").json()

    assert body["runtime_image"] == OTHER_RUNTIME
    assert body["room_runtime_image"] == config__api_runtime.runtime_image
    # The whole point of carrying both: this agent is behind.
    assert body["runtime_image"] != body["room_runtime_image"]
    assert body["runtime_pinned"] is True


def test_get_agent_reads_behind_from_what_it_is_built_on_not_from_its_choice(session_factory__api_runtime, config__api_runtime):
    """Wire shape (2026-11-01): an agent that follows the room (no runtime of
    its own) and was built on the runtime the room pinned yesterday reads as
    behind once the room's pin moves: `runtime_image` is the built fact, not
    the room's pin echoed back, so the two halves differ and the page can say
    so. Before this the page compared the pin with itself."""
    client = _make_client(session_factory__api_runtime, config__api_runtime)
    _seed_agent__api_runtime(session_factory__api_runtime, config__api_runtime)
    with session_factory__api_runtime() as session:
        session.query(Agent).filter_by(name=AGENT_NAME).one().built_runtime_image = OTHER_RUNTIME
        session.commit()

    body = client.get(f"/api/agents/{AGENT_NAME}").json()

    assert body["runtime_image"] == OTHER_RUNTIME
    assert body["built_runtime_image"] == OTHER_RUNTIME
    assert body["room_runtime_image"] == config__api_runtime.runtime_image
    assert body["runtime_pinned"] is False


def test_get_agent_says_its_runtime_is_unknown_until_the_room_has_read_it(session_factory__api_runtime, config__api_runtime):
    """Wire shape (2026-11-01): a row the boot backfill has not reached serves
    `built_runtime_image` null (the page says it is checking) and keeps the old
    resolved value under `runtime_image` so nothing older breaks."""
    client = _make_client(session_factory__api_runtime, config__api_runtime)
    _seed_agent__api_runtime(session_factory__api_runtime, config__api_runtime)

    body = client.get(f"/api/agents/{AGENT_NAME}").json()

    assert body["built_runtime_image"] is None
    assert body["runtime_image"] == config__api_runtime.runtime_image
    assert body["runtime_pinned"] is False


# --- from test_api_org_status.py ---


WHOAMI_AUDIENCE = "test-audience"


WHOAMI_KID = "test-key-1"


def _generate_jwk(kid: str = WHOAMI_KID):
    private_key = ec.generate_private_key(ec.SECP256R1())
    jwk = jwt.algorithms.ECAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk["kid"] = kid
    jwk["use"] = "sig"
    jwk["alg"] = "ES256"
    return private_key, jwk


def _sign_whoami(private_key, *, email: str | None, kid: str = WHOAMI_KID) -> str:
    now = int(time.time())
    claims = {
        "aud": WHOAMI_AUDIENCE,
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


def _iap_client(config: Config, tmp_path, *, jwk) -> TestClient:
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    return TestClient(
        create_app(
            session_factory,
            config=config,
            iap_jwks_fetcher=_fetcher(jwk),
        )
    )


def test_whoami_with_iap_operator_email_reports_email_and_operator_true(tmp_path):
    """Wire shape: Whoami with iap operator email reports email and operator true."""
    private_key, jwk = _generate_jwk()
    config = Config(
        workdir=tmp_path / "work",
        iap_audience=WHOAMI_AUDIENCE,
        operators=["ops@example.com"],
    )
    client = _iap_client(config, tmp_path, jwk=jwk)

    token = _sign_whoami(private_key, email="ops@example.com")
    response = client.get("/api/whoami", headers={ASSERTION_HEADER: token})

    assert response.status_code == 200
    assert response.json() == {
        "email": "ops@example.com",
        "operator": True,
        "groups": [],
        "groups_unavailable": False,
        "admin_panel": False,
        "logout_url": None,
        "access_on": False,
        # No GitHub app from the room or the deployment config: the agent's
        # Repositories card greys out on this flag (#304).
        "github_available": False,
        # shared-group + group-requests wave: an admin's whoami also carries
        # the pending-queue badge count (kobo_api.main.whoami) -- zero here
        # since this test seeds no GroupRequest rows.
        "pending_group_requests": 0,
    }


# --- from test_api_people.py ---


@pytest.fixture
def session_factory__api_people(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__api_people(tmp_path):
    return Config(workdir=tmp_path / "work")


def _make_client__api_people(session_factory__api_people, config__api_people, *, people_search=None):
    return TestClient(
        create_app(
            session_factory__api_people,
            config=config__api_people,
            clients=Clients(
                github=FakeGitHub(),
                gojira=FakeGojira(),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=FakeKubectl(),
                people_search=people_search,
            ),
        )
    )


def test_search_happy_path_returns_capped_people_shape(session_factory__api_people, config__api_people):
    """Wire shape: Search happy path returns capped people shape."""
    fake = FakePeopleSearch(result=[{"name": "Alice Smith", "email": "alice@example.com"}])
    client = _make_client__api_people(session_factory__api_people, config__api_people, people_search=fake)

    response = client.get("/api/people/search", params={"q": "ali"})

    assert response.status_code == 200
    assert response.json() == {
        "people": [{"name": "Alice Smith", "email": "alice@example.com"}]
    }
    assert fake.search_calls == ["ali"]


# --- from test_api_colleagues.py ---


ENDPOINT_TEMPLATE = "http://agent-{name}.default.svc.cluster.local"


@pytest.fixture
def session_factory__api_colleagues(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__api_colleagues(tmp_path):
    return Config(workdir=tmp_path / "work")


def _enabled_config(config__api_colleagues):
    return config__api_colleagues.model_copy(
        update={
            "cabal_url": "http://cabal.internal",
            "agent_endpoint_template": ENDPOINT_TEMPLATE,
        }
    )


def _make_client__api_colleagues(session_factory__api_colleagues, config__api_colleagues, *, cabal=None):
    return TestClient(
        create_app(
            session_factory__api_colleagues,
            config=config__api_colleagues,
            clients=Clients(
                github=FakeGitHub(),
                gojira=FakeGojira(),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=FakeKubectl(),
                cabal=cabal,
            ),
        )
    )


def _seed_agent__api_colleagues(session_factory__api_colleagues, config__api_colleagues, name: str = "ward", **kwargs) -> None:
    session = session_factory__api_colleagues()
    session.add(
        Agent(
            name=name,
            persona="helpful-generalist",
            model_id=config__api_colleagues.models[0].id,
            status=AgentStatus.READY,
            repo_full_name=f"acme/agent-{name}",
            **kwargs,
        )
    )
    session.commit()
    session.close()


def _task_result(*, ask_text: str, reply_text: str) -> dict:
    """A completed a2a Task body shaped like a REAL daedalus agent's persisted
    `result_json` (the shape `daedalus.transports.a2a.task_to_a2a` emits and
    the switchboard stores): `status.message` is the reply, and `history`
    holds the FULL transcript -- the user's question AND the agent's reply,
    in order. NOTE the earlier fixture modeled only cabal's builtin EchoAgent
    (history = [user] alone), where `history[-1]` was the ask; every real
    colleague (a daedalus agent) persists [user, agent], so `history[-1]` is
    the ANSWER, not the ask. The feed must take the ask from the user-role
    message, not blindly from `history[-1]`."""
    return {
        "id": "irrelevant-to-the-feed",
        "status": {
            "state": "completed",
            "message": {"role": "agent", "parts": [{"kind": "text", "text": reply_text}]},
        },
        "history": [
            {"role": "user", "parts": [{"kind": "text", "text": ask_text}]},
            {"role": "agent", "parts": [{"kind": "text", "text": reply_text}]},
        ],
    }


def _feed_summary(task_id: str, *, target: str, caller: str) -> dict:
    return {
        "task_id": task_id,
        "target_agent": target,
        "target_generation": 1,
        "caller_agent": caller,
        "state": "completed",
        "cabal_escalated_from": None,
        "created_at": "unused-by-the-feed-route",
        "updated_at": "unused-by-the-feed-route",
        "result_snippet": "unused-by-the-feed-route",
    }


def _feed_detail(
    task_id: str,
    *,
    target: str,
    caller: str,
    created_at: str,
    updated_at: str,
    ask_text: str,
    reply_text: str,
) -> dict:
    return {
        "conversation": {
            "task_id": task_id,
            "target_agent": target,
            "caller_agent": caller,
            "created_at": created_at,
            "updated_at": updated_at,
            "result": _task_result(ask_text=ask_text, reply_text=reply_text),
        },
        "events": [],
    }


def test_feed_flattens_one_conversation_into_ask_then_reply(
    session_factory__api_colleagues, config__api_colleagues, monkeypatch
):
    """Wire shape: Feed flattens one conversation into ask then reply."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config__api_colleagues)
    _seed_agent__api_colleagues(session_factory__api_colleagues, cfg)
    summaries = [_feed_summary("t-1", target="network", caller="agent:ward")]
    detail = _feed_detail(
        "t-1",
        target="network",
        caller="agent:ward",
        created_at="2026-08-19T10:00:00Z",
        updated_at="2026-08-19T10:00:05Z",
        ask_text="What's the config?",
        reply_text="Here's the config.",
    )
    fake_cabal = FakeCabal(conversations_result=summaries, conversation_results={"t-1": detail})
    client = _make_client__api_colleagues(session_factory__api_colleagues, cfg, cabal=fake_cabal)

    response = client.get("/api/colleagues/feed", params={"agent": "ward"})

    assert response.status_code == 200
    assert response.json() == {
        "entries": [
            {
                "ts": "2026-08-19T10:00:00Z",
                "conversation_id": "t-1",
                "from": "ward",
                "to": "network",
                "role": "ask",
                "text": "What's the config?",
            },
            {
                "ts": "2026-08-19T10:00:05Z",
                "conversation_id": "t-1",
                "from": "network",
                "to": "ward",
                "role": "reply",
                "text": "Here's the config.",
            },
        ],
        "truncated": False,
    }
    # Fix round 2: TWO server-side-scoped queries (the switchboard's
    # omitted-limit default was 50, never unbounded) -- see
    # `colleague_feed`'s own docstring.
    assert fake_cabal.list_conversations_calls[-2:] == [
        {"limit": 200, "target": "ward", "caller": None},
        {"limit": 200, "target": None, "caller": "agent:ward"},
    ]


# --- from test_api_requestable.py ---


PERSONA = "helpful-generalist"


REPO_TEMPLATE = "acme/agent-{name}"


OWNER_A_EMAIL = "owner-a@example.com"


OWNER_B_EMAIL = "owner-b@example.com"


OPERATOR_EMAIL = "ops@example.com"


AUDIENCE = "test-audience"


KID = "test-key-1"


def _generate_jwk__api_requestable(kid: str = KID):
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


def _headers(private_key, *, email: str) -> dict:
    return {ASSERTION_HEADER: _sign(private_key, email=email)}


@pytest.fixture
def session_factory__api_requestable(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__api_requestable(tmp_path):
    return Config(workdir=tmp_path / "work", operators=[OPERATOR_EMAIL], iap_audience=AUDIENCE)


def _make_client__api_requestable(session_factory__api_requestable, config__api_requestable, iap_jwks_fetcher):
    return TestClient(
        create_app(
            session_factory__api_requestable,
            config=config__api_requestable,
            clients=Clients(
                github=FakeGitHub(),
                gojira=FakeGojira(),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=FakeKubectl(),
            ),
            iap_jwks_fetcher=iap_jwks_fetcher,
        )
    )


def _seed_agent__api_requestable(
    session_factory__api_requestable,
    *,
    name,
    created_by=None,
    group_name=None,
    team=None,
    capability=None,
    purpose_tags=None,
    status=AgentStatus.READY,
):
    from kobo_api.purpose_tags import encode_tags

    session = session_factory__api_requestable()
    session.add(
        Agent(
            name=name,
            persona=PERSONA,
            model_id="vertex_ai/claude-sonnet-4-5",
            status=status,
            repo_full_name=REPO_TEMPLATE.format(name=name),
            created_by=created_by,
            group_name=group_name,
            team=team,
            capability=capability,
            purpose_tags=encode_tags(purpose_tags) if purpose_tags is not None else None,
        )
    )
    session.commit()
    session.close()


@pytest.fixture
def client__api_requestable(session_factory__api_requestable, config__api_requestable):
    private_key, jwk = _generate_jwk__api_requestable()
    c = _make_client__api_requestable(session_factory__api_requestable, config__api_requestable, _fetcher(jwk))
    return c, private_key


def test_requestable_entry_shape_is_capped_to_five_fields(session_factory__api_requestable, client__api_requestable):
    """Wire shape: Requestable entry shape is capped to five fields."""
    c, key = client__api_requestable
    _seed_agent__api_requestable(session_factory__api_requestable, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent__api_requestable(
        session_factory__api_requestable,
        name="b",
        created_by=OWNER_B_EMAIL,
        group_name="payments-eng",
        team="payments",
        capability="Handles refund inquiries.",
        purpose_tags=["billing", "refunds"],
    )

    response = c.get("/api/agents/a/requestable", headers=_headers(key, email=OWNER_A_EMAIL))

    assert response.status_code == 200
    entry = response.json()["requestable"][0]
    assert entry == {
        "name": "b",
        "group": "payments-eng",
        "team": "payments",
        "capability": "Handles refund inquiries.",
        "purpose_tags": ["billing", "refunds"],
    }


def test_requestable_names_each_teams_group_and_the_one_it_leaves_out(session_factory__api_requestable, client__api_requestable):
    """Wire shape: the picker groups by team, so an `already` row carries its
    `group`, a not-ready colleague in the viewer's own group is left out like
    a ready one, and `paired_group` names that group."""
    c, key = client__api_requestable
    _seed_agent__api_requestable(session_factory__api_requestable, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent__api_requestable(session_factory__api_requestable, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")
    _seed_agent__api_requestable(
        session_factory__api_requestable, name="c", created_by=OWNER_A_EMAIL, group_name="growth", status=AgentStatus.PROVISIONING
    )
    _seed_agent__api_requestable(
        session_factory__api_requestable, name="d", created_by=OWNER_B_EMAIL, group_name="security", status=AgentStatus.PROVISIONING
    )
    session = session_factory__api_requestable()
    session.add(A2AInvite(from_agent="a", to_agent="b", status="pending", requested_by=OWNER_A_EMAIL))
    session.commit()
    session.close()

    body = c.get("/api/agents/a/requestable", headers=_headers(key, email=OWNER_A_EMAIL)).json()

    assert body["already"] == [{"name": "b", "group": "payments-eng", "status": "pending"}]
    assert [(e["name"], e["group"]) for e in body["not_ready"]] == [("d", "security")]
    assert body["paired_group"] == "growth"


# --- from test_api_threads.py ---


PERSONA = "helpful-generalist"


MODEL_ID = "vertex_ai/claude-sonnet-4-5"


REPO_TEMPLATE = "acme/agent-{name}"


ENDPOINT_TEMPLATE = "http://agent-{name}.default.svc.cluster.local"


OPERATOR_EMAIL = "ops@example.com"


INITIATOR_EMAIL = "initiator@example.com"


AUDIENCE = "test-audience"


KID = "test-key-1"


GROUPS = [
    GroupChoice(name="growth", okta_group="okta-growth"),
    GroupChoice(name="payments-eng", okta_group="okta-payments"),
]


CANNED_REPLY_TEXT = "Here's my take."


class FakeGroupResolver:
    """The plainest possible stand-in for `GroupResolver` (kobo_api.groups),
    lifted verbatim from test_web_visibility.py's own copy: a bare object
    exposing `groups_for(email) -> list[str] | None`, nothing more."""

    def __init__(self, mapping: dict[str, list[str]] | None = None, *, down: bool = False):
        self._mapping = dict(mapping) if mapping else {}
        self._down = down

    def groups_for(self, email: str) -> list[str] | None:
        if self._down:
            return None
        return self._mapping.get(email.lower(), [])


def _generate_jwk__api_threads(kid: str = KID):
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


def _headers(private_key, *, email: str) -> dict:
    return {ASSERTION_HEADER: _sign(private_key, email=email)}


@pytest.fixture
def session_factory__api_threads(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__api_threads(tmp_path):
    return Config(
        workdir=tmp_path / "work",
        operators=[OPERATOR_EMAIL],
        iap_audience=AUDIENCE,
        groups=GROUPS,
        cabal_url="http://cabal.internal",
        agent_endpoint_template=ENDPOINT_TEMPLATE,
    )


def _make_client__api_threads(
    session_factory__api_threads,
    config__api_threads,
    *,
    groups_resolver=None,
    cabal=None,
    iap_jwks_fetcher,
) -> TestClient:
    return TestClient(
        create_app(
            session_factory__api_threads,
            config=config__api_threads,
            clients=Clients(
                github=FakeGitHub(),
                gojira=FakeGojira(),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=FakeKubectl(),
                groups=groups_resolver,
                cabal=cabal,
            ),
            iap_jwks_fetcher=iap_jwks_fetcher,
        )
    )


def _seed_agent__api_threads(
    session_factory__api_threads,
    *,
    name: str,
    group_name: str | None = None,
    status: str = AgentStatus.READY,
    created_by: str | None = None,
) -> None:
    session = session_factory__api_threads()
    session.add(
        Agent(
            name=name,
            persona=PERSONA,
            model_id=MODEL_ID,
            status=status,
            repo_full_name=REPO_TEMPLATE.format(name=name),
            created_by=created_by,
            group_name=group_name,
        )
    )
    session.commit()
    session.close()


def _relay_result(text: str) -> dict:
    return {
        "status": {
            "state": "completed",
            "message": {"role": "agent", "parts": [{"kind": "text", "text": text}]},
        },
    }


def test_turn_relay_envelope_context_id_equals_the_thread_id(
    session_factory__api_threads, config__api_threads, monkeypatch
):
    """Wire shape: Turn relay envelope context id equals the thread id.

    Every relay for a thread passes the thread's own id as `context_id`
    to `_build_task_envelope`, so the switchboard's loop guard groups a
    thread's hops under one real key."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    private_key, jwk = _generate_jwk__api_threads()
    resolver = FakeGroupResolver({INITIATOR_EMAIL: ["growth"]})
    fake_cabal = FakeCabal(relay_result=_relay_result(CANNED_REPLY_TEXT))
    client = _make_client__api_threads(
        session_factory__api_threads,
        config__api_threads,
        groups_resolver=resolver,
        cabal=fake_cabal,
        iap_jwks_fetcher=_fetcher(jwk),
    )
    _seed_agent__api_threads(session_factory__api_threads, name="a", group_name="growth")
    _seed_agent__api_threads(session_factory__api_threads, name="b", group_name="growth")
    thread_id = client.post(
        "/api/threads",
        json={"agent": "a"},
        headers=_headers(private_key, email=INITIATOR_EMAIL),
    ).json()["id"]
    client.post(
        f"/api/threads/{thread_id}/participants",
        json={"agent": "b"},
        headers=_headers(private_key, email=INITIATOR_EMAIL),
    )

    client.post(
        f"/api/threads/{thread_id}/turns",
        json={"messages": [{"role": "user", "text": "hello both"}], "broadcast": True},
        headers=_headers(private_key, email=INITIATOR_EMAIL),
    )

    assert len(fake_cabal.relay_calls) == 2
    for call in fake_cabal.relay_calls:
        assert call["task_envelope"]["contextId"] == str(thread_id)


# --- from test_api_slack.py ---


AGENT_NAME = "clio"


PERSONA = "helpful-generalist"


PERSONA_DESCRIPTION = "A friendly, plain-spoken generalist for everyday questions, drafts and quick research."


REPO = "acme/agent-clio"


APP_ID = "A0EXISTING"


BOT_TOKEN = "xoxb-super-secret-bot-token"


APP_TOKEN = "xapp-super-secret-app-token"


CHANNEL = "C1234567"


# FakeGitHub.clone only writes bundle/agent.yaml when agent_yaml_transports
# is given; every /connect test needs the file on disk for wire_slack to
# edit, so _CapturingGitHub below defaults it (same fix/idiom as
# test_slack_setup.py's own _CapturingGitHub).
_DEFAULT_AGENT_YAML_TRANSPORTS = [{"kind": "a2a", "host": "0.0.0.0", "port": 8080}]


class _CapturingGitHub(FakeGitHub):
    """Snapshots bundle/agent.yaml and gojira.yaml content at the moment
    commit_push is called — the only window in which wire_slack's cloned
    workdir still exists before it's discarded. Same trick as
    test_api_knowledge.py's/test_slack_setup.py's own _CapturingGitHub,
    duplicated locally rather than imported cross-file."""

    def __init__(self, agent_yaml_transports=None, **kwargs):
        if agent_yaml_transports is None:
            agent_yaml_transports = _DEFAULT_AGENT_YAML_TRANSPORTS
        super().__init__(agent_yaml_transports=agent_yaml_transports, **kwargs)
        self.captured_agent_yaml = ""
        self.captured_gojira_yaml = ""
        self.captured_dockerfile = ""

    def commit_push(self, workdir: Path, message: str) -> bool:
        self.captured_agent_yaml = (workdir / "bundle" / "agent.yaml").read_text()
        self.captured_gojira_yaml = (workdir / "gojira.yaml").read_text()
        self.captured_dockerfile = (workdir / "Dockerfile").read_text()
        return super().commit_push(workdir, message)


@pytest.fixture(autouse=True)
def _no_real_sleep__api_slack(monkeypatch):
    """wire_slack's wait_active polls gojira in a loop; never let it sleep
    for real (same discipline as test_slack_setup.py's own fixture)."""
    import kobo_api.slack_setup as slack_setup_module

    monkeypatch.setattr(slack_setup_module, "_sleep", lambda seconds: None)


@pytest.fixture
def session_factory__api_slack(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__api_slack(tmp_path):
    # Task 5: create_slack_app's persona_first_line lookup now reads the
    # bucket catalog (via search.get_entry) instead of the old local-clone
    # scanner — buckets must be named for that lookup to find anything.
    return Config(
        workdir=tmp_path / "work",
        skills_bucket="skills-bucket",
        personas_bucket="personas-bucket",
    )


# Sentinel distinguishing "slack kwarg not passed" (-> default FakeSlack())
# from an explicit `slack=None` (-> Clients.slack is None, the "Slack isn't
# configured" 503 path) — plain `None` can't serve as both.
_SLACK_UNSET = object()


def _make_client__api_slack(
    session_factory__api_slack,
    config__api_slack,
    *,
    github=None,
    kubectl=None,
    gojira=None,
    slack=_SLACK_UNSET,
    storage=None,
    sero=None,
    iap_jwks_fetcher=None,
):
    if storage is None:
        # Seeded the same way a real bootstrap seeds a fresh org's bucket —
        # gives create_slack_app's persona lookup something real to find
        # ("helpful-generalist") without every call site here needing to set
        # this up itself.
        storage = FakeStorage()
        seed_default_personas(storage, bucket=config__api_slack.personas_bucket)
    return TestClient(
        create_app(
            session_factory__api_slack,
            config=config__api_slack,
            clients=Clients(
                github=github if github is not None else FakeGitHub(),
                gojira=gojira if gojira is not None else FakeGojira(activate_after=1),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=kubectl if kubectl is not None else FakeKubectl(),
                slack=FakeSlack() if slack is _SLACK_UNSET else slack,
                storage=storage,
                # None (the default) is a room with no secrets platform
                # configured: token delivery stays the direct Secret write.
                sero=sero,
            ),
            iap_jwks_fetcher=iap_jwks_fetcher,
        )
    )


def _seed_agent__api_slack(
    session_factory__api_slack,
    config__api_slack,
    *,
    status=AgentStatus.READY,
    repo_full_name=REPO,
    gojira_app_id=None,
    slack_app_id=None,
    slack_channels=None,
    slack_wired=False,
    created_by=None,
):
    session = session_factory__api_slack()
    session.add(
        Agent(
            name=AGENT_NAME,
            persona=PERSONA,
            model_id=config__api_slack.models[0].id,
            status=status,
            repo_full_name=repo_full_name,
            gojira_app_id=gojira_app_id,
            slack_app_id=slack_app_id,
            slack_channels=encode_channels(slack_channels) if slack_channels is not None else None,
            slack_wired=slack_wired,
            created_by=created_by,
        )
    )
    session.commit()
    session.close()


def _onboarded_gojira(**kwargs):
    """A FakeGojira with one already-onboarded app (mirrors a READY agent:
    OnboardStep already ran create_app for it during original provisioning),
    at the cadence connect_slack's wait_active expects (activate_after=1
    unless a test overrides it)."""
    kwargs.setdefault("activate_after", 1)
    gojira = FakeGojira(**kwargs)
    app_id = gojira.create_app(
        name=AGENT_NAME, repo_full_name=REPO, cluster="example-cluster", namespace=AGENT_NAME
    )
    return gojira, app_id


class _GojiraCapturingGitHub(FakeGitHub):
    """Snapshots gojira.yaml at commit_push: the wake route edits only that file."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.captured_gojira: dict | None = None

    def commit_push(self, workdir: Path, message: str) -> bool:
        target = workdir / "gojira.yaml"
        if target.is_file():
            self.captured_gojira = yaml.safe_load(target.read_text())
        return super().commit_push(workdir, message)


def test_slack_manifest_renders_agent_manifest(session_factory__api_slack, config__api_slack):
    """Wire shape: Slack manifest renders agent manifest."""
    client = _make_client__api_slack(session_factory__api_slack, config__api_slack)
    _seed_agent__api_slack(session_factory__api_slack, config__api_slack)

    response = client.get(f"/api/agents/{AGENT_NAME}/slack/manifest")

    assert response.status_code == 200
    manifest = response.json()["manifest"]
    assert manifest["display_information"]["name"] == AGENT_NAME
    assert manifest["display_information"]["description"] == PERSONA_DESCRIPTION
    assert manifest["features"]["bot_user"]["display_name"] == AGENT_NAME
    assert manifest["oauth_config"]["scopes"]["bot"] == [
        "app_mentions:read",
        "channels:history",
        "groups:history",
        "groups:read",
        "channels:read",
        "im:history",
        "im:read",
        "im:write",
        "mpim:history",
        "mpim:read",
        "chat:write",
        "files:read",
        "reactions:read",
        "reactions:write",
    ]


def test_connect_slack_wires_secret_transport_and_secrets_from_returns_sha(
    session_factory__api_slack, config__api_slack
):
    """Wire shape: Connect slack wires secret transport and secrets from returns sha."""
    github = _CapturingGitHub()
    kubectl = FakeKubectl()
    gojira, app_id = _onboarded_gojira()
    client = _make_client__api_slack(session_factory__api_slack, config__api_slack, github=github, kubectl=kubectl, gojira=gojira)
    _seed_agent__api_slack(
        session_factory__api_slack, config__api_slack, gojira_app_id=app_id, slack_app_id=APP_ID,
        slack_channels=[CHANNEL],
    )

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"app_id": APP_ID, "bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
    )

    assert response.status_code == 200
    assert response.json() == {"sha": FAKE_LOCAL_HEAD_DEFAULT}

    # secret delivered via kubectl with both tokens
    assert kubectl.secrets_applied == [
        (
            AGENT_NAME,
            f"{AGENT_NAME}-slack",
            {"SLACK_BOT_TOKEN": BOT_TOKEN, "SLACK_APP_TOKEN": APP_TOKEN},
        )
    ]

    # transport + secrets_from wired into the repo, commit message exact
    agent_yaml = yaml.safe_load(github.captured_agent_yaml)
    assert {"kind": "slack", "channel": CHANNEL} in agent_yaml["transports"]
    gojira_yaml = yaml.safe_load(github.captured_gojira_yaml)
    assert gojira_yaml["secrets_from"] == [f"{AGENT_NAME}-slack"]
    subject, _, note = github.pushes[-1][1].partition("\n\n")
    assert subject == "kobo: connect slack"
    # The fake clone's Dockerfile is on an old image, so the commit re-pins
    # it and says so (issue #140).
    assert note.startswith("Also re-pins the Dockerfile FROM from ")

    # a changed push (the default) triggers a reprovision
    assert gojira.reprovision_calls == [app_id]

    # tokens never land in a file written to the clone
    assert BOT_TOKEN not in github.captured_agent_yaml
    assert APP_TOKEN not in github.captured_agent_yaml
    assert BOT_TOKEN not in github.captured_gojira_yaml
    assert APP_TOKEN not in github.captured_gojira_yaml

    # persisted: slack_wired flips True, state derives to "connected"
    detail = client.get(f"/api/agents/{AGENT_NAME}").json()
    assert detail["slack"] == {
        "app_id": APP_ID, "channels": [CHANNEL], "state": "connected",
        "wake": "mention",
        "available": True,
        # The paste flow's app keeps the Slack tab (issue #371): `legacy`.
        "relay": False,
        "legacy": True,
    }


def test_get_agent_slack_carries_app_id_and_channels_after_connect(session_factory__api_slack, config__api_slack):
    """Wire shape: Get agent slack carries app id and channels after connect."""
    github = _CapturingGitHub()
    gojira, app_id = _onboarded_gojira()
    client = _make_client__api_slack(session_factory__api_slack, config__api_slack, github=github, gojira=gojira)
    _seed_agent__api_slack(session_factory__api_slack, config__api_slack, gojira_app_id=app_id, slack_channels=[CHANNEL])
    # slack_app_id left at its default None: connect is what sets it now.

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"app_id": APP_ID, "bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
    )
    assert response.status_code == 200

    detail = client.get(f"/api/agents/{AGENT_NAME}").json()
    assert detail["slack"] == {
        "app_id": APP_ID, "channels": [CHANNEL], "state": "connected",
        "wake": "mention",
        "available": True,
        # The paste flow's app keeps the Slack tab (issue #371): `legacy`.
        "relay": False,
        "legacy": True,
    }


def test_get_slack_app_view_carries_the_install_link_while_created_and_not_installed(
    session_factory__api_slack, config__api_slack
):
    """Wire shape: the Access tab's Slack section (issue #371) reads one
    view: the state (none, created, installed), the workspace once installed,
    the channels and wake, and the install link only while created."""
    from datetime import datetime

    from kobo_api.models import SlackRelayApp

    relay_config = config__api_slack.model_copy(update={"slack_relay": True})
    client = _make_client__api_slack(session_factory__api_slack, relay_config)
    _seed_agent__api_slack(session_factory__api_slack, relay_config, slack_app_id="A0FAKE", slack_channels=[CHANNEL])
    with session_factory__api_slack() as s:
        s.add(SlackRelayApp(relay_id="relay-id-for-clio-0000", agent_name=AGENT_NAME, app_id="A0FAKE",
                            client_id="fake-client-id", client_secret="fake-client-secret",
                            signing_secret="fake-signing-secret", relay_secret="fake-relay-secret",
                            created_at=datetime(2026, 11, 1)))
        s.commit()

    view = client.get(f"/api/agents/{AGENT_NAME}/slack").json()

    assert view["install_url"].startswith("https://slack.com/oauth/v2/authorize?client_id=fake-client-id")
    assert {k: v for k, v in view.items() if k != "install_url"} == {
        "enabled": True, "available": True, "state": "created", "app_id": "A0FAKE",
        "workspace": None, "app_name": None, "channels": [CHANNEL], "wake": "mention", "legacy": False,
    }
    detail = client.get(f"/api/agents/{AGENT_NAME}").json()
    assert (detail["slack"]["relay"], detail["slack"]["legacy"]) == (True, False)


def test_put_wake_writes_the_env_key_and_says_when_the_manifest_changed(session_factory__api_slack, config__api_slack):
    """Wire shape: Put wake writes the env key, sets it on the running agent
    at once (`applied_now`, 2026-11-01) and says when the manifest changed."""
    gojira, app_id = _onboarded_gojira()
    github = _GojiraCapturingGitHub()
    kubectl = FakeKubectl()
    client = _make_client__api_slack(
        session_factory__api_slack, config__api_slack, github=github, gojira=gojira, kubectl=kubectl
    )
    _seed_agent__api_slack(session_factory__api_slack, config__api_slack, gojira_app_id=app_id)

    # A row from before the choice reads as mention, and the manifest says so.
    assert client.get(f"/api/agents/{AGENT_NAME}").json()["slack"]["wake"] == "mention"
    events = client.get(f"/api/agents/{AGENT_NAME}/slack/manifest").json()["manifest"]["settings"][
        "event_subscriptions"
    ]["bot_events"]
    assert "message.channels" not in events

    response = client.put(f"/api/agents/{AGENT_NAME}/slack/wake", json={"wake": "channel"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["wake"], body["previous_wake"], body["manifest_changed"], body["applied_now"]) == (
        "channel", "mention", True, True
    )
    # The room's own clock, for the page to tell a later health reading apart.
    assert body["applied_at"].endswith("Z")
    assert github.captured_gojira is not None
    assert github.captured_gojira["env"]["SLACK_WAKE_MODE"] == "channel"
    # The live deployment gets the same value, in the agent's own namespace.
    assert kubectl.env_sets == [(AGENT_NAME, AGENT_NAME, "SLACK_WAKE_MODE", "channel")]

    assert client.get(f"/api/agents/{AGENT_NAME}").json()["slack"]["wake"] == "channel"
    events = client.get(f"/api/agents/{AGENT_NAME}/slack/manifest").json()["manifest"]["settings"][
        "event_subscriptions"
    ]["bot_events"]
    assert "message.channels" in events and "message.groups" in events

    # thread <-> channel keeps the same events: nothing to re-apply in Slack.
    again = client.put(f"/api/agents/{AGENT_NAME}/slack/wake", json={"wake": "thread"}).json()
    assert (again["previous_wake"], again["manifest_changed"]) == ("channel", False)


def test_put_wake_says_applied_now_false_when_the_live_apply_fails(session_factory__api_slack, config__api_slack):
    """Wire shape: the commit landed, so a cluster that refuses the live
    change is reported as `applied_now: false` on a 200, never a 5xx."""
    gojira, app_id = _onboarded_gojira()
    client = _make_client__api_slack(
        session_factory__api_slack, config__api_slack, github=_GojiraCapturingGitHub(), gojira=gojira,
        kubectl=FakeKubectl(set_env_error="forbidden"),
    )
    _seed_agent__api_slack(session_factory__api_slack, config__api_slack, gojira_app_id=app_id)

    response = client.put(f"/api/agents/{AGENT_NAME}/slack/wake", json={"wake": "thread"})

    assert (response.status_code, response.json()["applied_now"], response.json()["applied_at"]) == (200, False, None)
    assert client.get(f"/api/agents/{AGENT_NAME}").json()["slack"]["wake"] == "thread"


# --- from test_api_model_change.py ---


AGENT_NAME = "clio"


REPO = "acme/agent-clio"


OLD_MODEL = "vertex_ai/claude-sonnet-4-5"


NEW_MODEL = "vertex_ai/gemini-3.5-flash"


class _CapturingGitHub__api_model_change(FakeGitHub):
    """Reads gojira.yaml at commit_push time, the only window in which the
    caller's cloned workdir still exists."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.captured_gojira: dict | None = None

    def commit_push(self, workdir: Path, message: str) -> bool:
        target = workdir / "gojira.yaml"
        if target.is_file():
            self.captured_gojira = yaml.safe_load(target.read_text())
        return super().commit_push(workdir, message)


def _client(session_factory, config, *, github=None, gojira=None):
    config.models = [
        ModelChoice(id=OLD_MODEL, label="Claude Affordable (Sonnet)"),
        ModelChoice(id=NEW_MODEL, label="Gemini Affordable (Flash)", location="global"),
    ]
    return TestClient(
        create_app(
            session_factory,
            config=config,
            clients=Clients(
                github=github if github is not None else _CapturingGitHub__api_model_change(),
                gojira=gojira if gojira is not None else FakeGojira(),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=FakeKubectl(),
            ),
        )
    )


def _seed_agent__api_model_change(session_factory, *, status=AgentStatus.READY, kind=None, gojira=None):
    # The fake platform only reprovisions an app it created, the way the
    # real one does; the row carries that app's id.
    app_id = (
        gojira.create_app(name=AGENT_NAME, repo_full_name=REPO, cluster="dev", namespace=AGENT_NAME)
        if gojira is not None
        else None
    )
    with session_factory() as session:
        agent = Agent(
            name=AGENT_NAME,
            persona="helpful-generalist",
            model_id=OLD_MODEL,
            status=status,
            repo_full_name=REPO,
            gojira_app_id=app_id,
        )
        if kind is not None:
            agent.kind = kind
        session.add(agent)
        session.commit()
    return app_id


def _model_on_row(session_factory) -> str:
    with session_factory() as session:
        return session.query(Agent).filter_by(name=AGENT_NAME).one().model_id


def test_set_model_writes_the_two_env_keys_reprovisions_and_records_it(session_factory, config):
    """Wire shape: Set model writes the two env keys reprovisions and records it."""
    github = _CapturingGitHub__api_model_change()
    gojira = FakeGojira()
    client = _client(session_factory, config, github=github, gojira=gojira)
    app_id = _seed_agent__api_model_change(session_factory, gojira=gojira)

    response = client.patch(f"/api/agents/{AGENT_NAME}/model", json={"model": NEW_MODEL})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["model"] == NEW_MODEL
    assert body["model_label"] == "Gemini Affordable (Flash)"
    assert body["previous_model"] == OLD_MODEL
    env = (github.captured_gojira or {}).get("env") or {}
    assert env["AGENT_MODEL"] == NEW_MODEL
    assert env["VERTEXAI_LOCATION"] == "global"
    assert github.pushes[-1][1] == "kobo: model is now Gemini Affordable (Flash) [skip ci]"
    assert f"reprovision:{app_id}" in gojira.calls
    assert _model_on_row(session_factory) == NEW_MODEL


# --- from test_api_admin_connections.py ---


GATEWAY_REPO = "example-org/gateway-config"


GRANTS = '[[grant]]\nissuer = "cabal-core"\npersona = "janus"\nscopes = ["github:read"]\nallowed_handlers = ["github", "scratchpad"]\n'


class FakeLiminalAdmin:
    def __init__(self, grants: list[GatewayGrant] | None = None, *, fail: str | None = None):
        self.grants = {(g.issuer, g.persona): g for g in (grants or [])}
        self.writes: list[GatewayGrant] = []
        self.revokes: list[tuple[str, str]] = []
        self._fail = fail
        # Targets through the admin plane (liminal#222): held live, no pull request.
        self.targets: dict[str, dict] = {}

    def list_grants(self) -> list[GatewayGrant]:
        if self._fail:
            raise ClientError(self._fail)
        return list(self.grants.values())

    def grant(self, grant: GatewayGrant) -> None:
        if self._fail:
            raise ClientError(self._fail)
        self.writes.append(grant)
        self.grants[(grant.issuer, grant.persona)] = grant

    def revoke(self, issuer: str, persona: str) -> None:
        self.revokes.append((issuer, persona))
        self.grants.pop((issuer, persona), None)

    def list_targets(self) -> list[dict]:
        return [{"name": n, "source": "live", "status": "ready", "spec": dict(s)} for n, s in self.targets.items()]

    def put_target(self, name: str, spec: dict) -> dict:
        self.targets[name] = dict(spec)
        return {"name": name, "status": "ready"}

    def delete_target(self, name: str) -> dict:
        self.targets.pop(name, None)
        return {"removed": name}


def _client__api_admin_connections(tmp_path, fake_storage, *, github: FakeGitHub, admin, config_overrides=None):
    engine = create_engine(f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    values = {
        "workdir": tmp_path / "work", "iap_audience": WHOAMI_AUDIENCE, "operators": ["ops@example.com"],
        "admin_panel": True, "sero_url": "http://sero.internal", "gateway_config_repo": GATEWAY_REPO,
        "gateway_namespace": "liminal", "liminal_admin_url": "http://liminal-admin.liminal.svc",
    }
    values.update(config_overrides or {})
    private_key, jwk = _generate_jwk()
    clients = Clients(github=github, gojira=FakeGojira(), cloud=FakeCloud(), agent=FakeAgent(),
                      kubectl=FakeKubectl(), storage=fake_storage, sero=FakeSero(), cabal=FakeCabal())
    clients.liminal_admin = admin
    app = create_app(session_factory, config=Config(**values), clients=clients, iap_jwks_fetcher=_fetcher(jwk))
    with session_factory() as s:
        for name, group in (("janus", "platform"), ("sage", "data")):
            s.add(Agent(name=name, persona="helpful-generalist", model_id="vertex_ai/claude-sonnet-4-5", skills=[],
                        status=AgentStatus.READY, repo_full_name=f"example-org/agent-{name}", group_name=group))
        s.commit()
    return TestClient(app), {ASSERTION_HEADER: _sign_whoami(private_key, email="ops@example.com")}


def _connect_datadog(client, headers, base_access="read"):
    r = client.post("/api/admin/integrations", json={"vendor": "datadog", "name": "Datadog", "base_access": base_access,
                    "credential": "dd-token", "address": "https://mcp.datadoghq.com", "trust": "untrusted"}, headers=headers)
    assert r.status_code == 201, r.text


def test_every_agent_is_a_row_and_a_lane_applies_live_and_as_a_pull_request(tmp_path, fake_storage):
    """Wire shape: Every agent is a row and a lane applies live and as a pull request."""
    github = FakeGitHub(existing_repos={GATEWAY_REPO}, extra_clone_files={"grants.toml": GRANTS, "targets.toml": ""}, track_pushed_content=True)
    admin = FakeLiminalAdmin([GatewayGrant("cabal-core", "janus", ("github:read",), ("github", "scratchpad"))])
    client, headers = _client__api_admin_connections(tmp_path, fake_storage, github=github, admin=admin)
    _connect_datadog(client, headers)

    view = client.get("/api/admin/connections", headers=headers).json()
    assert [a["agent"] for a in view["agents"]] == ["janus", "sage"]
    assert view["agents"][0]["lanes"] == [] and view["live"] is True

    r = client.put("/api/admin/connections/janus/datadog", json={"access": "read"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "applied" and body["outcome"]["live"] == "live at the gateway"
    # Live: one write for the persona, merged with what the gateway held.
    [written] = admin.writes
    assert written.persona == "janus"
    assert written.scopes == ("datadog:read", "github:read")
    assert written.allowed_handlers == ("datadog", "github", "scratchpad")
    # Durable: the grants file's janus block is rewritten in a pull request.
    grants = github.pushed_file("grants.toml")
    assert grants.count('persona = "janus"') == 1 and '"datadog:read"' in grants
    assert "pull request open on the gateway" in body["outcome"]["file"]


# --- from test_api_admin_integrations.py ---


GATEWAY_REPO = "example-org/gateway-config"


TARGETS = '[[target]]\nname = "existing"\ntransport = "http"\naddress = "https://old.example"\n'


def _client__api_admin_integrations(tmp_path, fake_storage, config: Config, *, github: FakeGitHub, sero: FakeSero | None):
    engine = create_engine(f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    private_key, jwk = _generate_jwk()
    client = TestClient(
        create_app(
            session_factory,
            config=config,
            clients=Clients(
                github=github, gojira=FakeGojira(), cloud=FakeCloud(), agent=FakeAgent(),
                kubectl=FakeKubectl(), storage=fake_storage, sero=sero,
            ),
            iap_jwks_fetcher=_fetcher(jwk),
        )
    )
    return client, {ASSERTION_HEADER: _sign_whoami(private_key, email="ops@example.com")}


def test_admin_room_names_the_real_cluster_behind_the_deploy_alias(tmp_path, fake_storage):
    """"Deploys to" named the alias gojira registered the cluster under; the panel now
    says where that is (cluster, project, location) when gojira can tell, and falls back
    to the alias when it cannot."""
    client, headers = _client__api_admin_integrations(
        tmp_path, fake_storage, _config(tmp_path, gojira_cluster="dev-alias"), github=FakeGitHub(), sero=None
    )
    gojira = client.app.state.clients.gojira
    gojira.clusters = [{"name": "dev-alias", "gcp_project": "example-project", "location": "us-east4",
                        "gke_cluster": "agents-us-east4", "ar_repo": "x"}]
    body = client.get("/api/admin/room", headers=headers).json()
    assert body["cluster"] == "dev-alias"
    assert body["cluster_detail"] == {"name": "dev-alias", "gcp_project": "example-project",
                                      "location": "us-east4", "gke_cluster": "agents-us-east4"}
    gojira.clusters = []
    assert client.get("/api/admin/room", headers=headers).json()["cluster_detail"] is None


def _config(tmp_path, **overrides) -> Config:
    values = {
        "workdir": tmp_path / "work", "iap_audience": WHOAMI_AUDIENCE, "operators": ["ops@example.com"],
        "admin_panel": True, "sero_url": "http://sero.internal", "gateway_config_repo": GATEWAY_REPO,
        "gateway_namespace": "liminal",
    }
    values.update(overrides)
    return Config(**values)


BODY = {"vendor": "datadog", "name": "Datadog", "base_access": "read", "credential": "dd-secret-token",
        "address": "https://mcp.datadoghq.com", "trust": "untrusted"}


class FakeAwsAdmin:
    """The gateway's admin plane as the AWS flow sees it: who the gateway is,
    and whether AWS lets it assume a role."""

    def __init__(self, *, refuse: str | None = None, identity_error: str | None = None):
        self.probes: list[tuple[str, str]] = []
        self._refuse = refuse
        self._identity_error = identity_error

    def aws_identity(self, audience: str) -> dict[str, str]:
        if self._identity_error:
            raise ClientError(self._identity_error)
        return {"issuer": "https://accounts.google.com", "subject": "1234567890", "email": "gw@p.iam.gserviceaccount.com", "audience": audience}

    def aws_probe(self, role_arn: str, audience: str) -> dict[str, str]:
        self.probes.append((role_arn, audience))
        if self._refuse:
            raise ClientError(self._refuse)
        return {"account": role_arn.split(":")[4], "arn": f"arn:aws:sts::{role_arn.split(':')[4]}:assumed-role/KoboAccess-Role/probe", "role_arn": role_arn}

    def list_grants(self):
        return []


REGISTRY = 'service_audience = "liminal"\n\n[tool_extras]\n\n[tool_extras."gateway.clusters"."dev/a"]\nproject_number = "1"\n'


AWS_ROLE = "arn:aws:iam::123456789012:role/KoboAccess-Role"


def test_connect_stores_the_credential_opens_the_gateway_pr_and_records_the_row(tmp_path, fake_storage):
    """Wire shape: Connect stores the credential opens the gateway pr and records the row."""
    github = FakeGitHub(existing_repos={GATEWAY_REPO}, extra_clone_files={"targets.toml": TARGETS}, track_pushed_content=True)
    sero = FakeSero()
    client, headers = _client__api_admin_integrations(tmp_path, fake_storage, _config(tmp_path), github=github, sero=sero)

    response = client.post("/api/admin/integrations", json=BODY, headers=headers)

    assert response.status_code == 201, response.text
    row = response.json()
    assert row["id"] == "datadog" and row["state"] == "pending"
    assert row["credential_ref"] == "apps/kobo/integrations/datadog"
    assert "pull request open on the gateway" in row["state_detail"]
    # The credential went to the vault under the room's path and was synced into the gateway's namespace.
    assert sero.puts == [("ops@example.com", "apps/kobo/integrations/datadog", {"TOKEN": "dd-secret-token"})]
    assert sero.syncs[0][1:] == ("kobo", "apps/kobo/integrations/datadog", "liminal", "integration-datadog")
    # The pull request adds the target without touching the one already there.
    targets = github.pushed_file("targets.toml")
    assert 'name = "existing"' in targets and 'name = "datadog"' in targets
    assert 'bearer_token_env = "INTEGRATION_DATADOG_TOKEN"' in targets
    assert row["spec"]["pull_request"].startswith("https://")
    # And nothing secret is in the row or the audit log.
    listing = client.get("/api/admin/integrations", headers=headers).json()["integrations"]
    assert "dd-secret-token" not in str(listing)
    audit = client.get("/api/admin/audit", headers=headers).json()["rows"]
    assert audit[0]["action"] == "integration.connect" and "dd-secret-token" not in str(audit)
    assert audit[0]["summary"] == "Connected Datadog"


def test_an_aws_role_is_checked_with_aws_then_written_into_the_trust_registry(tmp_path, fake_storage):
    """Wire shape: An aws role is checked with aws then written into the trust registry."""
    github = FakeGitHub(existing_repos={GATEWAY_REPO}, extra_clone_files={"registry.toml": REGISTRY, "targets.toml": TARGETS}, track_pushed_content=True)
    sero, admin = FakeSero(), FakeAwsAdmin()
    client, headers = _client__api_admin_integrations(tmp_path, fake_storage, _config(tmp_path), github=github, sero=sero)
    client.app.state.clients.liminal_admin = admin
    body = {"vendor": "aws", "name": "AWS", "base_access": "read", "auth": "aws_role", "identity": {"role_arn": AWS_ROLE}}
    response = client.post("/api/admin/integrations", json=body, headers=headers)
    assert response.status_code == 201, response.text
    row = response.json()
    assert row["credential_ref"] is None and sero.puts == [] and sero.syncs == []
    assert admin.probes == [(AWS_ROLE, "sts.amazonaws.com")]
    assert "AWS agreed" in row["state_detail"] and "123456789012" in row["state_detail"]
    assert row["spec"]["identity"] == {"role_arn": AWS_ROLE, "audience": "sts.amazonaws.com"}
    registry = github.pushed_file("registry.toml")
    assert '[tool_extras."aws.roles".aws]' in registry and f'role_arn = "{AWS_ROLE}"' in registry
    assert registry.startswith('service_audience = "liminal"') and '"gateway.clusters"' in registry, "the rest of the registry is kept"
    assert github.pushed_file("targets.toml") is None or 'name = "aws"' not in (github.pushed_file("targets.toml") or "")


def test_the_connect_aws_page_carries_the_bedrock_block_only_when_ticked(tmp_path, fake_storage):
    """Wire shape (kobo#390): with `bedrock=1` the role's Terraform gains the
    inline Bedrock policy and `bedrock_policy` carries the same permissions
    for CloudFormation and by hand; without it neither appears; the trust
    policy is the same either way; an organization's hub never gets it."""
    client, headers = _client__api_admin_integrations(tmp_path, fake_storage, _config(tmp_path, bab_url="http://bab.bab.svc:8080"),
                                                       github=FakeGitHub(), sero=FakeSero())
    client.app.state.clients.bab = FakeBab(integrations=[])
    plain = client.get("/api/admin/integrations/aws/setup?mode=role", headers=headers).json()
    ticked = client.get("/api/admin/integrations/aws/setup?mode=role&bedrock=1", headers=headers).json()
    assert plain["bedrock_policy"] is None and "bedrock" not in plain["terraform"]
    assert ticked["bedrock_default_region"] == "us-east-1"
    assert ticked["terraform"].startswith(plain["terraform"]) and 'resource "aws_iam_role_policy" "kobo_bedrock"' in ticked["terraform"]
    policy = json.loads(ticked["bedrock_policy"])
    assert [s["Sid"] for s in policy["Statement"]] == ["BedrockCatalogue", "BedrockInvoke"]
    assert "bedrock:InvokeModelWithResponseStream" in policy["Statement"][1]["Action"] and "bedrock:ListInferenceProfiles" in policy["Statement"][0]["Action"]
    assert all(line in ticked["terraform"] for line in ("bedrock:Converse", "arn:aws:bedrock:*:*:inference-profile/*"))
    assert ticked["trust_policy"] == plain["trust_policy"]
    org = client.get("/api/admin/integrations/aws/setup?mode=org&bedrock=1", headers=headers).json()
    assert org["bedrock_policy"] is None and "kobo_bedrock" not in org["terraform"]


def test_the_connect_google_cloud_page_renders_the_identity_and_the_pack_three_ways(tmp_path, fake_storage):
    """Wire shape (kobo#390): the page the UI reads carries the permission service's identity
    (email and subject) and the chosen packs' roles bound three ways, gcloud, Terraform and
    the plain list, from the vendored packs file; the models pack is a project's alone."""
    client, headers = _client__api_admin_integrations(tmp_path, fake_storage, _config(tmp_path, bab_url="http://bab.bab.svc:8080"),
                                                       github=FakeGitHub(), sero=FakeSero())
    client.app.state.clients.bab = FakeBab(integrations=[])
    page = client.get("/api/admin/integrations/gcp/setup?mode=project&target=example-prod-1234&packs=models", headers=headers).json()
    assert page["mode"] == "project" and page["principal"] == "access" and page["checked"] is True
    assert page["identity"] == {"email": "access@example.iam.gserviceaccount.com", "subject": "102000000000000000000"}
    assert page["chosen"] == ["inventory", "models"] and page["vertex_default_location"] == "us-east5"
    assert [p["id"] for p in page["packs"]] == ["inventory", "models"] and page["packs"][0]["required"] is True
    assert "roles/compute.viewer" in page["roles"] and page["roles"][-1] == "roles/aiplatform.user"
    assert ("gcloud projects add-iam-policy-binding example-prod-1234 \\\n"
            "  --member=serviceAccount:access@example.iam.gserviceaccount.com --role=roles/aiplatform.user --condition=None\n") in page["gcloud"]
    assert page["gcloud"].count("add-iam-policy-binding") == len(page["roles"])
    assert 'resource "google_project_iam_member" "kobo_access"' in page["terraform"] and 'project = "example-prod-1234"' in page["terraform"]
    assert all(f'"{r}",' in page["terraform"] for r in page["roles"])
    # A folder: the folder resource, no models pack whatever was asked.
    folder = client.get("/api/admin/integrations/gcp/setup?mode=parent&target=folders/123&packs=models", headers=headers).json()
    assert folder["chosen"] == ["inventory"] and "roles/aiplatform.user" not in folder["roles"]
    assert "gcloud resource-manager folders add-iam-policy-binding 123 " in folder["gcloud"]
    assert 'resource "google_folder_iam_member" "kobo_access"' in folder["terraform"] and 'folder  = "folders/123"' in folder["terraform"]
    org = client.get("/api/admin/integrations/gcp/setup?mode=parent&target=organizations/456", headers=headers).json()
    assert "gcloud organizations add-iam-policy-binding 456 " in org["gcloud"] and 'org_id  = "456"' in org["terraform"]
    # Without the permission service the page says so and cannot check; the roles still render.
    client.app.state.clients.bab = None
    off = client.get("/api/admin/integrations/gcp/setup?mode=project", headers=headers).json()
    assert off["identity"] is None and "permission service" in off["identity_note"] and off["checked"] is False
    assert "<identity email>" in off["gcloud"] and "<project id>" in off["gcloud"]


def test_named_keys_are_stored_together_and_reach_the_gateway_as_one_env_each(tmp_path, fake_storage):
    """Wire shape: Named keys are stored together and reach the gateway as one env each."""
    github = FakeGitHub(existing_repos={GATEWAY_REPO}, extra_clone_files={"targets.toml": TARGETS}, track_pushed_content=True)
    sero = FakeSero()
    client, headers = _client__api_admin_integrations(tmp_path, fake_storage, _config(tmp_path), github=github, sero=sero)
    body = {
        "vendor": "datadog", "name": "Datadog", "base_access": "read", "auth": "api_keys",
        "secrets": {"API_KEY": "dd-api", "APP_KEY": "dd-app"},
    }
    response = client.post("/api/admin/integrations", json=body, headers=headers)
    assert response.status_code == 201, response.text
    assert sero.puts == [("ops@example.com", "apps/kobo/integrations/datadog", {"API_KEY": "dd-api", "APP_KEY": "dd-app"})]
    targets = github.pushed_file("targets.toml")
    assert 'auth_method = "api_keys"' in targets
    assert 'api_key_env = "INTEGRATION_DATADOG_API_KEY"' in targets and 'app_key_env = "INTEGRATION_DATADOG_APP_KEY"' in targets
    assert "dd-api" not in targets and "dd-app" not in targets


DATADOG_SA = "11111111-2222-3333-4444-555555555555"


def test_datadog_through_the_permission_service_writes_its_secret_and_the_read_ceiling(tmp_path, fake_storage):
    """Wire shape: Datadog's keys land in the Secret the permission service reads (the four
    env names) and the connection is held there with every Datadog read permission; nothing
    goes to the secrets add-on, and the gateway learns only the `datadog-mcp` target, by pull
    request (kobo#322, kobo#327)."""
    github = FakeGitHub(existing_repos={GATEWAY_REPO}, extra_clone_files={"targets.toml": TARGETS}, track_pushed_content=True)
    sero, bab = FakeSero(), FakeBab(integrations=[])
    client, headers = _client__api_admin_integrations(tmp_path, fake_storage, _config(tmp_path, bab_url="http://bab.bab.svc:8080"), github=github, sero=sero)
    client.app.state.clients.bab = bab
    body = {"vendor": "datadog", "name": "Datadog", "base_access": "read", "auth": "datadog_service_account",
            "identity": {"service_account_id": DATADOG_SA}, "secrets": {"API_KEY": "dd-api", "MGMT_APP_KEY": "dd-mgmt"}}
    response = client.post("/api/admin/integrations", json=body, headers=headers)
    assert response.status_code == 201, response.text
    row = response.json()
    assert row["state"] == "pending" and "minted and removed a probe key" in row["state_detail"]
    assert "pull request open on the gateway" in row["state_detail"]
    assert client.app.state.clients.kubectl.secrets_applied == [("bab", "bab-datadog", {
        "DD_SITE": "datadoghq.com", "DD_API_KEY": "dd-api", "DD_MGMT_APP_KEY": "dd-mgmt", "DD_SERVICE_ACCOUNT_ID": DATADOG_SA})]
    assert bab.datadog_probes == [DATADOG_SA]
    (integ,) = bab.integrations("x")
    assert integ["integration"] == "datadog" and integ["spec"] == {"site": "datadoghq.com", "service_account_id": DATADOG_SA, "name": "Datadog"}
    assert integ["ceiling"] == ["datadog_monitors_read"]
    assert row["credential_ref"] is None and sero.puts == []
    targets = github.pushed_file("targets.toml")
    assert 'name = "existing"' in targets and 'name = "datadog-mcp"' in targets and 'name = "datadog"\n' not in targets
    listing = client.get("/api/admin/integrations", headers=headers).json()
    assert "dd-mgmt" not in str(listing) and "dd-api" not in str(listing) and "dd-mgmt" not in targets


# --- from test_web_learnings.py ---


AGENT_NAME__web_learnings = "clio"


# --- from test_web_learnings.py ---


@pytest.fixture
def session_factory__web_learnings(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__web_learnings(tmp_path):
    return Config(workdir=tmp_path / "work")


def _make_client__web_learnings(session_factory__web_learnings, config__web_learnings, *, github=None, iap_jwks_fetcher=None, cabal=None):
    return TestClient(
        create_app(
            session_factory__web_learnings,
            config=config__web_learnings,
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


# --- from test_web_learnings.py ---


PERSONA = "helpful-generalist"


REPO = "acme/agent-clio"


def _seed_agent__web_learnings(
    session_factory__web_learnings,
    config__web_learnings,
    *,
    status=AgentStatus.READY,
    repo_full_name=REPO,
    created_by=None,
):
    session = session_factory__web_learnings()
    session.add(
        Agent(
            name=AGENT_NAME__web_learnings,
            persona=PERSONA,
            model_id=config__web_learnings.models[0].id,
            status=status,
            repo_full_name=repo_full_name,
            created_by=created_by,
        )
    )
    session.commit()
    session.close()


# --- from test_web_activity.py ---


class FakeAudit:
    def __init__(self, rows, *, fail: str | None = None):
        self.rows, self._fail, self.asked = rows, fail, []

    def audit(self, *, persona: str, since=None, limit=200):
        self.asked.append((persona, limit))
        if self._fail:
            raise ClientError(self._fail)
        return self.rows


ROWS = [
    {"ts": 1700000000.0, "action": "access_allow", "persona": AGENT_NAME__web_learnings, "agent": f"{AGENT_NAME__web_learnings}-primus",
     "detail": {"kind": "call", "handler": "aws", "tool": "aws.call", "args": "action=DescribeInstances service=ec2", "outcome": "ok", "ms": 412}},
    {"ts": 1699999000.0, "action": "access_deny", "persona": AGENT_NAME__web_learnings, "agent": None,
     "detail": {"handler": "github", "tool": "github.read_file", "required_scope": "github:read"}},
    {"ts": 1699998000.0, "action": "grant", "persona": AGENT_NAME__web_learnings, "agent": None, "detail": {}},
]


def test_activity_shapes_calls_and_refusals_for_the_table(session_factory__web_learnings, config__web_learnings):
    """Wire shape: Activity shapes calls and refusals for the table."""
    client = _make_client__web_learnings(session_factory__web_learnings, config__web_learnings)
    _seed_agent__web_learnings(session_factory__web_learnings, config__web_learnings)
    fake = FakeAudit(ROWS)
    client.app.state.clients.liminal_admin = fake
    body = client.get(f"/api/agents/{AGENT_NAME__web_learnings}/activity?days=30").json()
    assert body["available"] is True and fake.asked == [(AGENT_NAME__web_learnings, 500)]
    assert [r["outcome"] for r in body["rows"]] == ["ok", "denied"], "grants are not activity"
    call, deny = body["rows"]
    assert call == {"ts": 1700000000.0, "handler": "aws", "tool": "aws.call", "outcome": "ok", "args": "action=DescribeInstances service=ec2", "ms": 412, "detail": ""}
    assert deny["tool"] == "github.read_file" and deny["detail"] == "needs github:read" and deny["ms"] is None


# --- from test_web_teach.py ---


AGENT_NAME = "clio"


PERSONA = "helpful-generalist"


REPO = "acme/agent-clio"


ENDPOINT_TEMPLATE = "http://agent-{name}.default.svc.cluster.local"


def _enabled_config__web_teach(config__web_teach):
    """Same switchboard-on config shape as test_api_colleagues.py's own
    `_enabled_config`: both org config keys set. Combined with the
    `CABAL_OPERATOR_TOKEN` env var (set per-test via monkeypatch) and a
    wired `clients.cabal`, this is what flips `cabal_enabled(cfg)` true."""
    return config__web_teach.model_copy(
        update={
            "cabal_url": "http://cabal.internal",
            "agent_endpoint_template": ENDPOINT_TEMPLATE,
        }
    )


class _CapturingGitHub__web_teach(FakeGitHub):
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


@pytest.fixture
def session_factory__web_teach(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__web_teach(tmp_path):
    return Config(workdir=tmp_path / "work")


def _make_client__web_teach(session_factory__web_teach, config__web_teach, *, github=None, iap_jwks_fetcher=None, cabal=None):
    return TestClient(
        create_app(
            session_factory__web_teach,
            config=config__web_teach,
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


def _seed_agent__web_teach(session_factory__web_teach, config__web_teach, *, status=AgentStatus.READY, repo_full_name=REPO):
    session = session_factory__web_teach()
    session.add(
        Agent(
            name=AGENT_NAME,
            persona=PERSONA,
            model_id=config__web_teach.models[0].id,
            status=status,
            repo_full_name=repo_full_name,
        )
    )
    session.commit()
    session.close()


def test_teach_instant_lane_relays_and_archives_in_background(session_factory__web_teach, config__web_teach, monkeypatch):
    """Wire shape: Teach instant lane relays and archives in background."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config__web_teach(config__web_teach)
    github = _CapturingGitHub__web_teach()
    relay_result = {
        "status": {
            "state": "completed",
            "message": {"parts": [{"text": "noted, thanks"}]},
        }
    }
    fake_cabal = FakeCabal(relay_result=relay_result)
    client = _make_client__web_teach(session_factory__web_teach, cfg, github=github, cabal=fake_cabal)
    _seed_agent__web_teach(session_factory__web_teach, cfg)

    text = "always answer in one paragraph"
    response = client.post(f"/api/agents/{AGENT_NAME}/teach", json={"text": text})

    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"filename", "mode", "learning"}
    assert body["mode"] == "instant"
    filename = body["filename"]

    # BackgroundTasks run after the response completes but before
    # TestClient.post() returns control here -- the archive commit has
    # already happened by this point.
    assert list(github.captured_intake.keys()) == [filename]
    committed_note = github.captured_intake[filename]

    assert len(fake_cabal.relay_calls) == 1
    envelope = fake_cabal.relay_calls[0]["task_envelope"]
    assert envelope["metadata"] == {"system_task": "teach", "filename": filename}
    # Both lanes agree on content: the relayed note is byte-for-byte what
    # the background archive committed.
    assert envelope["status"]["message"]["parts"][0]["text"] == committed_note


# --- from test_mcp_tools_read.py ---


PERSONA = "helpful-generalist"


REPO_TEMPLATE = "acme/agent-{name}"


GROUP_A = "growth"


GROUP_B = "design-team"


ADMIN_GROUP = "infra-engineering"


AUDIENCE__mcp_tools_read = "test-iap-audience"


CLIENT_ID = "test-mcp-client"


WORKSPACE_DOMAIN = "example.com"


ISSUER_BASE_URL = "https://kobo.example.org"


OWNER_A_EMAIL = "owner-a@example.com"


OWNER_B_EMAIL = "owner-b@example.com"


MEMBER_A_EMAIL = "member-a@example.com"


OPERATOR_EMAIL = "ops@example.com"


ADMIN_MEMBER_EMAIL = "admin-member@example.com"


class FakeGroupResolver__mcp_tools_read:
    """The plainest possible `GroupResolver` stand-in, borrowed from
    `tests/test_mcp_authz.py`: a bare object exposing `groups_for(email)
    -> list[str] | None`, mapping-driven, or always-`None` ("the
    directory is down") when `down=True`."""

    def __init__(self, mapping: dict[str, list[str]] | None = None, *, down: bool = False):
        self._mapping = dict(mapping) if mapping else {}
        self._down = down

    def groups_for(self, email: str) -> list[str] | None:
        if self._down:
            return None
        return self._mapping.get(email.lower(), [])


def _resolver():
    return FakeGroupResolver__mcp_tools_read(
        {
            MEMBER_A_EMAIL: [GROUP_A],
            OWNER_A_EMAIL: [GROUP_A],
            OWNER_B_EMAIL: [GROUP_B],
            ADMIN_MEMBER_EMAIL: [ADMIN_GROUP],
        }
    )


@pytest.fixture(autouse=True)
def _reset_rate_limit_and_audit():
    """Every test starts with a clean sliding window and an unconfigured
    audit layer -- same discipline `test_mcp_audit.py` holds, needed here
    too since `build_mcp_server` calls `audit.configure` for real."""
    rate_limit.reset()
    audit.configure(None)
    yield
    rate_limit.reset()
    audit.configure(None)


@pytest.fixture
def session_factory__mcp_tools_read(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__mcp_tools_read(tmp_path):
    return Config(
        workdir=tmp_path / "work",
        operators=[OPERATOR_EMAIL],
        admin_groups=[ADMIN_GROUP],
        groups=[
            GroupChoice(name=GROUP_A, okta_group="okta-growth"),
            GroupChoice(name=GROUP_B, okta_group="okta-design-team"),
            GroupChoice(name=ADMIN_GROUP, okta_group="okta-infra-engineering"),
        ],
        iap_audience=AUDIENCE__mcp_tools_read,
        mcp=MCPConfig(
            allow_without_iap=True, oauth_client_id=CLIENT_ID,
            workspace_domain=WORKSPACE_DOMAIN,
            issuer_base_url=ISSUER_BASE_URL,
        ),
    )


def _app(session_factory__mcp_tools_read, config__mcp_tools_read, *, groups_resolver=None, cabal=None, storage=None, github=None):
    clients = Clients(
        github=github if github is not None else FakeGitHub(),
        gojira=FakeGojira(),
        cloud=FakeCloud(),
        agent=FakeAgent(),
        kubectl=FakeKubectl(),
        groups=groups_resolver,
        cabal=cabal,
        storage=storage,
    )
    return create_app(session_factory__mcp_tools_read, config=config__mcp_tools_read, clients=clients)


def _seed_agent__mcp_tools_read(
    session_factory__mcp_tools_read,
    *,
    name,
    created_by=None,
    group_name=None,
    team=None,
    purpose_tags=None,
    status=AgentStatus.READY,
    kind=KIND_COMPOSED,
):
    session = session_factory__mcp_tools_read()
    session.add(
        Agent(
            name=name,
            persona=PERSONA,
            model_id="vertex_ai/claude-sonnet-4-5",
            status=status,
            repo_full_name=REPO_TEMPLATE.format(name=name),
            created_by=created_by,
            group_name=group_name,
            team=team,
            purpose_tags=encode_tags(purpose_tags) if purpose_tags else None,
            kind=kind,
        )
    )
    session.commit()
    session.close()


class _StubAccessToken:
    def __init__(self, user_email):
        self.user_email = user_email


class _StubUser:
    def __init__(self, user_email):
        self.access_token = _StubAccessToken(user_email)


class _StubRequest:
    def __init__(self, user_email):
        self.user = _StubUser(user_email)


def _ctx_for(email):
    request_context = RequestContext(
        request_id="test-request",
        meta=None,
        session=None,
        lifespan_context=None,
        request=_StubRequest(email),
    )
    return Context(request_context=request_context)


def _call_tool(mcp, tool_name, ctx, **arguments):
    return asyncio.run(mcp._tool_manager.call_tool(tool_name, arguments, context=ctx))


def _seed_learning(session_factory__mcp_tools_read, agent_name, **overrides):
    session = session_factory__mcp_tools_read()
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


PERSONAS_BUCKET = "personas-bucket"


def _seed_persona(
    app,
    session_factory__mcp_tools_read,
    *,
    name: str,
    who_they_are: str = "a calm, capable colleague",
    what_they_do: str = "Helps out wherever needed.",
    team: str = "shared",
    creator_email: str = MEMBER_A_EMAIL,
) -> dict:
    """Seeds one persona through the REAL POST /api/personas route -- the
    same `_endpoint` + `authz.room_request` shim `kobo_api.mcp.tools.write`
    uses for its own write tools (this module has no write tools of its
    own, so this helper borrows that module's `_endpoint`), so the
    persona's ingest (the route's own inline `ingest_indexes` call after a
    successful create) runs for real and the persona is immediately
    visible to `/api/personas/search` -- exactly like a real caller
    creating one through the UI, not a hand-rolled fixture that skips the
    route's own gates and side effects.
    """
    from kobo_api.main import CreatePersonaRequest

    create_persona_endpoint = _endpoint(app, "POST", "/api/personas")
    payload = CreatePersonaRequest(
        name=name, who_they_are=who_they_are, what_they_do=what_they_do, team=team
    )
    req = authz.room_request(app, creator_email)
    session = session_factory__mcp_tools_read()
    try:
        response = create_persona_endpoint(payload=payload, request=req, session=session)
    finally:
        session.close()
    assert response.status_code == 201, response.body
    return json.loads(response.body)


def _search_personas(
    app,
    session_factory__mcp_tools_read,
    *,
    q: str = "",
    team: str | None = None,
    scope: str = "mine",
    email: str = MEMBER_A_EMAIL,
) -> dict:
    """The route's own answer, called the same way -- through `_endpoint`,
    not a second implementation -- so the tool's output can be compared
    against it directly for parity."""
    search_personas_endpoint = _endpoint(app, "GET", "/api/personas/search")
    session = session_factory__mcp_tools_read()
    try:
        return search_personas_endpoint(
            request=authz.room_request(app, email),
            q=q,
            team=team,
            scope=scope,
            session=session,
        )
    finally:
        session.close()


def _personas_app(session_factory__mcp_tools_read, config__mcp_tools_read, **kwargs):
    cfg = config__mcp_tools_read.model_copy(update={"personas_bucket": PERSONAS_BUCKET})
    storage = kwargs.pop("storage", None) or FakeStorage(buckets={PERSONAS_BUCKET: {}})
    return _app(session_factory__mcp_tools_read, cfg, storage=storage, **kwargs)


def test_list_agents_returns_name_group_team_owner_status_purpose(session_factory__mcp_tools_read, config__mcp_tools_read):
    """Wire shape: List agents returns name group team owner status purpose."""
    _seed_agent__mcp_tools_read(
        session_factory__mcp_tools_read,
        name="agent-a",
        created_by=OWNER_A_EMAIL,
        group_name=GROUP_A,
        team="growth-team",
        purpose_tags=["billing", "support"],
    )
    app = _app(session_factory__mcp_tools_read, config__mcp_tools_read, groups_resolver=_resolver())
    mcp = build_mcp_server(app)

    result = _call_tool(mcp, "kobo_list_agents", _ctx_for(OWNER_A_EMAIL))

    assert result == [
        {
            "name": "agent-a",
            "group": GROUP_A,
            "team": "growth-team",
            "owner": OWNER_A_EMAIL,
            "status": AgentStatus.READY,
            "purpose": ["billing", "support"],
            "kind": KIND_COMPOSED,
        }
    ]


def test_get_agent_carries_the_detail_routes_full_field_set(session_factory__mcp_tools_read, config__mcp_tools_read):
    # Task 7 (create-identity wave): kobo_get_agent had drifted from the
    # HTTP detail route (GET /api/agents/{name}) -- this pins that the MCP
    # card now mirrors that route's own additions field for field: the
    # learnings count/gate (daily-learnings spec), skills, slack, avatar,
    # repo, and model-label facts.
    """Wire shape: Get agent carries the detail routes full field set."""
    _seed_agent__mcp_tools_read(
        session_factory__mcp_tools_read,
        name="agent-a",
        created_by=OWNER_A_EMAIL,
        group_name=GROUP_A,
        team="growth-team",
    )
    _seed_learning(session_factory__mcp_tools_read, "agent-a")
    app = _app(session_factory__mcp_tools_read, config__mcp_tools_read, groups_resolver=_resolver())
    mcp = build_mcp_server(app)

    result = _call_tool(mcp, "kobo_get_agent", _ctx_for(OWNER_A_EMAIL), name="agent-a")

    assert result["pending_learnings"] == 1
    assert result["can_decide_learnings"] is True
    assert result["has_avatar"] is False
    assert result["model_label"] == "Claude Sonnet 4.5"
    assert result["skills"] == []
    assert result["repo_full_name"] == REPO_TEMPLATE.format(name="agent-a")
    assert result["slack"] == {"app_id": None, "channels": [], "state": "idle"}
    # OWNER_A_EMAIL owns this agent (can_decide_learnings above) but holds
    # no operator standing -- destroy stays operator-only regardless.
    assert result["destroy_visible"] is False


def test_list_personas_mirrors_search_route_shape(session_factory__mcp_tools_read, config__mcp_tools_read):
    """Wire shape: List personas mirrors search route shape."""
    app = _personas_app(session_factory__mcp_tools_read, config__mcp_tools_read, groups_resolver=_resolver())
    _seed_persona(app, session_factory__mcp_tools_read, name="Aria the Assistant")
    mcp = build_mcp_server(app)

    result = _call_tool(mcp, "kobo_list_personas", _ctx_for(MEMBER_A_EMAIL))
    route_body = _search_personas(app, session_factory__mcp_tools_read)

    assert result == {"result": route_body["results"]}
    assert len(result["result"]) == 1
    assert set(result["result"][0].keys()) == {
        "id",
        "name",
        "description",
        "team",
        "tags",
        "builtin",
    }


# --- from test_mcp_tools_write.py ---


PERSONA = "helpful-generalist"


GROUP_A = "growth"


GROUP_B = "design-team"


AUDIENCE__mcp_tools_write = "test-iap-audience"


CLIENT_ID = "test-mcp-client"


WORKSPACE_DOMAIN = "example.com"


ISSUER_BASE_URL = "https://kobo.example.org"


SKILLS_BUCKET = "skills-bucket"


PERSONAS_BUCKET = "personas-bucket"


OWNER_A_EMAIL = "owner-a@example.com"


OWNER_B_EMAIL = "owner-b@example.com"


MEMBER_A_EMAIL = "member-a@example.com"


OPERATOR_EMAIL = "ops@example.com"


@pytest.fixture(autouse=True)
def _no_real_sleep__mcp_tools_write(monkeypatch):
    """kobo_create_agent's pipeline (OnboardStep/WireStep/BuildStep) and
    kobo_wire_slack's wait_active both poll in a loop -- never let either
    sleep for real (same discipline as test_api_agents.py/test_api_slack.py's
    own copies of this fixture)."""
    import kobo_api.slack_setup as slack_setup_module
    import kobo_api.steps as steps_module

    monkeypatch.setattr(steps_module, "_sleep", lambda seconds: None)
    monkeypatch.setattr(slack_setup_module, "_sleep", lambda seconds: None)


@pytest.fixture(autouse=True)
def _reset_rate_limit_and_audit__mcp_tools_write():
    rate_limit.reset()
    audit.configure(None)
    yield
    rate_limit.reset()
    audit.configure(None)


@pytest.fixture
def session_factory__mcp_tools_write(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__mcp_tools_write(tmp_path):
    return Config(
        workdir=tmp_path / "work",
        operators=[OPERATOR_EMAIL],
        groups=[
            GroupChoice(name=GROUP_A, okta_group="okta-growth"),
            GroupChoice(name=GROUP_B, okta_group="okta-design-team"),
        ],
        iap_audience=AUDIENCE__mcp_tools_write,
        skills_bucket=SKILLS_BUCKET,
        personas_bucket=PERSONAS_BUCKET,
        mcp=MCPConfig(
            allow_without_iap=True, oauth_client_id=CLIENT_ID,
            workspace_domain=WORKSPACE_DOMAIN,
            issuer_base_url=ISSUER_BASE_URL,
        ),
    )


@pytest.fixture
def fake_storage__mcp_tools_write():
    """Seeded the same way conftest.py's own `fake_storage` is -- kept as a
    local copy (not imported) so this file's `config` fixture, which needs
    the MCP block conftest's own `config` doesn't carry, isn't forced to
    also match conftest's bucket-name constants by coincidence."""
    storage = FakeStorage()
    seed_default_personas(storage, bucket=PERSONAS_BUCKET)
    return storage


class FakeGroupResolver__mcp_tools_write:
    """Mapping-driven, or always-`None` ("the directory is down") when
    `down=True` -- same seam `test_mcp_tools_read.py`'s own copy of this
    fake uses for `kobo_whoami`'s `groups_unavailable` case, reused here
    for `kobo_create_skill`/`kobo_create_tool`'s membership gate (MCP
    marketplace wave, Task 5)."""

    def __init__(self, mapping: dict[str, list[str]] | None = None, *, down: bool = False):
        self._mapping = dict(mapping) if mapping else {}
        self._down = down

    def groups_for(self, email: str) -> list[str] | None:
        if self._down:
            return None
        return self._mapping.get(email.lower(), [])


def _resolver__mcp_tools_write():
    return FakeGroupResolver__mcp_tools_write(
        {
            MEMBER_A_EMAIL: [GROUP_A],
            OWNER_A_EMAIL: [GROUP_A],
            OWNER_B_EMAIL: [GROUP_B],
        }
    )


def _app__mcp_tools_write(
    session_factory__mcp_tools_write,
    config__mcp_tools_write,
    *,
    groups_resolver=None,
    storage=None,
    github=None,
    gojira=None,
    agent=None,
    slack=None,
    cabal=None,
    name=None,
    sero=None,
    kubectl=None,
):
    clients = Clients(
        github=github if github is not None else FakeGitHub(),
        gojira=gojira if gojira is not None else FakeGojira(),
        cloud=FakeCloud(),
        agent=agent if agent is not None else FakeAgent(),
        kubectl=kubectl if kubectl is not None else FakeKubectl(),
        groups=groups_resolver,
        storage=storage,
        slack=slack,
        cabal=cabal,
        name=name,
        sero=sero,
    )
    return create_app(session_factory__mcp_tools_write, config=config__mcp_tools_write, clients=clients)


class _StubAccessToken:
    def __init__(self, user_email):
        self.user_email = user_email


class _StubUser:
    def __init__(self, user_email):
        self.access_token = _StubAccessToken(user_email)


class _StubRequest:
    def __init__(self, user_email):
        self.user = _StubUser(user_email)


def _ctx_for(email):
    request_context = RequestContext(
        request_id="test-request",
        meta=None,
        session=None,
        lifespan_context=None,
        request=_StubRequest(email),
    )
    return Context(request_context=request_context)


def _call_tool(mcp, tool_name, ctx, **arguments):
    return asyncio.run(mcp._tool_manager.call_tool(tool_name, arguments, context=ctx))


def test_create_agent_happy_path_returns_name_status_and_note(
    session_factory__mcp_tools_write, config__mcp_tools_write, fake_storage__mcp_tools_write
):
    """Wire shape: Create agent happy path returns name status and note."""
    app = _app__mcp_tools_write(session_factory__mcp_tools_write, config__mcp_tools_write, groups_resolver=_resolver__mcp_tools_write(), storage=fake_storage__mcp_tools_write)
    mcp = build_mcp_server(app)

    result = _call_tool(
        mcp,
        "kobo_create_agent",
        _ctx_for(MEMBER_A_EMAIL),
        who_they_are=PERSONA,
        group=GROUP_A,
        name="clio",
    )

    assert result["name"] == "clio"
    assert result["status"] == AgentStatus.PENDING
    assert result.get("note")
    session = session_factory__mcp_tools_write()
    try:
        agent = session.query(Agent).filter_by(name="clio").one()
        assert agent.group_name == GROUP_A
        assert agent.created_by == MEMBER_A_EMAIL
    finally:
        session.close()


def test_set_agent_skills_is_marked_destructive(session_factory__mcp_tools_write, config__mcp_tools_write, fake_storage__mcp_tools_write):
    """Wire shape: Set agent skills is marked destructive.

    remove deletes a whole bundle/skills/<id>/ tree from the agent's
    repo -- this tool's own annotation must say so, distinct from the
    module-wide `destructiveHint=False` every other write tool here
    carries."""
    app = _app__mcp_tools_write(session_factory__mcp_tools_write, config__mcp_tools_write, groups_resolver=_resolver__mcp_tools_write(), storage=fake_storage__mcp_tools_write)
    mcp = build_mcp_server(app)

    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == "kobo_set_agent_skills")

    assert tool.annotations.destructiveHint is True


def test_add_agent_tool_is_marked_destructive(session_factory__mcp_tools_write, config__mcp_tools_write, fake_storage__mcp_tools_write):
    """Wire shape: Add agent tool is marked destructive.

    A same-named upload silently REPLACES whatever `bundle/tools` file
    was already there (`kobo_api.tooling.upload_tooling_file`'s own
    "Write (or overwrite)" docstring, an unconditional `write_text`) --
    including a live, enforced hook -- so this tool's own annotation must
    say so, matching `kobo_set_agent_skills`'s own `destructiveHint=True`
    rather than the module-wide `False` most other write tools carry
    (review round 1, important 2)."""
    app = _app__mcp_tools_write(session_factory__mcp_tools_write, config__mcp_tools_write, groups_resolver=_resolver__mcp_tools_write(), storage=fake_storage__mcp_tools_write)
    mcp = build_mcp_server(app)

    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == "kobo_add_agent_tool")

    assert tool.annotations.destructiveHint is True


def test_create_persona_rules_none_explicit_succeeds(session_factory__mcp_tools_write, config__mcp_tools_write, fake_storage__mcp_tools_write):
    """Wire shape: Create persona rules none explicit succeeds.

    WHY this test exists: FastMCP materializes every declared default at
    call time, so a caller who never mentions `rules` at all still sends
    `rules=None` into the tool body -- there is no "key omitted" case
    inside the function, only "rules is None" or "rules is a list".
    `CreatePersonaRequest.rules` (kobo_api.main/kobo_api.personas) is
    typed `list[str]` with `Field(default_factory=list)`, NOT
    `Optional[list[str]]` -- passing `None` straight through raises a
    pydantic `ValidationError`. `write.py`'s own `rules=rules or []` is
    the ONLY thing standing between the common, rules-less call and that
    failure. This test pins that guard directly, with `rules=None` passed
    explicitly, against a future "simplification" to plain `rules=rules`
    that would quietly break every rules-less `kobo_create_persona` call."""
    app = _app__mcp_tools_write(session_factory__mcp_tools_write, config__mcp_tools_write, groups_resolver=_resolver__mcp_tools_write(), storage=fake_storage__mcp_tools_write)
    mcp = build_mcp_server(app)

    result = _call_tool(
        mcp,
        "kobo_create_persona",
        _ctx_for(MEMBER_A_EMAIL),
        name="Quiet Auditor",
        who_they_are="an unassuming reviewer",
        what_they_do="Check the numbers without commentary.",
        rules=None,
    )

    assert result["id"] == "quiet-auditor"
    assert result["team"] == "shared"


# --- from test_mcp_mount.py ---


CLIENT_ID = "test-mcp-client"


WORKSPACE_DOMAIN = "example.com"


ISSUER_BASE_URL = "https://kobo.example.org"


EMAIL = "person@example.com"


@pytest.fixture(autouse=True)
def _reset_rate_limit_and_audit__mcp_mount():
    """Same discipline test_mcp_tools_read.py/test_mcp_tools_write.py hold
    -- `mount_mcp` (enabled) calls `build_mcp_server`, which calls `audit.
    configure` for real, a module-level global that would otherwise leak
    across test files sharing this process."""
    rate_limit.reset()
    audit.configure(None)
    yield
    rate_limit.reset()
    audit.configure(None)


@pytest.fixture
def session_factory__mcp_mount(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _enabled_config__mcp_mount(tmp_path):
    return Config(
        workdir=tmp_path / "work",
        mcp=MCPConfig(
            allow_without_iap=True, oauth_client_id=CLIENT_ID,
            workspace_domain=WORKSPACE_DOMAIN,
            issuer_base_url=ISSUER_BASE_URL,
        ),
    )


def _enabled_app(session_factory__mcp_mount, tmp_path):
    clients = Clients(
        github=FakeGitHub(),
        gojira=FakeGojira(),
        cloud=FakeCloud(),
        agent=FakeAgent(),
        kubectl=FakeKubectl(),
    )
    return create_app(session_factory__mcp_mount, config=_enabled_config__mcp_mount(tmp_path), clients=clients)


def test_real_initialize_request_does_not_hit_the_uninitialized_session_manager(
    session_factory__mcp_mount, tmp_path
):
    """Wire shape: Real initialize request does not hit the uninitialized session manager.

    The load-bearing proof, against the real SDK, not just a boot-
    without-raising check: a genuine bearer-authenticated `initialize`
    POST to `/mcp/` must not 500 with the SDK's own `RuntimeError("Task
    group is not initialized. Make sure to use run().")` -- exactly the
    failure a mounted FastMCP app produces when its own lifespan (the one
    that starts `StreamableHTTPSessionManager.run()`'s task group) never
    ran. Reproduced by hand while building kobo_api.mcp.mount (a manual
    `app.mount(...)` with no lifespan chaining raises precisely this, from
    inside the request, not at boot) -- this test is that reproduction
    turned around to assert the FIXED behavior instead.

    An unauthenticated request can't stand in for this proof: the SDK's
    own `RequireAuthMiddleware` answers 401 BEFORE ever reaching the
    session manager, so it would pass even with a completely broken
    lifespan chain. Hence minting a real access token here rather than
    reusing the simpler well-known/oauth checks above.

    `base_url` is the room's own configured `ISSUER_BASE_URL`
    (`https://kobo.example.org`), not `localhost`, per the rollout-blocker
    fix in `_transport_security_for` (`kobo_api.mcp.server`): `build_mcp_
    server` now points `FastMCP`'s `transport_security` at this room's
    real hostname (parsed off `config.mcp.issuer_base_url`) instead of
    leaving the SDK's own loopback-only default (`host="127.0.0.1"` ->
    `allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"]`) in place --
    a `localhost` Host header no longer matches at all post-fix, exactly
    as it shouldn't for a room whose real ingress never serves `/mcp` on
    loopback. See `test_real_initialize_request_over_the_rooms_real_
    hostname_is_not_421` below for the fix's own dedicated regression
    test, reproduced RED against the pre-fix 421 and GREEN after.
    """
    app = _enabled_app(session_factory__mcp_mount, tmp_path)
    cfg = app.state.config
    token = mint(
        "access_token",
        EMAIL,
        CLIENT_ID,
        ACCESS_TTL_S,
        key=signing_key(cfg.mcp.jwt_key_env),
        scope="mcp",
    )

    with TestClient(app, base_url=ISSUER_BASE_URL) as client:
        response = client.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test-mount-smoke", "version": "0.0.1"},
                },
            },
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )

    assert response.status_code == 200
    assert "Task group is not initialized" not in response.text
    assert '"protocolVersion"' in response.text


# --- from test_learnings.py ---


def test_learnings_envelope_shape():
    """Wire shape: Learnings envelope shape."""
    envelope = _build_learnings_envelope("2026-09-01")

    assert envelope["metadata"]["system_task"] == "learnings"
    assert envelope["metadata"]["date"] == "2026-09-01"
    assert "2026-09-01" in envelope["status"]["message"]["parts"][0]["text"]


# --- from test_steps.py ---


AGENT_NAME = "clio"


PERSONA = "helpful-generalist"


MODEL_ID = "vertex_ai/claude-sonnet-4-5"


@pytest.fixture(autouse=True)
def _no_real_sleep__steps(monkeypatch):
    """Every OnboardStep test runs a poll loop; never let it sleep for real."""
    import kobo_api.steps as steps_module

    sleep_calls = []

    def fake_sleep(seconds):
        sleep_calls.append(seconds)

    fake_sleep.calls = sleep_calls
    monkeypatch.setattr(steps_module, "_sleep", fake_sleep)


def _make_agent(
    session_factory,
    *,
    name: str = AGENT_NAME,
    persona: str = PERSONA,
    model_id: str = MODEL_ID,
    skills: list | None = None,
    repo_full_name: str | None = None,
    gojira_app_id: int | None = None,
    team: str | None = None,
    group_name: str | None = None,
    purpose_tags: str | None = None,
    skip_baseline: bool = False,
    archetype: str | None = None,
):
    session = session_factory()
    agent = Agent(
        archetype=archetype,
        name=name,
        persona=persona,
        model_id=model_id,
        skills=skills or [],
        repo_full_name=repo_full_name,
        gojira_app_id=gojira_app_id,
        team=team,
        group_name=group_name,
        purpose_tags=purpose_tags,
        skip_baseline=skip_baseline,
    )
    session.add(agent)
    session.commit()
    return session, agent


def _clients(
    *,
    github=None,
    gojira=None,
    cloud=None,
    storage=None,
    agent=None,
    kubectl=None,
    name=None,
    cabal=None,
    image=None,
) -> Clients:
    return Clients(
        github=github,
        gojira=gojira,
        cloud=cloud,
        agent=agent,
        kubectl=kubectl,
        storage=storage,
        name=name,
        cabal=cabal,
        image=image,
    )


def _ready_agent(session, name, *, group_name=None, capability=None):
    agent = Agent(
        name=name,
        persona=PERSONA,
        model_id=MODEL_ID,
        skills=[],
        status=AgentStatus.READY,
        group_name=group_name,
        capability=capability,
    )
    session.add(agent)
    session.commit()
    return agent


HELLO_CABAL_ENDPOINT_TEMPLATE = "http://agent-{name}.default.svc.cluster.local"


def _hello_cabal_enabled_config(config):
    return config.model_copy(
        update={
            "cabal_url": "http://cabal.internal",
            "agent_endpoint_template": HELLO_CABAL_ENDPOINT_TEMPLATE,
        }
    )


def test_refresh_colleague_rosters_envelope_shape_and_content(
    session_factory, config, monkeypatch
):
    """Wire shape: Refresh colleague rosters envelope shape and content."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _hello_cabal_enabled_config(config)
    session, target = _make_agent(session_factory, name="target", group_name="acme-acl")
    target.status = AgentStatus.READY
    target.capability = "fixes things"
    session.commit()
    _ready_agent(session, "viewer", group_name="acme-acl")
    fake_cabal = FakeCabal(
        relay_result={"status": {"state": "completed", "message": {"parts": [{"text": "ok"}]}}}
    )
    clients = _clients(cabal=fake_cabal)

    refresh_colleague_rosters(
        clients, cfg, session, name=target.name, group_name=target.group_name
    )

    assert len(fake_cabal.relay_calls) == 1
    call = fake_cabal.relay_calls[0]
    assert call["name"] == "viewer"
    envelope = call["task_envelope"]
    assert envelope["metadata"] == {"system_task": "knowledge", "filename": "colleagues.md"}
    delivered_text = envelope["status"]["message"]["parts"][0]["text"]
    assert "target" in delivered_text
    assert "fixes things" in delivered_text
    # Byte-for-byte the same render a fresh compose would produce for this
    # exact roster (`_render_colleagues_md`/`_colleague_roster`, the same
    # functions RepoStep itself calls at birth).
    viewer = session.query(Agent).filter_by(name="viewer").one()
    assert delivered_text == _render_colleagues_md(viewer, _colleague_roster(session, viewer))


# --- from test_directory.py ---


AGENT_NAME = "clio"


PERSONA = "helpful-generalist"


MODEL_ID = "vertex_ai/claude-sonnet-4-5"


REPO = "acme/agent-clio"


ENDPOINT_TEMPLATE = "http://agent-{name}.default.svc.cluster.local"


def _agent(**overrides) -> Agent:
    fields = {
        "name": AGENT_NAME,
        "persona": PERSONA,
        "model_id": MODEL_ID,
        "repo_full_name": REPO,
    }
    fields.update(overrides)
    return Agent(**fields)


def _config__directory(**overrides) -> Config:
    fields = {"agent_endpoint_template": ENDPOINT_TEMPLATE}
    fields.update(overrides)
    return Config(**fields)


def test_sync_directory_card_is_fetched_verbatim_not_recomposed():
    """Wire shape: Sync directory card is fetched verbatim not recomposed.

    The exact dict `clone` writes to `bundle/agent-card.json` must reach
    `graduate` byte-identically — never rebuilt via BundleSpec/
    kobo_api.composer, which could drift from whatever the runtime actually
    serves at its own well-known endpoint."""
    card = {
        "name": AGENT_NAME,
        "description": "hand-edited on purpose, not compose's default",
        "version": "9.9.9",
        "capabilities": ["something-unusual"],
        "skills": [],
        "metadata": {"team": "acme-team", "purpose_tags": ["billing"], "capability": "Helps."},
    }
    github = FakeGitHub(agent_card=card)
    cabal = FakeCabal()

    sync_directory(github, cabal, _config__directory(), _agent())

    assert cabal.graduate_calls[0]["card"] == card


def test_sync_directory_registers_with_name_endpoint_and_audience():
    """Wire shape: Sync directory registers with name endpoint and audience."""
    github = FakeGitHub()
    cabal = FakeCabal()

    sync_directory(github, cabal, _config__directory(), _agent())

    assert cabal.graduate_calls == [
        {
            "name": AGENT_NAME,
            "endpoint": f"http://agent-{AGENT_NAME}.default.svc.cluster.local",
            "audience": AGENT_NAME,
            "card": cabal.graduate_calls[0]["card"],
        }
    ]


# --- from test_tooling.py ---


REPO = "acme/agent-clio"


NAMESPACE = "clio"


DEPLOY = "clio"


NEWER_SHA = "newnewnewnewnewnewnewnewnewnewnewnewnewn"


OLDER_SHA = "oldoldoldoldoldoldoldoldoldoldoldoldoldo"


_DELIVERED_AND_PENDING_GITHUB_KWARGS = {
    "tooling_files": {"old.py": "stale = 1\n", "fresh.py": "brand_new = 1\n"},
    "intake_commit_shas": {
        "bundle/tools/old.py": OLDER_SHA,
        "bundle/tools/fresh.py": NEWER_SHA,
    },
    "rev_parse_results": {"oldoldoldold": OLDER_SHA},
}


def _mixed_delivery_github_and_kubectl():
    github = FakeGitHub(**_DELIVERED_AND_PENDING_GITHUB_KWARGS)
    kubectl = FakeKubectl(images={(NAMESPACE, DEPLOY): f"gojira-app:{OLDER_SHA[:12]}"})
    return github, kubectl


def test_list_tooling_attested_and_locked_reports_locked_file():
    """Wire shape: List tooling attested and locked reports locked file."""
    github, kubectl = _mixed_delivery_github_and_kubectl()
    agent = FakeAgent(
        card={
            "enforcement": {
                "version": 1,
                "hooks": ["old.py"],
                "locked": True,
                "locked_file": "old.py",
            }
        }
    )
    files, enforcement = list_tooling(
        github, kubectl, agent, repo=REPO, namespace=NAMESPACE, deploy=DEPLOY
    )
    assert enforcement == {"attested": True, "locked": True, "locked_file": "old.py"}
    # Files keep their plain delivered truth even when locked — list_tooling
    # itself never derives "status" (the route does).
    by_name = {f["name"]: f["delivered"] for f in files}
    assert by_name == {"old.py": True, "fresh.py": False}


# --- from test_metrics.py ---


def test_requests_land_in_the_red_series(client):
    """Wire shape: Requests land in the red series."""
    client.get("/healthz")
    body = client.get("/metrics").text
    # The observability layer's web profile queries these exact names.
    assert 'http_requests_total{method="GET"' in body
    assert "http_request_duration_seconds_bucket" in body


# --- from test_personas.py ---


def _fields(**overrides) -> PersonaFields:
    base = {
        "name": "Aria the Assistant",
        "who_they_are": "a calm troubleshooter for billing questions",
        "what_they_do": "Help customers fix billing problems quickly.",
    }
    base.update(overrides)
    return PersonaFields(**base)


def test_create_persona_writes_body_and_index_with_exact_keys_and_metadata():
    """Wire shape: Create persona writes body and index with exact keys and metadata."""
    storage = FakeStorage()
    fields = _fields()

    entry = create_persona(storage, bucket="personas-bucket", fields=fields, team="cs-team")

    # Create-flow polish (2026-08-28): tags are supplied-only -- _fields()
    # passes none, so the entry carries none. The retired derivation used
    # to invent six from the persona's own nouns.
    expected_tags = []

    assert entry.kind == "persona"
    assert entry.id == "aria-the-assistant"
    assert entry.name == "Aria the Assistant"
    assert entry.description == "a calm troubleshooter for billing questions"
    assert entry.team == "cs-team"
    assert entry.tags == expected_tags
    assert entry.builtin is False

    body = storage.read_bytes("personas-bucket", "aria-the-assistant/persona.md")
    assert body == render_persona(fields).encode()

    meta = storage.object_metadata("personas-bucket", "aria-the-assistant/persona.md")
    assert meta["metadata"] == {"team": "cs-team", "creator": "room"}

    index = json.loads(storage.read_bytes("personas-bucket", "index.json"))
    assert index["entries"] == [
        {
            "id": "aria-the-assistant",
            "name": "Aria the Assistant",
            "description": "a calm troubleshooter for billing questions",
            "team": "cs-team",
            "tags": expected_tags,
            "builtin": False,
        }
    ]


# --- from test_publish_skills.py ---


_TEMPLATES_DIR = Path(__file__).parent.parent / "kobo_api" / "templates" / "skills_publish"


_SCRIPT_PATH = _TEMPLATES_DIR / "publish_skills.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("publish_skills", _SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # dataclasses' postponed-annotation resolution (this script uses `from
    # __future__ import annotations`) looks the module up in `sys.modules`
    # by name — without registering it there first, `@dataclass` on `Skill`
    # raises AttributeError the moment this module is exec'd.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def ps():
    return _load_module()


def test_build_index_golden_sorted_by_team_then_id(ps, monkeypatch):
    """Wire shape: Build index golden sorted by team then id."""
    monkeypatch.setattr(ps, "_utcnow_iso", lambda: "2026-08-03T00:00:00Z")

    skills = [
        ps.Skill(
            id="commit-helper",
            team="platform",
            dir=Path("/x"),
            files=[Path("SKILL.md")],
            name="commit-helper",
            description="Writes commit messages",
            tags=["git"],
        ),
        ps.Skill(
            id="word-games",
            team="acme",
            dir=Path("/y"),
            files=[Path("SKILL.md")],
            name="word-games",
            description="Plays word games",
            tags=["fun"],
        ),
        ps.Skill(
            id="code-review",
            team="acme",
            dir=Path("/z"),
            files=[Path("SKILL.md")],
            name="code-review",
            description="Reviews PRs",
            tags=[],
        ),
    ]

    index = ps.build_index(skills, "abc123")

    assert index == {
        "generated_at": "2026-08-03T00:00:00Z",
        "entries": [
            {
                "id": "code-review",
                "name": "code-review",
                "description": "Reviews PRs",
                "team": "acme",
                "tags": [],
                "sha": "abc123",
            },
            {
                "id": "word-games",
                "name": "word-games",
                "description": "Plays word games",
                "team": "acme",
                "tags": ["fun"],
                "sha": "abc123",
            },
            {
                "id": "commit-helper",
                "name": "commit-helper",
                "description": "Writes commit messages",
                "team": "platform",
                "tags": ["git"],
                "sha": "abc123",
            },
        ],
    }


# --- from test_bootstrap.py ---


GITHUB_ORG = "acme-corp"


SKILLS_BUCKET__bootstrap = "acme-corp-skills"


PERSONAS_BUCKET__bootstrap = "acme-corp-personas"


def _assert_no_leftover_placeholders(text: str) -> None:
    """Assert that no placeholder tokens from the template remain unsubstituted.

    Checks for leftover __KOBO_*__ tokens only. This is precise (checks actual
    template tokens) rather than broad (checking all bare {word} patterns).

    The rendering uses literal token replacement (str.replace), never .format()
    or Jinja, so an unsubstituted __KOBO_* token in the output indicates a
    rendering error. Config values legitimately contain bare {word} patterns
    (e.g., agent_endpoint_template="http://agent-{name}.svc"), so we don't
    flag those — only the actual substitution tokens matter.
    """
    assert "__KOBO_" not in text


@pytest.fixture
def config__bootstrap(tmp_path):
    """The PRE-SET-buckets path: skills_bucket/personas_bucket are given
    explicitly, so every test using this fixture exercises bootstrap with
    those raw Config fields already non-empty. `fresh_config` below is the
    other path — a TRUE fresh org with neither field set at all — kept as a
    separate fixture so Finding 2's regression (the catalog-ingest fallback
    silently seeing `{}` when the raw fields are still "") stays covered by
    a test that can't accidentally pass because these fields happen to be set.
    """
    return Config(
        github_org=GITHUB_ORG,
        workdir=tmp_path / "work",
        db_url=f"sqlite:///{tmp_path / 'kobo.db'}",
        skills_bucket=SKILLS_BUCKET__bootstrap,
        personas_bucket=PERSONAS_BUCKET__bootstrap,
    )


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """Every run touches step 6, which tries `httpx.post` against
    `config.api_base_url` first — no real kobo-api server is up during these
    tests ("Tests (all against fakes; no network)" per the brief), so this
    simulates "not reachable yet" by default for every test here. Tests that
    want the primary (refresh-endpoint) path override `bootstrap.httpx.post`
    themselves.
    """

    def _raise(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(bootstrap.httpx, "post", _raise)


def test_render_org_yaml_golden_with_computed_defaults():
    """Wire shape: Render org yaml golden with computed defaults."""
    config__bootstrap = Config(
        github_org=GITHUB_ORG,
        gojira_cluster="acme-cluster",
        runtime_image="registry.example/agents/daedalus-runtime:v1",
        gcp_project="acme-project",
        vertex_project="acme-models",
        vertex_location="us-east5",
        model_group="models@acme-corp.example",
        avatar_model="gemini-2.5-flash-image",
        avatar_location="us-central1",
        skills_publish_wif_provider=(
            "projects/123/locations/global/workloadIdentityPools/acme/providers/github"
        ),
        skills_publish_service_account="skills-publisher@acme-project.iam.gserviceaccount.com",
    )

    rendered = render_org_yaml(config__bootstrap)
    _assert_no_leftover_placeholders(rendered)

    parsed = yaml.safe_load(rendered)
    assert set(parsed) == ORG_KEYS
    assert parsed == {
        "github_org": "acme-corp",
        "skills_repo": "acme-corp-skills",
        "skills_bucket": "acme-corp-skills",
        "personas_bucket": "acme-corp-personas",
        "gojira_cluster": "acme-cluster",
        "runtime_image": "registry.example/agents/daedalus-runtime:v1",
        "memory_replica_bucket": "",
        "broker_url": "",
        "models_gateway_url": "",
        "liminal_url": "",
        "bab_url": "",
        "noto_url": None,
        "noto_project": None,
        "reader_groups": [],
        "gcp_project": "acme-project",
        "vertex_project": "acme-models",
        "vertex_location": "us-east5",
        "model_group": "models@acme-corp.example",
        "avatar_model": "gemini-2.5-flash-image",
        "avatar_location": "us-central1",
        "models": [{"id": "vertex_ai/claude-sonnet-4-5", "label": "Claude Sonnet 4.5"}],
        "skills_publish_wif_provider": (
            "projects/123/locations/global/workloadIdentityPools/acme/providers/github"
        ),
        "skills_publish_service_account": (
            "skills-publisher@acme-project.iam.gserviceaccount.com"
        ),
        "skills_publish_external": False,
        "slack_operators": [],
        "operators": [],
        "admin_groups": [],
        "admin_panel": False,
        "slack_relay": False,
        "logout_url": None,
        "gateway_config_repo": "",
        "gateway_namespace": "",
        "liminal_admin_url": "",
        "cabal_url": None,
        "agent_endpoint_template": None,
        "groups": [],
        "okta_domain": "",
        "mcp": {
            "oauth_client_id": "",
            "oauth_client_secret_env": "KOBO_MCP_GOOGLE_CLIENT_SECRET",
            "workspace_domain": "",
            "issuer_base_url": "",
            "jwt_key_env": "KOBO_MCP_JWT_KEY",
        },
    }


# --- from test_org.py ---


ORG_REPO = "acme-corp/kobo-config"


def test_org_yaml_overrides_local_for_org_keys():
    """Wire shape: Org yaml overrides local for org keys."""
    local = Config(org_repo=ORG_REPO)
    github = FakeGitHub(
        org_yaml=(
            "github_org: acme-corp\n"
            "gojira_cluster: acme-cluster\n"
            "vertex_project: acme-models\n"
        )
    )

    merged, org = load_org_config(local, github)

    assert org.wired is True
    assert org.repo == ORG_REPO
    assert org.error is None
    assert merged.github_org == "acme-corp"
    assert merged.gojira_cluster == "acme-cluster"
    assert merged.vertex_project == "acme-models"
    # Fields org.yaml didn't touch keep the local (default) value.
    assert merged.vertex_location == local.vertex_location
    assert merged.workdir == local.workdir
    assert merged.db_url == local.db_url


# --- from test_bundle_publish.py ---


REPOSITORY = "us-east4-docker.pkg.dev/example/agents"


def test_bundle_reference_shape():
    """Wire shape: Bundle reference shape."""
    assert bundle_reference(REPOSITORY + "/", "sam", "abc") == f"{REPOSITORY}/sam-bundle:abc"



# --- asking another team's agent, with the permission service on (kobo#310) -----


def test_asking_another_teams_agent_is_a_permission_request_on_the_target_not_an_invite(a2a_room):
    """Wire shape (kobo#310): what bab receives, `{agent, integration: <target>,
    permissions: ["a2a_ask"]}` on a target that is an `agents` integration with
    ceiling ["a2a_ask"]; what the UI reads back, the target under `already`
    while it waits and a pair `via: "grant"` once approved. No A2AInvite row."""
    c, h, bab = a2a_room.client, a2a_room.headers, a2a_room.bab
    r = c.post("/api/agents/asker/access/requests", headers=h,
               json={"integration": "oracle", "permissions": ["a2a_ask"], "note": "needs its view"})
    assert r.status_code == 201, r.text
    assert (r.json()["agent"], r.json()["integration"], r.json()["permissions"]) == ("asker", "oracle", ["a2a_ask"])
    (target,) = bab.integrations("ops@example.com")
    assert (target["integration"], target["vendor"], target["ceiling"]) == ("oracle", "agents", ["a2a_ask"])
    with a2a_room.session_factory() as s:
        assert s.query(A2AInvite).count() == 0
    assert c.get("/api/agents/asker/requestable", headers=h).json()["already"] == [{"name": "oracle", "group": "security", "status": "pending"}]
    assert c.get("/api/agents/asker/pairs", headers=h).json()["pairs"] == []

    c.post("/api/admin/access/decide", headers=h, json={"ids": [r.json()["id"]], "verdict": "approve"})
    assert c.get("/api/agents/asker/pairs", headers=h).json()["pairs"] == [{"name": "oracle", "group": "security", "via": "grant"}]
    assert c.get("/api/agents/asker/requestable", headers=h).json()["already"] == [{"name": "oracle", "group": "security", "status": "accepted"}]


GITHUB_LEVELS__connections = ["github_pulls_review", "github_pulls_write", "github_repos_read"]


def test_agent_connections_group_a_child_a_grant_and_a_lease_under_the_agent_and_its_vendor(a2a_room):
    """Wire shape (kobo#371): `GET /api/admin/access/agents` lists every agent the room
    knows, newest activity first; under the agent, by vendor, its own connection (the
    GitHub child with its repositories), what it holds and what is leased right now; an
    agent with nothing is still listed, with no vendors. The child never reaches the
    Integrations table, which is the room's connections only."""
    c, h, bab = a2a_room.client, a2a_room.headers, a2a_room.bab
    bab.put_integration("github", "github", {"installation_id": "151256501", "name": "GitHub"}, GITHUB_LEVELS__connections, "x")
    bab.put_integration("github-asker", "github", {"repositories": ["acme/web", "acme/infra"], "name": "GitHub for asker"},
                        GITHUB_LEVELS__connections, "x", parent="github")
    r = c.post("/api/agents/asker/access/requests", headers=h,
               json={"integration": "github-asker", "permissions": ["github_repos_read"], "note": None})
    assert r.status_code == 201, r.text
    c.post("/api/admin/access/decide", headers=h, json={"ids": [r.json()["id"]], "verdict": "approve"})
    bab._leases.extend([
        {"id": 1, "agent": "asker", "integration": "github-asker", "permissions": ["github_repos_read"], "task": "list the open pull requests",
         "minter": "github", "issued_at": "2099-01-01T00:00:00+00:00", "expires_at": "2099-01-01T01:00:00+00:00"},
        {"id": 2, "agent": "asker", "integration": "github-asker", "permissions": ["github_repos_read"], "task": "old",
         "minter": "github", "issued_at": "2020-01-01T00:00:00+00:00", "expires_at": "2020-01-01T01:00:00+00:00"},
    ])

    body = c.get("/api/admin/access/agents", headers=h).json()
    assert body["fetched_at"]
    assert [a["name"] for a in body["agents"]] == ["asker", "oracle"]
    asker, oracle = body["agents"]
    assert (asker["team"], asker["status"]) == (None, "ready")
    (github,) = asker["vendors"]
    assert (github["vendor"], github["label"]) == ("github", "GitHub")
    assert github["connections"] == [{"id": "github-asker", "name": "GitHub for asker", "repositories": ["acme/infra", "acme/web"]}]
    (grant,) = github["grants"]
    assert (grant["integration"], grant["permission"], grant["tier"], grant["granted_by"], grant["state"]) == (
        "github-asker", "github_repos_read", "read", "ops@example.com", "active")
    assert set(grant) == {"id", "integration", "permission", "tier", "expires_at", "granted_by", "state"}
    (lease,) = github["leases"]  # the run-out one is not "right now"
    assert (lease["id"], lease["integration"], lease["permissions"], lease["task"], lease["via"]) == (
        1, "github-asker", ["github_repos_read"], "list the open pull requests", "github")
    assert set(lease) == {"id", "integration", "permissions", "task", "issued_at", "expires_at", "via"}
    assert oracle["vendors"] == []

    rows = c.get("/api/admin/integrations", headers=h).json()["integrations"]
    assert [x["id"] for x in rows if x["vendor"] == "github"] == ["github"]


def test_access_summary_counts_agents_that_hold_access_without_a_grant_call_per_agent(a2a_room):
    """Wire shape (admin Overview, 2026-11-01): `GET /api/admin/access/summary` is the
    Agent connections card's figure, `agents_with_access` over `agents`, from the same
    reads the pane starts with (connections, leases, requests) and never a grant call
    per agent, which the Overview must not pay for."""
    c, h, bab = a2a_room.client, a2a_room.headers, a2a_room.bab
    bab.put_integration("github", "github", {"installation_id": "151256501", "name": "GitHub"}, GITHUB_LEVELS__connections, "x")
    r = c.post("/api/agents/asker/access/requests", headers=h,
               json={"integration": "github", "permissions": ["github_repos_read"], "note": None})
    assert r.status_code == 201, r.text
    assert c.get("/api/admin/access/summary", headers=h).json()["agents_with_access"] == 0  # asked is not held
    c.post("/api/admin/access/decide", headers=h, json={"ids": [r.json()["id"]], "verdict": "approve"})

    grant_calls = []
    bab.grants = lambda agent, actor, _real=bab.grants: grant_calls.append(agent) or _real(agent, actor)
    body = c.get("/api/admin/access/summary", headers=h).json()
    assert (body["agents_with_access"], body["agents"]) == (1, 2)
    assert body["fetched_at"]
    assert grant_calls == []


def test_agents_entries_never_show_on_the_integrations_page(a2a_room):
    """Wire shape (kobo#310): the permission service's `agents` rows are plumbing behind
    Review and the Access tab; Admin > Integrations lists vendors only."""
    c, h, _ = a2a_room.client, a2a_room.headers, a2a_room.bab
    r = c.post("/api/agents/asker/access/requests", headers=h, json={"integration": "oracle", "permissions": ["a2a_ask"], "note": None})
    assert r.status_code == 201, r.text
    rows = c.get("/api/admin/integrations", headers=h).json()["integrations"]
    assert [x for x in rows if x["vendor"] == "agents"] == []



# --- the room's audit log (issue #316) ---


def _audit_client(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    private_key, jwk = _generate_jwk()
    config = Config(workdir=tmp_path / "work", iap_audience=WHOAMI_AUDIENCE, operators=["ops@example.com"], admin_panel=True)
    app = create_app(session_factory, config=config, iap_jwks_fetcher=_fetcher(jwk))
    with session_factory() as s:
        for i, (actor, action, agent) in enumerate((
            ("ops@example.com", "agent.create", "ward"),
            ("someone@example.com", "skills.add", "ward"),
            ("ops@example.com", "agent.create", "sage"),
            ("room", "audit.prune", None),
        )):
            room_audit.record(s, action=action, target_kind="agent" if agent else "audit_log", target_id=agent or "audit_log",
                         agent_name=agent, actor=actor, summary=f"{actor} did {action}",
                         at=datetime(2026, 10, 30, 12, 0, i))
        s.commit()
    return TestClient(app), {ASSERTION_HEADER: _sign_whoami(private_key, email="ops@example.com")}


def test_audit_page_shape_and_cursor(tmp_path):
    """Wire shape (issue #316): the Audit page reads rows newest first with
    `next_cursor`, `total_in_window` and the facets; the cursor walks to the
    end without repeating or skipping a row."""
    client, headers = _audit_client(tmp_path)

    first = client.get("/api/admin/audit?limit=3", headers=headers).json()

    assert set(first) >= {"rows", "next_cursor", "total_in_window", "actors", "agents", "families"}
    assert first["total_in_window"] == 4 and len(first["rows"]) == 3 and first["next_cursor"]
    assert set(first["rows"][0]) >= {"id", "at", "actor", "actor_kind", "action", "target_kind", "target_id",
                                     "agent_name", "summary", "outcome", "source", "request_ip", "landed", "detail"}
    assert first["rows"][0]["at"].endswith("Z") and first["rows"][0]["action"] == "audit.prune"
    assert first["families"] == ["agent", "audit", "skills"] and first["agents"] == ["sage", "ward"]
    rest = client.get(f"/api/admin/audit?limit=3&cursor={first['next_cursor']}", headers=headers).json()
    assert [r["id"] for r in first["rows"]] + [r["id"] for r in rest["rows"]] == [4, 3, 2, 1]
    assert rest["next_cursor"] is None


def test_audit_export_matches_the_filter(tmp_path):
    """Wire shape (issue #316): the export carries exactly the rows the same
    filter pages, as CSV with a header or as a JSON array."""
    client, headers = _audit_client(tmp_path)

    page = client.get("/api/admin/audit?agent=ward&action=agent", headers=headers).json()
    csv_body = client.get("/api/admin/audit/export?agent=ward&action=agent", headers=headers)
    json_body = client.get("/api/admin/audit/export?agent=ward&action=agent&format=json", headers=headers)

    assert [r["id"] for r in page["rows"]] == [1]
    assert csv_body.headers["content-type"].startswith("text/csv")
    lines = csv_body.text.strip().splitlines()
    assert lines[0].startswith("id,at,actor,") and len(lines) == 2 and ",agent.create," in lines[1]
    assert [r["id"] for r in json_body.json()] == [1]


def test_a_request_on_a_github_child_carries_the_repository_set_for_review(a2a_room):
    """Wire shape (kobo#365): a permission request targets the agent's GitHub child, and the
    Review pane's rows and decide results carry `repositories` (the child's set) so a reviewer
    sees what the level would open; a request on anything else carries none."""
    c, h, bab = a2a_room.client, a2a_room.headers, a2a_room.bab
    levels = ["github_pulls_review", "github_pulls_write", "github_repos_read"]
    bab.put_integration("github", "github", {"installation_id": "151256501", "name": "GitHub"}, levels, "ops@example.com")
    c.app.state.clients.github = FakeGitHub(existing_repos={"acme/infra", "acme/web"})
    c.app.state.repo_index = RepoIndex(c.app.state.clients.github)
    put = c.put("/api/agents/asker/github/repositories", headers=h, json={"repositories": ["acme/infra"]})
    assert put.status_code == 200, put.text
    r = c.post("/api/agents/asker/access/requests", headers=h,
               json={"integration": "github-asker", "permissions": ["github_pulls_review"], "note": "reviews"})
    assert r.status_code == 201, r.text
    other = c.post("/api/agents/asker/access/requests", headers=h,
                   json={"integration": "oracle", "permissions": ["a2a_ask"], "note": None}).json()
    rows = {row["id"]: row for row in c.get("/api/admin/access/requests", headers=h).json()["requests"]}
    assert rows[r.json()["id"]]["repositories"] == ["acme/infra"] and rows[r.json()["id"]]["integration"] == "github-asker"
    assert "repositories" not in rows[other["id"]]
    (result,) = c.post("/api/admin/access/decide", headers=h, json={"ids": [r.json()["id"]], "verdict": "approve"}).json()["results"]
    assert result["status"] == "approved" and result["repositories"] == ["acme/infra"]
    page = c.get("/api/agents/asker/access", headers=h).json()
    gh = next(v for v in page["vendors"] if v["id"] == "github")
    assert gh["accounts"][0]["id"] == "github-asker" and gh["accounts"][0]["has_repo_set"] is True
    assert [g["integration"] for g in page["grants"]] == ["github-asker"]


# --- the Audit page's Agents tab (kobo#371) ---


def _agents_audit(a2a_room):
    bab = a2a_room.bab
    bab.put_integration("aws-prod", "aws", {"role_arn": "arn:aws:iam::111111111111:role/Access"}, ["aws_inventory_read"], "ops@example.com")
    bab._leases.append({"id": 7, "agent": "asker", "integration": "aws-prod", "permissions": ["aws_inventory_read"],
                        "task": "count instances", "minter": "aws", "via": "gateway",
                        "issued_at": "2026-10-30T12:00:00", "expires_at": "2026-10-30T13:00:00"})
    bab._events.append({"id": 3, "at": "2026-10-30T12:05:00", "kind": "lease.refused", "actor": "asker",
                        "detail": {"agent": "asker", "integration": "aws-prod", "permissions": ["aws_ecs_update_service"],
                                   "reason": "not granted", "via": None}})
    bab._events.append({"id": 4, "at": "2026-10-30T12:10:00", "kind": "lease.failed", "actor": "oracle",
                        "detail": {"agent": "oracle", "integration": "aws-prod", "permissions": ["aws_inventory_read"],
                                   "reason": "the vendor is away", "error": "the vendor is away", "via": "gateway"}})
    bab._events.append({"id": 5, "at": "2026-10-30T12:11:00", "kind": "sweep", "actor": "ops@example.com", "detail": {}})
    return a2a_room.client, a2a_room.headers


def test_agents_audit_merges_leases_and_refusals_into_audit_rows(a2a_room):
    """Wire shape (kobo#371): the Agents tab reads the permission service's leases and its
    refused and failed events as one list, newest first, in the room's audit row shape with
    one sentence each; its facets are the agents and outcomes in the window, and the filters
    the pane sends (agent, outcome, q) narrow it."""
    c, h = _agents_audit(a2a_room)

    body = c.get("/api/admin/audit/agents", headers=h).json()

    assert set(body) == {"rows", "total_in_window", "agents", "outcomes"}
    assert body["total_in_window"] == 3 and body["agents"] == ["asker", "oracle"] and body["outcomes"] == ["failed", "ok", "refused"]
    assert [(r["action"], r["outcome"], r["summary"]) for r in body["rows"]] == [
        ("lease.failed", "failed", "oracle could not borrow read on AWS aws-prod: the vendor is away"),
        ("lease.refused", "refused", "asker was refused write on AWS aws-prod: not granted"),
        ("lease.minted", "ok", "asker borrowed read on AWS aws-prod for 1h: count instances"),
    ]
    minted = body["rows"][2]
    assert set(minted) >= {"id", "at", "actor", "actor_kind", "action", "target_kind", "target_id", "agent_name",
                           "summary", "outcome", "source", "detail", "landed", "request_ip"}
    assert (minted["actor"], minted["actor_kind"], minted["agent_name"]) == ("agent:asker", "agent", "asker")
    assert (minted["target_kind"], minted["target_id"], minted["source"]) == ("integration", "aws-prod", "permission-service")
    assert minted["at"] == "2026-10-30T12:00:00Z" and minted["detail"]["expires_at"] == "2026-10-30T13:00:00Z"
    assert minted["detail"]["permissions"] == ["aws_inventory_read"] and minted["detail"]["via"] == "gateway"
    assert [r["action"] for r in c.get("/api/admin/audit/agents?agent=asker", headers=h).json()["rows"]] == ["lease.refused", "lease.minted"]
    assert [r["action"] for r in c.get("/api/admin/audit/agents?outcome=ok", headers=h).json()["rows"]] == ["lease.minted"]
    assert [r["action"] for r in c.get("/api/admin/audit/agents?q=vendor", headers=h).json()["rows"]] == ["lease.failed"]
    assert c.get("/api/admin/audit/agents?since=2026-10-30T12:04:00Z&until=2026-10-30T12:09:00Z", headers=h).json()["total_in_window"] == 1


def test_agents_audit_export_carries_the_same_window(a2a_room):
    """Wire shape (kobo#371): `format=csv|json` on the Agents route hands the same filtered
    window back as a file, CSV with a header or a JSON array, named apart from the people export."""
    c, h = _agents_audit(a2a_room)

    csv_body = c.get("/api/admin/audit/agents?agent=asker&format=csv", headers=h)
    json_body = c.get("/api/admin/audit/agents?agent=asker&format=json", headers=h)

    assert csv_body.headers["content-type"].startswith("text/csv")
    assert "audit-agents-" in csv_body.headers["content-disposition"]
    lines = csv_body.text.strip().splitlines()
    assert lines[0].startswith("id,at,actor,actor_kind,action,") and len(lines) == 3
    assert ",lease.refused," in lines[1] and ",lease.minted," in lines[2]
    assert [r["action"] for r in json_body.json()] == ["lease.refused", "lease.minted"]



def test_the_access_tab_carries_each_vendors_last_failed_lease_until_a_newer_lease_clears_it(a2a_room):
    """Wire shape (kobo#382, owner ask 2026-11-01): each vendor on `GET /api/agents/{name}/access`
    carries `last_failure`, the newest failed or refused lease for THIS agent at THAT vendor
    in the last day as `{at, kind, reason}`, so the owner reads why the agent's credential
    did not resolve. Another agent's failure, one older than a day and a lease issued before
    the failure leave it standing; a lease at the vendor issued after it clears it; and the
    service's events being away leaves every vendor without one instead of failing the tab."""
    from datetime import timedelta

    c, h, bab = a2a_room.client, a2a_room.headers, a2a_room.bab
    bab.put_integration("aws-prod", "aws", {"role_arn": "arn:aws:iam::111111111111:role/Access"}, ["aws_inventory_read"], "ops@example.com")
    now = datetime.now(UTC)
    def stamp(minutes: int) -> str:
        return (now - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S")
    failed = {"agent": "asker", "integration": "aws-prod", "permissions": ["aws_inventory_read"],
              "reason": "the vendor answered 422 for the narrowed token", "error": "422 Unprocessable Entity"}
    bab._events.extend([
        {"id": 1, "at": stamp(25 * 60), "kind": "lease.failed", "actor": "asker", "detail": {**failed, "reason": "yesterday's"}},
        {"id": 2, "at": stamp(30), "kind": "lease.refused", "actor": "asker", "detail": {**failed, "reason": "not granted"}},
        {"id": 3, "at": stamp(12), "kind": "lease.failed", "actor": "asker", "detail": failed},
        {"id": 4, "at": stamp(1), "kind": "lease.failed", "actor": "oracle", "detail": {**failed, "agent": "oracle"}},
    ])
    bab._leases.append({"id": 7, "agent": "asker", "integration": "aws-prod", "permissions": ["aws_inventory_read"], "task": "before",
                        "minter": "aws", "issued_at": stamp(20), "expires_at": stamp(-40)})

    page = c.get("/api/agents/asker/access", headers=h).json()
    by_id = {v["id"]: v for v in page["vendors"]}
    assert by_id["aws"]["last_failure"] == {"at": stamp(12) + "Z", "kind": "lease.failed",
                                            "reason": "the vendor answered 422 for the narrowed token"}
    assert all(v["last_failure"] is None for vid, v in by_id.items() if vid != "aws")

    bab._leases.append({"id": 8, "agent": "asker", "integration": "aws-prod", "permissions": ["aws_inventory_read"], "task": "after",
                        "minter": "aws", "issued_at": stamp(5), "expires_at": stamp(-55)})
    assert c.get("/api/agents/asker/access", headers=h).json()["vendors"][0]["last_failure"] is None

    del bab._leases[-1]

    def away(*_args, **_kwargs):
        raise ClientError("the permission service is away")

    bab.events = away
    r = c.get("/api/agents/asker/access", headers=h)
    assert r.status_code == 200 and all(v["last_failure"] is None for v in r.json()["vendors"])



# --- the health block's `rolling` (2026-11-01) ---

_SETTLED_STATUS = FAKE_DEPLOYMENT_STATUS_SETTLED["status"]
_PROGRESSING = {"type": "Progressing", "status": "True", "reason": "ReplicaSetUpdated"}
_FINISHED = {"type": "Progressing", "status": "True", "reason": "NewReplicaSetAvailable"}


_READY_SINCE = "2026-11-01T09:00:00Z"


@pytest.mark.parametrize(
    ("dep", "state", "rolling", "ready_since"),
    [
        # Recreate gap: the old pod is gone, the new one is on the new spec but not ready yet.
        ({"metadata": {"generation": 2}, "spec": {"replicas": 1},
          "status": {"observedGeneration": 2, "replicas": 1, "updatedReplicas": 1, "readyReplicas": 0,
                     "conditions": [_PROGRESSING]}}, "down", True, None),
        # A change was just asked for: the controller has not caught up with the spec; the old pod still serves.
        ({"metadata": {"generation": 3}, "spec": {"replicas": 1}, "status": {**_SETTLED_STATUS, "observedGeneration": 2}},
         "up", True, _READY_SINCE),
        # Recreate's first step: the old pod is being taken down, nothing is on the new spec yet.
        ({"metadata": {"generation": 2}, "spec": {"replicas": 1},
          "status": {"observedGeneration": 2, "replicas": 0, "updatedReplicas": 0, "conditions": [_PROGRESSING]}},
         "down", True, None),
        # Crashed after a finished rollout (lumbot's shape): down, and not a restart.
        ({"metadata": {"generation": 3}, "spec": {"replicas": 1},
          "status": {"observedGeneration": 3, "replicas": 1, "updatedReplicas": 1, "readyReplicas": 0,
                     "conditions": [_FINISHED]}}, "down", False, None),
        (FAKE_DEPLOYMENT_STATUS_SETTLED, "up", False, _READY_SINCE),
    ],
    ids=["recreate-gap", "unfinished-generation", "old-pod-going", "crashed-after-rollout", "settled"],
)
def test_health_reports_rolling_for_a_recreate_gap_and_an_unfinished_generation_but_not_a_crash(dep, state, rolling, ready_since):
    """Wire shape: the agent payload's `health.rolling` is true while the agent is
    mid-restart (a Recreate gap, or a generation the controller has not caught up
    with), so the page says Restarting and the Slack row's Live waits; it is false
    for a pod that is simply down, so a crash loop is never dressed as a restart.
    `ready_since` is when the agent last became available, None while it is not."""
    kubectl = FakeKubectl(deployment_status={("clio", "clio"): dep})

    verdict = probe_health(kubectl, "clio", now=datetime(2026, 11, 1, 12, 0, tzinfo=UTC))

    assert (verdict.state, verdict.rolling, verdict.ready_since) == (state, rolling, ready_since)
    assert verdict.body()["rolling"] is rolling


# --- the model gateway (kobo#390) -------------------------------------------------

MODELS_GATEWAY_URL = "http://models-gateway.models.svc"


def _vertex_provider(session, *, row_id="gcp-example", project="example-model-project", location="us-east5", state=admin_store.STATE_APPLIED):
    """A Google Cloud project row that serves the room's models (kobo#390)."""
    admin_store.put_integration(
        session, vendor="gcp", name="Google Cloud example", base_access="read", integration_id=row_id,
        spec={"kind": "account", "project": project, "models": {"provider": "vertex_ai", "location": location}},
        credential_ref=None, actor="ops@example.com",
    )
    admin_store.set_integration_state(session, row_id, state=state, detail="connected")
    session.commit()


def _models_room(tmp_path, *, gateway, models_gateway_url=MODELS_GATEWAY_URL, github=None, gojira=None, models=None):
    """A room with the model gateway on, an operator signed in through IAP
    and the admin panel open. Returns the client, the operator's headers
    and the session factory, so a test seeds its own provider rows. `models`
    replaces the two Vertex entries of the org's list."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    config = Config(
        workdir=tmp_path / "work", iap_audience=WHOAMI_AUDIENCE, operators=["ops@example.com"], admin_panel=True,
        models_gateway_url=models_gateway_url,
        models=models or [
            ModelChoice(id="vertex_ai/claude-sonnet-4-5", label="Claude Affordable Sonnet", family="Claude", tier="balanced"),
            ModelChoice(id="vertex_ai/gemini-2.5-flash", label="Gemini Fast Flash", family="Gemini", tier="fast", location="global"),
        ],
    )
    private_key, jwk = _generate_jwk()
    clients = Clients(github=github or FakeGitHub(), gojira=gojira or FakeGojira(), cloud=FakeCloud(), agent=FakeAgent(),
                      kubectl=FakeKubectl(), models_gateway=gateway)
    app = create_app(session_factory, config=config, clients=clients, iap_jwks_fetcher=_fetcher(jwk))
    return TestClient(app), {ASSERTION_HEADER: _sign_whoami(private_key, email="ops@example.com")}, session_factory


def test_catalog_models_answers_from_the_providers_with_available_and_reason(tmp_path):
    """Wire shape (kobo#390): with the gateway on, each model says which cloud
    serves it and whether it can be picked; nothing connected reads as
    unavailable with the sentence naming the cloud, the gateway down reads
    as unavailable with its own sentence, and `default` is the first
    available model or null."""
    gateway = FakeModelsGateway()
    client, headers, session_factory = _models_room(tmp_path, gateway=gateway)

    before = client.get("/api/catalog/models", headers=headers).json()
    assert before["default"] is None
    assert [m["available"] for m in before["models"]] == [False, False]
    assert before["models"][0]["reason"] == REASON_NOT_CONNECTED.format(cloud="Google Cloud")
    assert before["models"][0]["provider_label"] == "Google Cloud"
    assert before["models"][0]["gateway_model"] == "claude-affordable-sonnet"

    with session_factory() as session:
        _vertex_provider(session)
    connected = client.get("/api/catalog/models", headers=headers).json()
    assert connected["default"] == "vertex_ai/claude-sonnet-4-5"
    assert [m["available"] for m in connected["models"]] == [True, True]
    assert connected["gateway"] is True

    gateway.up = False
    down = client.get("/api/catalog/models", headers=headers).json()
    assert down["default"] is None
    assert {m["reason"] for m in down["models"]} == {REASON_GATEWAY_DOWN}


def _bedrock_provider(session, *, row_id, name, region="us-east-1"):
    """An AWS account row that serves the room's Bedrock models (kobo#390)."""
    role = f"arn:aws:iam::123456789012:role/KoboModels-{row_id}"
    admin_store.put_integration(
        session, vendor="aws", name=name, base_access="read", integration_id=row_id,
        spec={"auth": "aws_role", "identity": {"role_arn": role, "audience": "sts.amazonaws.com"},
              "models": {"provider": "bedrock", "region": region, "role_arn": role}},
        credential_ref=None, actor="ops@example.com",
    )
    admin_store.set_integration_state(session, row_id, state=admin_store.STATE_APPLIED, detail="connected")
    session.commit()


def test_catalog_models_lists_bedrock_entries_under_aws_once_an_account_with_bedrock_is_connected(tmp_path):
    """Wire shape (kobo#390): a `bedrock/` entry of the org's list reads as
    AWS's; with no account ticked it is unavailable with the sentence naming
    AWS with Bedrock, with one it is available under "AWS", and with several
    accounts the label names them so a person sees which answer."""
    gateway = FakeModelsGateway()
    client, headers, session_factory = _models_room(tmp_path, gateway=gateway, models=[
        ModelChoice(id="bedrock/anthropic.claude-3-5-haiku-20241022-v1:0", label="Claude Cheap on AWS (Haiku)", family="Claude", tier="fast"),
        ModelChoice(id="vertex_ai/claude-sonnet-4-5", label="Claude Affordable Sonnet", family="Claude", tier="balanced"),
    ])

    before = client.get("/api/catalog/models", headers=headers).json()
    haiku = before["models"][0]
    assert haiku["provider"] == "bedrock" and haiku["provider_label"] == "AWS" and haiku["gateway_model"] == "claude-cheap-on-aws-haiku"
    assert haiku["available"] is False and haiku["reason"] == "Ask a kobo admin to connect AWS with Bedrock"

    with session_factory() as session:
        _bedrock_provider(session, row_id="acme-models", name="acme-models")
    one = client.get("/api/catalog/models", headers=headers).json()
    assert one["default"] == "bedrock/anthropic.claude-3-5-haiku-20241022-v1:0"
    assert [(m["available"], m["provider_label"]) for m in one["models"]] == [(True, "AWS"), (False, "Google Cloud")]

    with session_factory() as session:
        _bedrock_provider(session, row_id="acme-lab", name="acme-lab", region="us-west-2")
    two = client.get("/api/catalog/models", headers=headers).json()
    assert two["models"][0]["provider_label"] in ("AWS (acme-models, acme-lab)", "AWS (acme-lab, acme-models)")
    # The Services row counts both accounts as providers.
    assert model_providers.gateway_view(session_factory(), client.app.state.config, client.app.state.clients).providers == 2


def test_catalog_models_lists_a_self_hosted_rows_answering_models_under_its_name(tmp_path):
    """Wire shape (kobo#390): a Self-hosted models row adds its answering
    models to the catalogue under the row's own name as `provider_label`,
    provider `self-hosted`, id `self-hosted/<row>/<name>`, available while
    the gateway answers; a model the probe marked failed is not offered,
    and the org's own list is untouched."""
    gateway = FakeModelsGateway()
    client, headers, session_factory = _models_room(tmp_path, gateway=gateway)
    with session_factory() as session:
        admin_store.put_integration(
            session, vendor="self-hosted", name="Lab GPU pool", base_access="read", integration_id="lab-gpu-pool",
            spec={"auth": "self_hosted_endpoint", "base_url": "http://10.0.0.5/v1", "options": {},
                  "models": [{"name": "qwen3-32b", "label": "Qwen3 32B", "ok": True, "reason": None},
                             {"name": "old-model", "label": "old-model", "ok": False, "reason": "HTTP 404"}]},
            credential_ref=None, actor="ops@example.com",
        )
        admin_store.set_integration_state(session, "lab-gpu-pool", state=admin_store.STATE_APPLIED, detail="connected")
        session.commit()

    body = client.get("/api/catalog/models", headers=headers).json()

    assert [m["id"] for m in body["models"]] == [
        "vertex_ai/claude-sonnet-4-5", "vertex_ai/gemini-2.5-flash", "self-hosted/lab-gpu-pool/qwen3-32b"]
    own = body["models"][2]
    assert own == {
        "id": "self-hosted/lab-gpu-pool/qwen3-32b", "label": "Qwen3 32B", "family": None, "tier": None,
        "provider": "self-hosted", "provider_label": "Lab GPU pool", "gateway_model": "lab-gpu-pool-qwen3-32b",
        "available": True, "reason": None}
    assert body["default"] == "self-hosted/lab-gpu-pool/qwen3-32b"
    gateway.up = False
    assert client.get("/api/catalog/models", headers=headers).json()["models"][2]["reason"] == REASON_GATEWAY_DOWN


def test_catalog_models_is_unchanged_with_the_gateway_off(tmp_path):
    """Wire shape (kobo#390): an install that has not set `models_gateway_url`
    gets exactly the payload it always did, so nothing moves until it does."""
    client, headers, _ = _models_room(tmp_path, gateway=None, models_gateway_url="")

    body = client.get("/api/catalog/models", headers=headers).json()

    assert body == {
        "models": [
            {"id": "vertex_ai/claude-sonnet-4-5", "label": "Claude Affordable Sonnet", "family": "Claude", "tier": "balanced"},
            {"id": "vertex_ai/gemini-2.5-flash", "label": "Gemini Fast Flash", "family": "Gemini", "tier": "fast"},
        ],
        "default": "vertex_ai/claude-sonnet-4-5",
    }


def test_services_models_row_states(tmp_path):
    """Wire shape (kobo#390): the Services row is off without the key, not
    reachable when the gateway does not answer, and on with the providers,
    models and default the filler sees."""
    off, headers, _ = _models_room(tmp_path / "off", gateway=None, models_gateway_url="")
    row = next(a for a in off.get("/api/admin/addons", headers=headers).json()["addons"] if a["id"] == "models")
    assert row["name"] == "Models"
    assert (row["state"], row["detail"]) == ("off", "Not configured; agents reach models with their own credentials.")

    gateway = FakeModelsGateway(up=False)
    client, headers, session_factory = _models_room(tmp_path / "on", gateway=gateway)
    row = next(a for a in client.get("/api/admin/addons", headers=headers).json()["addons"] if a["id"] == "models")
    assert row["state"] == "error"
    assert row["detail"].startswith("Not reachable")

    gateway.up = True
    with session_factory() as session:
        _vertex_provider(session)
    row = next(a for a in client.get("/api/admin/addons", headers=headers).json()["addons"] if a["id"] == "models")
    assert row["state"] == "on"
    assert row["detail"] == "1 provider connected, 2 models available, default Claude Affordable Sonnet."


class _CapturingBundleGitHub(FakeGitHub):
    """Captures the bundle's agent.yaml and gojira.yaml at the push."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.agent_yaml: str | None = None
        self.gojira: dict | None = None

    def commit_push(self, workdir: Path, message: str) -> bool:
        self.agent_yaml = (workdir / "bundle" / "agent.yaml").read_text()
        self.gojira = yaml.safe_load((workdir / "gojira.yaml").read_text())
        return super().commit_push(workdir, message)


def test_set_model_through_the_gateway_rewrites_the_model_block_and_delivers_a_key(tmp_path):
    """Wire shape (kobo#390): Change model with the gateway on points the
    agent's bundle at the gateway (`openai/<gateway model>`, the gateway's
    `/v1`, the key's env name), wires the `<agent>-models` Secret into the
    deploy contract, drops an earlier `AGENT_MODEL`, mints the key for every
    model the providers serve and delivers it under `MODELS_GATEWAY_KEY`;
    the key value itself is in the Secret and nowhere else."""
    github = _CapturingBundleGitHub(extra_clone_files={
        "bundle/agent.yaml": "name: clio\nmodel: {id: '${AGENT_MODEL:-vertex_ai/claude-sonnet-4-5}', max_tokens: 8192}\ntransports: []\n",
        "gojira.yaml": "app: clio\nenv: {AGENT_MODEL: vertex_ai/claude-sonnet-4-5, VERTEXAI_PROJECT: p}\n",
    })
    gateway, gojira = FakeModelsGateway(), FakeGojira()
    client, headers, session_factory = _models_room(tmp_path, gateway=gateway, github=github, gojira=gojira)
    app_id = gojira.create_app(name="clio", repo_full_name="acme/agent-clio", cluster="dev", namespace="clio")
    with session_factory() as session:
        _vertex_provider(session)
        session.add(Agent(name="clio", persona="helpful-generalist", model_id="vertex_ai/claude-sonnet-4-5", status=AgentStatus.READY,
                          repo_full_name="acme/agent-clio", gojira_app_id=app_id, created_by="ops@example.com"))
        session.commit()

    response = client.patch("/api/agents/clio/model", json={"model": "vertex_ai/gemini-2.5-flash"}, headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["wiring"] == "committed"
    model = yaml.safe_load(github.agent_yaml)["model"]
    assert model == {"id": "openai/gemini-fast-flash", "max_tokens": 8192,
                     "api_base": f"{MODELS_GATEWAY_URL}/v1", "api_key_env": "MODELS_GATEWAY_KEY"}
    assert github.gojira["secrets_from"] == ["clio-models"]
    assert "AGENT_MODEL" not in github.gojira["env"]
    assert f"reprovision:{app_id}" in gojira.calls
    [minted] = gateway.keys["agent:clio"]
    assert minted["models"] == ["claude-affordable-sonnet", "gemini-fast-flash"]
    kubectl = client.app.state.clients.kubectl
    assert kubectl.secrets[("clio", "clio-models")] == {"MODELS_GATEWAY_KEY": minted["key"]}
    assert minted["key"] not in response.text and minted["key"] not in github.agent_yaml
