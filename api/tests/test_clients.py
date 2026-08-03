"""Tests for cortex_api.clients: real-client argv/request construction (via a
RecordingRunner and httpx.MockTransport — no real subprocesses or network),
plus structural checks that the fakes satisfy the Protocols.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from cortex_api.clients import (
    AgentClient,
    ChatMessage,
    ClientError,
    Clients,
    CloudClient,
    CommandRunner,
    GcloudClient,
    GhCLI,
    GitHubClient,
    GojiraClient,
    GojiraConflict,
    HttpxGojiraClient,
    KubectlAgentClient,
    KubectlCLI,
    KubectlClient,
    _build_task_envelope,
    _post_task,
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


# --- CommandRunner.run_bytes (Task 6: sha-pinned skill archives need raw bytes) ----


def test_run_bytes_returns_raw_stdout_bytes():
    runner = CommandRunner()
    payload = bytes([0, 1, 2, 255, 10, 13])  # includes bytes text-mode decoding would mangle
    result = runner.run_bytes(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(bytes([0,1,2,255,10,13]))"]
    )
    assert result == payload
    assert isinstance(result, bytes)


def test_run_bytes_raises_client_error_on_nonzero_exit_with_decoded_stderr():
    runner = CommandRunner()
    with pytest.raises(ClientError) as exc_info:
        runner.run_bytes(
            [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"]
        )
    assert exc_info.value.returncode == 3
    assert "boom" in str(exc_info.value)
    assert "boom" in exc_info.value.stderr


def test_run_bytes_passes_stdin_bytes_through():
    runner = CommandRunner()
    result = runner.run_bytes(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
        stdin=b"\x00hello\xff",
    )
    assert result == b"\x00hello\xff"


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


def test_head_sha_argv_and_parsing():
    # No trailing newline here: the real CommandRunner already strips stdout
    # before GhCLI ever sees it (RecordingRunner intentionally doesn't, to
    # keep this test focused on argv + the pass-through parse).
    runner = RecordingRunner(results=[(0, "abc123def456", "")])
    client = GhCLI(runner=runner)
    result = client.head_sha("acme/agent-clio")
    assert runner.calls == [
        ["gh", "api", "repos/acme/agent-clio/commits", "--jq", ".[0].sha"]
    ]
    assert result == "abc123def456"


def test_runs_for_commit_argv_and_parsing():
    body = json.dumps(
        [
            {"databaseId": 42, "status": "completed", "conclusion": "success"},
            {"databaseId": 41, "status": "completed", "conclusion": "failure"},
        ]
    )
    runner = RecordingRunner(results=[(0, body, "")])
    client = GhCLI(runner=runner)
    result = client.runs_for_commit("acme/agent-clio", "deadbeef")
    assert runner.calls == [
        [
            "gh", "run", "list", "--repo", "acme/agent-clio", "--commit", "deadbeef",
            "--json", "databaseId,status,conclusion",
        ]
    ]
    assert result == [
        {"id": 42, "status": "completed", "conclusion": "success"},
        {"id": 41, "status": "completed", "conclusion": "failure"},
    ]


def test_runs_for_commit_empty_list():
    runner = RecordingRunner(results=[(0, "[]", "")])
    client = GhCLI(runner=runner)
    result = client.runs_for_commit("acme/agent-clio", "deadbeef")
    assert result == []


def test_rerun_argv():
    runner = RecordingRunner()
    client = GhCLI(runner=runner)
    client.rerun("acme/agent-clio", 99)
    assert runner.calls == [["gh", "run", "rerun", "99", "--repo", "acme/agent-clio"]]


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


def test_ensure_gsa_does_not_exist_wording_also_falls_through_to_create():
    # gcloud's exact NOT_FOUND wording isn't the only phrasing a real describe
    # failure can carry — "does not exist" must also fall through to create.
    runner = RecordingRunner(
        results=[(1, "", "Service account agent-clio does not exist."), (0, "", "")]
    )
    client = GcloudClient("example-project", runner=runner)
    email = client.ensure_gsa("agent-clio")
    assert email == "agent-clio@example-project.iam.gserviceaccount.com"
    assert len(runner.calls) == 2  # describe, then create


def test_ensure_gsa_reraises_non_not_found_describe_failure():
    # Anything other than a "no such GSA" describe failure (permission denied,
    # a transient API error, ...) is a real problem — it must raise rather
    # than being papered over by a blind create-on-any-failure.
    runner = RecordingRunner(results=[(1, "", "ERROR: (gcloud...) PERMISSION_DENIED")])
    client = GcloudClient("example-project", runner=runner)
    with pytest.raises(ClientError):
        client.ensure_gsa("agent-clio")
    assert len(runner.calls) == 1  # never got to (or attempted) create


def test_ensure_gsa_parity_non_agent_prefixed_name():
    # Parity check: GcloudClient and FakeCloud must return identical emails for
    # the same non-agent-prefixed name (describe-succeeds path). This guards
    # against reintroducing prefix-stripping behavior that would break the
    # new prefix-agnostic approach.
    runner = RecordingRunner(results=[(0, "clio@example-project.iam.gserviceaccount.com", "")])
    gcloud_client = GcloudClient("example-project", runner=runner)
    gcloud_email = gcloud_client.ensure_gsa("clio")

    fake_client = FakeCloud(project="example-project")
    fake_email = fake_client.ensure_gsa("clio")

    assert gcloud_email == fake_email == "clio@example-project.iam.gserviceaccount.com"


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


def test_deploy_env_names_argv_and_split():
    runner = RecordingRunner(results=[(0, "VERTEXAI_PROJECT VERTEXAI_LOCATION", "")])
    client = KubectlCLI(runner=runner)
    assert client.deploy_env_names("clio", "clio") == ["VERTEXAI_PROJECT", "VERTEXAI_LOCATION"]
    assert runner.calls == [
        [
            "kubectl", "-n", "clio", "get", "deploy", "clio",
            "-o", "jsonpath={.spec.template.spec.containers[0].env[*].name}",
        ]
    ]


def test_deploy_env_names_empty_list_when_no_env():
    runner = RecordingRunner(results=[(0, "", "")])
    client = KubectlCLI(runner=runner)
    assert client.deploy_env_names("clio", "clio") == []


def test_deploy_service_account_argv_and_value():
    runner = RecordingRunner(results=[(0, "agent-clio", "")])
    client = KubectlCLI(runner=runner)
    assert client.deploy_service_account("clio", "clio") == "agent-clio"
    assert runner.calls == [
        [
            "kubectl", "-n", "clio", "get", "deploy", "clio",
            "-o", "jsonpath={.spec.template.spec.serviceAccountName}",
        ]
    ]


def test_deploy_service_account_empty_string_when_unset():
    runner = RecordingRunner(results=[(0, "", "")])
    client = KubectlCLI(runner=runner)
    assert client.deploy_service_account("clio", "clio") == ""


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


def test_gojira_client_default_http_client_has_generous_timeout():
    # No client injected -> HttpxGojiraClient must build its own with real
    # headroom over httpx's bare 5s default (gojira calls were fast in P0;
    # this is just insurance), not a bare httpx.Client().
    client = HttpxGojiraClient("http://gojira.local")
    timeout = client._client.timeout
    assert timeout.connect == 10.0
    assert timeout.read == 30.0


# --- Agent (a2a chat over kubectl port-forward) --------------------------------


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


def test_build_task_envelope_shape_single_turn():
    envelope = _build_task_envelope([{"role": "user", "text": "are you there?"}])
    assert envelope["kind"] == "task"
    assert envelope["id"].startswith("chat-")
    assert len(envelope["id"]) == len("chat-") + 32  # uuid4().hex
    assert envelope["history"] == [
        {"kind": "message", "role": "user", "parts": [{"kind": "text", "text": "are you there?"}]}
    ]


def test_build_task_envelope_shape_multi_turn():
    # Two user turns plus one agent reply in between -> three history
    # entries in the same order, each mapped to the verified message shape.
    messages: list[ChatMessage] = [
        {"role": "user", "text": "hi"},
        {"role": "agent", "text": "hello yourself"},
        {"role": "user", "text": "how are you?"},
    ]
    envelope = _build_task_envelope(messages)
    assert envelope["history"] == [
        {"kind": "message", "role": "user", "parts": [{"kind": "text", "text": "hi"}]},
        {"kind": "message", "role": "agent", "parts": [{"kind": "text", "text": "hello yourself"}]},
        {"kind": "message", "role": "user", "parts": [{"kind": "text", "text": "how are you?"}]},
    ]


def test_post_task_sends_envelope_and_extracts_reply():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"status": {"message": {"parts": [{"text": "hi there"}]}}}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    reply = _post_task(client, 54321, [{"role": "user", "text": "hello?"}])

    assert captured["url"] == "http://127.0.0.1:54321/a2a/tasks"
    assert captured["body"]["history"][0]["parts"][0]["text"] == "hello?"
    assert reply == "hi there"


def test_post_task_raises_on_non_200():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ClientError):
        _post_task(client, 54321, [{"role": "user", "text": "hello?"}])


def test_post_task_raises_client_error_on_non_json_body():
    # 200 with a body that isn't JSON at all (json.JSONDecodeError, a
    # ValueError) must be wrapped, not left to escape as a raw ValueError.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ClientError) as exc_info:
        _post_task(client, 54321, [{"role": "user", "text": "hello?"}])

    assert "malformed a2a reply" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, ValueError)


def test_post_task_raises_client_error_on_json_missing_reply_path():
    # Valid JSON that doesn't carry the expected status/message/parts/text
    # path raises KeyError; must be wrapped the same as the non-JSON case.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": {}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ClientError) as exc_info:
        _post_task(client, 54321, [{"role": "user", "text": "hello?"}])

    assert "malformed a2a reply" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, KeyError)


def test_post_task_wraps_read_timeout_as_client_error():
    # httpx.ReadTimeout isn't a ClientError; left unwrapped it would surface
    # as a raw httpx exception to the caller (hello or converse), and every
    # identical retry would hit the same cap. _post_task must translate it,
    # preserving the original exception as the cause.
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("The read operation timed out", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ClientError) as exc_info:
        _post_task(client, 54321, [{"role": "user", "text": "hello?"}])

    assert isinstance(exc_info.value.__cause__, httpx.ReadTimeout)


def test_kubectl_agent_client_default_http_client_has_generous_timeout():
    # No http_client injected -> KubectlAgentClient must build its own with a
    # timeout generous enough for a full model roundtrip (routinely >5s
    # live), not httpx's bare 5s default.
    client = KubectlAgentClient()
    timeout = client._http_client.timeout
    assert timeout.connect == 10.0
    assert timeout.read == 120.0


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


class _BlockingReader:
    """stdout stand-in whose readline() blocks forever on an Event the test
    never sets — simulates a real OS pipe hanging because kubectl never
    prints (or stops printing) the forwarding line.
    """

    def __init__(self, event: threading.Event):
        self._event = event

    def readline(self) -> str:
        self._event.wait()  # never set by the test -> blocks indefinitely
        return ""  # pragma: no cover - unreachable in this test


class _FakeBlockingPopen:
    """Stands in for subprocess.Popen when the port-forward never reports a
    port: stdout hangs, and wait() always times out so hello()'s cleanup
    falls through to kill(). Every cleanup call is recorded.

    `wait()` always raises TimeoutExpired regardless of which call it is
    (both the pre-kill wait and, per the zombie-reap fix, the post-kill
    one) — every timeout it's given is recorded in `wait_calls` so a test
    can assert on the exact sequence without the fake needing to know how
    many times it'll be called.
    """

    def __init__(self, event: threading.Event):
        self.stdout = _BlockingReader(event)
        self.terminated = False
        self.killed = False
        self.wait_calls: list[float | None] = []

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        raise subprocess.TimeoutExpired(cmd="kubectl", timeout=timeout)

    def kill(self):
        self.killed = True


def test_send_task_wraps_popen_oserror_as_client_error(monkeypatch):
    # subprocess.Popen itself can fail before any process exists (e.g.
    # `kubectl` missing from PATH) — that's OSError, not ClientError; must be
    # wrapped rather than left to escape to callers that only expect
    # ClientError.
    def raise_oserror(*args, **kwargs):
        raise OSError("no such file or directory: kubectl")

    monkeypatch.setattr("cortex_api.clients.subprocess.Popen", raise_oserror)

    client = KubectlAgentClient()
    with pytest.raises(ClientError) as exc_info:
        client.hello("clio", "clio", "hello?")

    assert "failed to start port-forward" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, OSError)


def test_agent_hello_times_out_and_cleans_up_when_port_forward_hangs(monkeypatch):
    event = threading.Event()  # never set -> fake stdout.readline() never returns
    fake_proc = _FakeBlockingPopen(event)
    monkeypatch.setattr(
        "cortex_api.clients.subprocess.Popen", lambda *a, **k: fake_proc
    )

    client = KubectlAgentClient(port_forward_timeout_s=0.5)

    start = time.monotonic()
    with pytest.raises(ClientError):
        client.hello("clio", "clio", "hello?")
    elapsed = time.monotonic() - start

    # bounded by the configured timeout (plus a little slop), not a hang
    assert elapsed < 5.0
    assert fake_proc.terminated is True
    assert fake_proc.killed is True
    # Zombie reap: a second, short wait() follows kill() (best-effort — its
    # own TimeoutExpired here must not propagate and mask the ClientError
    # already raised above).
    assert fake_proc.wait_calls == [5, 2]


def test_agent_hello_terminates_port_forward_on_success(monkeypatch):
    fake_proc = _FakePopen(["Forwarding from 127.0.0.1:54321 -> 80\n"])
    monkeypatch.setattr(
        "cortex_api.clients.subprocess.Popen", lambda *a, **k: fake_proc
    )
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"status": {"message": {"parts": [{"text": "hi there"}]}}}
        )

    client = KubectlAgentClient(
        http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    reply = client.hello("clio", "clio", "hello?")

    assert reply == "hi there"
    # hello() now shares converse()'s _send_task path, so its envelope also
    # carries the chat- id prefix and a single-turn user history entry.
    assert captured["body"]["id"].startswith("chat-")
    assert captured["body"]["history"] == [
        {"kind": "message", "role": "user", "parts": [{"kind": "text", "text": "hello?"}]}
    ]
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


def test_agent_converse_sends_multi_turn_history_and_terminates_port_forward(monkeypatch):
    fake_proc = _FakePopen(["Forwarding from 127.0.0.1:54321 -> 80\n"])
    monkeypatch.setattr(
        "cortex_api.clients.subprocess.Popen", lambda *a, **k: fake_proc
    )
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"status": {"message": {"parts": [{"text": "still here"}]}}}
        )

    client = KubectlAgentClient(
        http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    messages: list[ChatMessage] = [
        {"role": "user", "text": "hi"},
        {"role": "agent", "text": "hello yourself"},
        {"role": "user", "text": "how are you?"},
    ]
    reply = client.converse("clio", "clio", messages)

    assert reply == "still here"
    assert captured["body"]["id"].startswith("chat-")
    assert captured["body"]["history"] == [
        {"kind": "message", "role": "user", "parts": [{"kind": "text", "text": "hi"}]},
        {"kind": "message", "role": "agent", "parts": [{"kind": "text", "text": "hello yourself"}]},
        {"kind": "message", "role": "user", "parts": [{"kind": "text", "text": "how are you?"}]},
    ]
    assert fake_proc.terminated is True
    assert fake_proc.waited is True


def test_agent_converse_terminates_port_forward_even_on_post_failure(monkeypatch):
    # Same ClientError-wrapping guarantee as hello's equivalent test above,
    # re-exercised through converse() over the shared _send_task machinery.
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
        client.converse("clio", "clio", [{"role": "user", "text": "hello?"}])

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
    sha = fake.head_sha("acme/agent-clio")
    runs = fake.runs_for_commit("acme/agent-clio", sha)
    assert runs and {"id", "status", "conclusion"} <= set(runs[0])
    fake.rerun("acme/agent-clio", runs[0]["id"])


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


def test_fake_gojira_surfaces_failed_steps_on_failure():
    steps = [
        {"name": "create-namespace", "status": "failed", "error": "quota exceeded"},
        {"name": "bind-workload-identity", "status": "skipped", "error": None},
    ]
    fake = FakeGojira(activate_after=1, end_status="failed", failed_steps=steps)
    app_id = fake.create_app(name="clio", repo_full_name="acme/agent-clio", cluster="dev", namespace="clio")
    status = fake.app_status(app_id)
    assert status["status"] == "failed"
    assert status["steps"] == steps


def test_fake_gojira_conflict_on_create():
    fake = FakeGojira(conflict_repos={"acme/agent-clio"})
    with pytest.raises(GojiraConflict):
        fake.create_app(name="clio", repo_full_name="acme/agent-clio", cluster="dev", namespace="clio")


def test_fake_github_runs_for_commit_sequence_can_end_in_failure():
    fake = FakeGitHub(
        head_sha="deadbeef",
        runs_by_sha={
            "deadbeef": [
                [{"id": 1, "status": "in_progress", "conclusion": None}],
                [{"id": 1, "status": "completed", "conclusion": "failure"}],
            ]
        },
    )
    assert fake.runs_for_commit("acme/agent-clio", "deadbeef")[0]["status"] == "in_progress"
    assert fake.runs_for_commit("acme/agent-clio", "deadbeef")[0]["conclusion"] == "failure"
    # holds at last
    assert fake.runs_for_commit("acme/agent-clio", "deadbeef")[0]["conclusion"] == "failure"


def test_fake_github_runs_for_commit_empty_for_unregistered_sha():
    """A sha the fake wasn't configured for (e.g. an older commit no test
    cares about) comes back empty rather than raising — the same
    "registration lag" shape as a sha whose CI hasn't registered yet."""
    fake = FakeGitHub(head_sha="deadbeef")
    assert fake.runs_for_commit("acme/agent-clio", "some-other-sha") == []


