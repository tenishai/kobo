"""Tests for the four knowledge HTTP routes (P2b): read/save/delete an
agent's `bundle/knowledge/*.md` seed-memory files, plus the delivery-status
poll target — all against the fakes (`tests/fakes.py`), no real
git/kubectl/subprocess involved.

Same harness shape as test_api_agents.py (own `session_factory`/`config`
fixtures, a `TestClient` built from `create_app`), trimmed to what these
routes actually need: no pipeline/provisioning involved here, so agents are
seeded directly at whatever status a test wants, already carrying a
`repo_full_name` (these routes need one to call into `kobo_api.knowledge`
at all).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.clients import ClientError, Clients
from kobo_api.config import Config
from kobo_api.knowledge import KNOWLEDGE_MAX_BYTES
from kobo_api.main import create_app
from kobo_api.models import Agent, AgentStatus, Base
from tests.fakes import (
    FAKE_LOCAL_HEAD_DEFAULT,
    FakeAgent,
    FakeCabal,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
)

AGENT_NAME = "clio"
PERSONA = "helpful-generalist"
REPO = "acme/agent-clio"

# 40 hex chars — matches FakeKubectl's default deploy_image tag (see
# tests/fakes.py FAKE_DEPLOY_IMAGE_DEFAULT), so a bare FakeGitHub()/
# FakeKubectl() pair reads "delivered" for this sha without configuring
# either fake.
SHA = FAKE_LOCAL_HEAD_DEFAULT


class _CapturingGitHub(FakeGitHub):
    """Same snapshot-on-commit_push trick as test_knowledge.py's own
    `_CapturingGitHub`, replicated locally (rather than imported cross-file,
    which would couple this test module to another test module's internals)
    — captures `bundle/knowledge/*.md` content the moment `commit_push` is
    called, the only window in which the caller's cloned workdir still
    exists before `save_knowledge_file`/`delete_knowledge_file` discard it.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.captured_knowledge: dict[str, str] = {}

    def commit_push(self, workdir: Path, message: str) -> bool:
        knowledge_dir = workdir / "bundle" / "knowledge"
        if knowledge_dir.is_dir():
            self.captured_knowledge = {
                path.name: path.read_text() for path in knowledge_dir.glob("*.md")
            }
        return super().commit_push(workdir, message)


class _RaisingGitHub(FakeGitHub):
    """A GitHubClient whose every operation raises `ClientError` — the 502
    guard's fixture, modeling a transient GitHub outage/auth failure without
    a real subprocess. `clone` is the first call every one of read/save/
    delete makes, and `runs_for_commit` is delivery's only github call, so
    overriding just those two covers all four routes' github-side 502 path.
    """

    def clone(self, repo: str, dest: Path) -> None:
        raise ClientError("gh: rate limited")

    def runs_for_commit(self, repo: str, sha: str) -> list[dict]:
        raise ClientError("gh: rate limited")


class _RaisingKubectl(FakeKubectl):
    """A KubectlClient whose `deploy_image` raises `ClientError` — the
    delivery route's other 502 seam (only reached past a successful CI run,
    unlike the github-side fixture above)."""

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


def _make_client(session_factory, config, *, github=None, kubectl=None, cabal=None):
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
                cabal=cabal,
            ),
        )
    )


def _enabled_config(config):
    """Same switchboard-on config shape as test_web_teach.py's own
    `_enabled_config`: both org config keys set. Combined with the
    `CABAL_OPERATOR_TOKEN` env var (set per-test via monkeypatch) and a
    wired `clients.cabal`, this is what flips `cabal_enabled(cfg)` true."""
    return config.model_copy(
        update={
            "cabal_url": "http://cabal.internal",
            "agent_endpoint_template": "http://agent-{name}.default.svc.cluster.local",
        }
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


# --- GET /api/agents/{name}/knowledge -----------------------------------------------


def test_list_knowledge_returns_files_sorted_by_name(session_factory, config):
    github = FakeGitHub(knowledge_files={"b.md": "second", "a.md": "first"})
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}/knowledge")

    assert response.status_code == 200
    assert response.json() == {
        "files": [
            {"name": "a.md", "content": "first"},
            {"name": "b.md", "content": "second"},
        ]
    }


