"""Tests for kobo_api.clients: real-client argv/request construction (via a
RecordingRunner and httpx.MockTransport — no real subprocesses or network),
plus structural checks that the fakes satisfy the Protocols.
"""

from __future__ import annotations

import asyncio
import base64
import collections.abc
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
    CabalClient,
    CabalError,
    CachedGcloudToken,
    _google_access_token,
    _metadata_access_token,
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
    FileSlackTokenStore,
    HttpxSlackClient,
    HttpxStorageClient,
    ImageClient,
    KubeSlackTokenStore,
    KubectlAgentClient,
    KubectlCLI,
    KubectlClient,
    NameClient,
    SLACK_STORE_WRITE_ATTEMPTS,
    SlackApiError,
    SlackClient,
    StorageClient,
    StorageNotFound,
    VertexImageClient,
    VertexNameClient,
    _build_system_task_envelope,
    _build_task_envelope,
    _build_teach_envelope,
    _extract_json_array,
    _extract_json_object,
    _gcloud_print_access_token,
    _get_card,
    _post_task,
    _universe_from_groups,
    _wait_for_forwarded_port,
    _write_slack_tokens,
    cabal_enabled,
    cabal_operator_token,
    filter_cabal_event_stream,
    image_tag,
    installation_token_provider,
    probe_slack_token_store,
    slack_token_store,
)
from kobo_api.config import Config, GroupChoice
from kobo_api.groups import OktaGroupResolver, OktaPeopleSearch
from tests.fakes import (
    FakeAgent,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeImage,
    FakeKubectl,
    FakeName,
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


# --- GitHub (GhCLI) ----------------------------------------------------------


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


# --- GitHub (GhCLI intake methods: commit_shas_for_paths, is_ancestor, rev_parse_prefix) ----
#
# Added for intake (Task 3c: list_intake's commit history checks): three plumbing methods
# that were previously unexercised, now pinned to their exact argv and return-value contracts.


def test_is_ancestor_rc_128_reraises():
    # git merge-base --is-ancestor exits 128 for errors (bad refs, missing repo).
    # Any exit code other than 0 or 1 must re-raise, not swallow.
    runner = RecordingRunner(results=[(128, "", "fatal: invalid revision range")])
    client = GhCLI(runner=runner)

    with pytest.raises(ClientError) as exc_info:
        client.is_ancestor(Path("/tmp/repo"), "bad-ref", "other-bad-ref")

    assert exc_info.value.returncode == 128


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


# --- GitHub (GhCLI.rename_repo / archive_repo — Task 3: agent destruction) --------


def test_rename_repo_404_raises_repo_not_found_prefixed_error():
    # `gh api` on a 404 prints the API's own JSON error body to stdout and
    # writes its own status line to stderr as "gh: <message> (HTTP
    # <code>)" — confirmed against a real, authenticated `gh` in this
    # sandbox: `gh api -X PATCH repos/<a-repo-that-does-not-exist>` exits
    # 1 with stderr exactly "gh: Not Found (HTTP 404)".
    runner = RecordingRunner(
        results=[(1, '{"message":"Not Found","status":"404"}', "gh: Not Found (HTTP 404)")]
    )
    client = GhCLI(runner=runner)
    with pytest.raises(ClientError) as exc_info:
        client.rename_repo("acme/agent-gone", "archived-agent-gone-1")
    assert str(exc_info.value).startswith("repo not found: acme/agent-gone")


def test_archive_repo_404_raises_repo_not_found_prefixed_error():
    runner = RecordingRunner(
        results=[(1, '{"message":"Not Found","status":"404"}', "gh: Not Found (HTTP 404)")]
    )
    client = GhCLI(runner=runner)
    with pytest.raises(ClientError) as exc_info:
        client.archive_repo("acme/agent-gone")
    assert str(exc_info.value).startswith("repo not found: acme/agent-gone")


def test_rename_repo_non_404_failure_raises_plain_client_error_no_prefix():
    runner = RecordingRunner(results=[(1, "", "gh: Bad credentials (HTTP 401)")])
    client = GhCLI(runner=runner)
    with pytest.raises(ClientError) as exc_info:
        client.rename_repo("acme/agent-clio", "archived-agent-clio-1")
    assert not str(exc_info.value).startswith("repo not found:")


def test_archive_repo_non_404_failure_raises_plain_client_error_no_prefix():
    # Deliberately NOT a 403-shaped stderr here even though the real
    # already-archived case 403s (per the destruction design): GhCLI only
    # special-cases the 404 status marker; recognizing the "already
    # archived" 403 shape is the destroy step's job (Task 4), not this
    # transport method's — this pins that archive_repo raises the plain
    # underlying error for ANY non-404 failure, 403 included.
    runner = RecordingRunner(
        results=[(1, "", "gh: Repository was archived so is read-only (HTTP 403)")]
    )
    client = GhCLI(runner=runner)
    with pytest.raises(ClientError) as exc_info:
        client.archive_repo("acme/archived-agent-clio-1")
    assert not str(exc_info.value).startswith("repo not found:")
    assert "read-only" in str(exc_info.value)


# --- GitHub (GhCLI.merge_pr — connected agents, design 2026-09-21) ----------------
#
# The room asks GitHub to merge its own wiring pull request. GitHub refusing
# that (a protected branch, a required review, a required check) is an
# ORDINARY answer, not a transport failure: the step turns it into one plain
# sentence naming the pull request, and a person merges it. So merge_pr
# answers False there and raises only for failures nobody can act on, the
# same "classify gh's stderr by its stable markers" discipline
# `_gh_returned_404` and `destroy._is_already_archived` already hold.

REVIEW_REQUIRED_STDERR = (
    "GraphQL: At least 1 approving review is required by reviewers with "
    "write access. (mergePullRequest)"
)


@pytest.mark.parametrize(
    "stderr",
    [
        "X https://github.com/acme/clio/pull/4 is already merged",
        (
            "GraphQL: Pull request Pull request has already been merged. "
            "(mergePullRequest)"
        ),
    ],
)
def test_merge_pr_true_when_the_pull_request_was_already_merged(stderr):
    runner = RecordingRunner(results=[(1, "", stderr)])
    client = GhCLI(runner=runner)

    assert client.merge_pr("acme/clio", "https://github.com/acme/clio/pull/4") is True


def test_merge_pr_falls_back_to_squash_then_rebase_when_a_method_is_not_allowed():
    """A repository that allows only one merge method refuses the others by
    name. The operator ruling is that the room merges by default, so it
    asks the ways the repository might accept rather than giving up on the
    first no."""
    runner = RecordingRunner(
        results=[
            (1, "", "GraphQL: Merge commits are not allowed on this repository."),
            (1, "", "GraphQL: Squash merges are not allowed on this repository."),
            (0, "", ""),
        ]
    )
    client = GhCLI(runner=runner)

    merged = client.merge_pr("acme/clio", "https://github.com/acme/clio/pull/4")

    assert merged is True
    assert [call[-1] for call in runner.calls] == ["--merge", "--squash", "--rebase"]


def test_merge_pr_false_when_the_repository_allows_no_method_the_room_can_use():
    runner = RecordingRunner(
        results=[
            (1, "", "GraphQL: Merge commits are not allowed on this repository."),
            (1, "", "GraphQL: Squash merges are not allowed on this repository."),
            (1, "", "GraphQL: Rebase merges are not allowed on this repository."),
        ]
    )
    client = GhCLI(runner=runner)

    assert client.merge_pr("acme/clio", "https://github.com/acme/clio/pull/4") is False


def test_merge_pr_stops_asking_once_a_method_is_refused_for_another_reason():
    """A method the repository does allow, refused on the pull request's own
    merits, is the answer for every other method too: asking again would
    only add noise to somebody else's repository."""
    runner = RecordingRunner(
        results=[
            (1, "", "GraphQL: Merge commits are not allowed on this repository."),
            (1, "", REVIEW_REQUIRED_STDERR),
        ]
    )
    client = GhCLI(runner=runner)

    assert client.merge_pr("acme/clio", "https://github.com/acme/clio/pull/4") is False
    assert [call[-1] for call in runner.calls] == ["--merge", "--squash"]


@pytest.mark.parametrize(
    "stderr",
    [
        "GraphQL: Pull request is not mergeable (mergePullRequest)",
        (
            "GraphQL: Pull request is not mergeable: the base branch policy "
            "prohibits the merge. (mergePullRequest)"
        ),
        REVIEW_REQUIRED_STDERR,
        "GraphQL: Required status check \"build\" is expected. (mergePullRequest)",
        "GraphQL: Changes must be approved: review is required (mergePullRequest)",
        "gh: Protected branch update failed (HTTP 405)",
        (
            "GraphQL: Pull request is in a merge queue and cannot be merged "
            "directly (mergePullRequest)"
        ),
    ],
)
def test_merge_pr_false_when_github_refuses_the_merge(stderr):
    runner = RecordingRunner(results=[(1, "", stderr)])
    client = GhCLI(runner=runner)

    assert client.merge_pr("acme/clio", "https://github.com/acme/clio/pull/4") is False


def test_merge_pr_raises_for_a_failure_nobody_can_merge_their_way_out_of():
    runner = RecordingRunner(results=[(1, "", "gh: Bad credentials (HTTP 401)")])
    client = GhCLI(runner=runner)

    with pytest.raises(ClientError):
        client.merge_pr("acme/clio", "https://github.com/acme/clio/pull/4")


def test_merge_pr_classifies_on_stderr_alone_never_the_command_line():
    """A ClientError's message embeds the argv and the pull request URL, so a
    pull request whose branch happens to be named "not-mergeable" would
    classify itself if the message were read. Only what GitHub said counts."""
    runner = RecordingRunner(
        results=[(1, "", "gh: Bad credentials (HTTP 401)")]
    )
    client = GhCLI(runner=runner)

    with pytest.raises(ClientError):
        client.merge_pr("acme/clio", "https://github.com/acme/clio/pull/not-mergeable")


# --- GitHub (GhCLI.branch_exists — connected agents, review fix) -----------------


def test_branch_exists_raises_for_any_other_failure():
    """Unlike repo_exists, this never collapses every failure to False: a
    401 answered "no branch here" would send the caller straight into a
    push that then fails for a reason it has already ruled out."""
    runner = RecordingRunner(results=[(1, "", "gh: Bad credentials (HTTP 401)")])
    client = GhCLI(runner=runner)

    with pytest.raises(ClientError):
        client.branch_exists("acme/clio", "kobo/wire")


# --- GitHub (reading a repository without cloning it) ---------------------------
#
# The connect scan answers four questions about a repository the room does
# not own, before anything has been cloned and before a row exists. Every
# one of them is a `gh api` read, and every one of them has the same
# discipline: "the App cannot see it" is an ANSWER (visible=False, no file,
# no entries, not protected), never an exception, because a person typing a
# repository name into a form gets a report, not a stack trace. Only a
# failure that says nothing about this repository (bad credentials, an
# unreachable API) still raises.

REPO_JSON = (
    '{"full_name":"acme/clio","default_branch":"main",'
    '"permissions":{"admin":false,"push":true,"pull":true}}'
)
NOT_FOUND = (1, '{"message":"Not Found"}', "gh: Not Found (HTTP 404)")
FORBIDDEN = (1, '{"message":"Resource not accessible"}', "gh: Resource not accessible by integration (HTTP 403)")
BAD_CREDENTIALS = (1, "", "gh: Bad credentials (HTTP 401)")


def test_repo_access_argv_and_a_writable_repo():
    runner = RecordingRunner(results=[(0, REPO_JSON, "")])
    client = GhCLI(runner=runner)

    access = client.repo_access("acme/clio")

    assert runner.calls == [["gh", "api", "repos/acme/clio"]]
    assert access.visible is True
    assert access.can_write is True
    assert access.default_branch == "main"
    assert access.full_name == "acme/clio"


def test_repo_access_read_only_when_github_says_push_is_false():
    runner = RecordingRunner(
        results=[(0, '{"default_branch":"trunk","permissions":{"push":false}}', "")]
    )
    client = GhCLI(runner=runner)

    access = client.repo_access("acme/clio")

    assert access.visible is True
    assert access.can_write is False
    assert access.default_branch == "trunk"


def test_repo_access_as_an_app_trusts_the_installations_grant_not_push():
    """GitHub answers an App's repository read with `permissions` all False,
    `push` and `pull` alike, even on a repository the App created (a client org,
    2026-09-25: nine of them). Reading `push` refused every repository. As an
    App the verdict is the installation's own contents grant."""
    runner = RecordingRunner(
        results=[
            (
                0,
                (
                    '{"default_branch":"main","full_name":"acme/clio",'
                    '"permissions":{"admin":false,"pull":false,"push":false}}'
                ),
                "",
            )
        ]
    )
    client = GhCLI(
        runner=runner, token_provider=lambda: "ghs_x", installation_writes=lambda: True
    )

    access = client.repo_access("acme/clio")

    assert access.visible is True
    assert access.can_write is True
    assert access.default_branch == "main"


def test_repo_access_as_an_app_without_the_contents_grant_is_read_only():
    runner = RecordingRunner(results=[(0, REPO_JSON, "")])  # push:true, and irrelevant
    client = GhCLI(
        runner=runner, token_provider=lambda: "ghs_x", installation_writes=lambda: False
    )

    assert client.repo_access("acme/clio").can_write is False


def test_repo_access_not_visible_on_a_404():
    runner = RecordingRunner(results=[NOT_FOUND])
    client = GhCLI(runner=runner)

    access = client.repo_access("acme/private-thing")

    assert access.visible is False
    assert access.can_write is False
    assert access.default_branch == ""
    assert access.full_name == ""


def test_repo_access_not_visible_on_a_403():
    """GitHub answers "you may not look at this" as often as it answers
    "there is nothing here", and the room can do nothing with either. An
    unclassified 403 would otherwise reach a person as a 500."""
    runner = RecordingRunner(results=[FORBIDDEN])
    client = GhCLI(runner=runner)

    access = client.repo_access("acme/private-thing")

    assert access.visible is False
    assert access.can_write is False


def test_repo_access_raises_for_a_failure_that_is_not_about_this_repo():
    runner = RecordingRunner(results=[BAD_CREDENTIALS])
    client = GhCLI(runner=runner)

    with pytest.raises(ClientError):
        client.repo_access("acme/clio")


def test_read_file_argv_and_decoded_content():
    body = base64.b64encode(b"FROM registry.example/agents/base\n").decode()
    runner = RecordingRunner(
        results=[
            (0, f'{{"type":"file","size":33,"encoding":"base64","content":"{body}"}}', "")
        ]
    )
    client = GhCLI(runner=runner)

    text = client.read_file("acme/clio", "Dockerfile")

    assert runner.calls == [["gh", "api", "repos/acme/clio/contents/Dockerfile"]]
    assert text == "FROM registry.example/agents/base\n"


def test_read_file_none_when_the_file_is_not_there():
    runner = RecordingRunner(results=[NOT_FOUND])
    client = GhCLI(runner=runner)

    assert client.read_file("acme/clio", "Dockerfile") is None


def test_read_file_none_for_a_file_over_the_size_cap():
    """The reader has one job: small text files the scan reasons about. A
    huge one is refused before its base64 is ever decoded into memory."""
    runner = RecordingRunner(
        results=[
            (0, '{"type":"file","size":9999999,"encoding":"base64","content":"aGk="}', "")
        ]
    )
    client = GhCLI(runner=runner)

    assert client.read_file("acme/clio", "Dockerfile") is None


def test_read_file_none_for_anything_that_is_not_a_plain_file():
    """A symlink and a submodule both come back with content of their own
    that is not the file anyone asked for. Only a `file` is a file."""
    body = base64.b64encode(b"../elsewhere/Dockerfile").decode()
    runner = RecordingRunner(
        results=[
            (0, f'{{"type":"symlink","size":23,"encoding":"base64","content":"{body}"}}', "")
        ]
    )
    client = GhCLI(runner=runner)

    assert client.read_file("acme/clio", "Dockerfile") is None


def test_read_file_none_on_a_403():
    runner = RecordingRunner(results=[FORBIDDEN])
    client = GhCLI(runner=runner)

    assert client.read_file("acme/clio", "Dockerfile") is None


def test_read_file_raises_for_a_failure_that_is_not_about_this_file():
    runner = RecordingRunner(results=[BAD_CREDENTIALS])
    client = GhCLI(runner=runner)

    with pytest.raises(ClientError):
        client.read_file("acme/clio", "Dockerfile")


def test_list_root_argv_and_names():
    runner = RecordingRunner(results=[(0, "Dockerfile\nk8s\n.env\n", "")])
    client = GhCLI(runner=runner)

    names = client.list_root("acme/clio")

    assert runner.calls == [
        ["gh", "api", "repos/acme/clio/contents/", "--jq", ".[].name"]
    ]
    assert names == ["Dockerfile", "k8s", ".env"]


def test_list_root_empty_for_a_repo_with_nothing_in_it():
    """An empty repository has no root tree at all, and GitHub answers the
    contents endpoint with a 404. Nothing there is exactly the answer."""
    runner = RecordingRunner(results=[NOT_FOUND])
    client = GhCLI(runner=runner)

    assert client.list_root("acme/clio") == []


def test_list_root_empty_on_a_403():
    runner = RecordingRunner(results=[FORBIDDEN])
    client = GhCLI(runner=runner)

    assert client.list_root("acme/clio") == []


def test_list_root_raises_for_a_failure_that_is_not_about_this_repo():
    runner = RecordingRunner(results=[BAD_CREDENTIALS])
    client = GhCLI(runner=runner)

    with pytest.raises(ClientError):
        client.list_root("acme/clio")


def test_branch_protected_false_when_there_is_no_such_branch():
    runner = RecordingRunner(results=[NOT_FOUND])
    client = GhCLI(runner=runner)

    assert client.branch_protected("acme/clio", "main") is False


def test_branch_protected_false_on_a_403():
    """Branch protection is readable only with some permissions. Not being
    allowed to look is not a reason to fail a scan: the warning it would
    have raised is the mildest thing in the report."""
    runner = RecordingRunner(results=[FORBIDDEN])
    client = GhCLI(runner=runner)

    assert client.branch_protected("acme/clio", "main") is False


def test_branch_protected_encodes_a_branch_name_with_a_slash_in_it():
    """`release/main` is one path segment, not two, and an unencoded slash
    would ask GitHub about a branch called "release" instead."""
    runner = RecordingRunner(results=[(0, "true", "")])
    client = GhCLI(runner=runner)

    assert client.branch_protected("acme/clio", "release/main") is True
    assert runner.calls == [
        [
            "gh", "api", "repos/acme/clio/branches/release%2Fmain",
            "--jq", ".protected",
        ]
    ]


def test_branch_protected_raises_for_a_failure_that_is_not_about_this_branch():
    runner = RecordingRunner(results=[BAD_CREDENTIALS])
    client = GhCLI(runner=runner)

    with pytest.raises(ClientError):
        client.branch_protected("acme/clio", "main")


def _installation_page(names: list[str], **overrides) -> str:
    """One `GET /installation/repositories` page, in GitHub's own shape."""
    repositories = [
        {
            "full_name": name,
            "archived": overrides.get("archived", False),
            "private": overrides.get("private", True),
            "default_branch": overrides.get("default_branch", "main"),
            "pushed_at": overrides.get("pushed_at", "2026-09-01T00:00:00Z"),
        }
        for name in names
    ]
    return json.dumps({"total_count": len(repositories), "repositories": repositories})


def test_list_installation_repos_argv_and_fields():
    runner = RecordingRunner(
        results=[(0, _installation_page(["example-org/clio"], archived=True), "")]
    )
    client = GhCLI(runner=runner)

    repos = client.list_installation_repos()

    assert runner.calls == [
        ["gh", "api", "installation/repositories?per_page=100&page=1"]
    ]
    assert len(repos) == 1
    assert repos[0].full_name == "example-org/clio"
    assert repos[0].archived is True
    assert repos[0].private is True
    assert repos[0].default_branch == "main"
    assert repos[0].pushed_at == "2026-09-01T00:00:00Z"


def test_list_installation_repos_follows_pages_until_a_short_one():
    """A full page means there may be another; a short page is the end. The
    installation can see more repositories than one page holds, and a
    typeahead over the first hundred alphabetically would quietly hide the
    rest."""
    first = _installation_page([f"example-org/repo-{i:03d}" for i in range(100)])
    second = _installation_page(["example-org/last-one"])
    runner = RecordingRunner(results=[(0, first, ""), (0, second, "")])
    client = GhCLI(runner=runner)

    repos = client.list_installation_repos()

    assert runner.calls == [
        ["gh", "api", "installation/repositories?per_page=100&page=1"],
        ["gh", "api", "installation/repositories?per_page=100&page=2"],
    ]
    assert len(repos) == 101
    assert repos[-1].full_name == "example-org/last-one"


def test_list_installation_repos_raises_when_github_will_not_answer():
    """Not a finding about any repository: an App-less token cannot ask this
    at all, and answering "no repositories" would read as an empty room."""
    runner = RecordingRunner(results=[BAD_CREDENTIALS])
    client = GhCLI(runner=runner)

    with pytest.raises(ClientError):
        client.list_installation_repos()


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


# --- Cloud teardown (remove_from_group / delete_gsa) --------------------------


def test_remove_from_group_removes_and_returns_true():
    runner = RecordingRunner()
    client = GcloudClient("example-project", runner=runner)
    removed = client.remove_from_group(
        "agent-clio@example-project.iam.gserviceaccount.com", "models@example.com"
    )
    assert removed is True
    assert runner.calls == [
        [
            "gcloud", "identity", "groups", "memberships", "delete",
            "--group-email", "models@example.com",
            "--member-email", "agent-clio@example-project.iam.gserviceaccount.com",
            "--quiet",
        ]
    ]


def test_remove_from_group_absent_membership_returns_false_without_raising():
    # Grounded in the installed gcloud SDK source (not run live; the
    # installed google-cloud-sdk under ~/Workspace/google-cloud-sdk has the
    # Python source for this command tree). `gcloud identity groups
    # memberships delete` resolves --group-email/--member-email into a
    # membership resource name before deleting
    # (googlecloudsdk/command_lib/identity/groups/memberships/hooks.py,
    # SetMembershipResourceName -> ConvertEmailToMembershipResourceName).
    # That lookup's HttpNotFoundError (absent membership) is caught and
    # re-raised as a calliope InvalidArgumentException with this exact,
    # fixed message: "There is no such membership associated with the
    # specified arguments: <group-email>, <member-email>" (same function,
    # its except clause). Calliope then renders any known exception as
    # "ERROR: (<command.path>) <message>" (calliope/exceptions.py,
    # _LogKnownError), giving the stderr text pinned below.
    stderr = (
        "ERROR: (gcloud.identity.groups.memberships.delete) Invalid value "
        "for [--group-email, --member-email]: There is no such membership "
        "associated with the specified arguments: models@example.com, "
        "agent-clio@example-project.iam.gserviceaccount.com"
    )
    runner = RecordingRunner(results=[(1, "", stderr)])
    client = GcloudClient("example-project", runner=runner)
    removed = client.remove_from_group(
        "agent-clio@example-project.iam.gserviceaccount.com", "models@example.com"
    )
    assert removed is False


def test_remove_from_group_permission_denied_raises():
    # The lookup-stage collapse above only fires before the delete request
    # itself; a real permission problem on the actual delete call surfaces
    # as a plain HttpException with no "There is no such membership" text,
    # so it must still raise rather than being swallowed as absent.
    runner = RecordingRunner(
        results=[(1, "", "ERROR: (gcloud.identity.groups.memberships.delete) PERMISSION_DENIED")]
    )
    client = GcloudClient("example-project", runner=runner)
    with pytest.raises(ClientError):
        client.remove_from_group(
            "agent-clio@example-project.iam.gserviceaccount.com", "models@example.com"
        )


def test_delete_gsa_deletes_and_returns_true():
    runner = RecordingRunner(
        results=[(0, "agent-clio@example-project.iam.gserviceaccount.com", ""), (0, "", "")]
    )
    client = GcloudClient("example-project", runner=runner)
    deleted = client.delete_gsa("agent-clio@example-project.iam.gserviceaccount.com")
    assert deleted is True
    assert runner.calls == [
        [
            "gcloud", "iam", "service-accounts", "describe",
            "agent-clio@example-project.iam.gserviceaccount.com",
            "--project", "example-project",
        ],
        [
            "gcloud", "iam", "service-accounts", "delete",
            "agent-clio@example-project.iam.gserviceaccount.com",
            "--project", "example-project", "--quiet",
        ],
    ]


def test_delete_gsa_absent_returns_false():
    # Grounded in the installed gcloud SDK source
    # (~/Workspace/google-cloud-sdk/lib/surface/iam/service_accounts/delete.py,
    # Delete's own docstring): "If the service account does not exist, this
    # command returns a `PERMISSION_DENIED` error." Delete's own not-found
    # signal is therefore NOT distinguishable from a real permission
    # problem by stderr text alone, so delete_gsa describes first, reusing
    # ensure_gsa's already-grounded describe-call discriminator
    # ("not_found"/"does not exist" in stderr, same real error text used by
    # ensure_gsa's own tests) and only issues the delete once describe
    # confirms the GSA exists. This mirrors gojira's delete_service_account
    # (gojira/gcp.py), which takes the identical describe-then-delete shape
    # for the same documented reason.
    stderr = (
        "ERROR: (gcloud.iam.service-accounts.describe) NOT_FOUND: "
        "Service account agent-clio@example-project.iam.gserviceaccount.com "
        "does not exist."
    )
    runner = RecordingRunner(results=[(1, "", stderr)])
    client = GcloudClient("example-project", runner=runner)
    deleted = client.delete_gsa("agent-clio@example-project.iam.gserviceaccount.com")
    assert deleted is False
    assert len(runner.calls) == 1  # describe only, no delete issued


def test_delete_gsa_permission_denied_raises():
    runner = RecordingRunner(results=[(1, "", "ERROR: (gcloud...) PERMISSION_DENIED")])
    client = GcloudClient("example-project", runner=runner)
    with pytest.raises(ClientError):
        client.delete_gsa("agent-clio@example-project.iam.gserviceaccount.com")
    assert len(runner.calls) == 1  # never got to (or attempted) delete


# --- grant_bucket_prefix_write (resilience arc, Task 14; Important 9 fix) ---
#
# A per-prefix conditioned roles/storage.objectAdmin binding alone denies
# storage.objects.list -- list is a bucket-level operation, not scopable by
# the object-name CEL condition the way get/create/delete are -- and
# litestream needs to list its replica prefix's existing generations on
# startup. This method now issues TWO bindings: the original conditioned
# per-prefix write grant, plus an unconditioned, bucket-wide, list-only
# roles/storage.legacyBucketReader grant.


def test_grant_bucket_prefix_write_issues_conditioned_object_admin_binding():
    runner = RecordingRunner()
    client = GcloudClient("example-project", runner=runner)
    client.grant_bucket_prefix_write(
        "agent-clio@example-project.iam.gserviceaccount.com", "kobo-memory-replicas", "clio/"
    )
    assert runner.calls[0] == [
        "gcloud", "storage", "buckets", "add-iam-policy-binding",
        "gs://kobo-memory-replicas",
        "--member", "serviceAccount:agent-clio@example-project.iam.gserviceaccount.com",
        "--role", "roles/storage.objectAdmin",
        "--condition",
        "expression="
        'resource.name.startsWith("projects/_/buckets/kobo-memory-replicas/objects/clio/")'
        ",title=agent-memory-replica-clio",
    ]


def test_grant_bucket_prefix_write_also_issues_an_unconditioned_bucket_wide_list_binding():
    runner = RecordingRunner()
    client = GcloudClient("example-project", runner=runner)
    client.grant_bucket_prefix_write(
        "agent-clio@example-project.iam.gserviceaccount.com", "kobo-memory-replicas", "clio/"
    )
    assert len(runner.calls) == 2
    assert runner.calls[1] == [
        "gcloud", "storage", "buckets", "add-iam-policy-binding",
        "gs://kobo-memory-replicas",
        "--member", "serviceAccount:agent-clio@example-project.iam.gserviceaccount.com",
        "--role", "roles/storage.legacyBucketReader",
        "--condition", "None",
    ]
    # Explicitly unconditioned: the first call above already put a
    # conditional binding on this policy, so gcloud's conditional-binding
    # semantics require every later add-iam-policy-binding to state its
    # condition explicitly -- omitting the flag makes gcloud PROMPT
    # ("Binding condition:"), and run non-interactively (subprocess, no
    # TTY, as this client always is) an unanswered prompt is an ERROR, not
    # a default. `--condition=None` is gcloud's own token for "explicitly
    # no condition", not this codebase's convention.


def test_gsa_email_for_composes_without_any_gcloud_call():
    # Pure composition (destroy-access, kobo_api.destroy, needs the SAME
    # email ensure_gsa would return, without creating anything) — never
    # shells out at all, unlike ensure_gsa's own describe-then-maybe-create.
    runner = RecordingRunner(results=[])
    client = GcloudClient("example-project", runner=runner)

    email = client.gsa_email_for("agent-clio")

    assert email == "agent-clio@example-project.iam.gserviceaccount.com"
    assert runner.calls == []


def test_gsa_email_for_parity_with_ensure_gsa():
    # Same email ensure_gsa would return (or create) for the same name —
    # the whole point of the shared gsa_email() composition both delegate
    # to, so destroy-access can never drift from what AccessStep granted.
    runner = RecordingRunner(
        results=[(0, "agent-clio@example-project.iam.gserviceaccount.com", "")]
    )
    client = GcloudClient("example-project", runner=runner)

    ensured = client.ensure_gsa("agent-clio")
    derived = client.gsa_email_for("agent-clio")

    assert derived == ensured == "agent-clio@example-project.iam.gserviceaccount.com"


# --- Kubectl (KubectlCLI) -----------------------------------------------------


# --- KubectlCLI.external_secret_status (the Secrets panel's lock) --------------


def test_external_secret_status_is_none_when_there_is_no_such_object():
    # `--ignore-not-found` prints nothing and succeeds, which is what
    # lets "nothing is delivering this" be an answer rather than a
    # failure the room would have to tell apart from an unreachable
    # cluster.
    runner = RecordingRunner(results=[(0, "", "")])
    client = KubectlCLI(runner=runner)

    assert client.external_secret_status("clio", "clio-env") is None


def test_external_secret_status_raises_when_the_cluster_cannot_be_asked():
    runner = RecordingRunner(results=[(1, "", "Unable to connect to the server")])
    client = KubectlCLI(runner=runner)

    with pytest.raises(ClientError):
        client.external_secret_status("clio", "clio-env")


def test_external_secret_status_says_what_it_could_not_read_and_not_what_was_in_it():
    runner = RecordingRunner(results=[(0, "not-a-real-key-value", "")])
    client = KubectlCLI(runner=runner)

    with pytest.raises(ClientError) as exc_info:
        client.external_secret_status("clio", "clio-env")

    assert "clio/clio-env" in str(exc_info.value)
    assert "not-a-real-key-value" not in str(exc_info.value)


# --- KubectlCLI.secret_literals (two-replica safety: the Slack token store) ----
#
# Obviously-fake token literals only. `kubectl get secret -o json` puts the
# Secret's base64 values on STDOUT, so unlike every other read above, this
# one's stdout is secret-bearing: nothing here may log it or let it reach a
# raised exception (CommandRunner's own failure message embeds `stderr or
# stdout`, so the failure path has to be re-raised, unchained).

SECRET_CONFIG_TOKEN = "xoxe.xoxp-TEST-config"
SECRET_REFRESH_TOKEN = "xoxe-TEST-refresh"


def _secret_get_json(**literals: str) -> str:
    data = {
        key: base64.b64encode(value.encode()).decode() for key, value in literals.items()
    }
    return json.dumps({"kind": "Secret", "metadata": {"name": "kobo-slack-tokens"}, "data": data})


def test_secret_literals_argv_and_base64_decode():
    runner = RecordingRunner(
        results=[
            (
                0,
                _secret_get_json(
                    config_token=SECRET_CONFIG_TOKEN, refresh_token=SECRET_REFRESH_TOKEN
                ),
                "",
            )
        ]
    )
    client = KubectlCLI(runner=runner)

    assert client.secret_literals("kobo", "kobo-slack-tokens") == {
        "config_token": SECRET_CONFIG_TOKEN,
        "refresh_token": SECRET_REFRESH_TOKEN,
    }
    assert runner.calls == [
        ["kubectl", "-n", "kobo", "get", "secret", "kobo-slack-tokens", "-o", "json"]
    ]


def test_secret_literals_missing_secret_raises_naming_only_namespace_and_name():
    runner = RecordingRunner(
        results=[(1, "", 'Error from server (NotFound): secrets "kobo-slack-tokens" not found')]
    )
    client = KubectlCLI(runner=runner)

    with pytest.raises(ClientError) as exc_info:
        client.secret_literals("kobo", "kobo-slack-tokens")

    assert "kobo/kobo-slack-tokens" in str(exc_info.value)


def test_secret_literals_failure_cannot_leak_stdout_into_the_raised_error():
    # CommandRunner.run's own ClientError message is `f"... failed (rc=N):
    # {stderr or stdout}"` and it carries stdout/stderr on the exception --
    # for THIS command, stdout is the Secret's (base64) values. A kubectl
    # that fails with an empty stderr after already printing part of the
    # JSON must not put any of it into the error, its attributes, or the
    # chained cause a traceback would print.
    leaky_stdout = _secret_get_json(
        config_token=SECRET_CONFIG_TOKEN, refresh_token=SECRET_REFRESH_TOKEN
    )
    encoded = base64.b64encode(SECRET_REFRESH_TOKEN.encode()).decode()
    runner = RecordingRunner(results=[(1, leaky_stdout, "")])
    client = KubectlCLI(runner=runner)

    with pytest.raises(ClientError) as exc_info:
        client.secret_literals("kobo", "kobo-slack-tokens")

    error = exc_info.value
    for leak in (leaky_stdout, encoded, SECRET_REFRESH_TOKEN, SECRET_CONFIG_TOKEN):
        assert leak not in str(error)
        assert leak not in error.stdout
        assert leak not in error.stderr
        assert leak not in str(error.__cause__ or "")
    # The original (whose message embeds that stdout) is still the implicit
    # context, as it is for any raise inside an except block -- what keeps
    # it out of a printed traceback is the `from None` suppression.
    assert error.__cause__ is None
    assert error.__suppress_context__ is True


def test_apply_secret_failure_cannot_leak_literals_through_the_traceback():
    # Stage 1's argv is the one place the literal values exist, and the
    # runner's own error message embeds that argv; its stdout is the
    # rendered manifest. Neither may reach the message, the attributes, or
    # a cause a traceback would print.
    import traceback

    # Built at runtime: a traceback prints the source of the calling line, so
    # a value spelled out there would show up in `printed` on its own.
    parts = ["secret", "value"]
    value = "-".join(parts)
    encoded = base64.b64encode(value.encode()).decode()
    literals = {"TOKEN": value}
    runner = RecordingRunner(results=[(1, f"data:\n  TOKEN: {encoded}\n", "boom")])
    client = KubectlCLI(runner=runner)

    with pytest.raises(ClientError) as exc_info:
        client.apply_secret("kobo", "kobo-slack-tokens", literals)

    error = exc_info.value
    printed = "".join(traceback.format_exception(error))
    for leak in (value, encoded):
        assert leak not in str(error)
        assert leak not in error.stdout
        assert leak not in printed
    assert error.__cause__ is None
    assert error.__suppress_context__ is True


def test_secret_literals_unparsable_output_raises_naming_only_namespace_and_name():
    runner = RecordingRunner(results=[(0, "not json at all", "")])
    client = KubectlCLI(runner=runner)

    with pytest.raises(ClientError) as exc_info:
        client.secret_literals("kobo", "kobo-slack-tokens")

    assert "kobo/kobo-slack-tokens" in str(exc_info.value)
    assert "not json at all" not in str(exc_info.value)


def test_secret_literals_undecodable_value_raises_without_the_value():
    runner = RecordingRunner(
        results=[(0, json.dumps({"data": {"config_token": "!!!not base64!!!"}}), "")]
    )
    client = KubectlCLI(runner=runner)

    with pytest.raises(ClientError) as exc_info:
        client.secret_literals("kobo", "kobo-slack-tokens")

    assert "kobo/kobo-slack-tokens" in str(exc_info.value)
    assert "!!!not base64!!!" not in str(exc_info.value)


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


def test_gojira_create_app_carries_the_workload_class_it_is_given():
    """Connected agents (design 2026-09-21): a team's own code may have no
    HTTP port at all, and the platform's probing is chosen by this field --
    so it is an argument now, not a constant. The default stays "agent",
    which is what every composed agent has always sent."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(201, json={"id": 7})

    client = HttpxGojiraClient(
        "http://gojira.local", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    client.create_app(
        name="clio",
        repo_full_name="acme/clio",
        cluster="dev",
        namespace="clio",
        workload_class="agent-headless",
    )

    assert captured["json"]["workload_class"] == "agent-headless"


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


def test_gojira_sync_app_posts_api_route_with_bearer_and_returns_body():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "http://gojira.local/api/v1/apps/7/sync"
        assert request.headers["Authorization"] == "Bearer tok"
        return httpx.Response(200, json={"id": 7, "status": "active", "changed": True})

    client = HttpxGojiraClient(
        "http://gojira.local",
        token="tok",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert client.sync_app(7) == {"id": 7, "status": "active", "changed": True}


def test_gojira_sync_app_raises_on_unexpected_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    client = HttpxGojiraClient(
        "http://gojira.local", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(ClientError):
        client.sync_app(7)


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


def test_gojira_ping_false_on_non_200():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

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


# --- Gojira.delete_app (Task 3: agent destruction's platform-teardown step) ------


def test_gojira_delete_app_request_shape_default_delete_namespaces_true():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(202, json={"app": "clio", "rows_deleted": True})

    client = HttpxGojiraClient(
        "http://gojira.local", token="secret-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = client.delete_app(7)

    assert captured["method"] == "DELETE"
    assert captured["url"] == "http://gojira.local/api/v1/apps/7"
    assert captured["json"] == {"delete_namespaces": True}
    assert captured["auth"] == "Bearer secret-token"
    assert result == {"app": "clio", "rows_deleted": True}


def test_gojira_delete_app_404_maps_to_already_gone():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    client = HttpxGojiraClient(
        "http://gojira.local", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert client.delete_app(7) == {"already_gone": True}


def test_gojira_delete_app_404_with_html_body_raises_client_error():
    # A route-missing or proxy/gateway 404 carries no gojira JSON body at
    # all — it is not gojira's own "unknown app id" signal and must not be
    # read as already-gone.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="<html><body>Not Found</body></html>")

    client = HttpxGojiraClient(
        "http://gojira.local", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(ClientError):
        client.delete_app(7)


def test_gojira_delete_app_raises_client_error_on_unexpected_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = HttpxGojiraClient(
        "http://gojira.local", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(ClientError):
        client.delete_app(7)


def test_gojira_delete_app_overrides_client_default_with_120s_timeout():
    # Final-review Finding A: the client's default timeout (30.0, connect
    # 10.0 - see HttpxGojiraClient.__init__) is too tight for a real
    # teardown (gcloud/kubectl on gojira's side, realistically 15-60s); a
    # per-call override on this one request gives it the headroom without
    # loosening every other Gojira call's timeout.
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["timeout"] = request.extensions.get("timeout")
        return httpx.Response(202, json={"app": "clio"})

    client = HttpxGojiraClient(
        "http://gojira.local", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    client.delete_app(7)

    assert captured["timeout"] == {"connect": 10.0, "read": 120.0, "write": 120.0, "pool": 120.0}


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
    # A single message is the CURRENT turn -> status.message, not history.
    # The switchboard's Task model requires `status`; its convention is
    # that `status.message` carries the latest turn while `history` holds
    # only what came before it.
    envelope = _build_task_envelope([{"role": "user", "text": "are you there?"}])
    assert envelope["kind"] == "task"
    assert envelope["id"].startswith("chat-")
    assert len(envelope["id"]) == len("chat-") + 32  # uuid4().hex
    assert envelope["status"] == {
        "state": "submitted",
        "message": {
            "kind": "message",
            "role": "user",
            "parts": [{"kind": "text", "text": "are you there?"}],
        },
    }
    assert envelope["history"] == []


def test_build_task_envelope_shape_multi_turn():
    # Two user turns plus one agent reply in between -> the LAST message
    # (the current turn) lands in status.message; the two PRIOR turns land
    # in history, same order, each mapped to the verified message shape.
    messages: list[ChatMessage] = [
        {"role": "user", "text": "hi"},
        {"role": "agent", "text": "hello yourself"},
        {"role": "user", "text": "how are you?"},
    ]
    envelope = _build_task_envelope(messages)
    assert envelope["status"] == {
        "state": "submitted",
        "message": {
            "kind": "message",
            "role": "user",
            "parts": [{"kind": "text", "text": "how are you?"}],
        },
    }
    assert envelope["history"] == [
        {"kind": "message", "role": "user", "parts": [{"kind": "text", "text": "hi"}]},
        {"kind": "message", "role": "agent", "parts": [{"kind": "text", "text": "hello yourself"}]},
    ]


def test_build_task_envelope_carries_context_id_when_given():
    env = _build_task_envelope([{"role": "user", "text": "hi"}], context_id="ctx-abc")
    assert env["contextId"] == "ctx-abc"


def test_build_task_envelope_omits_context_id_by_default():
    """The direct path and HelloStep must keep their exact envelope."""
    env = _build_task_envelope([{"role": "user", "text": "hi"}])
    assert "contextId" not in env


def test_relay_envelope_context_id_is_the_key_the_switchboard_reads():
    """Contract pin. The switchboard's guard reads exactly "contextId".
    daedalus copies this key onto colleague hops (tools/colleagues.py).
    If this key is ever renamed, the loop guard silently stops working
    and no other test notices."""
    env = _build_task_envelope([{"role": "user", "text": "hi"}], context_id="ctx-1")
    assert "contextId" in env, "the guard reads contextId; any other spelling is inert"
    assert env["contextId"] == "ctx-1"


def test_build_teach_envelope_is_a_marked_single_message_task():
    envelope = _build_teach_envelope("Taught in chat.\n\nA fact.\n", "chat-20260820-a.md")
    assert envelope["kind"] == "task"
    assert envelope["status"]["state"] == "submitted"
    assert envelope["status"]["message"]["parts"][0]["text"] == "Taught in chat.\n\nA fact.\n"
    assert envelope["history"] == []
    assert envelope["metadata"] == {
        "system_task": "teach",
        "filename": "chat-20260820-a.md",
    }


def test_system_task_envelope_carries_the_marker_and_extra_metadata():
    env = _build_system_task_envelope("knowledge", "body text", metadata={"filename": "a.md"})
    assert env["metadata"] == {"system_task": "knowledge", "filename": "a.md"}
    assert env["status"]["message"]["parts"][0]["text"] == "body text"


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
    # single message -> the current turn, so it rides status.message; history
    # (only prior turns) is empty.
    assert captured["body"]["status"]["message"]["parts"][0]["text"] == "hello?"
    assert captured["body"]["history"] == []
    assert reply == "hi there"


def test_post_task_raises_on_non_200():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ClientError):
        _post_task(client, 54321, [{"role": "user", "text": "hello?"}])


def test_post_task_joins_all_text_parts_of_a_multi_part_reply():
    """The join-all-parts contract (daedalus #13 review): a multi-part reply
    arrives whole, non-text parts are skipped, order kept. parts[0] alone
    would have silently dropped everything after the first."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": {
                    "message": {
                        "parts": [
                            {"kind": "text", "text": "first half"},
                            {"kind": "file", "uri": "s3://irrelevant"},
                            {"kind": "text", "text": "second half"},
                        ]
                    }
                }
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    reply = _post_task(client, 54321, [{"role": "user", "text": "hello?"}])
    assert reply == "first half\nsecond half"


def test_post_task_raises_client_error_when_reply_has_no_text_parts():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"status": {"message": {"parts": [{"kind": "file", "uri": "s3://x"}]}}}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ClientError) as exc_info:
        _post_task(client, 54321, [{"role": "user", "text": "hello?"}])
    assert "no text parts" in str(exc_info.value)


