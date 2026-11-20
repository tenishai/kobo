"""Knowledge module (P2b): read, edit, and delete an agent's seed-memory files
(`bundle/knowledge/*.md` in the agent's repo — the bundle of record), plus a
stateless check for whether a saved edit has actually reached the live
Deployment yet.

Every operation here clones the repo fresh into a `tempfile.TemporaryDirectory`
and discards it before returning — there is no local working copy kept around
between calls, mirroring how the rest of kobo_api treats a repo clone as a
disposable means to an end (see `kobo_api.steps`), never state of its own.

`delivery_state` needs no local clone at all: it's a pure read against two
already-authoritative sources (the commit's CI run, the live Deployment's
image) with no kobo-side state to keep in sync — see its docstring for the
exact branch logic, which mirrors `kobo_api.steps.BuildStep`'s
newest-run-first convention over `runs_for_commit`.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from kobo_api import delivery
from kobo_api.clients import GitHubClient, KubectlClient, image_tag

# 1-40 chars: a lowercase letter, then lowercase/digit/hyphen, ".md" — no path
# separators, no leading dot/digit/hyphen, nothing that could escape
# bundle/knowledge/ or collide with a non-knowledge file.
KNOWLEDGE_FILENAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}\.md$")

KNOWLEDGE_MAX_BYTES = 65536


class KnowledgeError(Exception):
    """A knowledge filename failed validation, a save's content was empty or
    too large, or a delete named a file that isn't there."""


@dataclass
class KnowledgeFile:
    name: str
    content: str


def _validate_filename(filename: str) -> None:
    if not KNOWLEDGE_FILENAME_RE.match(filename):
        raise KnowledgeError(f"invalid knowledge filename: {filename!r}")


def _validate_content(content: str) -> None:
    size = len(content.encode("utf-8"))
    if size == 0:
        raise KnowledgeError("knowledge file content must not be empty")
    if size > KNOWLEDGE_MAX_BYTES:
        raise KnowledgeError(
            f"knowledge file content is {size} bytes, exceeds the "
            f"{KNOWLEDGE_MAX_BYTES}-byte limit"
        )


def read_knowledge(github: GitHubClient, repo: str) -> list[KnowledgeFile]:
    """The repo's current `bundle/knowledge/*.md` files, sorted by name.

    A repo with no `bundle/knowledge/` directory at all (an agent that
    predates any seed-memory file, or one that's had every file deleted)
    yields `[]` rather than an error — an empty knowledge base is a valid
    state, not a failure.
    """
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(repo, workdir)
        knowledge_dir = workdir / "bundle" / "knowledge"
        if not knowledge_dir.is_dir():
            return []
        return [
            KnowledgeFile(name=path.name, content=path.read_text(encoding="utf-8"))
            for path in sorted(knowledge_dir.glob("*.md"))
        ]


def save_knowledge_file(
    github: GitHubClient,
    repo: str,
    filename: str,
    content: str,
    *,
    archive_only: bool = False,
) -> str:
    """Write (or overwrite) `bundle/knowledge/<filename>` and push it.

    Returns the pushed commit's local sha (`github.local_head`) — a fresh
    clone's own HEAD right after the push, not a GitHub API read that could
    lag behind it. If `content` is byte-for-byte identical to what's already
    there, `commit_push` reports False (nothing to commit) but this still
    returns `local_head`: the caller's subsequent delivery check then reports
    "delivered" immediately, which is the truthful answer — the live image
    already matches, because nothing changed.

    `archive_only` (Critical 2 fix, the rebuild-per-memory-save cost):
    mirrors `kobo_api.intake.upload_intake_file`'s `archive_only` exactly,
    same commit-message marker. `kobo_api.main._archive_knowledge_save`
    calls this ONLY after the fast-memory-lane relay has already reported
    the write "completed" -- the runtime has already stored the new content
    live, so this commit is purely a repo-side mirror of it. Every agent
    repo's generated CI runs build-and-deploy on every push, and the
    Deployment uses the Recreate strategy, which hard-rotates the pod -- a
    rebuild bought for content the pod already has is pure downtime, not a
    real delivery. `archive_only=True` marks the commit `[skip ci]`, which
    GitHub Actions honors natively on push events. The default `False`
    leaves every pre-existing caller unaffected, in particular the
    degraded synchronous lane in `kobo_api.main.save_knowledge` (no relay
    ever reached the agent, so this commit itself IS the delivery -- it
    must keep CI).
    """
    _validate_filename(filename)
    _validate_content(content)
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(repo, workdir)
        knowledge_dir = workdir / "bundle" / "knowledge"
        knowledge_dir.mkdir(parents=True, exist_ok=True)
        (knowledge_dir / filename).write_text(content, encoding="utf-8")
        if archive_only:
            message = f"kobo: archive knowledge/{filename} [skip ci]"
        else:
            message = f"kobo: update knowledge/{filename}"
        delivery.push(github, workdir, message, restart=not archive_only)
        return github.local_head(workdir)


