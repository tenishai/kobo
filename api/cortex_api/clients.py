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
import queue
import re
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


class CommandRunner:
    """Runs a subprocess command; returns stdout, raises ClientError on rc != 0."""

    def run(self, args: list[str], stdin: str | None = None) -> str:
        log.debug("exec: %s", " ".join(args))
        proc = subprocess.run(args, input=stdin, capture_output=True, text=True, check=False)
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

    def run_bytes(self, args: list[str], stdin: bytes | None = None) -> bytes:
        """Like `run()`, but for commands whose stdout is binary (e.g. `git
        archive`, cortex_api.composer's skill-pinning materialization) —
        `run()`'s `text=True` decode-and-strip would corrupt a tar archive's
        bytes, so this skips both and hands back stdout untouched. Still
        raises ClientError on rc != 0, decoding stderr/stdout only for the
        error message.
        """
        log.debug("exec (bytes): %s", " ".join(args))
        proc = subprocess.run(args, input=stdin, capture_output=True, text=False, check=False)
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
    """
    try:
        resp = client.post(f"http://127.0.0.1:{port}/a2a/tasks", json=_build_task_envelope(messages))
    except httpx.HTTPError as exc:
        raise ClientError(f"a2a request failed: {exc!r}") from exc
    if resp.status_code != 200:
        raise ClientError(f"a2a request failed ({resp.status_code}): {resp.text}")
    return resp.json()["status"]["message"]["parts"][0]["text"]


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
        proc = subprocess.Popen(
            [self._kubectl, "-n", namespace, "port-forward", f"svc/{service}", ":80"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
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


# --- Wiring -----------------------------------------------------------------------


@dataclass
class Clients:
    github: GitHubClient
    gojira: GojiraClient
    cloud: CloudClient
    agent: AgentClient
    kubectl: KubectlClient

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
        )
