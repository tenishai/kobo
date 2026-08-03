"""Platform clients for cortex-api.

Every operation the provisioner performs against the outside world (GitHub,
gojira, GCP, an already-deployed agent's a2a endpoint, kubectl) sits behind a
`typing.Protocol` here, so provisioning logic never depends on a concrete
backend directly — only on these shapes. This mirrors gojira's own
GCPClient/KubectlClient/GitHubClient seam (`gojira/gcp.py`, `gojira/k8s.py`,
`gojira/github.py`): steps are testable against in-memory fakes
(`tests/fakes.py`) and the real backend is swappable.

`CommandRunner` is gojira's shell seam (`gojira/shell.py`) copied in spirit,
with one difference: it folds the raise-on-failure decision into the runner
itself (`run()` raises `ClientError` on a non-zero exit) rather than handing
back a raw return code for every caller to check. Callers that need to
tolerate one particular failure (e.g. a describe-then-create existence check,
or gcloud's "already a member" on a repeat group-add) catch `ClientError` and
inspect its `.stderr`.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypedDict, runtime_checkable

import httpx

from cortex_api.config import Config

log = logging.getLogger(__name__)

# git commit author for commits cortex makes on the caller's behalf (setup
# commits, artifact pushes) — explicit so it works in environments with no
# git user configured, mirroring gojira's GhCLI (gojira/github.py).
COMMIT_AUTHOR_NAME = "Cortex"
COMMIT_AUTHOR_EMAIL = "noreply@cortex.dev"


class ClientError(RuntimeError):
    """A shelled-out command or platform API call failed."""

    def __init__(
        self, message: str, *, returncode: int = 1, stdout: str = "", stderr: str = ""
    ):
        super().__init__(message)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class GojiraConflict(RuntimeError):
    """gojira already has an app connected for this repo (409 on create)."""


class SlackApiError(ClientError):
    """Slack's Web API returned `ok: false` (a non-auth failure, or a token
    rotation attempt that itself failed) — a ClientError subclass so it's
    caught by the same 502 mapping every other ClientError gets, without
    routes needing a Slack-specific except clause.
    """


class CommandRunner:
    """Runs a subprocess command; returns stdout, raises ClientError on rc != 0."""

    def run(
        self, args: list[str], stdin: str | None = None, *, redact_args: bool = False
    ) -> str:
        # Final-review Finding 3: this debug line used to log the full argv
        # unconditionally — for `KubectlCLI.apply_secret`'s stage-1 call,
        # that argv carries `--from-literal=SLACK_*_TOKEN=<value>`, so one
        # `--log-level debug` away put pasted Slack tokens straight into
        # logs. `redact_args=True` (passed by `apply_secret`, both stages)
        # logs a fixed placeholder instead of the real argv; every other
        # caller is unaffected (default False, unchanged behavior).
        log.debug("exec: %s", "<redacted argv>" if redact_args else " ".join(args))
        try:
            proc = subprocess.run(args, input=stdin, capture_output=True, text=True, check=False)
        except FileNotFoundError as exc:
            raise ClientError(f"{args[0]}: not found on PATH") from exc
        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()
        if proc.returncode != 0:
            raise ClientError(
                f"{' '.join(args)} failed (rc={proc.returncode}): {stderr or stdout}",
                returncode=proc.returncode,
                stdout=stdout,
                stderr=stderr,
            )
        return stdout

    def run_bytes(
        self, args: list[str], stdin: bytes | None = None, *, redact_args: bool = False
    ) -> bytes:
        """Like `run()`, but for commands whose stdout is binary (e.g. `git
        archive`, cortex_api.composer's skill-pinning materialization) —
        `run()`'s `text=True` decode-and-strip would corrupt a tar archive's
        bytes, so this skips both and hands back stdout untouched. Still
        raises ClientError on rc != 0, decoding stderr/stdout only for the
        error message.

        `redact_args` is the same debug-log seam as `run()`'s (see there for
        the rationale) — carried here too so any future binary-stdout caller
        with secret-bearing argv gets the same option, even though no
        current caller of `run_bytes` needs it.
        """
        log.debug("exec (bytes): %s", "<redacted argv>" if redact_args else " ".join(args))
        try:
            proc = subprocess.run(args, input=stdin, capture_output=True, text=False, check=False)
        except FileNotFoundError as exc:
            raise ClientError(f"{args[0]}: not found on PATH") from exc
        if proc.returncode != 0:
            stderr = proc.stderr.decode(errors="replace").strip()
            stdout = proc.stdout.decode(errors="replace").strip()
            raise ClientError(
                f"{' '.join(args)} failed (rc={proc.returncode}): {stderr or stdout}",
                returncode=proc.returncode,
                stdout=stdout,
                stderr=stderr,
            )
        return proc.stdout


# --- GitHub -------------------------------------------------------------------


@runtime_checkable
class GitHubClient(Protocol):
    def repo_exists(self, repo: str) -> bool:
        """Whether `repo` (owner/name) exists."""
        ...

    def create_repo_from_dir(self, repo: str, src: Path) -> None:
        """git-init a freshly-composed directory and push it as a new private repo."""
        ...

    def clone(self, repo: str, dest: Path) -> None: ...

    def commit_push(self, workdir: Path, message: str) -> bool:
        """Stage, commit, and push everything under workdir. False if nothing changed."""
        ...

    def local_head(self, workdir: Path) -> str:
        """The just-pushed local clone's head commit sha (`git rev-parse HEAD`
        against `workdir`), not a fresh API read.

        Deliberately local rather than `head_sha(repo)`: right after
        `commit_push`, GitHub's API can lag a moment before it reflects the
        push (the same read-after-write lag `runs_for_commit` callers already
        have to tolerate) — the local clone's HEAD is the pushed sha with no
        such lag, since it's the commit this process itself just made.
        """
        ...

    def head_sha(self, repo: str) -> str:
        """The current head commit sha of `repo`'s default branch.

        Deliberately `gh api repos/<repo>/commits --jq '.[0].sha'` rather than
        `gh api repos/<repo>/commits/<branch>`: the latter needs the default
        branch's *name* first (another round trip, or a hardcoded guess like
        "main"); GitHub's commits-list endpoint already defaults to the repo's
        default branch when no ref is given, so its first entry's sha is the
        exact same value for one shell-out instead of two.
        """
        ...

    def runs_for_commit(self, repo: str, sha: str) -> list[dict]:
        """Workflow runs triggered by commit `sha`, newest first — each a
        {"id": int, "status": str, "conclusion": str | None} dict.

        Keyed by commit rather than "latest across the whole repo" (the old
        `latest_run`'s mistake): a run for some other commit — a regeneration
        PR's own CI, say — can never masquerade as this commit's result.
        Empty means nothing has registered against this sha *yet*, which
        callers must treat as "keep polling", not "not found".
        """
        ...

    def rerun(self, repo: str, run_id: int) -> None:
        """Re-run workflow run `run_id` (gh reuses the same id, it doesn't
        mint a new one) — lets a step self-heal a first-run auth-propagation
        failure instead of an operator having to `gh run rerun` by hand.
        """
        ...


class GhCLI:
    """git + gh backed GitHubClient, using the operator's `gh auth` session.

    Mirrors gojira's GhCLI (gojira/github.py): auth-free local git plumbing
    plus `gh` for anything that needs the GitHub API.
    """

    def __init__(self, runner: CommandRunner | None = None, git: str = "git", gh: str = "gh"):
        self._runner = runner or CommandRunner()
        self._git = git
        self._gh = gh

    def repo_exists(self, repo: str) -> bool:
        try:
            self._runner.run([self._gh, "repo", "view", repo])
            return True
        except ClientError:
            return False

    def create_repo_from_dir(self, repo: str, src: Path) -> None:
        self._runner.run([self._git, "-C", str(src), "init"])
        self._runner.run([self._git, "-C", str(src), "branch", "-M", "main"])
        self._runner.run([self._git, "-C", str(src), "add", "-A"])
        self._runner.run(
            [
                self._git, "-C", str(src),
                "-c", f"user.name={COMMIT_AUTHOR_NAME}",
                "-c", f"user.email={COMMIT_AUTHOR_EMAIL}",
                "commit", "-m", "Initial commit",
            ]
        )
        self._runner.run(
            [self._gh, "repo", "create", repo, "--private", "--source", str(src), "--push"]
        )

    def clone(self, repo: str, dest: Path) -> None:
        self._runner.run([self._gh, "repo", "clone", repo, str(dest)])

    def commit_push(self, workdir: Path, message: str) -> bool:
        self._runner.run([self._git, "-C", str(workdir), "add", "-A"])
        status = self._runner.run([self._git, "-C", str(workdir), "status", "--porcelain"])
        if not status:
            log.info("nothing to commit in %s", workdir)
            return False
        self._runner.run(
            [
                self._git, "-C", str(workdir),
                "-c", f"user.name={COMMIT_AUTHOR_NAME}",
                "-c", f"user.email={COMMIT_AUTHOR_EMAIL}",
                "commit", "-m", message,
            ]
        )
        self._runner.run([self._git, "-C", str(workdir), "push"])
        return True

    def local_head(self, workdir: Path) -> str:
        return self._runner.run([self._git, "-C", str(workdir), "rev-parse", "HEAD"])

    def head_sha(self, repo: str) -> str:
        return self._runner.run(
            [self._gh, "api", f"repos/{repo}/commits", "--jq", ".[0].sha"]
        )

    def runs_for_commit(self, repo: str, sha: str) -> list[dict]:
        out = self._runner.run(
            [
                self._gh, "run", "list", "--repo", repo, "--commit", sha,
                "--json", "databaseId,status,conclusion",
            ]
        )
        runs = json.loads(out) if out else []
        return [
            {
                "id": run["databaseId"],
                "status": run["status"],
                "conclusion": run.get("conclusion"),
            }
            for run in runs
        ]

    def rerun(self, repo: str, run_id: int) -> None:
        self._runner.run([self._gh, "run", "rerun", str(run_id), "--repo", repo])


# --- Gojira ---------------------------------------------------------------------


@runtime_checkable
class GojiraClient(Protocol):
    def create_app(
        self, *, name: str, repo_full_name: str, cluster: str, namespace: str
    ) -> int:
        """POST /api/v1/apps; returns the new app id. Raises GojiraConflict on 409."""
        ...

    def app_status(self, app_id: int) -> dict:
        """GET /api/v1/apps/{id}."""
        ...

    def reprovision(self, app_id: int) -> None:
        """POST /apps/{id}/provision to re-run the provisioner."""
        ...


class HttpxGojiraClient:
    """Talks to gojira's onboarding API over httpx."""

    def __init__(
        self, base_url: str, *, token: str | None = None, client: httpx.Client | None = None
    ):
        self._base_url = base_url.rstrip("/")
        self._token = token
        # gojira's onboarding calls were fast in P0 (no model roundtrip
        # involved); this default just gives headroom over httpx's bare 5s.
        self._client = client or httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))

    def _headers(self) -> dict[str, str]:
        if self._token:
            return {"Authorization": f"Bearer {self._token}"}
        return {}

    def create_app(self, *, name, repo_full_name, cluster, namespace) -> int:
        resp = self._client.post(
            f"{self._base_url}/api/v1/apps",
            json={
                "name": name,
                "repo_full_name": repo_full_name,
                "workload_class": "agent",
                "environments": [{"name": "dev", "cluster": cluster, "namespace": namespace}],
                "auto_merge_setup_pr": True,
            },
            headers=self._headers(),
        )
        if resp.status_code == 409:
            raise GojiraConflict(f"app already connected for {repo_full_name!r}")
        if resp.status_code != 201:
            raise ClientError(f"create_app failed ({resp.status_code}): {resp.text}")
        return resp.json()["id"]

    def app_status(self, app_id: int) -> dict:
        resp = self._client.get(
            f"{self._base_url}/api/v1/apps/{app_id}", headers=self._headers()
        )
        if resp.status_code != 200:
            raise ClientError(f"app_status({app_id}) failed ({resp.status_code}): {resp.text}")
        return resp.json()

    def reprovision(self, app_id: int) -> None:
        resp = self._client.post(
            f"{self._base_url}/apps/{app_id}/provision",
            headers=self._headers(),
            follow_redirects=False,
        )
        if resp.status_code != 303:
            raise ClientError(f"reprovision({app_id}) failed ({resp.status_code}): {resp.text}")