def test_list_knowledge_empty_when_no_knowledge_dir(session_factory, config):
    client = _make_client(session_factory, config)  # bare FakeGitHub: no knowledge tree
    _seed_agent(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}/knowledge")

    assert response.status_code == 200
    assert response.json() == {"files": []}


def test_list_knowledge_unknown_agent_returns_404(session_factory, config):
    client = _make_client(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}/knowledge")

    assert response.status_code == 404
    assert "error" in response.json()


@pytest.mark.parametrize("status", NON_READY_STATUSES)
def test_list_knowledge_non_ready_agent_returns_409(session_factory, config, status):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, status=status)

    response = client.get(f"/api/agents/{AGENT_NAME}/knowledge")

    assert response.status_code == 409
    assert response.json() == {"error": "agent is not ready for memory changes"}


def test_list_knowledge_client_error_returns_502(session_factory, config):
    client = _make_client(session_factory, config, github=_RaisingGitHub())
    _seed_agent(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}/knowledge")

    assert response.status_code == 502
    assert response.json() == {"error": "couldn't reach your agent's files. Try again."}


# --- PUT /api/agents/{name}/knowledge/{filename} ------------------------------------


def test_save_knowledge_creates_file_commits_and_returns_sha(session_factory, config):
    github = _CapturingGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.put(
        f"/api/agents/{AGENT_NAME}/knowledge/gotchas.md", json={"content": "new content"}
    )

    assert response.status_code == 200
    assert response.json() == {"sha": FAKE_LOCAL_HEAD_DEFAULT, "mode": "archive"}
    assert github.captured_knowledge == {"gotchas.md": "new content"}
    assert github.pushes[-1][1] == "kobo: update knowledge/gotchas.md"


def test_save_knowledge_overwrites_existing_file(session_factory, config):
    github = _CapturingGitHub(knowledge_files={"gotchas.md": "old content"})
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.put(
        f"/api/agents/{AGENT_NAME}/knowledge/gotchas.md", json={"content": "replaced"}
    )

    assert response.status_code == 200
    assert github.captured_knowledge == {"gotchas.md": "replaced"}


def test_save_knowledge_unknown_agent_returns_404(session_factory, config):
    client = _make_client(session_factory, config)

    response = client.put(
        f"/api/agents/{AGENT_NAME}/knowledge/gotchas.md", json={"content": "hi"}
    )

    assert response.status_code == 404
    assert "error" in response.json()


@pytest.mark.parametrize("status", NON_READY_STATUSES)
def test_save_knowledge_non_ready_agent_returns_409(session_factory, config, status):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, status=status)

    response = client.put(
        f"/api/agents/{AGENT_NAME}/knowledge/gotchas.md", json={"content": "hi"}
    )

    assert response.status_code == 409
    assert response.json() == {"error": "agent is not ready for memory changes"}


def test_save_knowledge_bad_filename_returns_422_with_reason(session_factory, config):
    github = FakeGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.put(
        f"/api/agents/{AGENT_NAME}/knowledge/gotchas.txt", json={"content": "hi"}
    )

    assert response.status_code == 422
    assert "invalid knowledge filename" in response.json()["error"]
    assert github.pushes == []


def test_save_knowledge_empty_content_returns_422_with_reason(session_factory, config):
    github = FakeGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.put(
        f"/api/agents/{AGENT_NAME}/knowledge/gotchas.md", json={"content": ""}
    )

    assert response.status_code == 422
    assert "empty" in response.json()["error"]
    assert github.pushes == []


def test_save_knowledge_oversize_content_returns_422_with_reason(session_factory, config):
    github = FakeGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.put(
        f"/api/agents/{AGENT_NAME}/knowledge/gotchas.md",
        json={"content": "a" * (KNOWLEDGE_MAX_BYTES + 1)},
    )

    assert response.status_code == 422
    assert "exceeds" in response.json()["error"]
    assert github.pushes == []


def test_save_knowledge_client_error_returns_502(session_factory, config):
    client = _make_client(session_factory, config, github=_RaisingGitHub())
    _seed_agent(session_factory, config)

    response = client.put(
        f"/api/agents/{AGENT_NAME}/knowledge/gotchas.md", json={"content": "hi"}
    )

    assert response.status_code == 502
    assert response.json() == {"error": "couldn't reach your agent's files. Try again."}


