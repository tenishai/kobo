# MCP Marketplace Wave 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put skills and tooling on the MCP connector surface: list both catalogs, stage either into an owned agent, create new skills/tools by direct commit to the marketplace repo under a membership-gated team, and auto-stage a baseline tooling pack at agent create.

**Architecture:** One new module (`kobo_api/marketplace.py`) owns every marketplace-repo read/write, mirroring `kobo_api.skills`/`kobo_api.tooling` idioms (tempdir clone, validate-then-commit, typed error). MCP tools follow the existing split: read tools in `mcp/tools/read.py` (direct module calls), write tools in `mcp/tools/write.py` (route-handler calls via `_endpoint` wherever a room route already exists, so inline gates are inherited; direct marketplace-module calls only for the two creation lanes, which have no route). Baseline staging joins the compose path in `steps.py`.

**Tech Stack:** FastAPI, mcp>=1.15,<2 FastMCP, SQLAlchemy, pytest with the house fakes (`tests/fakes.py`). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-23-mcp-marketplace-wave1-design.md` — the operator rulings there govern any ambiguity.

## Global Constraints

- Vendor scan every commit: no name from `.superpowers/sdd/vendor-names.txt` in any tracked file (specs, plans, code, tests). Gate with a real exit flag, not a print.
- No em dashes in user-facing copy (tool descriptions, error messages); `--` in docstrings/comments is fine.
- Run the API suite (`cd api && venv/bin/python -m pytest -q`) before every commit; it must be green (2484 at plan time).
- `venv/` is the real environment, NOT `.venv/` (stale, lacks the mcp SDK).
- Ruff: repo has pre-existing drift; your changed files must add zero new findings (`venv/bin/ruff check <files>` on base vs head).
- MCP write tools that touch an agent must normalize 403/404 to the byte-identical "agent not found" ToolError (anti-enumeration), matching `mcp/tools/write.py`'s existing table.
- The marketplace repo name comes from `config.skills_repo` (bare name) prefixed by `config.github_org`. Never hardcode either.
- All new MCP tools get `@audited` and ride the existing per-email rate limit; `content` params join the audit redact set (never stored raw).

---

### Task 1: `kobo_api/marketplace.py` — the marketplace repo door

**Files:**
- Create: `api/kobo_api/marketplace.py`
- Test: `api/tests/test_marketplace.py`

**Interfaces:**
- Consumes: `GitHubClient` protocol (`clone(repo, dest)`, `commit_push(workdir, message)`, `local_head(workdir)` — `kobo_api/clients.py:190`), `kobo_api.tooling._validate_content` / `_validate_filename`, `kobo_api.skills._validate_skill_id` (house-normal cross-module underscore import, precedent `kobo_api/main.py:27`).
- Produces (later tasks call these exact signatures):
  - `class MarketplaceError(Exception)`
  - `marketplace_repo(config) -> str` — `"{github_org}/{skills_repo}"`, raises `MarketplaceError("the marketplace repo is not configured")` when `config.skills_repo` is falsy.
  - `list_tools(github, config) -> list[dict]` — one `{"id", "name", "description", "owner", "tags"}` per `tools/<id>/TOOL.md`, sorted by id.
  - `fetch_tool_py(github, config, tool_id) -> tuple[str, str]` — `(filename, content)` of `tools/<tool_id>/<tool_id>.py`; `MarketplaceError` if absent.
  - `create_skill(github, config, *, skill_id, content, team, author) -> str` — commit sha.
  - `create_tool(github, config, *, tool_id, content, team, description, tags, author) -> str` — commit sha.

- [ ] **Step 1: Write the failing tests**

```python
# api/tests/test_marketplace.py
"""The marketplace door: list/fetch the tools tree, publish skills and
tools by direct commit. All through FakeGitHub -- seed the fake's clone
tree the same way test_tooling.py's import tests seed a source repo
(read that file's fixtures first and reuse its idiom)."""

import pytest

from kobo_api.marketplace import (
    MarketplaceError,
    create_skill,
    create_tool,
    fetch_tool_py,
    list_tools,
    marketplace_repo,
)

TOOL_MD = """---
name: baseline-leakguard
description: Redacts credential-shaped strings from every reply.
owner: sre
tags: [security]
---
"""

TOOL_PY = (
    '"""Baseline leakguard."""\n'
    "import re\n"
    "def post_draft(draft):\n"
    "    return re.sub(r'xox[abps]-[A-Za-z0-9-]{10,}', '[redacted]', draft)\n"
)

