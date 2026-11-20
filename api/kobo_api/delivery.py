"""How a change the room makes to an agent reaches the running agent (one runtime,
bundles as artifacts; tenishai/kobo#434).

Every path that edits an agent's repository used to end in `github.commit_push`
and wait for the generated CI to build a new image and roll it: three minutes for
a persona edit, a skill, a picture. This module is the one place those paths go
through now. `push` commits as before (the repository stays the record) and then,
when the room is configured for artifacts (`configure`, from `Config.bundle_registry`),
publishes the bundle to the registry if the commit changed it and restarts the
agent's deployment, about thirty seconds. The image builds only when the
Dockerfile changes, which the runtime pin is the one path for: any other commit
skips the generated build (`needs_build`), which would only deploy the same image
again under the commit's tag and roll the agent a second time, a minute or two
after the room's own restart (seen on johnny-bravo, 2026-11-20). The restart
names the commit it delivers on the pod template (`BUNDLE_COMMIT_ANNOTATION`),
and what reads "is this commit live" reads that beside the image's tag
(`live_commit`).

An agent born before this (its Dockerfile still carries `COPY bundle/`) is moved to
the new shape by the first `push` that touches it, provided its Dockerfile already
pins the room's current runtime (the one that knows how to pull a bundle): the
Dockerfile shrinks to its FROM line, litestream and the entrypoint leave the
repository (the runtime carries them), and gojira.yaml gains the environment the
runtime reads. A changed gojira.yaml fails the generated build's contract check,
so that commit skips the build and the deploy platform is asked to render the deploy
files again, which deploys the new shape (`push`). An agent on an older runtime is
left as it is and converts after its next re-pin.

Unconfigured (no registry), `push` is `commit_push` and nothing else: every path
behaves exactly as it did.
"""
from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from kobo_api.agent_env import CI_WORKFLOW, GOJIRA_YAML, contract_behind
from kobo_api.bundle_publish import (
    POINTER_TAG,
    BundlePublisher,
    PublishError,
    bundle_changed_paths,
    bundle_reference,
    publish_if_changed,
)
from kobo_api.clients import (
    ClientError,
    GitHubClient,
    GojiraClient,
    KubectlClient,
    image_tag,
)
from kobo_api.kinds import is_connected
from kobo_api.models import Agent

log = logging.getLogger(__name__)

# The gojira.yaml key that mounts the bundle image read-only at /bundle (a Kubernetes image
# volume the node pulls on every start with its own credentials; gojira `bundle_image`).
BUNDLE_IMAGE_KEY = "bundle_image"
# The environment the runtime's entrypoint reads (daedalus docker/entrypoint.sh).
AGENT_NAME_ENV = "AGENT_NAME"
REPLICA_BUCKET_ENV = "MEMORY_REPLICA_BUCKET"
# The marker of the old shape: the bundle baked into the image.
LEGACY_COPY = "COPY bundle/"
# A Dockerfile that reads the commit into the image: the generated build's own test
# for whether an image can depend on the commit (its BUILD_KEY), kept the same.
_READS_CONTEXT = re.compile(r"^[ \t]*(COPY|ADD)[ \t]|--mount=type=bind", re.IGNORECASE | re.MULTILINE)
# On the agent's pod template: the commit whose bundle the room last restarted it
# with. A save that skips the build leaves the image's tag behind, so this is what
# says the save is live.
BUNDLE_COMMIT_ANNOTATION = "kobo/bundle-commit"
CONVERTED_NOTE = " (the image is now the runtime only; the bundle travels as an artifact)"
NOT_DELIVERED = "the change is in the repository but did not reach the agent"
PLATFORM_NOT_ASKED = "the change is in the repository, but the deploy platform could not be asked to deploy it"


@dataclass(frozen=True)
class Delivery:
    kubectl: KubectlClient
    publisher: BundlePublisher
    registry: str
    runtime_image: str
    replica_bucket: str = ""
    # Asks the deploy platform to render an agent's deploy files again, by name
    # (`reprovisioner`), for a push whose gojira.yaml they no longer match (see
    # `push`). Without it such a push builds, and the build's contract check
    # holds the agent where it is.
    reprovision: Callable[[str], None] | None = None


_delivery: Delivery | None = None


def configure(delivery: Delivery | None) -> None:
    """Set at boot from the room's config and clients; None turns artifacts off."""
    global _delivery
    _delivery = delivery


def configured() -> bool:
    return _delivery is not None


