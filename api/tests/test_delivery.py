"""One runtime, bundles as artifacts (tenishai/kobo#434): a change the room makes to
an agent publishes its bundle and restarts it, and only a Dockerfile change builds."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import httpx
import pytest
import yaml

from kobo_api import delivery
from kobo_api.bundle_publish import LAYER_MEDIA_TYPE, RegistryPublisher, bundle_tarball
from kobo_api.composer import _agent_yaml_data, _dockerfile

RUNTIME = "reg.example/agents/daedalus-runtime@sha256:" + "a" * 64
REGISTRY = "reg.example/agents"


def _git(workdir: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=workdir, capture_output=True, text=True, check=True).stdout.strip()


class GitHub:
    """commit_push and local_head over a real local repository."""

    def __init__(self):
        self.messages: list[str] = []

    def commit_push(self, workdir: Path, message: str) -> bool:
        _git(workdir, "add", "-A")
        if not _git(workdir, "status", "--porcelain"):
            return False
        _git(workdir, "commit", "-q", "-m", message)
        self.messages.append(message)
        return True

    def local_head(self, workdir: Path) -> str:
        return _git(workdir, "rev-parse", "HEAD")


class Publisher:
    def __init__(self):
        self.pushed: list[tuple[str, dict[str, str]]] = []
        self.tagged: list[tuple[str, str]] = []

    def push(self, directory: Path, reference: str) -> str:
        files = {p.relative_to(directory).as_posix(): p.read_text() for p in directory.rglob("*") if p.is_file()}
        self.pushed.append((reference, files))
        return "sha256:" + "b" * 64

    def tag(self, reference: str, tag: str) -> None:
        self.tagged.append((reference, tag))


class Kubectl:
    def __init__(self, exists: bool = True):
        self.exists, self.restarts = exists, []

    def deployment_exists(self, ns, deploy):
        return self.exists

    def restart_deploy(self, ns, deploy):
        self.restarts.append((ns, deploy))


def _repo(tmp_path: Path, *, dockerfile: str, gojira: dict | None = None) -> Path:
    work = tmp_path / "repo"
    (work / "bundle").mkdir(parents=True)
    (work / "bundle" / "agent.yaml").write_text("name: ledger\ntransports: []\n")
    (work / "bundle" / "persona.md").write_text("one\n")
    (work / "Dockerfile").write_text(dockerfile)
    (work / "gojira.yaml").write_text(yaml.safe_dump(gojira or {"app": "ledger", "secrets_from": ["ledger-cabal", "ledger-slack"]}))
    (work / "entrypoint.sh").write_text("#!/bin/sh\n")
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "t")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "compose")
    return work


@pytest.fixture
def configured():
    pub, kube = Publisher(), Kubectl()
    delivery.configure(delivery.Delivery(kubectl=kube, publisher=pub, registry=REGISTRY, runtime_image=RUNTIME,
                                         replica_bucket="mem-bucket"))
    yield pub, kube
    delivery.configure(None)


def test_unconfigured_a_change_is_a_commit_and_nothing_else(tmp_path):
    """Gate (integrity): without a registry the room behaves exactly as before."""
    delivery.configure(None)
    work = _repo(tmp_path, dockerfile=f"FROM {RUNTIME}\nCOPY bundle/ /bundle\n")
    (work / "bundle" / "persona.md").write_text("two\n")
    gh = GitHub()
    assert delivery.push(gh, work, "kobo: persona") is True
    assert gh.messages == ["kobo: persona"] and "COPY bundle/" in (work / "Dockerfile").read_text()


def test_a_bundle_change_publishes_and_restarts_without_touching_the_image(tmp_path, configured):
    """Gate (integrity): the new shape. A persona edit is a commit (the record), a
    publish of the bundle under the commit's sha and `current`, and a restart."""
    pub, kube = configured
    work = _repo(tmp_path, dockerfile=f"FROM {RUNTIME}\n")
    (work / "bundle" / "persona.md").write_text("two\n")
    assert delivery.push(GitHub(), work, "kobo: persona") is True
    (ref, files), = pub.pushed
    assert ref.startswith(f"{REGISTRY}/ledger-bundle:") and files["persona.md"] == "two\n"
    assert pub.tagged == [(ref, "current")]
    assert kube.restarts == [("ledger", "ledger")]
    assert (work / "Dockerfile").read_text() == f"FROM {RUNTIME}\n"


def test_a_commit_outside_the_bundle_neither_publishes_nor_restarts_and_a_live_lane_skips_the_restart(tmp_path, configured):
    pub, kube = configured
    work = _repo(tmp_path, dockerfile=f"FROM {RUNTIME}\n")
    (work / "notes.txt").write_text("x")
    delivery.push(GitHub(), work, "kobo: notes")
    assert pub.pushed == [] and kube.restarts == []
    (work / "bundle" / "persona.md").write_text("relayed\n")
    delivery.push(GitHub(), work, "kobo: archive [skip ci]", restart=False)
    assert len(pub.pushed) == 1 and kube.restarts == []


