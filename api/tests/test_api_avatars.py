"""Tests for the avatar HTTP routes (P2d): persona-derived face generation
(`POST .../avatar`), its DB-cached read-back (`GET .../avatar`), and the
"has_avatar" field folded into `GET /api/agents` and `GET /api/agents/{name}`.
Same harness shape as test_api_knowledge.py/test_api_slack.py: fakes only
(`tests/fakes.py`), no real git/Vertex AI network involved anywhere.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.avatars import avatar_prompt
from kobo_api.clients import ClientError, Clients
from kobo_api.config import Config
from kobo_api.main import AVATAR_MAX_BYTES, create_app
from kobo_api.models import Agent, AgentStatus, Base
from kobo_api.personas import seed_default_personas
from tests.fakes import (
    FAKE_IMAGE_PNG_BYTES,
    FAKE_LOCAL_HEAD_DEFAULT,
    FakeAgent,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeImage,
    FakeKubectl,
    FakeStorage,
)

AGENT_NAME = "clio"
PERSONA = "helpful-generalist"
PERSONA_DESCRIPTION = "A friendly, concise generalist assistant."
REPO = "acme/agent-clio"

EXPECTED_PROMPT = avatar_prompt(AGENT_NAME, PERSONA_DESCRIPTION)


class _CapturingGitHub(FakeGitHub):
    """Snapshots bundle/avatar.png bytes at the moment commit_push is called
    — the only window in which set_avatar's cloned workdir still exists
    before it's discarded. Same trick as test_api_knowledge.py's own
    _CapturingGitHub, replicated locally rather than imported cross-file."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.captured_avatar_bytes: bytes | None = None

    def commit_push(self, workdir: Path, message: str) -> bool:
        avatar_path = workdir / "bundle" / "avatar.png"
        if avatar_path.is_file():
            self.captured_avatar_bytes = avatar_path.read_bytes()
        return super().commit_push(workdir, message)


class _RaisingGitHub(FakeGitHub):
    """clone raises ClientError — models a transient GitHub outage on the
    push side of avatar generation (mirrors test_api_knowledge.py's own
    fixture of the same name)."""

    def clone(self, repo: str, dest: Path) -> None:
        raise ClientError("gh: rate limited")


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config(tmp_path):
    # Task 5: generate_avatar's persona-description lookup now reads the
    # bucket catalog (via search.get_entry) instead of the old local-clone
    # scanner — buckets must be named for that lookup to find anything.
    return Config(
        workdir=tmp_path / "work",
        skills_bucket="skills-bucket",
        personas_bucket="personas-bucket",
    )


# Sentinel distinguishing "image kwarg not passed" (-> default FakeImage())
# from an explicit `image=None` (-> Clients.image is None, the "picture
# generation is not available" 503 path) — plain `None` can't serve as both,
# same idiom as test_api_slack.py's own _SLACK_UNSET.
_IMAGE_UNSET = object()


def _make_client(session_factory, config, *, github=None, image=_IMAGE_UNSET, storage=None):
    if storage is None:
        # Seeded the same way a real bootstrap seeds a fresh org's bucket —
        # gives generate_avatar's persona-description lookup something real
        # to find ("helpful-generalist") without every call site here
        # needing to set this up itself.
        storage = FakeStorage()
        seed_default_personas(storage, bucket=config.personas_bucket)
    return TestClient(
        create_app(
            session_factory,
            config=config,
            clients=Clients(
                github=github if github is not None else FakeGitHub(),
                gojira=FakeGojira(),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=FakeKubectl(),
                image=FakeImage() if image is _IMAGE_UNSET else image,
                storage=storage,
            ),
        )
    )


def _seed_agent(
    session_factory,
    config,
    *,
    status=AgentStatus.READY,
    repo_full_name=REPO,
    avatar_png=None,
):
    session = session_factory()
    session.add(
        Agent(
            name=AGENT_NAME,
            persona=PERSONA,
            model_id=config.models[0].id,
            status=status,
            repo_full_name=repo_full_name,
            avatar_png=avatar_png,
        )
    )
    session.commit()
    session.close()


NON_READY_STATUSES = [AgentStatus.PENDING, AgentStatus.PROVISIONING, AgentStatus.FAILED]


# --- POST /api/agents/{name}/avatar -------------------------------------------------