def delete_knowledge_file(
    github: GitHubClient, repo: str, filename: str, *, archive_only: bool = False
) -> str:
    """Remove `bundle/knowledge/<filename>` and push the removal.

    Raises `KnowledgeError` if the file isn't there to delete — a stale UI
    state (someone else already deleted it) is a real error here, unlike
    read_knowledge's empty-directory case, since there's no removal to push.

    `archive_only`: same Critical 2 fix as `save_knowledge_file`, delete
    side. `kobo_api.main._archive_knowledge_delete` calls this ONLY after
    the fast-memory-lane relay has already reported the delete
    "completed" -- the runtime has already retired the entry live, so this
    commit purely mirrors that removal in the repo and must not trigger a
    rebuild. The default `False` leaves the degraded synchronous lane (the
    delete itself IS the delivery) unaffected.
    """
    _validate_filename(filename)
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(repo, workdir)
        target = workdir / "bundle" / "knowledge" / filename
        if not target.is_file():
            raise KnowledgeError(f"knowledge file not found: {filename}")
        target.unlink()
        if archive_only:
            message = f"kobo: archive knowledge/{filename} [skip ci]"
        else:
            message = f"kobo: remove knowledge/{filename}"
        delivery.push(github, workdir, message, restart=not archive_only)
        return github.local_head(workdir)