# --- DELETE /api/agents/{name}/knowledge/{filename} ---------------------------------


def test_delete_knowledge_removes_file_commits_and_returns_sha(session_factory, config):
    github = _CapturingGitHub(knowledge_files={"gotchas.md": "bye", "keep.md": "stay"})
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.delete(f"/api/agents/{AGENT_NAME}/knowledge/gotchas.md")

    assert response.status_code == 200
    assert response.json() == {"sha": FAKE_LOCAL_HEAD_DEFAULT, "mode": "archive"}
    assert github.captured_knowledge == {"keep.md": "stay"}
    assert github.pushes[-1][1] == "kobo: remove knowledge/gotchas.md"


def test_delete_knowledge_unknown_agent_returns_404(session_factory, config):
    client = _make_client(session_factory, config)

    response = client.delete(f"/api/agents/{AGENT_NAME}/knowledge/gotchas.md")

    assert response.status_code == 404
    assert "error" in response.json()


@pytest.mark.parametrize("status", NON_READY_STATUSES)
def test_delete_knowledge_non_ready_agent_returns_409(session_factory, config, status):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, status=status)

    response = client.delete(f"/api/agents/{AGENT_NAME}/knowledge/gotchas.md")

    assert response.status_code == 409
    assert response.json() == {"error": "agent is not ready for memory changes"}


def test_delete_knowledge_missing_file_returns_404_with_exact_body(session_factory, config):
    github = FakeGitHub(knowledge_files={"keep.md": "stay"})
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.delete(f"/api/agents/{AGENT_NAME}/knowledge/gotchas.md")

    assert response.status_code == 404
    assert response.json() == {"error": "no such knowledge file: 'gotchas.md'"}
    assert github.pushes == []


def test_delete_knowledge_bad_filename_returns_422_not_404(session_factory, config):
    """A malformed filename must be distinguished from a valid-but-missing
    one: `_validate_filename` raises before `delete_knowledge_file` ever
    reaches its own not-found check, so this must be the 422 the brief
    reserves for bad input, never the 404 reserved for "not there"."""
    github = FakeGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.delete(f"/api/agents/{AGENT_NAME}/knowledge/gotchas.txt")

    assert response.status_code == 422
    assert "invalid knowledge filename" in response.json()["error"]
    assert github.pushes == []


def test_delete_knowledge_client_error_returns_502(session_factory, config):
    client = _make_client(session_factory, config, github=_RaisingGitHub())
    _seed_agent(session_factory, config)

    response = client.delete(f"/api/agents/{AGENT_NAME}/knowledge/gotchas.md")

    assert response.status_code == 502
    assert response.json() == {"error": "couldn't reach your agent's files. Try again."}


# --- PUT/DELETE knowledge: switchboard relay, instant lane (fast-memory-lane,
# arc3c Task 3) -----------------------------------------------------------
#
# Knowledge edits now ride the same fast lane teach already proved
# (test_web_teach.py's own "instant lane" section): the write/delete relays
# through the switchboard first (system_task "knowledge") and only falls
# back to today's synchronous archive lane on ANY miss -- a CabalError, an
# httpx transport error, a malformed reply, or a well-formed reply whose
# task status isn't exactly "completed". Same
# `cabal_enabled(config) and clients.cabal is not None` gate as teach/chat.


