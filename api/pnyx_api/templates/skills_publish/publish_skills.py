"""publish_skills.py — runs inside the client repo's own GitHub Action
(installed there by pnyx's bootstrap alongside `publish-skills.yml`, the
sibling workflow template in this same directory). This file ships as
packaged data inside pnyx (`pnyx_api/templates/skills_publish/`); it is never
imported by pnyx itself — only copied byte-for-byte into a client org's
skills repo at `.github/scripts/publish_skills.py` and invoked as a plain
script (`python .github/scripts/publish_skills.py --bucket ...`).

Because of that, this module is stdlib + pyyaml only at parse/discover/
validate time — pyyaml is the one third-party dependency the workflow's
`pip install` step brings in alongside `google-cloud-storage`, which this
module never imports at module scope. `google-cloud-storage` is imported
lazily, only inside `_real_client()` (only ever called from `main()`'s
publish path, never from `--check`), so a PR's `--check` run — and pnyx's
own test suite, which loads this file by path via `importlib.util` and
exercises `sync()` against a duck-typed fake client — never needs that SDK
installed at all.

Two skill layouts are supported, and may coexist in the same repo:

  - `teams/<team>/<id>/SKILL.md`      — team-scoped
  - `skills/<id>/SKILL.md`            — legacy/flat, team is always "shared"

Every skill directory's `SKILL.md` carries `---`-fenced YAML frontmatter
(`name`, required; `description`, falls back to the first non-blank body
line if omitted; `tags`, optional). The published bucket layout mirrors the
discovered tree: every file under a skill's directory lands at
`<team>/<id>/<relpath>`, plus one `index.json` at the bucket root — the same
contract `pnyx_api.search._load_catalog_entries` reads back
(`{"generated_at": ..., "entries": [{"id", "name", "description", "team",
"tags", "sha"}, ...]}`).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

# Authoritative for this script (echoes pnyx_api.catalog.NAME_RE's shape, but
# is deliberately its own pattern rather than an import — this file has to
# run standalone in a client repo with no pnyx_api on its path at all).
SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,39}$")

# A tag is a plain slug: lowercase alnum groups separated by single hyphens,
# never a leading/trailing/doubled hyphen.
_TAG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

_SKILL_MD = "SKILL.md"

_CONTENT_TYPES = {
    ".md": "text/markdown",
    ".json": "application/json",
    ".yaml": "application/x-yaml",
    ".yml": "application/x-yaml",
    ".sh": "text/x-shellscript",
    ".py": "text/x-python",
    ".txt": "text/plain",
}


class ValidationError(ValueError):
    """One or more skill directories failed discovery or frontmatter
    validation. The message may embed multiple newline-separated problems
    (see `discover()`) — `main()`'s `--check` path prints each line as its
    own "error: ..." entry, so a PR sees every problem in the repo at once
    rather than only the first one `discover()` happened to hit.
    """


@dataclass
class Skill:
    id: str
    team: str
    dir: Path
    files: list[Path]  # relative to `dir`, sorted
    name: str
    description: str
    tags: list[str]


# --- discover -----------------------------------------------------------------


def _find_skill_dirs(root: Path) -> list[tuple[str, Path]]:
    """Every `(team, skill_dir)` pair from both layouts, teams-layout first
    (each team's skill dirs sorted), then the legacy flat layout (sorted) —
    a deterministic order, not a meaningful one (`build_index` is what
    actually orders the published index, by `(team, id)`).
    """
    found: list[tuple[str, Path]] = []

    teams_dir = root / "teams"
    if teams_dir.is_dir():
        for team_dir in sorted(p for p in teams_dir.iterdir() if p.is_dir()):
            for skill_dir in sorted(p for p in team_dir.iterdir() if p.is_dir()):
                if (skill_dir / _SKILL_MD).is_file():
                    found.append((team_dir.name, skill_dir))

    skills_dir = root / "skills"
    if skills_dir.is_dir():
        for skill_dir in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
            if (skill_dir / _SKILL_MD).is_file():
                found.append(("shared", skill_dir))

    return found


def discover(root: Path) -> list[Skill]:
    """Every skill under `root`, from either layout (see module docstring).

    A `(team, id)` collision — two directories that would publish to the
    same bucket prefix — is flagged the moment the second directory is seen,
    independent of whether either directory's frontmatter is itself valid:
    it's a structural conflict about *where things would be published*, not
    a content problem. Every problem found (duplicates and per-skill
    frontmatter failures alike) is collected before raising, so a single
    `ValidationError` here can report every issue in the repo at once — see
    `ValidationError`'s docstring for why that matters to `main()`'s
    `--check` path.
    """
    errors: list[str] = []
    seen: dict[tuple[str, str], Path] = {}
    skills: list[Skill] = []

    for team, skill_dir in _find_skill_dirs(root):
        skill_id = skill_dir.name
        key = (team, skill_id)
        if key in seen:
            errors.append(
                f"duplicate skill (team={team!r}, id={skill_id!r}): "
                f"{seen[key]} and {skill_dir}"
            )
            continue
        seen[key] = skill_dir

        try:
            name, description, tags = validate(skill_dir)
        except ValidationError as exc:
            errors.append(str(exc))
            continue

        files = sorted(
            (p.relative_to(skill_dir) for p in skill_dir.rglob("*") if p.is_file()),
            key=lambda p: p.as_posix(),
        )
        skills.append(
            Skill(
                id=skill_id,
                team=team,
                dir=skill_dir,
                files=files,
                name=name,
                description=description,
                tags=tags,
            )
        )

    if errors:
        raise ValidationError("\n".join(errors))

    return skills


# --- validate -------------------------------------------------------------------


def _parse_frontmatter(text: str, skill_dir: Path) -> tuple[dict, str]:
    if not text.startswith("---"):
        raise ValidationError(f"{skill_dir}: SKILL.md is missing its '---' frontmatter fence")

    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ValidationError(f"{skill_dir}: SKILL.md frontmatter is not closed with '---'")

    _, frontmatter_text, body = parts
    try:
        frontmatter = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as exc:
        raise ValidationError(f"{skill_dir}: invalid YAML frontmatter: {exc}") from exc

    if frontmatter is None:
        frontmatter = {}
    if not isinstance(frontmatter, dict):
        raise ValidationError(f"{skill_dir}: frontmatter must be a YAML mapping")

    return frontmatter, body


def validate(skill_dir: Path) -> tuple[str, str, list[str]]:
    """Parse and validate `<skill_dir>/SKILL.md`'s frontmatter; returns
    `(name, description, tags)`. Raises `ValidationError` naming `skill_dir`
    for any problem: unreadable/missing file, unfenced or malformed YAML
    frontmatter, a missing or malformed `name`, a missing `description` with
    no fallback body line available, or a malformed `tags` list.
    """
    skill_md = skill_dir / _SKILL_MD
    try:
        text = skill_md.read_text()
    except OSError as exc:
        raise ValidationError(f"{skill_dir}: could not read {_SKILL_MD} ({exc})") from exc

    frontmatter, body = _parse_frontmatter(text, skill_dir)

    name = frontmatter.get("name")
    if not name or not isinstance(name, str):
        raise ValidationError(f"{skill_dir}: frontmatter is missing required 'name'")
    if not SKILL_NAME_RE.fullmatch(name):
        raise ValidationError(
            f"{skill_dir}: 'name' {name!r} does not match {SKILL_NAME_RE.pattern}"
        )

    description = frontmatter.get("description")
    if not description:
        # Same fallback semantic the pre-Task-5 catalog frontmatter parser
        # used (pnyx_api.catalog's old `_parse_skill_frontmatter`): the
        # first non-blank line of the body, if any.
        description = next((line.strip() for line in body.splitlines() if line.strip()), "")
    if not description:
        raise ValidationError(
            f"{skill_dir}: frontmatter is missing 'description' and the body has no "
            "non-blank line to fall back to"
        )

    raw_tags = frontmatter.get("tags")
    if raw_tags is None:
        raw_tags = []
    if not isinstance(raw_tags, list):
        raise ValidationError(f"{skill_dir}: 'tags' must be a list of slugs")

    tags: list[str] = []
    for tag in raw_tags:
        if not isinstance(tag, str) or not _TAG_RE.fullmatch(tag):
            raise ValidationError(f"{skill_dir}: invalid tag {tag!r} (must be a slug)")
        tags.append(tag)

    return str(name), str(description), tags


# --- build_index ----------------------------------------------------------------


def _utcnow_iso() -> str:
    """Now, in UTC, `...Z`-suffixed ISO 8601 — a small seam so tests can
    monkeypatch a fixed clock for a byte-exact `build_index` golden
    comparison, without freezing real time anywhere else in the process.
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_index(skills: list[Skill], sha: str) -> dict:
    """The Task 3 index contract (see `pnyx_api.search._load_catalog_entries`
    for the reader side): entries sorted by `(team, id)` so the published
    file's diff is stable run to run regardless of filesystem iteration
    order.
    """
    ordered = sorted(skills, key=lambda s: (s.team, s.id))
    return {
        "generated_at": _utcnow_iso(),
        "entries": [
            {
                "id": skill.id,
                "name": skill.name,
                "description": skill.description,
                "team": skill.team,
                "tags": list(skill.tags),
                "sha": sha,
            }
            for skill in ordered
        ],
    }


# --- sync -----------------------------------------------------------------------


def _content_type_for(path: Path) -> str:
    return _CONTENT_TYPES.get(path.suffix, "application/octet-stream")


def sync(client, bucket: str, skills: list[Skill], sha: str) -> dict:
    """Upload every discovered skill's files to `bucket`, prune whatever's
    left over from a prior publish, and rewrite `index.json`.

    `client` is duck-typed to the one corner of the `google-cloud-storage`
    surface this needs: `client.bucket(name) -> bucket` (a handle, no API call);
    `bucket.blob(key) -> blob` with a `.metadata` setter plus `.upload_from_string(data,
    content_type=...)`; `bucket.list_blobs(prefix=...)` yielding blobs with
    `.name`/`.delete()`. This script requires only object-level permissions
    (e.g. roles/storage.objectAdmin); no bucket-metadata operations. Every
    uploaded object (skill files and `index.json` alike) carries custom
    metadata `{"source-sha": sha}`.

    The prune is computed from a prefix-wide listing (every object currently
    in the bucket) minus this run's newly-uploaded key set — never a
    per-skill listing — so a skill directory removed entirely between runs
    still has its old objects cleaned up. `index.json` is excluded from the
    prune unconditionally (it's never a member of the "new keys" set either,
    since it's uploaded separately, last): a bucket with versioning enabled
    is the safety net for its history, not this prune.
    """
    bucket_obj = client.bucket(bucket)

    new_keys: set[str] = set()
    uploaded = 0
    for skill in skills:
        for rel_path in skill.files:
            key = f"{skill.team}/{skill.id}/{rel_path.as_posix()}"
            data = (skill.dir / rel_path).read_bytes()
            blob = bucket_obj.blob(key)
            blob.metadata = {"source-sha": sha}
            blob.upload_from_string(data, content_type=_content_type_for(rel_path))
            new_keys.add(key)
            uploaded += 1

    deleted = 0
    for existing in bucket_obj.list_blobs(prefix=""):
        if existing.name == "index.json" or existing.name in new_keys:
            continue
        existing.delete()
        deleted += 1

    index = build_index(skills, sha)
    index_bytes = (json.dumps(index, indent=2) + "\n").encode()
    index_blob = bucket_obj.blob("index.json")
    index_blob.metadata = {"source-sha": sha}
    index_blob.upload_from_string(index_bytes, content_type="application/json")

    return {"uploaded": uploaded, "deleted": deleted}


# --- main -------------------------------------------------------------------------


def _real_client():
    """The real `google.cloud.storage.Client`, imported lazily so nothing at
    module scope — including pnyx's own test suite loading this file by path
    — ever needs the `google-cloud-storage` SDK installed. Only `main()`'s
    publish path (never `--check`) calls this.
    """
    from google.cloud import storage

    return storage.Client()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Publish this repo's skills to the org's skills bucket."
    )
    parser.add_argument("--bucket", required=True, help="destination bucket name")
    parser.add_argument(
        "--check",
        action="store_true",
        help="discover and validate only; no upload, no cloud auth required (PR gate)",
    )
    args = parser.parse_args(argv)

    root = Path.cwd()

    try:
        skills = discover(root)
    except ValidationError as exc:
        for line in str(exc).splitlines():
            print(f"error: {line}", file=sys.stderr)
        return 1

    if args.check:
        return 0

    sha = os.environ.get("GITHUB_SHA", "")
    client = _real_client()
    result = sync(client, args.bucket, skills, sha)
    print(f"uploaded {result['uploaded']} file(s), deleted {result['deleted']} stale object(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