def save_pin(runtime_image: str | None) -> str | None:
    """The runtime a save may pin its agent's Dockerfile to on the way past
    (an upload, a check, a teach note, Slack wiring): `runtime_image` in a room
    that builds an image per change, where a save is a build anyway, and None
    in a room that delivers bundles as artifacts. There the runtime route is the
    one place a pin moves: a re-pin riding a save moved an agent onto a runtime
    nobody chose, and rebuilt and restarted it a second time (a teach note moved
    johnny-bravo, 2026-11-20)."""
    return None if configured() else runtime_image


def reprovisioner(
    session_factory: Callable[[], Any], gojira: GojiraClient | None,
) -> Callable[[str], None] | None:
    """`Delivery.reprovision` for a room or a script: finds the agent's deploy
    platform app by name and asks for its deploy files again, as a hand-over
    for a connected agent (`kobo_api.agent_env.regenerate_handed_over`). None
    for a room without the platform, so a conversion there builds as it did.
    The ask raises ClientError when the agent has no app to ask."""
    if gojira is None:
        return None

    def reprovision(name: str) -> None:
        with session_factory() as session:
            agent = session.query(Agent).filter_by(name=name).first()
            app_id = agent.gojira_app_id if agent is not None else None
            handed_over = agent is not None and is_connected(agent)
        if app_id is None:
            raise ClientError(f"no deploy platform app to ask for {name}")
        gojira.reprovision(app_id, handed_over=handed_over)

    return reprovision


def bundle_artifact(registry: str, name: str) -> str:
    """The pointer the pod pulls at start: `<registry>/<agent>-bundle:current`."""
    return bundle_reference(registry, name, POINTER_TAG)


def optional_secrets(name: str) -> list[str]:
    """The Secrets a pod reads when they exist (gojira `secrets_from_optional`): the
    Slack values and the owner's env panel, both added after birth."""
    return [f"{name}-slack", f"{name}-env"]


def runtime_env(name: str, *, registry: str, replica_bucket: str) -> dict[str, str]:
    """What the entrypoint reads, for gojira.yaml `env`: the agent's name (the memory
    replica's path) and the replica bucket. The bundle itself arrives as a mounted image
    volume (`BUNDLE_IMAGE_KEY`), never through the environment, so the pod pulls nothing."""
    env = {AGENT_NAME_ENV: name}
    if replica_bucket:
        env[REPLICA_BUCKET_ENV] = replica_bucket
    return env


def agent_name_of(workdir: Path) -> str | None:
    try:
        data = yaml.safe_load((workdir / "bundle" / "agent.yaml").read_text()) or {}
    except (OSError, yaml.YAMLError):
        return None
    name = data.get("name")
    return str(name) if name else None


def legacy_shape(workdir: Path) -> bool:
    """True when the agent's image still bakes its bundle in."""
    try:
        return LEGACY_COPY in (workdir / "Dockerfile").read_text()
    except OSError:
        return False