def test_fake_github_rerun_swaps_failed_run_to_configurable_followup():
    fake = FakeGitHub(
        head_sha="deadbeef",
        runs_by_sha={
            "deadbeef": [[{"id": 7, "status": "completed", "conclusion": "failure"}]]
        },
        rerun_result={"status": "completed", "conclusion": "success"},
    )
    fake.runs_for_commit("acme/agent-clio", "deadbeef")  # learns run 7 -> sha deadbeef
    fake.rerun("acme/agent-clio", 7)
    assert fake.rerun_calls == [("acme/agent-clio", 7)]
    followup = fake.runs_for_commit("acme/agent-clio", "deadbeef")
    assert followup == [{"id": 7, "status": "completed", "conclusion": "success"}]


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
    assert fake.converse("clio", "clio", [{"role": "user", "text": "hi"}]) == "hello yourself"
    assert fake.converse_calls == [("clio", "clio", [{"role": "user", "text": "hi"}])]


def test_fake_agent_converse_records_full_messages_list_per_call():
    fake = FakeAgent()
    messages: list[ChatMessage] = [
        {"role": "user", "text": "hi"},
        {"role": "agent", "text": "hello yourself"},
        {"role": "user", "text": "how are you?"},
    ]
    fake.converse("clio", "clio", messages)
    assert fake.converse_calls == [("clio", "clio", messages)]


