"""One runtime, bundles as artifacts (tenishai/kobo#434): a change the room makes to
an agent publishes its bundle and restarts it, and only a Dockerfile change builds."""
from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import yaml

from kobo_api import delivery
from kobo_api.agent_env import CI_WORKFLOW, contract_fingerprint
from kobo_api.bundle_publish import LAYER_MEDIA_TYPE, RegistryPublisher, bundle_tarball
from kobo_api.clients import ClientError
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

    def rev_parse_prefix(self, workdir: Path, prefix: str) -> str | None:
        try:
            return _git(workdir, "rev-parse", f"{prefix}^{{commit}}")
        except subprocess.CalledProcessError:
            return None

    def is_ancestor(self, workdir: Path, ancestor: str, descendant: str) -> bool:
        return subprocess.run(["git", "merge-base", "--is-ancestor", ancestor, descendant], cwd=workdir, check=False).returncode == 0

    def commit_shas_for_paths(self, workdir: Path, paths: list[str]) -> dict[str, str]:
        return {path: _git(workdir, "log", "-1", "--format=%H", "--", path) for path in paths}


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
        self.exists, self.restarts, self.named = exists, [], []
        # The image the agent runs, tagged with the commit it was built from (`_built`).
        self.image: str | None = None

    def deployment_exists(self, ns, deploy):
        return self.exists

    def deploy_image(self, ns, deploy):
        if self.image is None:
            raise ClientError(f"deployments.apps {deploy!r} not found")
        return self.image

    def restart_deploy(self, ns, deploy, *, annotations=None):
        self.restarts.append((ns, deploy))
        self.named.append((annotations or {}).get(delivery.BUNDLE_COMMIT_ANNOTATION))


def _repo(tmp_path: Path, *, dockerfile: str, gojira: dict | None = None) -> Path:
    work = tmp_path / "repo"
    (work / "bundle").mkdir(parents=True)
    (work / "bundle" / "agent.yaml").write_text("name: ledger\ntransports: []\n")
    (work / "bundle" / "persona.md").write_text("one\n")
    (work / "Dockerfile").write_text(dockerfile)
    (work / "gojira.yaml").write_text(yaml.safe_dump(gojira or {"app": "ledger", "secrets_from": ["ledger-cabal", "ledger-slack"]}))
    (work / "entrypoint.sh").write_text("#!/bin/sh\n")
    # The deploy files the platform rendered from that gojira.yaml.
    (work / CI_WORKFLOW).parent.mkdir(parents=True)
    (work / CI_WORKFLOW).write_text(f"env:\n  GOJIRA_CONTRACT: {contract_fingerprint((work / 'gojira.yaml').read_text())}\n")
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "t")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "compose")
    return work