def _dockerfile_from(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("FROM "):
            return line[5:].strip()
    return None


def wire_gojira_yaml(data: dict, name: str, *, registry: str, replica_bucket: str) -> bool:
    """The gojira.yaml half of the new shape: the runtime's env and the optional
    Secret references. Idempotent; returns whether anything changed."""
    changed = False
    pointer = bundle_artifact(registry, name)
    if data.get(BUNDLE_IMAGE_KEY) != pointer:
        data[BUNDLE_IMAGE_KEY] = pointer
        changed = True
    env = data.get("env") if isinstance(data.get("env"), dict) else {}
    env.pop("BUNDLE_ARTIFACT", None)  # the earlier shape: the runtime pulled it itself
    for key, value in runtime_env(name, registry=registry, replica_bucket=replica_bucket).items():
        if env.get(key) != value:
            env[key] = value
            changed = True
    data["env"] = env
    optional = [s for s in (data.get("secrets_from_optional") or []) if s]
    strict = [s for s in (data.get("secrets_from") or []) if s]
    for secret in optional_secrets(name):
        if secret not in optional:
            optional.append(secret)
            changed = True
        if secret in strict:
            strict.remove(secret)
            changed = True
    data["secrets_from_optional"] = optional
    if strict or "secrets_from" in data:
        data["secrets_from"] = strict
    return changed


def convert_to_runtime_only(workdir: Path, name: str, *, runtime_image: str, registry: str, replica_bucket: str) -> bool:
    """Move an agent of the old shape to the new one, in the working copy. Only
    when its Dockerfile pins the room's current runtime, the one that can pull a
    bundle; otherwise nothing changes and False is returned."""
    dockerfile = workdir / "Dockerfile"
    text = dockerfile.read_text()
    if _dockerfile_from(text) != runtime_image:
        log.info("%s stays on the old shape: its Dockerfile pins %s, not the room's runtime", name, _dockerfile_from(text))
        return False
    dockerfile.write_text(
        "# Agent image: the shared runtime, nothing else. The bundle is a registry image the\n"
        "# node mounts at /bundle on every start (bundle_image in gojira.yaml).\n"
        f"FROM {runtime_image}\n"
    )
    for leftover in ("litestream.yml", "entrypoint.sh"):
        (workdir / leftover).unlink(missing_ok=True)
    gojira_yaml = workdir / "gojira.yaml"
    if gojira_yaml.exists():
        data = yaml.safe_load(gojira_yaml.read_text()) or {}
        wire_gojira_yaml(data, name, registry=registry, replica_bucket=replica_bucket)
        gojira_yaml.write_text(yaml.safe_dump(data, sort_keys=False))
    return True


def _bundle_changed(workdir: Path) -> bool:
    return bool(bundle_changed_paths(workdir))


def deploy_files_behind(workdir: Path) -> bool:
    """True when the working copy's deploy files were rendered from a gojira.yaml
    other than the one it holds: its build fails the contract check, and only the
    platform rendering them again deploys it (ledger, 2026-11-04 to 11-20: its
    conversion changed gojira.yaml and every later build failed). False when
    either file is missing or unreadable."""
    try:
        text = (workdir / GOJIRA_YAML).read_text()
        workflow = (workdir / CI_WORKFLOW).read_text()
    except (OSError, UnicodeDecodeError):
        return False
    return contract_behind(text, workflow) is not None


def needs_build(workdir: Path) -> bool:
    """Whether the working copy's commit can change the agent's image: its
    Dockerfile reads the commit (the old shape bakes the bundle in) or changed.
    Otherwise the generated build makes the same image again and its deploy rolls
    the agent once more for nothing, so the commit skips it."""
    try:
        text = (workdir / "Dockerfile").read_text()
    except OSError:
        return True
    if _READS_CONTEXT.search(text):
        return True
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", "Dockerfile"], cwd=workdir, capture_output=True, text=True, check=False,
    )
    return status.returncode != 0 or bool(status.stdout.strip())


# What this process published last for each agent, and when (monotonic): the commit
# a restart that follows a publishing step names (`restart_once_secret_carries`).
_published: dict[str, tuple[str, float]] = {}


def publish(workdir: Path, name: str, *, head_sha: str) -> str | None:
    """Publish the working copy's bundle (HEAD's) and move the agent's pointer.
    Returns the pointer, or None when HEAD has no bundle. Raises ClientError."""
    if _delivery is None:
        return None
    try:
        published = publish_if_changed(
            _delivery.publisher, workdir=workdir, agent_name=name, repository=_delivery.registry,
            head_sha=head_sha or "0" * 12, last_tree_id=None,
        )
    except PublishError as exc:
        raise ClientError(f"{NOT_DELIVERED}: {exc}; try again") from exc
    if published and head_sha:
        _published[name] = (head_sha, time.monotonic())
    return published.pointer if published else None


def bundle_commit(kubectl: KubectlClient, namespace: str, deploy: str) -> str | None:
    """The commit the room last restarted the agent with, from its pod template;
    None for one it has not restarted since this was written, or when the
    cluster cannot be read."""
    try:
        dep = kubectl.deployment_status_json(namespace, deploy)
    except Exception:  # noqa: BLE001 -- no claim about what was not seen
        return None
    template = ((dep.get("spec") or {}).get("template") or {}).get("metadata") or {}
    commit = (template.get("annotations") or {}).get(BUNDLE_COMMIT_ANNOTATION)
    return commit if isinstance(commit, str) and commit else None


def live_commit(github: GitHubClient, kubectl: KubectlClient, workdir: Path, *, namespace: str, deploy: str) -> str | None:
    """The newest commit the running agent is known to carry, resolved in the
    clone `workdir`: the one its image was built from (the image's tag) or the
    one the room last restarted it with (`bundle_commit`), whichever is later.
    None when neither resolves (a tagless image and no restart named yet)."""
    tag = image_tag(kubectl.deploy_image(namespace, deploy))
    built = github.rev_parse_prefix(workdir, tag) if tag is not None else None
    named = bundle_commit(kubectl, namespace, deploy)
    restarted = github.rev_parse_prefix(workdir, named) if named is not None else None
    if built is None or restarted is None:
        return built or restarted
    return restarted if github.is_ancestor(workdir, built, restarted) else built


