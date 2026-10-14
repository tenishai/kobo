"""Tests for the tooling HTTP routes (2026-08-13 addendum): upload/import/
list/delete an agent's staged `bundle/tools/*.{py,go}` law-to-be files — all
against the fakes (`tests/fakes.py`), no real git/kubectl involved.

Same harness shape as test_web_intake.py (own `session_factory`/`config`
fixtures, a `TestClient` built from `create_app`): agents are seeded
directly at whatever status a test wants, already carrying a
`repo_full_name`. Namespace/deploy for the delivered-derivation call are
both the agent's own name, same convention every sibling route uses.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.clients import ClientError, Clients
from kobo_api.config import Config
from kobo_api.main import create_app
from kobo_api.models import Agent, AgentStatus, Base
from kobo_api.tooling import TOOLING_MAX_BYTES
from tests.fakes import (
    FAKE_DOCKERFILE_OLD_IMAGE,
    FAKE_LOCAL_HEAD_DEFAULT,
    FakeAgent,
    FakeCabal,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
    FakeStorage,
)

AGENT_NAME = "clio"
PERSONA = "helpful-generalist"
REPO = "acme/agent-clio"
SOURCE_REPO = "acme/tools-library"

# Matches FakeKubectl's default deploy_image tag (FAKE_DEPLOY_IMAGE_DEFAULT's
# tag is FAKE_LOCAL_HEAD_DEFAULT's own first 12 chars) — the sha every
# mutating route below returns (github.local_head).
SHA = FAKE_LOCAL_HEAD_DEFAULT

# Carries a real entrypoint (2026-08-16 addendum: the import allowlist +
# entrypoint check) — every fixture below that stands in for "a valid
# tooling file" reuses this constant.
VALID_PY = "def post_draft(draft):\n    return draft\n"
BROKEN_PY = "x = 1\ny = 2\nz = )\n"


class _CapturingGitHub(FakeGitHub):
    """Same snapshot-on-commit_push trick as test_web_intake.py's own
    `_CapturingGitHub`, replicated locally for `bundle/tools/*`."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.captured_tools: dict[str, str] = {}
        self.captured_dockerfile = ""

    def commit_push(self, workdir: Path, message: str) -> bool:
        tools_dir = workdir / "bundle" / "tools"
        if tools_dir.is_dir():
            self.captured_tools = {
                path.name: path.read_text()
                for path in tools_dir.iterdir()
                if path.is_file()
            }
        self.captured_dockerfile = (workdir / "Dockerfile").read_text()
        return super().commit_push(workdir, message)


class _RaisingGitHub(FakeGitHub):
    """A GitHubClient whose `clone` raises `ClientError` — the 502 guard's
    fixture. `clone` is the first call every tooling.py operation makes, so
    overriding just it covers every route's github-side 502 path.

    `succeed_first_n_clones` (Important 7 fix follow-on), when > 0, lets the
    first N `clone()` calls succeed normally (delegating to FakeGitHub's
    own) before raising on every call after that -- needed by the
    background-archive-failure test below: the upload route's shadow-
    collision check (`existing_tool_filenames`) now clones once,
    synchronously, in the FOREGROUND before the fast lane even relays, so
    that clone must succeed for the instant-lane response to go out at all;
    it's the SEPARATE background-archive clone that test wants to fail.
    Default 0 preserves every other call site's original "clone always
    raises" behavior unchanged."""

    def __init__(self, *, succeed_first_n_clones: int = 0, **kwargs):
        super().__init__(**kwargs)
        self._succeed_first_n_clones = succeed_first_n_clones
        self._clone_calls = 0

    def clone(self, repo: str, dest: Path) -> None:
        self._clone_calls += 1
        if self._clone_calls <= self._succeed_first_n_clones:
            super().clone(repo, dest)
            return
        raise ClientError("gh: rate limited")


class _RaisingKubectl(FakeKubectl):
    """A KubectlClient whose `deploy_image` raises `ClientError` — the list
    route's other 502 seam (only reached once there's at least one tooling
    file to check delivery for)."""

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