def _built(kube: Kubectl, work: Path) -> None:
    """The agent runs the image its generated build made from `work`'s HEAD."""
    kube.image = f"{REGISTRY}/ledger:{_git(work, 'rev-parse', 'HEAD')[:12]}@sha256:" + "d" * 64


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
    gains the runtime's env and the optional Secret references. The platform builds
    that commit from the deploy files it renders again, so the room asks it to (the
    commit itself skips CI, whose contract check would fail it until then), and the roll
    is the restart: the room does not restart it on top. Without the ask, ledger kept
    its old image for two weeks while every later build failed the check."""
    pub, kube = configured
    asked: list[str] = []
    delivery.configure(delivery.Delivery(kubectl=kube, publisher=pub, registry=REGISTRY, runtime_image=RUNTIME,
                                         replica_bucket="mem-bucket", reprovision=asked.append))
    work = _repo(tmp_path, dockerfile=f"FROM {RUNTIME}\nCOPY bundle/ /bundle\nENTRYPOINT [\"/entrypoint.sh\"]\n")
    (work / "bundle" / "persona.md").write_text("two\n")
    gh = GitHub()
    delivery.push(gh, work, "kobo: persona")
    assert gh.messages[-1].endswith(f"{delivery.CONVERTED_NOTE} [skip ci]")
    assert asked == ["ledger"]
    assert "COPY" not in (work / "Dockerfile").read_text() and f"FROM {RUNTIME}" in (work / "Dockerfile").read_text()
    assert not (work / "entrypoint.sh").exists()
    data = yaml.safe_load((work / "gojira.yaml").read_text())
    assert data["bundle_image"] == f"{REGISTRY}/ledger-bundle:current"
    assert data["env"] == {"AGENT_NAME": "ledger", "MEMORY_REPLICA_BUCKET": "mem-bucket"}
    assert data["secrets_from"] == ["ledger-cabal"]
    assert data["secrets_from_optional"] == ["ledger-slack", "ledger-env"]
    assert len(pub.pushed) == 1 and kube.restarts == []


def test_a_conversion_whose_ask_failed_is_asked_again_by_the_next_push(tmp_path, configured):
    """Gate (integrity): the conversion's commit lands with CI skipped, so if the platform
    cannot be asked, nothing else would ever deploy it. The push says so (a timeout as
    much as a refusal), and the next push asks again, even with nothing to commit; so
    does a save to an agent left that way before this (ledger, 2026-11-04 to 11-20). A
    restart still delivers what it can on the way, except to the conversion's own image,
    which still carries its old bundle."""
    pub, kube = configured
    asked: list[str] = []

    def reprovision(name: str) -> None:
        asked.append(name)
        if len(asked) == 1:
            raise httpx.ReadTimeout("timed out")

    delivery.configure(delivery.Delivery(kubectl=kube, publisher=pub, registry=REGISTRY, runtime_image=RUNTIME,
                                         replica_bucket="mem-bucket", reprovision=reprovision))
    work = _repo(tmp_path, dockerfile=f"FROM {RUNTIME}\nCOPY bundle/ /bundle\n")
    (work / "bundle" / "persona.md").write_text("two\n")
    gh = GitHub()
    with pytest.raises(ClientError, match=delivery.PLATFORM_NOT_ASKED):
        delivery.push(gh, work, "kobo: persona")
    assert kube.restarts == []
    assert gh.messages == [f"kobo: persona{delivery.CONVERTED_NOTE} [skip ci]"]

    assert delivery.push(gh, work, "kobo: persona") is False
    (work / "bundle" / "persona.md").write_text("three\n")
    assert delivery.push(gh, work, "kobo: persona") is True
    assert asked == ["ledger", "ledger", "ledger"]
    assert gh.messages[-1] == "kobo: persona [skip ci]"
    assert kube.restarts == [("ledger", "ledger")]


def test_a_caller_that_asks_the_platform_itself_is_not_asked_for_twice(tmp_path, configured):
    """A model change or Slack wiring edits gojira.yaml and asks the platform right
    after: its commit skips the build that would fail, the room asks nothing on top,
    and the platform's deploy is the one restart."""
    pub, kube = configured
    asked: list[str] = []
    delivery.configure(delivery.Delivery(kubectl=kube, publisher=pub, registry=REGISTRY, runtime_image=RUNTIME,
                                         reprovision=asked.append))
    work = _repo(tmp_path, dockerfile=f"FROM {RUNTIME}\n")
    (work / "gojira.yaml").write_text(yaml.safe_dump({"app": "ledger", "secrets_from": ["ledger-models"]}))
    (work / "bundle" / "persona.md").write_text("two\n")
    gh = GitHub()
    assert delivery.push(gh, work, "kobo: model", asks_platform=True) is True
    assert gh.messages == ["kobo: model [skip ci]"]
    assert asked == [] and kube.restarts == [] and len(pub.pushed) == 1


def test_without_the_platform_to_ask_a_conversion_builds_as_it_did(tmp_path, configured):
    pub, kube = configured
    work = _repo(tmp_path, dockerfile=f"FROM {RUNTIME}\nCOPY bundle/ /bundle\n")
    (work / "bundle" / "persona.md").write_text("two\n")
    gh = GitHub()
    assert delivery.push(gh, work, "kobo: persona") is True
    assert gh.messages == [f"kobo: persona{delivery.CONVERTED_NOTE}"]
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


class _CloningGitHub(GitHub):
    """Clones by copying a local repository, and keeps what each commit's
    Dockerfile said, since the clone itself is thrown away."""

    def __init__(self, source: Path):
        super().__init__()
        self.source = source
        self.dockerfiles: list[str] = []

    def clone(self, repo: str, dest: Path) -> None:
        shutil.copytree(self.source, dest, dirs_exist_ok=True)

    def commit_push(self, workdir: Path, message: str) -> bool:
        self.dockerfiles.append((workdir / "Dockerfile").read_text())
        return super().commit_push(workdir, message)


def test_a_save_never_moves_the_runtime_when_bundles_are_artifacts(tmp_path, configured):
    """Gate (integrity, 2026-11-20): with bundles delivered as artifacts, a teach
    note's archive, an upload or a check leaves the agent's Dockerfile on the runtime
    it has; only the runtime route moves it. A re-pin riding a teach note moved
    johnny-bravo onto a new runtime the moment the room's pin changed, rebuilding and
    restarting it twice, and an archive meant to restart nothing did."""
    from kobo_api.intake import upload_intake_file
    from kobo_api.tooling import upload_tooling_file

    _pub, kube = configured
    old = "reg.example/agents/daedalus-runtime@sha256:" + "b" * 64
    source = _repo(tmp_path, dockerfile=f"FROM {old}\n")
    _built(kube, source)
    gh = _CloningGitHub(source)

    upload_intake_file(gh, "acme/agent-ledger", "note.md", "remember this", runtime_image=RUNTIME, archive_only=True)
    upload_tooling_file(gh, "acme/agent-ledger", "check.py", "def post_draft(draft):\n    return draft\n",
                        runtime_image=RUNTIME)

    assert gh.dockerfiles == [f"FROM {old}\n", f"FROM {old}\n"]
    assert gh.messages[0] == "kobo: archive intake/note.md [skip ci]"
    assert gh.messages[1] == "kobo: add tools/check.py [skip ci]"
    # The archive restarts nothing; the check is delivered by one restart.
    assert kube.restarts == [("ledger", "ledger")]


