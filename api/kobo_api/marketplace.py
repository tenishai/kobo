"""The marketplace repo door: list and fetch the shared tools tree,
publish new skills and tools by direct commit (operator ruling 1 in the
wave-1 spec: no review gate; mechanical validation runs BEFORE any
commit, attribution rides the commit message, the MCP audit log records
the call). Mirrors kobo_api.skills / kobo_api.tooling: tempdir clone,
validate-then-commit, one typed error class -- every caller-facing error
out of this module is a `MarketplaceError`, even when the underlying
check lives in `kobo_api.skills`/`kobo_api.tooling` and raises its own
typed error first.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

import yaml

from kobo_api.clients import GitHubClient
from kobo_api.config import Config
from kobo_api.skills import SkillsError, _validate_skill_id
from kobo_api.tooling import ToolingError, _validate_content, _validate_filename


class MarketplaceError(Exception):
    pass


# Baseline tooling pack (MCP marketplace wave 1, Task 6): a tool in this repo
# belongs to the pack every NEW agent ships with when its id carries this
# prefix. The whole selection rule, deliberately a naming convention rather
# than a new org-config key -- the deployed config loader rejects unknown org
# keys, so a key would have to land in every room's config file before any
# room could get a pack at all.
#
# It lives HERE, not in either consumer, because the prefix is one rule with
# two enforcement points that must never drift apart: `kobo_api.steps.
# _stage_baseline_tools` stages what matches it, and `kobo_api.mcp.tools.
# write.kobo_create_tool` reserves what matches it for room operators
# (publishing one is a room-wide act -- it changes what every agent born
# afterward carries -- not a team-scoped one like any other tool).
BASELINE_TOOL_PREFIX = "baseline-"


def marketplace_repo(config: Config) -> str:
    if not config.skills_repo:
        raise MarketplaceError("the marketplace repo is not configured")
    return f"{config.github_org}/{config.skills_repo}"


_FENCE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """(frontmatter dict, body) -- ({}, whole text) when no fence."""
    match = _FENCE.match(text)
    if not match:
        return {}, text
    try:
        data = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise MarketplaceError(f"invalid frontmatter: {exc}") from exc
    if not isinstance(data, dict):
        raise MarketplaceError("invalid frontmatter: not a mapping")
    return data, text[match.end():]


def _first_non_blank_line(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _render_frontmatter_doc(frontmatter: dict, body: str) -> str:
    # width=4096 turns off yaml.safe_dump's default 80-column folding
    # (these files are hand-edited by a person, not a machine-consumed
    # stream -- folding a long description across lines degrades it for
    # them) and allow_unicode=True stops it escaping non-ASCII characters
    # (a name/description/owner/tag with an accent or an emoji should
    # round-trip as itself, not a \uXXXX escape).
    dumped = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True, width=4096)
    return "---\n" + dumped + "---\n" + body


def list_tools(github: GitHubClient, config: Config) -> list[dict]:
    """One `{"id", "name", "description", "owner", "tags"}` per
    `tools/<id>/TOOL.md` in the marketplace repo, sorted by id. A
    directory under `tools/` with no `TOOL.md` is skipped, not an error --
    same "not every dir is a real entry" tolerance `kobo_api.tooling`'s
    own directory walks use. A `TOOL.md` that fails to parse (invalid YAML,
    or a frontmatter block that isn't a mapping) is ALSO skipped rather
    than raised: this repo is org-shared, so one team's malformed file
    (an unquoted colon, say) must never take listing down for every other
    team's tools too -- unlike `create_skill`/`create_tool`, where the
    caller IS the one team whose own submission failed to parse, and a
    raise is exactly the right, actionable answer.
    """
    repo = marketplace_repo(config)
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(repo, workdir)
        tools_dir = workdir / "tools"
        if not tools_dir.is_dir():
            return []

        results = []
        for entry in sorted(tools_dir.iterdir(), key=lambda p: p.name):
            if not entry.is_dir():
                continue
            tool_md = entry / "TOOL.md"
            if not tool_md.is_file():
                continue
            try:
                frontmatter, _ = _parse_frontmatter(tool_md.read_text(encoding="utf-8"))
            except MarketplaceError:
                continue
            results.append(
                {
                    "id": entry.name,
                    "name": frontmatter.get("name", entry.name),
                    "description": frontmatter.get("description", ""),
                    "owner": frontmatter.get("owner", ""),
                    "tags": frontmatter.get("tags", []),
                }
            )
        return results


def fetch_tool_py(github: GitHubClient, config: Config, tool_id: str) -> tuple[str, str]:
    """`(filename, content)` of `tools/<tool_id>/<tool_id>.py`.

    Raises `MarketplaceError` for an invalid `tool_id` shape (the same
    filename regex `kobo_api.tooling` upload/import use, so a path-
    traversal-shaped id is rejected before any clone) or when the file
    isn't there.
    """
    filename = f"{tool_id}.py"
    try:
        _validate_filename(filename)
    except ToolingError as exc:
        raise MarketplaceError(str(exc)) from exc

    repo = marketplace_repo(config)
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(repo, workdir)
        target = workdir / "tools" / tool_id / filename
        if not target.is_file():
            raise MarketplaceError(f"no such tool: {tool_id!r}")
        return filename, target.read_text(encoding="utf-8")


def create_skill(
    github: GitHubClient,
    config: Config,
    *,
    skill_id: str,
    content: str,
    team: str,
    author: str,
) -> str:
    """Publish `skills/<skill_id>/SKILL.md` to the marketplace repo by
    direct commit -- no review gate (module docstring). Validates, in
    order: the id shape, a frontmatter `owner` that names some OTHER team
    (a caller may not publish into a team it doesn't claim), and a
    non-empty body -- all before ever cloning. `name`/`owner` are always
    set to `skill_id`/`team` on the way out (frontmatter completed, not
    merely checked); `description` is filled from the body's first
    non-blank line when the incoming frontmatter doesn't already set one.

    Returns the pushed commit's local sha (`github.local_head`). Raises
    `MarketplaceError` if `skills/<skill_id>/` already exists in the repo
    -- a publish never overwrites another skill's slot.
    """
    try:
        _validate_skill_id(skill_id)
    except SkillsError as exc:
        raise MarketplaceError(str(exc)) from exc

    frontmatter, body = _parse_frontmatter(content)
    owner = frontmatter.get("owner")
    if owner is not None and owner != team:
        raise MarketplaceError(f"frontmatter owner {owner!r} conflicts with team {team!r}")

    frontmatter["name"] = skill_id
    frontmatter["owner"] = team
    if not frontmatter.get("description"):
        frontmatter["description"] = _first_non_blank_line(body)

    if not body.strip():
        raise MarketplaceError("skill content must have a non-empty body")

    repo = marketplace_repo(config)
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(repo, workdir)
        skill_dir = workdir / "skills" / skill_id
        # .exists(), not .is_dir(): a stray FILE already sitting at this
        # path (not a real skill directory) must raise the same typed
        # error here, rather than escape as a bare FileExistsError out of
        # the mkdir() below.
        if skill_dir.exists():
            raise MarketplaceError(f"skill {skill_id!r} already exists")
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            _render_frontmatter_doc(frontmatter, body), encoding="utf-8"
        )
        github.commit_push(workdir, f"kobo: publish skill {skill_id} (team {team}) for {author}")
        return github.local_head(workdir)


def create_tool(
    github: GitHubClient,
    config: Config,
    *,
    tool_id: str,
    content: str,
    team: str,
    description: str,
    tags: list[str],
    author: str,
) -> str:
    """Publish `tools/<tool_id>/TOOL.md` + `tools/<tool_id>/<tool_id>.py`
    to the marketplace repo by direct commit -- no review gate (module
    docstring). Filename shape and file content (`kobo_api.tooling`'s
    shared mechanical validator: size/UTF-8/non-empty/ast/import-allowlist/
    entrypoint) are both checked BEFORE any clone -- a tool that would fail
    the enforcement contract's own checks never reaches the repo at all.

    Returns the pushed commit's local sha (`github.local_head`). Raises
    `MarketplaceError` if `tools/<tool_id>/` already exists.
    """
    filename = f"{tool_id}.py"
    try:
        _validate_filename(filename)
        _validate_content(filename, content.encode("utf-8"))
    except ToolingError as exc:
        raise MarketplaceError(str(exc)) from exc

    repo = marketplace_repo(config)
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(repo, workdir)
        tool_dir = workdir / "tools" / tool_id
        # .exists(), not .is_dir() -- same file-in-the-way reasoning as
        # create_skill's collision check above.
        if tool_dir.exists():
            raise MarketplaceError(f"tool {tool_id!r} already exists")
        tool_dir.mkdir(parents=True)
        frontmatter = {
            "name": tool_id,
            "description": description,
            "owner": team,
            "tags": list(tags),
        }
        (tool_dir / "TOOL.md").write_text(
            _render_frontmatter_doc(frontmatter, ""), encoding="utf-8"
        )
        (tool_dir / filename).write_text(content, encoding="utf-8")
        github.commit_push(workdir, f"kobo: publish tool {tool_id} (team {team}) for {author}")
        return github.local_head(workdir)
