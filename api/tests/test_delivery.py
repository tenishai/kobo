"""One runtime, bundles as artifacts (tenishai/kobo#434): a change the room makes to
an agent publishes its bundle and restarts it, and only a Dockerfile change builds."""
from __future__ import annotations

import gzip
import hashlib
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

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
    assert data["bundle_image"] == f"{REGISTRY}/ledger-bundle:current"
    assert data["env"] == {"AGENT_NAME": "ledger", "MEMORY_REPLICA_BUCKET": "mem-bucket"}
    assert data["secrets_from"] == ["ledger-cabal"]
    assert data["secrets_from_optional"] == ["ledger-slack", "ledger-env"]
    assert len(pub.pushed) == 1 and kube.restarts == []


def test_an_old_shape_agent_on_an_older_runtime_is_left_alone(tmp_path, configured):
    assert configured
    work = _repo(tmp_path, dockerfile="FROM reg.example/agents/daedalus-runtime:old\nCOPY bundle/ /bundle\n")
    (work / "bundle" / "persona.md").write_text("two\n")
    delivery.push(GitHub(), work, "kobo: persona")
    assert "COPY bundle/" in (work / "Dockerfile").read_text()
    assert "bundle_image" not in yaml.safe_load((work / "gojira.yaml").read_text())


def test_a_change_whose_commit_landed_and_delivery_did_not_is_delivered_when_asked_again(tmp_path, configured):
    """Gate (integrity): asked again, `push` finds nothing to commit and stops, so
    the agent would never get a change whose publish or restart failed the first
    time. `redeliver` publishes the bundle as it stands and restarts the agent."""
    pub, kube = configured
    work = _repo(tmp_path, dockerfile=f"FROM {RUNTIME}\n")
    gh = GitHub()
    assert delivery.push(gh, work, "kobo: again") is False
    assert pub.pushed == [] and kube.restarts == []
    assert delivery.redeliver(gh, work) is True
    (ref, files), = pub.pushed
    assert ref.startswith(f"{REGISTRY}/ledger-bundle:") and files["persona.md"] == "one\n"
    assert kube.restarts == [("ledger", "ledger")]
    delivery.configure(None)
    assert delivery.redeliver(gh, work) is False


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
    # The seed gojira.yaml the deploy platform reads before its FIRST render, so the first
    # deployment already mounts the bundle (dev, 2026-11-03: without it the first pod had no /bundle).
    import tempfile

    from kobo_api.composer import compose
    d = Path(tempfile.mkdtemp()) / "repo"
    compose(spec, d)
    seed = yaml.safe_load((d / "gojira.yaml").read_text())
    assert seed == {"bundle_image": f"{REGISTRY}/ledger-bundle:current",
                    "env": {"AGENT_NAME": "ledger", "MEMORY_REPLICA_BUCKET": "mem-bucket"},
                    "secrets_from_optional": ["ledger-slack", "ledger-env"]}
    assert not (d / "litestream.yml").exists() and not (d / "entrypoint.sh").exists()


def test_the_registry_publisher_pushes_a_plain_oci_image_an_image_volume_can_mount(tmp_path):
    """Wire shape: a standard tar+gzip layer under an image config whose diff id is the
    uncompressed tarball's digest (proven on dev: a custom media type mounts empty), the
    bundle marked by an annotation, and `current` as a second manifest put."""
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
    assert layer["mediaType"] == LAYER_MEDIA_TYPE == "application/vnd.oci.image.layer.v1.tar+gzip"
    assert layer["digest"] == "sha256:" + hashlib.sha256(bundle_tarball(bundle)).hexdigest()
    assert manifest["config"]["mediaType"] == "application/vnd.oci.image.config.v1+json"
    config = json.loads(next(c for m, p, c, _ in seen if m == "PUT" and "/uploads/" in p and c.startswith(b"{")))
    assert config["rootfs"]["diff_ids"] == ["sha256:" + hashlib.sha256(gzip.decompress(bundle_tarball(bundle))).hexdigest()]
    assert manifest["annotations"]["dev.tenish.kobo.bundle"] == "application/vnd.tenish.agent-bundle.v1"
    assert manifests["current"] == manifests["abc123"]
    assert digest.startswith("sha256:")
    assert all(auth == "Bearer tok" for *_, auth in seen)
    built = bundle_tarball(bundle)
    with patch("time.time", return_value=1_800_000_000.0):
        assert bundle_tarball(bundle) == built  # deterministic: same bundle, same digest, whatever second it is


