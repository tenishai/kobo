"""Unit tests for the skills-publish workflow template (Phase A Task 6):
`api/pnyx_api/templates/skills_publish/publish_skills.py`, the script pnyx's
bootstrap installs into a client org's skills repo so it can run inside that
repo's own GitHub Action.

The templates directory ships as packaged data, not an importable package —
a client repo only ever runs `python .github/scripts/publish_skills.py`, it
never `import`s it — so this test module loads the script the same way,
by path via `importlib.util`, rather than `from pnyx_api.templates...
import publish_skills`. That also keeps pnyx's own test suite free of any
`google-cloud-storage` dependency: the script only imports that SDK lazily,
inside `_real_client()`, and `--check` (discover + validate only) never
calls it at all. Every `sync()` test below exercises a small duck-typed fake
bucket client defined right here (test-local, not `tests/fakes.py` — a
different protocol family than `pnyx_api.clients.StorageClient`) that mirrors
only the `google-cloud-storage` surface the script actually calls.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest
import yaml

_TEMPLATES_DIR = Path(__file__).parent.parent / "pnyx_api" / "templates" / "skills_publish"
_SCRIPT_PATH = _TEMPLATES_DIR / "publish_skills.py"
_YML_PATH = _TEMPLATES_DIR / "publish-skills.yml"


def _load_module():
    spec = importlib.util.spec_from_file_location("publish_skills", _SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # dataclasses' postponed-annotation resolution (this script uses `from
    # __future__ import annotations`) looks the module up in `sys.modules`
    # by name — without registering it there first, `@dataclass` on `Skill`
    # raises AttributeError the moment this module is exec'd.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def ps():
    return _load_module()


# --- repo-layout fixture helper ------------------------------------------------


def _write_skill_md(
    skill_dir: Path,
    *,
    name: str | None,
    description: str | None = None,
    tags: list[str] | None = None,
    body: str = "Do the thing.\n",
) -> None:
    skill_dir.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    if name is not None:
        lines.append(f"name: {name}")
    if description is not None:
        lines.append(f"description: {description}")
    if tags is not None:
        lines.append(f"tags: {json.dumps(tags)}")
    lines.append("---")
    (skill_dir / "SKILL.md").write_text("\n".join(lines) + "\n" + body)


# --- discover() -----------------------------------------------------------------


def test_discover_teams_layout(ps, tmp_path):
    _write_skill_md(
        tmp_path / "teams" / "acme" / "code-review", name="code-review", description="Reviews PRs"
    )

    skills = ps.discover(tmp_path)

    assert len(skills) == 1
    skill = skills[0]
    assert (skill.team, skill.id) == ("acme", "code-review")
    assert skill.name == "code-review"
    assert skill.description == "Reviews PRs"
    assert skill.tags == []
    assert skill.files == [Path("SKILL.md")]


def test_discover_legacy_flat_layout_is_team_shared(ps, tmp_path):
    _write_skill_md(tmp_path / "skills" / "word-games", name="word-games", description="Plays games")

    skills = ps.discover(tmp_path)

    assert len(skills) == 1
    assert (skills[0].team, skills[0].id) == ("shared", "word-games")


def test_discover_both_layouts_coexist(ps, tmp_path):
    _write_skill_md(tmp_path / "teams" / "acme" / "code-review", name="code-review", description="d")
    _write_skill_md(tmp_path / "skills" / "word-games", name="word-games", description="d")

    skills = ps.discover(tmp_path)

    assert {(s.team, s.id) for s in skills} == {
        ("acme", "code-review"),
        ("shared", "word-games"),
    }


def test_discover_duplicate_team_id_raises(ps, tmp_path):
    # teams/shared/word-games and skills/word-games both resolve to the same
    # (team="shared", id="word-games") key — a structural conflict (both
    # would publish to the same bucket prefix) regardless of whether either
    # one's frontmatter is otherwise valid.
    _write_skill_md(tmp_path / "teams" / "shared" / "word-games", name="word-games", description="d")
    _write_skill_md(tmp_path / "skills" / "word-games", name="word-games", description="d")

    with pytest.raises(ps.ValidationError, match="duplicate"):
        ps.discover(tmp_path)


def test_discover_no_skill_dirs_returns_empty(ps, tmp_path):
    assert ps.discover(tmp_path) == []


def test_discover_ignores_dirs_without_skill_md(ps, tmp_path):
    (tmp_path / "teams" / "acme" / "not-a-skill").mkdir(parents=True)
    (tmp_path / "skills" / "also-not-a-skill").mkdir(parents=True)

    assert ps.discover(tmp_path) == []


def test_discover_collects_all_files_relative_to_skill_dir(ps, tmp_path):
    skill_dir = tmp_path / "teams" / "acme" / "code-review"
    _write_skill_md(skill_dir, name="code-review", description="d")
    (skill_dir / "reference.md").write_text("extra reference\n")
    (skill_dir / "scripts").mkdir()
    (skill_dir / "scripts" / "run.sh").write_text("#!/bin/sh\necho hi\n")

    skills = ps.discover(tmp_path)

    assert skills[0].files == [
        Path("SKILL.md"),
        Path("reference.md"),
        Path("scripts/run.sh"),
    ]


def test_discover_propagates_frontmatter_validation_errors(ps, tmp_path):
    _write_skill_md(tmp_path / "teams" / "acme" / "bad-skill", name=None, description="d")

    with pytest.raises(ps.ValidationError, match="name"):
        ps.discover(tmp_path)


# --- validate() -------------------------------------------------------------------


def test_validate_missing_name_raises(ps, tmp_path):
    skill_dir = tmp_path / "s"
    _write_skill_md(skill_dir, name=None, description="no name here")

    with pytest.raises(ps.ValidationError, match="name"):
        ps.validate(skill_dir)


def test_validate_bad_slug_name_raises(ps, tmp_path):
    skill_dir = tmp_path / "s"
    _write_skill_md(skill_dir, name="Not_A_Slug", description="x")

    with pytest.raises(ps.ValidationError, match="name"):
        ps.validate(skill_dir)


def test_validate_name_too_long_raises(ps, tmp_path):
    skill_dir = tmp_path / "s"
    _write_skill_md(skill_dir, name="a" * 41, description="x")

    with pytest.raises(ps.ValidationError, match="name"):
        ps.validate(skill_dir)


def test_validate_description_falls_back_to_first_nonblank_body_line(ps, tmp_path):
    skill_dir = tmp_path / "s"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: code-review\n---\n\n   \nFirst real line.\nSecond line.\n"
    )

    name, description, tags = ps.validate(skill_dir)

    assert name == "code-review"
    assert description == "First real line."
    assert tags == []


def test_validate_missing_description_and_no_body_raises(ps, tmp_path):
    skill_dir = tmp_path / "s"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: code-review\n---\n\n\n   \n")

    with pytest.raises(ps.ValidationError, match="description"):
        ps.validate(skill_dir)


def test_validate_explicit_description_wins_over_body(ps, tmp_path):
    skill_dir = tmp_path / "s"
    _write_skill_md(skill_dir, name="code-review", description="Explicit description", body="Body line.\n")

    _, description, _ = ps.validate(skill_dir)

    assert description == "Explicit description"


def test_validate_tags_default_to_empty_list(ps, tmp_path):
    skill_dir = tmp_path / "s"
    _write_skill_md(skill_dir, name="code-review", description="d")

    _, _, tags = ps.validate(skill_dir)

    assert tags == []


def test_validate_valid_tags_round_trip(ps, tmp_path):
    skill_dir = tmp_path / "s"
    _write_skill_md(skill_dir, name="code-review", description="d", tags=["git", "code-quality"])

    _, _, tags = ps.validate(skill_dir)

    assert tags == ["git", "code-quality"]


def test_validate_invalid_tag_raises(ps, tmp_path):
    skill_dir = tmp_path / "s"
    _write_skill_md(skill_dir, name="code-review", description="d", tags=["Not_A_Slug"])

    with pytest.raises(ps.ValidationError, match="tag"):
        ps.validate(skill_dir)


def test_validate_tags_not_a_list_raises(ps, tmp_path):
    skill_dir = tmp_path / "s"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: code-review\ndescription: d\ntags: not-a-list\n---\nBody.\n"
    )

    with pytest.raises(ps.ValidationError, match="tag"):
        ps.validate(skill_dir)


def test_validate_missing_frontmatter_fence_raises(ps, tmp_path):
    skill_dir = tmp_path / "s"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("no frontmatter here at all\n")

    with pytest.raises(ps.ValidationError):
        ps.validate(skill_dir)


def test_validate_missing_skill_md_raises(ps, tmp_path):
    skill_dir = tmp_path / "s"
    skill_dir.mkdir()

    with pytest.raises(ps.ValidationError):
        ps.validate(skill_dir)


# --- build_index() ----------------------------------------------------------------


def test_build_index_golden_sorted_by_team_then_id(ps, monkeypatch):
    monkeypatch.setattr(ps, "_utcnow_iso", lambda: "2026-08-03T00:00:00Z")

    skills = [
        ps.Skill(
            id="commit-helper",
            team="platform",
            dir=Path("/x"),
            files=[Path("SKILL.md")],
            name="commit-helper",
            description="Writes commit messages",
            tags=["git"],
        ),
        ps.Skill(
            id="word-games",
            team="acme",
            dir=Path("/y"),
            files=[Path("SKILL.md")],
            name="word-games",
            description="Plays word games",
            tags=["fun"],
        ),
        ps.Skill(
            id="code-review",
            team="acme",
            dir=Path("/z"),
            files=[Path("SKILL.md")],
            name="code-review",
            description="Reviews PRs",
            tags=[],
        ),
    ]

    index = ps.build_index(skills, "abc123")

    assert index == {
        "generated_at": "2026-08-03T00:00:00Z",
        "entries": [
            {
                "id": "code-review",
                "name": "code-review",
                "description": "Reviews PRs",
                "team": "acme",
                "tags": [],
                "sha": "abc123",
            },
            {
                "id": "word-games",
                "name": "word-games",
                "description": "Plays word games",
                "team": "acme",
                "tags": ["fun"],
                "sha": "abc123",
            },
            {
                "id": "commit-helper",
                "name": "commit-helper",
                "description": "Writes commit messages",
                "team": "platform",
                "tags": ["git"],
                "sha": "abc123",
            },
        ],
    }


def test_build_index_empty_skills(ps, monkeypatch):
    monkeypatch.setattr(ps, "_utcnow_iso", lambda: "2026-08-03T00:00:00Z")

    assert ps.build_index([], "abc123") == {
        "generated_at": "2026-08-03T00:00:00Z",
        "entries": [],
    }


# --- sync(): duck-typed fake bucket client -----------------------------------------


class _FakeBlob:
    """Mirrors `google.cloud.storage.Blob`'s surface `sync()` calls: a
    `.metadata` setter (read by `upload_from_string`, matching the real
    client's multipart-upload behavior of sending whatever `.metadata` was
    set to before the upload call) plus `.upload_from_string(data,
    content_type=...)` and `.delete()`, and the `.name` attribute
    `list_blobs()` results are read for.
    """

    def __init__(self, bucket: _FakeBucket, name: str):
        self._bucket = bucket
        self.name = name
        self.metadata: dict | None = None

    def upload_from_string(self, data: bytes, content_type: str = "application/octet-stream") -> None:
        self._bucket.objects[self.name] = {
            "data": data,
            "metadata": dict(self.metadata or {}),
            "content_type": content_type,
        }

    def delete(self) -> None:
        self._bucket.objects.pop(self.name, None)


class _FakeBucket:
    """Mirrors `google.cloud.storage.Bucket`'s surface `sync()` calls:
    `.blob(key)` and `.list_blobs(prefix=...)`."""

    def __init__(self, name: str):
        self.name = name
        self.objects: dict[str, dict] = {}

    def blob(self, key: str) -> _FakeBlob:
        return _FakeBlob(self, key)

    def list_blobs(self, prefix: str = ""):
        return [_FakeBlob(self, name) for name in list(self.objects) if name.startswith(prefix)]


class _FakeBucketClient:
    """Mirrors `google.cloud.storage.Client`'s surface `sync()` calls:
    `.bucket(name) -> Bucket` — exactly what `_real_client()`'s real
    `storage.Client()` would need to support for this script to run
    unmodified against it.
    """

    def __init__(self):
        self._buckets: dict[str, _FakeBucket] = {}

    def bucket(self, name: str) -> _FakeBucket:
        return self._buckets.setdefault(name, _FakeBucket(name))


def _skill_on_disk(ps, tmp_path: Path, *, team: str, skill_id: str, files: dict[str, str]):
    skill_dir = tmp_path / team / skill_id
    for rel, content in files.items():
        path = skill_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return ps.Skill(
        id=skill_id,
        team=team,
        dir=skill_dir,
        files=sorted(Path(rel) for rel in files),
        name=skill_id,
        description=f"{skill_id} description",
        tags=[],
    )


def test_sync_uploads_with_metadata_and_returns_counts(ps, tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "_utcnow_iso", lambda: "2026-08-03T00:00:00Z")
    skill = _skill_on_disk(
        ps, tmp_path, team="acme", skill_id="code-review",
        files={"SKILL.md": "---\nname: code-review\n---\nBody.\n"},
    )
    client = _FakeBucketClient()

    result = ps.sync(client, "skills-bucket", [skill], "deadbeef")

    assert result == {"uploaded": 1, "deleted": 0}
    bucket = client.bucket("skills-bucket")
    obj = bucket.objects["acme/code-review/SKILL.md"]
    assert obj["metadata"] == {"source-sha": "deadbeef"}
    assert obj["content_type"] == "text/markdown"

    index_obj = bucket.objects["index.json"]
    assert index_obj["metadata"] == {"source-sha": "deadbeef"}
    index = json.loads(index_obj["data"])
    assert index["entries"] == [
        {
            "id": "code-review",
            "name": "code-review",
            "description": "code-review description",
            "team": "acme",
            "tags": [],
            "sha": "deadbeef",
        }
    ]


def test_sync_prunes_stale_but_spares_index_json(ps, tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "_utcnow_iso", lambda: "2026-08-03T00:00:00Z")
    client = _FakeBucketClient()
    bucket = client.bucket("skills-bucket")
    # Pre-seed leftover state from a previous publish: a skill file that no
    # longer exists in this run's discovered set, plus an existing index.json
    # (which must be spared by the prune even though it's never in the
    # "new keys" set either — it always gets rewritten at the end instead).
    bucket.blob("acme/old-skill/SKILL.md").upload_from_string(b"stale", content_type="text/markdown")
    bucket.blob("index.json").upload_from_string(b"{}", content_type="application/json")

    skill = _skill_on_disk(
        ps, tmp_path, team="acme", skill_id="code-review", files={"SKILL.md": "x"}
    )

    result = ps.sync(client, "skills-bucket", [skill], "cafe")

    assert result == {"uploaded": 1, "deleted": 1}
    assert "acme/old-skill/SKILL.md" not in bucket.objects
    assert "index.json" in bucket.objects
    assert "acme/code-review/SKILL.md" in bucket.objects


def test_sync_multiple_files_per_skill_all_uploaded(ps, tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "_utcnow_iso", lambda: "2026-08-03T00:00:00Z")
    skill = _skill_on_disk(
        ps, tmp_path, team="acme", skill_id="code-review",
        files={"SKILL.md": "x", "reference.md": "y", "scripts/run.sh": "z"},
    )
    client = _FakeBucketClient()

    result = ps.sync(client, "skills-bucket", [skill], "sha1")

    assert result["uploaded"] == 3
    bucket = client.bucket("skills-bucket")
    assert set(bucket.objects) == {
        "acme/code-review/SKILL.md",
        "acme/code-review/reference.md",
        "acme/code-review/scripts/run.sh",
        "index.json",
    }


# --- main() / --check ---------------------------------------------------------------


def test_main_check_good_repo_exits_zero_and_never_builds_a_client(ps, tmp_path, monkeypatch):
    _write_skill_md(tmp_path / "teams" / "acme" / "code-review", name="code-review", description="d")
    monkeypatch.chdir(tmp_path)

    def _boom():
        raise AssertionError("--check must never construct a storage client")

    monkeypatch.setattr(ps, "_real_client", _boom)

    assert ps.main(["--bucket", "skills-bucket", "--check"]) == 0


def test_main_check_bad_repo_exits_nonzero_and_prints_each_error(ps, tmp_path, monkeypatch, capsys):
    skill_dir = tmp_path / "teams" / "acme" / "code-review"
    _write_skill_md(skill_dir, name=None, description="no name here")
    monkeypatch.chdir(tmp_path)

    rc = ps.main(["--bucket", "skills-bucket", "--check"])

    assert rc == 1
    captured = capsys.readouterr()
    assert "name" in (captured.out + captured.err)


def test_main_check_never_reads_github_sha(ps, tmp_path, monkeypatch):
    _write_skill_md(tmp_path / "skills" / "word-games", name="word-games", description="d")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_SHA", raising=False)

    assert ps.main(["--bucket", "skills-bucket", "--check"]) == 0


def test_main_publish_path_uses_real_client_seam_and_github_sha(ps, tmp_path, monkeypatch):
    _write_skill_md(tmp_path / "skills" / "word-games", name="word-games", description="d")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_SHA", "sha123")

    fake_client = _FakeBucketClient()
    monkeypatch.setattr(ps, "_real_client", lambda: fake_client)

    rc = ps.main(["--bucket", "skills-bucket"])

    assert rc == 0
    bucket = fake_client.bucket("skills-bucket")
    assert "shared/word-games/SKILL.md" in bucket.objects
    index = json.loads(bucket.objects["index.json"]["data"])
    assert index["entries"][0]["sha"] == "sha123"


# --- workflow template: token inventory + shape ------------------------------------

_PNYX_TOKEN_RE = re.compile(r"__PNYX_[A-Z_]+__")

_EXPECTED_TOKENS = {"__PNYX_BUCKET__", "__PNYX_WIF_PROVIDER__", "__PNYX_SERVICE_ACCOUNT__"}


def test_workflow_token_inventory_is_exactly_pinned():
    text = _YML_PATH.read_text()
    assert set(_PNYX_TOKEN_RE.findall(text)) == _EXPECTED_TOKENS


def test_workflow_no_leftover_tokens_after_literal_replacement():
    text = _YML_PATH.read_text()
    rendered = (
        text.replace("__PNYX_BUCKET__", "example-org-skills")
        .replace(
            "__PNYX_WIF_PROVIDER__",
            "projects/123/locations/global/workloadIdentityPools/example-pool/providers/github",
        )
        .replace(
            "__PNYX_SERVICE_ACCOUNT__",
            "skills-publisher@example-project.iam.gserviceaccount.com",
        )
    )
    assert "__PNYX_" not in rendered
    # A literal-token render must never disturb any `${{ }}` GitHub Actions
    # expression already in the template (the whole reason bootstrap uses
    # plain string replacement rather than any `{}`-based templating).
    assert "${{" in rendered


def test_workflow_yaml_is_well_formed_with_expected_triggers_and_permissions():
    doc = yaml.safe_load(_YML_PATH.read_text())

    assert set(doc["on"]) == {"pull_request", "push"}
    assert doc["on"]["push"]["branches"] == ["main"]
    # Workflow-level permissions: only contents for PR checks
    assert doc["permissions"] == {"contents": "read"}
    # publish job has additional id-token permission
    assert doc["jobs"]["publish"]["permissions"] == {"contents": "read", "id-token": "write"}
    # check job has only contents permission
    assert doc["jobs"]["check"]["permissions"] == {"contents": "read"}


def test_workflow_jobs_run_check_on_pr_and_publish_on_push():
    doc = yaml.safe_load(_YML_PATH.read_text())
    jobs = doc["jobs"]

    all_runs = " ".join(
        step["run"] for job in jobs.values() for step in job["steps"] if "run" in step
    )
    assert "publish_skills.py" in all_runs
    assert "--check" in all_runs

    # Both jobs share: checkout and setup-python
    for job in jobs.values():
        uses = [step["uses"] for step in job["steps"] if "uses" in step]
        assert any(u.startswith("actions/checkout@") for u in uses)
        assert any(u.startswith("actions/setup-python@") for u in uses)

    # check job (PR): no cloud auth, no google-cloud-storage
    check_job = jobs["check"]
    check_uses = [step["uses"] for step in check_job["steps"] if "uses" in step]
    assert not any(u.startswith("google-github-actions/auth@") for u in check_uses)
    check_pip = next(
        (step["run"] for step in check_job["steps"] if step.get("run", "").startswith("pip install")),
        None,
    )
    assert check_pip is not None
    assert "google-cloud-storage" not in check_pip
    assert "pyyaml" in check_pip

    # publish job (push): has cloud auth and google-cloud-storage
    publish_job = jobs["publish"]
    publish_uses = [step["uses"] for step in publish_job["steps"] if "uses" in step]
    assert any(u.startswith("google-github-actions/auth@") for u in publish_uses)
    auth_step = next(step for step in publish_job["steps"] if step.get("uses", "").startswith("google-github-actions/auth@"))
    assert auth_step["with"]["workload_identity_provider"] == "__PNYX_WIF_PROVIDER__"
    assert auth_step["with"]["service_account"] == "__PNYX_SERVICE_ACCOUNT__"
    publish_pip = next(
        (step["run"] for step in publish_job["steps"] if step.get("run", "").startswith("pip install")),
        None,
    )
    assert publish_pip is not None
    assert "google-cloud-storage" in publish_pip
    assert "pyyaml" in publish_pip
