"""The bundle publisher's two rules (issue #172, step 1): publish only when
`bundle/` changed, keyed on its git tree id; and publish never restarts, it
hands back what changed so the caller can decide."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from kobo_api.bundle_publish import (
    POINTER_TAG,
    OrasCliPublisher,
    PublishError,
    bundle_changed_paths,
    bundle_reference,
    bundle_tree_id,
    publish_if_changed,
)

REPOSITORY = "us-east4-docker.pkg.dev/example/agents"


class FakePublisher:
    def __init__(self, *, fail: str | None = None):
        self.pushes: list[tuple[Path, str]] = []
        self.tags: list[tuple[str, str]] = []
        self._fail = fail

    def push(self, directory: Path, reference: str) -> str:
        if self._fail:
            raise PublishError(self._fail)
        self.pushes.append((directory, reference))
        return "sha256:" + "ab" * 32

    def tag(self, reference: str, tag: str) -> None:
        self.tags.append((reference, tag))


def _git(workdir: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=workdir, capture_output=True, text=True, check=True
    ).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    workdir = tmp_path / "agent-repo"
    workdir.mkdir()
    _git(workdir, "init", "-q", "-b", "main")
    _git(workdir, "config", "user.email", "t@example.com")
    _git(workdir, "config", "user.name", "t")
    (workdir / "bundle").mkdir()
    (workdir / "bundle" / "persona.md").write_text("# sam\n")
    (workdir / "gojira.yaml").write_text("name: sam\n")
    _git(workdir, "add", "-A")
    _git(workdir, "commit", "-q", "-m", "compose")
    return workdir


def _commit(workdir: Path, path: str, text: str, message: str) -> str:
    target = workdir / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    _git(workdir, "add", path)
    _git(workdir, "commit", "-q", "-m", message)
    return _git(workdir, "rev-parse", "HEAD")


def test_first_publish_pushes_the_bundle_and_moves_the_pointer(tmp_path):
    workdir = _repo(tmp_path)
    sha = _git(workdir, "rev-parse", "HEAD")
    publisher = FakePublisher()

    result = publish_if_changed(
        publisher, workdir=workdir, agent_name="sam", repository=REPOSITORY,
        head_sha=sha, last_tree_id=None,
    )

    assert result is not None
    assert result.reference == f"{REPOSITORY}/sam-bundle:{sha[:12]}"
    assert result.pointer == f"{REPOSITORY}/sam-bundle:{POINTER_TAG}"
    assert result.digest.startswith("sha256:")
    assert result.tree_id == bundle_tree_id(workdir)
    assert publisher.pushes == [(workdir / "bundle", result.reference)]
    assert publisher.tags == [(result.reference, POINTER_TAG)]


def test_a_commit_outside_the_bundle_publishes_nothing(tmp_path):
    """A contract-only commit has a new sha and an identical bundle tree:
    no push, no pointer move, so no restart for nothing."""
    workdir = _repo(tmp_path)
    before = bundle_tree_id(workdir)
    sha = _commit(workdir, "gojira.yaml", "name: sam\nenv: {A: b}\n", "env")
    publisher = FakePublisher()

    result = publish_if_changed(
        publisher, workdir=workdir, agent_name="sam", repository=REPOSITORY,
        head_sha=sha, last_tree_id=before,
    )

    assert result is None
    assert publisher.pushes == [] and publisher.tags == []
    assert bundle_changed_paths(workdir) == []


def test_a_bundle_change_publishes_and_names_what_changed(tmp_path):
    workdir = _repo(tmp_path)
    before = bundle_tree_id(workdir)
    sha = _commit(workdir, "bundle/intake/chat-note.md", "taught\n", "kobo: archive intake [skip ci]")
    publisher = FakePublisher()

    result = publish_if_changed(
        publisher, workdir=workdir, agent_name="sam", repository=REPOSITORY,
        head_sha=sha, last_tree_id=before,
    )

    assert result is not None and result.tree_id != before
    # The caller decides from this whether a restart is due: an intake note
    # the relay already delivered is not one.
    assert bundle_changed_paths(workdir) == ["intake/chat-note.md"]


def test_identical_bytes_in_a_new_commit_publish_nothing(tmp_path):
    """Same files, new commit (a revert and re-apply, say): the tree id is
    the key, so nothing is published."""
    workdir = _repo(tmp_path)
    before = bundle_tree_id(workdir)
    _commit(workdir, "bundle/persona.md", "# sam v2\n", "edit")
    sha = _commit(workdir, "bundle/persona.md", "# sam\n", "revert")
    publisher = FakePublisher()

    result = publish_if_changed(
        publisher, workdir=workdir, agent_name="sam", repository=REPOSITORY,
        head_sha=sha, last_tree_id=before,
    )

    assert result is None
    assert bundle_tree_id(workdir) == before


def test_no_bundle_directory_publishes_nothing(tmp_path):
    workdir = tmp_path / "connected-repo"
    workdir.mkdir()
    _git(workdir, "init", "-q", "-b", "main")
    _git(workdir, "config", "user.email", "t@example.com")
    _git(workdir, "config", "user.name", "t")
    (workdir / "Dockerfile").write_text("FROM scratch\n")
    _git(workdir, "add", "-A")
    _git(workdir, "commit", "-q", "-m", "own image")
    publisher = FakePublisher()

    assert bundle_tree_id(workdir) is None
    result = publish_if_changed(
        publisher, workdir=workdir, agent_name="own", repository=REPOSITORY,
        head_sha=_git(workdir, "rev-parse", "HEAD"), last_tree_id=None,
    )
    assert result is None and publisher.pushes == []


def test_a_refused_push_raises_and_moves_no_pointer(tmp_path):
    workdir = _repo(tmp_path)
    publisher = FakePublisher(fail="denied: artifactregistry.repositories.uploadArtifacts")

    with pytest.raises(PublishError, match="uploadArtifacts"):
        publish_if_changed(
            publisher, workdir=workdir, agent_name="sam", repository=REPOSITORY,
            head_sha=_git(workdir, "rev-parse", "HEAD"), last_tree_id=None,
        )
    assert publisher.tags == []


def test_bundle_reference_shape():
    assert bundle_reference(REPOSITORY + "/", "sam", "abc") == f"{REPOSITORY}/sam-bundle:abc"


def test_oras_cli_missing_is_a_publish_error(tmp_path):
    publisher = OrasCliPublisher(oras=str(tmp_path / "no-such-oras"))
    with pytest.raises(PublishError, match="not installed"):
        publisher.push(tmp_path, f"{REPOSITORY}/sam-bundle:x")


def test_oras_cli_push_reads_the_digest_and_runs_from_the_bundle(tmp_path):
    """A fake `oras` on disk: records its argv and cwd, prints a digest."""
    script = tmp_path / "oras"
    script.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$PWD\" \"$@\" >> {tmp_path / 'argv'}\n"
        f"printf -- '--\\n' >> {tmp_path / 'argv'}\n"
        "if [ \"$1\" = push ]; then printf 'sha256:%064d' 7; fi\n"
    )
    script.chmod(0o755)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    publisher = OrasCliPublisher(oras=str(script))

    digest = publisher.push(bundle, f"{REPOSITORY}/sam-bundle:abc")
    publisher.tag(f"{REPOSITORY}/sam-bundle:abc", POINTER_TAG)

    assert digest == "sha256:" + "0" * 63 + "7"
    calls = [c.splitlines() for c in (tmp_path / "argv").read_text().split("--\n") if c.strip()]
    push, tag = calls
    # The push ran from inside the bundle directory, with `.` as the source,
    # so the artifact's paths are relative to the bundle root.
    assert Path(push[0]).resolve() == bundle.resolve()
    assert push[1] == "push" and push[-2:] == [f"{REPOSITORY}/sam-bundle:abc", "."]
    assert tag[1:] == ["tag", f"{REPOSITORY}/sam-bundle:abc", POINTER_TAG]