def _make_client(session_factory, config, *, github=None, kubectl=None, agent=None, cabal=None):
    return TestClient(
        create_app(
            session_factory,
            config=config,
            clients=Clients(
                github=github if github is not None else FakeGitHub(),
                gojira=FakeGojira(),
                cloud=FakeCloud(),
                agent=agent if agent is not None else FakeAgent(),
                kubectl=kubectl if kubectl is not None else FakeKubectl(),
                storage=FakeStorage(),
                cabal=cabal,
            ),
        )
    )


def _enabled_config(config):
    """Same switchboard-on config shape as test_api_knowledge.py's own
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


# --- GET /api/agents/{name}/tooling -------------------------------------------

# Task 4 (card probe + banner graduation): the enforcement envelope a bare
# FakeAgent() (no card configured — models a pre-enforcement runtime) reads
# as, on every route below that doesn't care about enforcement itself.
UNATTESTED = {"attested": False, "locked": False, "locked_file": None}


def test_list_tooling_route_empty(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}/tooling")

    assert response.status_code == 200
    assert response.json() == {"files": [], "enforcement": UNATTESTED}


def test_list_tooling_route_returns_files_with_delivered(session_factory, config):
    # Bare fakes wired together: an unconfigured tooling file reads
    # "delivered" with no extra setup (same convention as list_intake), and
    # an unconfigured agent card reads "unattested" — no "status" key is
    # added to a file's dict unless attested-and-not-locked (see the
    # dedicated enforcement section below).
    github = FakeGitHub(tooling_files={"orchestrator.py": VALID_PY})
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}/tooling")

    assert response.status_code == 200
    assert response.json() == {
        "files": [
            {
                "name": "orchestrator.py",
                "content_bytes": len(VALID_PY.encode()),
                "delivered": True,
            }
        ],
        "enforcement": UNATTESTED,
    }


def test_list_tooling_route_unknown_agent_returns_404(session_factory, config):
    client = _make_client(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}/tooling")

    assert response.status_code == 404
    assert "error" in response.json()


@pytest.mark.parametrize("status", NON_READY_STATUSES)
def test_list_tooling_route_non_ready_agent_returns_409(session_factory, config, status):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, status=status)

    response = client.get(f"/api/agents/{AGENT_NAME}/tooling")

    assert response.status_code == 409
    assert response.json() == {"error": "agent is not ready for memory changes"}


def test_list_tooling_route_github_error_returns_502(session_factory, config):
    client = _make_client(session_factory, config, github=_RaisingGitHub())
    _seed_agent(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}/tooling")

    assert response.status_code == 502
    assert response.json() == {"error": "couldn't reach your agent's files. Try again."}


def test_list_tooling_route_kubectl_error_returns_502(session_factory, config):
    github = FakeGitHub(tooling_files={"tool.py": VALID_PY})
    client = _make_client(session_factory, config, github=github, kubectl=_RaisingKubectl())
    _seed_agent(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}/tooling")

    assert response.status_code == 502
    assert response.json() == {"error": "couldn't reach your agent's files. Try again."}


# --- GET /api/agents/{name}/tooling: enforcement graduation (Task 4) ---------
#
# One delivered file (old.py) and one pending file (fresh.py) — mirrors
# test_tooling.py's own mixed-delivery fixture — held FIXED across every
# card scenario below, proving delivery truth and enforcement status are
# independent axes that never interfere with each other.

OLDER_SHA = "oldoldoldoldoldoldoldoldoldoldoldoldoldo"
NEWER_SHA = "newnewnewnewnewnewnewnewnewnewnewnewnewn"


def _mixed_delivery_github_and_kubectl():
    github = FakeGitHub(
        tooling_files={"old.py": "stale = 1\n", "fresh.py": "brand_new = 1\n"},
        intake_commit_shas={
            "bundle/tools/old.py": OLDER_SHA,
            "bundle/tools/fresh.py": NEWER_SHA,
        },
        rev_parse_results={"oldoldoldold": OLDER_SHA},
    )
    kubectl = FakeKubectl(images={(AGENT_NAME, AGENT_NAME): f"gojira-app:{OLDER_SHA[:12]}"})
    return github, kubectl


def test_list_tooling_route_attested_not_locked_marks_only_delivered_files_enforced(
    session_factory, config
):
    github, kubectl = _mixed_delivery_github_and_kubectl()
    agent = FakeAgent(card={"enforcement": {"version": 1, "hooks": ["old.py"], "locked": False}})
    client = _make_client(session_factory, config, github=github, kubectl=kubectl, agent=agent)
    _seed_agent(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}/tooling")

    assert response.status_code == 200
    body = response.json()
    assert body["enforcement"] == {"attested": True, "locked": False, "locked_file": None}
    files_by_name = {f["name"]: f for f in body["files"]}
    assert files_by_name["old.py"] == {
        "name": "old.py",
        "content_bytes": len(b"stale = 1\n"),
        "delivered": True,
        "status": "enforced",
    }
    # Pending stays pending — delivery truth unchanged, and no "status" key
    # at all (not even "pending") for a file that isn't delivered yet.
    assert files_by_name["fresh.py"] == {
        "name": "fresh.py",
        "content_bytes": len(b"brand_new = 1\n"),
        "delivered": False,
    }


def test_list_tooling_route_attested_and_locked_never_marks_enforced(session_factory, config):
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
    client = _make_client(session_factory, config, github=github, kubectl=kubectl, agent=agent)
    _seed_agent(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}/tooling")

    assert response.status_code == 200
    body = response.json()
    assert body["enforcement"] == {"attested": True, "locked": True, "locked_file": "old.py"}
    # Locked means serving refusals, not real enforcement — even the
    # delivered file must NOT be labeled "enforced".
    for file in body["files"]:
        assert "status" not in file
    files_by_name = {f["name"]: f["delivered"] for f in body["files"]}
    assert files_by_name == {"old.py": True, "fresh.py": False}


def test_list_tooling_route_unattested_never_marks_enforced_even_when_delivered(
    session_factory, config
):
    github, kubectl = _mixed_delivery_github_and_kubectl()
    agent = FakeAgent()  # bare: pre-enforcement runtime, no card enforcement block
    client = _make_client(session_factory, config, github=github, kubectl=kubectl, agent=agent)
    _seed_agent(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}/tooling")

    assert response.status_code == 200
    body = response.json()
    assert body["enforcement"] == UNATTESTED
    for file in body["files"]:
        assert "status" not in file


def test_list_tooling_route_unreachable_agent_card_reads_unattested(session_factory, config):
    github, kubectl = _mixed_delivery_github_and_kubectl()
    agent = FakeAgent(card_error="fetch_card request failed: connection refused")
    client = _make_client(session_factory, config, github=github, kubectl=kubectl, agent=agent)
    _seed_agent(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}/tooling")

    assert response.status_code == 200
    body = response.json()
    # Byte-identical to today's (pre-Task-4) response, modulo the added
    # enforcement envelope — an unreachable agent card never changes a
    # file's delivered-derived truth, and never marks anything "enforced".
    assert body == {
        "files": [
            {"name": "fresh.py", "content_bytes": len(b"brand_new = 1\n"), "delivered": False},
            {"name": "old.py", "content_bytes": len(b"stale = 1\n"), "delivered": True},
        ],
        "enforcement": UNATTESTED,
    }


# --- POST /api/agents/{name}/tooling ------------------------------------------


def test_upload_tooling_route_creates_commits_and_returns_sha(session_factory, config):
    github = _CapturingGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/tooling",
        json={"filename": "orchestrator.py", "content": VALID_PY},
    )

    assert response.status_code == 200
    assert response.json() == {"sha": SHA, "mode": "archive"}
    assert github.captured_tools == {"orchestrator.py": VALID_PY}
    subject, _, note = github.pushes[-1][1].partition("\n\n")
    assert subject == "kobo: add tools/orchestrator.py"
    # The fake clone's Dockerfile is on an old image, so the commit re-pins
    # it and says so (issue #140).
    assert note.startswith("Also re-pins the Dockerfile FROM from ")


def test_upload_tooling_route_rewrites_dockerfile_to_configured_runtime_image(
    session_factory, config
):
    assert FAKE_DOCKERFILE_OLD_IMAGE != config.runtime_image

    github = _CapturingGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/tooling",
        json={"filename": "orchestrator.py", "content": VALID_PY},
    )

    assert response.status_code == 200
    assert github.captured_tools == {"orchestrator.py": VALID_PY}
    assert github.captured_dockerfile == (
        "# Agent image: the shared runtime plus this agent's bundle.\n"
        f"FROM {config.runtime_image}\n"
        "COPY bundle/ /bundle\n"
    )


def test_upload_tooling_route_accepts_go_extension(session_factory, config):
    github = _CapturingGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/tooling",
        json={"filename": "guard.go", "content": "package main\n"},
    )

    assert response.status_code == 200
    assert github.captured_tools == {"guard.go": "package main\n"}


def test_upload_tooling_route_invalid_filename_returns_422(session_factory, config):
    github = _CapturingGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/tooling",
        json={"filename": "Not-Valid.py", "content": VALID_PY},
    )

    assert response.status_code == 422
    assert "invalid tooling filename" in response.json()["error"]
    assert github.pushes == []


def test_upload_tooling_route_empty_content_returns_422(session_factory, config):
    github = _CapturingGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/tooling", json={"filename": "tool.py", "content": ""}
    )

    assert response.status_code == 422
    assert "empty" in response.json()["error"]
    assert github.pushes == []


def test_upload_tooling_route_oversize_content_returns_422(session_factory, config):
    github = _CapturingGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    oversize = '"' + "a" * (TOOLING_MAX_BYTES + 1) + '"\n'
    response = client.post(
        f"/api/agents/{AGENT_NAME}/tooling",
        json={"filename": "tool.py", "content": oversize},
    )

    assert response.status_code == 422
    assert "exceeds" in response.json()["error"]
    assert github.pushes == []


def test_upload_tooling_route_python_syntax_error_returns_422_with_line_number(
    session_factory, config
):
    github = _CapturingGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/tooling",
        json={"filename": "tool.py", "content": BROKEN_PY},
    )

    assert response.status_code == 422
    assert response.json()["error"].startswith("tooling file has a syntax error on line 3:")
    assert github.pushes == []


def test_upload_tooling_route_non_stdlib_import_returns_422(session_factory, config):
    github = _CapturingGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/tooling",
        json={
            "filename": "tool.py",
            "content": "import httpx\n\n\ndef post_draft(draft):\n    return draft\n",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"] == (
        "tooling file imports beyond the standard library on line 1: httpx"
    )
    assert github.pushes == []


def test_upload_tooling_route_relative_import_returns_422(session_factory, config):
    github = _CapturingGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/tooling",
        json={
            "filename": "tool.py",
            "content": "from . import helper\n\n\ndef post_draft(draft):\n    return draft\n",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"] == "tooling file uses a relative import on line 1"
    assert github.pushes == []


def test_upload_tooling_route_async_hook_returns_422(session_factory, config):
    github = _CapturingGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/tooling",
        json={"filename": "tool.py", "content": "async def pre_task(task):\n    return task\n"},
    )

    assert response.status_code == 422
    assert response.json()["error"] == (
        "tooling file hooks must be regular functions, not async"
    )
    assert github.pushes == []


def test_upload_tooling_route_missing_entrypoint_returns_422(session_factory, config):
    github = _CapturingGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/tooling",
        json={"filename": "tool.py", "content": "def helper():\n    return 1\n"},
    )

    assert response.status_code == 422
    assert response.json()["error"] == "tooling file defines neither pre_task nor post_draft"
    assert github.pushes == []


def test_upload_tooling_route_client_error_returns_502(session_factory, config):
    client = _make_client(session_factory, config, github=_RaisingGitHub())
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/tooling",
        json={"filename": "tool.py", "content": VALID_PY},
    )

    assert response.status_code == 502
    assert response.json() == {"error": "couldn't reach your agent's files. Try again."}


# --- POST /api/agents/{name}/tooling: fast lane (arc3d Task 5) ---------------
#
# Tools ride the same instant-lane/archive-lane shape teach/knowledge/intake
# already proved, with two wrinkles the brief calls out: the response tells
# the caller the tool only arrives on restart (no hot reload), and a
# successful instant delivery also requests exactly one rollout restart so
# the enforcement loader's next boot actually picks the file up.


def test_upload_tooling_route_instant_lane_relays_archives_and_restarts_once(
    session_factory, config, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    github = _CapturingGitHub()
    kubectl = FakeKubectl()
    relay_result = {
        "status": {
            "state": "completed",
            "message": {"parts": [{"text": "kept, thanks"}]},
        }
    }
    fake_cabal = FakeCabal(relay_result=relay_result)
    client = _make_client(session_factory, cfg, github=github, kubectl=kubectl, cabal=fake_cabal)
    _seed_agent(session_factory, cfg)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/tooling",
        json={"filename": "orchestrator.py", "content": VALID_PY},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "instant"
    # Plain copy, not jargon: the UI can tell the caller the truth honestly.
    assert "restart" in body["message"].lower()

    # BackgroundTasks run after the response completes but before
    # TestClient.post() returns control here.
    assert github.captured_tools == {"orchestrator.py": VALID_PY}
    subject, _, note = github.pushes[-1][1].partition("\n\n")
    assert subject == "kobo: add tools/orchestrator.py"
    # The fake clone's Dockerfile is on an old image, so the commit re-pins
    # it and says so (issue #140).
    assert note.startswith("Also re-pins the Dockerfile FROM from ")

    assert len(fake_cabal.relay_calls) == 1
    envelope = fake_cabal.relay_calls[0]["task_envelope"]
    assert envelope["metadata"] == {"system_task": "tool", "filename": "orchestrator.py"}
    assert envelope["status"]["message"]["parts"][0]["text"] == VALID_PY

    # Exactly one restart requested, for this one delivery.
    assert kubectl.restarts == [(AGENT_NAME, AGENT_NAME)]


def test_upload_tooling_route_falls_back_to_archive_when_relay_fails(
    session_factory, config, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    github = _CapturingGitHub()
    kubectl = FakeKubectl()
    fake_cabal = FakeCabal(relay_error="switchboard unreachable")
    client = _make_client(session_factory, cfg, github=github, kubectl=kubectl, cabal=fake_cabal)
    _seed_agent(session_factory, cfg)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/tooling",
        json={"filename": "orchestrator.py", "content": VALID_PY},
    )

    assert response.status_code == 200
    assert response.json() == {"sha": SHA, "mode": "archive"}
    assert len(fake_cabal.relay_calls) == 1
    assert github.captured_tools == {"orchestrator.py": VALID_PY}
    # The archive lane's own rebuild is what eventually loads the tool here
    # -- no separate rollout-restart nudge is needed or requested.
    assert kubectl.restarts == []


def test_upload_tooling_route_falls_back_when_agent_reply_is_failed_task(
    session_factory, config, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    github = _CapturingGitHub()
    kubectl = FakeKubectl()
    fake_cabal = FakeCabal(relay_result={"status": {"state": "failed"}})
    client = _make_client(session_factory, cfg, github=github, kubectl=kubectl, cabal=fake_cabal)
    _seed_agent(session_factory, cfg)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/tooling",
        json={"filename": "orchestrator.py", "content": VALID_PY},
    )

    assert response.status_code == 200
    assert response.json() == {"sha": SHA, "mode": "archive"}
    assert kubectl.restarts == []


def test_upload_tooling_route_invalid_content_refused_before_any_relay(
    session_factory, config, monkeypatch
):
    """The security-adjacent kobo-side test: a tool failing the room's own
    validation must never even reach the switchboard -- refused before any
    delivery is attempted, cabal wired or not."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    github = _CapturingGitHub()
    kubectl = FakeKubectl()
    fake_cabal = FakeCabal()
    client = _make_client(session_factory, cfg, github=github, kubectl=kubectl, cabal=fake_cabal)
    _seed_agent(session_factory, cfg)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/tooling",
        json={
            "filename": "tool.py",
            "content": "import httpx\n\n\ndef post_draft(draft):\n    return draft\n",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"] == (
        "tooling file imports beyond the standard library on line 1: httpx"
    )
    assert fake_cabal.relay_calls == []
    assert github.pushes == []
    assert kubectl.restarts == []


