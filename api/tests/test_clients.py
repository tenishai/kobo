"""Tests for cortex_api.clients: real-client argv/request construction (via a
RecordingRunner and httpx.MockTransport — no real subprocesses or network),
plus structural checks that the fakes satisfy the Protocols.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from cortex_api.clients import (
    AgentClient,
    ClientError,
    Clients,
    CloudClient,
    GcloudClient,
    GhCLI,
    GitHubClient,
    GojiraClient,
    GojiraConflict,
    HttpxGojiraClient,
    KubectlAgentClient,
    KubectlCLI,
    KubectlClient,
    _build_hello_envelope,
    _post_hello,
    _wait_for_forwarded_port,
)
from cortex_api.config import Config
from tests.fakes import FakeAgent, FakeCloud, FakeGitHub, FakeGojira, FakeKubectl


class RecordingRunner:
    """Test double for CommandRunner: records exact argv for every call and
    replays canned (returncode, stdout, stderr) results in order (or reuses a
    single configured result for every call). Raises ClientError on rc != 0,
    exactly like the real CommandRunner, so real client branching logic (e.g.
    ensure_gsa's describe-then-create) exercises for real against it.
    """

    def __init__(self, results: list[tuple[int, str, str]] | None = None):
        self._results = list(results) if results is not None else [(0, "", "")]
        self.calls: list[list[str]] = []

    def run(self, args: list[str], stdin: str | None = None) -> str:
        self.calls.append(list(args))
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


# --- GitHub (GhCLI) ----------------------------------------------------------


def test_repo_exists_true_when_gh_repo_view_succeeds():
    runner = RecordingRunner(results=[(0, "", "")])
    client = GhCLI(runner=runner)
    assert client.repo_exists("acme/agent-clio") is True
    assert runner.calls == [["gh", "repo", "view", "acme/agent-clio"]]


def test_repo_exists_false_when_gh_repo_view_fails():
    runner = RecordingRunner(results=[(1, "", "GraphQL: Could not resolve")])
    client = GhCLI(runner=runner)
    assert client.repo_exists("acme/agent-clio") is False


def test_create_repo_from_dir_argv(tmp_path):
    runner = RecordingRunner()
    client = GhCLI(runner=runner)
    client.create_repo_from_dir("acme/agent-clio", tmp_path)

    assert runner.calls[0] == ["git", "-C", str(tmp_path), "init"]
    assert runner.calls[1] == ["git", "-C", str(tmp_path), "branch", "-M", "main"]
    assert runner.calls[2] == ["git", "-C", str(tmp_path), "add", "-A"]
    # commit call carries explicit author flags (so it works with no git config)
    assert runner.calls[3][:3] == ["git", "-C", str(tmp_path)]
    assert "commit" in runner.calls[3]
    assert runner.calls[-1] == [
        "gh", "repo", "create", "acme/agent-clio",
        "--private", "--source", str(tmp_path), "--push",
    ]


def test_clone_argv():
    runner = RecordingRunner()
    client = GhCLI(runner=runner)
    client.clone("acme/agent-clio", Path("/tmp/agent-clio"))
    assert runner.calls == [["gh", "repo", "clone", "acme/agent-clio", "/tmp/agent-clio"]]


def test_commit_push_returns_false_when_nothing_changed(tmp_path):
    runner = RecordingRunner(results=[(0, "", ""), (0, "", "")])
    client = GhCLI(runner=runner)
    assert client.commit_push(tmp_path, "msg") is False
    assert runner.calls == [
        ["git", "-C", str(tmp_path), "add", "-A"],
        ["git", "-C", str(tmp_path), "status", "--porcelain"],
    ]


def test_commit_push_commits_and_pushes_when_dirty(tmp_path):
    runner = RecordingRunner(
        results=[(0, "", ""), (0, " M file.txt", ""), (0, "", ""), (0, "", "")]
    )
    client = GhCLI(runner=runner)
    assert client.commit_push(tmp_path, "update") is True
    assert runner.calls[-1] == ["git", "-C", str(tmp_path), "push"]
    assert "update" in runner.calls[2]


def test_latest_run_argv_and_parsing():
    body = json.dumps([{"databaseId": 42, "status": "completed", "conclusion": "success"}])
    runner = RecordingRunner(results=[(0, body, "")])
    client = GhCLI(runner=runner)
    result = client.latest_run("acme/agent-clio")
    assert runner.calls == [
        [
            "gh", "run", "list", "--repo", "acme/agent-clio", "--limit", "1",
            "--json", "databaseId,status,conclusion",
        ]
    ]
    assert result == {"id": 42, "status": "completed", "conclusion": "success"}


def test_latest_run_empty_list():
    runner = RecordingRunner(results=[(0, "[]", "")])
    client = GhCLI(runner=runner)
    result = client.latest_run("acme/agent-clio")
    assert result == {"id": None, "status": "not_found", "conclusion": None}


# --- Cloud (GcloudClient) -----------------------------------------------------


def test_ensure_gsa_create_path_argv():
    runner = RecordingRunner(
        results=[(1, "", "ERROR: (gcloud.iam.service-accounts.describe) NOT_FOUND"), (0, "", "")]
    )
    client = GcloudClient("example-project", runner=runner)
    email = client.ensure_gsa("agent-clio")

    assert email == "agent-clio@example-project.iam.gserviceaccount.com"
    assert runner.calls == [
        [
            "gcloud", "iam", "service-accounts", "describe", email,
            "--project", "example-project",
        ],
        [
            "gcloud", "iam", "service-accounts", "create", "agent-clio",
            "--project", "example-project", "--display-name", "agent-clio",
        ],
    ]


def test_ensure_gsa_exists_path_skips_create():
    runner = RecordingRunner(results=[(0, "agent-clio@example-project.iam.gserviceaccount.com", "")])
    client = GcloudClient("example-project", runner=runner)
    email = client.ensure_gsa("agent-clio")
    assert email == "agent-clio@example-project.iam.gserviceaccount.com"
    assert len(runner.calls) == 1  # only the describe call, no create


def test_bind_workload_identity_argv():
    runner = RecordingRunner()
    client = GcloudClient("example-project", runner=runner)
    client.bind_workload_identity(
        "agent-clio@example-project.iam.gserviceaccount.com", "clio", "clio-ksa"
    )
    assert runner.calls == [
        [
            "gcloud", "iam", "service-accounts", "add-iam-policy-binding",
            "agent-clio@example-project.iam.gserviceaccount.com",
            "--project", "example-project",
            "--role", "roles/iam.workloadIdentityUser",
            "--member", "serviceAccount:example-project.svc.id.goog[clio/clio-ksa]",
        ]
    ]


def test_add_to_group_argv():
    runner = RecordingRunner()
    client = GcloudClient("example-project", runner=runner)
    client.add_to_group("agent-clio@example-project.iam.gserviceaccount.com", "models@example.com")
    assert runner.calls == [
        [
            "gcloud", "identity", "groups", "memberships", "add",
            "--group-email", "models@example.com",
            "--member-email", "agent-clio@example-project.iam.gserviceaccount.com",
        ]
    ]


@pytest.mark.parametrize("stderr", ["ERROR: already exists", "member is already a member"])
def test_add_to_group_tolerates_already_member(stderr):
    runner = RecordingRunner(results=[(1, "", stderr)])
    client = GcloudClient("example-project", runner=runner)
    client.add_to_group("email@example-project.iam.gserviceaccount.com", "models@example.com")


def test_add_to_group_reraises_other_errors():
    runner = RecordingRunner(results=[(1, "", "PERMISSION_DENIED")])
    client = GcloudClient("example-project", runner=runner)
    with pytest.raises(ClientError):
        client.add_to_group("email@example-project.iam.gserviceaccount.com", "models@example.com")


# --- Kubectl (KubectlCLI) -----------------------------------------------------


def test_rollout_ok_argv_and_success():
    runner = RecordingRunner(results=[(0, "deployment successfully rolled out", "")])
    client = KubectlCLI(runner=runner)
    assert client.rollout_ok("clio", "clio", timeout_s=60) is True
    assert runner.calls == [
        ["kubectl", "-n", "clio", "rollout", "status", "deploy/clio", "--timeout=60s"]
    ]


def test_rollout_ok_default_timeout_argv():
    runner = RecordingRunner()
    client = KubectlCLI(runner=runner)
    client.rollout_ok("clio", "clio")
    assert runner.calls == [
        ["kubectl", "-n", "clio", "rollout", "status", "deploy/clio", "--timeout=180s"]
    ]


def test_rollout_ok_false_on_failure():
    runner = RecordingRunner(results=[(1, "", "timed out waiting for the condition")])
    client = KubectlCLI(runner=runner)
    assert client.rollout_ok("clio", "clio") is False


# --- Gojira (HttpxGojiraClient) ------------------------------------------------


def test_gojira_create_app_request_shape_and_auth_header():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(201, json={"id": 7})

    client = HttpxGojiraClient(
        "http://gojira.local", token="secret-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    app_id = client.create_app(
        name="clio", repo_full_name="acme/agent-clio", cluster="dev", namespace="clio"
    )

    assert app_id == 7
    assert captured["method"] == "POST"
    assert captured["url"] == "http://gojira.local/api/v1/apps"
    assert captured["json"] == {
        "name": "clio",
        "repo_full_name": "acme/agent-clio",
        "workload_class": "agent",
        "environments": [{"name": "dev", "cluster": "dev", "namespace": "clio"}],
        "auto_merge_setup_pr": True,
    }
    assert captured["auth"] == "Bearer secret-token"


def test_gojira_create_app_no_token_sends_no_auth_header():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        return httpx.Response(201, json={"id": 1})

    client = HttpxGojiraClient(
        "http://gojira.local", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    client.create_app(name="clio", repo_full_name="acme/agent-clio", cluster="dev", namespace="clio")


def test_gojira_create_app_409_raises_conflict():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"error": "repo already connected"})

    client = HttpxGojiraClient(
        "http://gojira.local", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(GojiraConflict):
        client.create_app(name="clio", repo_full_name="acme/agent-clio", cluster="dev", namespace="clio")


def test_gojira_app_status_get():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == "http://gojira.local/api/v1/apps/7"
        return httpx.Response(200, json={"id": 7, "status": "active"})

    client = HttpxGojiraClient(
        "http://gojira.local", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert client.app_status(7) == {"id": 7, "status": "active"}


def test_gojira_reprovision_posts_and_accepts_303_without_following():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "http://gojira.local/apps/7/provision"
        return httpx.Response(303, headers={"location": "/apps/7"})

    client = HttpxGojiraClient(
        "http://gojira.local", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    client.reprovision(7)  # must not raise, must not attempt to follow the redirect


def test_gojira_reprovision_raises_on_unexpected_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = HttpxGojiraClient(
        "http://gojira.local", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(ClientError):
        client.reprovision(7)


# --- Agent (a2a hello over kubectl port-forward) ------------------------------


class _FakeReader:
    def __init__(self, lines: list[str]):
        self._lines = iter(lines)

    def readline(self) -> str:
        return next(self._lines, "")


def test_wait_for_forwarded_port_parses_port_from_output():
    reader = _FakeReader(
        ["Handling connection for 8080\n", "Forwarding from 127.0.0.1:54321 -> 8080\n"]
    )
    assert _wait_for_forwarded_port(reader, timeout_s=1) == 54321


def test_wait_for_forwarded_port_times_out_without_the_line():
    reader = _FakeReader([])
    with pytest.raises(ClientError):
        _wait_for_forwarded_port(reader, timeout_s=0.05)


def test_build_hello_envelope_shape():
    envelope = _build_hello_envelope("are you there?")
    assert envelope["kind"] == "task"
    assert envelope["id"].startswith("hello-")
    assert len(envelope["id"]) == len("hello-") + 32  # uuid4().hex
    assert envelope["history"] == [
        {"kind": "message", "role": "user", "parts": [{"kind": "text", "text": "are you there?"}]}
    ]


def test_post_hello_sends_envelope_and_extracts_reply():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"status": {"message": {"parts": [{"text": "hi there"}]}}}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    reply = _post_hello(client, 54321, "hello?")

    assert captured["url"] == "http://127.0.0.1:54321/a2a/tasks"
    assert captured["body"]["history"][0]["parts"][0]["text"] == "hello?"
    assert reply == "hi there"


def test_post_hello_raises_on_non_200():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ClientError):
        _post_hello(client, 54321, "hello?")


class _FakePopen:
    """Stands in for subprocess.Popen(kubectl port-forward ...) in tests: no
    real process, just a fake stdout reader and terminate()/wait() recorders.
    """

    def __init__(self, lines: list[str]):
        self.stdout = _FakeReader(lines)
        self.terminated = False
        self.waited = False

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        self.waited = True


def test_agent_hello_terminates_port_forward_on_success(monkeypatch):
    fake_proc = _FakePopen(["Forwarding from 127.0.0.1:54321 -> 80\n"])
    monkeypatch.setattr(
        "cortex_api.clients.subprocess.Popen", lambda *a, **k: fake_proc
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"status": {"message": {"parts": [{"text": "hi there"}]}}}
        )

    client = KubectlAgentClient(
        http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    reply = client.hello("clio", "clio", "hello?")

    assert reply == "hi there"
    assert fake_proc.terminated is True
    assert fake_proc.waited is True


def test_agent_hello_terminates_port_forward_even_on_post_failure(monkeypatch):
    fake_proc = _FakePopen(["Forwarding from 127.0.0.1:54321 -> 80\n"])
    monkeypatch.setattr(
        "cortex_api.clients.subprocess.Popen", lambda *a, **k: fake_proc
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = KubectlAgentClient(
        http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(ClientError):
        client.hello("clio", "clio", "hello?")

    assert fake_proc.terminated is True
    assert fake_proc.waited is True


# --- Fakes satisfy the Protocols ----------------------------------------------


def test_fake_github_satisfies_protocol(tmp_path):
    fake = FakeGitHub()
    assert isinstance(fake, GitHubClient)
    assert fake.repo_exists("acme/agent-clio") is False
    fake.create_repo_from_dir("acme/agent-clio", tmp_path)
    assert fake.repo_exists("acme/agent-clio") is True
    fake.clone("acme/agent-clio", tmp_path / "clone")
    assert fake.commit_push(tmp_path, "msg") is True
    run = fake.latest_run("acme/agent-clio")
    assert {"id", "status", "conclusion"} <= set(run)


def test_fake_gojira_satisfies_protocol():
    fake = FakeGojira()
    assert isinstance(fake, GojiraClient)
    app_id = fake.create_app(name="clio", repo_full_name="acme/agent-clio", cluster="dev", namespace="clio")
    status = fake.app_status(app_id)
    assert isinstance(status, dict)
    fake.reprovision(app_id)
    assert fake.reprovision_calls == [app_id]


def test_fake_gojira_walks_pending_to_active_on_nth_poll():
    fake = FakeGojira(activate_after=3)
    app_id = fake.create_app(name="clio", repo_full_name="acme/agent-clio", cluster="dev", namespace="clio")
    assert fake.app_status(app_id)["status"] == "pending"
    assert fake.app_status(app_id)["status"] == "pending"
    assert fake.app_status(app_id)["status"] == "active"


def test_fake_gojira_can_end_in_failed():
    fake = FakeGojira(activate_after=1, end_status="failed")
    app_id = fake.create_app(name="clio", repo_full_name="acme/agent-clio", cluster="dev", namespace="clio")
    assert fake.app_status(app_id)["status"] == "failed"


def test_fake_gojira_conflict_on_create():
    fake = FakeGojira(conflict_repos={"acme/agent-clio"})
    with pytest.raises(GojiraConflict):
        fake.create_app(name="clio", repo_full_name="acme/agent-clio", cluster="dev", namespace="clio")


def test_fake_github_latest_run_sequence_can_end_in_failure():
    fake = FakeGitHub(
        run_sequence=[
            {"id": 1, "status": "in_progress", "conclusion": None},
            {"id": 1, "status": "completed", "conclusion": "failure"},
        ]
    )
    assert fake.latest_run("acme/agent-clio")["status"] == "in_progress"
    assert fake.latest_run("acme/agent-clio")["conclusion"] == "failure"
    assert fake.latest_run("acme/agent-clio")["conclusion"] == "failure"  # holds at last


def test_fake_cloud_satisfies_protocol():
    fake = FakeCloud()
    assert isinstance(fake, CloudClient)
    email = fake.ensure_gsa("clio")
    fake.bind_workload_identity(email, "clio", "clio-ksa")
    fake.add_to_group(email, "models@example.com")
    assert email in fake.gsas


def test_fake_agent_satisfies_protocol():
    fake = FakeAgent()
    assert isinstance(fake, AgentClient)
    assert fake.hello("clio", "clio", "hi") == "hello yourself"
    assert fake.calls == [("clio", "clio", "hi")]


def test_fake_kubectl_satisfies_protocol():
    fake = FakeKubectl()
    assert isinstance(fake, KubectlClient)
    assert fake.rollout_ok("clio", "clio") is True
    assert fake.calls == [("clio", "clio")]


# --- Clients.real() wiring -----------------------------------------------------


def test_clients_real_wires_expected_implementations():
    config = Config(gojira_url="http://gojira.local", gcp_project="example-project")
    clients = Clients.real(config)
    assert isinstance(clients.github, GhCLI)
    assert isinstance(clients.gojira, HttpxGojiraClient)
    assert isinstance(clients.cloud, GcloudClient)
    assert isinstance(clients.kubectl, KubectlCLI)
    assert isinstance(clients.agent, KubectlAgentClient)
    assert isinstance(clients, Clients)
