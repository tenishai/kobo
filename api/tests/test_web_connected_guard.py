"""The room never writes to a repository it does not own.

Two steps of the connected pipeline honour that by construction. Every
route in this table does not: each one clones the agent's repo and pushes
to its DEFAULT BRANCH, gated on "ready" plus "owner or operator" (teach on
"ready" alone), all of which a connected agent passes the moment it goes
READY. `_room_edits_this_agent_or_409` is the one sentence that stops them,
and this module is the proof that every one of them says it.

The table is written out by hand rather than discovered, and that is
deliberate: a new route that pushes to an agent's repo should have to be
added here by someone who thought about it, not be covered by a pattern
that might quietly stop matching. Each row is a route the reviewer walked
from its decorator to its `commit_push`.

Every case asserts two things, because either alone would be a weaker
test: the 409 with the exact sentence, AND that `FakeGitHub` recorded no
clone, push or pull request. A route that refused after cloning would pass
the first and fail the second.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.clients import Clients
from kobo_api.config import Config
from kobo_api.kinds import KIND_COMPOSED, KIND_CONNECTED
from kobo_api.main import create_app
from kobo_api.models import Agent, AgentLearning, AgentStatus, Base
from tests.fakes import (
    FakeAgent,
    FakeCabal,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeImage,
    FakeKubectl,
    FakeSlack,
)

AGENT_NAME = "clio"
REPO = "example-org/team-chatbot"
LEARNING_ID = 1

REFUSAL = "this agent's repository belongs to its team, so the room does not change it"

# A 1x1 PNG, for the avatar upload route's own magic-byte check.
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08"
    b"\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00"
    b"\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)

# Every route that reaches a repo write, by the path the reviewer walked
# from its decorator down to `github.commit_push`. `files`/`data` carry a
# minimally valid body: the guard must fire BEFORE any validation that
# would otherwise 422 first, so a body here that is merely plausible is
# enough, and a row that starts 422-ing is itself a finding.
GUARDED_ROUTES = [
    ("PUT", f"/api/agents/{AGENT_NAME}/knowledge/notes.md", {"json": {"content": "hi"}}),
    ("DELETE", f"/api/agents/{AGENT_NAME}/knowledge/notes.md", {}),
    (
        "POST",
        f"/api/agents/{AGENT_NAME}/skills/catalog",
        {"json": {"name": "word-games", "team": "shared"}},
    ),
    (
        "POST",
        f"/api/agents/{AGENT_NAME}/skills/catalog/batch",
        {"json": {"skills": [{"name": "word-games", "team": "shared"}]}},
    ),
    ("DELETE", f"/api/agents/{AGENT_NAME}/skills/word-games", {}),
    (
        "PUT",
        f"/api/agents/{AGENT_NAME}/skills/local/note-taking",
        {"json": {"content": "# Note taking\n"}},
    ),
    (
        "POST",
        f"/api/agents/{AGENT_NAME}/intake",
        {"json": {"filename": "note.md", "content": "hello"}},
    ),
    ("DELETE", f"/api/agents/{AGENT_NAME}/intake/note.md", {}),
    ("POST", f"/api/agents/{AGENT_NAME}/teach", {"json": {"text": "remember this"}}),
    (
        "POST",
        f"/api/agents/{AGENT_NAME}/learnings/{LEARNING_ID}/approve",
        {"json": {}},
    ),
    (
        "POST",
        f"/api/agents/{AGENT_NAME}/learnings/bulk",
        {"json": {"action": "approve", "ids": [LEARNING_ID]}},
    ),
    (
        "POST",
        f"/api/agents/{AGENT_NAME}/tooling",
        {"json": {"filename": "helper.py", "content": "def go():\n    return 1\n"}},
    ),
    (
        "POST",
        f"/api/agents/{AGENT_NAME}/tooling/import",
        {"json": {"repo": "example-org/other", "path": "tools/helper.py"}},
    ),
    ("DELETE", f"/api/agents/{AGENT_NAME}/tooling/helper.py", {}),
    (
        "POST",
        f"/api/agents/{AGENT_NAME}/slack/connect",
        {
            "json": {
                "app_id": "A01234567",
                "bot_token": "xoxb-placeholder",
                "app_token": "xapp-placeholder",
            }
        },
    ),
    (
        "PUT",
        f"/api/agents/{AGENT_NAME}/slack/channels",
        {"json": {"channels": ["C01234567"]}},
    ),
    (
        "PATCH",
        f"/api/agents/{AGENT_NAME}/runtime",
        {"json": {"runtime_image": "example.dev/runtime@sha256:" + "a" * 64}},
    ),
    (
        "PATCH",
        f"/api/agents/{AGENT_NAME}/metadata",
        {"json": {"purpose_tags": ["billing"]}},
    ),
    ("POST", f"/api/agents/{AGENT_NAME}/metadata/regenerate", {"json": {}}),
    ("POST", f"/api/agents/{AGENT_NAME}/avatar", {"json": {}}),
    (
        "PUT",
        f"/api/agents/{AGENT_NAME}/avatar",
        {"files": {"file": ("avatar.png", PNG_BYTES, "image/png")}},
    ),
    # Not a repo write, but the same rule: registering a connected agent in
    # the switchboard would create a directory entry destroy then declines
    # to retire, because a connected agent is never listed there.
    ("POST", f"/api/agents/{AGENT_NAME}/directory/sync", {"json": {}}),
]

# What this guard must NOT touch. Reading an agent is never refused: a
# connected agent's status, checklist and build log are as real as any
# other's, and an empty skill or knowledge listing is an honest answer.
READ_ROUTES = [
    f"/api/agents/{AGENT_NAME}",
    f"/api/agents/{AGENT_NAME}/knowledge",
    f"/api/agents/{AGENT_NAME}/skills",
    f"/api/agents/{AGENT_NAME}/intake",
    f"/api/agents/{AGENT_NAME}/tooling",
]


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config(tmp_path, fake_storage):
    return Config(
        workdir=tmp_path / "work",
        skills_bucket="skills-bucket",
        personas_bucket="personas-bucket",
        cabal_url="http://cabal.internal",
        agent_endpoint_template="http://agent-{name}.default.svc.cluster.local",
    )


@pytest.fixture
def github():
    return FakeGitHub(existing_repos={REPO})


@pytest.fixture
def clients(github, fake_storage):
    return Clients(
        github=github,
        gojira=FakeGojira(),
        cloud=FakeCloud(),
        agent=FakeAgent(reply="sure"),
        kubectl=FakeKubectl(),
        storage=fake_storage,
        slack=FakeSlack(),
        cabal=FakeCabal(),
        image=FakeImage(),
    )


def _seed(session_factory, *, kind: str) -> None:
    session = session_factory()
    agent = Agent(
        name=AGENT_NAME,
        persona="connected" if kind == KIND_CONNECTED else "helpful-generalist",
        model_id="vertex_ai/claude-sonnet-4-5",
        skills=[],
        status=AgentStatus.READY,
        repo_full_name=REPO,
        kind=kind,
    )
    session.add(agent)
    session.add(
        AgentLearning(
            id=LEARNING_ID,
            agent_name=AGENT_NAME,
            day="2026-09-21",
            content="the billing export runs at 02:00",
            kind="fact",
            source="Slack, Sep 21",
            evidence="quote",
            content_hash="a" * 64,
        )
    )
    session.commit()
    session.close()


@pytest.fixture
def client(session_factory, config, clients, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    return TestClient(create_app(session_factory, config=config, clients=clients))


def _touched_the_repo(github: FakeGitHub) -> list[str]:
    reaching = ("clone:", "commit_push:", "open_pr:", "create_repo_from_dir:")
    return [call for call in github.calls if call.startswith(reaching)]


@pytest.mark.parametrize(
    ("method", "path", "body"),
    GUARDED_ROUTES,
    ids=[f"{method} {path}" for method, path, _ in GUARDED_ROUTES],
)
def test_every_repo_writing_route_refuses_a_connected_agent(
    client, session_factory, github, method, path, body
):
    _seed(session_factory, kind=KIND_CONNECTED)

    response = client.request(method, path, **body)

    assert response.status_code == 409, response.text
    assert response.json()["error"] == REFUSAL
    assert _touched_the_repo(github) == []


@pytest.mark.parametrize(
    ("method", "path", "body"),
    GUARDED_ROUTES,
    ids=[f"{method} {path}" for method, path, _ in GUARDED_ROUTES],
)
def test_no_repo_writing_route_refuses_a_composed_agent_with_this_message(
    client, session_factory, method, path, body
):
    """The guard must be about the kind and nothing else. A composed agent
    may still fail these calls for its own reasons (a missing skill, a
    Slack client that is not wired) -- it must never fail with THIS
    sentence."""
    _seed(session_factory, kind=KIND_COMPOSED)

    response = client.request(method, path, **body)

    if response.status_code == 409:
        assert response.json().get("error") != REFUSAL


@pytest.mark.parametrize("path", READ_ROUTES)
def test_reading_a_connected_agent_is_never_refused(client, session_factory, path):
    _seed(session_factory, kind=KIND_CONNECTED)

    response = client.get(path)

    assert response.status_code == 200, response.text


def test_listing_skills_for_a_connected_agent_never_pushes(
    client, session_factory, github
):
    """`GET .../skills` reconciles the ledger against the bundle and can
    COMMIT rows to the database, which is fine. It must never push."""
    _seed(session_factory, kind=KIND_CONNECTED)

    response = client.get(f"/api/agents/{AGENT_NAME}/skills")

    assert response.status_code == 200
    assert not any(call.startswith("commit_push:") for call in github.calls)
    assert not any(call.startswith("open_pr:") for call in github.calls)