# --- Cloud (GCP) ------------------------------------------------------------------


@runtime_checkable
class CloudClient(Protocol):
    def ensure_gsa(self, name: str) -> str:
        """describe-then-create a GSA named `name`; returns its email."""
        ...

    def bind_workload_identity(self, gsa_email: str, namespace: str, ksa: str) -> None: ...

    def add_to_group(self, gsa_email: str, group: str) -> None:
        """Add gsa_email to group; tolerates it already being a member."""
        ...


def gsa_email(name: str, project: str) -> str:
    """The GSA email convention for a GCP service account: `{name}@{project}.iam.gserviceaccount.com`.

    Prefix-agnostic: this is only the email-format helper, not the naming
    policy — callers that need an agent's GSA build the full `agent-<name>`
    id themselves before calling this (both `GcloudClient.ensure_gsa` below
    and `WireStep`, `cortex_api/steps.py`, which needs the same email for the
    KSA's Workload Identity annotation), so the real client and `FakeCloud`
    (`tests/fakes.py`) now behave identically for any name it's given.
    """
    return f"{name}@{project}.iam.gserviceaccount.com"


class GcloudClient:
    """gcloud-backed CloudClient, scoped to one GCP project."""

    def __init__(self, project: str, runner: CommandRunner | None = None, gcloud: str = "gcloud"):
        self._project = project
        self._runner = runner or CommandRunner()
        self._gcloud = gcloud

    def ensure_gsa(self, name: str) -> str:
        # `name` is always the caller's full "agent-<agent name>" SA id (the
        # only caller, AccessStep, constructs it that way); gsa_email() is
        # prefix-agnostic, so it's used as-is rather than round-tripped
        # through a stripped-then-reattached prefix.
        email = gsa_email(name, self._project)
        try:
            self._runner.run(
                [
                    self._gcloud, "iam", "service-accounts", "describe", email,
                    "--project", self._project,
                ]
            )
            return email
        except ClientError as exc:
            # Only a genuine "no such GSA" describe failure falls through to
            # create — anything else (permission denied, a transient API
            # error, ...) is a real problem the caller needs to see, not
            # something a blind create-on-any-failure should paper over.
            stderr = exc.stderr.lower()
            if "not_found" not in stderr and "does not exist" not in stderr:
                raise
        self._runner.run(
            [
                self._gcloud, "iam", "service-accounts", "create", name,
                "--project", self._project, "--display-name", name,
            ]
        )
        return email

    def bind_workload_identity(self, gsa_email: str, namespace: str, ksa: str) -> None:
        member = f"serviceAccount:{self._project}.svc.id.goog[{namespace}/{ksa}]"
        self._runner.run(
            [
                self._gcloud, "iam", "service-accounts", "add-iam-policy-binding", gsa_email,
                "--project", self._project,
                "--role", "roles/iam.workloadIdentityUser",
                "--member", member,
            ]
        )

    def add_to_group(self, gsa_email: str, group: str) -> None:
        try:
            self._runner.run(
                [
                    self._gcloud, "identity", "groups", "memberships", "add",
                    "--group-email", group, "--member-email", gsa_email,
                ]
            )
        except ClientError as exc:
            stderr = exc.stderr.lower()
            if "already exists" in stderr or "already a member" in stderr:
                return
            raise