def test_upload_tooling_route_bad_filename_refused_before_any_relay(
    session_factory, config, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    fake_cabal = FakeCabal()
    client = _make_client(session_factory, cfg, cabal=fake_cabal)
    _seed_agent(session_factory, cfg)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/tooling",
        json={"filename": "Not-Valid.py", "content": VALID_PY},
    )

    assert response.status_code == 422
    assert "invalid tooling filename" in response.json()["error"]
    assert fake_cabal.relay_calls == []


def test_upload_tooling_route_baseline_prefixed_filename_refused_before_any_relay(
    session_factory, config, monkeypatch
):
    """Important 7 fix (kobo-side shadowing refusal): the `baseline-*`
    namespace is reserved for the marketplace's baseline tooling pack
    (`kobo_api.marketplace.BASELINE_TOOL_PREFIX`, staged into every
    non-opted-out agent's own `bundle/tools/` at compose time) -- refused
    before any relay or archive write, cabal wired or not, so an upload can
    never silently overwrite a baseline hook file (the fallback-overwrites-
    hook hole this closes: a collision never even reaches the archive
    lane)."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    github = _CapturingGitHub()
    fake_cabal = FakeCabal()
    client = _make_client(session_factory, cfg, github=github, cabal=fake_cabal)
    _seed_agent(session_factory, cfg)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/tooling",
        json={"filename": "baseline-guardrail.py", "content": VALID_PY},
    )

    assert response.status_code == 422
    assert "baseline" in response.json()["error"].lower()
    assert fake_cabal.relay_calls == []
    assert github.pushes == []


def test_upload_tooling_route_existing_bundle_filename_refused_before_any_relay(
    session_factory, config, monkeypatch
):
    """Same refusal, the other collision source named by the fix: a
    filename that already names a file in THIS agent's own composed
    `bundle/tools/` -- whether that file arrived via the baseline pack or
    an earlier upload -- must never be silently overwritten either."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    github = _CapturingGitHub(tooling_files={"orchestrator.py": VALID_PY})
    fake_cabal = FakeCabal()
    client = _make_client(session_factory, cfg, github=github, cabal=fake_cabal)
    _seed_agent(session_factory, cfg)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/tooling",
        json={"filename": "orchestrator.py", "content": VALID_PY},
    )

    assert response.status_code == 422
    assert "orchestrator.py" in response.json()["error"]
    assert fake_cabal.relay_calls == []
    # Only the read-only listing clone happened -- no write was ever
    # attempted against the collision.
    assert github.pushes == []


