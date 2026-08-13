"""Tests for kobo_api.skills: read/save/delete an agent bundle's local
`bundle/skills/<id>/SKILL.md` files, plus add_catalog_skill's pull from the
org skills bucket catalog — all against the FakeGitHub/FakeStorage fakes (see
tests/fakes.py), no real git/gcs involved. Mirrors test_knowledge.py's idioms.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kobo_api.config import Config
from kobo_api.skills import (
    SKILL_MAX_BYTES,
    SkillsError,
    add_catalog_skill,
    read_local_skill,
    remove_skill,
    save_local_skill,
)
from tests.fakes import FAKE_LOCAL_HEAD_DEFAULT, FakeGitHub, FakeStorage

SKILLS_BUCKET = "skills-bucket"
REPO = "acme/agent-x"

SAMPLE_CONTENT = "---\nname: survey-field\ndescription: Plays a survey game.\n---\nBody.\n"


def _config() -> Config:
    return Config(skills_bucket=SKILLS_BUCKET)


def _seed_catalog_skill(
    storage: FakeStorage, team: str, skill_id: str, *, source_sha: str = "abc123"
) -> None:
    storage.write_bytes(
        SKILLS_BUCKET,
        f"{team}/{skill_id}/SKILL.md",
        f"---\nname: {skill_id}\ndescription: A sample skill.\n---\nBody.\n".encode(),
        metadata={"source-sha": source_sha},
        content_type="text/markdown",
    )


class _CapturingGitHub(FakeGitHub):
    """A FakeGitHub that snapshots bundle/skills/**/*.md content the moment
    commit_push is called — the only window in which the caller's cloned
    workdir still exists, since skills.py discards its TemporaryDirectory as
    part of returning. Mirrors test_knowledge.py's _CapturingGitHub."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.captured_skills: dict[str, str] = {}

    def commit_push(self, workdir: Path, message: str) -> bool:
        skills_dir = workdir / "bundle" / "skills"
        if skills_dir.is_dir():
            self.captured_skills = {
                str(path.relative_to(skills_dir)): path.read_text()
                for path in skills_dir.rglob("*")
                if path.is_file()
            }
        return super().commit_push(workdir, message)


class _SkillSeedingGitHub(_CapturingGitHub):
    """A github fake whose clone() also materializes an existing
    bundle/skills/<id>/SKILL.md tree — the local-repo state the
    remove/read/overwrite tests need already in place before their operation
    runs. Not something the shared FakeGitHub itself models (no test outside
    this module needs a pre-existing local skill), so it's built here the
    same way test_knowledge.py's _CapturingGitHub layers test-local behavior
    on top of the shared fake, without modifying tests/fakes.py."""

    def __init__(self, *, local_skills: dict[str, str] | None = None, **kwargs):
        super().__init__(**kwargs)
        self._local_skills = local_skills or {}

    def clone(self, repo: str, dest: Path) -> None:
        super().clone(repo, dest)
        for rel_path, content in self._local_skills.items():
            target = dest / "bundle" / "skills" / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)


# --- add_catalog_skill ------------------------------------------------------


def test_add_catalog_skill_materializes_and_returns_current_sha():
    storage = FakeStorage()
    _seed_catalog_skill(storage, "shared", "survey-field", source_sha="abc123")
    github = _CapturingGitHub()

    sha, source_sha = add_catalog_skill(
        github, storage, _config(), REPO, "survey-field", "shared"
    )

    assert "survey-field/SKILL.md" in github.captured_skills
    assert github.pushes[-1][1] == "kobo: add skill survey-field"
    assert sha == FAKE_LOCAL_HEAD_DEFAULT
    assert source_sha == "abc123"


def test_add_catalog_skill_gone_from_catalog_raises():
    storage = FakeStorage(buckets={SKILLS_BUCKET: {}})
    github = FakeGitHub()

    with pytest.raises(SkillsError, match="skill no longer in the catalog: shared/gone"):
        add_catalog_skill(github, storage, _config(), REPO, "gone", "shared")

    assert github.pushes == []


def test_add_catalog_skill_rejects_bad_id():
    storage = FakeStorage()
    github = FakeGitHub()

    with pytest.raises(SkillsError, match="invalid skill id"):
        add_catalog_skill(github, storage, _config(), REPO, "../escape", "shared")

    assert github.pushes == []
    assert github.cloned == []


# --- remove_skill -------------------------------------------------------------


def test_remove_skill_deletes_directory():
    github = _SkillSeedingGitHub(local_skills={"survey-field/SKILL.md": SAMPLE_CONTENT})

    sha = remove_skill(github, REPO, "survey-field")

    assert github.captured_skills == {}
    assert github.pushes[-1][1] == "kobo: remove skill survey-field"
    assert sha == FAKE_LOCAL_HEAD_DEFAULT


def test_remove_skill_absent_raises_not_found():
    github = _SkillSeedingGitHub()

    with pytest.raises(SkillsError, match="skill not found: ghost"):
        remove_skill(github, REPO, "ghost")

    assert github.pushes == []


def test_remove_skill_rejects_bad_id():
    github = FakeGitHub()

    with pytest.raises(SkillsError, match="invalid skill id"):
        remove_skill(github, REPO, "../escape")

    assert github.cloned == []


# --- read_local_skill ---------------------------------------------------------