GOOD_SKILL = "# PTR primer\n\nA PTR record maps an IP back to a hostname.\n"


def test_marketplace_repo_requires_config(config):
    config.skills_repo = None
    with pytest.raises(MarketplaceError):
        marketplace_repo(config)


def test_list_tools_reads_tool_md_frontmatter(config, marketplace_github):
    tools = list_tools(marketplace_github, config)
    assert [t["id"] for t in tools] == ["baseline-leakguard"]
    assert tools[0]["owner"] == "sre"
    assert tools[0]["description"].startswith("Redacts")


def test_fetch_tool_py_returns_filename_and_content(config, marketplace_github):
    filename, content = fetch_tool_py(marketplace_github, config, "baseline-leakguard")
    assert filename == "baseline-leakguard.py"
    assert "post_draft" in content


def test_fetch_tool_py_unknown_id_raises(config, marketplace_github):
    with pytest.raises(MarketplaceError):
        fetch_tool_py(marketplace_github, config, "nope")


def test_create_skill_injects_owner_frontmatter_and_commits(config, marketplace_github):
    sha = create_skill(
        marketplace_github, config,
        skill_id="ptr-primer", content=GOOD_SKILL,
        team="infra-engineering", author="user@example.com",
    )
    assert sha
    written = marketplace_github.pushed_file("skills/ptr-primer/SKILL.md")
    assert written.startswith("---\n")
    assert "owner: infra-engineering" in written
    assert "PTR record" in written


def test_create_skill_rejects_conflicting_owner_frontmatter(config, marketplace_github):
    content = "---\nname: x\nowner: other-team\n---\nbody\n"
    with pytest.raises(MarketplaceError, match="owner"):
        create_skill(
            marketplace_github, config,
            skill_id="x", content=content,
            team="infra-engineering", author="user@example.com",
        )


def test_create_skill_collision_raises(config, marketplace_github):
    # marketplace_github's tree already carries skills/existing-skill/SKILL.md
    with pytest.raises(MarketplaceError, match="already exists"):
        create_skill(
            marketplace_github, config,
            skill_id="existing-skill", content=GOOD_SKILL,
            team="infra-engineering", author="user@example.com",
        )


def test_create_tool_validates_py_before_any_commit(config, marketplace_github):
    with pytest.raises(MarketplaceError, match="neither pre_task nor post_draft"):
        create_tool(
            marketplace_github, config,
            tool_id="broken", content="x = 1\n",
            team="infra-engineering", description="d", tags=[],
            author="user@example.com",
        )
    assert marketplace_github.push_count == 0  # validate-then-commit ordering


def test_create_tool_writes_tool_md_and_py(config, marketplace_github):
    sha = create_tool(
        marketplace_github, config,
        tool_id="my-guard", content=TOOL_PY,
        team="infra-engineering", description="A guard.", tags=["security"],
        author="user@example.com",
    )
    assert sha
    md = marketplace_github.pushed_file("tools/my-guard/TOOL.md")
    assert "owner: infra-engineering" in md and "A guard." in md
    assert marketplace_github.pushed_file("tools/my-guard/my-guard.py") == TOOL_PY
```

The `marketplace_github` fixture: a `FakeGitHub` whose `clone` materializes a marketplace tree (`skills/existing-skill/SKILL.md`, `tools/baseline-leakguard/{TOOL.md,<id>.py}` from the constants above) instead of agent scaffolding, plus two tiny test-only helpers (`pushed_file(relpath)`, `push_count`) recorded off `commit_push`. Extend `FakeGitHub` in `tests/fakes.py` with an optional `clone_tree: dict[str, str] | None` constructor arg (path -> content; when set, `clone` writes exactly that tree) and record pushed trees on `commit_push` — follow the class's existing snapshot pattern (`_snapshot_pushed_content`, `fakes.py:510`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd api && venv/bin/python -m pytest tests/test_marketplace.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'kobo_api.marketplace'`

- [ ] **Step 3: Implement `kobo_api/marketplace.py`**

```python
"""The marketplace repo door: list and fetch the shared tools tree,
publish new skills and tools by direct commit (operator ruling 1 in the
wave-1 spec: no review gate; mechanical validation runs BEFORE any
commit, attribution rides the commit message, the MCP audit log records
the call). Mirrors kobo_api.skills / kobo_api.tooling: tempdir clone,
validate-then-commit, one typed error class."""

import re
import tempfile
from pathlib import Path

import yaml

from kobo_api.config import Config
from kobo_api.skills import _validate_skill_id
from kobo_api.tooling import ToolingError, _validate_content, _validate_filename


class MarketplaceError(Exception):
    pass


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
```