def test_an_old_shape_agent_on_the_rooms_runtime_converts_in_the_one_commit_that_builds(tmp_path, configured):
    """Gate (integrity): the first change to an agent of the old shape moves it: the
    Dockerfile is its FROM line alone, litestream and the entrypoint leave, gojira.yaml
    gains the runtime's env and the optional Secret references. That commit builds, so
    the roll is the restart and the room does not restart it on top."""
    pub, kube = configured
    work = _repo(tmp_path, dockerfile=f"FROM {RUNTIME}\nCOPY bundle/ /bundle\nENTRYPOINT [\"/entrypoint.sh\"]\n")
    (work / "bundle" / "persona.md").write_text("two\n")
    gh = GitHub()
    delivery.push(gh, work, "kobo: persona")
    assert gh.messages[-1].endswith(delivery.CONVERTED_NOTE)
    assert "COPY" not in (work / "Dockerfile").read_text() and f"FROM {RUNTIME}" in (work / "Dockerfile").read_text()
    assert not (work / "entrypoint.sh").exists()
    data = yaml.safe_load((work / "gojira.yaml").read_text())
    assert data["env"] == {"BUNDLE_ARTIFACT": f"{REGISTRY}/ledger-bundle:current", "AGENT_NAME": "ledger",
                           "MEMORY_REPLICA_BUCKET": "mem-bucket"}
    assert data["secrets_from"] == ["ledger-cabal"]
    assert data["secrets_from_optional"] == ["ledger-slack", "ledger-env"]
    assert len(pub.pushed) == 1 and kube.restarts == []


def test_an_old_shape_agent_on_an_older_runtime_is_left_alone(tmp_path, configured):
    assert configured
    work = _repo(tmp_path, dockerfile="FROM reg.example/agents/daedalus-runtime:old\nCOPY bundle/ /bundle\n")
    (work / "bundle" / "persona.md").write_text("two\n")
    delivery.push(GitHub(), work, "kobo: persona")
    assert "COPY bundle/" in (work / "Dockerfile").read_text()
    assert "env" not in yaml.safe_load((work / "gojira.yaml").read_text())


def test_a_secret_change_restarts_even_with_nothing_to_commit(tmp_path, configured):
    _, kube = configured
    work = _repo(tmp_path, dockerfile=f"FROM {RUNTIME}\n")
    assert delivery.push(GitHub(), work, "kobo: slack", force_restart=True) is False
    assert kube.restarts == [("ledger", "ledger")]


def test_an_agent_born_for_artifacts_has_a_one_line_image_and_a_dormant_slack_transport():
    """Gate (integrity): what compose writes for the new shape."""
    from kobo_api.composer import BundleSpec

    spec = BundleSpec(name="ledger", persona_body="p", model_id="m", skills=[], runtime_image=RUNTIME,
                      vertex_project="p", vertex_location="l", memory_replica_bucket="mem-bucket",
                      bundle_artifact=f"{REGISTRY}/ledger-bundle:current")
    lines = [line for line in _dockerfile(spec).splitlines() if not line.startswith("#")]
    assert lines == [f"FROM {RUNTIME}"]
    kinds = [dict(t) for t in _agent_yaml_data(spec)["transports"]]
    assert kinds[-1] == {"kind": "slack", "optional": True}


def test_the_registry_publisher_pushes_one_tarball_layer_the_runtime_reads(tmp_path):
    """Wire shape: blobs by digest, a manifest with the bundle layer's media type, and
    `current` as a second manifest put. Bearer from the pod's identity."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "agent.yaml").write_text("name: ledger\n")
    seen: list[tuple[str, str, bytes, str]] = []
    manifests: dict[str, bytes] = {}

    def handle(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path, req.content, req.headers.get("authorization", "")))
        if req.method == "HEAD":
            return httpx.Response(404)
        if req.method == "POST":
            return httpx.Response(202, headers={"location": "/v2/agents/ledger-bundle/blobs/uploads/u1"})
        if req.method == "PUT" and "/manifests/" in req.url.path:
            manifests[req.url.path.rsplit("/", 1)[1]] = req.content
            return httpx.Response(201, headers={"docker-content-digest": "sha256:" + hashlib.sha256(req.content).hexdigest()})
        if req.method == "GET" and "/manifests/" in req.url.path:
            return httpx.Response(200, content=manifests[req.url.path.rsplit("/", 1)[1]])
        return httpx.Response(201)

    pub = RegistryPublisher(token_provider=lambda: "tok", client=httpx.Client(transport=httpx.MockTransport(handle)))
    digest = pub.push(bundle, "reg.example/agents/ledger-bundle:abc123")
    pub.tag("reg.example/agents/ledger-bundle:abc123", "current")
    manifest = json.loads(manifests["abc123"])
    (layer,) = manifest["layers"]
    assert layer["mediaType"] == LAYER_MEDIA_TYPE
    assert layer["digest"] == "sha256:" + hashlib.sha256(bundle_tarball(bundle)).hexdigest()
    assert manifests["current"] == manifests["abc123"]
    assert digest.startswith("sha256:")
    assert all(auth == "Bearer tok" for *_, auth in seen)
    assert bundle_tarball(bundle) == bundle_tarball(bundle)  # deterministic: same bundle, same digest