def test_read_local_skill_roundtrip_and_not_found():
    github = _SkillSeedingGitHub(local_skills={"survey-field/SKILL.md": SAMPLE_CONTENT})

    assert read_local_skill(github, REPO, "survey-field") == SAMPLE_CONTENT

    with pytest.raises(SkillsError, match="skill not found: ghost"):
        read_local_skill(github, REPO, "ghost")


def test_read_local_skill_rejects_bad_id():
    github = FakeGitHub()

    with pytest.raises(SkillsError, match="invalid skill id"):
        read_local_skill(github, REPO, "../escape")

    assert github.cloned == []


# --- save_local_skill ----------------------------------------------------------


@pytest.mark.parametrize(
    "skill_id",
    [
        "Survey-Field",  # uppercase first char
        "-survey",  # can't start with a hyphen
        "1survey",  # can't start with a digit
        "survey field",  # space
        "../escape",  # path traversal
        "",
        "a" * 41,  # over the 40-char cap
    ],
)
def test_save_local_skill_rejects_bad_id(skill_id):
    github = _CapturingGitHub()
    with pytest.raises(SkillsError, match="invalid skill id"):
        save_local_skill(github, REPO, skill_id, SAMPLE_CONTENT)
    assert github.pushes == []


def test_save_local_skill_rejects_empty_content():
    github = _CapturingGitHub()
    with pytest.raises(SkillsError, match="skill file content"):
        save_local_skill(github, REPO, "survey-field", "")
    assert github.pushes == []


def test_save_local_skill_rejects_oversize_content():
    github = _CapturingGitHub()
    oversize = "---\nname: x\ndescription: d\n---\n" + "a" * SKILL_MAX_BYTES
    with pytest.raises(SkillsError, match="skill file content"):
        save_local_skill(github, REPO, "survey-field", oversize)
    assert github.pushes == []


def test_save_local_skill_rejects_missing_frontmatter():
    github = _CapturingGitHub()
    with pytest.raises(SkillsError, match="skill frontmatter"):
        save_local_skill(github, REPO, "survey-field", "No frontmatter here.\n")
    assert github.pushes == []


def test_save_local_skill_rejects_frontmatter_missing_description():
    github = _CapturingGitHub()
    content = "---\nname: x\n---\nBody\n"
    with pytest.raises(SkillsError, match="skill frontmatter needs a description"):
        save_local_skill(github, REPO, "survey-field", content)
    assert github.pushes == []


def test_save_local_skill_rejects_frontmatter_missing_name():
    github = _CapturingGitHub()
    content = "---\ndescription: d\n---\nBody\n"
    with pytest.raises(SkillsError, match="skill frontmatter needs a name"):
        save_local_skill(github, REPO, "survey-field", content)
    assert github.pushes == []


def test_save_local_skill_happy_path_writes_commits_and_returns_head():
    github = _CapturingGitHub()

    sha = save_local_skill(github, REPO, "survey-field", SAMPLE_CONTENT)

    assert github.captured_skills == {"survey-field/SKILL.md": SAMPLE_CONTENT}
    assert github.pushes[-1][1] == "kobo: update skill survey-field"
    assert sha == FAKE_LOCAL_HEAD_DEFAULT


def test_save_local_skill_overwrites_existing_file():
    github = _SkillSeedingGitHub(local_skills={"survey-field/SKILL.md": "old content"})
    replacement = "---\nname: survey-field\ndescription: Updated.\n---\nNew body.\n"

    save_local_skill(github, REPO, "survey-field", replacement)

    assert github.captured_skills == {"survey-field/SKILL.md": replacement}


def test_save_local_skill_identical_content_still_returns_head():
    # commit_push -> False (identical content, nothing to commit) still
    # returns local_head — same deliberate truthful-no-op as
    # kobo_api.knowledge.save_knowledge_file.
    github = FakeGitHub(commit_push_results=[False])
    sha = save_local_skill(github, REPO, "survey-field", SAMPLE_CONTENT)
    assert sha == FAKE_LOCAL_HEAD_DEFAULT


def test_save_local_skill_rejects_trailing_newline_in_id():
    github = _CapturingGitHub()
    with pytest.raises(SkillsError, match="invalid skill id"):
        save_local_skill(github, REPO, "survey-field\n", SAMPLE_CONTENT)
    assert github.pushes == []


def test_remove_skill_rejects_trailing_newline_in_id():
    github = FakeGitHub()
    with pytest.raises(SkillsError, match="invalid skill id"):
        remove_skill(github, REPO, "survey-field\n")
    assert github.cloned == []


def test_save_local_skill_accepts_max_length_id():
    github = _CapturingGitHub()
    max_id = "a" + "b" * 39  # 'a' + 39 more chars = 40 total
    sha = save_local_skill(github, REPO, max_id, SAMPLE_CONTENT)
    assert github.captured_skills == {f"{max_id}/SKILL.md": SAMPLE_CONTENT}
    assert sha == FAKE_LOCAL_HEAD_DEFAULT


def test_save_local_skill_accepts_max_size_content():
    github = _CapturingGitHub()
    # Create content that is exactly 65536 bytes
    header = "---\nname: x\ndescription: d\n---\n"
    remaining = SKILL_MAX_BYTES - len(header.encode("utf-8"))
    content = header + "x" * remaining
    assert len(content.encode("utf-8")) == SKILL_MAX_BYTES
    sha = save_local_skill(github, REPO, "survey-field", content)
    assert github.captured_skills == {"survey-field/SKILL.md": content}
    assert sha == FAKE_LOCAL_HEAD_DEFAULT