`list_tools` / `fetch_tool_py`: clone `marketplace_repo(config)` into a tempdir, scan `tools/*/TOOL.md` (parse frontmatter; skip and continue on a dir without TOOL.md), or read `tools/<id>/<id>.py` (validate `tool_id` with `_validate_filename(f"{tool_id}.py")` first; raise `MarketplaceError(f"no such tool: {tool_id!r}")` when absent).

`create_skill`: `_validate_skill_id(skill_id)` (re-raise `SkillsError` as `MarketplaceError`, message preserved); parse frontmatter; if it names an `owner` different from `team`, raise `MarketplaceError("frontmatter owner {x!r} conflicts with team {team!r}")`; inject/complete `name` (= skill_id), `owner` (= team), `description` (first non-blank body line when absent); require a non-empty body. Clone; if `skills/<skill_id>/` exists raise `MarketplaceError(f"skill {skill_id!r} already exists")`; write `skills/<skill_id>/SKILL.md` (frontmatter re-serialized with `yaml.safe_dump`, then body); `commit_push(workdir, f"kobo: publish skill {skill_id} (team {team}) for {author}")`; return `github.local_head(workdir)`.

`create_tool`: `_validate_filename(f"{tool_id}.py")` then `_validate_content(f"{tool_id}.py", content.encode("utf-8"))` (re-raise `ToolingError` as `MarketplaceError`, message preserved) BEFORE the clone; collision on `tools/<tool_id>/`; write `TOOL.md` (name/description/owner/tags frontmatter) and `<tool_id>.py`; same commit-message shape and return.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd api && venv/bin/python -m pytest tests/test_marketplace.py tests/test_tooling.py -q`
Expected: PASS (tooling suite proves the fakes extension broke nothing)

- [ ] **Step 5: Full suite, vendor scan, commit**

```bash
cd api && venv/bin/python -m pytest -q && cd ..
# vendor scan (exit-flag gate), then:
git add api/kobo_api/marketplace.py api/tests/test_marketplace.py api/tests/fakes.py
git commit -m "feat(marketplace): the marketplace repo door -- list/fetch tools, publish skills and tools"
```

---

### Task 2: MCP read tools — `kobo_list_skills` and `kobo_list_tools`

**Files:**
- Modify: `api/kobo_api/mcp/tools/read.py`
- Test: `api/tests/test_mcp_tools_read.py`

**Interfaces:**
- Consumes: `read_marketplace_catalog(storage, bucket)` (`kobo_api/search.py`), `marketplace.list_tools` (Task 1), the module's existing `register(mcp, app)` / `@audited` / closure-over-`app` pattern (read `read.py` top-to-bottom first; the context-injection decorator-order note at `mcp/server.py:41` is load-bearing).
- Produces: MCP tools `kobo_list_skills()` and `kobo_list_tools()`, both returning `{"result": [...]}`.

- [ ] **Step 1: Write the failing tests** (in `test_mcp_tools_read.py`, reusing its app/ctx fixtures)

```python
async def test_list_skills_returns_catalog_entries(mcp_app, mcp_ctx):
    # mcp_app's FakeStorage serves a marketplace-dialect catalog with one
    # skill: {"skills": {"ptr-primer": {"name": "ptr-primer", "version":
    # "1.0.0", "description": "d", "when_to_use": "w", "tags": ["net"],
    # "owner": "sre", ...}}} -- seed it the way test_api_skills seeds its
    # catalog fixtures.
    result = await call_tool(mcp_app, "kobo_list_skills", {})
    entry = result["result"][0]
    assert entry == {
        "id": "ptr-primer", "description": "d", "when_to_use": "w",
        "tags": ["net"], "owner": "sre", "version": "1.0.0",
    }


async def test_list_tools_reads_the_marketplace_tree(mcp_app, mcp_ctx):
    result = await call_tool(mcp_app, "kobo_list_tools", {})
    assert [t["id"] for t in result["result"]] == ["baseline-leakguard"]


async def test_registered_tool_set_grows_to_twelve(mcp_app):
    # extend the existing exact-set test: + kobo_list_skills, kobo_list_tools
    ...  # modify the EXISTING exact-set assertion in place, not a new test
