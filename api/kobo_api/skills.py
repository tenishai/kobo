"""Skills module: read, edit, and delete an agent's local, guardrail-authored
skills (`bundle/skills/<id>/SKILL.md` in the agent's repo — the same bundle
of record `kobo_api.knowledge` operates on for seed-memory files), plus
pulling a skill straight from the org's skills catalog (the GCS bucket
`kobo_api.steps._stage_skills` reads at provision time) into an agent's repo.

Every operation here clones the repo fresh into a `tempfile.TemporaryDirectory`
and discards it before returning — there is no local working copy kept around
between calls, mirroring `kobo_api.knowledge` (and, via `kobo_api.steps`, the
rest of kobo_api's treatment of a repo clone as a disposable means to an end,
never state of its own). Every write holds the agent's repo write lock
(`kobo_api.repo_lock`) from clone to push, so two writes to one agent queue
rather than race (issue #268); reads take no lock.

`add_catalog_skill` reuses `kobo_api.steps._materialize_catalog_skill` — the
exact same containment-checked bucket download `_stage_skills` runs at
provision time — so a skill added here after a provision, and one a fresh
provision stages from scratch, are always materialized identically. That
includes the marketplace dialect (persona-metadata plan Task 1b):
`add_catalog_skill` hoists `kobo_api.search.read_marketplace_catalog` once,
same as `_stage_skills` does, and passes it through, rather than defaulting
`_materialize_catalog_skill`'s `marketplace_catalog` argument to `None` and
always falling through to the legacy team-prefixed path.

Every mutation re-derives the agent card (`kobo_api.composer.
refresh_agent_card`) in the same commit as the skill change, so the card's
`skills` list is always the bundle's (issue #147).
"""

from __future__ import annotations

import logging
import re
import shutil
import tempfile
import time
from pathlib import Path

from kobo_api.clients import ClientError, GitHubClient, StorageClient
from kobo_api.composer import refresh_agent_card, update_agent_yaml_skills_load
from kobo_api.config import Config
from kobo_api.repo_lock import repo_write_lock
from kobo_api.search import read_marketplace_catalog
from kobo_api.steps import _materialize_catalog_skill

log = logging.getLogger(__name__)

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


def _admit_skills_load(workdir: Path, repo: str) -> None:
    """Add `skills.load` to the agent's allow list in the same commit as the
    skill (issue #137), so a skill added after birth is reachable on daedalus
    0.5.0 the way a skill composed at birth is. A bundle whose agent.yaml
    can't be edited still gets its skill; the warning names why the tool
    wasn't added."""
    try:
        update_agent_yaml_skills_load(workdir / "bundle")
    except ClientError as exc:
        log.warning("skills: %s: skills.load not added to tools.allow: %s", repo, exc)