def test_save_knowledge_instant_lane_relays_and_archives_in_background(
    session_factory, config, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    github = _CapturingGitHub()
    relay_result = {
        "status": {
            "state": "completed",
            "message": {"parts": [{"text": "learned, thanks"}]},
        }
    }
    fake_cabal = FakeCabal(relay_result=relay_result)
    client = _make_client(session_factory, cfg, github=github, cabal=fake_cabal)
    _seed_agent(session_factory, cfg)

    response = client.put(
        f"/api/agents/{AGENT_NAME}/knowledge/gotchas.md", json={"content": "new content"}
    )

    assert response.status_code == 200
    assert response.json() == {"mode": "instant"}

    # BackgroundTasks run after the response completes but before
    # TestClient.put() returns control here -- the archive commit has
    # already happened by this point. Critical 2 fix: the background
    # archive commit carries archive_only=True (the runtime already has
    # this content live -- rebuilding the image for it would be pure
    # downtime), so the commit message marks itself [skip ci], same
    # marker as kobo_api.intake.upload_intake_file's archive_only.
    assert github.captured_knowledge == {"gotchas.md": "new content"}
    assert github.pushes[-1][1] == "kobo: archive knowledge/gotchas.md [skip ci]"

    assert len(fake_cabal.relay_calls) == 1
    envelope = fake_cabal.relay_calls[0]["task_envelope"]
    assert envelope["metadata"] == {"system_task": "knowledge", "filename": "gotchas.md"}
    # Both lanes agree on content: the relayed text is byte-for-byte what the
    # background archive committed.
    assert (
        envelope["status"]["message"]["parts"][0]["text"]
        == github.captured_knowledge["gotchas.md"]
    )


def test_save_knowledge_falls_back_to_archive_when_relay_fails(
    session_factory, config, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    github = _CapturingGitHub()
    fake_cabal = FakeCabal(relay_error="switchboard unreachable")
    client = _make_client(session_factory, cfg, github=github, cabal=fake_cabal)
    _seed_agent(session_factory, cfg)

    response = client.put(
        f"/api/agents/{AGENT_NAME}/knowledge/gotchas.md", json={"content": "new content"}
    )

    assert response.status_code == 200
    assert response.json() == {"sha": SHA, "mode": "archive"}
    assert len(fake_cabal.relay_calls) == 1
    assert github.captured_knowledge == {"gotchas.md": "new content"}


def test_save_knowledge_falls_back_when_agent_reply_is_failed_task(
    session_factory, config, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    github = _CapturingGitHub()
    fake_cabal = FakeCabal(relay_result={"status": {"state": "failed"}})
    client = _make_client(session_factory, cfg, github=github, cabal=fake_cabal)
    _seed_agent(session_factory, cfg)

    response = client.put(
        f"/api/agents/{AGENT_NAME}/knowledge/gotchas.md", json={"content": "new content"}
    )

    assert response.status_code == 200
    assert response.json() == {"sha": SHA, "mode": "archive"}
    assert github.captured_knowledge == {"gotchas.md": "new content"}


def test_save_knowledge_without_cabal_is_archive_mode(session_factory, config):
    github = _CapturingGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.put(
        f"/api/agents/{AGENT_NAME}/knowledge/gotchas.md", json={"content": "new content"}
    )

    assert response.status_code == 200
    assert response.json() == {"sha": SHA, "mode": "archive"}


def test_save_knowledge_background_archive_failure_is_logged_not_surfaced(
    session_factory, config, monkeypatch, caplog
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    relay_result = {
        "status": {
            "state": "completed",
            "message": {"parts": [{"text": "learned, thanks"}]},
        }
    }
    fake_cabal = FakeCabal(relay_result=relay_result)
    # _RaisingGitHub's clone() raises ClientError -- the background archive
    # commit fails, but the instant-lane response has already gone out as
    # 200 by the time it runs.
    client = _make_client(session_factory, cfg, github=_RaisingGitHub(), cabal=fake_cabal)
    _seed_agent(session_factory, cfg)

    with caplog.at_level("WARNING"):
        response = client.put(
            f"/api/agents/{AGENT_NAME}/knowledge/gotchas.md", json={"content": "new content"}
        )

    assert response.status_code == 200
    assert response.json()["mode"] == "instant"
    assert "knowledge archive commit failed" in caplog.text


def test_delete_knowledge_instant_lane_relays_and_archives_in_background(
    session_factory, config, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    github = _CapturingGitHub(knowledge_files={"gotchas.md": "bye", "keep.md": "stay"})
    relay_result = {
        "status": {
            "state": "completed",
            "message": {"parts": [{"text": "forgotten, thanks"}]},
        }
    }
    fake_cabal = FakeCabal(relay_result=relay_result)
    client = _make_client(session_factory, cfg, github=github, cabal=fake_cabal)
    _seed_agent(session_factory, cfg)

    response = client.delete(f"/api/agents/{AGENT_NAME}/knowledge/gotchas.md")

    assert response.status_code == 200
    assert response.json() == {"mode": "instant"}

    # BackgroundTasks run after the response completes but before
    # TestClient.delete() returns control here -- the archive removal has
    # already happened by this point: after a fast-lane delete, the repo
    # file is gone (the runtime no longer recalling its facts is the
    # runtime's own already-tested territory, per the brief). Critical 2
    # fix: this background archive delete carries archive_only=True, same
    # skip-ci marker as the save side.
    assert github.captured_knowledge == {"keep.md": "stay"}
    assert github.pushes[-1][1] == "kobo: archive knowledge/gotchas.md [skip ci]"

    assert len(fake_cabal.relay_calls) == 1
    envelope = fake_cabal.relay_calls[0]["task_envelope"]
    # Pin the exact delete-flag key: DELETE_KEY="delete" on the runtime side
    # (daedalus.memory.teach.DELETE_KEY) -- a different spelling here would
    # silently fall back to a write instead of a delete.
    assert envelope["metadata"] == {
        "system_task": "knowledge",
        "filename": "gotchas.md",
        "delete": True,
    }


def test_delete_knowledge_falls_back_to_archive_when_relay_fails(
    session_factory, config, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    github = _CapturingGitHub(knowledge_files={"gotchas.md": "bye", "keep.md": "stay"})
    fake_cabal = FakeCabal(relay_error="switchboard unreachable")
    client = _make_client(session_factory, cfg, github=github, cabal=fake_cabal)
    _seed_agent(session_factory, cfg)

    response = client.delete(f"/api/agents/{AGENT_NAME}/knowledge/gotchas.md")

    assert response.status_code == 200
    assert response.json() == {"sha": SHA, "mode": "archive"}
    assert len(fake_cabal.relay_calls) == 1
    assert github.captured_knowledge == {"keep.md": "stay"}


def test_delete_knowledge_falls_back_when_agent_reply_is_failed_task(
    session_factory, config, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    github = _CapturingGitHub(knowledge_files={"gotchas.md": "bye", "keep.md": "stay"})
    fake_cabal = FakeCabal(relay_result={"status": {"state": "failed"}})
    client = _make_client(session_factory, cfg, github=github, cabal=fake_cabal)
    _seed_agent(session_factory, cfg)

    response = client.delete(f"/api/agents/{AGENT_NAME}/knowledge/gotchas.md")

    assert response.status_code == 200
    assert response.json() == {"sha": SHA, "mode": "archive"}
    assert github.captured_knowledge == {"keep.md": "stay"}


def test_delete_knowledge_without_cabal_is_archive_mode(session_factory, config):
    github = _CapturingGitHub(knowledge_files={"gotchas.md": "bye", "keep.md": "stay"})
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.delete(f"/api/agents/{AGENT_NAME}/knowledge/gotchas.md")

    assert response.status_code == 200
    assert response.json() == {"sha": SHA, "mode": "archive"}


def test_delete_knowledge_still_404s_on_missing_file_even_with_cabal_enabled(
    session_factory, config, monkeypatch
):
    # A relay against a filename the runtime has no ledger row for replies
    # with a failed task (see daedalus's apply_knowledge_delete), which
    # degrades to the archive lane exactly like any other miss -- the
    # archive lane's own not-found check is what actually produces the 404,
    # unchanged from today.
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    github = FakeGitHub(knowledge_files={"keep.md": "stay"})
    fake_cabal = FakeCabal(relay_result={"status": {"state": "failed"}})
    client = _make_client(session_factory, cfg, github=github, cabal=fake_cabal)
    _seed_agent(session_factory, cfg)

    response = client.delete(f"/api/agents/{AGENT_NAME}/knowledge/gotchas.md")

    assert response.status_code == 404
    assert response.json() == {"error": "no such knowledge file: 'gotchas.md'"}
    assert github.pushes == []


def test_delete_knowledge_background_archive_failure_is_logged_not_surfaced(
    session_factory, config, monkeypatch, caplog
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    relay_result = {
        "status": {
            "state": "completed",
            "message": {"parts": [{"text": "forgotten, thanks"}]},
        }
    }
    fake_cabal = FakeCabal(relay_result=relay_result)
    # _RaisingGitHub's clone() raises ClientError -- the background archive
    # delete fails, but the instant-lane response has already gone out as
    # 200 by the time it runs.
    client = _make_client(session_factory, cfg, github=_RaisingGitHub(), cabal=fake_cabal)
    _seed_agent(session_factory, cfg)

    with caplog.at_level("WARNING"):
        response = client.delete(f"/api/agents/{AGENT_NAME}/knowledge/gotchas.md")

    assert response.status_code == 200
    assert response.json()["mode"] == "instant"
    assert "knowledge archive delete failed" in caplog.text


# --- GET /api/agents/{name}/knowledge/delivery/{sha} --------------------------------


def test_delivery_building_when_no_runs_yet(session_factory, config):
    github = FakeGitHub(runs_by_sha={SHA: [[]]})
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}/knowledge/delivery/{SHA}")

    assert response.status_code == 200
    assert response.json() == {"state": "building", "detail": None}


def test_delivery_rolling_when_success_but_image_stale(session_factory, config):
    github = FakeGitHub(
        runs_by_sha={SHA: [[{"id": 1, "status": "completed", "conclusion": "success"}]]}
    )
    kubectl = FakeKubectl(images={(AGENT_NAME, AGENT_NAME): "gojira-app:stalestalestal"})
    client = _make_client(session_factory, config, github=github, kubectl=kubectl)
    _seed_agent(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}/knowledge/delivery/{SHA}")

    assert response.status_code == 200
    assert response.json() == {"state": "rolling", "detail": None}


def test_delivery_delivered_when_success_and_image_matches(session_factory, config):
    github = FakeGitHub(
        runs_by_sha={SHA: [[{"id": 1, "status": "completed", "conclusion": "success"}]]}
    )
    # bare FakeKubectl(): default image tag matches SHA's first 12 chars
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}/knowledge/delivery/{SHA}")

    assert response.status_code == 200
    assert response.json() == {"state": "delivered", "detail": None}


def test_delivery_failed_when_run_completed_non_success(session_factory, config):
    github = FakeGitHub(
        head_sha=SHA,  # main hasn't moved past it: a real failure, not a superseded one
        runs_by_sha={SHA: [[{"id": 1, "status": "completed", "conclusion": "failure"}]]},
    )
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}/knowledge/delivery/{SHA}")

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "failed"
    assert body["detail"]


def test_delivery_unknown_agent_returns_404(session_factory, config):
    client = _make_client(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}/knowledge/delivery/{SHA}")

    assert response.status_code == 404
    assert "error" in response.json()


@pytest.mark.parametrize("status", NON_READY_STATUSES)
def test_delivery_non_ready_agent_returns_409(session_factory, config, status):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, status=status)

    response = client.get(f"/api/agents/{AGENT_NAME}/knowledge/delivery/{SHA}")

    assert response.status_code == 409
    assert response.json() == {"error": "agent is not ready for memory changes"}


@pytest.mark.parametrize(
    "bad_sha",
    [
        "abc123",  # 6 hex chars: below the 7-char floor
        "not-hex!!",  # not hex at all, plus a forbidden char
        "ABCDEF12345",  # uppercase hex isn't accepted
        "a" * 41,  # over the 40-char ceiling
    ],
)
def test_delivery_malformed_sha_returns_422(session_factory, config, bad_sha):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}/knowledge/delivery/{bad_sha}")

    assert response.status_code == 422
    assert "error" in response.json()


def test_delivery_client_error_from_github_returns_502(session_factory, config):
    client = _make_client(session_factory, config, github=_RaisingGitHub())
    _seed_agent(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}/knowledge/delivery/{SHA}")

    assert response.status_code == 502
    assert response.json() == {"error": "couldn't check on the delivery. Try again."}


def test_delivery_client_error_from_kubectl_returns_502(session_factory, config):
    github = FakeGitHub(
        runs_by_sha={SHA: [[{"id": 1, "status": "completed", "conclusion": "success"}]]}
    )
    client = _make_client(session_factory, config, github=github, kubectl=_RaisingKubectl())
    _seed_agent(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}/knowledge/delivery/{SHA}")

    assert response.status_code == 502
    assert response.json() == {"error": "couldn't check on the delivery. Try again."}