def push(
    github: GitHubClient, workdir: Path, message: str, *,
    restart: bool = True, force_restart: bool = False, asks_platform: bool = False,
) -> bool:
    """Commit and push `workdir`, then deliver: publish the bundle when the commit
    changed it and restart the agent, when the room is configured for artifacts.
    `force_restart` restarts even when the bundle did not change (a Secret the pod
    reads changed). Returns whether anything was pushed, like `commit_push`.

    When the deploy files no longer match gojira.yaml (the conversion below just
    changed it, or an earlier ask never reached the platform), the commit skips
    the build their contract check would fail, and the platform is asked to
    render them again, which builds and rolls the agent. A restart still
    delivers what it can first (never for the conversion, whose running image
    still carries its old bundle). The platform is asked whether or not anything
    was pushed, so asking again after a failure repairs the agent
    (`kobo_api.agent_env.edit_gojira_yaml` does the same). `asks_platform` is
    for a caller that changed gojira.yaml itself and asks the platform right
    after: the commit skips the build all the same, nothing is asked twice, and
    the platform's deploy is the one restart. Raises ClientError when the
    publish, the restart or the ask fails."""
    if _delivery is None:
        return github.commit_push(workdir, message)
    name = agent_name_of(workdir)
    if name is None:
        return github.commit_push(workdir, message)
    built = False
    if legacy_shape(workdir):
        built = convert_to_runtime_only(
            workdir, name, runtime_image=_delivery.runtime_image,
            registry=_delivery.registry, replica_bucket=_delivery.replica_bucket,
        )
        if built:
            message = f"{message}{CONVERTED_NOTE}"
    behind = (asks_platform or _delivery.reprovision is not None) and deploy_files_behind(workdir)
    if (behind or not needs_build(workdir)) and "[skip ci]" not in message:
        message = f"{message} [skip ci]"
    pushed = github.commit_push(workdir, message)
    changed = pushed and (built or _bundle_changed(workdir))
    head = github.local_head(workdir) or ""
    if changed:
        publish(workdir, name, head_sha=head)
    delivered = head if changed else None
    if behind:
        if asks_platform or _delivery.reprovision is None:
            return pushed
        if not built and ((changed and restart) or force_restart):
            _restart(name, delivered)
        try:
            _delivery.reprovision(name)
        except Exception as exc:  # a refusal, a timeout or no app: the same failure here
            raise ClientError(f"{PLATFORM_NOT_ASKED} ({exc}); try again") from exc
        return pushed
    if built:
        # Nobody to ask: the conversion commit builds, and the roll is the restart.
        return pushed
    if (changed and restart) or force_restart:
        _restart(name, delivered)
    return pushed


def redeliver(github: GitHubClient, workdir: Path) -> bool:
    """Publish the working copy's bundle as it stands and restart the agent,
    with nothing to commit: for a change whose commit landed and whose publish
    or restart did not, where `push` asked again finds nothing to push and
    stops. Returns whether it delivered (False in a room that builds each
    change, and for an agent still on the old shape). Raises ClientError."""
    if _delivery is None:
        return False
    name = agent_name_of(workdir)
    if name is None or legacy_shape(workdir):
        return False
    head = github.local_head(workdir) or ""
    publish(workdir, name, head_sha=head)
    _restart(name, head or None)
    return True


def _restart(name: str, commit: str | None = None) -> None:
    """Roll the agent now, naming `commit` (the one whose bundle it was just
    published with) on its pod template when there is one."""
    assert _delivery is not None
    try:
        if not _delivery.kubectl.deployment_exists(name, name):
            log.info("%s has no deployment yet; the pull at its first start delivers the bundle", name)
            return
        _delivery.kubectl.restart_deploy(name, name, annotations=_named(commit))
    except ClientError as exc:
        raise ClientError(f"{NOT_DELIVERED}: the restart failed ({exc}); try again") from exc


SECRET_WAIT = 150.0
SECRET_POLL = 5.0
# How long a restart waits on the step given to run before it.
STEP_WAIT = 300.0