def test_extract_relay_reply_joins_all_text_parts_within_a_source():
    from kobo_api.clients import _extract_relay_reply

    task = {
        "status": {
            "message": {
                "parts": [
                    {"kind": "text", "text": "the summary"},
                    {"kind": "text", "text": "the detail"},
                ]
            }
        }
    }
    assert _extract_relay_reply(task) == "the summary\nthe detail"


def test_extract_relay_reply_skips_a_source_with_only_non_text_parts():
    """A source whose parts carry no text falls through to the next source
    (previously only an empty list or a text-less parts[0] fell through)."""
    from kobo_api.clients import _extract_relay_reply

    task = {
        "status": {"message": {"parts": [{"kind": "file", "uri": "s3://x"}]}},
        "history": [
            {"role": "agent", "parts": [{"kind": "text", "text": "from history"}]}
        ],
    }
    assert _extract_relay_reply(task) == "from history"


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


def test_get_card_sends_get_to_well_known_path_and_returns_parsed_json():
    captured = {}
    card = {"enforcement": {"version": 1, "hooks": ["guard.py"], "locked": False}}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        return httpx.Response(200, json=card)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = _get_card(client, 54321)

    assert captured["method"] == "GET"
    assert captured["url"] == "http://127.0.0.1:54321/.well-known/agent-card.json"
    assert result == card


