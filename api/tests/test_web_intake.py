"""Tests for the intake HTTP routes (Task 3): upload/list/delete an agent's
one-shot `bundle/intake/*` memory files — all against the fakes
(`tests/fakes.py`), no real git/kubectl involved.

Same harness shape as test_web_skills.py (own `session_factory`/`config`
fixtures, a `TestClient` built from `create_app`): agents are seeded
directly at whatever status a test wants, already carrying a
`repo_full_name` (these routes need one to call into `kobo_api.intake` at
all). Namespace/deploy for the status-derivation call are both the agent's
own name, same convention `knowledge_delivery`'s route already uses.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.clients import ClientError, Clients
from kobo_api.config import Config
from kobo_api.intake import INTAKE_MAX_BYTES
from kobo_api.main import create_app
from kobo_api.models import Agent, AgentStatus, Base
from tests.fakes import (
    FAKE_DOCKERFILE_OLD_IMAGE,
    FAKE_LOCAL_HEAD_DEFAULT,
    FakeAgent,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
    FakeStorage,
)

AGENT_NAME = "clio"
PERSONA = "helpful-generalist"
REPO = "acme/agent-clio"

# Matches FakeKubectl's default deploy_image tag (FAKE_DEPLOY_IMAGE_DEFAULT's
# tag is FAKE_LOCAL_HEAD_DEFAULT's own first 12 chars) — the sha every
# mutating route below returns (github.local_head).
SHA = FAKE_LOCAL_HEAD_DEFAULT

DELETE_NOTE = "memories are kept — only the source file was removed"


class _CapturingGitHub(FakeGitHub):
    """Same snapshot-on-commit_push trick as test_web_skills.py's own
    `_CapturingGitHub`, replicated locally — captures `bundle/intake/*`
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


class _RaisingGitHub(FakeGitHub):
    """A GitHubClient whose `clone` raises `ClientError` — the 502 guard's
    fixture. `clone` is the first call every intake.py operation makes, so
    overriding just it covers every route's github-side 502 path."""

    def clone(self, repo: str, dest: Path) -> None:
        raise ClientError("gh: rate limited")


class _RaisingKubectl(FakeKubectl):
    """A KubectlClient whose `deploy_image` raises `ClientError` — the list
    route's other 502 seam (only reached once there's at least one intake
    file to check status for)."""

    def deploy_image(self, namespace: str, deploy: str) -> str:
        raise ClientError("kubectl: connection refused")


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


def _make_client(session_factory, config, *, github=None, kubectl=None):
    return TestClient(
        create_app(
            session_factory,
            config=config,
            clients=Clients(
                github=github if github is not None else FakeGitHub(),
                gojira=FakeGojira(),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=kubectl if kubectl is not None else FakeKubectl(),
                storage=FakeStorage(),
            ),
        )
    )


def _seed_agent(session_factory, config, *, status=AgentStatus.READY, repo_full_name=REPO):
    session = session_factory()
    session.add(
        Agent(
            name=AGENT_NAME,
            persona=PERSONA,
            model_id=config.models[0].id,
            status=status,
            repo_full_name=repo_full_name,
        )
    )
    session.commit()
    session.close()


NON_READY_STATUSES = [AgentStatus.PENDING, AgentStatus.PROVISIONING, AgentStatus.FAILED]


# --- GET /api/agents/{name}/intake -------------------------------------------


def test_list_intake_route_empty(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}/intake")

    assert response.status_code == 200
    assert response.json() == {"files": []}


def test_list_intake_route_returns_files_with_status(session_factory, config):
    # Bare fakes wired together (see tests/fakes.py FakeGitHub docstring):
    # an unconfigured intake file reads "ingested" with no extra setup.
    github = FakeGitHub(intake_files={"notes.md": "hello there"})
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}/intake")

    assert response.status_code == 200
    assert response.json() == {
        "files": [
            {
                "name": "notes.md",
                "content_bytes": len(b"hello there"),
                "status": "ingested",
            }
        ]
    }


def test_list_intake_route_unknown_agent_returns_404(session_factory, config):
    client = _make_client(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}/intake")

    assert response.status_code == 404
    assert "error" in response.json()


@pytest.mark.parametrize("status", NON_READY_STATUSES)
def test_list_intake_route_non_ready_agent_returns_409(session_factory, config, status):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, status=status)

    response = client.get(f"/api/agents/{AGENT_NAME}/intake")

    assert response.status_code == 409
    assert response.json() == {"error": "agent is not ready for memory changes"}


def test_list_intake_route_github_error_returns_502(session_factory, config):
    client = _make_client(session_factory, config, github=_RaisingGitHub())
    _seed_agent(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}/intake")

    assert response.status_code == 502
    assert response.json() == {"error": "couldn't reach your agent's files. Try again."}


def test_list_intake_route_kubectl_error_returns_502(session_factory, config):
    github = FakeGitHub(intake_files={"notes.md": "hello"})
    client = _make_client(session_factory, config, github=github, kubectl=_RaisingKubectl())
    _seed_agent(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}/intake")

    assert response.status_code == 502
    assert response.json() == {"error": "couldn't reach your agent's files. Try again."}


# --- POST /api/agents/{name}/intake ------------------------------------------