```

- [ ] **Step 2: Run to verify failure** — `venv/bin/python -m pytest tests/test_mcp_tools_read.py -q` → FAIL (unknown tool).

- [ ] **Step 3: Implement.** Register both tools in `read.py`'s `register(mcp, app)`. Descriptions carry the recommendation protocol verbatim:

```python
@mcp.tool()
@audited("kobo_list_skills")
async def kobo_list_skills(ctx: Context) -> dict:
    """List every skill in the org marketplace: id, description,
    when to use it, tags, owning team, version. Before creating a
    new skill, ALWAYS list and check fit here first -- prefer an
    existing skill over a near-twin. Any signed-in caller may list.
    """
    catalog = read_marketplace_catalog(clients.storage, config.skills_bucket)
    ...
```

(`kobo_list_tools` mirrors it over `marketplace.list_tools(clients.github, config)`, description: "the shared tooling catalog... a new agent should always carry tooling; see kobo_add_agent_tool and kobo_create_tool".) Both wrap module errors into plain `ToolError(str(exc))` -- no visibility normalization needed (org-visible catalogs).

- [ ] **Step 4: Verify pass** — same command, PASS, including the amended exact-set test.
- [ ] **Step 5: Full suite, vendor scan, commit** (`feat(mcp): list the skills and tooling marketplaces`).

---

### Task 3: MCP `kobo_set_agent_skills`

**Files:**
- Modify: `api/kobo_api/mcp/tools/write.py`
- Test: `api/tests/test_mcp_tools_write.py`

**Interfaces:**
- Consumes: `_endpoint(app, "POST", "/api/agents/{name}/skills/catalog")` and `_endpoint(app, "DELETE", "/api/agents/{name}/skills/{skill_id}")` (route handlers at `main.py:2851`/`main.py:2914` — they own the gates, 409 collision, and the DB pick recording at `main.py:2907`), the module's existing status→ToolError table.
- Produces: MCP tool `kobo_set_agent_skills(agent, add=[], remove=[])` → `{"added": [...], "removed": [...]}` where each add entry is `{"id", "sha"}`.

- [ ] **Step 1: Failing tests.** In `test_mcp_tools_write.py`'s idiom (seeded agent owned by the caller, FakeStorage marketplace catalog, FakeGitHub):

```python
async def test_set_agent_skills_adds_and_records_db_picks(...):
    result = await call_tool(app, "kobo_set_agent_skills",
                             {"agent": "ward", "add": ["ptr-primer"]})
    assert result["added"][0]["id"] == "ptr-primer"
    agent = session.query(Agent).filter_by(name="ward").one()
    assert agent.skills[0]["name"] == "ptr-primer"   # the canary seam, closed

async def test_set_agent_skills_remove(...):          # DELETE path
async def test_set_agent_skills_conflict_is_tool_error(...):   # re-add -> ToolError w/ 409 msg
async def test_set_agent_skills_invisible_agent_not_found(...):  # stranger caller -> "agent not found"
async def test_set_agent_skills_team_resolved_from_catalog(...):
    # the route needs payload.team: the tool resolves it from the
    # marketplace catalog entry's owner -- callers pass ids only
```

Also grow the registered-tool exact-set test by this task's tool name — it is an EXACT set and will fail otherwise (same applies in Tasks 4 and 5; each task adds its own names).

- [ ] **Step 2: RED** — unknown tool.
- [ ] **Step 3: Implement.** The tool loops `add` then `remove`; for each add it reads the catalog once (`read_marketplace_catalog`) to resolve the entry's `owner` as the route's `team` payload field (unknown id → `ToolError(f"no such skill: {id!r}")` before any route call); calls the handlers via `_endpoint`; maps responses through the existing table (403/404 → "agent not found", 409/422 → route message). Partial application is acceptable and reported: entries already applied stay applied; the error names the id that failed.
- [ ] **Step 4: GREEN.**
- [ ] **Step 5: Full suite, vendor scan, commit** (`feat(mcp): stage marketplace skills on an owned agent`).

---

### Task 4: MCP `kobo_add_agent_tool`

**Files:**
- Modify: `api/kobo_api/mcp/tools/write.py`
- Test: `api/tests/test_mcp_tools_write.py`

**Interfaces:**
- Consumes: `_endpoint(app, "POST", "/api/agents/{name}/tooling")` (upload route, `main.py:3366` — owns gate + validator + runtime re-pin), `marketplace.fetch_tool_py` (Task 1).
- Produces: MCP tool `kobo_add_agent_tool(agent, tool_id=None, filename=None, content=None)` → `{"filename", "sha"}`.

- [ ] **Step 1: Failing tests.**

```python
async def test_add_agent_tool_from_marketplace(...):
    # tool_id path: fetch_tool_py -> upload route; response carries filename+sha