# --- Agent (a2a chat over a kubectl port-forward) -----------------------------


class ChatMessage(TypedDict):
    """One turn of an a2a chat history."""

    role: str  # "user" | "agent"
    text: str


@runtime_checkable
class AgentClient(Protocol):
    def hello(self, namespace: str, service: str, text: str) -> str:
        """Say `text` to the agent's a2a endpoint; return its reply text."""
        ...

    def converse(self, namespace: str, service: str, messages: list[ChatMessage]) -> str:
        """Send the full `messages` history to the agent's a2a endpoint as
        one task; return its reply text."""
        ...


_FORWARDING_RE = re.compile(r"Forwarding from 127\.0\.0\.1:(\d+)")


def _wait_for_forwarded_port(reader, timeout_s: float = 10.0) -> int:
    """Read lines from `reader` (anything exposing `.readline() -> str`, like a
    subprocess's stdout) until kubectl's "Forwarding from 127.0.0.1:PORT" line
    appears; return the parsed port.

    `reader.readline()` blocks indefinitely on a real OS pipe (kubectl may
    simply never print the line), so it can't be called directly from the
    thread that owns the deadline: a deadline check only between calls does
    nothing to interrupt a call already in progress. Instead, a daemon thread
    drives `readline()` in a loop and pushes each line onto a `queue.Queue`;
    this function only ever blocks on `queue.Queue.get(timeout=...)`, computed
    against a monotonic deadline, so a hung `readline()` can never prevent
    this function (or a caller's `finally`-cleanup) from returning once the
    deadline passes. The reader thread is left running (it's a daemon, so it
    won't block interpreter exit); on a real subprocess, terminating/killing
    the process in the caller's `finally` closes the pipe and lets it exit.

    Factored out from `hello()` so this parsing logic is unit-testable against
    a fake reader, with no real subprocess involved.
    """
    lines: queue.Queue[str] = queue.Queue()

    def _pump() -> None:
        while True:
            line = reader.readline()
            lines.put(line)
            if not line:
                return

    threading.Thread(target=_pump, daemon=True).start()

    deadline = time.monotonic() + timeout_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ClientError("timed out waiting for port-forward")
        try:
            line = lines.get(timeout=remaining)
        except queue.Empty:
            raise ClientError("timed out waiting for port-forward") from None
        if not line:
            continue
        match = _FORWARDING_RE.search(line)
        if match:
            return int(match.group(1))