def test_upload_intake_route_creates_commits_and_returns_sha(session_factory, config):
    github = _CapturingGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/intake",
        json={"filename": "notes.md", "content": "new content"},
    )

    assert response.status_code == 200
    assert response.json() == {"sha": SHA}
    assert github.captured_intake == {"notes.md": "new content"}
    assert github.pushes[-1][1] == "kobo: add intake/notes.md"


def test_upload_intake_route_rewrites_dockerfile_to_configured_runtime_image(
    session_factory, config
):
    # B1: the route must pass the org's CURRENT config.runtime_image through
    # to upload_intake_file, which re-pins the clone's Dockerfile FROM line
    # to it in the same commit as the uploaded file — closing the
    # false-ingested window (an agent composed before a runtime bump ships a
    # seeder-less image; without this re-pin, list_intake's ancestor
    # derivation could call the upload "ingested" before any seeder capable
    # of ingesting it ever ran). FakeGitHub's default Dockerfile starts on
    # FAKE_DOCKERFILE_OLD_IMAGE, distinct from config.runtime_image's own
    # default, so this proves the rewrite actually happened.
    assert FAKE_DOCKERFILE_OLD_IMAGE != config.runtime_image

    github = _CapturingGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/intake",
        json={"filename": "notes.md", "content": "new content"},
    )

    assert response.status_code == 200
    assert github.captured_intake == {"notes.md": "new content"}
    assert github.captured_dockerfile == (
        "# Agent image: the shared runtime plus this agent's bundle.\n"
        f"FROM {config.runtime_image}\n"
        "COPY bundle/ /bundle\n"
    )


def test_upload_intake_route_accepts_txt_extension(session_factory, config):
    github = _CapturingGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/intake",
        json={"filename": "transcript.txt", "content": "hello"},
    )

    assert response.status_code == 200
    assert github.captured_intake == {"transcript.txt": "hello"}


def test_upload_intake_route_invalid_filename_returns_422(session_factory, config):
    github = _CapturingGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/intake",
        json={"filename": "Not-Valid.md", "content": "content"},
    )

    assert response.status_code == 422
    assert "invalid intake filename" in response.json()["error"]
    assert github.pushes == []


def test_upload_intake_route_empty_content_returns_422(session_factory, config):
    github = _CapturingGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/intake", json={"filename": "notes.md", "content": ""}
    )

    assert response.status_code == 422
    assert "empty" in response.json()["error"]
    assert github.pushes == []


def test_upload_intake_route_oversize_content_returns_422(session_factory, config):
    github = _CapturingGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/intake",
        json={"filename": "notes.md", "content": "a" * (INTAKE_MAX_BYTES + 1)},
    )

    assert response.status_code == 422
    assert "exceeds" in response.json()["error"]
    assert github.pushes == []


def test_upload_intake_route_unknown_agent_returns_404(session_factory, config):
    client = _make_client(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/intake",
        json={"filename": "notes.md", "content": "content"},
    )

    assert response.status_code == 404
    assert "error" in response.json()


@pytest.mark.parametrize("status", NON_READY_STATUSES)
def test_upload_intake_route_non_ready_agent_returns_409(session_factory, config, status):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, status=status)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/intake",
        json={"filename": "notes.md", "content": "content"},
    )

    assert response.status_code == 409
    assert response.json() == {"error": "agent is not ready for memory changes"}


def test_upload_intake_route_client_error_returns_502(session_factory, config):
    client = _make_client(session_factory, config, github=_RaisingGitHub())
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/intake",
        json={"filename": "notes.md", "content": "content"},
    )

    assert response.status_code == 502
    assert response.json() == {"error": "couldn't reach your agent's files. Try again."}


# --- DELETE /api/agents/{name}/intake/{filename} -----------------------------


def test_delete_intake_route_removes_and_returns_sha_and_verbatim_note(session_factory, config):
    github = _CapturingGitHub(intake_files={"notes.md": "bye", "keep.md": "stay"})
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.delete(f"/api/agents/{AGENT_NAME}/intake/notes.md")

    assert response.status_code == 200
    assert response.json() == {"sha": SHA, "note": DELETE_NOTE}
    assert github.captured_intake == {"keep.md": "stay"}
    assert github.pushes[-1][1] == "kobo: remove intake/notes.md"


def test_delete_intake_route_not_found_returns_404(session_factory, config):
    github = FakeGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.delete(f"/api/agents/{AGENT_NAME}/intake/ghost.md")

    assert response.status_code == 404
    assert "error" in response.json()
    assert github.pushes == []


def test_delete_intake_route_unknown_agent_returns_404(session_factory, config):
    client = _make_client(session_factory, config)

    response = client.delete(f"/api/agents/{AGENT_NAME}/intake/notes.md")

    assert response.status_code == 404
    assert "error" in response.json()


@pytest.mark.parametrize("status", NON_READY_STATUSES)
def test_delete_intake_route_non_ready_agent_returns_409(session_factory, config, status):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, status=status)

    response = client.delete(f"/api/agents/{AGENT_NAME}/intake/notes.md")

    assert response.status_code == 409
    assert response.json() == {"error": "agent is not ready for memory changes"}


def test_delete_intake_route_client_error_returns_502(session_factory, config):
    client = _make_client(session_factory, config, github=_RaisingGitHub())
    _seed_agent(session_factory, config)

    response = client.delete(f"/api/agents/{AGENT_NAME}/intake/notes.md")

    assert response.status_code == 502
    assert response.json() == {"error": "couldn't reach your agent's files. Try again."}
