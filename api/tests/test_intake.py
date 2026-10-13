"""Tests for kobo_api.intake: upload/list/delete an agent bundle's
`bundle/intake/*` one-shot memory files, plus the ancestor-derived
pending/ingested status `list_intake` computes for each — all against the
FakeGitHub/FakeKubectl fakes, no real git/kubectl involved.

Status derivation is the one subtle algorithm here (see kobo_api.intake.
list_intake's docstring): a file is "ingested" iff the live image's tag
resolves to a full commit inside the clone AND the file's last-touch commit
is that commit or an ancestor of it; anything else (unresolvable tag, file
not an ancestor) is "pending". FakeGitHub models the two moving pieces
list_intake needs (commit_shas_for_paths, rev_parse_prefix, is_ancestor) via
dedicated constructor knobs — see tests/fakes.py's FakeGitHub docstring for
their exact shape.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kobo_api.clients import ClientError
from kobo_api.intake import (
    INTAKE_MAX_BYTES,
    IntakeError,
    delete_intake_file,
    list_intake,
    upload_intake_file,
)
from tests.fakes import FAKE_DOCKERFILE_OLD_IMAGE, FAKE_LOCAL_HEAD_DEFAULT, FakeGitHub, FakeKubectl

REPO = "acme/agent-clio"
NAMESPACE = "clio"
DEPLOY = "clio"

# A commit sha distinct from FAKE_LOCAL_HEAD_DEFAULT, for scenarios where a
# file's last-touch commit must NOT equal (and must not be an ancestor of)
# the live commit.
NEWER_SHA = "newnewnewnewnewnewnewnewnewnewnewnewnewn"
OLDER_SHA = "oldoldoldoldoldoldoldoldoldoldoldoldoldo"

# The "current" configured runtime image the runtime-re-pin tests pass —
# deliberately distinct from FakeGitHub's own FAKE_DOCKERFILE_OLD_IMAGE
# default so a test proves the FROM line actually changed, not that the two
# happened to already match.
RUNTIME_IMAGE = "registry.example/agents/daedalus-runtime:new"


class _CapturingGitHub(FakeGitHub):
    """Snapshots bundle/intake/* content and the Dockerfile the moment
    commit_push is called — the only window in which the caller's cloned
    workdir still exists, since intake.py discards its TemporaryDirectory as
    part of returning. Mirrors test_knowledge.py's own _CapturingGitHub."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.captured_intake: dict[str, str] = {}
        self.captured_dockerfile = ""

    def commit_push(self, workdir: Path, message: str) -> bool:
        intake_dir = workdir / "bundle" / "intake"
        if intake_dir.is_dir():
            self.captured_intake = {
                path.name: path.read_text()
                for path in intake_dir.iterdir()
                if path.is_file()
            }
        self.captured_dockerfile = (workdir / "Dockerfile").read_text()
        return super().commit_push(workdir, message)


# --- upload_intake_file -------------------------------------------------------


def test_upload_intake_file_writes_content_commits_and_returns_local_head():
    github = _CapturingGitHub()
    result = upload_intake_file(github, REPO, "notes.md", "new content")

    assert github.captured_intake == {"notes.md": "new content"}
    assert github.pushes[-1][1] == "kobo: add intake/notes.md"
    assert result == FAKE_LOCAL_HEAD_DEFAULT
    assert github.local_head_calls == [github.pushes[-1][0]]


def test_upload_intake_file_accepts_txt_extension():
    github = _CapturingGitHub()
    upload_intake_file(github, REPO, "transcript.txt", "hello")
    assert github.captured_intake == {"transcript.txt": "hello"}


def test_upload_intake_file_overwrites_existing_file():
    github = _CapturingGitHub(intake_files={"notes.md": "old"})
    upload_intake_file(github, REPO, "notes.md", "replaced")
    assert github.captured_intake == {"notes.md": "replaced"}


def test_upload_intake_file_returns_local_head_even_when_nothing_changed():
    # commit_push -> False (identical content, nothing to commit) still
    # returns local_head — same no-op semantics as knowledge saves.
    github = FakeGitHub(commit_push_results=[False])
    result = upload_intake_file(github, REPO, "notes.md", "same")
    assert result == FAKE_LOCAL_HEAD_DEFAULT


@pytest.mark.parametrize(
    "filename",
    [
        "Notes.md",  # uppercase first char
        "notes.pdf",  # wrong extension
        "-notes.md",  # can't start with a hyphen/digit
        "1notes.md",
        "notes md.md",  # space
        "../notes.md",  # path traversal
        "notes",  # no extension at all
        "a" * 41 + ".md",  # over the 40-char cap (1 + 39 more)
        "notes.md\n",  # trailing newline — must use fullmatch, not match
        "",
    ],
)
def test_upload_intake_file_rejects_bad_filename(filename):
    github = FakeGitHub()
    with pytest.raises(IntakeError):
        upload_intake_file(github, REPO, filename, "content")
    assert github.pushes == []


def test_upload_intake_file_rejects_empty_content():
    github = FakeGitHub()
    with pytest.raises(IntakeError):
        upload_intake_file(github, REPO, "notes.md", "")
    assert github.pushes == []


def test_upload_intake_file_rejects_oversize_content():
    github = FakeGitHub()
    oversize = "a" * (INTAKE_MAX_BYTES + 1)
    with pytest.raises(IntakeError):
        upload_intake_file(github, REPO, "notes.md", oversize)
    assert github.pushes == []


def test_upload_intake_file_accepts_content_at_exact_byte_cap():
    github = _CapturingGitHub()
    exact = "a" * INTAKE_MAX_BYTES
    upload_intake_file(github, REPO, "notes.md", exact)
    assert github.captured_intake == {"notes.md": exact}


# --- upload_intake_file: runtime_image re-pin (the false-ingested-window fix) -----
#
# B1: an agent composed before a runtime bump has its Dockerfile pinned to
# the OLD image. Without a re-pin, an intake upload's own delivery boots a
# seeder-less image, yet list_intake's ancestor derivation would still call
# the file "ingested" the moment the live commit descends from this push —
# arming a Remove against content the seeder never actually saw. The route
# always passes the org's current runtime_image; upload_intake_file re-pins
# the Dockerfile FROM line to it, in the SAME commit as the uploaded file.


def test_upload_intake_file_with_runtime_image_rewrites_dockerfile_in_same_commit():
    github = _CapturingGitHub()  # defaults to FAKE_DOCKERFILE_OLD_IMAGE
    assert FAKE_DOCKERFILE_OLD_IMAGE != RUNTIME_IMAGE

    upload_intake_file(
        github, REPO, "notes.md", "new content", runtime_image=RUNTIME_IMAGE
    )

    # Both files landed in the one push this call made.
    assert len(github.pushes) == 1
    assert github.captured_intake == {"notes.md": "new content"}
    assert github.captured_dockerfile == (
        "# Agent image: the shared runtime plus this agent's bundle.\n"
        f"FROM {RUNTIME_IMAGE}\n"
        "COPY bundle/ /bundle\n"
    )


def test_upload_intake_file_runtime_image_omitted_leaves_dockerfile_untouched():
    # None (the default) is a no-op edit entirely — every pre-existing
    # caller of upload_intake_file is unaffected by this parameter's
    # addition.
    github = _CapturingGitHub()
    upload_intake_file(github, REPO, "notes.md", "new content")
    assert github.captured_dockerfile == (
        "# Agent image: the shared runtime plus this agent's bundle.\n"
        f"FROM {FAKE_DOCKERFILE_OLD_IMAGE}\n"
        "COPY bundle/ /bundle\n"
    )


def test_upload_intake_file_identical_content_and_already_pinned_from_is_noop():
    # Content byte-identical to what's already there AND the Dockerfile
    # already names runtime_image: a real git backend would see nothing to
    # commit (FakeGitHub models that outcome via commit_push_results=[False],
    # same as test_upload_intake_file_returns_local_head_even_when_nothing_
    # changed — FakeGitHub doesn't diff content itself). The Dockerfile edit
    # is a no-op byte-for-byte (same idempotent semantics as wire_slack's own
    # re-pin), and upload_intake_file still returns the truthful local_head.
    github = _CapturingGitHub(
        intake_files={"notes.md": "same"},
        dockerfile_image=RUNTIME_IMAGE,
        commit_push_results=[False],
    )

    result = upload_intake_file(
        github, REPO, "notes.md", "same", runtime_image=RUNTIME_IMAGE
    )

    assert github.captured_dockerfile == (
        "# Agent image: the shared runtime plus this agent's bundle.\n"
        f"FROM {RUNTIME_IMAGE}\n"
        "COPY bundle/ /bundle\n"
    )
    assert result == FAKE_LOCAL_HEAD_DEFAULT


# --- upload_intake_file: missing Dockerfile (guard against bare exceptions) -----
#
# When runtime_image is passed, upload_intake_file reads the Dockerfile to
# check if the FROM pin actually changed. A missing Dockerfile raises OSError
# before the ClientError boundary can catch it, so this guard wraps the read
# and converts OSError to ClientError.


def test_upload_intake_file_missing_dockerfile_with_runtime_image_raises_client_error():
    # A minimal fake GitHub that doesn't write a Dockerfile (unlike the default).
    class NoDockerfileFakeGitHub(_CapturingGitHub):
        def clone(self, repo: str, workdir: Path) -> None:
            # Call the parent to write the standard scaffolding...
            super().clone(repo, workdir)
            # ...then delete the Dockerfile so runtime_image read fails.
            (workdir / "Dockerfile").unlink()

    github = NoDockerfileFakeGitHub()
    # Attempting to upload with runtime_image should raise ClientError, not FileNotFoundError.
    with pytest.raises(ClientError):
        upload_intake_file(
            github, REPO, "notes.md", "new content", runtime_image=RUNTIME_IMAGE
        )
    assert github.pushes == []  # No commit made; error occurred during processing.


# --- upload_intake_file: archive_only (skip-ci archive commits) -------------
#
# An intake commit that only archives a note already stored in-process
# (kobo_api.main._archive_teach_note, run as a background task strictly AFTER
# a successful instant-lane relay) must not trigger the agent repo's
# generated CI: the memory is already live in the runtime, so a
# rebuild-and-Recreate-redeploy would be pure downtime for content the pod
# already has. `archive_only=True` marks exactly that commit -- UNLESS a
# real Dockerfile re-pin rides along in the same push, which is a wanted
# delivery no matter which lane asked for the commit.


def test_upload_intake_file_archive_only_with_no_runtime_image_skips_ci():
    github = _CapturingGitHub()
    upload_intake_file(github, REPO, "notes.md", "content", archive_only=True)
    assert github.pushes[-1][1] == "kobo: archive intake/notes.md [skip ci]"


def test_upload_intake_file_archive_only_false_never_emits_skip_ci():
    github = _CapturingGitHub()
    upload_intake_file(github, REPO, "notes.md", "content", archive_only=False)
    assert github.pushes[-1][1] == "kobo: add intake/notes.md"
    assert "[skip ci]" not in github.pushes[-1][1]


def test_upload_intake_file_archive_only_with_dockerfile_already_pinned_skips_ci():
    github = _CapturingGitHub(dockerfile_image=RUNTIME_IMAGE)
    upload_intake_file(
        github,
        REPO,
        "notes.md",
        "content",
        runtime_image=RUNTIME_IMAGE,
        archive_only=True,
    )
    assert github.pushes[-1][1] == "kobo: archive intake/notes.md [skip ci]"


def test_upload_intake_file_archive_only_with_stale_dockerfile_pin_keeps_ci():
    # The pin actually changed in this same push -- that's a wanted
    # delivery, so the commit message stays the normal one even though the
    # caller asked for archive_only.
    github = _CapturingGitHub()  # defaults to FAKE_DOCKERFILE_OLD_IMAGE
    assert FAKE_DOCKERFILE_OLD_IMAGE != RUNTIME_IMAGE
    upload_intake_file(
        github,
        REPO,
        "notes.md",
        "content",
        runtime_image=RUNTIME_IMAGE,
        archive_only=True,
    )
    subject, _, note = github.pushes[-1][1].partition("\n\n")
    assert subject == "kobo: add intake/notes.md"
    # The fake clone's Dockerfile is on an old image, so the commit re-pins
    # it and says so (issue #140).
    assert note.startswith("Also re-pins the Dockerfile FROM from ")


# --- delete_intake_file -----------------------------------------------------


def test_delete_intake_file_removes_and_commits():
    github = _CapturingGitHub(intake_files={"notes.md": "bye", "keep.md": "stay"})
    result = delete_intake_file(github, REPO, "notes.md")

    assert github.captured_intake == {"keep.md": "stay"}
    assert github.pushes[-1][1] == "kobo: remove intake/notes.md"
    assert result == FAKE_LOCAL_HEAD_DEFAULT


def test_delete_intake_file_missing_file_raises_and_does_not_push():
    github = FakeGitHub(intake_files={"keep.md": "stay"})
    with pytest.raises(IntakeError):
        delete_intake_file(github, REPO, "notes.md")
    assert github.pushes == []


def test_delete_intake_file_missing_intake_dir_raises():
    github = FakeGitHub()  # no bundle/intake tree at all
    with pytest.raises(IntakeError):
        delete_intake_file(github, REPO, "notes.md")
    assert github.pushes == []


def test_delete_intake_file_rejects_bad_filename():
    github = FakeGitHub(intake_files={"notes.md": "bye"})
    with pytest.raises(IntakeError):
        delete_intake_file(github, REPO, "../escape.md")
    assert github.pushes == []


# --- list_intake: shape / sorting --------------------------------------------


def test_list_intake_empty_when_no_intake_dir():
    github = FakeGitHub()  # no intake_files -> clone writes no bundle/intake tree
    kubectl = FakeKubectl()
    assert list_intake(github, kubectl, repo=REPO, namespace=NAMESPACE, deploy=DEPLOY) == []


def test_list_intake_sorted_by_name_with_content_bytes():
    github = FakeGitHub(intake_files={"b.md": "second", "a.md": "first!"})
    kubectl = FakeKubectl()
    files = list_intake(github, kubectl, repo=REPO, namespace=NAMESPACE, deploy=DEPLOY)

    assert [f["name"] for f in files] == ["a.md", "b.md"]
    assert files[0]["content_bytes"] == len(b"first!")
    assert files[1]["content_bytes"] == len(b"second")
    for f in files:
        assert f["status"] in {"pending", "ingested"}


# --- list_intake: status derivation (the subtle part) ------------------------


def test_list_intake_defaults_wired_happy_path_all_ingested():
    # Bare FakeGitHub()/FakeKubectl() defaults are deliberately consistent
    # (mirrors test_knowledge.py's delivery_state default-wiring test): the
    # default per-file commit sha, the default deploy image tag, and its
    # default resolution all line up on FAKE_LOCAL_HEAD_DEFAULT, so an
    # unconfigured file reads "ingested" without any knob set.
    github = FakeGitHub(intake_files={"notes.md": "content"})
    kubectl = FakeKubectl()
    files = list_intake(github, kubectl, repo=REPO, namespace=NAMESPACE, deploy=DEPLOY)
    assert files == [{"name": "notes.md", "content_bytes": len(b"content"), "status": "ingested"}]


def test_list_intake_file_commit_equals_live_commit_is_ingested():
    # Equality counts as ancestry (git's own semantics for --is-ancestor).
    github = FakeGitHub(
        intake_files={"notes.md": "content"},
        intake_commit_shas={"bundle/intake/notes.md": OLDER_SHA},
        rev_parse_results={"oldoldoldold": OLDER_SHA},
    )
    kubectl = FakeKubectl(images={(NAMESPACE, DEPLOY): f"gojira-app:{OLDER_SHA[:12]}"})
    files = list_intake(github, kubectl, repo=REPO, namespace=NAMESPACE, deploy=DEPLOY)
    assert files[0]["status"] == "ingested"


def test_list_intake_file_is_genuine_ancestor_of_live_commit_is_ingested():
    github = FakeGitHub(
        intake_files={"notes.md": "content"},
        intake_commit_shas={"bundle/intake/notes.md": OLDER_SHA},
        rev_parse_results={"newnewnewnew": NEWER_SHA},
        ancestor_pairs={(OLDER_SHA, NEWER_SHA)},
    )
    kubectl = FakeKubectl(images={(NAMESPACE, DEPLOY): f"gojira-app:{NEWER_SHA[:12]}"})
    files = list_intake(github, kubectl, repo=REPO, namespace=NAMESPACE, deploy=DEPLOY)
    assert files[0]["status"] == "ingested"


def test_list_intake_file_newer_than_image_is_pending():
    # The file's last-touch commit is NOT an ancestor of (nor equal to) the
    # live commit — the seeder can't have processed it yet.
    github = FakeGitHub(
        intake_files={"notes.md": "content"},
        intake_commit_shas={"bundle/intake/notes.md": NEWER_SHA},
        rev_parse_results={"oldoldoldold": OLDER_SHA},
    )
    kubectl = FakeKubectl(images={(NAMESPACE, DEPLOY): f"gojira-app:{OLDER_SHA[:12]}"})
    files = list_intake(github, kubectl, repo=REPO, namespace=NAMESPACE, deploy=DEPLOY)
    assert files[0]["status"] == "pending"


def test_list_intake_tag_unresolvable_is_pending():
    # An image tag that doesn't resolve to any commit in the clone (e.g. an
    # image built from a since-force-pushed sha) — rev_parse_prefix -> None
    # -> pending, regardless of the file's own commit.
    github = FakeGitHub(
        intake_files={"notes.md": "content"},
        intake_commit_shas={"bundle/intake/notes.md": FAKE_LOCAL_HEAD_DEFAULT},
        rev_parse_results={"deadbeef0000": None},
    )
    kubectl = FakeKubectl(images={(NAMESPACE, DEPLOY): "gojira-app:deadbeef0000"})
    files = list_intake(github, kubectl, repo=REPO, namespace=NAMESPACE, deploy=DEPLOY)
    assert files[0]["status"] == "pending"


def test_list_intake_non_hex_short_tag_is_pending_never_crashes():
    # A malformed/short tag (e.g. ":latest") must never raise — it's simply
    # unresolvable, same as any other tag with no matching commit.
    github = FakeGitHub(intake_files={"notes.md": "content"})
    kubectl = FakeKubectl(images={(NAMESPACE, DEPLOY): "gojira-app:latest"})
    files = list_intake(github, kubectl, repo=REPO, namespace=NAMESPACE, deploy=DEPLOY)
    assert files[0]["status"] == "pending"


def test_list_intake_digest_pinned_image_with_matching_tag_resolves_ingested():
    # Live repro (digest-pinned deploy fix): the platform's regenerated CI
    # now applies Deployments pinned BY DIGEST (`repo:tag@sha256:...`) —
    # this must resolve exactly as
    # test_list_intake_file_commit_equals_live_commit_is_ingested does, not
    # regress to "pending" because the digest hex got mistaken for the tag.
    github = FakeGitHub(
        intake_files={"notes.md": "content"},
        intake_commit_shas={"bundle/intake/notes.md": OLDER_SHA},
        rev_parse_results={"oldoldoldold": OLDER_SHA},
    )
    kubectl = FakeKubectl(
        images={
            (NAMESPACE, DEPLOY): (
                f"gojira-app:{OLDER_SHA[:12]}"
                "@sha256:3c68b1e1a2a3f4b5c6d7e8f900112233445566778899aabbccddeeff0011223"
            )
        }
    )
    files = list_intake(github, kubectl, repo=REPO, namespace=NAMESPACE, deploy=DEPLOY)
    assert files[0]["status"] == "ingested"


def test_list_intake_mixed_statuses_across_multiple_files():
    github = FakeGitHub(
        intake_files={"old.md": "stale", "fresh.md": "brand new"},
        intake_commit_shas={
            "bundle/intake/old.md": OLDER_SHA,
            "bundle/intake/fresh.md": NEWER_SHA,
        },
        rev_parse_results={"oldoldoldold": OLDER_SHA},
    )
    kubectl = FakeKubectl(images={(NAMESPACE, DEPLOY): f"gojira-app:{OLDER_SHA[:12]}"})
    files = list_intake(github, kubectl, repo=REPO, namespace=NAMESPACE, deploy=DEPLOY)

    by_name = {f["name"]: f["status"] for f in files}
    assert by_name == {"old.md": "ingested", "fresh.md": "pending"}


def test_list_intake_clones_only_once():
    github = FakeGitHub(intake_files={"a.md": "1", "b.md": "2"})
    kubectl = FakeKubectl()
    list_intake(github, kubectl, repo=REPO, namespace=NAMESPACE, deploy=DEPLOY)
    assert len(github.cloned) == 1