def test_get_card_raises_on_non_200():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ClientError):
        _get_card(client, 54321)


def test_get_card_raises_client_error_on_non_json_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ClientError) as exc_info:
        _get_card(client, 54321)

    assert "malformed agent card" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, ValueError)


def test_get_card_wraps_read_timeout_as_client_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("The read operation timed out", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ClientError) as exc_info:
        _get_card(client, 54321)

    assert isinstance(exc_info.value.__cause__, httpx.ReadTimeout)


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
    # carries the chat- id prefix; a single turn is the CURRENT turn, so it
    # rides status.message and history is empty.
    assert captured["body"]["id"].startswith("chat-")
    assert captured["body"]["status"]["message"] == {
        "kind": "message", "role": "user", "parts": [{"kind": "text", "text": "hello?"}]
    }
    assert captured["body"]["history"] == []
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
    # the LAST message is the current turn -> status.message; the two prior
    # turns land in history, same order.
    assert captured["body"]["status"]["message"] == {
        "kind": "message", "role": "user", "parts": [{"kind": "text", "text": "how are you?"}]
    }
    assert captured["body"]["history"] == [
        {"kind": "message", "role": "user", "parts": [{"kind": "text", "text": "hi"}]},
        {"kind": "message", "role": "agent", "parts": [{"kind": "text", "text": "hello yourself"}]},
    ]
    assert fake_proc.terminated is True
    assert fake_proc.waited is True