def test_a_save_that_cannot_change_the_image_skips_the_build_and_names_its_commit(tmp_path, configured):
    """Gate (integrity, 2026-11-20): a commit that cannot change the image skips the
    generated build, whose deploy of the same image under the commit's tag rolled the
    agent a second time a minute after the room's own restart (johnny-bravo). The one
    restart names the commit it delivers, which is what says the save is live."""
    _pub, kube = configured
    work = _repo(tmp_path, dockerfile=f"FROM {RUNTIME}\n")
    _built(kube, work)
    (work / "bundle" / "persona.md").write_text("two\n")
    gh = GitHub()
    assert delivery.push(gh, work, "kobo: persona") is True
    assert gh.messages == ["kobo: persona [skip ci]"]
    assert kube.restarts == [("ledger", "ledger")] and kube.named == [gh.local_head(work)]


def test_a_commit_that_can_change_the_image_still_builds(tmp_path, configured):
    """A new runtime in the Dockerfile builds (the runtime route), and so does every
    commit of an agent whose Dockerfile reads the commit (the old shape on an older
    runtime, which bakes its bundle in)."""
    _pub, kube = configured
    work = _repo(tmp_path / "moved", dockerfile=f"FROM {RUNTIME}\n")
    (work / "Dockerfile").write_text("FROM reg.example/agents/daedalus-runtime@sha256:" + "c" * 64 + "\n")
    gh = GitHub()
    delivery.push(gh, work, "kobo: runtime image")
    assert gh.messages == ["kobo: runtime image"] and kube.restarts == []

    baked = _repo(tmp_path / "baked", dockerfile="FROM reg.example/agents/daedalus-runtime:old\nCOPY bundle/ /bundle\n")
    (baked / "bundle" / "persona.md").write_text("two\n")
    delivery.push(gh, baked, "kobo: persona")
    assert gh.messages[-1] == "kobo: persona"


def test_a_restart_after_a_secret_names_the_commit_it_delivers(tmp_path, configured):
    """The restart that follows a Secret names the commit it delivers: the one its
    caller published before it, or the one its step returns, and none otherwise."""
    _pub, _kube = configured
    plain = SecretKubectl(keys_after=0)
    delivery.restart_once_secret_carries(plain, "ledger", "ledger-slack", ["SLACK_BOT_TOKEN"], background=False)
    given = SecretKubectl(keys_after=0)
    delivery.restart_once_secret_carries(
        given, "ledger", "ledger-slack", ["SLACK_BOT_TOKEN"], background=False, commit="a" * 40,
    )
    stepped = SecretKubectl(keys_after=0)
    delivery.restart_once_secret_carries(
        stepped, "ledger", "ledger-slack", ["SLACK_BOT_TOKEN"], background=False, before=lambda: "b" * 40,
    )
    assert (plain.named, given.named, stepped.named) == ([None], ["a" * 40], ["b" * 40])


def test_a_save_after_a_build_that_never_landed_builds_again(tmp_path, configured):
    """Gate (integrity): a save used to retry a runtime build that failed, by being
    a commit that builds. It still does: when the image running predates the
    Dockerfile's last change, the next save builds, and the room's restart still
    delivers its bundle (named) on the image the agent has."""
    _pub, kube = configured
    work = _repo(tmp_path, dockerfile=f"FROM {RUNTIME}\n")
    _built(kube, work)
    (work / "Dockerfile").write_text("FROM reg.example/agents/daedalus-runtime@sha256:" + "c" * 64 + "\n")
    gh = GitHub()
    delivery.push(gh, work, "kobo: runtime image")
    (work / "bundle" / "persona.md").write_text("two\n")
    delivery.push(gh, work, "kobo: persona")
    assert gh.messages == ["kobo: runtime image", "kobo: persona"]
    assert kube.named == [gh.local_head(work)]


def test_an_image_that_bakes_its_bundle_in_is_restarted_by_its_build_alone(tmp_path, configured):
    """An agent whose Dockerfile copies its bundle in takes a change only from its
    build, so the room adds no restart that would deliver nothing."""
    _pub, kube = configured
    baked = _repo(tmp_path, dockerfile="FROM reg.example/agents/daedalus-runtime:old\nCOPY bundle/ /bundle\n")
    _built(kube, baked)
    (baked / "bundle" / "persona.md").write_text("two\n")
    gh = GitHub()
    delivery.push(gh, baked, "kobo: persona")
    assert gh.messages == ["kobo: persona"] and kube.restarts == []


def test_a_room_that_builds_each_change_names_no_restart(tmp_path):
    """Without bundles as artifacts no restart delivers a bundle (the build does), so
    none names a commit: the Slack steps' restart reads this before naming one."""
    work = _repo(tmp_path, dockerfile=f"FROM {RUNTIME}\n")
    assert delivery.delivered_by_restart(GitHub(), work) is None