async def test_add_agent_tool_inline_content(...):
    # filename+content path hits the same route; bad hook content ->
    # ToolError carrying the validator's exact message
async def test_add_agent_tool_requires_exactly_one_source(...):
    # neither, or both, of tool_id / (filename+content) -> ToolError
async def test_add_agent_tool_invisible_agent_not_found(...):
```

- [ ] **Step 2: RED.** — unknown tool.
- [ ] **Step 3: Implement.** Source arbitration first (exactly one of `tool_id` | `filename`+`content`); marketplace path resolves `(filename, content)` via `fetch_tool_py` (a `MarketplaceError` → `ToolError`, message preserved); both paths call the upload route handler; status table as everywhere. `content` joins the audit redact set.
- [ ] **Step 4: GREEN.**
- [ ] **Step 5: Full suite, vendor scan, commit** (`feat(mcp): stage tooling on an owned agent -- marketplace or inline`).

---

### Task 5: MCP `kobo_create_skill` and `kobo_create_tool`

**Files:**
- Modify: `api/kobo_api/mcp/tools/write.py`
- Test: `api/tests/test_mcp_tools_write.py`

**Interfaces:**
- Consumes: `marketplace.create_skill` / `create_tool` (Task 1), `get_user_email(ctx)` (`mcp/server.py`), `clients.groups.groups_for(email) -> list | None`, `authz.is_admin(app, email)`.
- Produces: `kobo_create_skill(skill_id, content, team)` and `kobo_create_tool(tool_id, content, team, description, tags=[])`, both → `{"commit": sha, "note": "appears in the catalog after the publish pipeline and the room's catalog refresh (minutes)"}`.

- [ ] **Step 1: Failing tests.** The membership matrix is the point — pin every branch:

```python
async def test_create_skill_member_of_team_commits(...):        # groups contain team -> commit sha returned
async def test_create_skill_non_member_refused(...):            # ToolError "you are not a member of team ..."
async def test_create_skill_resolver_down_fail_closed(...):     # groups_for -> None => refused (unless operator)
async def test_create_skill_operator_any_team(...):             # is_admin -> True bypasses membership
async def test_create_skill_collision_surfaces(...):            # MarketplaceError -> ToolError, message preserved
async def test_create_tool_invalid_hook_never_commits(...):     # validator message surfaces; FakeGitHub.push_count == 0
async def test_create_content_redacted_in_audit(...):           # audit row's args show content as [redacted]
```

- [ ] **Step 2: RED.** — unknown tools.
- [ ] **Step 3: Implement.** One shared `_require_team_membership(app, clients, email, team)` helper inside `write.py`: operator (`authz.is_admin`) short-circuits; otherwise `groups_for(email)` must contain `team` (a `None` resolve → `ToolError("directory lookup unavailable; try again")` — fail closed, matching the room's posture). Then straight to the marketplace module with `author=email`; wrap `MarketplaceError` as `ToolError`, message preserved. Tool descriptions carry the protocol: "Before creating, run kobo_list_skills (or kobo_list_tools) and check fit -- prefer an existing entry. The skill lands under YOUR team and publishes immediately, org-wide."
- [ ] **Step 4: GREEN.**
- [ ] **Step 5: Full suite, vendor scan, commit** (`feat(mcp): publish skills and tools to the marketplace, membership-gated`).

---

### Task 6: Baseline pack at create — compose staging + `skip_baseline`

**Files:**
- Modify: `api/kobo_api/steps.py` (the function that calls `_stage_skills` — locate with `grep -n "_stage_skills(" api/kobo_api/steps.py`), `api/kobo_api/models.py` (Agent), `api/kobo_api/main.py` (create route payload), `api/kobo_api/mcp/tools/write.py` (`kobo_create_agent`)
- Test: `api/tests/test_steps.py` (or the compose-step test module — find with `grep -rln "_stage_skills" api/tests/`), `api/tests/test_mcp_tools_write.py`

**Interfaces:**
- Consumes: `marketplace.marketplace_repo` + `GitHubClient.clone`, the compose dir layout (`bundle/tools/` beside `bundle/skills/`).
- Produces: `Agent.skip_baseline: Mapped[bool]` column (Boolean, `server_default=sa.false()`, nullable=False); `_stage_baseline_tools(github, config, bundle_dir) -> list[str]` (staged filenames; empty + a logged warning on any failure); create payload field `skip_baseline: bool = False`; `kobo_create_agent(..., skip_baseline: bool = False)`.

- [ ] **Step 1: Failing tests.**

```python
def test_compose_stages_baseline_pack_by_default(...):
    # marketplace fake tree carries tools/baseline-leakguard + a
    # non-baseline tools/other-tool; after compose, the agent repo tree has
    # bundle/tools/baseline-leakguard.py and NOT other-tool.py
