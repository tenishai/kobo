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
    overriding just it covers every route's github-side 502 path."""

    def clone(self, repo: str, dest: Path) -> None:
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


# --- GET /api/agents/{name}/tooling -------------------------------------------


def test_list_tooling_route_empty(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}/tooling")

    assert response.status_code == 200
    assert response.json() == {"files": []}


def test_list_tooling_route_returns_files_with_delivered(session_factory, config):
    # Bare fakes wired together: an unconfigured tooling file reads
    # "delivered" with no extra setup (same convention as list_intake).
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
        ]
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
    assert response.json() == {"error": "couldn't reach your agent's files — try again"}


def test_list_tooling_route_kubectl_error_returns_502(session_factory, config):
    github = FakeGitHub(tooling_files={"tool.py": VALID_PY})
    client = _make_client(session_factory, config, github=github, kubectl=_RaisingKubectl())
    _seed_agent(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}/tooling")

    assert response.status_code == 502
    assert response.json() == {"error": "couldn't reach your agent's files — try again"}


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
    assert response.json() == {"sha": SHA}
    assert github.captured_tools == {"orchestrator.py": VALID_PY}
    assert github.pushes[-1][1] == "kobo: add tools/orchestrator.py"


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


def test_upload_tooling_route_unknown_agent_returns_404(session_factory, config):
    client = _make_client(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/tooling",
        json={"filename": "tool.py", "content": VALID_PY},
    )

    assert response.status_code == 404
    assert "error" in response.json()


@pytest.mark.parametrize("status", NON_READY_STATUSES)
def test_upload_tooling_route_non_ready_agent_returns_409(session_factory, config, status):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, status=status)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/tooling",
        json={"filename": "tool.py", "content": VALID_PY},
    )

    assert response.status_code == 409
    assert response.json() == {"error": "agent is not ready for memory changes"}


def test_upload_tooling_route_client_error_returns_502(session_factory, config):
    client = _make_client(session_factory, config, github=_RaisingGitHub())
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/tooling",
        json={"filename": "tool.py", "content": VALID_PY},
    )

    assert response.status_code == 502
    assert response.json() == {"error": "couldn't reach your agent's files — try again"}


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
    assert github.pushes[-1][1] == f"kobo: import tools from {SOURCE_REPO}/tools"


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


def test_import_tooling_route_unknown_agent_returns_404(session_factory, config):
    client = _make_client(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/tooling/import",
        json={"repo": SOURCE_REPO, "path": "tools"},
    )

    assert response.status_code == 404
    assert "error" in response.json()


@pytest.mark.parametrize("status", NON_READY_STATUSES)
def test_import_tooling_route_non_ready_agent_returns_409(session_factory, config, status):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, status=status)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/tooling/import",
        json={"repo": SOURCE_REPO, "path": "tools"},
    )

    assert response.status_code == 409
    assert response.json() == {"error": "agent is not ready for memory changes"}


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
    assert response.json() == {"error": "couldn't reach your agent's files — try again"}


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


def test_delete_tooling_route_unknown_agent_returns_404(session_factory, config):
    client = _make_client(session_factory, config)

    response = client.delete(f"/api/agents/{AGENT_NAME}/tooling/tool.py")

    assert response.status_code == 404
    assert "error" in response.json()


@pytest.mark.parametrize("status", NON_READY_STATUSES)
def test_delete_tooling_route_non_ready_agent_returns_409(session_factory, config, status):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, status=status)

    response = client.delete(f"/api/agents/{AGENT_NAME}/tooling/tool.py")

    assert response.status_code == 409
    assert response.json() == {"error": "agent is not ready for memory changes"}


def test_delete_tooling_route_client_error_returns_502(session_factory, config):
    client = _make_client(session_factory, config, github=_RaisingGitHub())
    _seed_agent(session_factory, config)

    response = client.delete(f"/api/agents/{AGENT_NAME}/tooling/tool.py")

    assert response.status_code == 502
    assert response.json() == {"error": "couldn't reach your agent's files — try again"}
