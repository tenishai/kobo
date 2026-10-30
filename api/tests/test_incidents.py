"""Production bugs, each named in its docstring. Kept verbatim.

Every test here reproduces something that bit a deployed room, with the
issue number or the date in its docstring. They came out of the files the
2026-10-29 purge deleted; the incident tests of test_destroy.py stay there.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from kobo_api import bootstrap, db
from kobo_api.agent_env import RESERVED_KEYS, read_env, read_room_env, write_refusal
from kobo_api.bootstrap import run_bootstrap
from kobo_api.catalog import validate_name
from kobo_api.clients import (
    DENY_ALL_PAIR,
    CabalClient,
    CabalError,
    CachedGcloudToken,
    ClientError,
    Clients,
    KubectlCLI,
)
from kobo_api.colleagues import deliver_pass
from kobo_api.composer import (
    BundleSpec,
    compose,
    refresh_agent_card,
    update_dockerfile_runtime_image,
)
from kobo_api.config import Config, MCPConfig
from kobo_api.health import DOWN, probe_health
from kobo_api.iap import ASSERTION_HEADER, IAP_ISSUER
from kobo_api.kinds import KIND_COMPOSED, KIND_CONNECTED, RUN_MODE_HTTP
from kobo_api.learnings import SOURCE_TAUGHT
from kobo_api.main import create_app
from kobo_api.marketplace import create_skill
from kobo_api.mcp import audit, rate_limit
from kobo_api.mcp.jwt import ACCESS_TTL_S, mint, signing_key
from kobo_api.models import LEARNING_APPROVED, Agent, AgentLearning, AgentStatus, Base
from kobo_api.org import load_org_config
from kobo_api.pairs import sync_dispatch_pairs
from kobo_api.personas import seed_default_personas
from kobo_api.provisioner import Ctx
from kobo_api.sero import _TOKEN_MAX_AGE_S, MetadataIdentityTokenProvider
from kobo_api.skills import save_local_skill
from kobo_api.steps import (
    HELLO_RETRY_ATTEMPTS,
    HELLO_RETRY_BACKOFF_S,
    BuildStep,
    ConnectedWireStep,
    HelloStep,
    OnboardStep,
    StagedSkill,
)
from tests.fakes import (
    FAKE_CONNECTED_DOCKERFILE,
    FakeAgent,
    FakeCabal,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeImage,
    FakeKubectl,
    FakeName,
    FakeSero,
    FakeSlack,
    FakeStorage,
)

# --- from test_agent_env.py ---


def test_a_name_the_other_list_already_holds_is_refused_naming_that_list():
    """Production bug: A name the other list already holds is refused naming that list.

    Data integrity (issue #272): a secret and a variable with one name
    both land in the pod as one variable, and which value wins would be
    whichever was written last. Removing is never a clash."""
    assert (
        write_refusal({"TEST": "v"}, [], [], taken=["TEST"], taken_as="secret")
        == "TEST is already a secret on this agent"
    )
    assert write_refusal({}, ["TEST"], [], taken=["TEST"], taken_as="variable") is None


def test_read_env_leaves_out_every_key_the_room_sets_and_read_room_env_has_them():
    """Production bug: Read env leaves out every key the room sets and read room env has them.

    Issue #250: dropping the two model keys alone handed SLACK_WAKE_MODE
    back as an owner row, the panel re-sent it, and no variable could be
    saved on an agent with Slack wired."""
    data = {
        "env": {
            "VERTEXAI_PROJECT": "a-project",
            "SLACK_WAKE_MODE": "thread",
            "AGENT_MODEL": "a-model",
            "OWN_KEY": "own value",
        }
    }
    assert read_env(data) == {"OWN_KEY": "own value"}
    assert read_room_env(data) == {
        "VERTEXAI_PROJECT": "a-project",
        "SLACK_WAKE_MODE": "thread",
        "AGENT_MODEL": "a-model",
    }


def test_the_panels_reserved_list_is_the_servers():
    """Production bug: The panels reserved list is the servers.

    Issue #250 was the two lists drifting: the panel's inline rule let a
    name through that the route refused. One list, checked here."""
    source = (Path(__file__).parents[2] / "ui" / "src" / "lib" / "dotenv.ts").read_text()
    block = re.search(r"export const RESERVED_KEYS = \[(.*?)\];", source, re.DOTALL)
    assert block is not None
    assert set(re.findall(r'"([A-Z0-9_]+)"', block.group(1))) == RESERVED_KEYS


# --- from test_api_agents.py ---


AGENT_NAME = "clio"


PERSONA = "helpful-generalist"


TEAM = "clio-team"


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


def test_create_refuses_when_the_cluster_cannot_be_asked(session_factory__api_agents, config__api_agents, clients):
    """Production bug: Create refuses when the cluster cannot be asked.

    Guards ownership of the agent's namespace (issue #193): when the room
    cannot tell whether the namespace exists, it does not make the agent."""
    clients.kubectl.namespace_error = "Unable to connect to the server"
    client__api_agents = TestClient(create_app(session_factory__api_agents, config=config__api_agents, clients=clients))

    response = client__api_agents.post("/api/agents", json={"name": AGENT_NAME, "persona": PERSONA, "team": TEAM})

    assert response.status_code == 503
    session = session_factory__api_agents()
    assert session.query(Agent).filter_by(name=AGENT_NAME).first() is None
    session.close()


def test_create_refuses_a_name_whose_namespace_already_exists(
    session_factory__api_agents, config__api_agents, clients
):
    """Production bug: Create refuses a name whose namespace already exists.

    Guards ownership of the agent's namespace (issue #193): a namespace no
    row records belongs to something else, and the platform would adopt it."""
    clients.kubectl.namespaces[AGENT_NAME] = datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None)
    client__api_agents = TestClient(create_app(session_factory__api_agents, config=config__api_agents, clients=clients))

    response = client__api_agents.post(
        "/api/agents", json={"name": AGENT_NAME, "persona": PERSONA, "team": TEAM}
    )

    assert response.status_code == 409
    session = session_factory__api_agents()
    assert session.query(Agent).filter_by(name=AGENT_NAME).first() is None
    session.close()


def test_chat_relay_failure_logs_exception_type_and_elapsed_seconds(
    session_factory__api_agents, config__api_agents, fake_github, fake_gojira, fake_agent, monkeypatch, caplog
):
    """Production bug: Chat relay failure logs exception type and elapsed seconds.

    Task 8 (502 instrumentation): the root cause of the intermittent
    502 isn't established yet, so the relay-failure log line must carry
    enough to characterize the next live one -- the exception TYPE and the
    elapsed wall time of the relay call -- without logging message content,
    which stays out of this path on purpose."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_cabal_config(config__api_agents)
    fake_cabal = FakeCabal(relay_error="switchboard unreachable")
    client__api_agents = _client_with_cabal(
        session_factory__api_agents, cfg, fake_github, fake_gojira, fake_agent, fake_cabal
    )
    _seed_agent(session_factory__api_agents, cfg)

    # Deliberately NOT patching time.monotonic: `main_module.time` IS the
    # stdlib module, so patching its attribute swaps the clock for every
    # consumer in the process (httpx, threading, pytest itself) and hangs
    # the run. Assert the SHAPE of the duration instead, which is what the
    # instrumentation actually promises.
    with caplog.at_level("WARNING"):
        response = client__api_agents.post(
            f"/api/agents/{AGENT_NAME}/chat",
            json={"messages": [{"role": "user", "text": "do not log this text"}]},
        )

    assert response.status_code == 502
    assert "CabalError" in caplog.text
    assert re.search(r"after \d+\.\d{3}s", caplog.text), caplog.text
    assert "do not log this text" not in caplog.text


def test_create_registers_its_pipeline_thread_for_the_drain(client__api_agents):
    """Production bug: Create registers its pipeline thread for the drain.

    Issue #202: an inline launch is registered like a queue launch, so
    `drain_pipelines` waits for it at shutdown instead of killing the build."""
    before = len(client__api_agents.app.state.pipeline_threads)
    client__api_agents.post("/api/agents", json={"name": AGENT_NAME, "persona": PERSONA, "team": TEAM})
    threads = client__api_agents.app.state.pipeline_threads
    assert len(threads) == before + 1
    _poll_until(client__api_agents, AGENT_NAME, {"ready", "failed"})
    threads[-1].join(timeout=5)
    assert not threads[-1].is_alive()


# --- from test_api_colleague_mode.py ---


AGENT_NAME = "clio"


GROUP = "acme-acl"


DOC = "bundle/knowledge/colleagues.md"


def _composed_tree(tmp_path: Path) -> dict[str, str]:
    """The agent's repository as the room composed it, default mode."""
    spec = BundleSpec(
        name=AGENT_NAME,
        persona_body="You are clio.\n",
        model_id="vertex_ai/claude-sonnet-4-5",
        skills=[],
        runtime_image="registry.example/agents/daedalus-runtime:v1",
        vertex_project="p",
        vertex_location="us-east5",
        cabal_url="https://cabal.example",
        broker_url="http://kobo.svc",
    )
    dest = tmp_path / "composed"
    compose(spec, dest)
    return {
        p.relative_to(dest).as_posix(): p.read_text() for p in dest.rglob("*") if p.is_file()
    }


class _CapturingGitHub(FakeGitHub):
    """Snapshots the workdir at `commit_push`, the only moment the cloned
    tree still exists."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.pushed: dict[str, str] = {}
        self.messages: list[str] = []

    def commit_push(self, workdir: Path, message: str) -> bool:
        self.pushed = {
            p.relative_to(workdir).as_posix(): p.read_text()
            for p in workdir.rglob("*")
            if p.is_file()
        }
        self.messages.append(message)
        return super().commit_push(workdir, message)


def _client(session_factory, config, github):
    return TestClient(
        create_app(
            session_factory,
            config=config,
            clients=Clients(
                github=github,
                gojira=FakeGojira(),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=FakeKubectl(),
            ),
        )
    )


def _seed(session_factory, *, kind=None):
    session = session_factory()
    for name in (AGENT_NAME, "ada"):
        agent = Agent(
            name=name,
            persona="helpful-generalist",
            model_id="vertex_ai/claude-sonnet-4-5",
            status=AgentStatus.READY,
            repo_full_name=f"acme/agent-{name}",
            group_name=GROUP,
            skills=[],
        )
        if kind is not None and name == AGENT_NAME:
            agent.kind = kind
        session.add(agent)
    session.commit()
    session.close()


def _mode_on_row(session_factory):
    session = session_factory()
    try:
        return session.query(Agent).filter_by(name=AGENT_NAME).one().colleague_mode
    finally:
        session.close()


def _put(client, mode):
    return client.put(f"/api/agents/{AGENT_NAME}/colleague-mode", json={"mode": mode})


def test_the_row_is_written_only_after_the_push(session_factory, config, tmp_path):
    """Production bug: The row is written only after the push.

    Issue #133: no UPDATE on the agents row may reach the database before
    the push, or Postgres holds the row lock across the clone and push."""
    agent_updates: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("UPDATE AGENTS"):
            agent_updates.append(statement)

    engine = session_factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", _record)

    class _CountingGitHub(_CapturingGitHub):
        updates_at_push: int | None = None

        def commit_push(self, workdir: Path, message: str) -> bool:
            self.updates_at_push = len(agent_updates)
            return super().commit_push(workdir, message)

    github = _CountingGitHub(clone_tree=_composed_tree(tmp_path))
    client = _client(session_factory, config, github)
    _seed(session_factory)
    try:
        assert _put(client, "roster-only").status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert github.updates_at_push == 0
    assert agent_updates, "the mode never reached the row"
    assert _mode_on_row(session_factory) == "roster-only"
    # Rendered for the new mode even though the row still said default.
    assert "ask_colleague FIRST" not in github.pushed[DOC]


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


def _make_client(session_factory__api_colleagues, config__api_colleagues, *, cabal=None):
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


def test_feed_ask_line_shows_the_question_not_the_answer_when_history_holds_both(
    session_factory__api_colleagues, config__api_colleagues, monkeypatch
):
    """Production bug: Feed ask line shows the question not the answer when history holds both.

    Regression: the 'ward told gemini-one what a PSC is' display bug. A
    completed daedalus conversation's `history` ends with the AGENT reply, so
    deriving the ask from `history[-1]` put the answer on the outgoing (ask)
    line. Here the ask is a short question and the reply is a long, distinct
    answer -- the ask entry MUST carry the question, never the answer text."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config__api_colleagues)
    _seed_agent__api_colleagues(session_factory__api_colleagues, cfg)
    question = "what a psc is"
    answer = "PSC stands for Private Service Connect, a Google Cloud networking feature..."
    summaries = [_feed_summary("t-psc", target="gemini-one", caller="agent:ward")]
    detail = _feed_detail(
        "t-psc",
        target="gemini-one",
        caller="agent:ward",
        created_at="2026-08-23T04:30:40Z",
        updated_at="2026-08-23T04:30:46Z",
        ask_text=question,
        reply_text=answer,
    )
    fake_cabal = FakeCabal(conversations_result=summaries, conversation_results={"t-psc": detail})
    client = _make_client(session_factory__api_colleagues, cfg, cabal=fake_cabal)

    entries = client.get("/api/colleagues/feed", params={"agent": "ward"}).json()["entries"]

    ask = next(e for e in entries if e["role"] == "ask")
    reply = next(e for e in entries if e["role"] == "reply")
    assert ask["from"] == "ward" and ask["to"] == "gemini-one"
    assert ask["text"] == question  # the QUESTION, not the answer
    assert answer not in ask["text"]  # the answer never bleeds onto the ask line
    assert reply["from"] == "gemini-one" and reply["to"] == "ward"
    assert reply["text"] == answer


# --- from test_api_connect.py ---


AGENT_NAME = "clio"


ORG = "example-org"


REPO = f"{ORG}/team-chatbot"


@pytest.fixture(autouse=True)
def _no_real_sleep__api_connect(monkeypatch):
    import kobo_api.steps as steps_module

    monkeypatch.setattr(steps_module, "_sleep", lambda seconds: None)


@pytest.fixture(autouse=True)
def _fresh_scan_window():
    """The scan limiter's window is module state shared by every caller in
    the process, so each case starts with an empty one rather than one
    carrying the previous case's scans."""
    rate_limit.reset()
    yield
    rate_limit.reset()


@pytest.fixture
def session_factory__api_connect(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__api_connect(tmp_path):
    return Config(
        workdir=tmp_path / "work",
        skills_bucket="skills-bucket",
        personas_bucket="personas-bucket",
        github_org=ORG,
    )


@pytest.fixture
def github():
    return FakeGitHub(existing_repos={REPO})


@pytest.fixture
def clients__api_connect(github, fake_storage):
    return Clients(
        github=github,
        gojira=FakeGojira(activate_after=1),
        cloud=FakeCloud(),
        agent=FakeAgent(reply="hello yourself, human"),
        kubectl=FakeKubectl(),
        storage=fake_storage,
        image=FakeImage(),
    )


@pytest.fixture
def client__api_connect(session_factory__api_connect, config__api_connect, clients__api_connect):
    return TestClient(create_app(session_factory__api_connect, config=config__api_connect, clients=clients__api_connect))


@pytest.fixture
def client_for(session_factory__api_connect, config__api_connect, fake_storage):
    """A client over one particular repository, for the cases that are about
    what the scan finds in it rather than about the route."""

    def _build(github: FakeGitHub) -> TestClient:
        clients__api_connect = Clients(
            github=github,
            gojira=FakeGojira(activate_after=1),
            cloud=FakeCloud(),
            agent=FakeAgent(reply="hello yourself, human"),
            kubectl=FakeKubectl(),
            storage=fake_storage,
            image=FakeImage(),
        )
        return TestClient(create_app(session_factory__api_connect, config=config__api_connect, clients=clients__api_connect))

    return _build


def _payload(**overrides) -> dict:
    payload = {
        "repo": REPO,
        "name": AGENT_NAME,
        "run_mode": RUN_MODE_HTTP,
        # The room cannot check who owns a repository, so every connect
        # carries the caller's word that they may. Tests for its absence
        # are in their own section below.
        "confirm_repo_owner": True,
    }
    payload.update(overrides)
    return payload


def _row(session_factory__api_connect, name=AGENT_NAME) -> Agent | None:
    session = session_factory__api_connect()
    try:
        return session.query(Agent).filter_by(name=name).first()
    finally:
        session.close()


BASE_DEPLOYMENT = (
    "kind: Deployment\nmetadata:\n  name: pr-reviewer\nspec:\n  template:\n"
    "    spec:\n      serviceAccountName: pr-reviewer\n      containers: []\n"
)


def _deploying_repo() -> FakeGitHub:
    return FakeGitHub(
        existing_repos={REPO},
        root_entries=["Dockerfile", "k8s"],
        repo_files={
            "Dockerfile": FAKE_CONNECTED_DOCKERFILE,
            "k8s/base/deployment.yaml": BASE_DEPLOYMENT,
        },
    )


def test_connect_refuses_to_rename_a_repository_that_already_deploys(
    client_for, session_factory__api_connect
):
    """Production bug: Connect refuses to rename a repository that already deploys.

    The route runs the scan again for itself, so the form's word is not
    what stands between a connect and an orphaned production overlay."""
    client__api_connect = client_for(_deploying_repo())

    response = client__api_connect.post("/api/agents/connect", json=_payload(name="prudence"))

    assert response.status_code == 422
    assert [b["code"] for b in response.json()["blockers"]] == ["repo_name_mismatch"]
    assert _row(session_factory__api_connect, name="prudence") is None


def test_connect_refuses_when_the_cluster_cannot_be_asked(session_factory__api_connect, config__api_connect, clients__api_connect):
    """Production bug: Connect refuses when the cluster cannot be asked.

    Guards ownership of the agent's namespace (issue #193): when the room
    cannot tell whether the namespace exists, it does not make the agent."""
    clients__api_connect.kubectl.namespace_error = "Unable to connect to the server"
    client__api_connect = TestClient(create_app(session_factory__api_connect, config=config__api_connect, clients=clients__api_connect))

    response = client__api_connect.post("/api/agents/connect", json=_payload())

    assert response.status_code == 503
    session = session_factory__api_connect()
    assert session.query(Agent).filter_by(name=AGENT_NAME).first() is None
    session.close()


def test_connect_refuses_a_name_whose_namespace_already_exists(
    session_factory__api_connect, config__api_connect, clients__api_connect
):
    """Production bug: Connect refuses a name whose namespace already exists.

    Guards ownership of the agent's namespace (issue #193): a connected
    agent's name is its namespace too, and its secret_names mount from it."""
    clients__api_connect.kubectl.namespaces[AGENT_NAME] = datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None)
    client__api_connect = TestClient(create_app(session_factory__api_connect, config=config__api_connect, clients=clients__api_connect))

    response = client__api_connect.post("/api/agents/connect", json=_payload())

    assert response.status_code == 409
    assert _row(session_factory__api_connect) is None


# --- from test_api_runtime.py ---


AGENT_NAME = "clio"


PERSONA = "helpful-generalist"


REPO__api_runtime = "acme/agent-clio"


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


def _make_client__api_runtime(session_factory__api_runtime, config__api_runtime, *, github=None, cabal=None):
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
            repo_full_name=REPO__api_runtime,
            team="shared",
            skills=[],
            runtime_image=runtime_image,
        )
    )
    session.commit()
    session.close()


