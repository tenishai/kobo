"""Skills module: read, edit, and delete an agent's local, guardrail-authored
skills (`bundle/skills/<id>/SKILL.md` in the agent's repo — the same bundle
of record `kobo_api.knowledge` operates on for seed-memory files), plus
pulling a skill straight from the org's skills catalog (the GCS bucket
`kobo_api.steps._stage_skills` reads at provision time) into an agent's repo.

Every operation here clones the repo fresh into a `tempfile.TemporaryDirectory`
and discards it before returning — there is no local working copy kept around
between calls, mirroring `kobo_api.knowledge` (and, via `kobo_api.steps`, the
rest of kobo_api's treatment of a repo clone as a disposable means to an end,
never state of its own).

`add_catalog_skill` reuses `kobo_api.steps._materialize_catalog_skill` — the
exact same containment-checked bucket download `_stage_skills` runs at
provision time — so a skill added here after a provision, and one a fresh
provision stages from scratch, are always materialized identically. That
includes the marketplace dialect (persona-metadata plan Task 1b):
`add_catalog_skill` hoists `kobo_api.search.read_marketplace_catalog` once,
same as `_stage_skills` does, and passes it through, rather than defaulting
`_materialize_catalog_skill`'s `marketplace_catalog` argument to `None` and
always falling through to the legacy team-prefixed path.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import time
from pathlib import Path

from kobo_api.clients import GitHubClient, StorageClient
from kobo_api.config import Config
from kobo_api.search import read_marketplace_catalog
from kobo_api.steps import _materialize_catalog_skill

# 1-40 chars: a lowercase letter, then lowercase/digit/hyphen — no path
# separators, no leading digit/hyphen. Unlike KNOWLEDGE_FILENAME_RE
# (kobo_api.knowledge), a skill id names a whole directory
# (bundle/skills/<id>/), not one file, so there's no ".md" suffix to match.
SKILL_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")

SKILL_MAX_BYTES = 65536

# A leading `---`-fenced block, DOTALL so `.` spans the block's newlines.
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


class SkillsError(Exception):
    """A skill id failed validation, a save's content was empty, too large,
    or missing required frontmatter, a lookup/removal named a skill that
    isn't there locally, or a catalog add named a skill the catalog no
    longer serves."""


def _validate_skill_id(skill_id: str) -> None:
    if not SKILL_ID_RE.fullmatch(skill_id):
        raise SkillsError(f"invalid skill id: {skill_id!r}")


def _validate_content(content: str) -> None:
    size = len(content.encode("utf-8"))
    if size == 0:
        raise SkillsError("skill file content must not be empty")
    if size > SKILL_MAX_BYTES:
        raise SkillsError(
            f"skill file content is {size} bytes, exceeds the "
            f"{SKILL_MAX_BYTES}-byte limit"
        )


def _validate_frontmatter(content: str) -> None:
    match = _FRONTMATTER_RE.match(content)
    if not match:
        raise SkillsError("skill frontmatter is missing — start the file with a --- block")
    block = match.group(1)
    for field in ("name", "description"):
        if not re.search(rf"^{field}\s*:\s*\S", block, re.MULTILINE):
            raise SkillsError(f"skill frontmatter needs a {field}: line")


def add_catalog_skill(
    github: GitHubClient,
    storage: StorageClient,
    config: Config,
    repo: str,
    skill_id: str,
    team: str,
) -> tuple[str, str]:
    """Materialize the catalog skill `<team>/<skill_id>/` into `repo`'s
    `bundle/skills/<skill_id>/` and push it.

    Reuses `kobo_api.steps._materialize_catalog_skill` — the exact same
    containment-checked bucket download a fresh provision runs
    (`_stage_skills`) — so a skill picked here later downloads identically
    to one picked at provision time. That includes the marketplace dialect
    (persona-metadata plan Task 1b): mirroring `_stage_skills`' own hoisted
    pattern, this reads the bucket's dialect probe
    (`kobo_api.search.read_marketplace_catalog`) once, up front, and passes
    it through, so a marketplace-dialect bucket resolves via the
    version-pinned path here exactly as it does at provision time — never
    silently falling through to the legacy team-prefixed listing (which
    would 404 for a skill the marketplace dialect never wrote under a
    team-prefixed key at all). Its `RuntimeError` (raised when the catalog
    no longer serves the skill) is re-raised as `SkillsError`, message
    preserved verbatim, so every caller-facing error out of this module is
    a `SkillsError`.

    Returns `(commit_sha, current_source_sha)`: the pushed commit's local
    sha (`github.local_head` — a fresh clone's own HEAD right after the
    push, not a GitHub API read that could lag behind it, same reasoning as
    `kobo_api.knowledge.save_knowledge_file`) alongside the catalog's
    current `source-sha` for this skill (`""` if the publisher set none) —
    the sha a caller would record on the agent row as this pick's pin.
    """
    _validate_skill_id(skill_id)
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(repo, workdir)
        skill_dir = workdir / "bundle" / "skills" / skill_id
        skill_dir.mkdir(parents=True, exist_ok=True)
        marketplace_catalog = read_marketplace_catalog(storage, config.skills_bucket)
        try:
            source_sha = _materialize_catalog_skill(
                storage, config, team, skill_id, skill_dir, marketplace_catalog
            )
        except RuntimeError as exc:
            raise SkillsError(str(exc)) from exc
        github.commit_push(workdir, f"kobo: add skill {skill_id}")
        return github.local_head(workdir), source_sha


def remove_skill(github: GitHubClient, repo: str, skill_id: str) -> str:
    """Delete `bundle/skills/<skill_id>/` (the whole tree) from `repo` and
    push the removal.

    Raises `SkillsError` if the directory isn't there to delete — a stale
    UI state (someone else already removed it) is a real error here, same
    as `kobo_api.knowledge.delete_knowledge_file`'s missing-file case.
    """
    _validate_skill_id(skill_id)
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(repo, workdir)
        skill_dir = workdir / "bundle" / "skills" / skill_id
        if not skill_dir.is_dir():
            raise SkillsError(f"skill not found: {skill_id}")
        shutil.rmtree(skill_dir)
        github.commit_push(workdir, f"kobo: remove skill {skill_id}")
        return github.local_head(workdir)


def read_local_skill(github: GitHubClient, repo: str, skill_id: str) -> str:
    """The current content of `repo`'s `bundle/skills/<skill_id>/SKILL.md`.

    Raises `SkillsError` if the file isn't there — unlike
    `kobo_api.knowledge.read_knowledge`'s whole-directory listing (where an
    empty result is a valid state), a caller asking to read one specific
    skill id that doesn't exist is asking about something that isn't there.
    """
    _validate_skill_id(skill_id)
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(repo, workdir)
        target = workdir / "bundle" / "skills" / skill_id / "SKILL.md"
        if not target.is_file():
            raise SkillsError(f"skill not found: {skill_id}")
        return target.read_text(encoding="utf-8")


# The reconciling GET route (main.py's list_skills) clones this repo on
# every call to learn the runtime truth-set, and three multipliers make
# that hot: every tabpanel mounts on page view (so this fires on every
# agent-detail load, not just when the Skills tab is selected),
# `refetchOnWindowFocus` defaults on for the query, and a learnings verdict
# invalidates the whole ["agents", name] prefix (which this route's own
# query key falls under). A short TTL cache absorbs that multiplier
# without ever serving stale-for-long: a real mutation (catalog add, local
# save, delete) busts its repo's entry immediately after its own repo
# write succeeds, so a GET landing within the TTL right after a mutation
# still sees that mutation's result, never a ghost of what used to be
# there.
_REPO_SKILLS_TTL_S = 60.0
_repo_skills_cache: dict[str, tuple[float, list[str]]] = {}


def list_repo_skills(
    github: GitHubClient, repo: str, *, now: float | None = None
) -> list[str]:
    """Every skill id present in `repo`'s bundle/skills/ -- the runtime
    truth-set the reconciling route reads. A skill is a directory holding
    a SKILL.md; anything else under bundle/skills/ is ignored, matching
    what the bundle loader itself registers. Same clone-a-tempdir pattern
    as every other function in this module, except a fresh clone only
    actually runs when `repo`'s cached listing is missing or older than
    `_REPO_SKILLS_TTL_S` (`time.monotonic()`, immune to wall-clock
    adjustment; `now` is a test-only override).

    A failed clone is never cached -- only a successful listing is stored
    on the way out, so a transient `ClientError` propagates to the caller
    exactly as it always has, and the very next call tries a fresh clone
    again rather than being poisoned by the failure.
    """
    moment = time.monotonic() if now is None else now
    cached = _repo_skills_cache.get(repo)
    if cached is not None:
        stored_at, ids = cached
        if moment - stored_at < _REPO_SKILLS_TTL_S:
            return ids
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(repo, workdir)
        skills_dir = workdir / "bundle" / "skills"
        if not skills_dir.is_dir():
            ids: list[str] = []
        else:
            ids = sorted(
                entry.name
                for entry in skills_dir.iterdir()
                if entry.is_dir() and (entry / "SKILL.md").is_file()
            )
    _repo_skills_cache[repo] = (moment, ids)
    return ids


def bust_repo_skills_cache(repo: str) -> None:
    """Drop `repo`'s cached skills listing, if any. Called by the three
    kobo-lane skills-mutation routes (main.py) right after their own repo
    write succeeds -- without this, a delete followed by a GET within the
    TTL would re-heal the just-deleted skill back into the ledger from the
    stale cached listing, and a just-added skill would stay invisible for
    the rest of the window."""
    _repo_skills_cache.pop(repo, None)


def save_local_skill(github: GitHubClient, repo: str, skill_id: str, content: str) -> str:
    """Write (or overwrite) `bundle/skills/<skill_id>/SKILL.md` and push it.

    Validates, in order: the id (`SKILL_ID_RE`), the content's size (empty
    or over `SKILL_MAX_BYTES`), and the frontmatter (`_validate_frontmatter`
    — a leading `---` block with both a `name:` and `description:` line,
    the two fields a bundle loader needs to register the skill). Returns
    the pushed commit's local sha (`github.local_head`) even when `content`
    is byte-for-byte identical to what's already there — same truthful-no-op
    reasoning as `kobo_api.knowledge.save_knowledge_file`.
    """
    _validate_skill_id(skill_id)
    _validate_content(content)
    _validate_frontmatter(content)
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(repo, workdir)
        skill_dir = workdir / "bundle" / "skills" / skill_id
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
        github.commit_push(workdir, f"kobo: update skill {skill_id}")
        return github.local_head(workdir)