def test_upload_tooling_route_non_colliding_filename_still_proceeds(
    session_factory, config, monkeypatch
):
    """Regression guard: the collision check must not over-refuse -- an
    upload naming a file that ISN'T already in the bundle, alongside other
    files that already are, still proceeds normally."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    github = _CapturingGitHub(tooling_files={"baseline-guard.py": VALID_PY})
    fake_cabal = FakeCabal()
    client = _make_client(session_factory, cfg, github=github, cabal=fake_cabal)
    _seed_agent(session_factory, cfg)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/tooling",
        json={"filename": "orchestrator.py", "content": VALID_PY},
    )

    assert response.status_code == 200
    assert github.captured_tools == {
        "baseline-guard.py": VALID_PY,
        "orchestrator.py": VALID_PY,
    }


def test_upload_tooling_route_without_cabal_is_archive_mode(session_factory, config):
    github = _CapturingGitHub()
    kubectl = FakeKubectl()
    client = _make_client(session_factory, config, github=github, kubectl=kubectl)
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/tooling",
        json={"filename": "orchestrator.py", "content": VALID_PY},
    )

    assert response.status_code == 200
    assert response.json() == {"sha": SHA, "mode": "archive"}
    assert kubectl.restarts == []


def test_upload_tooling_route_background_archive_failure_is_logged_not_surfaced(
    session_factory, config, monkeypatch, caplog
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    relay_result = {
        "status": {
            "state": "completed",
            "message": {"parts": [{"text": "kept, thanks"}]},
        }
    }
    fake_cabal = FakeCabal(relay_result=relay_result)
    # succeed_first_n_clones=1: the shadow-collision check's own foreground
    # clone succeeds (so the instant-lane relay is even reached), but the
    # SEPARATE background archive clone that follows still raises
    # ClientError -- the commit fails, but the instant-lane response has
    # already gone out.
    github = _RaisingGitHub(succeed_first_n_clones=1)
    client = _make_client(session_factory, cfg, github=github, cabal=fake_cabal)
    _seed_agent(session_factory, cfg)

    with caplog.at_level("WARNING"):
        response = client.post(
            f"/api/agents/{AGENT_NAME}/tooling",
            json={"filename": "orchestrator.py", "content": VALID_PY},
        )

    assert response.status_code == 200
    assert response.json()["mode"] == "instant"
    assert "tooling archive commit failed" in caplog.text


def test_upload_tooling_route_restart_failure_is_logged_not_surfaced(
    session_factory, config, monkeypatch, caplog
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    github = _CapturingGitHub()
    kubectl = FakeKubectl(restart_error="deployments.apps \"clio\" not found")
    relay_result = {
        "status": {
            "state": "completed",
            "message": {"parts": [{"text": "kept, thanks"}]},
        }
    }
    fake_cabal = FakeCabal(relay_result=relay_result)
    client = _make_client(session_factory, cfg, github=github, kubectl=kubectl, cabal=fake_cabal)
    _seed_agent(session_factory, cfg)

    with caplog.at_level("WARNING"):
        response = client.post(
            f"/api/agents/{AGENT_NAME}/tooling",
            json={"filename": "orchestrator.py", "content": VALID_PY},
        )

    assert response.status_code == 200
    assert response.json()["mode"] == "instant"
    assert "restart" in caplog.text.lower()


# --- POST /api/agents/{name}/tooling/import -----------------------------------


def test_import_tooling_route_all_pass_returns_sha_and_imported(session_factory, config):
    github = _CapturingGitHub(
        tooling_source_files={SOURCE_REPO: {"tools/good.py": VALID_PY}}
    )
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/tooling/import",
        json={"repo": SOURCE_REPO, "path": "tools"},
    )

    assert response.status_code == 200
    assert response.json() == {"sha": SHA, "imported": ["good.py"], "rejected": []}
    assert github.captured_tools == {"good.py": VALID_PY}
    subject, _, note = github.pushes[-1][1].partition("\n\n")
    assert subject == f"kobo: import tools from {SOURCE_REPO}/tools"
    # The fake clone's Dockerfile is on an old image, so the commit re-pins
    # it and says so (issue #140).
    assert note.startswith("Also re-pins the Dockerfile FROM from ")


def test_import_tooling_route_partial_pass_returns_both_lists(session_factory, config):
    github = _CapturingGitHub(
        tooling_source_files={
            SOURCE_REPO: {"tools/good.py": VALID_PY, "tools/broken.py": BROKEN_PY}
        }
    )
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/tooling/import",
        json={"repo": SOURCE_REPO, "path": "tools"},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["sha"] == SHA
    assert body["imported"] == ["good.py"]
    assert body["rejected"] == [
        {
            "name": "broken.py",
            "reason": "tooling file has a syntax error on line 3: unmatched ')'",
        }
    ]


def test_import_tooling_route_none_pass_returns_null_sha(session_factory, config):
    github = _CapturingGitHub(
        tooling_source_files={SOURCE_REPO: {"tools/broken.py": BROKEN_PY}}
    )
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/tooling/import",
        json={"repo": SOURCE_REPO, "path": "tools"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "sha": None,
        "imported": [],
        "rejected": [
            {
                "name": "broken.py",
                "reason": "tooling file has a syntax error on line 3: unmatched ')'",
            }
        ],
    }
    assert github.pushes == []


def test_import_tooling_route_rewrites_dockerfile_to_configured_runtime_image(
    session_factory, config
):
    assert FAKE_DOCKERFILE_OLD_IMAGE != config.runtime_image

    github = _CapturingGitHub(
        tooling_source_files={SOURCE_REPO: {"tools/good.py": VALID_PY}}
    )
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    client.post(
        f"/api/agents/{AGENT_NAME}/tooling/import",
        json={"repo": SOURCE_REPO, "path": "tools"},
    )

    assert github.captured_dockerfile == (
        "# Agent image: the shared runtime plus this agent's bundle.\n"
        f"FROM {config.runtime_image}\n"
        "COPY bundle/ /bundle\n"
    )


def test_import_tooling_route_invalid_source_repo_returns_422(session_factory, config):
    github = _CapturingGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/tooling/import",
        json={"repo": "not-a-valid-repo", "path": "tools"},
    )

    assert response.status_code == 422
    assert "invalid source repo" in response.json()["error"]
    assert github.cloned == []


def test_import_tooling_route_traversal_source_path_returns_422(session_factory, config):
    github = _CapturingGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/tooling/import",
        json={"repo": SOURCE_REPO, "path": "../escape"},
    )

    assert response.status_code == 422
    assert "invalid source path" in response.json()["error"]
    assert github.cloned == []


def test_import_tooling_route_nonexistent_source_path_returns_404(session_factory, config):
    github = _CapturingGitHub(
        tooling_source_files={SOURCE_REPO: {"other/thing.py": VALID_PY}}
    )
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/tooling/import",
        json={"repo": SOURCE_REPO, "path": "tools"},
    )

    assert response.status_code == 404
    assert "tooling source path not found" in response.json()["error"]
    assert github.pushes == []


def test_import_tooling_route_client_error_returns_502(session_factory, config):
    # _RaisingGitHub.clone raises for ANY repo, including the source clone —
    # import_tooling's own first call.
    client = _make_client(session_factory, config, github=_RaisingGitHub())
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/tooling/import",
        json={"repo": SOURCE_REPO, "path": "tools"},
    )

    assert response.status_code == 502
    assert response.json() == {"error": "couldn't reach your agent's files. Try again."}


# --- DELETE /api/agents/{name}/tooling/{filename} -----------------------------


def test_delete_tooling_route_removes_and_returns_sha(session_factory, config):
    github = _CapturingGitHub(tooling_files={"tool.py": VALID_PY, "keep.go": "package main\n"})
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.delete(f"/api/agents/{AGENT_NAME}/tooling/tool.py")

    assert response.status_code == 200
    assert response.json() == {"sha": SHA}
    assert github.captured_tools == {"keep.go": "package main\n"}
    assert github.pushes[-1][1] == "kobo: remove tools/tool.py"


def test_delete_tooling_route_not_found_returns_404(session_factory, config):
    github = FakeGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.delete(f"/api/agents/{AGENT_NAME}/tooling/ghost.py")

    assert response.status_code == 404
    assert "error" in response.json()
    assert github.pushes == []


def test_delete_tooling_route_client_error_returns_502(session_factory, config):
    client = _make_client(session_factory, config, github=_RaisingGitHub())
    _seed_agent(session_factory, config)

    response = client.delete(f"/api/agents/{AGENT_NAME}/tooling/tool.py")

    assert response.status_code == 502
    assert response.json() == {"error": "couldn't reach your agent's files. Try again."}


# --- a fast-lane re-pin re-registers the card (issue #147) ---------------------


def _instant_client(session_factory, config, monkeypatch, *, dockerfile_image):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    github = _CapturingGitHub(dockerfile_image=dockerfile_image)
    fake_cabal = FakeCabal(relay_result={"status": {"state": "completed"}})
    client = _make_client(
        session_factory, cfg, github=github, kubectl=FakeKubectl(), cabal=fake_cabal
    )
    _seed_agent(session_factory, cfg)
    return client, fake_cabal


def test_tool_upload_that_repins_from_reregisters_the_card(
    session_factory, config, monkeypatch
):
    """The agent row has no runtime of its own (most agents), the clone is on
    an older image, so the archive commit moves FROM to the room's runtime.
    The card in that commit names the new runtime, so the switchboard is
    sent it."""
    client, fake_cabal = _instant_client(
        session_factory, config, monkeypatch, dockerfile_image=FAKE_DOCKERFILE_OLD_IMAGE
    )

    response = client.post(
        f"/api/agents/{AGENT_NAME}/tooling",
        json={"filename": "orchestrator.py", "content": VALID_PY},
    )

    assert response.status_code == 200
    assert [call["name"] for call in fake_cabal.graduate_calls] == [AGENT_NAME]


def test_tool_upload_without_a_repin_leaves_the_directory_alone(
    session_factory, config, monkeypatch
):
    client, fake_cabal = _instant_client(
        session_factory, config, monkeypatch, dockerfile_image=config.runtime_image
    )

    response = client.post(
        f"/api/agents/{AGENT_NAME}/tooling",
        json={"filename": "orchestrator.py", "content": VALID_PY},
    )

    assert response.status_code == 200
    assert fake_cabal.graduate_calls == []