def test_generate_avatar_happy_path_pushes_caches_and_returns_sha(session_factory, config):
    github = _CapturingGitHub()
    image = FakeImage()
    client = _make_client(session_factory, config, github=github, image=image)
    _seed_agent(session_factory, config)

    response = client.post(f"/api/agents/{AGENT_NAME}/avatar")

    assert response.status_code == 200
    assert response.json() == {"sha": FAKE_LOCAL_HEAD_DEFAULT}
    assert github.captured_avatar_bytes == FAKE_IMAGE_PNG_BYTES
    assert github.pushes[-1][1] == "kobo: set avatar"
    assert image.prompts == [EXPECTED_PROMPT]

    session = session_factory()
    agent = session.query(Agent).filter_by(name=AGENT_NAME).first()
    assert agent.avatar_png == base64.b64encode(FAKE_IMAGE_PNG_BYTES).decode("ascii")
    session.close()


def test_generate_avatar_regenerate_overwrites_bytes_and_cache(session_factory, config):
    github = _CapturingGitHub()
    first_bytes = FAKE_IMAGE_PNG_BYTES
    second_bytes = b"\x89PNG\r\n\x1a\ntotally-different-generation"

    first_client = _make_client(
        session_factory, config, github=github, image=FakeImage(image_bytes=first_bytes)
    )
    _seed_agent(session_factory, config)
    first_response = first_client.post(f"/api/agents/{AGENT_NAME}/avatar")
    assert first_response.status_code == 200

    second_image = FakeImage(image_bytes=second_bytes)
    second_client = _make_client(session_factory, config, github=github, image=second_image)
    second_response = second_client.post(f"/api/agents/{AGENT_NAME}/avatar")

    assert second_response.status_code == 200
    assert second_response.json() == {"sha": FAKE_LOCAL_HEAD_DEFAULT}
    assert github.captured_avatar_bytes == second_bytes
    assert second_image.prompts == [EXPECTED_PROMPT]

    session = session_factory()
    agent = session.query(Agent).filter_by(name=AGENT_NAME).first()
    assert agent.avatar_png == base64.b64encode(second_bytes).decode("ascii")
    session.close()


def test_generate_avatar_unknown_agent_returns_404(session_factory, config):
    client = _make_client(session_factory, config)

    response = client.post(f"/api/agents/{AGENT_NAME}/avatar")

    assert response.status_code == 404
    assert "error" in response.json()


@pytest.mark.parametrize("status", NON_READY_STATUSES)
def test_generate_avatar_non_ready_agent_returns_409(session_factory, config, status):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, status=status)

    response = client.post(f"/api/agents/{AGENT_NAME}/avatar")

    assert response.status_code == 409
    assert "error" in response.json()


def test_generate_avatar_no_image_client_returns_503(session_factory, config):
    client = _make_client(session_factory, config, image=None)
    _seed_agent(session_factory, config)

    response = client.post(f"/api/agents/{AGENT_NAME}/avatar")

    assert response.status_code == 503
    assert response.json() == {"error": "picture generation is not available"}


def test_generate_avatar_client_error_returns_fixed_502(session_factory, config):
    image = FakeImage(generate_error="the picture didn't come through — try again")
    github = FakeGitHub()
    client = _make_client(session_factory, config, github=github, image=image)
    _seed_agent(session_factory, config)

    response = client.post(f"/api/agents/{AGENT_NAME}/avatar")

    assert response.status_code == 502
    assert response.json() == {"error": "the picture didn't come through — try again"}
    assert github.cloned == []

    session = session_factory()
    agent = session.query(Agent).filter_by(name=AGENT_NAME).first()
    assert agent.avatar_png is None
    session.close()


def test_generate_avatar_github_client_error_returns_502(session_factory, config):
    """A ClientError from the push side (not the image side) must also 502,
    same as every other repo-writing route in this app."""
    client = _make_client(session_factory, config, github=_RaisingGitHub())
    _seed_agent(session_factory, config)

    response = client.post(f"/api/agents/{AGENT_NAME}/avatar")

    assert response.status_code == 502
    assert response.json() == {"error": "couldn't save your agent's picture — try again"}