def add_catalog_skill(
    github: GitHubClient,
    storage: StorageClient,
    config: Config,
    repo: str,
    skill_id: str,
    team: str,
    *,
    replace: bool = False,
) -> tuple[str, str]:
    """Materialize the catalog skill `<team>/<skill_id>/` into `repo`'s
    `bundle/skills/<skill_id>/` and push it. With `replace`, the directory
    is emptied first so the push is the catalog's current version exactly,
    in one commit (issue #182: a refresh used to be a remove and an add,
    two commits and two rolls).

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
    with repo_write_lock(repo), tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(repo, workdir)
        marketplace_catalog = read_marketplace_catalog(storage, config.skills_bucket)
        source_sha = _write_catalog_skill(
            workdir, storage, config, team, skill_id, marketplace_catalog, replace=replace
        )
        _admit_skills_load(workdir, repo)
        refresh_agent_card(workdir / "bundle")
        verb = "refresh" if replace else "add"
        github.commit_push(workdir, f"kobo: {verb} skill {skill_id}")
        return github.local_head(workdir), source_sha


def _write_catalog_skill(
    workdir: Path,
    storage: StorageClient,
    config: Config,
    team: str,
    skill_id: str,
    marketplace_catalog: dict[str, dict] | None,
    *,
    replace: bool,
) -> str:
    """Materialize one catalog skill into the clone at `workdir` and return
    its current source-sha. The one download step `add_catalog_skill` and
    `add_catalog_skills` share. A skill the catalog no longer serves leaves
    no directory behind (so a batch commit never carries an empty
    `bundle/skills/<id>/`) and raises `SkillsError`, message preserved."""
    skill_dir = workdir / "bundle" / "skills" / skill_id
    if replace and skill_dir.exists():
        shutil.rmtree(skill_dir)
    skill_dir.mkdir(parents=True, exist_ok=True)
    try:
        return _materialize_catalog_skill(
            storage, config, team, skill_id, skill_dir, marketplace_catalog
        )
    except RuntimeError as exc:
        shutil.rmtree(skill_dir, ignore_errors=True)
        raise SkillsError(str(exc)) from exc


def add_catalog_skills(
    github: GitHubClient,
    storage: StorageClient,
    config: Config,
    repo: str,
    picks: list[tuple[str, str]],
) -> tuple[str, dict[str, str], list[tuple[str, str]]]:
    """Materialize several catalog skills (`(skill_id, team)` pairs) into
    `repo` in ONE clone, one commit and one push (issue #266: one rebuild
    for the whole pick, not one per skill).

    Each skill is validated and downloaded on its own; one that fails (a
    bad id, or a skill the catalog no longer serves) is refused with its
    `SkillsError` message and the rest still land. Returns
    `(commit_sha, landed, refused)`: `landed` maps each skill id that was
    written to its catalog source-sha (same pin `add_catalog_skill`
    returns), `refused` lists `(skill_id, reason)` pairs in request order.
    Raises `SkillsError` (the first refusal's message) when nothing could
    land, so nothing is cloned or pushed for an all-bad request.
    """
    refused: list[tuple[str, str]] = []
    valid: list[tuple[str, str]] = []
    for skill_id, team in picks:
        try:
            _validate_skill_id(skill_id)
        except SkillsError as exc:
            refused.append((skill_id, str(exc)))
            continue
        valid.append((skill_id, team))
    if not valid:
        raise SkillsError(refused[0][1])
    with repo_write_lock(repo), tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(repo, workdir)
        marketplace_catalog = read_marketplace_catalog(storage, config.skills_bucket)
        landed: dict[str, str] = {}
        for skill_id, team in valid:
            try:
                landed[skill_id] = _write_catalog_skill(
                    workdir, storage, config, team, skill_id, marketplace_catalog, replace=False
                )
            except SkillsError as exc:
                refused.append((skill_id, str(exc)))
        if not landed:
            raise SkillsError(refused[0][1])
        _admit_skills_load(workdir, repo)
        refresh_agent_card(workdir / "bundle")
        github.commit_push(workdir, f"kobo: add skills {', '.join(landed)}")
        return github.local_head(workdir), landed, refused


def remove_skill(github: GitHubClient, repo: str, skill_id: str) -> str:
    """Delete `bundle/skills/<skill_id>/` (the whole tree) from `repo` and
    push the removal.

    Raises `SkillsError` if the directory isn't there to delete — a stale
    UI state (someone else already removed it) is a real error here, same
    as `kobo_api.knowledge.delete_knowledge_file`'s missing-file case.
    """
    _validate_skill_id(skill_id)
    with repo_write_lock(repo), tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(repo, workdir)
        skill_dir = workdir / "bundle" / "skills" / skill_id
        if not skill_dir.is_dir():
            raise SkillsError(f"skill not found: {skill_id}")
        shutil.rmtree(skill_dir)
        refresh_agent_card(workdir / "bundle")
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
    with repo_write_lock(repo), tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(repo, workdir)
        skill_dir = workdir / "bundle" / "skills" / skill_id
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
        _admit_skills_load(workdir, repo)
        refresh_agent_card(workdir / "bundle")
        github.commit_push(workdir, f"kobo: update skill {skill_id}")
        return github.local_head(workdir)
