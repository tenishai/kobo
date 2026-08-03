"""Knowledge module (P2b): read, edit, and delete an agent's seed-memory files
(`bundle/knowledge/*.md` in the agent's repo — the bundle of record), plus a
stateless check for whether a saved edit has actually reached the live
Deployment yet.

Every operation here clones the repo fresh into a `tempfile.TemporaryDirectory`
and discards it before returning — there is no local working copy kept around
between calls, mirroring how the rest of pnyx_api treats a repo clone as a
disposable means to an end (see `pnyx_api.steps`), never state of its own.

`delivery_state` needs no local clone at all: it's a pure read against two
already-authoritative sources (the commit's CI run, the live Deployment's
image) with no pnyx-side state to keep in sync — see its docstring for the
exact branch logic, which mirrors `pnyx_api.steps.BuildStep`'s
newest-run-first convention over `runs_for_commit`.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pnyx_api.clients import GitHubClient, KubectlClient

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


def save_knowledge_file(github: GitHubClient, repo: str, filename: str, content: str) -> str:
    """Write (or overwrite) `bundle/knowledge/<filename>` and push it.

    Returns the pushed commit's local sha (`github.local_head`) — a fresh
    clone's own HEAD right after the push, not a GitHub API read that could
    lag behind it. If `content` is byte-for-byte identical to what's already
    there, `commit_push` reports False (nothing to commit) but this still
    returns `local_head`: the caller's subsequent delivery check then reports
    "delivered" immediately, which is the truthful answer — the live image
    already matches, because nothing changed.
    """
    _validate_filename(filename)
    _validate_content(content)
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(repo, workdir)
        knowledge_dir = workdir / "bundle" / "knowledge"
        knowledge_dir.mkdir(parents=True, exist_ok=True)
        (knowledge_dir / filename).write_text(content, encoding="utf-8")
        github.commit_push(workdir, f"pnyx: update knowledge/{filename}")
        return github.local_head(workdir)


def delete_knowledge_file(github: GitHubClient, repo: str, filename: str) -> str:
    """Remove `bundle/knowledge/<filename>` and push the removal.

    Raises `KnowledgeError` if the file isn't there to delete — a stale UI
    state (someone else already deleted it) is a real error here, unlike
    read_knowledge's empty-directory case, since there's no removal to push.
    """
    _validate_filename(filename)
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(repo, workdir)
        target = workdir / "bundle" / "knowledge" / filename
        if not target.is_file():
            raise KnowledgeError(f"knowledge file not found: {filename}")
        target.unlink()
        github.commit_push(workdir, f"pnyx: remove knowledge/{filename}")
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
    no pnyx-side record of "in-flight saves" to keep in sync.

    runs = github.runs_for_commit(repo, sha), newest first (same convention
    BuildStep relies on, pnyx_api.steps.BuildStep):
      - no runs yet: "building" — registration lag, the same grace BuildStep
        extends CI before treating an empty list as anything but "not yet".
      - newest run not completed (queued or in_progress): "building".
      - newest completed run's conclusion != "success": "failed", with a
        detail pointing at the one remedy available here (no automatic
        rerun, unlike BuildStep — a save already means an operator is
        present): an identical re-save no-ops in commit_push and reports the
        same failed sha again, so the detail steers toward actually changing
        the content, not just saving again.
      - success: compare kubectl.deploy_image(namespace, deploy)'s tag (the
        part after the last ":") against sha[:12] — generated CI tags the
        image it builds with exactly that prefix, and applies the manifests
        in that same run, so a tag match is proof this exact commit is live.
        Match -> "delivered"; anything else -> "rolling" (the successful run
        completed, but the newer image hasn't rolled out yet).
    """
    runs = github.runs_for_commit(repo, sha)
    if not runs:
        return {"state": "building", "detail": None}

    newest = runs[0]
    if newest.get("status") != "completed":
        return {"state": "building", "detail": None}

    if newest.get("conclusion") != "success":
        return {
            "state": "failed",
            "detail": "the update didn't go through — change something and save to try again",
        }

    image = kubectl.deploy_image(namespace, deploy)
    tag = image.rsplit(":", 1)[-1]
    if tag == sha[:12]:
        return {"state": "delivered", "detail": None}
    return {"state": "rolling", "detail": None}
