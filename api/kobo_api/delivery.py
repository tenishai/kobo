"""How a change the room makes to an agent reaches the running agent (one runtime,
bundles as artifacts; tenishai/kobo#434).

Every path that edits an agent's repository used to end in `github.commit_push`
and wait for the generated CI to build a new image and roll it: three minutes for
a persona edit, a skill, a picture. This module is the one place those paths go
through now. `push` commits as before (the repository stays the record) and then,
when the room is configured for artifacts (`configure`, from `Config.bundle_registry`),
publishes the bundle to the registry if the commit changed it and restarts the
agent's deployment, about thirty seconds. The image builds only when the
Dockerfile changes, which the runtime pin is the one path for.

An agent born before this (its Dockerfile still carries `COPY bundle/`) is moved to
the new shape by the first `push` that touches it, provided its Dockerfile already
pins the room's current runtime (the one that knows how to pull a bundle): the
Dockerfile shrinks to its FROM line, litestream and the entrypoint leave the
repository (the runtime carries them), and gojira.yaml gains the environment the
runtime reads. That one commit builds, as the last one that ever will for that
agent. An agent on an older runtime is left as it is and converts after its next
re-pin.

Unconfigured (no registry), `push` is `commit_push` and nothing else: every path
behaves exactly as it did.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

from kobo_api.bundle_publish import (
    POINTER_TAG,
    BundlePublisher,
    PublishError,
    bundle_changed_paths,
    bundle_reference,
    publish_if_changed,
)
from kobo_api.clients import ClientError, GitHubClient, KubectlClient

log = logging.getLogger(__name__)

# The environment the runtime's entrypoint reads (daedalus docker/entrypoint.sh).
ARTIFACT_ENV = "BUNDLE_ARTIFACT"
AGENT_NAME_ENV = "AGENT_NAME"
REPLICA_BUCKET_ENV = "MEMORY_REPLICA_BUCKET"
# The marker of the old shape: the bundle baked into the image.
LEGACY_COPY = "COPY bundle/"
CONVERTED_NOTE = " (the image is now the runtime only; the bundle travels as an artifact)"
NOT_DELIVERED = "the change is in the repository but did not reach the agent"


@dataclass(frozen=True)
class Delivery:
    kubectl: KubectlClient
    publisher: BundlePublisher
    registry: str
    runtime_image: str
    replica_bucket: str = ""


_delivery: Delivery | None = None


def configure(delivery: Delivery | None) -> None:
    """Set at boot from the room's config and clients; None turns artifacts off."""
    global _delivery
    _delivery = delivery


def configured() -> bool:
    return _delivery is not None


def bundle_artifact(registry: str, name: str) -> str:
    """The pointer the pod pulls at start: `<registry>/<agent>-bundle:current`."""
    return bundle_reference(registry, name, POINTER_TAG)


def optional_secrets(name: str) -> list[str]:
    """The Secrets a pod reads when they exist (gojira `secrets_from_optional`): the
    Slack values and the owner's env panel, both added after birth."""
    return [f"{name}-slack", f"{name}-env"]


def runtime_env(name: str, *, registry: str, replica_bucket: str) -> dict[str, str]:
    """What the entrypoint reads, for gojira.yaml `env`."""
    env = {ARTIFACT_ENV: bundle_artifact(registry, name), AGENT_NAME_ENV: name}
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
    env = data.get("env") if isinstance(data.get("env"), dict) else {}
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
        "# Agent image: the shared runtime, nothing else. The bundle travels as a registry\n"
        "# artifact the runtime pulls at start (BUNDLE_ARTIFACT in gojira.yaml).\n"
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
    return published.pointer if published else None


def push(github: GitHubClient, workdir: Path, message: str, *, restart: bool = True, force_restart: bool = False) -> bool:
    """Commit and push `workdir`, then deliver: publish the bundle when the commit
    changed it and restart the agent, when the room is configured for artifacts.
    `force_restart` restarts even when the bundle did not change (a Secret the pod
    reads changed). Returns whether anything was pushed, like `commit_push`."""
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
    pushed = github.commit_push(workdir, message)
    if not pushed and not force_restart:
        return False
    changed = pushed and (built or _bundle_changed(workdir))
    if changed:
        publish(workdir, name, head_sha=github.local_head(workdir) or "")
    if built:
        # The conversion commit builds, as the last one that will: the roll is the restart.
        return pushed
    if (changed and restart) or force_restart:
        _restart(name)
    return pushed


def _restart(name: str) -> None:
    assert _delivery is not None
    try:
        if not _delivery.kubectl.deployment_exists(name, name):
            log.info("%s has no deployment yet; the pull at its first start delivers the bundle", name)
            return
        _delivery.kubectl.restart_deploy(name, name)
    except ClientError as exc:
        raise ClientError(f"{NOT_DELIVERED}: the restart failed ({exc}); try again") from exc


__all__ = [
    "AGENT_NAME_ENV", "ARTIFACT_ENV", "REPLICA_BUCKET_ENV", "Delivery", "agent_name_of", "bundle_artifact",
    "configure", "configured", "convert_to_runtime_only", "legacy_shape", "optional_secrets", "publish", "push",
    "runtime_env", "wire_gojira_yaml",
]