def secret_carries(
    kubectl: KubectlClient, namespace: str, secret: str, keys: Iterable[str],
    settings: Mapping[str, str | None] | None = None,
) -> bool:
    """Whether the cluster Secret holds every key named. Only the names are looked at,
    except for `settings`: plain settings the room wrote there itself, never a credential,
    which must read as given (None: the key is gone). A setting changed on a key the
    Secret already carried looks the same by name before and after it lands."""
    try:
        present = kubectl.secret_literals(namespace, secret)
    except ClientError:
        return False
    return set(keys) <= set(present) and all(present.get(k) == v for k, v in (settings or {}).items())


def wait_for_secret(
    kubectl: KubectlClient, namespace: str, secret: str, keys: Iterable[str],
    *, settings: Mapping[str, str | None] | None = None,
    timeout: float | None = None, interval: float | None = None,
    sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Wait until the Secret carries `keys`, up to `timeout` seconds. A value delivered
    through the secrets platform reaches the cluster at its next re-read, a minute or so
    later; a pod restarted before that starts on the old Secret (scooby-doo, 2026-11-03:
    the install's restart beat the bot token to the cluster by five seconds and the agent
    never answered in Slack). `settings` as `secret_carries` reads them. Returns whether
    the keys were seen."""
    keys = list(keys)
    timeout = SECRET_WAIT if timeout is None else timeout
    interval = SECRET_POLL if interval is None else interval
    deadline = clock() + timeout
    while True:
        if secret_carries(kubectl, namespace, secret, keys, settings):
            return True
        if clock() >= deadline:
            return False
        sleep(interval)


def restart_once_secret_carries(
    kubectl: KubectlClient, name: str, secret: str, keys: Iterable[str], *, background: bool = True,
    before: Callable[[], None] | None = None, settings: Mapping[str, str | None] | None = None,
) -> threading.Thread | None:
    """Restart agent `name` once its Secret carries `keys` (and reads `settings`, as
    `secret_carries` does), restarting anyway when the wait runs out (the next start
    then still reads whatever has landed). In the background by default, so a
    request does not hold for the platform's re-read.
    `before` is a bundle change this one restart should carry too: it runs beside the
    wait and the restart follows it. Whatever it raises is logged, and one still
    running `STEP_WAIT` seconds after the wait is left behind; the restart happens
    either way. The restart names a commit this process published since the call
    began (`before`'s), never an older one another replica may have moved past."""
    keys = list(keys)
    began = time.monotonic()

    def _before() -> None:
        try:
            before()
        except Exception as exc:  # noqa: BLE001 -- the restart must not be lost to it
            log.warning("%s: the step before its restart failed: %s", name, exc)

    def _run() -> None:
        step = None
        if before is not None:
            step = threading.Thread(target=_before, name=f"before-restart-{name}", daemon=True)
            step.start()
        if not wait_for_secret(kubectl, name, secret, keys, settings=settings):
            log.warning("%s: %s did not reach the cluster in time; restarting anyway", name, secret)
        if step is not None:
            step.join(STEP_WAIT)
            if step.is_alive():
                log.warning("%s: the step before its restart is still running; restarting without it", name)
        published = _published.get(name)
        commit = published[0] if published is not None and published[1] >= began else None
        try:
            kubectl.restart_deploy(name, name, annotations=_named(commit))
        except ClientError as exc:
            log.warning("restart of %s after a secret change failed: %s", name, exc)

    if not background:
        _run()
        return None
    thread = threading.Thread(target=_run, name=f"restart-{name}", daemon=True)
    _restarts[:] = [t for t in _restarts if t.is_alive()]
    _restarts.append(thread)
    thread.start()
    return thread


_restarts: list[threading.Thread] = []


def _named(commit: str | None) -> dict[str, str] | None:
    return {BUNDLE_COMMIT_ANNOTATION: commit} if commit else None


def join_restarts(timeout: float = 5.0) -> None:
    """Wait for the background restarts started so far (tests, and a clean shutdown)."""
    for thread in _restarts:
        thread.join(timeout)


__all__ = [
    "AGENT_NAME_ENV",
    "BUNDLE_COMMIT_ANNOTATION",
    "BUNDLE_IMAGE_KEY",
    "REPLICA_BUCKET_ENV",
    "Delivery",
    "agent_name_of",
    "bundle_artifact",
    "bundle_commit",
    "configure",
    "configured",
    "convert_to_runtime_only",
    "deploy_files_behind",
    "join_restarts",
    "legacy_shape",
    "live_commit",
    "needs_build",
    "optional_secrets",
    "publish",
    "push",
    "reprovisioner",
    "restart_once_secret_carries",
    "runtime_env",
    "save_pin",
    "secret_carries",
    "wait_for_secret",
    "wire_gojira_yaml",
]