class SecretKubectl(Kubectl):
    """A Secret whose keys appear only after a number of reads, the way a platform-delivered
    value reaches the cluster at its next re-read."""

    def __init__(self, keys_after: int, keys: tuple[str, ...] = ("SLACK_BOT_TOKEN",)):
        super().__init__()
        self.keys_after, self.keys, self.reads = keys_after, keys, 0

    def secret_literals(self, ns, name):
        self.reads += 1
        if self.reads <= self.keys_after:
            return {"SLACK_APP_ID": "A1"}
        return {"SLACK_APP_ID": "A1", **{k: "x" for k in self.keys}}


def test_the_restart_after_a_delivered_secret_waits_for_the_key_to_land():
    kubectl = SecretKubectl(keys_after=3)
    slept: list[float] = []
    assert delivery.wait_for_secret(kubectl, "ledger", "ledger-slack", ["SLACK_BOT_TOKEN"], sleep=slept.append, clock=lambda: 0.0)
    assert kubectl.reads == 4 and len(slept) == 3
    delivery.restart_once_secret_carries(kubectl, "ledger", "ledger-slack", ["SLACK_BOT_TOKEN"], background=False)
    assert kubectl.restarts == [("ledger", "ledger")]


def test_a_restart_for_a_changed_setting_waits_for_the_new_value():
    """A key that is already there says nothing about a value that changed, so the wait
    for a plain setting reads the value, and a setting taken away waits for the key to go."""
    kubectl = SecretKubectl(keys_after=0)
    kubectl.secret_literals = lambda ns, name: {"SLACK_WAKE_MODE": "thread", "SLACK_WAKE_EMOJI": "ticket"}
    carries = lambda settings: delivery.secret_carries(kubectl, "ledger", "ledger-slack", [], settings)
    assert carries({"SLACK_WAKE_MODE": "thread"}) and not carries({"SLACK_WAKE_MODE": "channel"})
    assert carries({"SLACK_COMMAND_PUBLIC": None}) and not carries({"SLACK_WAKE_EMOJI": None})


def test_a_step_before_the_restart_runs_first_and_cannot_lose_it():
    kubectl = SecretKubectl(keys_after=0)
    order: list[str] = []

    def before() -> None:
        order.append(f"before:{len(kubectl.restarts)}")
        raise RuntimeError("the push failed")

    delivery.restart_once_secret_carries(kubectl, "ledger", "ledger-slack", ["SLACK_BOT_TOKEN"], background=False, before=before)
    assert order == ["before:0"] and kubectl.restarts == [("ledger", "ledger")]


def test_a_step_that_never_returns_does_not_hold_the_restart(monkeypatch):
    import threading

    kubectl = SecretKubectl(keys_after=0)
    stuck = threading.Event()
    monkeypatch.setattr(delivery, "STEP_WAIT", 0.05)
    delivery.restart_once_secret_carries(
        kubectl, "ledger", "ledger-slack", ["SLACK_BOT_TOKEN"], background=False, before=stuck.wait
    )
    stuck.set()
    assert kubectl.restarts == [("ledger", "ledger")]


def test_a_key_that_never_lands_still_restarts_when_the_wait_runs_out(monkeypatch):
    kubectl = SecretKubectl(keys_after=10**6)
    ticks = iter([0.0, 1.0, 200.0, 201.0])
    monkeypatch.setattr(delivery, "SECRET_WAIT", 150.0)
    assert not delivery.wait_for_secret(kubectl, "ledger", "ledger-slack", ["SLACK_BOT_TOKEN"], sleep=lambda s: None, clock=lambda: next(ticks))
    monkeypatch.setattr(delivery, "SECRET_WAIT", 0.0)  # the restart's own wait runs on the real clock
    delivery.restart_once_secret_carries(kubectl, "ledger", "ledger-slack", ["SLACK_BOT_TOKEN"], background=False)
    assert kubectl.restarts == [("ledger", "ledger")]