def test_fetch_card_gets_well_known_path_and_terminates_port_forward_on_success(monkeypatch):
    fake_proc = _FakePopen(["Forwarding from 127.0.0.1:54321 -> 80\n"])
    monkeypatch.setattr(
        "kobo_api.clients.subprocess.Popen", lambda *a, **k: fake_proc
    )
    captured = {}
    card = {"enforcement": {"version": 1, "hooks": ["guard.py"], "locked": False}}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        return httpx.Response(200, json=card)

    client = KubectlAgentClient(
        http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    result = client.fetch_card("clio", "clio")

    assert result == card
    assert captured["method"] == "GET"
    assert captured["url"] == "http://127.0.0.1:54321/.well-known/agent-card.json"
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


# --- Slack token stores (two-replica safety) ------------------------------------
#
# Two pods can hit the same expired config token at the same moment, and
# Slack's refresh token is single-use: the store is the only thing both
# pods share, so rotation reads it before spending a refresh token and
# again after a rotate fails, and converges on whichever pair won.


class _MemorySlackTokenStore:
    """A SlackTokenStore standing in for one both pods can see (the
    Kubernetes Secret in a real two-replica room). `pair` is settable
    straight from a request handler, which is how these tests express
    "the other process rotated while this call was in flight".

    `read_error`/`write_error`, when set, make that half raise instead --
    the store half of a rotation is two kubectl round trips against a real
    cluster, so both halves can fail on their own.
    """

    def __init__(
        self,
        config_token: str,
        refresh_token: str,
        *,
        read_error: str | None = None,
        write_error: str | None = None,
    ):
        self.pair = (config_token, refresh_token)
        self.writes: list[tuple[str, str]] = []
        self.read_error = read_error
        self.write_error = write_error

    def read(self) -> tuple[str, str]:
        if self.read_error is not None:
            raise ClientError(self.read_error)
        return self.pair

    def write(self, config_token: str, refresh_token: str) -> None:
        self.writes.append((config_token, refresh_token))
        if self.write_error is not None:
            raise ClientError(self.write_error)
        self.pair = (config_token, refresh_token)


def test_file_slack_token_store_round_trips_the_pair(tmp_path):
    tokens_path = _write_slack_tokens_file(
        tmp_path, config_token="xoxe.xoxp-TEST-config", refresh_token="xoxe-TEST-refresh"
    )
    store = FileSlackTokenStore(tokens_path)

    assert store.read() == ("xoxe.xoxp-TEST-config", "xoxe-TEST-refresh")

    store.write("xoxe.xoxp-TEST-next", "xoxe-TEST-next-refresh")

    assert store.read() == ("xoxe.xoxp-TEST-next", "xoxe-TEST-next-refresh")
    assert "# GITIGNORED" in tokens_path.read_text()


def test_kube_slack_token_store_reads_the_two_secret_keys():
    kubectl = FakeKubectl(
        secrets={
            ("kobo", "kobo-slack-tokens"): {
                "config_token": "xoxe.xoxp-TEST-config",
                "refresh_token": "xoxe-TEST-refresh",
            }
        }
    )
    store = KubeSlackTokenStore(kubectl, "kobo", "kobo-slack-tokens")

    assert store.read() == ("xoxe.xoxp-TEST-config", "xoxe-TEST-refresh")


def test_kube_slack_token_store_writes_both_keys_through_apply_secret():
    kubectl = FakeKubectl()
    store = KubeSlackTokenStore(kubectl, "kobo", "kobo-slack-tokens")

    store.write("xoxe.xoxp-TEST-new", "xoxe-TEST-new-refresh")

    assert kubectl.secrets_applied == [
        (
            "kobo",
            "kobo-slack-tokens",
            {
                "config_token": "xoxe.xoxp-TEST-new",
                "refresh_token": "xoxe-TEST-new-refresh",
            },
        )
    ]
    assert store.read() == ("xoxe.xoxp-TEST-new", "xoxe-TEST-new-refresh")


def test_kube_slack_token_store_missing_key_names_namespace_name_and_key_only():
    kubectl = FakeKubectl(
        secrets={("kobo", "kobo-slack-tokens"): {"config_token": "xoxe.xoxp-TEST-config"}}
    )
    store = KubeSlackTokenStore(kubectl, "kobo", "kobo-slack-tokens")

    with pytest.raises(ClientError) as exc_info:
        store.read()

    message = str(exc_info.value)
    assert "kobo/kobo-slack-tokens" in message
    assert "refresh_token" in message
    assert "xoxe.xoxp-TEST-config" not in message


def test_slack_rotation_skips_slack_when_the_store_already_holds_a_newer_token():
    # The other pod rotated between this pod's read and this pod's auth
    # failure. Its pair is already in the store and this pod's refresh
    # token is already spent -- calling rotate with it would burn the
    # single-use token the other pod just issued.
    store = _MemorySlackTokenStore("xoxe.xoxp-TEST-old-config", "xoxe-TEST-old-refresh")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path == "/api/apps.manifest.create":
            if request.headers.get("authorization") == "Bearer xoxe.xoxp-TEST-old-config":
                store.pair = ("xoxe.xoxp-TEST-other-config", "xoxe-TEST-other-refresh")
                return httpx.Response(200, json={"ok": False, "error": "token_expired"})
            assert (
                request.headers.get("authorization")
                == "Bearer xoxe.xoxp-TEST-other-config"
            )
            return httpx.Response(200, json={"ok": True, "app_id": "A0OTHER"})
        raise AssertionError(f"unexpected path {path}")

    client = HttpxSlackClient(
        store, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    assert client.create_app({"display_information": {"name": "clio"}}) == "A0OTHER"
    assert calls == ["/api/apps.manifest.create", "/api/apps.manifest.create"]
    assert store.writes == []


def test_slack_rotation_failure_returns_the_other_processs_token_when_the_store_moved():
    # Both pods called rotate with the same refresh token; the other pod's
    # call won and this one's lost (Slack rejects an already-spent refresh
    # token). The winner's pair is in the store, so this pod uses it
    # instead of failing the create.
    store = _MemorySlackTokenStore("xoxe.xoxp-TEST-old-config", "xoxe-TEST-old-refresh")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path == "/api/apps.manifest.create":
            if request.headers.get("authorization") == "Bearer xoxe.xoxp-TEST-old-config":
                return httpx.Response(200, json={"ok": False, "error": "token_expired"})
            assert (
                request.headers.get("authorization")
                == "Bearer xoxe.xoxp-TEST-other-config"
            )
            return httpx.Response(200, json={"ok": True, "app_id": "A0OTHER"})
        if path == "/api/tooling.tokens.rotate":
            store.pair = ("xoxe.xoxp-TEST-other-config", "xoxe-TEST-other-refresh")
            return httpx.Response(200, json={"ok": False, "error": "invalid_refresh_token"})
        raise AssertionError(f"unexpected path {path}")

    client = HttpxSlackClient(
        store, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    assert client.create_app({"display_information": {"name": "clio"}}) == "A0OTHER"
    assert calls == [
        "/api/apps.manifest.create",
        "/api/tooling.tokens.rotate",
        "/api/apps.manifest.create",
    ]
    # This pod never overwrote the winner's pair.
    assert store.writes == []


def test_slack_rotation_failure_raises_when_the_store_is_unchanged():
    # Nobody else rotated: the rotate really did fail and the caller must
    # see it, exactly as before.
    store = _MemorySlackTokenStore("xoxe.xoxp-TEST-old-config", "xoxe-TEST-old-refresh")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path == "/api/apps.manifest.create":
            return httpx.Response(200, json={"ok": False, "error": "token_expired"})
        if path == "/api/tooling.tokens.rotate":
            return httpx.Response(200, json={"ok": False, "error": "invalid_refresh_token"})
        raise AssertionError(f"unexpected path {path}")

    client = HttpxSlackClient(
        store, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    with pytest.raises(SlackApiError) as exc_info:
        client.create_app({"display_information": {"name": "clio"}})

    assert "invalid_refresh_token" in str(exc_info.value)
    assert "xoxe-TEST-old-refresh" not in str(exc_info.value)
    assert calls == ["/api/apps.manifest.create", "/api/tooling.tokens.rotate"]
    assert store.pair == ("xoxe.xoxp-TEST-old-config", "xoxe-TEST-old-refresh")


def _clients_with_slack(slack) -> Clients:
    """A `Clients` carrying just the field under test -- the probe reads
    `clients.slack` and nothing else, and the other fakes are only here to
    satisfy the dataclass."""
    return Clients(
        github=FakeGitHub(),
        gojira=FakeGojira(),
        cloud=FakeCloud(),
        agent=FakeAgent(),
        kubectl=FakeKubectl(),
        slack=slack,
    )


def _rotating_handler(calls: list[str], *, app_id: str = "A0NEW"):
    """apps.manifest.create refuses the old token once, rotate succeeds,
    the retry with the new token lands."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path == "/api/apps.manifest.create":
            if request.headers.get("authorization") == "Bearer xoxe.xoxp-TEST-old-config":
                return httpx.Response(200, json={"ok": False, "error": "token_expired"})
            return httpx.Response(200, json={"ok": True, "app_id": app_id})
        if path == "/api/tooling.tokens.rotate":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "token": "xoxe.xoxp-TEST-new-config",
                    "refresh_token": "xoxe-TEST-new-refresh",
                },
            )
        raise AssertionError(f"unexpected path {path}")

    return handler


def test_slack_rotation_retries_a_failing_store_write_and_then_succeeds():
    # The Kube store's write is two kubectl round trips: a transient
    # failure there must not cost the pair Slack has already issued.
    store = _MemorySlackTokenStore(
        "xoxe.xoxp-TEST-old-config", "xoxe-TEST-old-refresh", write_error="kubectl: i/o timeout"
    )
    slept: list[float] = []

    def flaky_write(config_token, refresh_token):
        store.writes.append((config_token, refresh_token))
        if len(store.writes) < 3:
            raise ClientError("kubectl apply of secret 'kobo-slack-tokens' failed: i/o timeout")
        store.pair = (config_token, refresh_token)

    store.write = flaky_write
    calls: list[str] = []
    client = HttpxSlackClient(
        store,
        http_client=httpx.Client(transport=httpx.MockTransport(_rotating_handler(calls))),
        sleep=slept.append,
    )

    assert client.create_app({"display_information": {"name": "clio"}}) == "A0NEW"
    assert len(store.writes) == 3
    assert store.pair == ("xoxe.xoxp-TEST-new-config", "xoxe-TEST-new-refresh")
    assert len(slept) == 2  # only between attempts, never after the last


def test_slack_rotation_that_cannot_be_stored_says_the_pair_must_be_reissued():
    # The unrecoverable one: Slack has spent the old refresh token and
    # issued a new pair this room could not keep. Nothing automatic can
    # recover it, so the message has to tell an operator exactly what to do.
    store = _MemorySlackTokenStore(
        "xoxe.xoxp-TEST-old-config",
        "xoxe-TEST-old-refresh",
        write_error="kubectl apply of secret 'kobo-slack-tokens' failed: forbidden",
    )
    calls: list[str] = []
    slept: list[float] = []
    client = HttpxSlackClient(
        store,
        http_client=httpx.Client(transport=httpx.MockTransport(_rotating_handler(calls))),
        sleep=slept.append,
    )

    with pytest.raises(SlackApiError) as exc_info:
        client.create_app({"display_information": {"name": "clio"}})

    message = str(exc_info.value)
    assert len(store.writes) == SLACK_STORE_WRITE_ATTEMPTS == 3
    assert len(slept) == SLACK_STORE_WRITE_ATTEMPTS - 1
    assert "rotated" in message and "could not" in message
    assert "spent" in message
    assert "Slack" in message
    # The operator's actual next move, not just a complaint.
    assert "re-issue" in message.lower() or "reissue" in message.lower()
    for token in (
        "xoxe.xoxp-TEST-new-config",
        "xoxe-TEST-new-refresh",
        "xoxe.xoxp-TEST-old-config",
        "xoxe-TEST-old-refresh",
    ):
        assert token not in message
    # The create is never retried against a token the room cannot keep.
    assert calls == ["/api/apps.manifest.create", "/api/tooling.tokens.rotate"]


def test_slack_rotation_stores_once_when_the_write_succeeds_first_time():
    store = _MemorySlackTokenStore("xoxe.xoxp-TEST-old-config", "xoxe-TEST-old-refresh")
    slept: list[float] = []
    client = HttpxSlackClient(
        store,
        http_client=httpx.Client(transport=httpx.MockTransport(_rotating_handler([]))),
        sleep=slept.append,
    )

    assert client.create_app({"display_information": {"name": "clio"}}) == "A0NEW"
    assert store.writes == [("xoxe.xoxp-TEST-new-config", "xoxe-TEST-new-refresh")]
    assert slept == []


def test_slack_rotation_propagates_a_failing_pre_check_read():
    # The read that decides whether anyone else already rotated. Nothing
    # has been spent yet at this point, so this must surface as the plain
    # store failure it is -- never as a rotation the room might have lost.
    store = _MemorySlackTokenStore(
        "xoxe.xoxp-TEST-old-config", "xoxe-TEST-old-refresh", read_error="secret kobo/x failed"
    )
    calls: list[str] = []
    client = HttpxSlackClient(
        store, http_client=httpx.Client(transport=httpx.MockTransport(_rotating_handler(calls)))
    )

    with pytest.raises(ClientError) as exc_info:
        client.create_app({"display_information": {"name": "clio"}})

    assert "secret kobo/x failed" in str(exc_info.value)
    assert not isinstance(exc_info.value, SlackApiError)
    # Never called Slack at all: the very first read is the one that failed.
    assert calls == []


def test_slack_rotation_logs_when_it_takes_the_other_processs_token(caplog):
    store = _MemorySlackTokenStore("xoxe.xoxp-TEST-old-config", "xoxe-TEST-old-refresh")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/apps.manifest.create":
            if request.headers.get("authorization") == "Bearer xoxe.xoxp-TEST-old-config":
                store.pair = ("xoxe.xoxp-TEST-other-config", "xoxe-TEST-other-refresh")
                return httpx.Response(200, json={"ok": False, "error": "token_expired"})
            return httpx.Response(200, json={"ok": True, "app_id": "A0OTHER"})
        raise AssertionError("rotate must not be called")

    client = HttpxSlackClient(
        store, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    with caplog.at_level(logging.WARNING, logger="kobo_api.clients"):
        client.create_app({"display_information": {"name": "clio"}})

    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("another process" in m for m in messages)
    for message in messages:
        assert "xoxe" not in message


# --- probe_slack_token_store (boot-time write probe) --------------------------


def test_probe_checks_writability_without_persisting_anything():
    # A probe that actually WROTE would be a lost update: another replica
    # can rotate between this probe's read and its write, and writing back
    # the pair this process read would restore a refresh token Slack has
    # already spent -- causing the exact lockout the probe exists to catch
    # early. A server dry-run runs authorization and admission and stores
    # nothing.
    live = {"config_token": "xoxe.xoxp-TEST-config", "refresh_token": "xoxe-TEST-refresh"}
    kubectl = FakeKubectl(secrets={("kobo", "kobo-slack-tokens"): live})
    clients = _clients_with_slack(
        HttpxSlackClient(KubeSlackTokenStore(kubectl, "kobo", "kobo-slack-tokens"))
    )

    probe_slack_token_store(clients)

    assert kubectl.secret_write_checks == [("kobo", "kobo-slack-tokens")]
    # Not one real apply, and the stored pair is byte-identical to before.
    assert kubectl.secrets_applied == []
    assert kubectl.secrets == {("kobo", "kobo-slack-tokens"): live}


def test_probe_leaves_a_pair_another_replica_rotated_untouched():
    # The race the probe must not lose: the store moves between the read
    # and the check. Nothing this process does may put the old pair back.
    kubectl = FakeKubectl(
        secrets={
            ("kobo", "kobo-slack-tokens"): {
                "config_token": "xoxe.xoxp-TEST-config",
                "refresh_token": "xoxe-TEST-refresh",
            }
        }
    )
    store = KubeSlackTokenStore(kubectl, "kobo", "kobo-slack-tokens")
    rotated = {
        "config_token": "xoxe.xoxp-TEST-other-config",
        "refresh_token": "xoxe-TEST-other-refresh",
    }
    real_check = kubectl.apply_secret

    def rotate_then_check(namespace, name, literals, *, dry_run=False):
        # Another replica lands its rotation in the window.
        kubectl.secrets[(namespace, name)] = dict(rotated)
        real_check(namespace, name, literals, dry_run=dry_run)

    kubectl.apply_secret = rotate_then_check

    probe_slack_token_store(_clients_with_slack(HttpxSlackClient(store)))

    assert kubectl.secrets == {("kobo", "kobo-slack-tokens"): rotated}


def test_probe_failure_logs_one_error_naming_the_secret_and_never_raises(caplog):
    kubectl = FakeKubectl(secret_read_error="kubectl get secret kobo/kobo-slack-tokens failed")
    clients = _clients_with_slack(HttpxSlackClient(KubeSlackTokenStore(kubectl, "kobo", "kobo-slack-tokens")))

    with caplog.at_level(logging.ERROR, logger="kobo_api.clients"):
        probe_slack_token_store(clients)  # must not raise: the room still boots

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    message = errors[0].getMessage()
    assert "kobo/kobo-slack-tokens" in message
    assert "rotation" in message


def test_apply_secret_server_dry_run_argv():
    # Server-side, not client-side: only a server dry-run actually reaches
    # the API server's authorization and admission, which is the whole
    # question the probe asks.
    runner = RecordingRunner(results=[(0, "<rendered secret yaml>", ""), (0, "", "")])
    client = KubectlCLI(runner=runner)

    client.apply_secret("kobo", "kobo-slack-tokens", {"config_token": "x"}, dry_run=True)

    assert runner.calls[1] == ["kubectl", "apply", "-f", "-", "--dry-run=server"]


def test_kube_store_check_writable_names_the_secret_when_it_is_refused():
    kubectl = FakeKubectl(
        secrets={
            ("kobo", "kobo-slack-tokens"): {
                "config_token": "xoxe.xoxp-TEST-config",
                "refresh_token": "xoxe-TEST-refresh",
            }
        },
        apply_secret_error="secrets is forbidden: cannot patch resource",
    )
    store = KubeSlackTokenStore(kubectl, "kobo", "kobo-slack-tokens")

    with pytest.raises(ClientError) as exc_info:
        store.check_writable()

    assert "kobo/kobo-slack-tokens" in str(exc_info.value)
    assert "xoxe" not in str(exc_info.value)


def test_probe_does_nothing_for_the_file_store_or_no_slack(tmp_path, caplog):
    tokens_path = _write_slack_tokens_file(
        tmp_path, config_token="xoxe.xoxp-TEST-config", refresh_token="xoxe-TEST-refresh"
    )
    before = tokens_path.read_text()

    with caplog.at_level(logging.WARNING, logger="kobo_api.clients"):
        probe_slack_token_store(_clients_with_slack(HttpxSlackClient(tokens_path)))
        probe_slack_token_store(_clients_with_slack(None))

    assert tokens_path.read_text() == before
    assert caplog.records == []


# --- Slack.delete_app (Task 3: agent destruction's Slack step) -------------------
#
# Already-gone error codes grounded against Slack's own documented error
# table for apps.manifest.delete (docs.slack.dev/reference/methods/
# apps.manifest.delete, fetched 2026-08-17): "invalid_app_id" ("The app ID
# passed in was invalid.") and "app_not_found" ("The specified app was not
# found.") are both listed there; every other documented error there (bad
# auth aside, covered by the existing rotate-on-401 discipline) is a real
# failure, never an already-gone signal.


def test_slack_delete_app_request_shape_and_auth_header(tmp_path):
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
        captured["app_id"] = parse_qs(request.content.decode())["app_id"][0]
        return httpx.Response(200, json={"ok": True})

    client = HttpxSlackClient(
        tokens_path, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    result = client.delete_app("A0FAKE")

    assert result is True
    assert captured["method"] == "POST"
    assert captured["url"] == "https://slack.com/api/apps.manifest.delete"
    assert captured["auth"] == "Bearer xoxe.xoxp-1-fake-config-token"
    assert captured["app_id"] == "A0FAKE"


@pytest.mark.parametrize("error", ["app_not_found", "invalid_app_id"])
def test_slack_delete_app_already_gone_errors_return_false(tmp_path, error):
    tokens_path = _write_slack_tokens_file(
        tmp_path,
        config_token="xoxe.xoxp-1-fake-config-token",
        refresh_token="xoxe-1-fake-refresh-token",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": error})

    client = HttpxSlackClient(
        tokens_path, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert client.delete_app("A0GONE") is False


def test_slack_delete_app_other_error_raises_slack_api_error_without_token_material(tmp_path):
    tokens_path = _write_slack_tokens_file(
        tmp_path,
        config_token="xoxe.xoxp-1-super-secret-config",
        refresh_token="xoxe-1-super-secret-refresh",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "no_permission"})

    client = HttpxSlackClient(
        tokens_path, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(SlackApiError) as exc_info:
        client.delete_app("A0FAKE")

    assert isinstance(exc_info.value, ClientError)
    assert "no_permission" in str(exc_info.value)
    assert "xoxe.xoxp-1-super-secret-config" not in str(exc_info.value)
    assert "xoxe-1-super-secret-refresh" not in str(exc_info.value)


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

    assert str(exc_info.value) == "the picture didn't come through. Try again."
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

    assert str(exc_info.value) == "the picture didn't come through. Try again."
    assert prompt not in str(exc_info.value)


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

    assert str(exc_info.value) == "the picture didn't come through. Try again."
    assert "gcloud" not in str(exc_info.value)
    assert "account details" not in str(exc_info.value)


# --- CachedGcloudToken (perf fix: one gcloud shell-out per ttl, not per call) -----
#
# `_gcloud_print_access_token` shells out to `gcloud auth print-access-token`
# on every call (~1-2s live, per the perf report) even though the token it
# returns is good for about an hour. CachedGcloudToken wraps any `fetch`
# callable (the real `_gcloud_print_access_token` by default) with a
# time-boxed cache, mirroring InstallationTokenProvider's own cache+expiry
# discipline (kobo_api.github_app) — `now` is a test seam here too, real
# `time.monotonic` by default.


def test_cached_gcloud_token_concurrent_first_calls_share_one_fetch():
    # Two threads racing in on a cold cache (background threads calling
    # Vertex too, per the perf report) must still cost exactly one
    # underlying fetch — the lock must be held across the fetch itself, not
    # just around a read-then-write of the cached fields.
    calls: list[str] = []

    def fetch() -> str:
        calls.append("fetched")
        time.sleep(0.05)  # widen the race window
        return "concurrent-token"

    token_provider = CachedGcloudToken(fetch=fetch)
    results: list[str] = []

    def worker() -> None:
        results.append(token_provider())

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(calls) == 1
    assert results == ["concurrent-token", "concurrent-token"]


def test_cached_gcloud_token_fetch_failure_raises_and_later_call_retries():
    calls: list[str] = []

    def fetch() -> str:
        calls.append("fetched")
        if len(calls) == 1:
            raise ClientError("gcloud auth print-access-token failed (rc=1): not logged in")
        return "recovered-token"

    token_provider = CachedGcloudToken(fetch=fetch)

    with pytest.raises(ClientError):
        token_provider()

    result = token_provider()

    assert result == "recovered-token"
    assert len(calls) == 2


def test_cached_token_never_outlives_a_short_lived_token():
    """The 2026-08-24 production regression: `gcloud auth print-access-token`
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


def test_cached_token_unknown_lifetime_falls_back_to_a_conservative_window():
    """A fetch that reports only a string (the gcloud fallback) tells us
    nothing about remaining life, so the cache must stay conservative rather
    than assume a full hour."""
    calls: list[str] = []

    def fetch() -> str:
        calls.append("fetched")
        return f"token-{len(calls)}"

    clock = {"now": 1000.0}
    token_provider = CachedGcloudToken(fetch=fetch, now=lambda: clock["now"])

    token_provider()
    clock["now"] += 400
    token_provider()

    assert len(calls) == 2


def test_cached_token_long_lived_token_still_honors_the_ttl_ceiling():
    """The perf win the cache exists for survives: a genuinely fresh token is
    still held for the full ttl rather than refetched per call."""
    calls: list[str] = []

    def fetch() -> tuple[str, int]:
        calls.append("fetched")
        return (f"token-{len(calls)}", 3600)

    clock = {"now": 1000.0}
    token_provider = CachedGcloudToken(fetch=fetch, ttl_s=1800, now=lambda: clock["now"])

    token_provider()
    clock["now"] += 1700  # inside the ceiling
    token_provider()
    assert len(calls) == 1

    clock["now"] += 200  # now past it
    token_provider()
    assert len(calls) == 2


def test_metadata_access_token_returns_token_and_remaining_life():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Metadata-Flavor"] == "Google"
        return httpx.Response(200, json={"access_token": "md-token", "expires_in": 2400})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert _metadata_access_token(client) == ("md-token", 2400)


def test_metadata_access_token_returns_none_when_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no metadata server here")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert _metadata_access_token(client) is None


# --- Name (VertexNameClient) ----------------------------------------------------


def _text_reply(text: str) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def test_vertex_name_client_request_shape_url_body_and_auth_header():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=_text_reply('[{"name": "ember", "reason": "warm"}]'))

    client = VertexNameClient(
        "acme-project",
        "us-central1",
        "gemini-2.5-flash",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_provider=lambda: "fake-access-token",
    )
    client.suggest("a calm troubleshooter", "helps with billing")

    assert captured["method"] == "POST"
    assert captured["url"] == (
        "https://us-central1-aiplatform.googleapis.com/v1/projects/acme-project/"
        "locations/us-central1/publishers/google/models/gemini-2.5-flash:generateContent"
    )
    assert captured["json"]["contents"][0]["role"] == "user"
    prompt = captured["json"]["contents"][0]["parts"][0]["text"]
    assert "a calm troubleshooter" in prompt
    assert "helps with billing" in prompt
    assert captured["auth"] == "Bearer fake-access-token"
    # Naming perf fix: dynamic thinking is gemini-2.5-flash's default when
    # generationConfig is omitted, and it was dominating latency for a
    # three-name JSON task that needs none of it, so thinkingBudget: 0 turns
    # it off. 512 tokens is well more than three names plus their reasons need.
    assert captured["json"]["generationConfig"] == {
        "thinkingConfig": {"thinkingBudget": 0},
        "maxOutputTokens": 512,
        "temperature": 1.0,
    }


def test_vertex_name_client_parses_clean_json_array():
    reply_text = (
        '[{"name": "ember", "reason": "warm and steady"}, '
        '{"name": "cedar", "reason": "grounded and reliable"}]'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_text_reply(reply_text))

    client = VertexNameClient(
        "acme-project", "us-central1", "gemini-2.5-flash",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_provider=lambda: "fake-access-token",
    )

    result = client.suggest("a helper", "helps out")

    assert result == [
        {"name": "ember", "reason": "warm and steady"},
        {"name": "cedar", "reason": "grounded and reliable"},
    ]


def test_vertex_name_client_skips_non_dict_items_and_non_str_names():
    # Tolerant per-item filtering: a malformed item (bare string, or a dict
    # with a non-string name) is dropped rather than raising — only a
    # structural failure (no array found, invalid JSON) raises ClientError.
    reply_text = '["just a string", {"name": 42}, {"name": "cedar", "reason": "solid"}]'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_text_reply(reply_text))

    client = VertexNameClient(
        "acme-project", "us-central1", "gemini-2.5-flash",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_provider=lambda: "fake-access-token",
    )

    result = client.suggest("a helper", "helps out")

    assert result == [{"name": "cedar", "reason": "solid"}]


def test_vertex_name_client_token_provider_failure_raises_client_error_without_leaking_gcloud_output():
    def failing_token_provider() -> str:
        raise ClientError(
            "gcloud auth print-access-token failed (rc=1): ERROR: account details"
        )

    client = VertexNameClient(
        "acme-project", "us-central1", "gemini-2.5-flash",
        http_client=httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=_text_reply("[]"))
        )),
        token_provider=failing_token_provider,
    )

    with pytest.raises(ClientError) as exc_info:
        client.suggest("a helper", "helps out")

    assert "gcloud" not in str(exc_info.value)
    assert "account details" not in str(exc_info.value)