def test_compose_skip_baseline_stages_nothing(...):
def test_compose_marketplace_unreachable_warns_and_proceeds(...):
    # FakeGitHub.clone raising for the marketplace repo -> create still
    # completes; caplog carries "baseline tooling skipped"
async def test_mcp_create_agent_passes_skip_baseline(...):
```

- [ ] **Step 2: RED.**
- [ ] **Step 3: Implement.** `_stage_baseline_tools` clones the marketplace repo (own tempdir), copies every `tools/baseline-*/baseline-*.py` into the compose dir's `bundle/tools/`, returns the filenames; ANY exception → `log.warning("baseline tooling skipped for %s: %s", ...)` and `[]` (spec: an agent must not fail to exist because the marketplace hiccuped). Call it right after `_stage_skills(...)` in the same compose function, gated on `not agent.skip_baseline`. Column + ALTER: follow Task A2's precedent (`task-a2-report.md` documents the rollout ALTER pattern); document `ALTER TABLE agents ADD COLUMN skip_baseline BOOLEAN NOT NULL DEFAULT 0` in the task report. Create route: `skip_baseline: bool = False` on the payload model, written to the row at create. MCP `kobo_create_agent` grows the param and passes it through the route body.
- [ ] **Step 4: GREEN.**
- [ ] **Step 5: Full suite, vendor scan, commit** (`feat(create): every new agent ships the baseline tooling pack -- skip_baseline opts out`).

---

### Task 7: Seed the marketplace `tools/` tree (baseline pack)

Operational, not kobo code — executed by the controller session against the marketplace repo clone (SSH lane), NOT by a subagent.

**Files (in the marketplace repo):**
- Create: `tools/baseline-leakguard/TOOL.md`, `tools/baseline-leakguard/baseline-leakguard.py`
- Create: `tools/baseline-probe-guard/TOOL.md`, `tools/baseline-probe-guard/baseline-probe-guard.py`

- [ ] **Step 1:** Generalize the two canary hooks verbatim from `agent-canary`'s `bundle/tools/` (they are already stdlib-only and validator-clean; rename the files to the `baseline-` ids, docstrings updated to say "baseline pack"). Each `TOOL.md`: frontmatter `name`, `description`, `owner: sre`, `tags: [security]`, plus a one-paragraph body on what the hook does and how to opt out (`skip_baseline`).
- [ ] **Step 2:** Validate both `.py` files through `kobo_api.tooling._validate_content` locally before committing (one-liner per file in the API venv).
- [ ] **Step 3:** Commit to the marketplace repo: `kobo: seed the shared tools tree -- baseline pack (leakguard, probe-guard)`. NOTE: needs the operator's grant if pushed by the room's App; the controller's SSH lane works today.

---

### Task 8: Docs + registers

**Files:**
- Modify: `README.md` (the MCP bullet gains "browse and publish the skills/tooling marketplaces, staged tooling on every new agent"), `docs/superpowers/specs/2026-08-23-mcp-marketplace-wave1-design.md` (Status → implemented)

- [ ] **Step 1:** Make both edits; plain language, no capability overclaim (creation is instant-publish — say so).
- [ ] **Step 2:** Vendor scan; full suite one last time.
- [ ] **Step 3:** Commit (`docs: the room speaks marketplace`).

---

## Out of plan (session-driven after merge)

- Wire acceptance from a live connector session (spec's testing section): create a skill under the caller's team, list it, stage it to a test agent; create a tool, add it, prove the hook; fresh agent ships baseline attested on its card.
- Operator opens: GitHub App write access to the marketplace repo; heads-up to its owner about the `tools/` tree and bot commits.
- Rollout: image build + roll, then wire proofs — same digest-verify discipline as every wave.
