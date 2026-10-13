"""Intake module (Task 3): upload/list/delete an agent's one-shot memory
files (`bundle/intake/*.md` or `*.txt` in the agent's repo — the bundle of
record). Unlike `bundle/knowledge/*.md` (kobo_api.knowledge, edited freely,
re-read on every provision), an intake file is meant to be ingested into the
agent's memory exactly ONCE, by a runtime seeder that runs on boot (a later
task) — kobo's job here is just the upload/list/delete plumbing, plus
deriving each file's "pending" vs "ingested" status so the room can show
whether the seeder has actually gotten to a given upload yet.

Every mutating operation here clones the repo fresh into a `tempfile.
TemporaryDirectory` and discards it before returning, same discipline as
`kobo_api.knowledge` — no local working copy kept around between calls.

`list_intake` is the one operation here with any real logic: see its
docstring for the exact ancestor-based status derivation, which is pure
boot-ordering inference (no kobo-side state of "has this been ingested yet"
to keep in sync) — if the live pod's commit descends from (or IS) the
commit that last touched a given intake file, the seeder must have already
seen that file on its way up.

Final-review fix (the false-ingested window): that ancestor derivation is
only trustworthy if the pod it's inferring from actually ran an
intake-capable seeder. An agent composed before a runtime bump ships every
image FROM the old runtime; without a re-pin, an upload's own delivery boots
a seeder-less image, yet `list_intake` would still call the file "ingested"
the instant the live commit descends from that push — arming a Remove that
permanently loses content the seeder never saw. `upload_intake_file`'s
`runtime_image` parameter closes that window: see its docstring.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from kobo_api.clients import ClientError, GitHubClient, KubectlClient, image_tag
from kobo_api.composer import repin_commit_note, update_dockerfile_runtime_image

# 1-40 chars: a lowercase letter, then lowercase/digit/hyphen, then ".md" or
# ".txt" — same no-path-separator, no-leading-dot/digit/hyphen shape as
# kobo_api.knowledge.KNOWLEDGE_FILENAME_RE, just admitting a second
# extension (intake files are raw uploads, not always markdown).
INTAKE_FILENAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}\.(md|txt)$")

INTAKE_MAX_BYTES = 262144


class IntakeError(Exception):
    """An intake filename failed validation, an upload's content was empty
    or too large, or a delete named a file that isn't there."""


def _validate_filename(filename: str) -> None:
    # fullmatch, not match: `$` (without MULTILINE) matches just before a
    # single trailing newline as well as at the true end of string, so
    # `.match()` here would let e.g. "notes.md\n" slip past validation —
    # `.fullmatch()` requires the whole string to match with no such
    # allowance (house lesson).
    if not INTAKE_FILENAME_RE.fullmatch(filename):
        raise IntakeError(f"invalid intake filename: {filename!r}")


def _validate_content(content: str) -> None:
    size = len(content.encode("utf-8"))
    if size == 0:
        raise IntakeError("intake file content must not be empty")
    if size > INTAKE_MAX_BYTES:
        raise IntakeError(
            f"intake file content is {size} bytes, exceeds the "
            f"{INTAKE_MAX_BYTES}-byte limit"
        )