def delivery_state(
    github: GitHubClient,
    kubectl: KubectlClient,
    *,
    repo: str,
    namespace: str,
    deploy: str,
    sha: str,
) -> dict:
    """Whether the commit `sha` (as returned by save/delete above) has made
    it to the live Deployment yet — a stateless poll target for the UI, with
    no kobo-side record of "in-flight saves" to keep in sync.

    runs = github.runs_for_commit(repo, sha), newest first (same convention
    BuildStep relies on, kobo_api.steps.BuildStep):
      - no runs, with bundles as artifacts: a save that cannot change the
        image skips the build (`kobo_api.delivery.push`, 2026-11-20), and the
        room's restart names the commit it delivered on the pod template
        (`delivery.named_commit`). Named this commit (`sha` may be a prefix),
        or the repository's head (one branch, so the head carries this one):
        "rolling" while that restart runs, then "delivered". Anything else
        falls through below. One read of the Deployment answers both.
      - no runs yet: "building" — registration lag, the same grace BuildStep
        extends CI before treating an empty list as anything but "not yet".
      - newest run not completed (queued or in_progress): "building".
      - newest completed run's conclusion != "success": first check whether
        main has moved past `sha` (github.head_sha). Gojira finding #20
        (tenishai/gojira#41): a push that changes `gojira.yaml` is
        delivered by gojira's setup PR, which auto-merges seconds later
        carrying this commit — and the platform's generated CI now fails
        the contract commit's OWN run fast ("Verify manifests match
        gojira.yaml") instead of deploying a stale manifest set. GitHub's
        pending-queue rule can likewise "cancel" this run outright behind
        the setup PR's. Nothing else pushes to these repos (BuildStep's
        invariant), so an advanced head means exactly that: follow it and
        report the head's delivery instead. Head still == sha: "failed",
        with a detail pointing at the one remedy available here (no
        automatic rerun, unlike BuildStep — a save already means an
        operator is present): an identical re-save no-ops in commit_push
        and reports the same failed sha again, so the detail steers toward
        actually changing the content, not just saving again. (The
        contract-changing callers — WireStep, wire_slack — wait for gojira
        to be active before returning their sha, so the head has already
        advanced by the UI's first poll; the UI stops polling on "failed".)
      - success: compare kubectl.deploy_image(namespace, deploy)'s tag
        (via `image_tag`, kobo_api.clients — handles both a plain
        `repo:tag` and a digest-pinned `repo:tag@sha256:...` apply, the
        platform's generated CI moved to the latter) against sha[:12] —
        generated CI tags the image it builds with exactly that prefix,
        and applies the manifests in that same run, so a tag match is
        proof this exact commit is live. Match -> "delivered"; anything
        else -> "rolling" (the successful run completed, but the newer
        image hasn't rolled out yet). A tagless digest-only image
        (`repo@sha256:...`, `image_tag` -> None) can't be matched to a
        sha at all, so it also reads "rolling" — an honest pending state,
        not a crash. A tag match with the Deployment still mid-rollout
        (`rollout_in_progress`: the new pod not yet available) is also
        "rolling": delivered means the agent is back up on this commit.
    """
    runs = github.runs_for_commit(repo, sha)
    if not runs:
        if delivery.configured():
            # A commit that cannot change the image skips the build (`delivery.push`):
            # the room's restart delivers it and names it on the pod template. Named,
            # or a later head named (one branch, so it carries this one), and the
            # restart is over: delivered.
            try:
                dep = kubectl.deployment_status_json(namespace, deploy)
            except Exception:  # noqa: BLE001 -- no claim about what was not seen
                dep = None
            named = delivery.named_commit(dep)
            if named is not None and (named.startswith(sha) or named == github.head_sha(repo)):
                if _rollout_unfinished(dep):
                    return {"state": "rolling", "detail": None}
                return {"state": "delivered", "detail": None}
        return {"state": "building", "detail": None}

    newest = runs[0]
    if newest.get("status") != "completed":
        return {"state": "building", "detail": None}

    if newest.get("conclusion") != "success":
        head = github.head_sha(repo)
        if head != sha:
            # Superseded by gojira's setup PR (see docstring) — the head is
            # the commit that actually deploys this change. One hop only:
            # the recursive call's head == its sha.
            return delivery_state(
                github, kubectl, repo=repo, namespace=namespace, deploy=deploy, sha=head
            )
        return {
            "state": "failed",
            "detail": "the update didn't go through — change something and save to try again",
        }

    image = kubectl.deploy_image(namespace, deploy)
    tag = image_tag(image)
    # None means a tagless digest-only image (`repo@sha256:...`) — there's no
    # tag to compare against sha[:12] at all, so it can't be proven delivered;
    # "rolling" is the honest pending state here, not a crash.
    if tag is not None and tag == sha[:12]:
        # The manifest carries this commit, but with one replica and a
        # Recreate strategy that is the moment the old pod is gone and the new
        # one is still starting. "Delivered" means the new pod is available;
        # until then the change is rolling, and the agent header keeps saying
        # Updating instead of Down (kobo#268, seen live 2026-10-30).
        if rollout_in_progress(kubectl, namespace=namespace, deploy=deploy):
            return {"state": "rolling", "detail": None}
        return {"state": "delivered", "detail": None}
    return {"state": "rolling", "detail": None}


def rollout_in_progress(kubectl: KubectlClient, *, namespace: str, deploy: str) -> bool:
    """True when the agent's Deployment is genuinely mid-rollout: the
    controller hasn't observed the latest spec yet, or the updated or
    available replica count is short of spec. Conservative on purpose --
    any probe failure reads as False, so the chat route's delivery-aware
    copy can never mask a real switchboard failure behind a friendlier
    message."""
    try:
        dep = kubectl.deployment_status_json(namespace, deploy)
    except Exception:  # noqa: BLE001
        return False
    return _rollout_unfinished(dep)


def _rollout_unfinished(dep: dict | None) -> bool:
    """`rollout_in_progress` over a Deployment already read; False for none."""
    if not dep:
        return False
    status = dep.get("status") or {}
    spec = dep.get("spec") or {}
    generation = (dep.get("metadata") or {}).get("generation", 0)
    if status.get("observedGeneration", 0) < generation:
        return True
    replicas = spec.get("replicas", 1)
    return (
        status.get("updatedReplicas", 0) < replicas
        or status.get("availableReplicas", 0) < replicas
    )
