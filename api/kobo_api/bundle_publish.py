"""Publish an agent's `bundle/` as a registry artifact (one runtime, bundles as
artifacts; design `docs/superpowers/specs/2026-10-18-one-runtime-and-admin-
panel-design.md`, Arc 1, step 1; issue #172).

A composed agent stops being its own image. Its files travel as an OCI
artifact next to the runtime image: pushed once per commit that changes
`bundle/`, tagged with that commit's sha, and named by a per-agent pointer
tag the runtime resolves at boot. Two rules this module owns:

- **Publish only when the bundle changed.** The key is the git tree id of
  `bundle/` at HEAD, not the commit sha: a commit that touches only
  `gojira.yaml` has a new sha and an identical tree, and publishing it
  would move the pointer and restart the agent for nothing.
- **Publish is not restart.** This module moves the pointer; the caller
  decides whether the files that changed are ones the runtime reads at
  boot. `changed_paths` is handed back for that decision.

Nothing here knows about Gojira, the contract or restarts.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

POINTER_TAG = "current"
"""The per-agent pointer tag: `<repository>/<agent>-bundle:current`."""

BUNDLE_DIR = "bundle"


class PublishError(RuntimeError):
    """The registry refused, or the tool is missing. The commit already
    landed; the caller logs this and leaves the pointer where it was."""


@dataclass(frozen=True)
class Published:
    """One publish: what was pushed and what the pointer now names."""

    reference: str
    """`<repository>/<agent>-bundle:<sha>`, the immutable name."""
    pointer: str
    """`<repository>/<agent>-bundle:current`, moved to this digest."""
    digest: str
    """`sha256:<64 hex>` of the pushed artifact."""
    tree_id: str
    """The git tree id of `bundle/` that this artifact carries."""


class BundlePublisher(Protocol):
    """Push a directory as an OCI artifact and move a tag. The two methods a
    registry needs; the fake in tests records both."""

    def push(self, directory: Path, reference: str) -> str:
        """Push `directory` as one artifact named `reference`. Returns the
        digest (`sha256:...`)."""
        ...

    def tag(self, reference: str, tag: str) -> None:
        """Point `tag` (a bare tag name) at what `reference` names, in the
        same repository."""
        ...


class OrasCliPublisher:
    """The `oras` CLI, which the room image carries alongside gcloud. Auth is
    the ambient one `gcloud auth configure-docker` set up for the registry
    host, so no credential passes through here."""

    def __init__(self, *, oras: str = "oras", timeout_s: float = 300.0):
        self._oras = oras
        self._timeout_s = timeout_s

    def _run(self, *args: str, cwd: Path | None = None) -> str:
        try:
            proc = subprocess.run(
                [self._oras, *args],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
                check=False,
            )
        except FileNotFoundError as exc:
            raise PublishError(f"{self._oras} is not installed in this image") from exc
        except subprocess.TimeoutExpired as exc:
            raise PublishError(f"{self._oras} {args[0]} timed out after {self._timeout_s:.0f}s") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            tail = detail[-1] if detail else f"exit {proc.returncode}"
            raise PublishError(f"{self._oras} {args[0]} failed: {tail}")
        return proc.stdout

    def push(self, directory: Path, reference: str) -> str:
        # `.` from inside the directory so the artifact's paths are relative
        # to the bundle root, which is what the runtime unpacks into.
        out = self._run(
            "push",
            "--format",
            "go-template={{.digest}}",
            "--artifact-type",
            "application/vnd.tenish.agent-bundle.v1",
            reference,
            ".",
            cwd=directory,
        )
        digest = out.strip()
        if not digest.startswith("sha256:"):
            raise PublishError(f"{self._oras} push returned no digest: {out.strip()[:120]!r}")
        return digest

    def tag(self, reference: str, tag: str) -> None:
        self._run("tag", reference, tag)


def bundle_tree_id(workdir: Path) -> str | None:
    """The git tree id of `bundle/` at HEAD in `workdir`, or None when HEAD
    has no `bundle/`. This is the publish key: identical files, identical
    id, regardless of which commit carries them."""
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"HEAD:{BUNDLE_DIR}"],
        cwd=workdir,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def bundle_changed_paths(workdir: Path) -> list[str]:
    """The paths under `bundle/` that HEAD changed against its first parent,
    relative to `bundle/`. Empty for a root commit with no bundle, and for a
    commit that touched nothing under it. The caller reads this to decide
    whether a restart is due (persona, skills, tools, wiring) or not (intake
    notes and knowledge the relay already delivered)."""
    proc = subprocess.run(
        ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD", "--", BUNDLE_DIR],
        cwd=workdir,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return []
    prefix = f"{BUNDLE_DIR}/"
    return [line[len(prefix) :] for line in proc.stdout.splitlines() if line.startswith(prefix)]


def bundle_reference(repository: str, agent_name: str, tag: str) -> str:
    """`<repository>/<agent>-bundle:<tag>`. `repository` is the registry
    path the runtime image lives in (the org's `bundle_registry`), so the
    pod's one reader grant covers both."""
    return f"{repository.rstrip('/')}/{agent_name}-bundle:{tag}"


def publish_if_changed(
    publisher: BundlePublisher,
    *,
    workdir: Path,
    agent_name: str,
    repository: str,
    head_sha: str,
    last_tree_id: str | None,
) -> Published | None:
    """Publish HEAD's `bundle/` and move the pointer, unless its tree id is
    `last_tree_id` (nothing to publish) or HEAD has no bundle. Returns what
    was published, or None when nothing was.

    Raises PublishError when the registry refuses: the caller records the
    tree id only on success, so the next commit retries."""
    tree_id = bundle_tree_id(workdir)
    if tree_id is None:
        log.info("bundle publish skipped for %s: HEAD has no %s/", agent_name, BUNDLE_DIR)
        return None
    if tree_id == last_tree_id:
        log.info("bundle publish skipped for %s: %s/ unchanged (%s)", agent_name, BUNDLE_DIR, tree_id[:12])
        return None

    reference = bundle_reference(repository, agent_name, head_sha[:12])
    pointer = bundle_reference(repository, agent_name, POINTER_TAG)
    digest = publisher.push(workdir / BUNDLE_DIR, reference)
    publisher.tag(reference, POINTER_TAG)
    log.info("bundle published for %s: %s (%s), pointer %s", agent_name, reference, digest[:19], pointer)
    return Published(reference=reference, pointer=pointer, digest=digest, tree_id=tree_id)
