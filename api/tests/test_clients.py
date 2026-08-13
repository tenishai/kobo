"""Tests for kobo_api.clients: real-client argv/request construction (via a
RecordingRunner and httpx.MockTransport — no real subprocesses or network),
plus structural checks that the fakes satisfy the Protocols.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from kobo_api.clients import (
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
    HttpxSlackClient,
    HttpxStorageClient,
    ImageClient,
    KubectlAgentClient,
    KubectlCLI,
    KubectlClient,
    SlackApiError,
    SlackClient,
    StorageClient,
    StorageNotFound,
    VertexImageClient,
    _build_task_envelope,
    _gcloud_print_access_token,
    _post_task,
    _wait_for_forwarded_port,
    _write_slack_tokens,
    image_tag,
    installation_token_provider,
)
from kobo_api.config import Config
from tests.fakes import (
    FakeAgent,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeImage,
    FakeKubectl,
    FakeSlack,
    FakeStorage,
)


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


# --- CommandRunner missing-binary wrapping (final-review Finding 1) ---------------
#
# subprocess.run raises FileNotFoundError (not ClientError) when the binary
# itself is off PATH (gh, kubectl, the GKE auth plugin kubectl exec's) — that
# would otherwise escape run()/run_bytes() before any rc check and blow past
# every route's `except ClientError` net as a raw 500.


def test_run_wraps_missing_binary_as_client_error():
    runner = CommandRunner()
    with pytest.raises(ClientError) as exc_info:
        runner.run(["kobo-test-nonexistent-binary-xyz"])
    assert "kobo-test-nonexistent-binary-xyz" in str(exc_info.value)
    assert "not found on PATH" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, FileNotFoundError)


def test_run_bytes_wraps_missing_binary_as_client_error():
    runner = CommandRunner()
    with pytest.raises(ClientError) as exc_info:
        runner.run_bytes(["kobo-test-nonexistent-binary-xyz"])
    assert "kobo-test-nonexistent-binary-xyz" in str(exc_info.value)
    assert "not found on PATH" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, FileNotFoundError)


# --- CommandRunner.run redact_args (final-review Finding 3) -----------------------
#
# `KubectlCLI.apply_secret`'s stage-1 argv carries `--from-literal=SLACK_*_
# TOKEN=<value>` — before this fix, CommandRunner.run's unconditional debug
# log put that full argv (secret values and all) into logs one `--log-level
# debug` away. Exercised against the REAL CommandRunner (not a fake/recording
# double): the redaction lives there, so it must be proven there.


def test_run_redact_args_true_hides_argv_from_debug_log(caplog):
    runner = CommandRunner()
    with caplog.at_level(logging.DEBUG, logger="kobo_api.clients"):
        runner.run(["echo", "--from-literal=X=secretvalue"], redact_args=True)
    assert "secretvalue" not in caplog.text


def test_run_redact_args_false_by_default_still_logs_full_argv(caplog):
    # Baseline: redact_args defaults to False, so every other (non-secret-
    # bearing) caller's existing debug-log behavior is unchanged by this fix.
    runner = CommandRunner()
    with caplog.at_level(logging.DEBUG, logger="kobo_api.clients"):
        runner.run(["echo", "harmless-marker-value"])
    assert "harmless-marker-value" in caplog.text


# --- CommandRunner env overlay (Task 3: GitHub App installation tokens) -----------
#
# `run()`/`run_bytes()` gain a keyword `env` — when set, the subprocess gets
# `{**os.environ, **env}`; when None (every pre-existing caller, unchanged),
# no `env=` kwarg reaches subprocess.run at all, so behavior is byte-identical
# to before this parameter existed. Exercised against the REAL CommandRunner
# (real subprocesses) since the property under test — does the overlay
# actually reach the child, and does it ever mutate this process's own
# os.environ or get logged — can only be proven there.


def test_run_env_overlay_reaches_the_subprocess():
    runner = CommandRunner()
    result = runner.run(
        [sys.executable, "-c", "import os; print(os.environ.get('KOBO_TEST_VAR', 'MISSING'))"],
        env={"KOBO_TEST_VAR": "hello"},
    )
    assert result == "hello"


def test_run_env_overlay_preserves_existing_os_environ_vars():
    # {**os.environ, **env} — the subprocess still inherits the parent's own
    # environment (e.g. PATH), it isn't replaced by the overlay.
    runner = CommandRunner()
    result = runner.run(
        [sys.executable, "-c", "import os; print('PATH' in os.environ)"],
        env={"KOBO_TEST_VAR": "hello"},
    )
    assert result == "True"


def test_run_env_none_by_default_is_byte_identical_to_before(monkeypatch):
    # No env= kwarg reaches subprocess.run at all when the caller passes
    # none — proven by capturing subprocess.run's actual call kwargs.
    captured = {}
    real_run = subprocess.run

    def spy(*args, **kwargs):
        captured.update(kwargs)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    runner = CommandRunner()
    runner.run([sys.executable, "-c", "pass"])
    assert captured["env"] is None


def test_run_env_overlay_does_not_mutate_os_environ():
    runner = CommandRunner()
    before = dict(os.environ)
    runner.run(
        [sys.executable, "-c", "pass"], env={"KOBO_TEST_ONLY_VAR": "should-not-leak"}
    )
    assert os.environ == before
    assert "KOBO_TEST_ONLY_VAR" not in os.environ


def test_run_env_never_appears_in_client_error_message():
    runner = CommandRunner()
    with pytest.raises(ClientError) as exc_info:
        runner.run([sys.executable, "-c", "import sys; sys.exit(1)"], env={"GH_TOKEN": "ghs_SECRETVALUE"})
    assert "ghs_SECRETVALUE" not in str(exc_info.value)


def test_run_env_never_logged_at_debug_level(caplog):
    runner = CommandRunner()
    with caplog.at_level(logging.DEBUG, logger="kobo_api.clients"):
        runner.run([sys.executable, "-c", "pass"], env={"GH_TOKEN": "ghs_SECRETDEBUGVALUE"})
    assert "ghs_SECRETDEBUGVALUE" not in caplog.text


def test_run_bytes_env_overlay_reaches_the_subprocess():
    runner = CommandRunner()
    result = runner.run_bytes(
        [
            sys.executable, "-c",
            "import os, sys; sys.stdout.buffer.write(os.environ.get('KOBO_TEST_VAR', 'MISSING').encode())",
        ],
        env={"KOBO_TEST_VAR": "bytesval"},
    )
    assert result == b"bytesval"


def test_run_bytes_env_overlay_does_not_mutate_os_environ():
    runner = CommandRunner()
    before = dict(os.environ)
    runner.run_bytes(
        [sys.executable, "-c", "pass"], env={"KOBO_TEST_ONLY_VAR": "should-not-leak"}
    )
    assert os.environ == before
    assert "KOBO_TEST_ONLY_VAR" not in os.environ


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


# --- GitHub (GhCLI.open_pr, Task 7) -------------------------------------------


def test_open_pr_argv(tmp_path):
    runner = RecordingRunner(
        results=[
            (0, "", ""),  # checkout -b
            (0, "", ""),  # add -A
            (0, "", ""),  # commit
            (0, "", ""),  # push -u origin <branch>
            (0, "https://github.com/acme-corp/skills/pull/7", ""),  # gh pr create
        ]
    )
    client = GhCLI(runner=runner)
    pr_url = client.open_pr(
        tmp_path,
        repo="acme-corp/skills",
        branch="kobo-skills-publish",
        title="kobo: publish skills to the catalog bucket",
        body="This PR adds the publish workflow.",
    )

    assert runner.calls[0] == [
        "git", "-C", str(tmp_path), "checkout", "-b", "kobo-skills-publish",
    ]
    assert runner.calls[1] == ["git", "-C", str(tmp_path), "add", "-A"]
    # commit call carries explicit author flags (so it works with no git
    # config) — same convention as create_repo_from_dir/commit_push above.
    assert runner.calls[2][:3] == ["git", "-C", str(tmp_path)]
    assert "commit" in runner.calls[2]
    assert runner.calls[2][-2:] == ["-m", "kobo: publish skills to the catalog bucket"]
    assert runner.calls[3] == [
        "git", "-C", str(tmp_path), "push", "-u", "origin", "kobo-skills-publish",
    ]
    assert runner.calls[4] == [
        "gh", "pr", "create",
        "--repo", "acme-corp/skills",
        "--title", "kobo: publish skills to the catalog bucket",
        "--body", "This PR adds the publish workflow.",
        "--head", "kobo-skills-publish",
    ]
    assert pr_url == "https://github.com/acme-corp/skills/pull/7"


# --- GitHub (GhCLI.find_pr, live-incident fix: open-PR idempotence) ---------------


def test_find_pr_argv_and_url_when_found():
    runner = RecordingRunner(
        results=[(0, "https://github.com/acme-corp/skills/pull/7", "")]
    )
    client = GhCLI(runner=runner)
    result = client.find_pr("acme-corp/skills", branch="kobo-skills-publish")

    assert runner.calls == [
        [
            "gh", "pr", "list",
            "--repo", "acme-corp/skills", "--head", "kobo-skills-publish",
            "--state", "open", "--json", "url", "--jq", ".[0].url",
        ]
    ]
    assert result == "https://github.com/acme-corp/skills/pull/7"


def test_find_pr_returns_none_when_no_open_pr():
    # gh's --jq '.[0].url' against an empty PR list prints nothing —
    # RecordingRunner's canned "" mirrors that (CommandRunner.run's real
    # strip() already collapses a trailing-newline-only stdout to "" too).
    runner = RecordingRunner(results=[(0, "", "")])
    client = GhCLI(runner=runner)
    result = client.find_pr("acme-corp/skills", branch="kobo-skills-publish")

    assert runner.calls == [
        [
            "gh", "pr", "list",
            "--repo", "acme-corp/skills", "--head", "kobo-skills-publish",
            "--state", "open", "--json", "url", "--jq", ".[0].url",
        ]
    ]
    assert result is None


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


def test_local_head_argv_and_parsing():
    # No trailing newline here — same rationale as test_head_sha_argv_and_parsing:
    # RecordingRunner intentionally skips CommandRunner's real strip so this
    # test stays focused on argv + the pass-through parse.
    runner = RecordingRunner(results=[(0, "ed17ed17ed17ed17ed17ed17ed17ed17ed17ed17", "")])
    client = GhCLI(runner=runner)
    result = client.local_head(Path("/tmp/agent-clio"))
    assert runner.calls == [["git", "-C", "/tmp/agent-clio", "rev-parse", "HEAD"]]
    assert result == "ed17ed17ed17ed17ed17ed17ed17ed17ed17ed17"


# --- GitHub (GhCLI intake methods: commit_shas_for_paths, is_ancestor, rev_parse_prefix) ----
#
# Added for intake (Task 3c: list_intake's commit history checks): three plumbing methods
# that were previously unexercised, now pinned to their exact argv and return-value contracts.


def test_commit_shas_for_paths_argv_and_returns_dict():
    # One path -> one git log invocation; multiple paths -> one per path.
    runner = RecordingRunner(
        results=[
            (0, "abc123abc123abc123abc123abc123abc123abc1", ""),
            (0, "def456def456def456def456def456def456def4", ""),
        ]
    )
    client = GhCLI(runner=runner)
    result = client.commit_shas_for_paths(Path("/tmp/repo"), ["file1.py", "file2.py"])

    assert runner.calls == [
        ["git", "-C", "/tmp/repo", "log", "-1", "--format=%H", "--", "file1.py"],
        ["git", "-C", "/tmp/repo", "log", "-1", "--format=%H", "--", "file2.py"],
    ]
    assert result == {
        "file1.py": "abc123abc123abc123abc123abc123abc123abc1",
        "file2.py": "def456def456def456def456def456def456def4",
    }


def test_is_ancestor_rc_zero_returns_true():
    # git merge-base --is-ancestor exits 0 when ancestor is reachable from descendant.
    runner = RecordingRunner(results=[(0, "", "")])
    client = GhCLI(runner=runner)
    result = client.is_ancestor(Path("/tmp/repo"), "abc123", "def456")

    assert runner.calls == [
        ["git", "-C", "/tmp/repo", "merge-base", "--is-ancestor", "abc123", "def456"]
    ]
    assert result is True


def test_is_ancestor_rc_one_returns_false():
    # git merge-base --is-ancestor exits 1 when ancestor is NOT reachable from descendant.
    runner = RecordingRunner(results=[(1, "", "")])
    client = GhCLI(runner=runner)
    result = client.is_ancestor(Path("/tmp/repo"), "abc123", "def456")

    assert result is False


def test_is_ancestor_rc_128_reraises():
    # git merge-base --is-ancestor exits 128 for errors (bad refs, missing repo).
    # Any exit code other than 0 or 1 must re-raise, not swallow.
    runner = RecordingRunner(results=[(128, "", "fatal: invalid revision range")])
    client = GhCLI(runner=runner)

    with pytest.raises(ClientError) as exc_info:
        client.is_ancestor(Path("/tmp/repo"), "bad-ref", "other-bad-ref")

    assert exc_info.value.returncode == 128


def test_rev_parse_prefix_resolvable_returns_stripped_sha():
    # Resolves a 12-hex prefix (or any committish) to its full sha. The ^{commit}
    # peel is deliberate (annotated-tag safety): ensures we always get the commit
    # sha, never a tag object's own id.
    runner = RecordingRunner(results=[(0, "abcdef0123456789abcdef0123456789abcdef01", "")])
    client = GhCLI(runner=runner)
    result = client.rev_parse_prefix(Path("/tmp/repo"), "abcdef01234567")

    assert runner.calls == [
        ["git", "-C", "/tmp/repo", "rev-parse", "abcdef01234567^{commit}"]
    ]
    assert result == "abcdef0123456789abcdef0123456789abcdef01"


def test_rev_parse_prefix_unresolvable_returns_none():
    # Unable to resolve (bad prefix, force-pushed ref, non-existent tag) — returns
    # None rather than raising, so callers can distinguish "not found" (pending)
    # from "found and ingested" without try/except.
    runner = RecordingRunner(results=[(1, "", "fatal: bad revision 'nosuchprefix^{commit}'")])
    client = GhCLI(runner=runner)
    result = client.rev_parse_prefix(Path("/tmp/repo"), "nosuchprefix")

    assert result is None


# --- GitHub (GhCLI.token_provider — Task 3: GitHub App installation tokens) -------
#
# Deployed, GhCLI authenticates as an installed App instead of the operator's
# ambient `gh auth` session: `token_provider`, when set, makes every gh/git
# invocation pass `env={"GH_TOKEN": token_provider()}`. No `token_provider`
# (dev, unchanged) means every call's `env` stays None, exactly as before
# this parameter existed — the pre-existing GhCLI tests above already prove
# that (they never pass token_provider and never inspect `runner.envs`).


def test_ghcli_passes_gh_token_env_when_provider_set():
    runner = RecordingRunner(results=[(0, "", "")])
    client = GhCLI(runner=runner, token_provider=lambda: "ghs_faketoken123")

    client.repo_exists("acme/agent-clio")

    assert runner.envs == [{"GH_TOKEN": "ghs_faketoken123"}]


def test_ghcli_env_absent_when_no_token_provider():
    runner = RecordingRunner(results=[(0, "", "")])
    client = GhCLI(runner=runner)

    client.repo_exists("acme/agent-clio")

    assert runner.envs == [None]


def test_ghcli_token_provider_called_fresh_per_invocation():
    # "Provider called per-invocation (fresh cached token)" — a multi-call
    # method (create_repo_from_dir: 5 runner.run calls) must call the
    # provider once per call, not once total and reuse — the provider
    # itself (InstallationTokenProvider.token, in real use) is what caches;
    # GhCLI must never short-circuit that by calling it only once.
    tokens = iter(["tok-1", "tok-2", "tok-3", "tok-4", "tok-5"])
    calls = []

    def provider():
        tok = next(tokens)
        calls.append(tok)
        return tok

    runner = RecordingRunner()
    client = GhCLI(runner=runner, token_provider=provider)

    client.create_repo_from_dir("acme/agent-clio", Path("/tmp/agent-clio"))

    assert len(calls) == 5
    assert runner.envs == [{"GH_TOKEN": tok} for tok in calls]


def test_ghcli_failing_command_error_never_contains_the_token():
    # Security self-review: the installation token must never appear in a
    # ClientError raised from a failed gh/git call. RecordingRunner's
    # ClientError message (like the real CommandRunner's) is built purely
    # from argv + stdout/stderr — env is a separate channel entirely — so
    # this also guards against a future change accidentally folding env
    # into that message.
    runner = RecordingRunner(results=[(1, "", "fatal: repository not found")])
    client = GhCLI(runner=runner, token_provider=lambda: "ghs_SUPERSECRETTOKEN")

    with pytest.raises(ClientError) as exc_info:
        client.head_sha("acme/agent-clio")

    assert "ghs_SUPERSECRETTOKEN" not in str(exc_info.value)


def test_ghcli_real_runner_failing_command_error_never_contains_the_token(caplog):
    # Same property, but against the REAL CommandRunner (real subprocess,
    # real env passing) rather than RecordingRunner — belt and suspenders on
    # the actual code path a deployed room runs.
    runner = CommandRunner()
    client = GhCLI(
        runner=runner,
        git=sys.executable,
        gh=sys.executable,
        token_provider=lambda: "ghs_REALRUNNERSECRET",
    )
    with (
        caplog.at_level(logging.DEBUG, logger="kobo_api.clients"),
        pytest.raises(ClientError) as exc_info,
    ):
        # "git" here is sys.executable with no script — a real failure
        # (python prints its own usage/error and exits nonzero) with no
        # involvement from kobo's own code in choosing what to print.
        client.local_head(Path("/nonexistent/path/xyz"))
    assert "ghs_REALRUNNERSECRET" not in str(exc_info.value)
    assert "ghs_REALRUNNERSECRET" not in caplog.text


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


def test_deploy_image_argv_and_value():
    runner = RecordingRunner(
        results=[(0, "us-docker.pkg.dev/example/gojira-app:ed17ed17ed17", "")]
    )
    client = KubectlCLI(runner=runner)
    assert (
        client.deploy_image("clio", "clio")
        == "us-docker.pkg.dev/example/gojira-app:ed17ed17ed17"
    )
    assert runner.calls == [
        [
            "kubectl", "-n", "clio", "get", "deploy", "clio",
            "-o", "jsonpath={.spec.template.spec.containers[0].image}",
        ]
    ]


# --- image_tag (digest-pinned deploy fix, live incident) -----------------------
#
# The platform's regenerated CI now applies Deployment images pinned BY
# DIGEST (`repo:tag@sha256:...`) rather than by tag alone — a plain
# `image.rsplit(":", 1)[-1]` parse (the old bug, still present in knowledge.py
# and intake.py until this fix lands) would yield the digest hex instead of
# the tag in that shape, sticking delivery chips at "rolling"/"pending"
# forever. `image_tag` strips the `@sha256:...` suffix FIRST so the
# trailing-colon split can never mistake the digest hex for a tag.


def test_image_tag_plain_tag():
    assert image_tag("us-docker.pkg.dev/example/gojira-app:ed17ed17ed17") == "ed17ed17ed17"


def test_image_tag_tag_at_digest():
    # The live repro: digest-pinned apply, with the tag still present before
    # the '@sha256:...' suffix.
    assert (
        image_tag(
            "us-east4-docker.pkg.dev/p/r/network:63bd0c4fef25"
            "@sha256:3c68b1e1a2a3f4b5c6d7e8f900112233445566778899aabbccddeeff0011223"
        )
        == "63bd0c4fef25"
    )


def test_image_tag_digest_only_returns_none():
    # No tag at all — just a repo@sha256:digest reference.
    assert (
        image_tag(
            "us-docker.pkg.dev/example/gojira-app"
            "@sha256:3c68b1e1a2a3f4b5c6d7e8f900112233445566778899aabbccddeeff0011223"
        )
        is None
    )


def test_image_tag_port_no_tag_returns_none():
    # A registry port looks like a trailing ":segment", but that segment
    # contains a '/', so it must not be mistaken for a tag.
    assert image_tag("registry:5000/repo") is None


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


def test_gojira_reprovision_posts_api_route_with_bearer_and_accepts_202():
    # The UI form route (/apps/{id}/provision) sits behind gojira's
    # IAP-identity gate, which a server-to-server caller can never satisfy —
    # reprovision must ride the token-gated /api/v1 namespace like every
    # other programmatic call.
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "http://gojira.local/api/v1/apps/7/provision"
        assert request.headers["Authorization"] == "Bearer tok"
        return httpx.Response(202, json={"id": 7, "status": "provisioning"})

    client = HttpxGojiraClient(
        "http://gojira.local",
        token="tok",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client.reprovision(7)  # must not raise


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


# --- Gojira.ping (Task 8: org status's deploy-platform check) -----------------


def test_gojira_ping_healthz_request_shape():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={})

    client = HttpxGojiraClient(
        "http://gojira.local", token="secret-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.ping() is True
    assert captured["method"] == "GET"
    assert captured["url"] == "http://gojira.local/healthz"
    assert captured["auth"] is None


def test_gojira_ping_never_sends_auth_header():
    def handler(request: httpx.Request) -> httpx.Response:
        # ping() is an auth-free health probe, even when a token is configured
        assert "authorization" not in request.headers
        assert str(request.url) == "http://gojira.local/healthz"
        return httpx.Response(200, json={})

    client = HttpxGojiraClient(
        "http://gojira.local", token="secret-token",
        client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    assert client.ping() is True


def test_gojira_ping_false_on_non_200():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = HttpxGojiraClient(
        "http://gojira.local", client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    assert client.ping() is False


def test_gojira_ping_false_on_404():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    client = HttpxGojiraClient(
        "http://gojira.local", client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    assert client.ping() is False


def test_gojira_ping_never_raises_on_transport_error():
    # A connection failure (unreachable host, refused connection, ...) must
    # come back as a plain False, not an escaping httpx exception — ping()'s
    # whole contract (clients.py's GojiraClient Protocol) is that it never
    # raises.
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = HttpxGojiraClient(
        "http://gojira.local", client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    assert client.ping() is False


def test_gojira_ping_never_raises_on_arbitrary_exception():
    # Any exception during the request (not just httpx.HTTPError) must come
    # back as a plain False — ping()'s whole contract is that it never
    # raises, regardless of exception origin (header construction, URL
    # joining, anything).
    client = HttpxGojiraClient("http://gojira.local")
    # Monkeypatch _headers to raise RuntimeError
    def raising_headers():
        raise RuntimeError("something broke in header construction")
    client._headers = raising_headers

    assert client.ping() is False


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

    monkeypatch.setattr("kobo_api.clients.subprocess.Popen", raise_oserror)

    client = KubectlAgentClient()
    with pytest.raises(ClientError) as exc_info:
        client.hello("clio", "clio", "hello?")

    assert "failed to start port-forward" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, OSError)


def test_agent_hello_times_out_and_cleans_up_when_port_forward_hangs(monkeypatch):
    event = threading.Event()  # never set -> fake stdout.readline() never returns
    fake_proc = _FakeBlockingPopen(event)
    monkeypatch.setattr(
        "kobo_api.clients.subprocess.Popen", lambda *a, **k: fake_proc
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
        "kobo_api.clients.subprocess.Popen", lambda *a, **k: fake_proc
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
        "kobo_api.clients.subprocess.Popen", lambda *a, **k: fake_proc
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
        "kobo_api.clients.subprocess.Popen", lambda *a, **k: fake_proc
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
        "kobo_api.clients.subprocess.Popen", lambda *a, **k: fake_proc
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


# --- Slack (HttpxSlackClient) --------------------------------------------------
#
# Fake-shaped-but-obviously-fake token literals only (never real Slack token
# material) — these strings exist purely to prove the client reads/writes the
# right file fields and never leaks them into exception messages.

SLACK_TOKENS_TEMPLATE = """\
# Slack app-configuration credentials for P2c (sandbox workspace).
# GITIGNORED (*.local.yaml) — never commit.
# Access token expires ~12h from issue; rotate BOTH via tooling.tokens.rotate.
slack_app_config_token: {config_token}
slack_app_config_refresh_token: {refresh_token}
"""


def _write_slack_tokens_file(tmp_path: Path, *, config_token: str, refresh_token: str) -> Path:
    path = tmp_path / "slack.local.yaml"
    path.write_text(
        SLACK_TOKENS_TEMPLATE.format(config_token=config_token, refresh_token=refresh_token)
    )
    return path


def test_slack_create_app_request_shape_and_auth_header(tmp_path):
    tokens_path = _write_slack_tokens_file(
        tmp_path,
        config_token="xoxe.xoxp-1-fake-config-token",
        refresh_token="xoxe-1-fake-refresh-token",
    )
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["manifest"] = parse_qs(request.content.decode())["manifest"][0]
        return httpx.Response(200, json={"ok": True, "app_id": "A0FAKE"})

    client = HttpxSlackClient(
        tokens_path, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    manifest = {"display_information": {"name": "clio"}}
    app_id = client.create_app(manifest)

    assert app_id == "A0FAKE"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://slack.com/api/apps.manifest.create"
    assert captured["auth"] == "Bearer xoxe.xoxp-1-fake-config-token"
    assert captured["manifest"] == json.dumps(manifest)


def test_slack_create_app_rotates_and_retries_on_token_expired(tmp_path):
    tokens_path = _write_slack_tokens_file(
        tmp_path, config_token="xoxe.xoxp-1-old-config", refresh_token="xoxe-1-old-refresh"
    )
    calls: list[str] = []
    file_at_retry: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path == "/api/apps.manifest.create":
            auth = request.headers.get("authorization")
            if auth == "Bearer xoxe.xoxp-1-old-config":
                return httpx.Response(200, json={"ok": False, "error": "token_expired"})
            # Retry with the rotated token: the tokens file must already
            # carry the new pair by the time this fires (persist-before-use).
            file_at_retry["text"] = tokens_path.read_text()
            assert auth == "Bearer xoxe.xoxp-1-new-config"
            return httpx.Response(200, json={"ok": True, "app_id": "A0NEW"})
        elif path == "/api/tooling.tokens.rotate":
            body = parse_qs(request.content.decode())
            assert body["refresh_token"][0] == "xoxe-1-old-refresh"
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "token": "xoxe.xoxp-1-new-config",
                    "refresh_token": "xoxe-1-new-refresh",
                },
            )
        raise AssertionError(f"unexpected path {path}")

    client = HttpxSlackClient(
        tokens_path, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    app_id = client.create_app({"display_information": {"name": "clio"}})

    assert app_id == "A0NEW"
    assert calls == [
        "/api/apps.manifest.create",
        "/api/tooling.tokens.rotate",
        "/api/apps.manifest.create",
    ]
    # File already held the new pair (and not the old, single-use refresh
    # token) BEFORE the retry fired.
    assert "xoxe.xoxp-1-new-config" in file_at_retry["text"]
    assert "xoxe-1-new-refresh" in file_at_retry["text"]
    assert "xoxe-1-old-refresh" not in file_at_retry["text"]
    assert "xoxe.xoxp-1-old-config" not in file_at_retry["text"]
    # Comment lines survived the rewrite.
    assert "# GITIGNORED" in file_at_retry["text"]
    assert "# Access token expires ~12h from issue" in file_at_retry["text"]

    # And the final on-disk state matches (the rewrite wasn't undone/re-read
    # stale afterward).
    final_text = tokens_path.read_text()
    assert "xoxe.xoxp-1-new-config" in final_text
    assert "xoxe-1-new-refresh" in final_text


def test_slack_create_app_rotate_preserves_tokens_file_mode(tmp_path):
    # An operator may have hardened the tokens file to 0600 (it holds a live
    # Slack refresh token). Rotation rewrites the file via tmp + os.replace;
    # the tmp file must not silently widen permissions back to umask-default.
    tokens_path = _write_slack_tokens_file(
        tmp_path, config_token="xoxe.xoxp-1-old-config", refresh_token="xoxe-1-old-refresh"
    )
    os.chmod(tokens_path, 0o600)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/apps.manifest.create":
            auth = request.headers.get("authorization")
            if auth == "Bearer xoxe.xoxp-1-old-config":
                return httpx.Response(200, json={"ok": False, "error": "token_expired"})
            assert auth == "Bearer xoxe.xoxp-1-new-config"
            return httpx.Response(200, json={"ok": True, "app_id": "A0NEW"})
        elif path == "/api/tooling.tokens.rotate":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "token": "xoxe.xoxp-1-new-config",
                    "refresh_token": "xoxe-1-new-refresh",
                },
            )
        raise AssertionError(f"unexpected path {path}")

    client = HttpxSlackClient(
        tokens_path, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    app_id = client.create_app({"display_information": {"name": "clio"}})

    assert app_id == "A0NEW"
    assert os.stat(tokens_path).st_mode & 0o777 == 0o600


def test_write_slack_tokens_tmp_file_never_wider_than_0600_before_copymode(tmp_path, monkeypatch):
    # Quick-fold: the tmp file used to be created via plain write_text(),
    # which lands it at umask-default perms (e.g. 0644) for the window
    # between "content written" and "copymode fixes the mode up" — during
    # which it already holds the live refresh token. Spy on shutil.copymode
    # (called right after the content write, right before the atomic
    # replace) to catch the tmp file's mode at exactly that moment: it must
    # already be 0600, not merely corrected to it afterward.
    tokens_path = _write_slack_tokens_file(
        tmp_path, config_token="old-config", refresh_token="old-refresh"
    )
    os.chmod(tokens_path, 0o644)  # an unhardened original, so 0600 can only come from the fix
    real_copymode = shutil.copymode
    captured: dict[str, int] = {}

    def spy_copymode(src, dst):
        captured["tmp_mode_before_copymode"] = os.stat(dst).st_mode & 0o777
        return real_copymode(src, dst)

    monkeypatch.setattr("kobo_api.clients.shutil.copymode", spy_copymode)

    _write_slack_tokens(tokens_path, "new-config", "new-refresh")

    assert captured["tmp_mode_before_copymode"] == 0o600


def test_slack_create_app_second_auth_failure_after_rotate_raises(tmp_path):
    tokens_path = _write_slack_tokens_file(
        tmp_path, config_token="xoxe.xoxp-1-old-config", refresh_token="xoxe-1-old-refresh"
    )
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path == "/api/apps.manifest.create":
            return httpx.Response(200, json={"ok": False, "error": "invalid_auth"})
        elif path == "/api/tooling.tokens.rotate":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "token": "xoxe.xoxp-1-new-config",
                    "refresh_token": "xoxe-1-new-refresh",
                },
            )
        raise AssertionError(f"unexpected path {path}")

    client = HttpxSlackClient(
        tokens_path, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(SlackApiError):
        client.create_app({"display_information": {"name": "clio"}})

    # Exactly one rotate-and-retry — never an infinite loop.
    assert calls == [
        "/api/apps.manifest.create",
        "/api/tooling.tokens.rotate",
        "/api/apps.manifest.create",
    ]


def test_slack_create_app_non_auth_error_raises_slack_api_error_without_token_material(tmp_path):
    tokens_path = _write_slack_tokens_file(
        tmp_path,
        config_token="xoxe.xoxp-1-super-secret-config",
        refresh_token="xoxe-1-super-secret-refresh",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "invalid_manifest"})

    client = HttpxSlackClient(
        tokens_path, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(SlackApiError) as exc_info:
        client.create_app({"display_information": {"name": "clio"}})

    assert isinstance(exc_info.value, ClientError)
    assert "invalid_manifest" in str(exc_info.value)
    assert "xoxe.xoxp-1-super-secret-config" not in str(exc_info.value)
    assert "xoxe-1-super-secret-refresh" not in str(exc_info.value)


def test_slack_missing_tokens_file_raises_client_error_naming_path(tmp_path):
    missing_path = tmp_path / "does-not-exist.yaml"
    client = HttpxSlackClient(missing_path)

    with pytest.raises(ClientError) as exc_info:
        client.create_app({"display_information": {"name": "clio"}})

    assert str(missing_path) in str(exc_info.value)


def test_slack_malformed_tokens_file_raises_client_error_naming_path(tmp_path):
    tokens_path = tmp_path / "slack.local.yaml"
    tokens_path.write_text("# just a comment\nsome_other_key: value\n")
    client = HttpxSlackClient(tokens_path)

    with pytest.raises(ClientError) as exc_info:
        client.create_app({"display_information": {"name": "clio"}})

    assert str(tokens_path) in str(exc_info.value)


# --- Image (VertexImageClient) -------------------------------------------------


def test_vertex_image_client_request_shape_url_body_and_auth_header():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "inlineData": {
                                        "mimeType": "image/png",
                                        "data": base64.b64encode(b"x").decode(),
                                    }
                                }
                            ]
                        }
                    }
                ]
            },
        )

    client = VertexImageClient(
        "acme-project",
        "us-central1",
        "gemini-2.5-flash-image",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_provider=lambda: "fake-access-token",
    )
    client.generate("a small red circle on a white background, minimal icon")

    assert captured["method"] == "POST"
    assert captured["url"] == (
        "https://us-central1-aiplatform.googleapis.com/v1/projects/acme-project/"
        "locations/us-central1/publishers/google/models/gemini-2.5-flash-image:generateContent"
    )
    assert captured["json"] == {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": "a small red circle on a white background, minimal icon"}],
            }
        ],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
    }
    assert captured["auth"] == "Bearer fake-access-token"


def test_vertex_image_client_happy_path_skips_text_part_finds_first_inline_data():
    # Canned reply carries a TEXT part BEFORE the inlineData part (a real
    # generateContent reply routinely does) — proves generate() scans for the
    # first inlineData part rather than assuming it's parts[0].
    png_bytes = b"\x89PNG\r\n\x1a\nrest-of-a-real-png-payload"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "here's your avatar:"},
                                {
                                    "inlineData": {
                                        "mimeType": "image/png",
                                        "data": base64.b64encode(png_bytes).decode(),
                                    }
                                },
                            ]
                        }
                    }
                ]
            },
        )

    client = VertexImageClient(
        "acme-project", "us-central1", "gemini-2.5-flash-image",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_provider=lambda: "fake-access-token",
    )

    result = client.generate("a small red circle on a white background")

    assert result == png_bytes


def test_vertex_image_client_no_image_part_raises_client_error_without_leaking_prompt():
    # 200 with no inlineData part at all (safety-blocked or text-only reply).
    prompt = "a very specific secret prompt about a dragon riding a unicycle"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": "sorry, can't make that"}]}}]},
        )

    client = VertexImageClient(
        "acme-project", "us-central1", "gemini-2.5-flash-image",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_provider=lambda: "fake-access-token",
    )

    with pytest.raises(ClientError) as exc_info:
        client.generate(prompt)

    assert str(exc_info.value) == "the picture didn't come through — try again"
    assert prompt not in str(exc_info.value)


def test_vertex_image_client_invalid_base64_raises_client_error_without_leaking_prompt():
    # Well-formed JSON, inlineData present, but the data field isn't valid
    # base64 — binascii.Error is a ValueError subclass, so the extraction's
    # except clause must wrap it like every other malformed-reply shape.
    prompt = "a very specific secret prompt about a dragon riding a unicycle"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"inlineData": {"mimeType": "image/png", "data": "not-valid-base64!!"}}
                            ]
                        }
                    }
                ]
            },
        )

    client = VertexImageClient(
        "acme-project", "us-central1", "gemini-2.5-flash-image",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_provider=lambda: "fake-access-token",
    )

    with pytest.raises(ClientError) as exc_info:
        client.generate(prompt)

    assert str(exc_info.value) == "the picture didn't come through — try again"
    assert prompt not in str(exc_info.value)


def test_vertex_image_client_non_200_raises_client_error_without_leaking_prompt():
    prompt = "a very specific secret prompt about a dragon riding a unicycle"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    client = VertexImageClient(
        "acme-project", "us-central1", "gemini-2.5-flash-image",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_provider=lambda: "fake-access-token",
    )

    with pytest.raises(ClientError) as exc_info:
        client.generate(prompt)

    assert str(exc_info.value) == "the picture didn't come through — try again"
    assert prompt not in str(exc_info.value)


def test_vertex_image_client_malformed_json_raises_client_error_without_leaking_prompt():
    prompt = "a very specific secret prompt about a dragon riding a unicycle"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json at all")

    client = VertexImageClient(
        "acme-project", "us-central1", "gemini-2.5-flash-image",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_provider=lambda: "fake-access-token",
    )

    with pytest.raises(ClientError) as exc_info:
        client.generate(prompt)

    assert str(exc_info.value) == "the picture didn't come through — try again"
    assert prompt not in str(exc_info.value)


def test_vertex_image_client_unexpected_json_shape_raises_client_error():
    # Well-formed JSON, but missing the candidates/content/parts path
    # entirely — a different malformed-reply shape than "no inlineData part".
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    client = VertexImageClient(
        "acme-project", "us-central1", "gemini-2.5-flash-image",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_provider=lambda: "fake-access-token",
    )

    with pytest.raises(ClientError) as exc_info:
        client.generate("a prompt")

    assert str(exc_info.value) == "the picture didn't come through — try again"


def test_vertex_image_client_default_http_client_has_generous_timeout():
    # No client injected -> VertexImageClient must build its own with real
    # headroom over httpx's bare 5s default: image generation is a full
    # model roundtrip (routinely slow, like KubectlAgentClient's a2a hello),
    # not a quick CRUD call.
    client = VertexImageClient("acme-project", "us-central1", "gemini-2.5-flash-image")
    timeout = client._http_client.timeout
    assert timeout.connect == 10.0
    assert timeout.read == 120.0


def test_vertex_image_client_default_token_provider_is_gcloud_print_access_token():
    # No existing google-auth idiom exists anywhere in this codebase (grepped
    # kobo_api and the wider repo) — the default shells `gcloud auth
    # print-access-token` through CommandRunner, mirroring every other real
    # client in this module (GhCLI, GcloudClient, KubectlCLI all shell out
    # rather than use a google-auth library).
    client = VertexImageClient("acme-project", "us-central1", "gemini-2.5-flash-image")
    assert client._token_provider is _gcloud_print_access_token


def test_vertex_image_client_token_provider_failure_raises_client_error_without_leaking_gcloud_output():
    # A failing token_provider (the default shells `gcloud auth
    # print-access-token`, which raises ClientError carrying the full
    # command line + stderr — possibly the operator's account email) must
    # not escape generate() unwrapped: every failure mode raises the same
    # plain _IMAGE_GENERATION_FAILED message, never raw gcloud output.
    def failing_token_provider() -> str:
        raise ClientError(
            "gcloud auth print-access-token failed (rc=1): ERROR: account details"
        )

    client = VertexImageClient(
        "acme-project", "us-central1", "gemini-2.5-flash-image",
        http_client=httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={})
        )),
        token_provider=failing_token_provider,
    )

    with pytest.raises(ClientError) as exc_info:
        client.generate("a prompt")

    assert str(exc_info.value) == "the picture didn't come through — try again"
    assert "gcloud" not in str(exc_info.value)
    assert "account details" not in str(exc_info.value)


def test_gcloud_print_access_token_argv_and_pass_through():
    runner = RecordingRunner(results=[(0, "ya29.fake-access-token", "")])
    token = _gcloud_print_access_token(runner)
    assert runner.calls == [["gcloud", "auth", "print-access-token"]]
    assert token == "ya29.fake-access-token"


# --- Storage (HttpxStorageClient / GCS JSON API) --------------------------------

_STORAGE_API_BASE = "https://storage.googleapis.com/storage/v1"
_STORAGE_UPLOAD_BASE = "https://storage.googleapis.com/upload/storage/v1"


def test_storage_read_bytes_request_shape_and_key_quoting():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, content=b"hello world")

    client = HttpxStorageClient(
        "acme-project",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_provider=lambda: "fake-access-token",
    )
    result = client.read_bytes("my-bucket", "personas/clio.json")

    assert result == b"hello world"
    assert captured["method"] == "GET"
    assert captured["url"] == f"{_STORAGE_API_BASE}/b/my-bucket/o/personas%2Fclio.json?alt=media"
    assert captured["auth"] == "Bearer fake-access-token"


def test_storage_read_bytes_404_raises_storage_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "not found"}})

    client = HttpxStorageClient(
        "acme-project",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_provider=lambda: "fake-access-token",
    )
    with pytest.raises(StorageNotFound):
        client.read_bytes("my-bucket", "missing.json")


def test_storage_read_bytes_non_200_raises_client_error_without_leaking_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="a very sensitive internal error trace")

    client = HttpxStorageClient(
        "acme-project",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_provider=lambda: "fake-access-token",
    )
    with pytest.raises(ClientError) as exc_info:
        client.read_bytes("my-bucket", "some.json")
    assert not isinstance(exc_info.value, StorageNotFound)
    assert "a very sensitive internal error trace" not in str(exc_info.value)


def test_storage_object_metadata_request_shape_no_alt_media():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "name": "personas/clio.json",
                "updated": "2026-08-01T12:00:00Z",
                "metadata": {"author": "kai"},
            },
        )

    client = HttpxStorageClient(
        "acme-project",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_provider=lambda: "fake-access-token",
    )
    result = client.object_metadata("my-bucket", "personas/clio.json")

    assert captured["url"] == f"{_STORAGE_API_BASE}/b/my-bucket/o/personas%2Fclio.json"
    assert result["metadata"] == {"author": "kai"}
    assert result["updated"] == "2026-08-01T12:00:00Z"


def test_storage_object_metadata_404_raises_storage_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    client = HttpxStorageClient(
        "acme-project",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_provider=lambda: "fake-access-token",
    )
    with pytest.raises(StorageNotFound):
        client.object_metadata("my-bucket", "missing.json")


def test_storage_write_bytes_multipart_body_carries_metadata_json_and_media():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["content_type"] = request.headers.get("content-type")
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = request.content
        return httpx.Response(200, json={"name": "personas/clio.json"})

    client = HttpxStorageClient(
        "acme-project",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_provider=lambda: "fake-access-token",
    )
    client.write_bytes(
        "my-bucket",
        "personas/clio.json",
        b"\x00binary-payload\xff",
        metadata={"author": "kai"},
        content_type="application/json",
    )

    assert captured["url"] == f"{_STORAGE_UPLOAD_BASE}/b/my-bucket/o?uploadType=multipart"
    assert captured["auth"] == "Bearer fake-access-token"
    assert captured["content_type"].startswith("multipart/related; boundary=")
    boundary = captured["content_type"].split("boundary=")[1]
    body = captured["body"]
    assert f"--{boundary}".encode() in body
    assert b'"name": "personas/clio.json"' in body
    assert b'"contentType": "application/json"' in body
    assert b'"metadata": {"author": "kai"}' in body
    assert b"\x00binary-payload\xff" in body
    assert body.rstrip().endswith(f"--{boundary}--".encode())


def test_storage_write_bytes_no_metadata_omits_metadata_key():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(200, json={"name": "x"})

    client = HttpxStorageClient(
        "acme-project",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_provider=lambda: "fake-access-token",
    )
    client.write_bytes("my-bucket", "x", b"data")

    assert b'"metadata"' not in captured["body"]
    assert b'"contentType": "application/octet-stream"' in captured["body"]


def test_storage_write_bytes_non_200_raises_client_error_without_leaking_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad request detail")

    client = HttpxStorageClient(
        "acme-project",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_provider=lambda: "fake-access-token",
    )
    with pytest.raises(ClientError) as exc_info:
        client.write_bytes("my-bucket", "x", b"data")
    assert "bad request detail" not in str(exc_info.value)


def test_storage_list_keys_request_shape_and_follows_pagination():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        if "pageToken" not in request.url.params:
            return httpx.Response(
                200, json={"items": [{"name": "a"}, {"name": "b"}], "nextPageToken": "tok2"}
            )
        assert request.url.params["pageToken"] == "tok2"
        return httpx.Response(200, json={"items": [{"name": "c"}]})

    client = HttpxStorageClient(
        "acme-project",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_provider=lambda: "fake-access-token",
    )
    result = client.list_keys("my-bucket", "personas/")

    assert result == ["a", "b", "c"]
    assert len(calls) == 2
    assert str(calls[0]).startswith(f"{_STORAGE_API_BASE}/b/my-bucket/o?")
    assert calls[0].params["prefix"] == "personas/"
    assert calls[0].params["fields"] == "items(name),nextPageToken"


def test_storage_list_keys_empty_bucket_returns_empty_list():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = HttpxStorageClient(
        "acme-project",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_provider=lambda: "fake-access-token",
    )
    assert client.list_keys("my-bucket", "personas/") == []


def test_storage_list_keys_missing_bucket_raises_storage_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="no such bucket")

    client = HttpxStorageClient(
        "acme-project",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_provider=lambda: "fake-access-token",
    )
    with pytest.raises(StorageNotFound):
        client.list_keys("missing-bucket", "")


def test_storage_ensure_bucket_returns_exists_without_creating():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, json={"name": "my-bucket"})

    client = HttpxStorageClient(
        "acme-project",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_provider=lambda: "fake-access-token",
    )
    assert client.ensure_bucket("my-bucket") == "exists"
    assert calls == ["GET"]


def test_storage_ensure_bucket_creates_with_versioning_when_missing():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(404, text="not found")
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"name": "my-bucket"})

    client = HttpxStorageClient(
        "acme-project",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_provider=lambda: "fake-access-token",
    )
    assert client.ensure_bucket("my-bucket") == "created"
    assert captured["url"] == f"{_STORAGE_API_BASE}/b?project=acme-project"
    assert captured["body"] == {"name": "my-bucket", "versioning": {"enabled": True}}


def test_storage_ensure_bucket_non_404_describe_failure_raises_client_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = HttpxStorageClient(
        "acme-project",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_provider=lambda: "fake-access-token",
    )
    with pytest.raises(ClientError):
        client.ensure_bucket("my-bucket")


def test_storage_ensure_bucket_create_failure_raises_client_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(404, text="not found")
        return httpx.Response(500, text="create failed")

    client = HttpxStorageClient(
        "acme-project",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_provider=lambda: "fake-access-token",
    )
    with pytest.raises(ClientError):
        client.ensure_bucket("my-bucket")


def test_storage_ensure_versioning_already_enabled_no_patch():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, json={"versioning": {"enabled": True}})

    client = HttpxStorageClient(
        "acme-project",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_provider=lambda: "fake-access-token",
    )
    assert client.ensure_versioning("my-bucket") is False
    assert calls == ["GET"]


def test_storage_ensure_versioning_disabled_patches_and_returns_true():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"versioning": {"enabled": False}})
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"versioning": {"enabled": True}})

    client = HttpxStorageClient(
        "acme-project",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_provider=lambda: "fake-access-token",
    )
    assert client.ensure_versioning("my-bucket") is True
    assert captured["method"] == "PATCH"
    assert captured["url"] == f"{_STORAGE_API_BASE}/b/my-bucket"
    assert captured["body"] == {"versioning": {"enabled": True}}


def test_storage_ensure_versioning_missing_versioning_key_treated_as_disabled():
    # A bucket with no "versioning" key at all (never configured) must be
    # treated the same as explicitly disabled, not skipped as if already on.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"versioning": {"enabled": True}})

    client = HttpxStorageClient(
        "acme-project",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_provider=lambda: "fake-access-token",
    )
    assert client.ensure_versioning("my-bucket") is True


def test_storage_client_default_http_client_has_generous_timeout():
    # No client injected -> HttpxStorageClient must build its own with real
    # headroom over httpx's bare 5s default, same convention as every other
    # real client in this module.
    client = HttpxStorageClient("acme-project")
    timeout = client._http_client.timeout
    assert timeout.connect == 10.0
    assert timeout.read == 20.0


def test_storage_client_default_token_provider_is_gcloud_print_access_token():
    # Same seam as VertexImageClient: no existing google-auth idiom anywhere
    # in this codebase, so the default shells `gcloud auth print-access-token`.
    client = HttpxStorageClient("acme-project")
    assert client._token_provider is _gcloud_print_access_token


# --- Storage error branches (coverage gap) ------------------------------------


def test_storage_object_metadata_non_200_raises_client_error_without_leaking_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="a very sensitive internal error trace")

    client = HttpxStorageClient(
        "acme-project",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_provider=lambda: "fake-access-token",
    )
    with pytest.raises(ClientError) as exc_info:
        client.object_metadata("my-bucket", "some.json")
    assert not isinstance(exc_info.value, StorageNotFound)
    assert "a very sensitive internal error trace" not in str(exc_info.value)


def test_storage_list_keys_non_200_raises_client_error_without_leaking_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="backend failure details")

    client = HttpxStorageClient(
        "acme-project",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_provider=lambda: "fake-access-token",
    )
    with pytest.raises(ClientError) as exc_info:
        client.list_keys("my-bucket", "prefix/")
    assert not isinstance(exc_info.value, StorageNotFound)
    assert "backend failure details" not in str(exc_info.value)


def test_storage_ensure_versioning_404_raises_storage_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="bucket not found")

    client = HttpxStorageClient(
        "acme-project",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_provider=lambda: "fake-access-token",
    )
    with pytest.raises(StorageNotFound):
        client.ensure_versioning("missing-bucket")


def test_storage_ensure_versioning_describe_non_200_non_404_raises_client_error_without_leaking_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal server error on describe")

    client = HttpxStorageClient(
        "acme-project",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_provider=lambda: "fake-access-token",
    )
    with pytest.raises(ClientError) as exc_info:
        client.ensure_versioning("my-bucket")
    assert not isinstance(exc_info.value, StorageNotFound)
    assert "internal server error on describe" not in str(exc_info.value)


def test_storage_ensure_versioning_patch_failure_raises_client_error_without_leaking_body():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"versioning": {"enabled": False}})
        # PATCH fails
        return httpx.Response(500, text="patch operation failed")

    client = HttpxStorageClient(
        "acme-project",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_provider=lambda: "fake-access-token",
    )
    with pytest.raises(ClientError) as exc_info:
        client.ensure_versioning("my-bucket")
    assert "patch operation failed" not in str(exc_info.value)


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
    head = fake.local_head(tmp_path)
    assert head and fake.local_head_calls == [tmp_path]
    pr_url = fake.open_pr(
        tmp_path, repo="acme/agent-clio", branch="kobo-skills-publish", title="t", body="b"
    )
    assert pr_url and fake.open_pr_calls == [
        {
            "workdir": tmp_path,
            "repo": "acme/agent-clio",
            "branch": "kobo-skills-publish",
            "title": "t",
            "body": "b",
        }
    ]


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


def test_fake_gojira_ping_defaults_true():
    fake = FakeGojira()
    assert fake.ping() is True


def test_fake_gojira_ping_knob():
    fake = FakeGojira(ping_result=False)
    assert fake.ping() is False


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


def test_fake_github_clone_default_writes_no_knowledge_files(tmp_path):
    # knowledge_files defaults to None — clone must leave the generated
    # scaffolding as the only thing on disk, so every pre-existing test that
    # constructs a bare FakeGitHub() is unaffected by this knob's addition.
    fake = FakeGitHub()
    dest = tmp_path / "clone"
    fake.clone("acme/agent-clio", dest)
    assert not (dest / "bundle").exists()


def test_fake_github_clone_materializes_knowledge_files(tmp_path):
    fake = FakeGitHub(knowledge_files={"b.md": "second", "a.md": "first"})
    dest = tmp_path / "clone"
    fake.clone("acme/agent-clio", dest)
    knowledge_dir = dest / "bundle" / "knowledge"
    assert (knowledge_dir / "a.md").read_text() == "first"
    assert (knowledge_dir / "b.md").read_text() == "second"
    # generated scaffolding is still present alongside it
    assert (dest / "gojira.yaml").exists()


def test_fake_github_local_head_default_and_recording(tmp_path):
    fake = FakeGitHub()
    result = fake.local_head(tmp_path)
    assert result == "ed17ed17ed17ed17ed17ed17ed17ed17ed17ed17"
    assert len(result) == 40
    assert fake.local_head_calls == [tmp_path]


def test_fake_github_local_head_result_override(tmp_path):
    fake = FakeGitHub(local_head_result="cafef00dcafef00dcafef00dcafef00dcafef00")
    assert fake.local_head(tmp_path) == "cafef00dcafef00dcafef00dcafef00dcafef00"


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
    assert fake.deploy_image("clio", "clio")


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


def test_fake_kubectl_deploy_image_default_matches_fake_github_local_head_default():
    # The default deploy_image tag is deliberately the first 12 chars of
    # FakeGitHub's default local_head_result, so a happy-path test wiring
    # bare FakeGitHub()/FakeKubectl() together reads "delivered" without
    # configuring anything.
    fake = FakeKubectl()
    image = fake.deploy_image("clio", "clio")
    assert image.rsplit(":", 1)[-1] == "ed17ed17ed17"


def test_fake_kubectl_deploy_image_configurable_per_namespace_deploy():
    fake = FakeKubectl(images={("clio", "clio"): "gojira-app:abcabcabcabc"})
    assert fake.deploy_image("clio", "clio") == "gojira-app:abcabcabcabc"
    assert fake.deploy_image("other", "other").rsplit(":", 1)[-1] == "ed17ed17ed17"


def test_fake_slack_satisfies_protocol():
    fake = FakeSlack()
    assert isinstance(fake, SlackClient)
    manifest = {"display_information": {"name": "clio"}}
    assert fake.create_app(manifest) == "A0FAKE"
    assert fake.manifests == [manifest]


def test_fake_slack_custom_app_id():
    fake = FakeSlack(app_id="A1234567")
    assert fake.create_app({"display_information": {"name": "clio"}}) == "A1234567"


def test_fake_slack_records_multiple_manifests_in_order():
    fake = FakeSlack()
    fake.create_app({"display_information": {"name": "one"}})
    fake.create_app({"display_information": {"name": "two"}})
    assert fake.manifests == [
        {"display_information": {"name": "one"}},
        {"display_information": {"name": "two"}},
    ]


def test_fake_slack_create_error_knob_raises_slack_api_error():
    fake = FakeSlack(create_error="invalid_manifest")
    with pytest.raises(SlackApiError, match="invalid_manifest"):
        fake.create_app({"display_information": {"name": "clio"}})


def test_fake_image_satisfies_protocol():
    fake = FakeImage()
    assert isinstance(fake, ImageClient)
    result = fake.generate("a small red circle on a white background")
    assert result.startswith(b"\x89PNG")
    assert fake.prompts == ["a small red circle on a white background"]


def test_fake_image_records_multiple_prompts_in_order():
    fake = FakeImage()
    fake.generate("first prompt")
    fake.generate("second prompt")
    assert fake.prompts == ["first prompt", "second prompt"]


def test_fake_image_custom_image_bytes():
    fake = FakeImage(image_bytes=b"not-really-a-png-but-configurable")
    assert fake.generate("a prompt") == b"not-really-a-png-but-configurable"


def test_fake_image_generate_error_knob_raises_client_error():
    fake = FakeImage(generate_error="the picture didn't come through — try again")
    with pytest.raises(ClientError, match="the picture didn't come through"):
        fake.generate("a prompt")


def test_fake_storage_satisfies_protocol():
    fake = FakeStorage()
    assert isinstance(fake, StorageClient)
    assert fake.ensure_bucket("b") == "created"
    assert fake.ensure_bucket("b") == "exists"
    fake.write_bytes("b", "k", b"data", metadata={"a": "1"})
    assert fake.read_bytes("b", "k") == b"data"
    meta = fake.object_metadata("b", "k")
    assert meta["metadata"] == {"a": "1"}
    assert "updated" in meta
    assert fake.list_keys("b", "") == ["k"]


def test_fake_storage_seeded_via_constructor():
    fake = FakeStorage(buckets={"b": {"k": (b"seed", {"x": "y"})}})
    assert fake.read_bytes("b", "k") == b"seed"
    assert fake.object_metadata("b", "k") == {
        "metadata": {"x": "y"},
        "updated": "2026-08-03T00:00:00Z",
    }


def test_fake_storage_read_bytes_absent_key_raises_storage_not_found():
    fake = FakeStorage(buckets={"b": {}})
    with pytest.raises(StorageNotFound):
        fake.read_bytes("b", "missing")


def test_fake_storage_read_bytes_absent_bucket_raises_storage_not_found():
    fake = FakeStorage()
    with pytest.raises(StorageNotFound):
        fake.read_bytes("missing-bucket", "k")


def test_fake_storage_object_metadata_absent_key_raises_storage_not_found():
    fake = FakeStorage(buckets={"b": {}})
    with pytest.raises(StorageNotFound):
        fake.object_metadata("b", "missing")


def test_fake_storage_list_keys_missing_bucket_raises_storage_not_found():
    fake = FakeStorage()
    with pytest.raises(StorageNotFound):
        fake.list_keys("missing-bucket", "")


def test_fake_storage_list_keys_filters_by_prefix():
    fake = FakeStorage(
        buckets={"b": {"a/1": (b"x", {}), "a/2": (b"y", {}), "c/1": (b"z", {})}}
    )
    assert fake.list_keys("b", "a/") == ["a/1", "a/2"]


def test_fake_storage_ensure_bucket_already_enables_versioning():
    # Mirrors the real ensure_bucket contract (creation always turns
    # versioning on) -> ensure_versioning right after a creation reports
    # "already enabled", not "changed".
    fake = FakeStorage()
    fake.ensure_bucket("b")
    assert fake.ensure_versioning("b") is False


def test_fake_storage_ensure_versioning_true_for_pre_seeded_bucket():
    # A bucket seeded via the constructor kwarg models a pre-existing bucket
    # whose versioning state predates kobo -> the first ensure_versioning
    # call reports "changed", exercising the exact case ensure_versioning
    # exists for.
    fake = FakeStorage(buckets={"b": {}})
    assert fake.ensure_versioning("b") is True
    assert fake.ensure_versioning("b") is False


def test_fake_storage_records_calls():
    fake = FakeStorage(buckets={"b": {"k": (b"x", {})}})
    fake.read_bytes("b", "k")
    fake.object_metadata("b", "k")
    fake.write_bytes("b", "k2", b"y")
    fake.list_keys("b", "")
    fake.ensure_bucket("b")
    fake.ensure_versioning("b")
    assert fake.calls == [
        "read_bytes:b:k",
        "object_metadata:b:k",
        "write_bytes:b:k2",
        "list_keys:b:",
        "ensure_bucket:b",
        "ensure_versioning:b",
    ]


def test_fake_storage_error_knobs_raise_client_error():
    fake = FakeStorage(
        read_error="boom-read",
        metadata_error="boom-metadata",
        write_error="boom-write",
        list_error="boom-list",
        ensure_bucket_error="boom-bucket",
        ensure_versioning_error="boom-versioning",
    )
    with pytest.raises(ClientError, match="boom-read"):
        fake.read_bytes("b", "k")
    with pytest.raises(ClientError, match="boom-metadata"):
        fake.object_metadata("b", "k")
    with pytest.raises(ClientError, match="boom-write"):
        fake.write_bytes("b", "k", b"x")
    with pytest.raises(ClientError, match="boom-list"):
        fake.list_keys("b", "")
    with pytest.raises(ClientError, match="boom-bucket"):
        fake.ensure_bucket("b")
    with pytest.raises(ClientError, match="boom-versioning"):
        fake.ensure_versioning("b")


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


def test_clients_real_wires_image_client_from_avatar_and_vertex_config():
    # Always constructible (unlike slack, never gated behind an "is this
    # configured" check) — project comes from the EXISTING vertex_project
    # field, location/model from the new avatar_location/avatar_model.
    config = Config(
        gojira_url="http://gojira.local",
        gcp_project="example-project",
        vertex_project="acme-model-project",
        avatar_location="us-east4",
        avatar_model="some-model",
    )
    clients = Clients.real(config)
    assert isinstance(clients.image, VertexImageClient)
    assert clients.image._project == "acme-model-project"
    assert clients.image._location == "us-east4"
    assert clients.image._model == "some-model"


def test_clients_real_wires_slack_none_when_tokens_path_unset():
    # No slack_tokens_path configured -> slack stays None so local setups
    # without a Slack config-token pair keep working untouched.
    config = Config(gojira_url="http://gojira.local", gcp_project="example-project")
    clients = Clients.real(config)
    assert clients.slack is None


def test_clients_real_wires_slack_when_tokens_path_set(tmp_path):
    tokens_path = tmp_path / "slack.local.yaml"
    tokens_path.write_text(
        "slack_app_config_token: xoxe.xoxp-1-fake\n"
        "slack_app_config_refresh_token: xoxe-1-fake\n"
    )
    config = Config(
        gojira_url="http://gojira.local",
        gcp_project="example-project",
        slack_tokens_path=tokens_path,
    )
    clients = Clients.real(config)
    assert isinstance(clients.slack, HttpxSlackClient)


def test_clients_real_wires_storage_client_unconditionally():
    # Like image, storage is always constructible (gcp_project already
    # exists in Config for other clients) -> Clients.real() never gates it
    # behind an "is this configured" check the way it does slack.
    config = Config(gojira_url="http://gojira.local", gcp_project="example-project")
    clients = Clients.real(config)
    assert isinstance(clients.storage, HttpxStorageClient)
    assert clients.storage._project == "example-project"


# --- Clients.real() GitHub App wiring (Task 3) -------------------------------


def test_clients_real_leaves_github_token_provider_none_when_app_config_unset():
    # dev = operator's own `gh auth` login, unchanged.
    config = Config(gojira_url="http://gojira.local", gcp_project="example-project")
    clients = Clients.real(config)
    assert isinstance(clients.github, GhCLI)
    assert clients.github._token_provider is None


def test_clients_real_leaves_github_token_provider_none_when_only_some_app_config_set(
    tmp_path,
):
    # All three of github_app_id/github_app_installation_id/
    # github_app_private_key_path must be set together — any subset still
    # means dev mode.
    key_path = tmp_path / "app.pem"
    key_path.write_text("not-a-real-key")
    config = Config(
        gojira_url="http://gojira.local",
        gcp_project="example-project",
        github_app_id="123",
        github_app_installation_id="456",
        # github_app_private_key_path deliberately left unset.
    )
    clients = Clients.real(config)
    assert clients.github._token_provider is None


def test_clients_real_wires_github_token_provider_when_app_config_fully_set(tmp_path):
    key_path = tmp_path / "app.pem"
    key_path.write_text("not-a-real-key")
    config = Config(
        gojira_url="http://gojira.local",
        gcp_project="example-project",
        github_app_id="123",
        github_app_installation_id="456",
        github_app_private_key_path=key_path,
    )
    clients = Clients.real(config)
    assert isinstance(clients.github, GhCLI)
    assert clients.github._token_provider is not None
    # It's InstallationTokenProvider.token, bound to an instance configured
    # from this same config — not just "some callable".
    bound_provider = clients.github._token_provider.__self__
    assert bound_provider._app_id == "123"
    assert bound_provider._installation_id == "456"
    assert bound_provider._private_key_path == key_path


# --- installation_token_provider() (extracted from Clients.real, reused by --------
# --- main.build_default_app for the boot-time org-config clone) -------------------


def test_installation_token_provider_none_when_app_config_unset():
    config = Config(gojira_url="http://gojira.local", gcp_project="example-project")
    assert installation_token_provider(config) is None


def test_installation_token_provider_none_when_only_some_app_config_set(tmp_path):
    # All three of github_app_id/github_app_installation_id/
    # github_app_private_key_path must be set together — any subset still
    # means dev mode (None).
    key_path = tmp_path / "app.pem"
    key_path.write_text("not-a-real-key")
    config = Config(
        gojira_url="http://gojira.local",
        gcp_project="example-project",
        github_app_id="123",
        github_app_installation_id="456",
        # github_app_private_key_path deliberately left unset.
    )
    assert installation_token_provider(config) is None

    config_missing_id = Config(
        gojira_url="http://gojira.local",
        gcp_project="example-project",
        github_app_installation_id="456",
        github_app_private_key_path=key_path,
    )
    assert installation_token_provider(config_missing_id) is None


def test_installation_token_provider_wires_callable_when_app_config_fully_set(tmp_path):
    key_path = tmp_path / "app.pem"
    key_path.write_text("not-a-real-key")
    config = Config(
        gojira_url="http://gojira.local",
        gcp_project="example-project",
        github_app_id="123",
        github_app_installation_id="456",
        github_app_private_key_path=key_path,
    )
    provider = installation_token_provider(config)
    assert provider is not None
    assert callable(provider)
    # Bound to an InstallationTokenProvider instance configured from this
    # same config — not just "some callable" — without minting a real token
    # (construction reads the key lazily, only on first `.token()` call).
    bound_provider = provider.__self__
    assert bound_provider._app_id == "123"
    assert bound_provider._installation_id == "456"
    assert bound_provider._private_key_path == key_path


def test_installation_token_provider_wired_into_boot_org_clone_gh_cli():
    # Guards the actual bug fix: main.build_default_app must build the
    # org-config-clone GhCLI with this same helper's provider (not None),
    # so a PRIVATE org-config repo clones at boot when the App is
    # configured, instead of only being App-authed later inside
    # Clients.real(). This exercises the same wiring build_default_app does,
    # without booting the whole app (db/gojira/etc).
    from kobo_api.clients import GhCLI

    config = Config(
        gojira_url="http://gojira.local",
        gcp_project="example-project",
        github_app_id="123",
        github_app_installation_id="456",
        github_app_private_key_path=Path("/nonexistent/app.pem"),
    )
    github = GhCLI(runner=CommandRunner(), token_provider=installation_token_provider(config))
    assert github._token_provider is not None
    assert github._token_provider.__self__._app_id == "123"