def _build_task_envelope(messages: list[ChatMessage]) -> dict:
    return {
        "kind": "task",
        "id": f"chat-{uuid.uuid4().hex}",
        "history": [
            {
                "kind": "message",
                "role": message["role"],
                "parts": [{"kind": "text", "text": message["text"]}],
            }
            for message in messages
        ],
    }


def _post_task(client: httpx.Client, port: int, messages: list[ChatMessage]) -> str:
    """POST the a2a task envelope for `messages` to the forwarded port; return
    the reply text.

    Factored out from `_send_task()` so the request/response shape is
    unit-testable against an httpx.MockTransport-backed client, with no real
    network involved.

    httpx transport errors (timeouts, connection failures, ...) aren't
    ClientError, so left alone they'd surface as raw httpx exceptions to
    callers that only expect ClientError. Wrap them here, preserving the
    original exception as the cause.

    A 200 response doesn't guarantee a well-formed reply: a non-JSON body
    raises json.JSONDecodeError (a ValueError), and JSON missing the expected
    path raises KeyError/IndexError/TypeError — none of those are ClientError
    either, so they're wrapped the same way.
    """
    try:
        resp = client.post(f"http://127.0.0.1:{port}/a2a/tasks", json=_build_task_envelope(messages))
    except httpx.HTTPError as exc:
        raise ClientError(f"a2a request failed: {exc!r}") from exc
    if resp.status_code != 200:
        raise ClientError(f"a2a request failed ({resp.status_code}): {resp.text}")
    try:
        return resp.json()["status"]["message"]["parts"][0]["text"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ClientError(
            f"malformed a2a reply: {exc!r} (body: {resp.text[:200]!r})"
        ) from exc


class KubectlAgentClient:
    """Reaches an in-cluster agent's a2a endpoint via a kubectl port-forward.

    Spawns `kubectl -n <namespace> port-forward svc/<service> :80`, parses the
    ephemeral local port it reports, POSTs one a2a task to it, and always
    terminates the port-forward afterward (success or failure).
    """

    def __init__(
        self,
        kubectl: str = "kubectl",
        port_forward_timeout_s: float = 10.0,
        http_client: httpx.Client | None = None,
    ):
        self._kubectl = kubectl
        self._timeout_s = port_forward_timeout_s
        # The hello POST waits on a full model roundtrip (routinely >5s
        # live), well past httpx's bare 5s default — give it generous
        # headroom rather than let a slow-but-healthy reply read as a
        # timeout/failure.
        self._http_client = http_client or httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0))

    def hello(self, namespace: str, service: str, text: str) -> str:
        return self.converse(namespace, service, [{"role": "user", "text": text}])

    def converse(self, namespace: str, service: str, messages: list[ChatMessage]) -> str:
        return self._send_task(namespace, service, messages)

    def _send_task(self, namespace: str, service: str, messages: list[ChatMessage]) -> str:
        # Popen itself can fail before any subprocess exists (e.g. `kubectl`
        # missing from PATH) — that's OSError, not ClientError, so it's
        # wrapped here rather than left to escape to callers that only
        # expect ClientError.
        try:
            proc = subprocess.Popen(
                [self._kubectl, "-n", namespace, "port-forward", f"svc/{service}", ":80"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except OSError as exc:
            raise ClientError(f"failed to start port-forward: {exc}") from exc
        try:
            port = _wait_for_forwarded_port(proc.stdout, self._timeout_s)
            return _post_task(self._http_client, port, messages)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    # Best-effort zombie reap: kill() already sent SIGKILL,
                    # so the process is dying (or dead) regardless — this
                    # second wait is just cleanup, never worth raising or
                    # blocking longer over.
                    pass


# --- Kubectl (rollout status) -----------------------------------------------------


@runtime_checkable
class KubectlClient(Protocol):
    def rollout_ok(self, namespace: str, deploy: str, timeout_s: int = 180) -> bool: ...

    def deploy_env_names(self, namespace: str, deploy: str) -> list[str]:
        """Container 0's env var names on the *live* Deployment, in spec order
        (empty list when it has none).

        Defense-in-depth for the stale-apply race (see DeployStep,
        `cortex_api/steps.py`): `rollout_ok` only proves *some* generation of
        the Deployment rolled out cleanly, not that it's the generation
        carrying WireStep's contract — a racing `kubectl apply` from a
        concurrent CI run can land an older, unwired tree last and still
        report a clean rollout. This reads the actual live spec so DeployStep
        can check it directly instead of trusting rollout_ok alone.
        """
        ...

    def deploy_service_account(self, namespace: str, deploy: str) -> str:
        """The live Deployment's pod template `serviceAccountName` (empty
        string when unset). Same stale-apply defense as `deploy_env_names`."""
        ...

    def deploy_image(self, namespace: str, deploy: str) -> str:
        """Container 0's image reference on the *live* Deployment (e.g.
        `.../gojira-app:abc123def456`).

        Used by the knowledge module's delivery check (`cortex_api.knowledge`)
        to tell whether a saved edit has actually rolled out yet: generated
        CI tags the image it builds with the first 12 chars of the commit
        sha it built from, so comparing this tag against a known commit's
        `sha[:12]` is a stateless way to answer "is that commit live" without
        any of cortex's own state to keep in sync.
        """
        ...

    def apply_secret(self, namespace: str, name: str, literals: dict[str, str]) -> None:
        """Idempotent upsert of a generic Secret `name` in `namespace` whose
        data is exactly `literals` (e.g. `{"SLACK_BOT_TOKEN": ..., "SLACK_APP_TOKEN": ...}`).

        Used by `cortex_api.slack_setup.wire_slack` to deliver the pasted
        Slack tokens to the agent's namespace: a `create secret --dry-run
        -o yaml | apply -f -` round trip rather than a plain `create` (which
        would fail on a second call for the same name) — safe to call again
        with a rotated token pair. The literal values reach kubectl only via
        this call's argv/stdin; they are never logged, never written to a
        file cortex controls, and never appear in a raised exception's
        message (the real implementation calls `CommandRunner.run` with
        `redact_args=True` on both stages, so a debug-level log never embeds
        the argv either, and separately sanitizes CommandRunner's own
        argv-embedding failure message on both stages for exactly this
        reason).
        """
        ...


class KubectlCLI:
    """kubectl-backed KubectlClient."""

    def __init__(self, runner: CommandRunner | None = None, kubectl: str = "kubectl"):
        self._runner = runner or CommandRunner()
        self._kubectl = kubectl

    def rollout_ok(self, namespace: str, deploy: str, timeout_s: int = 180) -> bool:
        try:
            self._runner.run(
                [
                    self._kubectl, "-n", namespace, "rollout", "status",
                    f"deploy/{deploy}", f"--timeout={timeout_s}s",
                ]
            )
            return True
        except ClientError:
            return False

    def deploy_env_names(self, namespace: str, deploy: str) -> list[str]:
        out = self._runner.run(
            [
                self._kubectl, "-n", namespace, "get", "deploy", deploy,
                "-o", "jsonpath={.spec.template.spec.containers[0].env[*].name}",
            ]
        )
        # kubectl's jsonpath template separates repeated matches with a
        # single space; CommandRunner.run() has already .strip()'d the
        # output, so a plain split() both tokenizes on that separator and
        # turns "" (no env vars) into [] rather than [""].
        return out.split()

    def deploy_service_account(self, namespace: str, deploy: str) -> str:
        return self._runner.run(
            [
                self._kubectl, "-n", namespace, "get", "deploy", deploy,
                "-o", "jsonpath={.spec.template.spec.serviceAccountName}",
            ]
        )

    def deploy_image(self, namespace: str, deploy: str) -> str:
        return self._runner.run(
            [
                self._kubectl, "-n", namespace, "get", "deploy", deploy,
                "-o", "jsonpath={.spec.template.spec.containers[0].image}",
            ]
        )

    def apply_secret(self, namespace: str, name: str, literals: dict[str, str]) -> None:
        literal_args = [f"--from-literal={key}={value}" for key, value in literals.items()]
        try:
            # redact_args=True: this stage's argv is the only place either
            # token literal ever appears (final-review Finding 3) — without
            # it, CommandRunner.run's own debug log would put the raw
            # `--from-literal=KEY=value` argv (secret values and all) into
            # logs one `--log-level debug` away. The stage-2 call below
            # passes it too, defense-in-depth, even though its own argv
            # never carries a literal.
            manifest_yaml = self._runner.run(
                [
                    self._kubectl, "-n", namespace, "create", "secret", "generic", name,
                    *literal_args, "--dry-run=client", "-o", "yaml",
                ],
                redact_args=True,
            )
        except ClientError as exc:
            # CommandRunner.run's own ClientError message embeds the full
            # argv it ran — which, for this specific command, is the only
            # place the literal secret values ever appear. Re-raise with a
            # message built from just the underlying stderr/stdout instead,
            # so a failure here can never leak `literals`' values the way
            # letting the original exception propagate would.
            raise ClientError(
                f"kubectl create secret {name!r} (dry-run) failed: {exc.stderr or exc.stdout}",
                returncode=exc.returncode, stdout=exc.stdout, stderr=exc.stderr,
            ) from exc

        try:
            self._runner.run(
                [self._kubectl, "apply", "-f", "-"], stdin=manifest_yaml, redact_args=True
            )
        except ClientError as exc:
            # Same reasoning as above, defense-in-depth: this stage's argv
            # carries no secret material (the manifest is piped via stdin),
            # but sanitizing both stages identically means the safety
            # property doesn't depend on which stage happens to fail.
            raise ClientError(
                f"kubectl apply of secret {name!r} failed: {exc.stderr or exc.stdout}",
                returncode=exc.returncode, stdout=exc.stdout, stderr=exc.stderr,
            ) from exc


# --- Slack (Web API: manifest app creation + config-token rotation) -----------


_SLACK_API_BASE = "https://slack.com/api"
_SLACK_TOKEN_KEY = "slack_app_config_token"
_SLACK_REFRESH_KEY = "slack_app_config_refresh_token"
# Slack's only two config-token failure modes that mean "rotate and retry" —
# anything else is a real problem the caller needs to see, not something a
# blind rotate-on-any-failure should paper over.
_SLACK_AUTH_ERRORS = frozenset({"token_expired", "invalid_auth"})


@runtime_checkable
class SlackClient(Protocol):
    def create_app(self, manifest: dict) -> str:
        """Create a Slack app from `manifest`; return its app_id."""
        ...


def _read_slack_tokens(tokens_path: Path) -> tuple[str, str]:
    """Parse `tokens_path`'s two `key: value` lines; returns (config_token,
    refresh_token).

    Deliberately a tolerant line-based parse rather than a full YAML load:
    the file mixes free-form comment lines with exactly two `key: value`
    lines (see module docstring context — this mirrors the rotation
    tooling's own approach), and the same line-oriented reasoning lets
    `_write_slack_tokens` below replace just those two lines on rewrite
    without disturbing anything else in the file.

    Both a missing file and one missing either required key raise
    `ClientError` naming only the path — never the file's contents, which
    may itself hold partial token material.
    """
    try:
        lines = tokens_path.read_text().splitlines()
    except OSError as exc:
        raise ClientError(f"slack tokens file not found: {tokens_path}") from exc

    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        values[key.strip()] = value.strip()

    try:
        return values[_SLACK_TOKEN_KEY], values[_SLACK_REFRESH_KEY]
    except KeyError as exc:
        raise ClientError(f"slack tokens file missing required keys: {tokens_path}") from exc


def _write_slack_tokens(tokens_path: Path, config_token: str, refresh_token: str) -> None:
    """Rewrite `tokens_path` with a fresh (config_token, refresh_token) pair,
    preserving every comment/blank line untouched and replacing only the two
    `key: value` lines.

    Written atomically (tmp file + `os.replace`) so a crash mid-write can
    never leave a half-written file: Slack's refresh token is single-use, so
    losing the just-returned new pair after already spending the old one
    would lock cortex out of rotating ever again.
    """
    try:
        lines = tokens_path.read_text().splitlines(keepends=True)
    except OSError as exc:
        raise ClientError(f"slack tokens file not found: {tokens_path}") from exc

    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{_SLACK_TOKEN_KEY}:"):
            new_lines.append(f"{_SLACK_TOKEN_KEY}: {config_token}\n")
        elif stripped.startswith(f"{_SLACK_REFRESH_KEY}:"):
            new_lines.append(f"{_SLACK_REFRESH_KEY}: {refresh_token}\n")
        else:
            new_lines.append(line)

    tmp_path = tokens_path.with_name(tokens_path.name + ".tmp")
    # Create the tmp file at 0600 BEFORE writing its content, rather than a
    # plain `write_text()` (which creates it at umask-default perms, e.g.
    # 0644): this file holds a live Slack refresh token from the moment its
    # first byte lands, and `copymode` below only fixes the mode up AFTER
    # the full content is already written — closing that transient window
    # rather than narrowing it.
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("".join(new_lines))
    if tokens_path.exists():
        # Preserve the original file's mode on the tmp file: the tmp file
        # gets umask-default permissions (e.g. 0644), and os.replace carries
        # the SOURCE's mode — so without this, a tokens file an operator
        # hardened to 0600 would silently widen to 0644 after rotation. This
        # file holds a live Slack refresh token.
        shutil.copymode(tokens_path, tmp_path)
    os.replace(tmp_path, tokens_path)


class HttpxSlackClient:
    """Slack Web API client for app-manifest creation, with automatic
    config-token rotation.

    Slack's app-config tokens expire in ~12h; `create_app` reads the current
    token from `tokens_path` on every call (never cached across calls) so a
    rotation from a previous call — or from outside this process entirely —
    is always picked up fresh. On `ok: false` with error `token_expired` or
    `invalid_auth`, it rotates the pair exactly once (persisting the new pair
    to `tokens_path` before using it) and retries the create exactly once;
    a second auth failure after that raises rather than looping forever.
    """

    def __init__(self, tokens_path: Path, http_client: httpx.Client | None = None):
        self._tokens_path = tokens_path
        self._client = http_client or httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))

    def create_app(self, manifest: dict) -> str:
        config_token, _ = _read_slack_tokens(self._tokens_path)
        return self._create_app(manifest, config_token, retried=False)

    def _create_app(self, manifest: dict, config_token: str, *, retried: bool) -> str:
        resp = self._client.post(
            f"{_SLACK_API_BASE}/apps.manifest.create",
            data={"manifest": json.dumps(manifest)},
            headers={"Authorization": f"Bearer {config_token}"},
        )
        body = resp.json()
        if body.get("ok"):
            return body["app_id"]

        error = body.get("error", "unknown_error")
        if error in _SLACK_AUTH_ERRORS and not retried:
            new_config_token = self._rotate()
            return self._create_app(manifest, new_config_token, retried=True)

        # Never include `manifest` or any token material here — just Slack's
        # own error code.
        raise SlackApiError(f"slack app creation failed: {error}")

    def _rotate(self) -> str:
        """Rotate the config-token pair via `tooling.tokens.rotate` and
        persist the new pair to `tokens_path` BEFORE returning it — see the
        class docstring and `_write_slack_tokens` for why persist-first is
        load-bearing (the refresh token is single-use).
        """
        _, refresh_token = _read_slack_tokens(self._tokens_path)
        resp = self._client.post(
            f"{_SLACK_API_BASE}/tooling.tokens.rotate",
            data={"refresh_token": refresh_token},
        )
        body = resp.json()
        if not body.get("ok"):
            error = body.get("error", "unknown_error")
            raise SlackApiError(f"slack token rotation failed: {error}")

        new_config_token = body["token"]
        new_refresh_token = body["refresh_token"]
        _write_slack_tokens(self._tokens_path, new_config_token, new_refresh_token)
        return new_config_token


# --- Wiring -----------------------------------------------------------------------


@dataclass
class Clients:
    github: GitHubClient
    gojira: GojiraClient
    cloud: CloudClient
    agent: AgentClient
    kubectl: KubectlClient
    # None when Slack connect isn't configured (no slack_tokens_path) — routes
    # must 503 with a clear "Slack is not configured" error in that case,
    # keeping local setups without Slack tokens working untouched.
    slack: SlackClient | None = None

    @classmethod
    def real(cls, config: Config) -> Clients:
        """Wire the real, subprocess/network-backed implementations."""
        runner = CommandRunner()
        return cls(
            github=GhCLI(runner=runner),
            gojira=HttpxGojiraClient(config.gojira_url, token=config.gojira_token),
            cloud=GcloudClient(config.gcp_project, runner=runner),
            agent=KubectlAgentClient(),
            kubectl=KubectlCLI(runner=runner),
            slack=HttpxSlackClient(config.slack_tokens_path)
            if config.slack_tokens_path
            else None,
        )
