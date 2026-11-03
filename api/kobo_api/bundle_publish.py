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

import hashlib
import io
import json
import logging
import subprocess
import tarfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

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


LAYER_MEDIA_TYPE = "application/vnd.tenish.agent-bundle.tar+gzip"
ARTIFACT_TYPE = "application/vnd.tenish.agent-bundle.v1"
MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
EMPTY_CONFIG = b"{}"
EMPTY_CONFIG_MEDIA_TYPE = "application/vnd.oci.empty.v1+json"
METADATA_TOKEN_URL = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"


def metadata_access_token(client: httpx.Client | None = None) -> str:
    """The pod's own Google access token, from the metadata server (Workload
    Identity): the room's identity pushes the bundle, the way it already talks to
    Google. Raises PublishError when there is none."""
    own = client or httpx.Client(timeout=5.0)
    try:
        resp = own.get(METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"})
        if resp.status_code != 200:
            raise PublishError(f"no access token from the metadata server (HTTP {resp.status_code})")
        return str(resp.json().get("access_token") or "")
    except httpx.HTTPError as exc:
        raise PublishError(f"no access token from the metadata server ({exc.__class__.__name__})") from exc
    finally:
        if client is None:
            own.close()


def bundle_tarball(directory: Path) -> bytes:
    """The bundle directory as one deterministic gzip tarball: entries sorted,
    times zeroed, so an unchanged bundle has an unchanged digest."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=6) as tar:
        for path in sorted(p for p in directory.rglob("*") if p.is_file()):
            info = tar.gettarinfo(str(path), arcname=path.relative_to(directory).as_posix())
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            with path.open("rb") as handle:
                tar.addfile(info, handle)
    return buf.getvalue()


def _split_reference(reference: str) -> tuple[str, str, str]:
    host, _, rest = reference.partition("/")
    if "@" in rest:
        repo, _, ref = rest.partition("@")
    elif ":" in rest.rsplit("/", 1)[-1]:
        repo, _, ref = rest.rpartition(":")
    else:
        repo, ref = rest, "latest"
    if not host or not repo:
        raise PublishError(f"not a registry reference: {reference!r}")
    return host, repo, ref


class RegistryPublisher:
    """Pushes a bundle as an OCI artifact by talking to the registry directly (the
    distribution API), so the room's image needs no tool. The artifact is one
    layer, the bundle's gzip tarball (`LAYER_MEDIA_TYPE`), under an empty config,
    the shape the runtime's `bundle_pull.py` reads. Authorization is a Google
    access token the pod's identity provides; the registry must grant it write on
    the repository (`Config.bundle_registry`)."""

    def __init__(
        self,
        *,
        token_provider: Callable[[], str] = metadata_access_token,
        client: httpx.Client | None = None,
        timeout_s: float = 120.0,
    ):
        self._token_provider = token_provider
        self._client = client or httpx.Client(timeout=timeout_s, follow_redirects=False)
        self._token: str = ""
        self._token_at: float = 0.0

    def _headers(self) -> dict[str, str]:
        if not self._token or time.monotonic() - self._token_at > 1800:
            self._token = self._token_provider()
            self._token_at = time.monotonic()
        return {"Authorization": f"Bearer {self._token}"}

    def _refused(self, what: str, resp: httpx.Response) -> PublishError:
        detail = resp.text.strip().splitlines()
        return PublishError(f"the registry refused the {what} (HTTP {resp.status_code}): {detail[0][:160] if detail else ''}")

    def _upload_blob(self, host: str, repo: str, data: bytes, digest: str) -> None:
        base = f"https://{host}/v2/{repo}/blobs"
        try:
            head = self._client.head(f"{base}/{digest}", headers=self._headers())
            if head.status_code == 200:
                return
            start = self._client.post(f"{base}/uploads/", headers=self._headers())
            if start.status_code not in (201, 202):
                raise self._refused("upload", start)
            location = start.headers.get("location", "")
            if location.startswith("/"):
                location = f"https://{host}{location}"
            joiner = "&" if "?" in location else "?"
            put = self._client.put(
                f"{location}{joiner}digest={digest}", content=data,
                headers={**self._headers(), "Content-Type": "application/octet-stream"},
            )
            if put.status_code not in (201, 204):
                raise self._refused("blob", put)
        except httpx.HTTPError as exc:
            raise PublishError(f"the registry could not be reached ({exc.__class__.__name__})") from exc

    def push(self, directory: Path, reference: str) -> str:
        host, repo, tag = _split_reference(reference)
        blob = bundle_tarball(directory)
        layer_digest = "sha256:" + hashlib.sha256(blob).hexdigest()
        config_digest = "sha256:" + hashlib.sha256(EMPTY_CONFIG).hexdigest()
        self._upload_blob(host, repo, EMPTY_CONFIG, config_digest)
        self._upload_blob(host, repo, blob, layer_digest)
        manifest = {
            "schemaVersion": 2,
            "mediaType": MANIFEST_MEDIA_TYPE,
            "artifactType": ARTIFACT_TYPE,
            "config": {"mediaType": EMPTY_CONFIG_MEDIA_TYPE, "digest": config_digest, "size": len(EMPTY_CONFIG)},
            "layers": [{
                "mediaType": LAYER_MEDIA_TYPE, "digest": layer_digest, "size": len(blob),
                "annotations": {"org.opencontainers.image.title": "bundle.tar.gz"},
            }],
            "annotations": {"org.opencontainers.image.created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
        }
        body = json.dumps(manifest, separators=(",", ":")).encode()
        return self._put_manifest(host, repo, tag, body)

    def _put_manifest(self, host: str, repo: str, tag: str, body: bytes) -> str:
        try:
            resp = self._client.put(
                f"https://{host}/v2/{repo}/manifests/{tag}", content=body,
                headers={**self._headers(), "Content-Type": MANIFEST_MEDIA_TYPE},
            )
        except httpx.HTTPError as exc:
            raise PublishError(f"the registry could not be reached ({exc.__class__.__name__})") from exc
        if resp.status_code not in (201, 200):
            raise self._refused("manifest", resp)
        return resp.headers.get("docker-content-digest") or ("sha256:" + hashlib.sha256(body).hexdigest())

    def tag(self, reference: str, tag: str) -> None:
        host, repo, ref = _split_reference(reference)
        try:
            resp = self._client.get(
                f"https://{host}/v2/{repo}/manifests/{ref}", headers={**self._headers(), "Accept": MANIFEST_MEDIA_TYPE},
            )
        except httpx.HTTPError as exc:
            raise PublishError(f"the registry could not be reached ({exc.__class__.__name__})") from exc
        if resp.status_code != 200:
            raise self._refused("manifest read", resp)
        self._put_manifest(host, repo, tag, resp.content)


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