def test_extract_json_array_no_array_found_raises_value_error():
    with pytest.raises(ValueError):
        _extract_json_array("no brackets here at all")


def test_extract_json_array_finds_array_past_fence_and_preamble():
    text = "here you go:\n```json\n[1, 2, 3]\n```\n"
    assert _extract_json_array(text) == [1, 2, 3]


# --- Name: draft (VertexNameClient.draft, Persona v3 addendum) ------------------


def _object_reply(text: str) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def test_vertex_name_client_draft_request_shape_url_body_and_auth_header():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json=_object_reply(
                '{"name": "billing-buddy", "who_they_are": "calm", "what_they_do": "helps"}'
            ),
        )

    client = VertexNameClient(
        "acme-project",
        "us-central1",
        "gemini-2.5-flash",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_provider=lambda: "fake-access-token",
    )
    client.draft("help customers with billing", ["never share account numbers"])

    assert captured["method"] == "POST"
    assert captured["url"] == (
        "https://us-central1-aiplatform.googleapis.com/v1/projects/acme-project/"
        "locations/us-central1/publishers/google/models/gemini-2.5-flash:generateContent"
    )
    assert captured["json"]["contents"][0]["role"] == "user"
    prompt = captured["json"]["contents"][0]["parts"][0]["text"]
    assert "help customers with billing" in prompt
    assert "never share account numbers" in prompt
    assert captured["auth"] == "Bearer fake-access-token"


