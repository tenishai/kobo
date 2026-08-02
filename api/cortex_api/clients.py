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
from typing import Protocol, runtime_checkable

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

    def latest_run(self, repo: str) -> dict:
        """The most recent workflow run: {"id": int, "status": str, "conclusion": str | None}."""
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

    def latest_run(self, repo: str) -> dict:
        out = self._runner.run(
            [
                self._gh, "run", "list", "--repo", repo, "--limit", "1",
                "--json", "databaseId,status,conclusion",
            ]
        )
        runs = json.loads(out) if out else []
        if not runs:
            return {"id": None, "status": "not_found", "conclusion": None}
        run = runs[0]
        return {
            "id": run["databaseId"],
            "status": run["status"],
            "conclusion": run.get("conclusion"),
        }


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
        self._client = client or httpx.Client()

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


class GcloudClient:
    """gcloud-backed CloudClient, scoped to one GCP project."""

    def __init__(self, project: str, runner: CommandRunner | None = None, gcloud: str = "gcloud"):
        self._project = project
        self._runner = runner or CommandRunner()
        self._gcloud = gcloud

    def ensure_gsa(self, name: str) -> str:
        email = f"{name}@{self._project}.iam.gserviceaccount.com"
        try:
            self._runner.run(
                [
                    self._gcloud, "iam", "service-accounts", "describe", email,
                    "--project", self._project,
                ]
            )
            return email
        except ClientError:
            pass  # not found (or a transient describe failure) -> create
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


# --- Agent (a2a "hello" over a kubectl port-forward) -------------------------------


@runtime_checkable
class AgentClient(Protocol):
    def hello(self, namespace: str, service: str, text: str) -> str:
        """Say `text` to the agent's a2a endpoint; return its reply text."""
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


def _build_hello_envelope(text: str) -> dict:
    return {
        "kind": "task",
        "id": f"hello-{uuid.uuid4().hex}",
        "history": [
            {"kind": "message", "role": "user", "parts": [{"kind": "text", "text": text}]}
        ],
    }


def _post_hello(client: httpx.Client, port: int, text: str) -> str:
    """POST the bare a2a task envelope to the forwarded port; return the reply text.

    Factored out from `hello()` so the request/response shape is unit-testable
    against an httpx.MockTransport-backed client, with no real network involved.
    """
    resp = client.post(f"http://127.0.0.1:{port}/a2a/tasks", json=_build_hello_envelope(text))
    if resp.status_code != 200:
        raise ClientError(f"a2a hello failed ({resp.status_code}): {resp.text}")
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
        self._http_client = http_client or httpx.Client()

    def hello(self, namespace: str, service: str, text: str) -> str:
        proc = subprocess.Popen(
            [self._kubectl, "-n", namespace, "port-forward", f"svc/{service}", ":80"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            port = _wait_for_forwarded_port(proc.stdout, self._timeout_s)
            return _post_hello(self._http_client, port, text)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


# --- Kubectl (rollout status) -----------------------------------------------------


@runtime_checkable
class KubectlClient(Protocol):
    def rollout_ok(self, namespace: str, deploy: str, timeout_s: int = 180) -> bool: ...


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