def test_fake_agent_converse_scripted_replies_consumed_in_order():
    fake = FakeAgent(replies=["first reply", "second reply"])
    assert fake.converse("clio", "clio", [{"role": "user", "text": "one"}]) == "first reply"
    assert fake.converse("clio", "clio", [{"role": "user", "text": "two"}]) == "second reply"


def test_fake_agent_converse_error_knob():
    fake = FakeAgent(converse_error="a2a request failed (500): boom")
    with pytest.raises(ClientError):
        fake.converse("clio", "clio", [{"role": "user", "text": "hi"}])


def test_fake_kubectl_satisfies_protocol():
    fake = FakeKubectl()
    assert isinstance(fake, KubectlClient)
    assert fake.rollout_ok("clio", "clio") is True
    assert fake.calls == [("clio", "clio")]


def test_fake_kubectl_live_spec_defaults_match_happy_path():
    fake = FakeKubectl()
    assert fake.deploy_env_names("clio", "clio") == ["VERTEXAI_PROJECT", "VERTEXAI_LOCATION"]
    assert fake.deploy_service_account("clio", "clio") == "agent-clio"


def test_fake_kubectl_live_spec_configurable_per_namespace_deploy():
    fake = FakeKubectl(
        env_names={("clio", "clio"): ["OTHER_VAR"]},
        service_accounts={("clio", "clio"): "default"},
    )
    assert fake.deploy_env_names("clio", "clio") == ["OTHER_VAR"]
    assert fake.deploy_service_account("clio", "clio") == "default"
    # An unconfigured (namespace, deploy) pair still falls back to the
    # happy-path defaults.
    assert fake.deploy_env_names("other", "other") == ["VERTEXAI_PROJECT", "VERTEXAI_LOCATION"]
    assert fake.deploy_service_account("other", "other") == "agent-other"


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