def test_vertex_name_client_draft_parses_clean_json_object():
    reply_text = (
        '{"name": "billing-buddy", "who_they_are": "a calm troubleshooter", '
        '"what_they_do": "Helps with billing."}'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_object_reply(reply_text))

    client = VertexNameClient(
        "acme-project", "us-central1", "gemini-2.5-flash",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_provider=lambda: "fake-access-token",
    )

    result = client.draft("help with billing", [])

    assert result == {
        "name": "billing-buddy",
        "who_they_are": "a calm troubleshooter",
        "what_they_do": "Helps with billing.",
        "purpose_tags": None,
    }


def test_vertex_name_client_draft_missing_keys_return_none_for_those_keys():
    # Tolerant extraction: a reply object missing a key hands back None for
    # it rather than raising — kobo_api.persona_draft.finalize_draft is what
    # falls a missing/invalid field back, not this client.
    reply_text = '{"name": "flint"}'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_object_reply(reply_text))

    client = VertexNameClient(
        "acme-project", "us-central1", "gemini-2.5-flash",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_provider=lambda: "fake-access-token",
    )

    result = client.draft("help with billing", [])

    assert result == {
        "name": "flint",
        "who_they_are": None,
        "what_they_do": None,
        "purpose_tags": None,
    }


def test_vertex_name_client_draft_job_with_braces_does_not_break_prompt_building():
    # The job text is free-form user input and may itself contain literal
    # "{"/"}" characters (e.g. describing a templating task) — the prompt
    # builder must not run it through str.format(), which would raise (or
    # misinterpret) on those, the exact hazard render_persona's own
    # docstring (kobo_api.personas) documents for this same reason.
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json=_object_reply('{"name": "n"}'))

    client = VertexNameClient(
        "acme-project", "us-central1", "gemini-2.5-flash",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_provider=lambda: "fake-access-token",
    )

    client.draft("handle {curly} brace formatting requests", [])

    prompt = captured["json"]["contents"][0]["parts"][0]["text"]
    assert "handle {curly} brace formatting requests" in prompt


def test_extract_json_object_no_object_found_raises_value_error():
    with pytest.raises(ValueError):
        _extract_json_object("no braces here at all")


def test_extract_json_object_finds_object_past_fence_and_preamble():
    text = 'here you go:\n```json\n{"a": 1, "b": 2}\n```\n'
    assert _extract_json_object(text) == {"a": 1, "b": 2}


# --- Name: capability (VertexNameClient.capability, persona-metadata plan Task 3) -


def _text_reply(text: str) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def test_vertex_name_client_capability_request_shape_url_body_and_auth_header():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=_text_reply("Helps with billing questions."))

    client = VertexNameClient(
        "acme-project",
        "us-central1",
        "gemini-2.5-flash",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_provider=lambda: "fake-access-token",
    )
    result = client.capability("You are a billing helper.", [], [])

    assert captured["method"] == "POST"
    assert captured["url"] == (
        "https://us-central1-aiplatform.googleapis.com/v1/projects/acme-project/"
        "locations/us-central1/publishers/google/models/gemini-2.5-flash:generateContent"
    )
    assert captured["json"]["contents"][0]["role"] == "user"
    assert captured["auth"] == "Bearer fake-access-token"
    assert result == "Helps with billing questions."


def test_vertex_name_client_capability_persona_body_with_braces_does_not_break_prompt_building():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json=_text_reply("fine"))

    client = VertexNameClient(
        "acme-project", "us-central1", "gemini-2.5-flash",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_provider=lambda: "fake-access-token",
    )

    client.capability("Handles {curly} brace formatting requests.", [], [])

    prompt = captured["json"]["contents"][0]["parts"][0]["text"]
    assert "Handles {curly} brace formatting requests." in prompt


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


# --- Storage error branches (coverage gap) ------------------------------------


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


# --- Cabal (CabalClient: switchboard admin API - graduate/absorb/relay) ---------


def test_cabal_graduate_request_shape_body_and_auth_header():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={})

    client = CabalClient(
        "http://cabal.local", "op-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client.graduate(
        "clio",
        endpoint="http://agent-clio.default.svc.cluster.local",
        audience="clio",
        card={"name": "clio", "team": "acme"},
    )

    assert captured["method"] == "POST"
    assert captured["url"] == "http://cabal.local/admin/persona/clio/graduate"
    assert captured["json"] == {
        "endpoint": "http://agent-clio.default.svc.cluster.local",
        "audience": "clio",
        "agent_card_json": {"name": "clio", "team": "acme"},
    }
    assert captured["auth"] == "Bearer op-token"


def test_cabal_graduate_raises_client_error_on_non_2xx():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = CabalClient(
        "http://cabal.local", "op-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ClientError):
        client.graduate("clio", endpoint="http://agent-clio", audience="clio", card={})


def test_cabal_graduate_accepts_201_as_success():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={})

    client = CabalClient(
        "http://cabal.local", "op-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client.graduate("clio", endpoint="http://agent-clio", audience="clio", card={})  # no raise


def test_cabal_graduate_error_message_never_contains_the_token():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal failure detail")

    client = CabalClient(
        "http://cabal.local", "super-secret-operator-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ClientError) as exc_info:
        client.graduate("clio", endpoint="http://agent-clio", audience="clio", card={})
    assert "super-secret-operator-token" not in str(exc_info.value)
    # The stricter HttpxStorageClient convention this mirrors: never the
    # response body either (it's logged server-side, truncated, instead).
    assert "internal failure detail" not in str(exc_info.value)


def test_cabal_absorb_request_shape_empty_body_and_auth_header():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={})

    client = CabalClient(
        "http://cabal.local", "op-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client.absorb("clio")

    assert captured["method"] == "POST"
    assert captured["url"] == "http://cabal.local/admin/persona/clio/absorb"
    assert captured["json"] == {}
    assert captured["auth"] == "Bearer op-token"


def test_cabal_absorb_tolerates_404_as_already_delisted():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    client = CabalClient(
        "http://cabal.local", "op-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client.absorb("clio")  # must not raise


def test_cabal_absorb_raises_client_error_on_non_404_non_2xx():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = CabalClient(
        "http://cabal.local", "op-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ClientError):
        client.absorb("clio")


def test_cabal_relay_request_shape_body_and_auth_header():
    captured = {}
    envelope = {"kind": "task", "id": "chat-abc123", "history": []}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200, json={"status": {"message": {"parts": [{"text": "hi"}]}}}
        )

    client = CabalClient(
        "http://cabal.local", "op-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = client.relay("clio", envelope)

    assert captured["method"] == "POST"
    assert captured["url"] == "http://cabal.local/a2a/tasks"
    assert captured["json"] == {"target": "clio", "task": envelope}
    assert captured["auth"] == "Bearer op-token"
    assert result == {"status": {"message": {"parts": [{"text": "hi"}]}}}


def test_cabal_relay_uses_180s_timeout_regardless_of_client_default():
    # Relay waits on a real model round trip on the far side of the
    # switchboard (the curl -m 10 lesson) - this per-call override must win
    # over whatever default timeout the injected httpx.Client carries.
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json={})

    client = CabalClient(
        "http://cabal.local", "op-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client.relay("clio", {"kind": "task"})

    assert captured["timeout"] == {
        "connect": 10.0, "read": 180.0, "write": 180.0, "pool": 180.0
    }


# --- mint_colleague (Task 3/B3: every agent is born with a colleague pass) ------


def test_cabal_mint_colleague_request_shape_body_and_auth_header():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"access_token": "fresh-colleague-token"})

    client = CabalClient(
        "http://cabal.local", "op-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    token = client.mint_colleague("clio")

    assert captured["method"] == "POST"
    assert captured["url"] == "http://cabal.local/admin/token/mint-service"
    assert captured["json"] == {
        "sub": "agent:clio",
        "scope": ["colleague"],
        "aud": "cabal-core",
        "ttl_days": 365,
    }
    assert captured["auth"] == "Bearer op-token"
    assert token == "fresh-colleague-token"


def test_cabal_revoke_colleague_request_shape_and_auth_header():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"sub": "agent:clio", "not_before": 1_700_000_000})

    client = CabalClient(
        "http://cabal.local", "op-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client.revoke_colleague("clio")

    assert captured["method"] == "POST"
    assert captured["url"] == "http://cabal.local/admin/token/revoke"
    assert captured["json"] == {"sub": "agent:clio"}
    assert captured["auth"] == "Bearer op-token"


def test_cabal_revoke_colleague_raises_cabal_error_on_failure():
    client = CabalClient(
        "http://cabal.local", "op-token",
        client=httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(503, text="down"))
        ),
    )
    with pytest.raises(CabalError, match="revoke_colleague"):
        client.revoke_colleague("clio")