def test_patch_runtime_names_the_new_runtime_on_the_card_and_reregisters(
    session_factory__api_runtime, config__api_runtime, monkeypatch
):
    """Production bug: Patch runtime names the new runtime on the card and reregisters.

    Issue #147: the card names the runtime, so the commit that moves FROM
    moves the card with it, and the switchboard is sent the new card."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = config__api_runtime.model_copy(
        update={
            "cabal_url": "http://cabal.internal",
            "agent_endpoint_template": "http://agent-{name}.default.svc.cluster.local",
        }
    )

    class _CardGitHub(FakeGitHub):
        def commit_push(self, workdir: Path, message: str) -> bool:
            self.card = json.loads((workdir / "bundle" / "agent-card.json").read_text())
            return super().commit_push(workdir, message)

    github = _CardGitHub()
    cabal = FakeCabal()
    client = _make_client__api_runtime(session_factory__api_runtime, cfg, github=github, cabal=cabal)
    _seed_agent__api_runtime(session_factory__api_runtime, cfg, runtime_image=cfg.runtime_image)

    response = client.patch(
        f"/api/agents/{AGENT_NAME}/runtime", json={"runtime_image": OTHER_RUNTIME}
    )

    assert response.status_code == 200
    assert response.json()["directory"] == "synced"
    assert github.card["version"] == "v9"
    assert github.card["metadata"]["runtime_image"] == OTHER_RUNTIME
    assert [call["name"] for call in cabal.graduate_calls] == [AGENT_NAME]


# --- from test_api_search.py ---


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
    {
        "id": "terraform-check",
        "name": "Terraform check",
        "description": "Validates terraform plans against policy",
        "team": "sre",
        "tags": ["terraform", "infrastructure"],
        "sha": "def456",
        "builtin": False,
    },
    {
        "id": "word-games",
        "name": "Word games",
        "description": "Plays word games with the user",
        "team": "shared",
        "tags": ["fun"],
        "sha": "ghi789",
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


def _make_client__api_search(config__api_search: Config, clients__api_search: Clients, tmp_path) -> TestClient:
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    return TestClient(create_app(session_factory, config=config__api_search, clients=clients__api_search))


@pytest.fixture
def fake_storage__api_search():
    return FakeStorage(
        buckets={
            "skills-bucket": {"index.json": (_index_bytes(SKILL_ENTRIES), {})},
            "personas-bucket": {"index.json": (_index_bytes(PERSONA_ENTRIES), {})},
        }
    )


@pytest.fixture
def config__api_search(tmp_path):
    return Config(
        workdir=tmp_path / "work",
        skills_bucket="skills-bucket",
        personas_bucket="personas-bucket",
    )


@pytest.fixture
def clients__api_search(fake_storage__api_search):
    return _minimal_clients(storage=fake_storage__api_search)


@pytest.fixture
def client__api_search(config__api_search, clients__api_search, tmp_path):
    return _make_client__api_search(config__api_search, clients__api_search, tmp_path)


def test_recommend_skills_keeps_three_letter_domain_words(client__api_search):
    """Production bug: Recommend skills keeps three letter domain words.

    The live failure: 'A GCP expert' must surface gcp-tagged skills --
    gcp is three letters, and persona-generic words like 'expert' must
    never be what the match hangs on."""
    response = client__api_search.get(
        "/api/skills/recommended",
        params={"who": "A GCP expert", "what": "gcp terraform infrastructure"},
    )
    assert response.status_code == 200
    ids = [r["id"] for r in response.json()["results"]]
    assert "terraform-check" in ids


# --- from test_bootstrap.py ---


GITHUB_ORG = "acme-corp"


SKILLS_BUCKET = "acme-corp-skills"


PERSONAS_BUCKET__bootstrap = "acme-corp-personas"


# `_skills_repo_name`'s own computed default (and org.yaml's stored value)
# is bare — no owner prefix — but every GitHub call bootstrap itself makes
# must be owner-qualified (Finding 1: `gh` resolves a bare repo argument
# under the invoking user's own account, never the org's). SKILLS_REPO_BARE
# is what org.yaml stores/what a bare `_skills_repo_name` returns; SKILLS_REPO
# is what actually reaches every FakeGitHub call in this file.
SKILLS_REPO_BARE = "acme-corp-skills"


SKILLS_REPO = f"{GITHUB_ORG}/{SKILLS_REPO_BARE}"


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
        skills_bucket=SKILLS_BUCKET,
        personas_bucket=PERSONAS_BUCKET__bootstrap,
    )


@pytest.fixture
def github__bootstrap():
    return FakeGitHub(track_pushed_content=True)


@pytest.fixture
def storage():
    return FakeStorage()


def _clients(github__bootstrap, storage) -> Clients:
    return Clients(
        github=github__bootstrap,
        gojira=None,
        cloud=None,
        agent=None,
        kubectl=None,
        storage=storage,
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


def test_second_run_with_pr_still_open_reports_skipped_and_does_not_reopen_pr(
    config__bootstrap, storage
):
    """Production bug: Second run with pr still open reports skipped and does not reopen pr.

    The exact live-incident scenario (Phase A idempotence re-run): run 1
    opens the skills-publish-workflow PR from the fixed `PUBLISH_PR_BRANCH`.
    Run 2 fires before that PR has merged — the skills repo's default branch
    still has no workflow file (this fake's generic `clone()` template never
    gains a `.github/workflows/` tree on its own; `track_pushed_content` is
    deliberately left off here so the default-branch clone stays workflow-
    absent across both runs, unlike `open_pr`'s snapshot-based tests above).

    Before the fix, run 2 fell through to `open_pr`, which re-pushed the
    same branch name and failed non-fast-forward against the remote branch
    run 1 already pushed. The fix: `find_pr` sees run 1's still-open PR and
    run 2 reports `skipped` with its URL instead of retrying `open_pr`.
    """
    github__bootstrap = FakeGitHub(
        existing_repos={f"{GITHUB_ORG}/kobo-config", SKILLS_REPO},
        pr_url="https://github.com/acme-corp/acme-corp-skills/pull/9",
    )
    clients = _clients(github__bootstrap, storage)

    first_results = run_bootstrap(config__bootstrap, clients, out=lambda _line: None)
    first_publish = next(
        r for r in first_results if r.name == bootstrap.STEP_SKILLS_PUBLISH
    )
    assert first_publish.status == "done"
    assert (
        first_publish.detail == "https://github.com/acme-corp/acme-corp-skills/pull/9"
    )
    assert len(github__bootstrap.open_pr_calls) == 1

    second_results = run_bootstrap(config__bootstrap, clients, out=lambda _line: None)
    second_publish = next(
        r for r in second_results if r.name == bootstrap.STEP_SKILLS_PUBLISH
    )

    assert second_publish.status == "skipped"
    assert (
        second_publish.detail
        == "PR already open: https://github.com/acme-corp/acme-corp-skills/pull/9"
    )
    # open_pr was not retried on run 2 — the non-fast-forward push the
    # incident hit never had a chance to happen.
    assert len(github__bootstrap.open_pr_calls) == 1
    assert f"find_pr:{SKILLS_REPO}:{bootstrap.PUBLISH_PR_BRANCH}" in github__bootstrap.calls


# --- from test_build_log.py ---


class LogGitHub:
    def __init__(self, run=None, log="line1\nline2", raise_on_log=False):
        self._run = run
        self._log = log
        self._raise = raise_on_log

    def latest_run(self, repo):
        return self._run

    def run_log(self, repo, run_id, *, failed_only=True):
        if self._raise:
            from kobo_api.clients import ClientError

            raise ClientError("boom")
        self.failed_only = failed_only
        return self._log


def _client__build_log(session_factory, config, github):
    clients = Clients(
        github=github,
        gojira=FakeGojira(),
        cloud=FakeCloud(),
        agent=FakeAgent(),
        kubectl=FakeKubectl(),
        storage=FakeStorage(),
    )
    return TestClient(create_app(session_factory, config=config, clients=clients))


def _seed__build_log(session_factory, repo="acme/agent-ward"):
    session = session_factory()
    session.add(
        Agent(
            name="ward",
            persona="p",
            model_id="m",
            status=AgentStatus.READY,
            repo_full_name=repo,
        )
    )
    session.commit()
    session.close()


def test_secret_shaped_strings_are_scrubbed_from_the_log(session_factory, config):
    """Production bug: Secret shaped strings are scrubbed from the log.

    Issue #200: Actions masks only registered secrets; the room masks the rest."""
    github = LogGitHub(
        run={"id": 7, "status": "completed", "conclusion": "failure", "head_sha": "abc"},
        log="auth ghp_AbCdEfGhIjKlMnOpQrStUvWxYz012345 ok\nGITHUB_TOKEN=abcdefgh12345678\nplain line",
    )
    _seed__build_log(session_factory)
    body = _client__build_log(session_factory, config, github).get("/api/agents/ward/build-log").json()
    assert body["log"] == "auth [redacted] ok\nGITHUB_TOKEN=[redacted]\nplain line"


# --- from test_clients.py ---


class RecordingRunner:
    """Test double for CommandRunner: records exact argv (and, per Task 3,
    the exact `env` overlay) for every call, and replays canned (returncode,
    stdout, stderr) results in order (or reuses a single configured result
    for every call). Raises ClientError on rc != 0, exactly like the real
    CommandRunner, so real client branching logic (e.g. ensure_gsa's
    describe-then-create) exercises for real against it.
    """

    def __init__(self, results: list[tuple[int, str, str]] | None = None):
        self._results = list(results) if results is not None else [(0, "", "")]
        self.calls: list[list[str]] = []
        # Parallel to `calls`: the `env` kwarg each call was made with (None
        # when the caller passed none) — GhCLI's token_provider wiring is
        # the only thing exercising this today.
        self.envs: list[dict[str, str] | None] = []

    def run(
        self,
        args: list[str],
        stdin: str | None = None,
        *,
        redact_args: bool = False,
        env: dict[str, str] | None = None,
    ) -> str:
        self.calls.append(list(args))
        self.envs.append(env)
        if len(self._results) > 1:
            rc, out, err = self._results.pop(0)
        else:
            rc, out, err = self._results[0]
        if rc != 0:
            raise ClientError(
                f"{' '.join(args)} failed (rc={rc}): {err or out}",
                returncode=rc,
                stdout=out,
                stderr=err,
            )
        return out


def test_namespace_created_at_reads_absent_as_none_and_a_time_as_naive_utc():
    """Production bug: Namespace created at reads absent as none and a time as naive utc.

    Guards destruction safety (issue #193): destroy compares this with the
    agent row's naive-UTC created_at, so "absent" and the timezone matter."""
    runner = RecordingRunner(results=[(0, "", ""), (0, "2026-10-24T12:00:00Z", "")])
    client = KubectlCLI(runner=runner)

    assert client.namespace_created_at("clio") is None
    assert client.namespace_created_at("cabal") == datetime(2026, 10, 24, 12, tzinfo=UTC).replace(tzinfo=None)


def test_cached_token_never_outlives_a_short_lived_token():
    """Production bug: Cached token never outlives a short lived token.

    The 2026-08-24 production regression: `gcloud auth print-access-token`
    returns whatever gcloud already has cached, which may have only minutes
    left, while this cache pinned it for a fixed 1800s. The room then served
    an expired bearer token and every GCS call answered 401 Invalid
    Credentials until the fixed window closed. A fetch that reports its real
    remaining lifetime must shorten the cache window to match."""
    calls: list[str] = []

    def fetch() -> tuple[str, int]:
        calls.append("fetched")
        return (f"token-{len(calls)}", 600)  # only 10 minutes left

    clock = {"now": 1000.0}
    token_provider = CachedGcloudToken(fetch=fetch, ttl_s=1800, now=lambda: clock["now"])

    first = token_provider()
    clock["now"] += 400  # past (600 - margin), still far inside the old 1800s ttl
    second = token_provider()

    assert len(calls) == 2, "a token with 600s of life must not be cached for 1800s"
    assert second != first


def test_cabal_put_dispatch_pairs_never_sends_an_empty_set(caplog):
    """Production bug: Cabal put dispatch pairs never sends an empty set.

    Guards the switchboard's consent gate (issue #194): cabal reads an empty
    table as open, so an empty policy goes out as the deny-all sentinel, warns
    even on a fresh client, and reports 0."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json={"count": 1})

    client = CabalClient(
        "http://cabal.local", "op-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with caplog.at_level(logging.WARNING, logger="kobo_api.clients"):
        count = client.put_dispatch_pairs(set())

    assert captured["json"] == {"pairs": [list(DENY_ALL_PAIR)]}
    assert count == 0
    assert any(rec.levelname == "WARNING" for rec in caplog.records)


def test_deny_all_pair_can_never_name_a_real_agent():
    """Production bug: Deny all pair can never name a real agent.

    Guards the switchboard's consent gate (issue #194): the sentinel must
    match no caller kobo mints (`agent:<name>`) and no name an agent can take."""
    caller, target = DENY_ALL_PAIR
    assert not caller.startswith("agent:")
    assert validate_name(target) is not None


# --- from test_colleagues.py ---


AGENT_NAME = "clio"


PERSONA = "helpful-generalist"


MODEL_ID = "vertex_ai/claude-sonnet-4-5"


def _agent(**overrides) -> Agent:
    fields = {"name": AGENT_NAME, "persona": PERSONA, "model_id": MODEL_ID}
    fields.update(overrides)
    return Agent(**fields)


def test_deliver_pass_revokes_the_agents_earlier_passes_before_minting_the_new_one():
    """Production bug: Deliver pass revokes the agents earlier passes before minting the new one.

    A re-wire used to add one more live 365-day pass per run and take
    none back (the destroy-orphans audit, 2026-09-21). The switchboard now
    refuses every token a subject was issued before a cutoff, and this
    asks for that cutoff BEFORE minting, so the new pass is the only one
    that works and never falls to its own revocation."""
    cabal = FakeCabal()
    deliver_pass(FakeKubectl(), cabal, _agent())
    assert cabal.revoke_colleague_calls == [AGENT_NAME]
    assert cabal.calls == [f"revoke:{AGENT_NAME}", f"mint:{AGENT_NAME}"]


def test_deliver_pass_stops_before_minting_when_the_revocation_is_refused():
    """Production bug: Deliver pass stops before minting when the revocation is refused.

    A pass minted over a refused revocation is one more live credential
    than there should be; the step fails first and mints nothing."""
    cabal = FakeCabal(revoke_colleague_error="switchboard down")
    kubectl = FakeKubectl()
    with pytest.raises(CabalError):
        deliver_pass(kubectl, cabal, _agent())
    assert cabal.mint_colleague_calls == []
    assert kubectl.secrets_applied == []


def test_deliver_pass_records_when_the_pass_was_minted_after_the_mint():
    """Production bug: Deliver pass records when the pass was minted after the mint.

    Issue #199: the brokers refuse a pass issued before this instant, so it
    is set right after the mint and never before it."""
    from datetime import UTC, datetime

    cabal = FakeCabal()
    kubectl = FakeKubectl()
    agent = _agent()
    assert getattr(agent, "pass_minted_at", None) is None
    before = datetime.now(UTC).replace(tzinfo=None)

    deliver_pass(kubectl, cabal, agent)

    minted = agent.pass_minted_at.replace(tzinfo=None)
    assert minted >= before
    assert cabal.mint_colleague_calls == [AGENT_NAME]


# --- from test_composer.py ---


_RUNTIME_IMAGE = "registry.example/agents/daedalus-runtime:latest"


def _make_spec(
    skills: list[StagedSkill] | None = None,
    *,
    name: str = "sam",
    team: str = "shared",
    group: str | None = None,
    purpose_tags: list[str] | None = None,
    capability: str | None = None,
    colleagues: list[dict] | None = None,
    cabal_url: str | None = None,
    memory_replica_bucket: str = "",
    broker_url: str = "",
    liminal_url: str = "",
    archetype: str | None = None,
) -> BundleSpec:
    return BundleSpec(
        name=name,
        persona_body=f"You are {name}, a helpful assistant.\n",
        model_id="vertex_ai/claude-sonnet-4-5",
        skills=skills or [],
        runtime_image=_RUNTIME_IMAGE,
        vertex_project="example-model-project",
        vertex_location="us-east5",
        team=team,
        group=group,
        purpose_tags=purpose_tags if purpose_tags is not None else [],
        capability=capability,
        colleagues=colleagues if colleagues is not None else [],
        cabal_url=cabal_url,
        memory_replica_bucket=memory_replica_bucket,
        broker_url=broker_url,
        liminal_url=liminal_url,
        archetype=archetype,
    )


_DIGEST = "sha256:" + "a0b09334649d" + "0" * 52


def test_self_md_teaches_skills_are_procedures(tmp_path):
    """Production bug: Self md teaches skills are procedures.

    2026-08-28, from a live exchange: an agent listed its seeded skills
    from recall, then told its operator they were 'not loaded as callable
    functions'. Skills are procedures in memory; the self-card says so."""
    dest = tmp_path / "out"
    compose(_make_spec(name="kestrel"), dest)
    text = (dest / "bundle" / "knowledge" / "self.md").read_text()
    assert "Your skills are PROCEDURES seeded into your memory, never tools" in text
    assert "recall that procedure and follow" in text
    assert "say which step and ask for that capability" in text


def test_refresh_agent_card_repairs_an_old_placeholder_card(tmp_path):
    """Production bug: Refresh agent card repairs an old placeholder card.

    A card composed before issue #147: placeholder description and
    version, no runtime, stale skills. One refresh corrects all of it from
    the repo and keeps a field kobo doesn't know."""
    dest = tmp_path / "out"
    compose(_make_spec(), dest)
    bundle = dest / "bundle"
    (bundle / "agent-card.json").write_text(
        json.dumps(
            {
                "name": "sam",
                "description": "Assembled by kobo.",
                "version": "0.1.0",
                "capabilities": [],
                "skills": [{"id": "gone"}],
                "metadata": {"team": "shared", "purpose_tags": [], "capability": ""},
                "extra": 1,
            },
            indent=2,
        )
        + "\n"
    )

    refresh_agent_card(bundle)

    card = json.loads((bundle / "agent-card.json").read_text())
    assert card["description"] == "You are sam, a helpful assistant"
    assert card["version"] == "latest"
    assert card["skills"] == []
    assert card["capabilities"] == {}
    assert card["metadata"]["runtime_image"] == _RUNTIME_IMAGE
    assert card["extra"] == 1


@pytest.mark.parametrize(
    ("new_image", "version"),
    [
        ("registry.example/agents/daedalus-runtime:0.5.3", "0.5.3"),
        (f"registry.example/agents/daedalus-runtime@{_DIGEST}", "sha256:a0b09334649d"),
        (f"registry.example/agents/daedalus-runtime:0.5.3@{_DIGEST}", "0.5.3"),
    ],
    ids=["tag", "digest", "tag-and-digest"],
)
def test_repinned_card_version_for_each_pin_shape(tmp_path, new_image, version):
    """Production bug: Repinned card version for each pin shape.

    Issue #156: what each way of pinning a runtime puts in the card's
    `version`, end to end through the Dockerfile `FROM` line. A digest-only
    pin has no name to read, so it gives the digest's prefix; pinning
    `tag@digest` keeps the exact build and a readable version. The full ref
    is in `metadata.runtime_image` whichever shape is used."""
    dest = tmp_path / "out"
    compose(_make_spec(), dest)

    update_dockerfile_runtime_image(dest / "Dockerfile", new_image)

    card = json.loads((dest / "bundle" / "agent-card.json").read_text())
    assert card["version"] == version
    assert card["metadata"]["runtime_image"] == new_image
    assert f"FROM {new_image}" in (dest / "Dockerfile").read_text()


# --- from test_config.py ---


def test_room_logging_lets_info_lines_through_without_touching_the_root(caplog):
    """Production bug: Room logging lets info lines through without touching the root.

    The deployed entry configures no logging, so the auth-regime line
    (#198) and the other boot INFO lines were dropped by the root default."""
    import logging

    from kobo_api.main import configure_room_logging

    root = logging.getLogger()
    saved = list(root.handlers)
    try:
        root.handlers = []
        room = logging.getLogger("kobo_api")
        before = list(room.handlers)
        configure_room_logging()
        assert room.level == logging.INFO
        assert len(room.handlers) == len(before) + 1
        configure_room_logging()
        assert len(room.handlers) == len(before) + 1  # idempotent
        for h in room.handlers:
            if h not in before:
                room.removeHandler(h)
    finally:
        root.handlers = saved


# --- from test_connected_pipeline.py ---


AGENT_NAME = "clio"


REPO_NAME = "team-chatbot"


MODEL_ID = "vertex_ai/claude-sonnet-4-5"


WIRE_PR_URL = "https://github.com/example-org/team-chatbot/pull/4"


@pytest.fixture(autouse=True)
def _no_real_sleep__connected_pipeline(monkeypatch):
    import kobo_api.steps as steps_module

    monkeypatch.setattr(steps_module, "_sleep", lambda seconds: None)


def _repo(config) -> str:
    return f"{config.github_org}/{REPO_NAME}"


def _make_agent(
    session_factory,
    config,
    *,
    kind: str = KIND_CONNECTED,
    run_mode: str | None = RUN_MODE_HTTP,
    secret_names: list[str] | None = None,
    repo_full_name: str | None = None,
    gojira_app_id: int | None = None,
    purpose_tags: str | None = None,
):
    session = session_factory()
    agent = Agent(
        name=AGENT_NAME,
        persona="connected",
        model_id=MODEL_ID,
        skills=[],
        kind=kind,
        run_mode=run_mode,
        secret_names=secret_names or [],
        repo_full_name=repo_full_name if repo_full_name is not None else _repo(config),
        gojira_app_id=gojira_app_id,
        purpose_tags=purpose_tags,
    )
    session.add(agent)
    session.commit()
    return session, agent


def _make_ctx(session, agent, config, clients) -> Ctx:
    workdir = config.workdir / agent.name
    workdir.mkdir(parents=True, exist_ok=True)
    return Ctx(agent=agent, session=session, config=config, clients=clients, workdir=workdir)


def _clients__connected_pipeline(*, github=None, gojira=None, cloud=None, kubectl=None, image=None, agent=None):
    return Clients(
        github=github,
        gojira=gojira,
        cloud=cloud,
        agent=agent,
        kubectl=kubectl,
        image=image,
    )


def _seeded_gojira(config, repo, *, activate_after: int = 1) -> FakeGojira:
    """A FakeGojira that already holds app id 1 for `repo`, so a step which
    only waits (never onboards) has something to poll."""
    gojira = FakeGojira(activate_after=activate_after)
    gojira.create_app(
        name=AGENT_NAME,
        repo_full_name=repo,
        cluster=config.gojira_cluster,
        namespace=AGENT_NAME,
    )
    return gojira


@dataclass
class _Wiring:
    """Everything a ConnectedWireStep test needs, named rather than
    unpacked positionally -- each test reads two or three of these five."""

    session: object
    agent: Agent
    github: FakeGitHub
    gojira: FakeGojira
    ctx: Ctx


def _wire_ctx(session_factory, config, *, github=None, gojira=None, **agent_kwargs):
    repo = _repo(config)
    session, agent = _make_agent(session_factory, config, gojira_app_id=1, **agent_kwargs)
    github = github if github is not None else FakeGitHub(pr_url=WIRE_PR_URL)
    gojira = gojira if gojira is not None else _seeded_gojira(config, repo)
    return _Wiring(
        session=session,
        agent=agent,
        github=github,
        gojira=gojira,
        ctx=_make_ctx(session, agent, config, _clients__connected_pipeline(github=github, gojira=gojira)),
    )


def test_connected_wire_step_sees_nothing_to_change_in_the_generators_own_text(
    session_factory, config
):
    """Production bug: Connected wire step sees nothing to change in the generators own text.

    The wiring is on the default branch as the deploy platform renders it
    after a regeneration: header comments, two-space list indentation, quoted
    values. Same meaning, different bytes. The step compares meaning: a
    re-dump here proposed a pull request of pure reformatting, the platform
    regenerated against the merge, and the two took turns forever
    (tenishai/kobo#123 item 11, agent-gordon 2026-10-08 and 2026-10-13)."""
    from kobo_api.config import vertex_location_for
    from kobo_api.steps import SERVICEACCOUNT_YAML_TEMPLATE

    name = AGENT_NAME
    gsa_email = f"agent-{name}@{config.gcp_project}.iam.gserviceaccount.com"
    tree = {
        "gojira.yaml": (
            "# Gojira app spec — this file is the contract between your repo and Gojira.\n"
            "# Change the class or add overrides here; Gojira regenerates the manifests\n"
            "# when this file changes.\n"
            f"app: {name}\n"
            "class: agent\n"
            "environments:\n"
            "  - name: dev\n"
            "    cluster: example-cluster\n"
            f"    namespace: {name}\n"
            f"service_account: agent-{name}\n"
            "env:\n"
            f'  VERTEXAI_LOCATION: "{vertex_location_for(config, MODEL_ID)}"\n'
            f'  VERTEXAI_PROJECT: "{config.vertex_project}"\n'
            "# overrides:                  # needs a justification — see docs\n"
        ),
        "k8s/base/kustomization.yaml": (
            "# Generated by Gojira.\n"
            "apiVersion: kustomize.config.k8s.io/v1beta1\n"
            "kind: Kustomization\n"
            "resources:\n"
            "  - deployment.yaml\n"
            "  - podmonitoring.yaml\n"
            "  - rules.yaml\n"
            "  - service.yaml\n"
            "  # app-authored resources, preserved by Gojira:\n"
            "  - serviceaccount.yaml\n"
        ),
        "k8s/base/serviceaccount.yaml": SERVICEACCOUNT_YAML_TEMPLATE.format(
            name=name, gsa_email=gsa_email
        ),
    }
    github = FakeGitHub(pr_url=WIRE_PR_URL, clone_tree=tree)
    wiring = _wire_ctx(session_factory, config, github=github)

    note = ConnectedWireStep().run(wiring.ctx)

    assert github.open_pr_calls == []
    assert github.merge_pr_calls == []
    assert wiring.gojira.reprovision_calls == [1]
    assert "active" in note
    clone = wiring.ctx.workdir / "wire"
    for rel_path, text in tree.items():
        assert (clone / rel_path).read_text() == text, rel_path


# --- from test_db.py ---


@pytest.fixture
def recorded(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def fake_create_engine(url, **kwargs):
        calls.append((url, kwargs))
        return object()

    monkeypatch.setattr(db, "create_engine", fake_create_engine)
    return calls


def test_bare_postgresql_url_names_psycopg2_before_sqlalchemy_picks_a_driver(recorded):
    """Production bug: Bare postgresql url names psycopg2 before sqlalchemy picks a driver.

    SQLAlchemy 2.1 made a bare `postgresql://` mean psycopg v3. The image
    ships psycopg2-binary and nothing else, and the first engine the room
    builds at boot is alembic's, so a build that resolved 2.1 died with
    "No module named 'psycopg'" while the SQLite-backed tests stayed green
    (kobo:6488f3d, 2026-09-25). The driver is named here, once, for
    everyone."""
    db.make_engine("postgresql://kobo:kobo@localhost:5432/kobo")

    url, _ = recorded[0]
    assert url == "postgresql+psycopg2://kobo:kobo@localhost:5432/kobo"


# --- from test_health.py ---


NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


CRASHED = {
    "metadata": {"generation": 3},
    "spec": {"replicas": 1},
    "status": {"observedGeneration": 3, "updatedReplicas": 1, "unavailableReplicas": 1},
}


def test_no_available_pods_is_down_with_the_count_and_the_worst_container():
    """Production bug: No available pods is down with the count and the worst container.

    lumbot, 2026-09-30: four days of CrashLoopBackOff under a green Ready."""
    kubectl = FakeKubectl(deployment_status={("lumbot", "lumbot"): CRASHED})
    kubectl.pods = {
        "lumbot": [
            {
                "status": {
                    "containerStatuses": [
                        {
                            "name": "app",
                            "restartCount": 1393,
                            "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                        }
                    ]
                }
            }
        ]
    }

    verdict = probe_health(kubectl, "lumbot", now=NOW)

    assert verdict.state == DOWN
    assert verdict.reason == "0 of 1 running; restarted 1393 times, CrashLoopBackOff"


# --- from test_jira_broker.py ---


@pytest.fixture
def jira_config(tmp_path):
    pat = tmp_path / "jira-pat"
    pat.write_text("not-a-real-token\n")
    return Config(
        workdir=tmp_path / "work",
        cabal_url="http://cabal.test",
        jira_domain="tracker.test",
        jira_email="reader@acme.io",
        jira_pat_path=pat,
        jira_projects=["AREQ"],
        jira_read_agents=["ledger"],
    )


@pytest.fixture
def jira_client(session_factory, jira_config):
    return TestClient(create_app(session_factory, config=jira_config))


def test_jira_gate_hands_the_agents_mint_instant_to_the_verifier(jira_client, session_factory, monkeypatch):
    """Production bug: Jira gate hands the agents mint instant to the verifier.

    Issue #199: the jira lane applies the same not-before cut as the GitHub one."""
    from datetime import UTC, datetime

    import kobo_api.main as main_mod
    from kobo_api.models import Agent, AgentStatus

    minted = datetime(2026, 10, 24, 12, 0, tzinfo=UTC).replace(tzinfo=None)
    session = session_factory()
    session.add(Agent(name="ledger", persona="p", model_id="m", status=AgentStatus.READY, pass_minted_at=minted))
    session.commit()
    session.close()
    seen = {}

    def record(cabal_url, bearer, agent_name, *, not_before=None):
        seen["not_before"] = not_before
        return {}

    monkeypatch.setattr(main_mod, "verify_agent_token", record)
    monkeypatch.setattr(main_mod, "jira_fetch_issue", lambda *a, **k: {"key": "OPS-1"})
    jira_client.get("/broker/agents/ledger/jira/issues/OPS-1", headers={"Authorization": "Bearer x"})
    assert seen["not_before"] == minted


# --- from test_marketplace.py ---


GATED_GOOD_SKILL = "---\ntags: [networking]\n---\n# PTR primer\n\nA PTR record maps an IP back to a hostname.\n"


def _gate_config(**kwargs) -> Config:
    return Config(github_org="org", skills_repo="skills", **kwargs)


def test_create_skill_completes_version_and_profiles_2026_08_23_brick_regression():
    """Production bug: Create skill completes version and profiles 2026 08 23 brick regression.

    The exact 2026-08-23 shape: caller content with no version and no
    profiles used to publish as-is and brick the whole publish pipeline.
    Completion now makes it lawful from birth."""
    github = FakeGitHub()
    create_skill(github, _gate_config(), skill_id="ptr-primer",
                 content=GATED_GOOD_SKILL, team="sre", author="a@b.c")
    pushed = github.pushed_file("skills/ptr-primer/SKILL.md")
    assert "version: 1.0.0" in pushed
    assert "profiles:" in pushed and "agent-sdk" in pushed


# --- from test_mcp_mount.py ---


CLIENT_ID = "test-mcp-client"


WORKSPACE_DOMAIN = "example.com"


ISSUER_BASE_URL = "https://kobo.example.org"


EMAIL = "person@example.com"


@pytest.fixture(autouse=True)
def _reset_rate_limit_and_audit():
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


def test_real_initialize_request_over_the_rooms_real_hostname_is_not_421(
    session_factory__mcp_mount, tmp_path
):
    """Production bug: Real initialize request over the rooms real hostname is not 421.

    Rollout-blocker regression (Task 9 review, reproduced live): the
    design's ingress serves `/mcp` on the room's REAL hostname (same host
    as the UI) -- never on `localhost`/`127.0.0.1`, which is all
    `test_real_initialize_request_does_not_hit_the_uninitialized_session_
    manager` above ever drives through. `build_mcp_server` used to
    construct `FastMCP(...)` with no `host=`/`transport_security=` at
    all, so the SDK defaulted to `host="127.0.0.1"`, which auto-enables
    `TransportSecuritySettings(allowed_hosts=["127.0.0.1:*",
    "localhost:*", "[::1]:*"])` -- a request whose `Host` header is the
    room's real hostname then fails `TransportSecurityMiddleware.
    _validate_host` and the SDK answers 421 "Invalid Host header" before
    the request ever reaches the session manager or a tool.

    `base_url="https://kobo.example.org"` with NO explicit port -- the
    exact shape a real HTTPS-on-443 client uses, and the exact shape the
    reviewer's live probe used -- reproduces that 421 pre-fix. This test
    is the reproduction turned around to assert the FIXED behavior: 200,
    never 421.
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
                    "clientInfo": {"name": "test-real-host-smoke", "version": "0.0.1"},
                },
            },
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )

    assert response.status_code != 421
    assert response.status_code == 200
    assert '"protocolVersion"' in response.text


# --- from test_org.py ---


ORG_REPO = "acme-corp/kobo-config"


def test_override_of_explicit_local_value_is_disclosed(caplog):
    """Production bug: Override of explicit local value is disclosed.

    IE-6065: org.yaml silently defeating a value the deployment config
    explicitly set (the Secret's digest pin vs. org.yaml's floating tag) was
    invisible from either file alone. The merge now says so, once per
    differing key, at WARNING (the app configures no logging, so INFO from
    this logger never reaches pod logs)."""
    import logging

    local = Config(
        org_repo=ORG_REPO,
        runtime_image="reg.example/ai/runtime@sha256:aaa111",
        gojira_cluster="acme-cluster",
    )
    github = FakeGitHub(
        org_yaml=(
            "runtime_image: reg.example/ai/runtime:floating\n"
            "gojira_cluster: acme-cluster\n"
            "vertex_project: acme-models\n"
        )
    )

    with caplog.at_level(logging.WARNING, logger="kobo_api.org"):
        merged, org = load_org_config(local, github)

    assert org.wired is True
    assert merged.runtime_image == "reg.example/ai/runtime:floating"
    messages = [r.getMessage() for r in caplog.records]
    disclosures = [m for m in messages if "org.yaml overrides" in m]
    # The differing explicitly-set key is disclosed, with both values.
    assert len(disclosures) == 1
    assert "runtime_image" in disclosures[0]
    assert "sha256:aaa111" in disclosures[0]
    assert "floating" in disclosures[0]
    # Same-value override and default-only override stay silent.
    assert "gojira_cluster" not in disclosures[0]
    assert "vertex_project" not in disclosures[0]


# --- from test_pairs.py ---


@pytest.fixture
def session_factory__pairs(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__pairs(tmp_path):
    return Config(workdir=tmp_path / "work")


def test_concurrent_syncs_in_one_process_never_interleave(session_factory__pairs, config__pairs, monkeypatch):
    """Production bug: Concurrent syncs in one process never interleave.

    Guards concurrency of the consent push (issue #194): a second sync must
    not compute while the first is still pushing, or an older set can land
    after a newer one."""
    import threading
    import time

    import kobo_api.pairs as pairs_mod

    events: list[str] = []
    release = threading.Event()
    monkeypatch.setattr(
        pairs_mod, "allowed_pairs", lambda s, c: events.append("compute") or set()
    )

    class SlowCabal(FakeCabal):
        def put_dispatch_pairs(self, pairs):
            events.append("put")
            release.wait(5)
            events.append("done")
            return 0

    cabal = SlowCabal()

    def run():
        s = session_factory__pairs()
        try:
            sync_dispatch_pairs(cabal, s, config__pairs)
        finally:
            s.close()

    first = threading.Thread(target=run)
    first.start()
    while "put" not in events:
        time.sleep(0.01)
    second = threading.Thread(target=run)
    second.start()
    time.sleep(0.2)
    release.set()
    first.join(5)
    second.join(5)

    assert events == ["compute", "put", "done", "compute", "put", "done"]


# --- from test_pr_broker.py ---


@pytest.fixture
def broker_config(tmp_path):
    key = tmp_path / "pr-app.pem"
    key.write_text("not-a-real-key")
    return Config(
        workdir=tmp_path / "work",
        pr_app_id="4741722",
        pr_app_installation_id="157070479",
        pr_app_private_key_path=key,
        cabal_url="http://cabal.test",
    )


@pytest.fixture
def broker_client(session_factory, broker_config):
    return TestClient(create_app(session_factory, config=broker_config))


def _agent__pr_broker(session_factory, name="ward"):
    session = session_factory()
    agent = Agent(
        name=name, persona="helpful-generalist", model_id="m", status=AgentStatus.READY
    )
    session.add(agent)
    session.commit()
    session.close()
    return name


def test_token_route_404s_with_zero_approvals(
    broker_client, session_factory, monkeypatch
):
    """Production bug: Token route 404s with zero approvals.

    Still a 404, but after the token verifies the message says why: the
    caller is proven to be the agent, so "no such agent" would be false
    (issue #11)."""
    import kobo_api.main as main_mod

    name = _agent__pr_broker(session_factory)
    monkeypatch.setattr(main_mod, "verify_agent_token", lambda *a, **k: None)
    r = broker_client.post(
        f"/broker/agents/{name}/github-token", headers={"Authorization": "Bearer x"}
    )
    assert r.status_code == 404
    error = r.json()["error"]
    assert "no such agent" not in error
    assert "none has been requested" in error
    assert f"POST /api/agents/{name}/repo-access" in error


# --- from test_sero.py ---


AUDIENCE = "http://secrets.example"


def _jwt(exp: int, iat: int | None = None) -> str:
    """A JWT-shaped string with `exp` (and `iat` when given) in its payload --
    unsigned and never verified here or in kobo_api.sero (the platform
    verifies; this room only reads the two to know how long to cache)."""

    def segment(payload: dict) -> str:
        raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
        return raw.rstrip("=")

    claims = {"exp": exp} if iat is None else {"exp": exp, "iat": iat}
    return f"{segment({'alg': 'RS256'})}.{segment(claims)}.signature"


def test_identity_token_is_cached_no_longer_than_the_lane_accepts_it():
    """Production bug: Identity token is cached no longer than the lane accepts it.

    An hour-long token is not an hour of cache. The secrets platform refuses
    a machine token issued more than 15 minutes ago whatever its `exp` says
    (live, 2026-09-24: a Retry 20 minutes after the first one answered "no
    service identity" on a token this room was still holding). The cache is
    bounded by the token's `iat` plus `_TOKEN_MAX_AGE_S`, well inside that."""
    calls = []
    clock = {"now": 1_000.0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        now = int(clock["now"])
        return httpx.Response(200, text=_jwt(now + 3600, iat=now))

    provider = MetadataIdentityTokenProvider(
        AUDIENCE,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        now=lambda: clock["now"],
    )

    first = provider()
    clock["now"] += _TOKEN_MAX_AGE_S - 1
    assert provider() == first
    assert len(calls) == 1

    clock["now"] += 2  # past iat + the age this room allows itself
    second = provider()

    assert len(calls) == 2
    assert second != first
    assert _TOKEN_MAX_AGE_S < 900  # the platform's own ceiling, with room to spare


# --- from test_skills.py ---


REPO__skills = "acme/agent-x"


class _CapturingGitHub__skills(FakeGitHub):
    """A FakeGitHub that snapshots bundle/skills/**/*.md content the moment
    commit_push is called — the only window in which the caller's cloned
    workdir still exists, since skills.py discards its TemporaryDirectory as
    part of returning. Mirrors test_knowledge.py's _CapturingGitHub."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.captured_skills: dict[str, str] = {}

    def commit_push(self, workdir: Path, message: str) -> bool:
        skills_dir = workdir / "bundle" / "skills"
        if skills_dir.is_dir():
            self.captured_skills = {
                str(path.relative_to(skills_dir)): path.read_text()
                for path in skills_dir.rglob("*")
                if path.is_file()
            }
        return super().commit_push(workdir, message)


def test_save_local_skill_writes_frontmatter_for_a_plain_body():
    """Production bug: Save local skill writes frontmatter for a plain body.

    Data integrity (issue #265): what the owner wrote is kept whole and
    only prefixed; the written file carries the frontmatter the runtime
    requires, named after the skill id."""
    github = _CapturingGitHub__skills()
    save_local_skill(github, REPO__skills, "survey-field", "# Survey first\nAsk before acting.\n")
    assert github.captured_skills["survey-field/SKILL.md"] == (
        "---\nname: survey-field\ndescription: Survey first\n---\n\n# Survey first\nAsk before acting.\n"
    )


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


def _make_agent__steps(
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


def _make_ctx(session, agent, config, clients) -> Ctx:
    workdir = config.workdir / agent.name
    workdir.mkdir(parents=True, exist_ok=True)
    return Ctx(agent=agent, session=session, config=config, clients=clients, workdir=workdir)


def _clients__steps(
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


def _repo_full_name(config) -> str:
    return f"{config.github_org}/agent-{AGENT_NAME}"


HELLO_REPO = "acme/agent-clio"


def _hello_ctx(
    session_factory,
    config,
    fake_storage,
    *,
    github=None,
    name=None,
    agent_client=None,
    cabal=None,
):
    session, agent = _make_agent__steps(session_factory, repo_full_name=HELLO_REPO)
    clients = _clients__steps(
        github=github if github is not None else FakeGitHub(),
        storage=fake_storage,
        agent=agent_client if agent_client is not None else FakeAgent(),
        kubectl=FakeKubectl(),
        name=name,
        cabal=cabal,
    )
    return _make_ctx(session, agent, config, clients), agent


HELLO_CABAL_ENDPOINT_TEMPLATE = "http://agent-{name}.default.svc.cluster.local"


def _hello_cabal_enabled_config(config):
    return config.model_copy(
        update={
            "cabal_url": "http://cabal.internal",
            "agent_endpoint_template": HELLO_CABAL_ENDPOINT_TEMPLATE,
        }
    )


class _FlakyCabal:
    """`relay()` counterpart to `_FlakyAgent`, for pinning that the retry
    wraps the relay path too, not just the legacy direct path."""

    def __init__(self, *, fail_times: int, error: str = "switchboard unreachable"):
        self._fail_times = fail_times
        self._error = error
        self.relay_calls: list[dict] = []

    def relay(self, name: str, task_envelope: dict) -> dict:
        self.relay_calls.append({"name": name, "task_envelope": task_envelope})
        if len(self.relay_calls) <= self._fail_times:
            raise CabalError(self._error)
        return {"status": {"message": {"parts": [{"text": "hi from the switchboard"}]}}}


def _hello_sleep_recorder(monkeypatch):
    import kobo_api.steps as steps_module

    sleep_calls: list[int] = []
    monkeypatch.setattr(steps_module, "_sleep", sleep_calls.append)
    return sleep_calls


def test_onboard_step_conflict_resume_reprovisions_a_failed_app(session_factory, config):
    """Production bug: Onboard step conflict resume reprovisions a failed app.

    The live incident, reproduced exactly: a first provisioning attempt's
    OnboardStep created the app, then failed (an environmental kubectl outage
    on gojira's own side) — the app is now stuck in gojira's terminal
    `failed` state. An operator fixes the environment and the product Retry
    re-runs OnboardStep: create_app 409-conflicts (the app already exists)
    and the resume branch adopts the recorded id, exactly like the plain
    resume test above. The difference this test pins: that recorded id's
    live status is `failed`, not provisioning/active — so a bare "trust the
    id and keep polling" would just read the same stale failure straight
    back and raise, and the retry could never converge. Asserts the fix:
    reprovision() is called exactly once (on the right id) before the step
    waits, and the step then succeeds.
    """
    repo = _repo_full_name(config)
    gojira = FakeGojira(activate_after=1, end_status="failed", failed_steps=[
        {"name": "provision", "status": "failed", "error": "kubectl outage"},
    ])
    # Seed the fake exactly like the plain resume test: a prior attempt's
    # create_app already succeeded...
    prior_app_id = gojira.create_app(
        name=AGENT_NAME, repo_full_name=repo, cluster=config.gojira_cluster, namespace=AGENT_NAME
    )
    # ...and that prior attempt's own poll already observed it land in
    # gojira's terminal `failed` state (the stale status this retry must not
    # just trust).
    assert gojira.app_status(prior_app_id)["status"] == "failed"
    gojira._conflict_repos.add(repo)  # now gojira reports a live conflict for this repo
    # The operator has fixed the environment: a reprovision from here on
    # would actually succeed. Flip the fake's target status accordingly
    # (same idiom test_api_agents.py's retry-recovery test uses) so the
    # step's own reprovision -> _wait_active sequence can observe that.
    gojira._end_status = "active"

    session, agent = _make_agent__steps(session_factory, repo_full_name=repo, gojira_app_id=prior_app_id)
    ctx = _make_ctx(session, agent, config, _clients__steps(gojira=gojira))

    result = OnboardStep().run(ctx)

    assert gojira.reprovision_calls == [prior_app_id]
    assert agent.gojira_app_id == prior_app_id
    assert "active" in result


def test_onboard_step_conflict_resume_survives_stale_failed_read_after_reprovision(
    session_factory, config
):
    """Production bug: Onboard step conflict resume survives stale failed read after reprovision.

    Follow-up live incident, verified against the platform's DB: gojira
    DID accept the reprovision() kick above (a fresh provision run started
    ~2s later) but `_wait_active`'s first poll landed in the window before
    that new run flipped the app's status — it read the OLD run's stale
    `failed` (still carrying the old run's failed-steps detail) and raised
    immediately, even though the rerun was genuinely underway. A plain
    read-after-write race, the same class this codebase already learned to
    absorb on CI-run reads (BuildStep's grace, P1c).

    Modeled directly via `status_sequence`: the initial check reads
    `failed` (triggering reprovision), the very next read echoes that SAME
    stale `failed`, then the platform's new run actually shows up as
    `provisioning`, then `active`. Asserts the fix: the step still succeeds,
    and reprovision() is called exactly once — the stale echo must not
    trigger a second reprovision kick.
    """
    repo = _repo_full_name(config)
    gojira = FakeGojira(status_sequence=["failed", "failed", "provisioning", "active"])
    prior_app_id = gojira.create_app(
        name=AGENT_NAME, repo_full_name=repo, cluster=config.gojira_cluster, namespace=AGENT_NAME
    )
    gojira._conflict_repos.add(repo)  # now gojira reports a live conflict for this repo

    session, agent = _make_agent__steps(session_factory, repo_full_name=repo, gojira_app_id=prior_app_id)
    ctx = _make_ctx(session, agent, config, _clients__steps(gojira=gojira))

    result = OnboardStep().run(ctx)

    assert gojira.reprovision_calls == [prior_app_id]
    assert agent.gojira_app_id == prior_app_id
    assert "active" in result


def test_hello_step_retries_the_relay_path_too_and_succeeds(
    session_factory, config, fake_storage, monkeypatch
):
    """Production bug: Hello step retries the relay path too and succeeds.

    The retry wraps whichever contact path is selected, pinned here on
    the relay path (the incident's own wording: "on a relay failure,
    retries with backoff") so the loop isn't accidentally scoped to only
    the legacy direct path."""
    sleep_calls = _hello_sleep_recorder(monkeypatch)
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _hello_cabal_enabled_config(config)
    flaky_cabal = _FlakyCabal(fail_times=2)
    ctx, agent = _hello_ctx(session_factory, cfg, fake_storage, cabal=flaky_cabal)

    HelloStep().run(ctx)  # must not raise

    assert agent.first_reply == "hi from the switchboard"
    # The first HELLO_RETRY_ATTEMPTS calls are hello's own first-contact
    # retries (two failures then a success); the capability-distill self.md
    # refresh tail (Task 6) fires one further, distinct relay call right
    # after, over this same `clients.cabal` -- not itself a hello attempt,
    # so it is not counted against HELLO_RETRY_ATTEMPTS.
    assert len(flaky_cabal.relay_calls) == HELLO_RETRY_ATTEMPTS + 1 == 4
    assert sleep_calls == [HELLO_RETRY_BACKOFF_S, HELLO_RETRY_BACKOFF_S]


# --- from test_steps_wire.py ---


AGENT_NAME = "clio"


PERSONA = "helpful-generalist"


MODEL_ID = "vertex_ai/claude-sonnet-4-5"


@pytest.fixture(autouse=True)
def _no_real_sleep__steps_wire(monkeypatch):
    """WireStep (via _wait_active) and BuildStep both run poll loops; never
    let either sleep for real."""
    import kobo_api.steps as steps_module

    sleep_calls = []

    def fake_sleep(seconds):
        sleep_calls.append(seconds)

    fake_sleep.calls = sleep_calls
    monkeypatch.setattr(steps_module, "_sleep", fake_sleep)


def _make_agent__steps_wire(
    session_factory,
    *,
    name: str = AGENT_NAME,
    persona: str = PERSONA,
    model_id: str = MODEL_ID,
    repo_full_name: str | None = None,
    gojira_app_id: int | None = None,
    created_by: str | None = None,
):
    session = session_factory()
    agent = Agent(
        name=name,
        persona=persona,
        model_id=model_id,
        skills=[],
        repo_full_name=repo_full_name,
        gojira_app_id=gojira_app_id,
        created_by=created_by,
    )
    session.add(agent)
    session.commit()
    return session, agent


def _make_ctx(session, agent, config, clients) -> Ctx:
    workdir = config.workdir / agent.name
    workdir.mkdir(parents=True, exist_ok=True)
    return Ctx(agent=agent, session=session, config=config, clients=clients, workdir=workdir)


def _clients__steps_wire(*, github=None, gojira=None, kubectl=None, cabal=None, sero=None) -> Clients:
    return Clients(
        github=github, gojira=gojira, cloud=None, agent=None, kubectl=kubectl,
        cabal=cabal, sero=sero,
    )


def _repo_full_name(config) -> str:
    return f"{config.github_org}/agent-{AGENT_NAME}"


def test_build_step_failure_triggers_one_automatic_rerun_and_self_heals(session_factory, config):
    """Production bug: Build step failure triggers one automatic rerun and self heals.

    The field-observed WIF-propagation defect: a completed failure on the
    first CI run against this commit is retried automatically, once, and a
    subsequent success is reported as a self-heal rather than raising —
    Retry no longer needs an operator to `gh run rerun` by hand."""
    repo = _repo_full_name(config)
    session, agent = _make_agent__steps_wire(session_factory, repo_full_name=repo)
    github = FakeGitHub(
        head_sha="deadbeef",
        runs_by_sha={
            "deadbeef": [[{"id": 42, "status": "completed", "conclusion": "failure"}]]
        },
        rerun_result={"status": "completed", "conclusion": "success"},
    )
    ctx = _make_ctx(session, agent, config, _clients__steps_wire(github=github))

    detail = BuildStep().run(ctx)

    assert "42" in detail
    # Adapted for Fix 1: the success detail now names which conclusion the
    # rerun healed ("healed a failure run") instead of the old generic
    # "(after automatic rerun)" phrasing — the phrasing change is the point
    # of the fix (see the cancelled-run test below for the case that
    # motivated it), so this no longer contains the substring "rerun".
    assert "healed a failure run" in detail.lower()
    assert github.rerun_calls == [(repo, 42)]


def test_build_step_does_not_settle_for_a_stale_sha_success_once_head_has_advanced(
    session_factory, config
):
    """Production bug: Build step does not settle for a stale sha success once head has advanced.

    Regression test for the live incident described at module level: the
    platform's auto-merged setup PR landed while BuildStep was mid-poll, and
    a single stale head_sha read matched the WIRE commit's already-completed
    success while the *real* head had moved on to the merge commit — whose
    own CI run had been CANCELLED by GitHub's concurrency pending-queue
    rule. Shipped a stale tree that DeployStep's verification then correctly
    rejected two steps later.

    Sequenced so the pre-fix code (a single head_sha resolve at entry) would
    poll only sha1-wire and see its success on that commit's *second* poll —
    i.e. it would have passed on the stale success. The fixed code must
    instead notice, on its own second poll, that head has already advanced
    to sha2-merge, switch to watching it from then on (never re-querying
    sha1-wire again, so it never even sees that stale success), and heal
    sha2-merge's cancelled run.
    """
    repo = _repo_full_name(config)
    session, agent = _make_agent__steps_wire(session_factory, repo_full_name=repo)
    github = FakeGitHub(
        head_sha_sequence=["sha1-wire", "sha2-merge", "sha2-merge"],
        runs_by_sha={
            "sha1-wire": [
                [{"id": 1, "status": "in_progress", "conclusion": None}],
                [{"id": 1, "status": "completed", "conclusion": "success"}],
            ],
            "sha2-merge": [
                [{"id": 2, "status": "completed", "conclusion": "cancelled"}],
            ],
        },
        rerun_result={"status": "completed", "conclusion": "success"},
    )
    ctx = _make_ctx(session, agent, config, _clients__steps_wire(github=github))

    detail = BuildStep().run(ctx)

    assert "2" in detail
    assert "sha2-merge" in detail
    assert "healed a cancelled run" in detail.lower()
    assert "head advanced" in detail.lower()
    # sha1-wire is only ever polled once (the in-progress poll) — its own
    # eventual success (poll 2, never reached) is never queried once the
    # head has moved on, structurally the same "never mistaken for this
    # commit" guarantee BuildStep already had per-invocation, now extended
    # across an advancing head.
    assert github.calls.count(f"runs_for_commit:{repo}:sha1-wire") == 1
    assert github.rerun_calls == [(repo, 2)]


# --- from test_web_secrets.py ---


AGENT = "clio"


REPO__web_secrets = "example-org/agent-clio"


OWNER = "owner@example.com"


PR_URL = "https://example.test/pull/7"


BASE_GOJIRA_YAML = "name: clio\nservice_account: agent-clio\n"


AUDIENCE__web_secrets = "test-audience"


KID = "test-key-1"


@pytest.fixture(scope="module")
def _key():
    private_key = ec.generate_private_key(ec.SECP256R1())
    jwk = jwt.algorithms.ECAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk.update({"kid": KID, "use": "sig", "alg": "ES256"})
    return private_key, jwk


@pytest.fixture
def session_factory__web_secrets(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__web_secrets(tmp_path, fake_storage):
    # `iap_audience` is what makes the owner-or-operator gate real: without
    # a perimeter the room short-circuits every one of those gates to
    # allowed, so a file about who may read and write an agent's secrets
    # has to turn it on.
    return Config(
        workdir=tmp_path / "work",
        skills_bucket="skills-bucket",
        personas_bucket="personas-bucket",
        iap_audience=AUDIENCE__web_secrets,
        operators=[],
    )


def _github(**kwargs) -> FakeGitHub:
    """A repository whose `gojira.yaml` reads the same two ways the room
    reads it: through `read_file` (the panel's own read) and through a
    clone (what a write edits and pushes)."""
    repo_files = kwargs.pop("repo_files", {"gojira.yaml": BASE_GOJIRA_YAML})
    extra = kwargs.pop("extra_clone_files", None)
    if extra is None and "gojira.yaml" in repo_files:
        extra = {"gojira.yaml": repo_files["gojira.yaml"]}
    return FakeGitHub(
        existing_repos={REPO__web_secrets},
        repo_files=repo_files,
        extra_clone_files=extra,
        pr_url=PR_URL,
        **kwargs,
    )


def _client__web_secrets(
    session_factory__web_secrets,
    config__web_secrets,
    fake_storage,
    key,
    *,
    kind: str = KIND_COMPOSED,
    sero: FakeSero | None = None,
    github: FakeGitHub | None = None,
    kubectl: FakeKubectl | None = None,
    created_by: str | None = OWNER,
    status: AgentStatus = AgentStatus.READY,
) -> tuple[TestClient, Clients]:
    github = github or _github()
    gojira = FakeGojira()
    app_id = gojira.create_app(
        name=AGENT, repo_full_name=REPO__web_secrets, cluster="a-cluster", namespace=AGENT
    )
    clients = Clients(
        github=github,
        gojira=gojira,
        cloud=FakeCloud(),
        agent=FakeAgent(reply="sure"),
        kubectl=kubectl or FakeKubectl(),
        storage=fake_storage,
        slack=FakeSlack(),
        cabal=FakeCabal(),
        image=FakeImage(),
        sero=sero,
    )

    session = session_factory__web_secrets()
    session.add(
        Agent(
            name=AGENT,
            persona="connected" if kind == KIND_CONNECTED else "helpful-generalist",
            model_id="vertex_ai/claude-sonnet-4-5",
            skills=[],
            status=status,
            repo_full_name=REPO__web_secrets,
            gojira_app_id=app_id,
            created_by=created_by,
            kind=kind,
        )
    )
    session.commit()
    session.close()

    _, jwk = key
    app = create_app(
        session_factory__web_secrets,
        config=config__web_secrets,
        clients=clients,
        iap_jwks_fetcher=lambda: {"keys": [jwk]},
    )
    return TestClient(app), clients


def _as(key, email: str = OWNER) -> dict[str, str]:
    """A signed assertion naming `email`, the way the perimeter hands one
    to the room. Named once so every case reads as "who is asking" rather
    than as plumbing."""
    private_key, _ = key
    now = int(time.time())
    claims = {
        "aud": AUDIENCE__web_secrets,
        "iss": IAP_ISSUER,
        "iat": now,
        "exp": now + 3600,
        "sub": "accounts.google.com:1234567890",
        "email": email,
    }
    return {ASSERTION_HEADER: jwt.encode(claims, private_key, algorithm="ES256",
                                         headers={"kid": KID})}


@pytest.fixture(autouse=True)
def _clear_restart_window():
    rate_limit.reset()
    yield
    rate_limit.reset()


def test_a_connected_repositorys_env_is_operator_only(session_factory__web_secrets, config__web_secrets, fake_storage, _key):
    """Production bug: A connected repositorys env is operator only.

    Issue #200: whoever connects a repo becomes its owner in kobo, and the room
    cannot check who owns it on GitHub, so its env and CI log are operator-only."""
    operator = "ops@example.com"
    operator_config = config__web_secrets.model_copy(update={"operators": [operator]})
    github = _github(repo_files={"gojira.yaml": "name: clio\nenv:\n  API_BASE: https://example.test\n"})
    client, _ = _client__web_secrets(session_factory__web_secrets, operator_config, fake_storage, _key, kind=KIND_CONNECTED, github=github)

    as_owner = client.get(f"/api/agents/{AGENT}/env", headers=_as(_key))
    assert as_owner.status_code == 403
    assert "operator-only" in as_owner.json()["error"]
    as_operator = client.get(f"/api/agents/{AGENT}/env", headers=_as(_key, email=operator))
    assert as_operator.status_code == 200
    assert as_operator.json()["env"] == {"API_BASE": "https://example.test"}


# --- from test_web_teach.py ---


AGENT_NAME = "clio"


PERSONA = "helpful-generalist"


REPO__web_teach = "acme/agent-clio"


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


def test_teach_records_one_approved_taught_row_even_when_taught_twice(
    session_factory__web_teach, config__web_teach
):
    """Production bug: Teach records one approved taught row even when taught twice.

    Data integrity (issue #269): a hand-taught note leaves exactly one
    AgentLearning row, approved and sourced `taught`, so the Learnings
    table shows it after a reload and the decide routes (pending only)
    never offer it for Remember/Skip; teaching the same text again
    refreshes that row rather than inserting a twin."""
    client = _make_client__web_teach(session_factory__web_teach, config__web_teach, github=_CapturingGitHub__web_teach())
    _seed_agent__web_teach(session_factory__web_teach, config__web_teach)

    text = "always answer in one paragraph"
    first = client.post(f"/api/agents/{AGENT_NAME}/teach", json={"text": text})
    second = client.post(f"/api/agents/{AGENT_NAME}/teach", json={"text": text})

    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["learning"]["content"] == text
    assert first.json()["learning"]["status"] == LEARNING_APPROVED
    session = session_factory__web_teach()
    rows = session.query(AgentLearning).filter_by(agent_name=AGENT_NAME).all()
    session.close()
    assert [(r.content, r.source, r.status) for r in rows] == [
        (text, SOURCE_TAUGHT, LEARNING_APPROVED)
    ]

