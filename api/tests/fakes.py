"""In-memory test fakes for the platform-client Protocols (cortex_api.clients).

Mirror the P0-observed behaviors of the real GitHub/gojira/gcloud/kubectl/a2a
backends closely enough that step logic exercised against these fakes behaves
like it will against the real thing later (Tasks 6-8) — including the failure
paths those tests need: a gojira app that ends up failed instead of active, a
CI run sequence that ends in failure, a 409 conflict on repo connect. Every
fake records what it was called with (`.calls`, plus a few typed lists) so
tests can assert on side effects without touching a real subprocess or network.
"""

from __future__ import annotations

from pathlib import Path

from cortex_api.clients import GojiraConflict


class FakeGitHub:
    """Records repo creation/clones/pushes; latest_run replays a configurable
    sequence of CI-run dicts, holding at the last entry once exhausted (so a
    sequence can be made to end in success or in failure).
    """

    def __init__(
        self,
        *,
        existing_repos: set[str] | None = None,
        run_sequence: list[dict] | None = None,
    ):
        self.repos: set[str] = set(existing_repos) if existing_repos else set()
        self.created_from_dir: list[tuple[str, Path]] = []
        self.cloned: list[tuple[str, Path]] = []
        self.pushes: list[tuple[Path, str]] = []
        self.calls: list[str] = []
        self._run_sequence = (
            list(run_sequence)
            if run_sequence
            else [{"id": 1, "status": "completed", "conclusion": "success"}]
        )
        self._run_index = 0

    def repo_exists(self, repo: str) -> bool:
        self.calls.append(f"repo_exists:{repo}")
        return repo in self.repos

    def create_repo_from_dir(self, repo: str, src: Path) -> None:
        self.calls.append(f"create_repo_from_dir:{repo}")
        self.created_from_dir.append((repo, src))
        self.repos.add(repo)

    def clone(self, repo: str, dest: Path) -> None:
        self.calls.append(f"clone:{repo}")
        self.cloned.append((repo, dest))

    def commit_push(self, workdir: Path, message: str) -> bool:
        self.calls.append(f"commit_push:{message}")
        self.pushes.append((workdir, message))
        return True

    def latest_run(self, repo: str) -> dict:
        self.calls.append(f"latest_run:{repo}")
        run = self._run_sequence[min(self._run_index, len(self._run_sequence) - 1)]
        if self._run_index < len(self._run_sequence) - 1:
            self._run_index += 1
        return dict(run)


class FakeGojira:
    """Walks a created app from "pending" to `end_status` on the
    `activate_after`-th status poll; records reprovision calls; can simulate a
    409 conflict on create for repos named in `conflict_repos`.
    """

    def __init__(
        self,
        *,
        activate_after: int = 1,
        end_status: str = "active",
        conflict_repos: set[str] | None = None,
    ):
        self._activate_after = activate_after
        self._end_status = end_status
        self._conflict_repos = set(conflict_repos) if conflict_repos else set()
        self._apps: dict[int, dict] = {}
        self._next_id = 1
        self.reprovision_calls: list[int] = []
        self.calls: list[str] = []

    def create_app(self, *, name, repo_full_name, cluster, namespace) -> int:
        self.calls.append(f"create_app:{repo_full_name}")
        if repo_full_name in self._conflict_repos:
            raise GojiraConflict(f"app already connected for {repo_full_name!r}")
        app_id = self._next_id
        self._next_id += 1
        self._apps[app_id] = {
            "id": app_id,
            "name": name,
            "repo_full_name": repo_full_name,
            "cluster": cluster,
            "namespace": namespace,
            "status": "pending",
            "polls": 0,
            "setup_pr_url": None,
            "auto_merge_setup_pr": True,
            "steps": [],
        }
        return app_id

    def app_status(self, app_id: int) -> dict:
        self.calls.append(f"app_status:{app_id}")
        app = self._apps[app_id]
        app["polls"] += 1
        if app["status"] == "pending" and app["polls"] >= self._activate_after:
            app["status"] = self._end_status
        return dict(app)

    def reprovision(self, app_id: int) -> None:
        self.calls.append(f"reprovision:{app_id}")
        self.reprovision_calls.append(app_id)
        app = self._apps[app_id]
        app["status"] = "pending"
        app["polls"] = 0


class FakeCloud:
    """Records ensure_gsa/bind_workload_identity/add_to_group calls; every
    method is idempotent against its own recorded state, mirroring the real
    gcloud backend's describe-then-create / add-iam-policy-binding semantics.
    """

    def __init__(self, *, project: str = "example-project"):
        self._project = project
        self.gsas: set[str] = set()
        self.bindings: set[tuple[str, str, str]] = set()
        self.group_members: set[tuple[str, str]] = set()
        self.calls: list[str] = []

    def ensure_gsa(self, name: str) -> str:
        email = f"{name}@{self._project}.iam.gserviceaccount.com"
        self.calls.append(f"ensure_gsa:{name}")
        self.gsas.add(email)
        return email

    def bind_workload_identity(self, gsa_email: str, namespace: str, ksa: str) -> None:
        self.calls.append(f"bind_workload_identity:{gsa_email}:{namespace}/{ksa}")
        self.bindings.add((gsa_email, namespace, ksa))

    def add_to_group(self, gsa_email: str, group: str) -> None:
        self.calls.append(f"add_to_group:{gsa_email}:{group}")
        self.group_members.add((gsa_email, group))


class FakeAgent:
    """hello() returns a canned reply and records every call it received."""

    def __init__(self, *, reply: str = "hello yourself"):
        self._reply = reply
        self.calls: list[tuple[str, str, str]] = []

    def hello(self, namespace: str, service: str, text: str) -> str:
        self.calls.append((namespace, service, text))
        return self._reply


class FakeKubectl:
    """rollout_ok() returns a configurable, constant result and records calls."""

    def __init__(self, *, rollout_result: bool = True):
        self._rollout_result = rollout_result
        self.calls: list[tuple[str, str]] = []

    def rollout_ok(self, namespace: str, deploy: str, timeout_s: int = 180) -> bool:
        self.calls.append((namespace, deploy))
        return self._rollout_result