def test_cabal_mint_colleague_error_message_never_contains_the_operator_token():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal failure detail")

    client = CabalClient(
        "http://cabal.local", "super-secret-operator-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(CabalError) as exc_info:
        client.mint_colleague("clio")
    assert "super-secret-operator-token" not in str(exc_info.value)
    # Same stricter convention graduate/relay already pin: never the
    # response body either (it's logged server-side, truncated, instead).
    assert "internal failure detail" not in str(exc_info.value)


def test_cabal_mint_colleague_returned_token_never_appears_in_a_debug_log(caplog):
    """The minted token is a brand-new secret this call exists to hand back
    — it must never end up in a log line, at any level, the same way the
    operator token this client already holds never does."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"access_token": "ultra-secret-minted-colleague-token"}
        )

    client = CabalClient(
        "http://cabal.local", "op-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with caplog.at_level(logging.DEBUG, logger="kobo_api.clients"):
        token = client.mint_colleague("clio")

    assert token == "ultra-secret-minted-colleague-token"
    assert "ultra-secret-minted-colleague-token" not in caplog.text


# --- put_dispatch_pairs (Task C4: kobo syncs the switchboard's allow-pairs
# policy on every consent change) ---------------------------------------------


def test_cabal_put_dispatch_pairs_request_shape_body_and_auth_header():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"count": 2})

    client = CabalClient(
        "http://cabal.local", "op-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    count = client.put_dispatch_pairs({("agent:b", "c"), ("agent:a", "b")})

    assert captured["method"] == "PUT"
    assert captured["url"] == "http://cabal.local/admin/dispatch-pairs"
    # Sorted for determinism -- a set's own iteration order must never leak
    # into the request body two calls with the same pairs could otherwise
    # send in a different order.
    assert captured["json"] == {"pairs": [["agent:a", "b"], ["agent:b", "c"]]}
    assert captured["auth"] == "Bearer op-token"
    assert count == 2


def test_cabal_put_dispatch_pairs_empty_set_still_pushes():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json={"count": 0})

    client = CabalClient(
        "http://cabal.local", "op-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    count = client.put_dispatch_pairs(set())

    assert captured["json"] == {"pairs": []}
    assert count == 0


def test_cabal_put_dispatch_pairs_does_not_parse_pairs_back_from_the_response():
    # The response only ever carries {"count": N} -- this must never reach
    # into a "pairs" key on the response body, even if one happened to be
    # present (a future cabal change echoing the pairs back must not change
    # this method's return shape).
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"count": 1, "pairs": [["should", "be-ignored"]]})

    client = CabalClient(
        "http://cabal.local", "op-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert client.put_dispatch_pairs([("agent:a", "b")]) == 1


def test_cabal_put_dispatch_pairs_raises_typed_cabal_error_on_non_2xx():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = CabalClient(
        "http://cabal.local", "op-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(CabalError):
        client.put_dispatch_pairs([("agent:a", "b")])


def test_cabal_put_dispatch_pairs_shrink_to_empty_after_nonempty_logs_a_warning(caplog):
    # cabal treats an EMPTY pair set as WIDE OPEN by design -- so a computed
    # set that shrinks to empty right after this same client pushed a
    # non-empty one is exactly the accidental-open-the-gate shape worth a
    # loud warning, even though the push itself still goes through
    # ("policy is policy").
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"count": 0})

    client = CabalClient(
        "http://cabal.local", "op-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client.put_dispatch_pairs([("agent:a", "b")])  # non-empty first

    with caplog.at_level(logging.WARNING, logger="kobo_api.clients"):
        client.put_dispatch_pairs([])

    assert any(
        rec.levelname == "WARNING" and "empty" in rec.message.lower()
        for rec in caplog.records
    )


def test_cabal_put_dispatch_pairs_first_ever_push_being_empty_does_not_warn(caplog):
    # No prior push from this client at all -- an empty set here is just
    # "nothing to allow yet" (a fresh room, or one with no groups/invites),
    # not a shrink from anything.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"count": 0})

    client = CabalClient(
        "http://cabal.local", "op-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with caplog.at_level(logging.WARNING, logger="kobo_api.clients"):
        client.put_dispatch_pairs([])

    assert not any(rec.levelname == "WARNING" for rec in caplog.records)


# --- dispatch_pairs_dirty (final-review fix round, HIGH-2b: the lost-sync
# reconciliation flag) -- False fresh, True after a failed push, cleared by
# the next SUCCESSFUL push regardless of whether the pairs it carries are
# the same set as before. ------------------------------------------------


def test_dispatch_pairs_dirty_is_false_on_a_fresh_client():
    client = CabalClient(
        "http://cabal.local", "op-token",
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"count": 0}))),
    )
    assert client.dispatch_pairs_dirty is False


def test_dispatch_pairs_dirty_true_after_a_non_2xx_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = CabalClient(
        "http://cabal.local", "op-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(CabalError):
        client.put_dispatch_pairs([("agent:a", "b")])

    assert client.dispatch_pairs_dirty is True


def test_dispatch_pairs_dirty_true_after_a_raw_transport_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = CabalClient(
        "http://cabal.local", "op-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(httpx.ConnectError):
        client.put_dispatch_pairs([("agent:a", "b")])

    assert client.dispatch_pairs_dirty is True


def test_dispatch_pairs_dirty_clears_on_the_next_successful_push_even_with_the_same_pairs():
    # The reconciliation guarantee itself: a failed push leaves the flag
    # set; the NEXT push -- carrying the exact same pairs, nothing "changed"
    # -- still actually goes out over HTTP and, once it succeeds, clears
    # the flag. Nothing here special-cases "pairs unchanged" into a skip.
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"count": 1})

    client = CabalClient(
        "http://cabal.local", "op-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    same_pairs = [("agent:a", "b")]

    with pytest.raises(CabalError):
        client.put_dispatch_pairs(same_pairs)
    assert client.dispatch_pairs_dirty is True

    count = client.put_dispatch_pairs(same_pairs)

    assert count == 1
    assert calls["n"] == 2  # the retry actually went out, not skipped
    assert client.dispatch_pairs_dirty is False


# --- list_conversations / get_conversation / stream_events (Task 4/B4: the
# watching window) ------------------------------------------------------------


def test_cabal_list_conversations_request_shape_and_auth_header():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"conversations": [{"task_id": "t1"}]})

    client = CabalClient(
        "http://cabal.local", "op-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = client.list_conversations()

    assert captured["method"] == "GET"
    assert captured["url"] == "http://cabal.local/admin/conversations"
    assert captured["auth"] == "Bearer op-token"
    assert result == [{"task_id": "t1"}]


def test_cabal_list_conversations_passes_target_and_caller_when_given():
    """The two halves of participant scoping (cabal #128): `target` for the
    conversations an agent answered, `caller` (verbatim `agent:<name>`) for
    the ones it started. Sent only when given, like `limit`."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"conversations": []})

    client = CabalClient(
        "http://cabal.local", "op-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client.list_conversations(limit=200, target="ward")
    assert captured["params"] == {"limit": "200", "target": "ward"}

    client.list_conversations(limit=200, caller="agent:ward")
    assert captured["params"] == {"limit": "200", "caller": "agent:ward"}


def test_cabal_get_conversation_request_shape_and_auth_header():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200, json={"conversation": {"task_id": "t1"}, "events": []}
        )

    client = CabalClient(
        "http://cabal.local", "op-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = client.get_conversation("t1")

    assert captured["method"] == "GET"
    assert captured["url"] == "http://cabal.local/admin/conversations/t1"
    assert captured["auth"] == "Bearer op-token"
    assert result == {"conversation": {"task_id": "t1"}, "events": []}


# Fix round 1 (Important review finding on Task 4/B4): `stream_events` is
# now an ASYNC generator over `httpx.AsyncClient`, not a sync one over
# `httpx.Client` - so a plain `list(...)` no longer works. `_collect` drains
# it inside a real event loop via stdlib `asyncio.run`, no new test
# dependency needed (no pytest-asyncio in this repo). Every test below
# wires `async_client=` (not `client=`) to `CabalClient`, since that's the
# one field `stream_events` now reads.
async def _collect(agen: collections.abc.AsyncIterator[bytes]) -> list[bytes]:
    return [chunk async for chunk in agen]


def test_cabal_stream_events_request_shape_and_auth_header():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, content=b"event: hello\ndata: {}\n\n")

    client = CabalClient(
        "http://cabal.local", "op-token",
        async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    chunks = asyncio.run(_collect(client.stream_events()))

    assert captured["method"] == "GET"
    assert captured["url"] == "http://cabal.local/admin/events/stream"
    assert captured["auth"] == "Bearer op-token"
    assert b"".join(chunks) == b"event: hello\ndata: {}\n\n"


def test_cabal_stream_events_yields_raw_bytes_unparsed():
    # Multiple named frames in one body - the client forwards them
    # byte-for-byte, no per-frame parsing/re-encoding.
    body = b"event: hello\ndata: {}\n\nevent: dispatch_start\ndata: {\"x\": 1}\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    client = CabalClient(
        "http://cabal.local", "op-token",
        async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    chunks = asyncio.run(_collect(client.stream_events()))
    assert b"".join(chunks) == body


def test_cabal_stream_events_non_2xx_ends_generator_with_no_chunks():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad token")

    client = CabalClient(
        "http://cabal.local", "op-token",
        async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert asyncio.run(_collect(client.stream_events())) == []


def test_cabal_stream_events_transport_error_ends_generator_with_no_chunks():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = CabalClient(
        "http://cabal.local", "op-token",
        async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert asyncio.run(_collect(client.stream_events())) == []  # must not raise


def test_cabal_stream_events_uses_a_bounded_read_timeout():
    # Fix round 1's whole point: no more `timeout=None`. A bounded read
    # timeout (generous for SSE gaps, still finite) means a silently-stalled
    # upstream eventually frees the call instead of hanging forever - and,
    # combined with the async client, lets a client disconnect actually
    # cancel this promptly rather than blocking a worker thread past it.
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, content=b"")

    client = CabalClient(
        "http://cabal.local", "op-token",
        async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    asyncio.run(_collect(client.stream_events()))

    assert captured["timeout"] == {
        "connect": 10.0, "read": 300.0, "write": 10.0, "pool": 10.0
    }


def test_cabal_stream_events_returns_an_async_iterator_not_a_sync_one():
    # The route (kobo_api.main) hands this straight to Starlette's
    # `StreamingResponse`, which only skips `iterate_in_threadpool` (the
    # thing that made the old sync generator's blocking read immune to
    # cancellation) when the object satisfies `AsyncIterable`. This is the
    # structural proof that the fix actually gets that fast path, not just
    # that it happens to return the right bytes.
    client = CabalClient(
        "http://cabal.local", "op-token",
        async_client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b""))
        ),
    )
    result = client.stream_events()
    try:
        assert isinstance(result, collections.abc.AsyncIterator)
        assert not isinstance(result, collections.abc.Iterator)
    finally:
        asyncio.run(result.aclose())


def test_cabal_stream_events_closes_promptly_on_early_cancellation():
    # Simulates a client disconnecting mid-stream: only one chunk is ever
    # consumed, then the generator is torn down via `aclose()` (what an
    # awaited async generator gets on task cancellation) instead of being
    # drained to the end. Must return promptly with no exception - proof
    # the async `with` block's cleanup isn't itself stuck on anything
    # blocking.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"event: hello\ndata: {}\n\nevent: more\ndata: {}\n\n")

    client = CabalClient(
        "http://cabal.local", "op-token",
        async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    async def _partial_then_close():
        agen = client.stream_events()
        first = await agen.__anext__()
        await agen.aclose()
        return first

    first_chunk = asyncio.run(asyncio.wait_for(_partial_then_close(), timeout=5))
    assert first_chunk


# --- filter_cabal_event_stream: colleagues conversation scoping on the SSE
# stream (access-tab refinement wave, conversation scoping) ------------------
#
# The switchboard's own wire shape (observed straight from cabal's event
# publisher): every frame's `data:` line is JSON `{"event_type": ...,
# "task_id": ..., "payload": {...}}`. Only `dispatch_start`'s own payload
# ever embeds who's talking to whom (`target`/`caller`, the same two keys
# `CabalClient.list_conversations`'s row shape carries as `target_agent`/
# `caller_agent`); every later event for that same task_id (`dispatch_
# success`/`dispatch_failure`) carries only state/error, so the filter must
# remember what an earlier `dispatch_start` in THIS stream said about that
# task_id to judge them. `caller` is only ever `agent:<name>` for a genuine
# colleague-to-colleague call (`CabalClient.mint_colleague`'s own `sub`
# shape) -- a human/service caller never takes that form.


def _dispatch_start_frame(
    task_id: str,
    *,
    target: str | None,
    caller: str | None,
    metadata: dict | None = None,
) -> bytes:
    inner = {"target": target, "caller": caller}
    if metadata is not None:
        inner["metadata"] = metadata
    payload = {
        "event_type": "dispatch_start",
        "task_id": task_id,
        "payload": inner,
    }
    return f"event: dispatch_start\ndata: {json.dumps(payload)}\n\n".encode()


def _dispatch_success_frame(task_id: str, *, state: str = "completed") -> bytes:
    payload = {"event_type": "dispatch_success", "task_id": task_id, "payload": {"state": state}}
    return f"event: dispatch_success\ndata: {json.dumps(payload)}\n\n".encode()


def _hello_frame() -> bytes:
    payload = {"msg": "connected", "subscriber_count": 1}
    return f"event: hello\ndata: {json.dumps(payload)}\n\n".encode()


async def _achunks(chunks: list[bytes]):
    for chunk in chunks:
        yield chunk


def _filtered(chunks: list[bytes], agent: str) -> list[bytes]:
    async def run() -> list[bytes]:
        return [c async for c in filter_cabal_event_stream(_achunks(chunks), agent)]

    return asyncio.run(run())


def _filtered_no_agent(chunks: list[bytes]) -> list[bytes]:
    async def run() -> list[bytes]:
        return [c async for c in filter_cabal_event_stream(_achunks(chunks))]

    return asyncio.run(run())


def test_filter_forwards_dispatch_start_matching_target():
    frame = _dispatch_start_frame("t-1", target="clio", caller="agent:iris")
    assert _filtered([frame], "clio") == [frame]


def test_filter_forwards_dispatch_start_matching_caller():
    frame = _dispatch_start_frame("t-1", target="clio", caller="agent:iris")
    assert _filtered([frame], "iris") == [frame]


def test_filter_drops_dispatch_start_matching_neither_side():
    frame = _dispatch_start_frame("t-1", target="clio", caller="agent:iris")
    assert _filtered([frame], "warden") == []


def test_filter_drops_dispatch_start_with_a_human_caller_when_scoped_to_target_name():
    # "clio" as a plain string could coincidentally equal a human email's
    # local part -- the caller side only ever matches the "agent:<name>"
    # form, never a bare comparison against the raw caller_agent string.
    frame = _dispatch_start_frame("t-1", target="warden", caller="operator:clio@example.com")
    assert _filtered([frame], "clio") == []


def test_filter_forwards_a_later_event_for_a_matched_tasks_task_id():
    start = _dispatch_start_frame("t-1", target="clio", caller="agent:iris")
    success = _dispatch_success_frame("t-1")
    assert _filtered([start, success], "clio") == [start, success]


def test_filter_drops_a_later_event_for_an_unmatched_tasks_task_id():
    start = _dispatch_start_frame("t-1", target="warden", caller="agent:iris")
    success = _dispatch_success_frame("t-1")
    assert _filtered([start, success], "clio") == []


def test_filter_drops_an_event_for_a_task_id_this_stream_never_saw_a_start_for():
    # The conversation began before this connection opened -- stream_events
    # never replays history, so this filter has no participant memory for
    # the task_id at all. Fail-closed: an event this filter cannot confirm
    # belongs to `agent` is dropped, never guessed as a match.
    success = _dispatch_success_frame("t-unseen")
    assert _filtered([success], "clio") == []


def test_filter_handles_frames_split_across_chunk_boundaries():
    frame = _dispatch_start_frame("t-1", target="clio", caller="agent:iris")
    # Split mid-frame -- stream_events' chunks carry no guarantee of landing
    # on a frame boundary, so the filter must buffer and reassemble.
    midpoint = len(frame) // 2
    chunks = [frame[:midpoint], frame[midpoint:]]
    assert _filtered(chunks, "clio") == [frame]


def test_filter_discards_a_trailing_partial_frame_when_the_stream_ends_mid_frame():
    frame = _dispatch_start_frame("t-1", target="clio", caller="agent:iris")
    # No closing blank line on the second chunk -- the connection dropped
    # mid-frame. Must not raise; the incomplete tail is simply discarded,
    # matching stream_events' own "ends the generator, never raises"
    # contract.
    chunks = [frame, b"event: dispatch_start\ndata: {\"incomplete"]
    assert _filtered(chunks, "clio") == [frame]


def test_filter_composes_with_stream_events_end_to_end():
    body = (
        _dispatch_start_frame("t-1", target="clio", caller="agent:iris")
        + _dispatch_success_frame("t-1")
        + _dispatch_start_frame("t-2", target="warden", caller="agent:iris")
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    client = CabalClient(
        "http://cabal.local", "op-token",
        async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    async def run() -> list[bytes]:
        return [
            c async for c in filter_cabal_event_stream(client.stream_events(), "clio")
        ]

    result = asyncio.run(run())
    assert result == [
        _dispatch_start_frame("t-1", target="clio", caller="agent:iris"),
        _dispatch_success_frame("t-1"),
    ]


# --- filter_cabal_event_stream: thread exclusion (Critical 3 fix) -----------
#
# A thread turn's relay stamps `metadata: {"thread": <id>}` on the envelope
# (`kobo_api.main.post_thread_turn`) -- the switchboard's own dispatch_start
# event for that task echoes the submitted envelope, `metadata` included,
# same as `target`/`caller` already do. A thread is a private,
# initiator-only conversation (operator ruling) and must never reach the
# SSE stream at all -- regardless of `?agent=` scoping, and even when no
# `agent` is given at all (the route's own unfiltered-passthrough case
# before this fix).


def test_filter_drops_a_thread_tagged_dispatch_start_with_no_agent_scoping():
    frame = _dispatch_start_frame(
        "t-1", target="clio", caller="operator:kgreiner@example.com", metadata={"thread": 7}
    )
    assert _filtered_no_agent([frame]) == []


def test_filter_forwards_a_non_thread_dispatch_start_with_no_agent_scoping():
    frame = _dispatch_start_frame("t-1", target="clio", caller="agent:iris")
    assert _filtered_no_agent([frame]) == [frame]


def test_filter_drops_a_later_event_for_a_threads_task_id_with_no_agent_scoping():
    start = _dispatch_start_frame(
        "t-1", target="clio", caller="operator:kgreiner@example.com", metadata={"thread": 7}
    )
    success = _dispatch_success_frame("t-1")
    assert _filtered_no_agent([start, success]) == []


def test_filter_drops_a_thread_tagged_dispatch_start_even_when_it_matches_the_scoped_agent():
    # Thread exclusion takes precedence over agent-matching -- a thread turn
    # relayed to an agent the caller is scoping to must still never surface.
    frame = _dispatch_start_frame(
        "t-1", target="clio", caller="operator:kgreiner@example.com", metadata={"thread": 7}
    )
    assert _filtered([frame], "clio") == []


def test_filter_unrelated_metadata_is_not_mistaken_for_a_thread():
    # Regression guard, mirrors _flatten_conversation_entries' own: only the
    # "thread" key trips exclusion.
    frame = _dispatch_start_frame(
        "t-1", target="clio", caller="agent:iris", metadata={"something_else": True}
    )
    assert _filtered_no_agent([frame]) == [frame]


def test_cabal_client_default_http_client_has_generous_timeout():
    client = CabalClient("http://cabal.local", "op-token")
    timeout = client._client.timeout
    assert timeout.connect == 10.0
    assert timeout.read == 30.0


def test_cabal_client_strips_trailing_slash_from_base_url():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={})

    client = CabalClient(
        "http://cabal.local/", "op-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client.absorb("clio")

    assert captured["url"] == "http://cabal.local/admin/persona/clio/absorb"


# --- cabal_enabled / cabal_operator_token (Task 1: switchboard truthiness gate) --


def test_cabal_enabled_true_when_both_keys_and_env_token_present(monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    config = Config(
        cabal_url="http://cabal.local",
        agent_endpoint_template="http://agent-{name}.default.svc.cluster.local",
    )
    assert cabal_enabled(config) is True


def test_cabal_enabled_false_when_env_token_is_empty_string(monkeypatch):
    # An empty string is falsy the same way an unset var is - a mounted
    # env var that resolved to "" (a misconfigured Secret, say) must not
    # read as "configured".
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "")
    config = Config(
        cabal_url="http://cabal.local",
        agent_endpoint_template="http://agent-{name}",
    )
    assert cabal_enabled(config) is False


def test_cabal_enabled_false_when_nothing_configured(monkeypatch):
    monkeypatch.delenv("CABAL_OPERATOR_TOKEN", raising=False)
    assert cabal_enabled(Config()) is False


# --- Fakes satisfy the Protocols ----------------------------------------------


# --- FakeGojira.delete_app (Task 3: agent destruction) ---------------------------


# --- FakeGitHub.rename_repo / archive_repo (Task 3: agent destruction) -----------


# --- FakeSlack.delete_app (Task 3: agent destruction) ----------------------------


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


def test_clients_real_wires_name_client_from_vertex_and_name_config():
    # Same "always constructible" treatment as image above (never gated
    # behind an "is this configured" check) — project comes from the
    # existing vertex_project field, location/model from the new
    # name_location/name_model.
    config = Config(
        gojira_url="http://gojira.local",
        gcp_project="example-project",
        vertex_project="acme-model-project",
        name_location="us-east4",
        name_model="some-text-model",
    )
    clients = Clients.real(config)
    assert isinstance(clients.name, VertexNameClient)
    assert clients.name._project == "acme-model-project"
    assert clients.name._location == "us-east4"
    assert clients.name._model == "some-text-model"


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
    assert isinstance(clients.slack.token_store, FileSlackTokenStore)


# --- Slack token store selection (two-replica safety) -------------------------


def test_slack_token_store_none_when_neither_setting_is_configured():
    assert slack_token_store(Config(), FakeKubectl()) is None


def test_slack_token_store_prefers_the_secret_and_warns_once(tmp_path, caplog):
    config = Config(
        slack_tokens_path=tmp_path / "slack.local.yaml",
        slack_tokens_secret="kobo/kobo-slack-tokens",
    )
    kubectl = FakeKubectl()

    with caplog.at_level(logging.WARNING, logger="kobo_api.clients"):
        store = slack_token_store(config, kubectl)

    assert isinstance(store, KubeSlackTokenStore)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "slack_tokens_secret" in warnings[0].getMessage()
    assert "slack_tokens_path" in warnings[0].getMessage()


def test_clients_real_wires_slack_from_the_secret_store(tmp_path):
    config = Config(
        gojira_url="http://gojira.local",
        gcp_project="example-project",
        slack_tokens_secret="kobo/kobo-slack-tokens",
    )
    clients = Clients.real(config)

    assert isinstance(clients.slack, HttpxSlackClient)
    store = clients.slack.token_store
    assert isinstance(store, KubeSlackTokenStore)
    # The same kubectl the rest of the room shells out through -- one
    # binary, one runner, no second client built just for this read.
    assert store._kubectl is clients.kubectl


def test_clients_real_wires_storage_client_unconditionally():
    # Like image, storage is always constructible (gcp_project already
    # exists in Config for other clients) -> Clients.real() never gates it
    # behind an "is this configured" check the way it does slack.
    config = Config(gojira_url="http://gojira.local", gcp_project="example-project")
    clients = Clients.real(config)
    assert isinstance(clients.storage, HttpxStorageClient)
    assert clients.storage._project == "example-project"


def test_clients_real_shares_one_cached_gcloud_token_across_image_name_and_storage():
    # Perf fix: every real client that defaults to `_gcloud_print_access_token`
    # (VertexImageClient, VertexNameClient, HttpxStorageClient) previously
    # shelled out to `gcloud auth print-access-token` on every single call
    # (~1-2s live) even though the token is good for about an hour.
    # Clients.real() must wire ONE shared CachedGcloudToken instance into all
    # three, so a token fetched for one (e.g. a name suggestion) is reused by
    # the others (an avatar generation, a storage read) within its ttl.
    config = Config(
        gojira_url="http://gojira.local",
        gcp_project="example-project",
        vertex_project="acme-model-project",
    )
    clients = Clients.real(config)
    assert isinstance(clients.image._token_provider, CachedGcloudToken)
    assert clients.image._token_provider is clients.name._token_provider
    assert clients.image._token_provider is clients.storage._token_provider


def test_clients_real_wires_cabal_none_when_disabled(monkeypatch):
    # Same "is this configured" gate as slack, but via cabal_enabled (all
    # three of cabal_url/agent_endpoint_template/the operator token env var)
    # rather than a single field. Nothing set -> stays None, same "absent =
    # feature off" convention as slack.
    monkeypatch.delenv("CABAL_OPERATOR_TOKEN", raising=False)
    config = Config(gojira_url="http://gojira.local", gcp_project="example-project")
    clients = Clients.real(config)
    assert clients.cabal is None


def test_clients_real_wires_cabal_when_enabled(monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    config = Config(
        gojira_url="http://gojira.local",
        gcp_project="example-project",
        cabal_url="http://cabal.internal",
        agent_endpoint_template="http://agent-{name}.default.svc.cluster.local",
    )
    clients = Clients.real(config)
    assert isinstance(clients.cabal, CabalClient)
    assert clients.cabal._base_url == "http://cabal.internal"
    assert clients.cabal._token == "op-token"


# --- Clients.real() secrets-platform wiring (kobo_api.sero) -----------------


@pytest.mark.parametrize(
    "settings",
    [
        {},
        {"sero_url": "http://secrets.example"},
        {"sero_url": "http://secrets.example", "sero_audience": "aud"},
        {"sero_audience": "aud", "sero_namespace": "platform"},
    ],
)
def test_clients_real_leaves_sero_none_until_all_three_settings_are_set(settings):
    # Same "is this configured" gate shape as cabal above, via
    # Config.sero_enabled: a half-configured lane reads as off, and the
    # room keeps writing per-agent Secrets directly.
    config = Config(
        gojira_url="http://gojira.local", gcp_project="example-project", **settings
    )
    assert Clients.real(config).sero is None


def test_clients_real_wires_sero_when_all_three_settings_are_set():
    from kobo_api.sero import HttpxSeroClient

    config = Config(
        gojira_url="http://gojira.local",
        gcp_project="example-project",
        sero_url="http://secrets.example",
        sero_audience="http://secrets.example",
        sero_namespace="platform",
    )

    clients = Clients.real(config)

    assert isinstance(clients.sero, HttpxSeroClient)
    assert clients.sero._base_url == "http://secrets.example"
    assert clients.sero._namespace == "platform"
    # Its credential is this workload's own identity, minted for the
    # configured audience -- nothing configured, nothing stored.
    assert clients.sero._token_provider._audience == "http://secrets.example"


# --- Clients.real() group resolver wiring (access-control arc, Task B1) ------


def test_clients_real_leaves_groups_none_when_okta_token_env_unset(monkeypatch):
    monkeypatch.delenv("OKTA_API_TOKEN", raising=False)
    config = Config(
        gojira_url="http://gojira.local",
        gcp_project="example-project",
        okta_domain="acme.okta.com",
        groups=[GroupChoice(name="SRE", okta_group="okta-sre")],
    )
    clients = Clients.real(config)
    assert clients.groups is None


def test_clients_real_leaves_groups_none_when_groups_empty(monkeypatch):
    # Empty universe -> not constructed at all, even with a token and a
    # domain set: there's nothing for it to resolve against.
    monkeypatch.setenv("OKTA_API_TOKEN", "okta-token")
    config = Config(
        gojira_url="http://gojira.local",
        gcp_project="example-project",
        okta_domain="acme.okta.com",
        groups=[],
    )
    clients = Clients.real(config)
    assert clients.groups is None


def test_clients_real_wires_groups_when_fully_configured(monkeypatch):
    monkeypatch.setenv("OKTA_API_TOKEN", "okta-token")
    config = Config(
        gojira_url="http://gojira.local",
        gcp_project="example-project",
        okta_domain="acme.okta.com",
        groups=[
            GroupChoice(name="SRE", okta_group="okta-sre"),
            GroupChoice(name="QA", okta_group="okta-qa"),
        ],
    )
    clients = Clients.real(config)
    assert isinstance(clients.groups, OktaGroupResolver)
    assert clients.groups._domain == "acme.okta.com"
    assert clients.groups._token == "okta-token"
    # Inverted at construction (okta name -> config name) -- see
    # kobo_api.groups._invert_universe.
    assert clients.groups._universe == {"okta-sre": "SRE", "okta-qa": "QA"}


def test_universe_from_groups_merges_duplicate_config_names():
    """The flagged Low this amendment fixes: `config.groups` is a LIST, so
    nothing stops two entries sharing a `name` -- the old
    `{group.name: group.okta_group for group in config.groups}` dict
    comprehension silently kept only the LAST entry's okta name. The
    replacement must union both entries' okta names instead of dropping
    either."""
    groups = [
        GroupChoice(name="SRE", okta_group="okta-sre-usa"),
        GroupChoice(name="SRE", okta_group="okta-sre-india"),
    ]

    universe = _universe_from_groups(groups)

    assert universe == {"SRE": ["okta-sre-usa", "okta-sre-india"]}


# --- Clients.real() people-search wiring (access-tab refinement wave) --------
#
# Deliberately independent of `config.groups` (unlike `clients.groups`
# above, which stays None on an empty `groups` list) -- people search is a
# directory-wide lookup, not an ACL feature, so it only ever needs the two
# things every Okta call in this room needs: the env token and the domain.


def test_clients_real_wires_people_search_even_with_no_acl_groups_configured(monkeypatch):
    # The contrast with `clients.groups` above: an empty `groups` list
    # leaves the ACL resolver unwired, but people search has nothing to do
    # with ACL groups at all -- token + domain alone are enough.
    monkeypatch.setenv("OKTA_API_TOKEN", "okta-token")
    config = Config(
        gojira_url="http://gojira.local",
        gcp_project="example-project",
        okta_domain="acme.okta.com",
        groups=[],
    )
    clients = Clients.real(config)
    assert clients.groups is None
    assert isinstance(clients.people_search, OktaPeopleSearch)
    assert clients.people_search._domain == "acme.okta.com"
    assert clients.people_search._token == "okta-token"


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
    provider = installation_token_provider(config)
    github = GhCLI(
        runner=CommandRunner(), token_provider=provider.token if provider else None
    )
    assert github._token_provider is not None
    assert github._token_provider.__self__._app_id == "123"


# --- GitHub (delete_branch / compare -- the room recycling its own branch) -----


def test_delete_branch_sends_the_ref_path_with_its_slash_intact():
    """The git-refs route takes a REF, not a path segment: `heads/kobo/env`
    is the whole thing and its slashes are part of the path. Encoding them
    asked GitHub to delete a ref literally named `heads/kobo%2Fenv`, which
    is a 404, which this method then reported as "already gone" -- so a
    branch the room meant to recycle stayed exactly where it was."""
    runner = RecordingRunner(results=[(0, "", "")])
    client = GhCLI(runner=runner)

    assert client.delete_branch("acme/clio", "kobo/secrets-wiring") is True
    assert runner.calls == [
        [
            "gh", "api", "-X", "DELETE",
            "repos/acme/clio/git/refs/heads/kobo/secrets-wiring",
        ]
    ]
    # And the same spelling `branch_exists` asks about, which is the point:
    # one of them finding a branch the other cannot delete is the bug.
    assert "heads/kobo/secrets-wiring" in runner.calls[0][-1]


def test_delete_branch_false_when_it_is_already_gone():
    runner = RecordingRunner(
        results=[(1, '{"message":"Reference does not exist"}', "gh: Not Found (HTTP 404)")]
    )
    client = GhCLI(runner=runner)

    assert client.delete_branch("acme/clio", "kobo/env") is False


def test_delete_branch_raises_for_any_other_failure():
    runner = RecordingRunner(results=[(1, "", "gh: Bad credentials (HTTP 401)")])
    client = GhCLI(runner=runner)

    with pytest.raises(ClientError):
        client.delete_branch("acme/clio", "kobo/env")


def test_compare_reports_the_files_and_the_commit_authors():
    payload = json.dumps(
        {
            "files": [{"filename": "gojira.yaml"}],
            "commits": [
                {"commit": {"author": {"email": "noreply@kobo.dev"}}},
                {"commit": {"author": {"email": "noreply@kobo.dev"}}},
            ],
        }
    )
    runner = RecordingRunner(results=[(0, payload, "")])
    client = GhCLI(runner=runner)

    answer = client.compare("acme/clio", "HEAD", "kobo/env")

    assert answer.found is True
    assert answer.files == ("gojira.yaml",)
    assert answer.authors == ("noreply@kobo.dev", "noreply@kobo.dev")
    # Both refs are ONE path segment on this route, unlike the git-refs
    # route above, so a slash in a branch name has to be encoded here.
    assert runner.calls == [["gh", "api", "repos/acme/clio/compare/HEAD...kobo%2Fenv"]]


def test_compare_of_a_ref_that_is_not_there_is_not_found_rather_than_empty():
    """An empty file list from a failed lookup and an empty one from an
    identical branch are the same value and opposite answers, which is why
    `found` exists and why a caller must check it."""
    runner = RecordingRunner(
        results=[(1, '{"message":"Not Found"}', "gh: Not Found (HTTP 404)")]
    )
    client = GhCLI(runner=runner)

    answer = client.compare("acme/clio", "HEAD", "kobo/env")

    assert answer.found is False
    assert answer.files == ()


def test_compare_raises_for_any_other_failure():
    runner = RecordingRunner(results=[(1, "", "gh: Bad credentials (HTTP 401)")])
    client = GhCLI(runner=runner)

    with pytest.raises(ClientError):
        client.compare("acme/clio", "HEAD", "kobo/env")


def test_close_pr_comments_before_it_closes():
    """A closed request of the room's always carries the reason, even if
    the close itself then fails and is retried."""
    runner = RecordingRunner(results=[(0, "", ""), (0, "", "")])
    client = GhCLI(runner=runner)

    client.close_pr("acme/clio", "https://example.test/pull/1", "superseded")

    assert runner.calls[0][:3] == ["gh", "pr", "comment"]
    assert runner.calls[0][-1] == "superseded"
    assert runner.calls[1][:3] == ["gh", "pr", "close"]


def test_close_pr_tolerates_one_that_is_already_closed():
    runner = RecordingRunner(
        results=[(0, "", ""), (1, "", "gh: Not Found (HTTP 404)")]
    )
    client = GhCLI(runner=runner)

    client.close_pr("acme/clio", "https://example.test/pull/1", "superseded")


# --- Model access without a group: the Vertex role on the project ---------------


def test_grant_model_access_argv():
    runner = RecordingRunner()
    client = GcloudClient("example-project", runner=runner)
    client.grant_model_access("agent-clio@example-project.iam.gserviceaccount.com", "models-project")
    assert runner.calls == [
        [
            "gcloud", "projects", "add-iam-policy-binding", "models-project",
            "--member", "serviceAccount:agent-clio@example-project.iam.gserviceaccount.com",
            "--role", "roles/aiplatform.user", "--condition=None",
        ]
    ]


def test_revoke_model_access_absent_binding_returns_false():
    runner = RecordingRunner(
        results=[(1, "", "ERROR: Policy binding with the specified principal and role not found!")]
    )
    client = GcloudClient("example-project", runner=runner)
    assert client.revoke_model_access("e@example-project.iam.gserviceaccount.com", "models-project") is False


def test_revoke_model_access_reraises_other_errors():
    runner = RecordingRunner(results=[(1, "", "PERMISSION_DENIED")])
    client = GcloudClient("example-project", runner=runner)
    with pytest.raises(ClientError):
        client.revoke_model_access("e@example-project.iam.gserviceaccount.com", "models-project")



def test_repo_exists_is_false_for_a_name_github_only_redirects():
    """Destroy renames an agent's repo to archived-agent-<name>-<id>; GitHub
    keeps answering at the old name through a redirect. That name is free."""
    runner = RecordingRunner(
        results=[(0, '{"full_name":"acme/archived-agent-clio-5","archived":true}', "")]
    )
    client = GhCLI(runner=runner)

    assert client.repo_exists("acme/agent-clio") is False
    assert runner.calls == [["gh", "api", "repos/acme/agent-clio"]]


def test_repo_exists_is_true_for_the_repo_itself_whatever_the_case():
    runner = RecordingRunner(results=[(0, '{"full_name":"Acme/Agent-Clio"}', "")])
    client = GhCLI(runner=runner)

    assert client.repo_exists("acme/agent-clio") is True


def test_repo_exists_is_false_on_a_404():
    runner = RecordingRunner(results=[(1, "", "gh: Not Found (HTTP 404)")])
    client = GhCLI(runner=runner)

    assert client.repo_exists("acme/agent-clio") is False