def upload_intake_file(
    github: GitHubClient,
    repo: str,
    filename: str,
    content: str,
    *,
    runtime_image: str | None = None,
    archive_only: bool = False,
) -> str:
    """Write (or overwrite) `bundle/intake/<filename>` and push it.

    Returns the pushed commit's local sha (`github.local_head`), same
    no-op-but-still-returns-head semantics as
    `kobo_api.knowledge.save_knowledge_file`: if `content` is byte-for-byte
    identical to what's already there, `commit_push` reports False (nothing
    to commit) but this still returns `local_head` — the truthful answer,
    since nothing changed and whatever was live before still is.

    `runtime_image`, when given (the route always passes the org's current
    `Config.runtime_image`), re-pins the clone's Dockerfile `FROM` line to
    it in this SAME commit — the false-ingested-window fix: an agent
    composed before a runtime bump has its Dockerfile pinned to the OLD
    image, so an intake upload that only wrote the file would deliver a
    seeder-less image on its next boot, yet `list_intake`'s ancestor
    derivation would still call that file "ingested" the moment the live
    pod's commit descends from this push — a Remove armed against content a
    seeder never actually saw. Re-pinning here, in the same push that adds
    the file, means the delivery this upload triggers always carries an
    intake-capable seeder before the derivation can call anything ingested.
    Reuses `kobo_api.composer.update_dockerfile_runtime_image` — the exact
    same idempotent, raise-on-unrecognized-FROM edit `kobo_api.slack_setup.
    wire_slack` already makes for the sibling runtime-bump incident there.
    `None` (the default) skips the edit entirely, unchanged from before this
    parameter existed — every pre-existing caller is unaffected.

    `archive_only` (the rebuild-per-memory fix): every agent repo's
    generated CI runs build-and-deploy on EVERY push, and the Deployment
    uses the Recreate strategy — a bare `Recreate` tears the old pod down
    before the new one comes up, so any push here that triggers CI hard-
    rotates the agent's pod minutes later. That's the right price for a
    push that actually changes what boots (a new intake file for the
    seeder to ingest, or a re-pinned runtime image), but `kobo_api.main.
    _archive_teach_note` calls this function AFTER the note has already
    been relayed straight into the agent's live memory — the commit here is
    purely a repo-side mirror of content the pod already has. Rebuilding
    for it would just be downtime bought for nothing: the memory was
    already stored and is already sha-ledgered in the runtime's own
    records, so the pod that would replace it needs nothing from this push
    to serve the very note it's supposedly delivering.
    `archive_only=True` marks that commit with a trailing `[skip ci]`,
    which GitHub Actions honors natively on push events (no workflow-side
    change needed) — UNLESS a real Dockerfile re-pin rides along in the
    same push. A pin change is read directly off the clone (compare the
    Dockerfile's bytes before and after `update_dockerfile_runtime_image`
    runs — that edit is idempotent by construction, so "before == after"
    means nothing on disk actually changed), not inferred from
    `runtime_image is not None`, because the false-ingested-window fix
    means an already-current repo passes a non-None `runtime_image` on
    every call without ever changing a byte. A pin that DID change is a
    delivery no lane gets to suppress: the old image predates the runtime's
    intake-seeder support, so its rebuild is exactly the "code-shaped
    change" the fast-memory-lane spec still wants CI for, even on a commit
    `archive_only` would otherwise mark skippable. `archive_only=False`
    (the default) never emits `[skip ci]` — every pre-existing caller
    (the plain intake upload route, and the teach route's own
    synchronous archive-lane fallback, whose upload IS the delivery
    because no relay ever reached the agent) is unaffected.
    """
    _validate_filename(filename)
    _validate_content(content)
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(repo, workdir)
        intake_dir = workdir / "bundle" / "intake"
        intake_dir.mkdir(parents=True, exist_ok=True)
        (intake_dir / filename).write_text(content, encoding="utf-8")

        pin_changed = False
        note = ""
        if runtime_image is not None:
            dockerfile_path = workdir / "Dockerfile"
            try:
                before = dockerfile_path.read_text()
            except OSError as exc:
                raise ClientError(f"Dockerfile not found: {dockerfile_path}") from exc
            previous = update_dockerfile_runtime_image(dockerfile_path, runtime_image)
            pin_changed = dockerfile_path.read_text() != before
            note = repin_commit_note(repo, previous, runtime_image)

        if archive_only and not pin_changed:
            message = f"kobo: archive intake/{filename} [skip ci]"
        else:
            message = f"kobo: add intake/{filename}{note}"

        github.commit_push(workdir, message)
        return github.local_head(workdir)