def test_generate_avatar_catalogs_unconfigured_falls_back_and_warns(session_factory, config, caplog):
    github = _CapturingGitHub()
    image = FakeImage()
    # Storage with empty personas bucket (unconfigured)
    empty_storage = FakeStorage()
    client = _make_client(session_factory, config, github=github, image=image, storage=empty_storage)
    _seed_agent(session_factory, config)

    response = client.post(f"/api/agents/{AGENT_NAME}/avatar")

    assert response.status_code == 200
    assert response.json() == {"sha": FAKE_LOCAL_HEAD_DEFAULT}
    # Verify generic text was used in the prompt
    assert len(image.prompts) == 1
    assert f"A {PERSONA} agent." in image.prompts[0]
    # Verify warning was logged
    assert any(
        f"persona {PERSONA!r} not in the catalog" in record.message
        and "avatar prompt" in record.message
        for record in caplog.records
    )


def test_generate_avatar_oversize_returns_fixed_502_and_stores_nothing(session_factory, config):
    oversize_bytes = b"x" * (AVATAR_MAX_BYTES + 1)
    github = FakeGitHub()
    image = FakeImage(image_bytes=oversize_bytes)
    client = _make_client(session_factory, config, github=github, image=image)
    _seed_agent(session_factory, config)

    response = client.post(f"/api/agents/{AGENT_NAME}/avatar")

    assert response.status_code == 502
    assert response.json() == {"error": "that picture came out too large — try again"}
    # Rejected before any store: no clone/push against the repo at all, and
    # the DB cache is untouched.
    assert github.cloned == []
    assert github.pushes == []

    session = session_factory()
    agent = session.query(Agent).filter_by(name=AGENT_NAME).first()
    assert agent.avatar_png is None
    session.close()


def test_generate_avatar_at_exact_size_cap_is_accepted(session_factory, config):
    exact_bytes = b"x" * AVATAR_MAX_BYTES
    github = _CapturingGitHub()
    image = FakeImage(image_bytes=exact_bytes)
    client = _make_client(session_factory, config, github=github, image=image)
    _seed_agent(session_factory, config)

    response = client.post(f"/api/agents/{AGENT_NAME}/avatar")

    assert response.status_code == 200
    assert github.captured_avatar_bytes == exact_bytes


# --- GET /api/agents/{name}/avatar --------------------------------------------------


def test_get_avatar_returns_png_bytes_round_trip(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(
        session_factory,
        config,
        avatar_png=base64.b64encode(FAKE_IMAGE_PNG_BYTES).decode("ascii"),
    )

    response = client.get(f"/api/agents/{AGENT_NAME}/avatar")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == FAKE_IMAGE_PNG_BYTES


def test_get_avatar_unknown_agent_returns_404(session_factory, config):
    client = _make_client(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}/avatar")

    assert response.status_code == 404
    assert "error" in response.json()


def test_get_avatar_no_cache_returns_404_no_picture_yet(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)  # avatar_png defaults to None

    response = client.get(f"/api/agents/{AGENT_NAME}/avatar")

    assert response.status_code == 404
    assert response.json() == {"error": "no picture yet"}


@pytest.mark.parametrize("status", NON_READY_STATUSES)
def test_get_avatar_no_ready_gate_non_ready_agent_still_gets_house_404(
    session_factory, config, status
):
    """GET carries no READY gate (unlike every other agent-scoped route in
    this app) — a provisioning agent's page may render; the <img> just 404s
    the same way it would for a READY agent with no cache yet."""
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, status=status)

    response = client.get(f"/api/agents/{AGENT_NAME}/avatar")

    assert response.status_code == 404
    assert response.json() == {"error": "no picture yet"}


# --- has_avatar fold-in (GET /api/agents, GET /api/agents/{name}) -------------------


def test_list_agents_has_avatar_false_then_true_after_generation(session_factory, config):
    github = _CapturingGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    before = client.get("/api/agents")
    assert before.json()["agents"][0]["has_avatar"] is False

    generate_response = client.post(f"/api/agents/{AGENT_NAME}/avatar")
    assert generate_response.status_code == 200

    after = client.get("/api/agents")
    assert after.json()["agents"][0]["has_avatar"] is True


def test_get_agent_has_avatar_false_then_true_after_generation(session_factory, config):
    github = _CapturingGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    before = client.get(f"/api/agents/{AGENT_NAME}")
    assert before.json()["has_avatar"] is False

    generate_response = client.post(f"/api/agents/{AGENT_NAME}/avatar")
    assert generate_response.status_code == 200

    after = client.get(f"/api/agents/{AGENT_NAME}")
    assert after.json()["has_avatar"] is True