def delete_intake_file(github: GitHubClient, repo: str, filename: str) -> str:
    """Remove `bundle/intake/<filename>` and push the removal.

    This deletes only the SOURCE file the upload created — it says nothing
    about whatever the seeder already wrote into the agent's actual memory
    from it (out of scope for kobo, a runtime concern) — the route's 200
    body carries the plain-language version of that distinction.

    Raises `IntakeError` if the file isn't there to delete, same as
    `kobo_api.knowledge.delete_knowledge_file` — a stale UI state is a real
    error here, since there's no removal to push.
    """
    _validate_filename(filename)
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(repo, workdir)
        target = workdir / "bundle" / "intake" / filename
        if not target.is_file():
            raise IntakeError(f"intake file not found: {filename}")
        target.unlink()
        github.commit_push(workdir, f"kobo: remove intake/{filename}")
        return github.local_head(workdir)


def list_intake(
    github: GitHubClient,
    kubectl: KubectlClient,
    *,
    repo: str,
    namespace: str,
    deploy: str,
) -> list[dict]:
    """The repo's current `bundle/intake/*` files, sorted by name, each
    `{"name", "content_bytes": int, "status": "pending" | "ingested"}`.

    A repo with no `bundle/intake/` directory at all (no upload has ever
    happened) yields `[]`, mirroring `read_knowledge`'s same empty-dir
    convention.

    Status derivation (the one subtle algorithm here — implement exactly,
    do not simplify): clone once; for every intake file, get its last-touch
    commit via `github.commit_shas_for_paths` (one `git log -1 -- <path>`
    per file); read the live Deployment's image via
    `kubectl.deploy_image(namespace, deploy)` and take the tag (via
    `image_tag`, kobo_api.clients — handles both a plain `repo:tag` and a
    digest-pinned `repo:tag@sha256:...` apply, the platform's generated CI
    convention, generated CI's 12-hex commit-prefix convention — same read
    `kobo_api.knowledge.delivery_state` makes); resolve that tag to a full
    commit sha inside THIS clone via `github.rev_parse_prefix` (None if it
    doesn't resolve — an image built from a since-force-pushed sha, a
    malformed/non-hex tag like "latest", or a tagless digest-only image
    that `image_tag` itself already reports as having no tag at all).

    A file is "ingested" iff the tag resolved to a commit AND that commit
    is the file's last-touch commit or a descendant of it
    (`github.is_ancestor(file_commit, live_commit)` — equality counts,
    matching git's own `merge-base --is-ancestor` semantics). Anything else
    — an unresolvable (or absent) tag, or a file whose last-touch commit is
    NOT an ancestor of the live commit — is "pending" (a tagless digest-only
    image can't be matched to a sha at all, so every file reads "pending" in
    that case — an honest state, not a crash). This is pure boot-ordering
    logic: if the live pod's commit already contains this file, the runtime
    seeder (which runs once at boot, ingesting whatever `bundle/intake/`
    held at that point) must have processed it before this pod started
    serving; if the file was added/changed after that commit, the seeder
    hasn't seen it yet.
    """
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(repo, workdir)
        intake_dir = workdir / "bundle" / "intake"
        if not intake_dir.is_dir():
            return []

        paths = sorted(
            (p for p in intake_dir.iterdir() if p.is_file()), key=lambda p: p.name
        )
        if not paths:
            return []

        rel_paths = [f"bundle/intake/{p.name}" for p in paths]
        commit_shas = github.commit_shas_for_paths(workdir, rel_paths)

        image = kubectl.deploy_image(namespace, deploy)
        tag = image_tag(image)
        # None means a tagless digest-only image (`repo@sha256:...`) — there's
        # no tag to resolve at all, so every file below falls through to
        # "pending" (the honest state; rev_parse_prefix is never called with
        # a None prefix).
        live_commit = github.rev_parse_prefix(workdir, tag) if tag is not None else None

        results = []
        for path, rel_path in zip(paths, rel_paths):
            file_commit = commit_shas[rel_path]
            ingested = live_commit is not None and github.is_ancestor(
                workdir, file_commit, live_commit
            )
            results.append(
                {
                    "name": path.name,
                    "content_bytes": path.stat().st_size,
                    "status": "ingested" if ingested else "pending",
                }
            )
        return results
