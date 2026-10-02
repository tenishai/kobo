# Agent Content Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Post-assembly skills editing (catalog picks + agent-local guardrails), one-shot memory intake with a crash-safe seeder ledger, and a `slack_operators` route gate.

**Architecture:** Three kobo API surfaces mirror the proven knowledge.py shape (stateless repo-clone modules + thin routes + the existing delivery-state poll); one small daedalus runtime-kit change adds an intake pass to the boot seeder; two new Svelte panels reuse the queries.ts factory + delivery-chip idioms. Spec: `docs/superpowers/specs/2026-08-13-agent-content-lifecycle-design.md`.

**Tech Stack:** FastAPI + SQLAlchemy JSON column (kobo api), stdlib-only seeder (daedalus/docker), Svelte 5 + TanStack Query (kobo ui).

## Global Constraints

- Vendor rule: NO client names in any tracked file — client names are treated like secret values. Before staging, plain-grep every file you touched against the name list at `.superpowers/sdd/vendor-names.txt` (untracked by design): `grep -i -f .superpowers/sdd/vendor-names.txt <files>` must return nothing. Plain grep, not git grep — git grep misses untracked files.
- Stage named files only: `git add <exact paths>` — never `git add -A`/`.`.
- TDD: write the failing test first, run it RED, implement, run GREEN. Report suite counts.
- kobo API tests: `cd api && venv/bin/python -m pytest -q` (baseline 723 green). kobo UI: `PATH="~/google-cloud-sdk/bin:/opt/homebrew/bin:/usr/local/bin:$PATH" make -C ui check` (svelte-check 0 errors + vitest green). daedalus seeder: `cd ../daedalus && python -m pytest docker/test_seeder.py -v` (stdlib-only, no techne needed).
- daedalus work goes on a feature branch; NO PR until Kyle says (standing rule).
- Error copy is plain language, no argv/stderr leakage; 502 bodies match the knowledge routes' wording family.
- `Agent.skills` JSON entries: reassign the whole list, never mutate dicts in place (SQLAlchemy JSON change tracking — see `_stage_skills`'s docstring).
- Skill entry shape on the DB row is `{"name": ..., "team": ..., "sha": ...}` (key is `name`, not `id`); new `source` key: absent or `"catalog"` = catalog, `"local"` = local.

---

### Task 1: skills.py module (repo-side operations)

**Files:**
- Create: `api/kobo_api/skills.py`
- Modify: `api/kobo_api/steps.py` (extract `_materialize_catalog_skill` from `_stage_skills`, lines 150-232)
- Test: `api/tests/test_skills.py`

**Interfaces:**
- Consumes: `GitHubClient.clone/commit_push/local_head` (clients.py), `StorageClient.list_keys/read_bytes/object_metadata` (clients.py), `Config.skills_bucket`.
- Produces (Task 2 depends on these exact signatures):
  - `SKILL_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")`
  - `SKILL_MAX_BYTES = 65536`
  - `class SkillsError(Exception)` with stable message prefixes: `"invalid skill id: "`, `"skill file content"`, `"skill not found: "`, `"skill frontmatter"`
  - `add_catalog_skill(github, storage, config, repo: str, skill_id: str, team: str) -> tuple[str, str]` — returns `(commit_sha, current_source_sha)`; raises `SkillsError("skill no longer in the catalog: ...")` when the bucket has no keys for it.
  - `remove_skill(github, repo: str, skill_id: str) -> str` — deletes `bundle/skills/<skill_id>/`, returns commit sha; `SkillsError("skill not found: ...")` if absent.
  - `read_local_skill(github, repo: str, skill_id: str) -> str` — content of `bundle/skills/<skill_id>/SKILL.md`; `SkillsError("skill not found: ...")` if absent.
  - `save_local_skill(github, repo: str, skill_id: str, content: str) -> str` — validates id, size, and frontmatter (`name:` + `description:` present in a leading `---` block), writes `bundle/skills/<skill_id>/SKILL.md`, returns commit sha.
  - In steps.py: `_materialize_catalog_skill(storage, config, team: str, skill_id: str, dest_dir: Path) -> str` — downloads the skill tree under `dest_dir` with the existing containment check, returns the bucket's current `source-sha` (empty string if unset). `_stage_skills` now calls it per entry; behavior identical.

- [ ] **Step 1: Extract the materializer (refactor with existing tests green)**

In `api/kobo_api/steps.py`, lift the body of `_stage_skills`'s per-entry loop (bucket listing, containment-checked download, `source-sha` metadata read — currently steps.py:186-222) into:

```python
def _materialize_catalog_skill(
    storage: StorageClient, config: Config, team: str, skill_id: str, dest_dir: Path
) -> str:
    """Download the catalog skill `<team>/<skill_id>/` into dest_dir.

    Same containment rule as before the extraction: a bucket key must not
    escape dest_dir. Returns the SKILL.md object's current source-sha
    custom metadata ("" when the publisher set none). Raises RuntimeError
    when the catalog no longer serves the skill.
    """
    prefix = f"{team}/{skill_id}/"
    keys = storage.list_keys(config.skills_bucket, prefix)
    if not keys:
        raise RuntimeError(f"skill no longer in the catalog: {team}/{skill_id}")
    resolved_dest = dest_dir.resolve()
    for key in keys:
        relative = key[len(prefix):]
        if not relative:
            continue
        target = dest_dir / relative
        if not target.resolve().is_relative_to(resolved_dest):
            raise RuntimeError(
                f"skill {skill_id!r} (team {team!r}) bucket key {key!r} "
                f"would download outside its staging directory; refusing to write it"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(storage.read_bytes(config.skills_bucket, key))
    try:
        metadata = storage.object_metadata(config.skills_bucket, f"{prefix}SKILL.md")
    except StorageNotFound:
        metadata = {}
    return metadata.get("metadata", {}).get("source-sha", "")
```

`_stage_skills` keeps its signature, drift bookkeeping, and `resolved_entries` reassignment — its loop body becomes `skill_dir.mkdir(...)` + `current_sha = _materialize_catalog_skill(storage, config, team, skill_id, skill_dir)`.

- [ ] **Step 2: Run the existing steps suite — must stay green**

Run: `cd api && venv/bin/python -m pytest tests/test_steps.py -q`
Expected: same pass count as before the refactor, zero failures.

- [ ] **Step 3: Write failing tests for the module**

`api/tests/test_skills.py`, following `api/tests/test_knowledge.py`'s idioms (FakeGitHub + tmp repo trees; FakeStorage seeded with `<team>/<id>/SKILL.md` objects and `source-sha` metadata). Cover at minimum:

```python
def test_add_catalog_skill_materializes_and_returns_current_sha(...):
    # FakeStorage carries shared/survey-field with source-sha "abc123"
    sha, source_sha = skills.add_catalog_skill(github, storage, config,
                                               "acme/agent-x", "survey-field", "shared")
    # clone shows bundle/skills/survey-field/SKILL.md, commit_push was called,
    # source_sha == "abc123"

def test_add_catalog_skill_gone_from_catalog_raises(...):
    # empty storage -> SkillsError "skill no longer in the catalog: shared/gone"

def test_remove_skill_deletes_directory(...):
def test_remove_skill_absent_raises_not_found(...):
def test_read_local_skill_roundtrip_and_not_found(...):
def test_save_local_skill_validates_id_size_frontmatter(...):
    # bad id 422-class error; >64KB rejected; missing name/description in
    # frontmatter rejected with "skill frontmatter" prefix; happy path returns sha
def test_save_local_skill_identical_content_still_returns_head(...):
```

- [ ] **Step 4: Run RED** — `venv/bin/python -m pytest tests/test_skills.py -q` → import errors/failures.

- [ ] **Step 5: Implement `api/kobo_api/skills.py`**

Mirror knowledge.py's structure exactly (module docstring, tempdir-per-call, no state). Frontmatter check for local saves:

```python
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)

def _validate_frontmatter(content: str) -> None:
    match = _FRONTMATTER_RE.match(content)
    if not match:
        raise SkillsError("skill frontmatter is missing — start the file with a --- block")
    block = match.group(1)
    for field in ("name", "description"):
        if not re.search(rf"^{field}\s*:\s*\S", block, re.MULTILINE):
            raise SkillsError(f"skill frontmatter needs a {field}: line")
```

`add_catalog_skill` clones, calls `_materialize_catalog_skill(storage, config, team, skill_id, workdir / "bundle" / "skills" / skill_id)` (wrap its `RuntimeError` into `SkillsError` preserving the message), commits `kobo: add skill {skill_id}`, returns `(github.local_head(workdir), source_sha)`. `remove_skill` uses `shutil.rmtree` on the skill dir, commit `kobo: remove skill {skill_id}`. `save_local_skill` commit `kobo: update skill {skill_id}`.

- [ ] **Step 6: Run GREEN** — `venv/bin/python -m pytest tests/test_skills.py tests/test_steps.py -q`.

- [ ] **Step 7: Vendor scan + commit**

```bash
grep -i -f .superpowers/sdd/vendor-names.txt api/kobo_api/skills.py api/kobo_api/steps.py api/tests/test_skills.py
git add api/kobo_api/skills.py api/kobo_api/steps.py api/tests/test_skills.py
git commit -m "kobo: skills module — catalog materialize/remove + local guardrail CRUD"
```

---

### Task 2: skills routes + Agent.skills provenance

**Files:**
- Modify: `api/kobo_api/main.py` (after the knowledge routes, ~line 1137)
- Test: `api/tests/test_web_skills.py`

**Interfaces:**
- Consumes: Task 1's module functions verbatim; `_ready_agent_or_error` (main.py); `app.state.clients.github/storage`; `app.state.config`.
- Produces (UI Task 6 relies on these exact shapes):
  - `GET /api/agents/{name}/skills` → `{"skills": [{"name", "team", "source", "sha", "editable"}]}` — `team` null and `sha` null for local entries; `source` defaulted to `"catalog"` when the stored entry has none; `editable` = `source == "local"`.
  - `POST /api/agents/{name}/skills/catalog` body `{"name": str, "team": str}` → `{"sha": <commit>}`; 409 `{"error": "the agent already carries a skill named ..."}` on either-class collision; 404 when the catalog no longer serves it.
  - `DELETE /api/agents/{name}/skills/{skill_id}` → `{"sha": ...}`; 404 when the agent doesn't carry it.
  - `GET /api/agents/{name}/skills/local/{skill_id}` → `{"name", "content"}`; 404 for catalog-class ids (content is not served for catalog skills) and missing ids.
  - `PUT /api/agents/{name}/skills/local/{skill_id}` body `{"content": str}` → `{"sha": ...}`; 409 if the id exists as catalog-class; 422 on validation failures.
  - Delivery polling reuses the existing `GET /api/agents/{name}/knowledge/delivery/{sha}` unchanged.

- [ ] **Step 1: Failing route tests** (`test_web_skills.py`, TestClient + fakes per `test_web.py` idioms): list shape incl. legacy entries without `source`; catalog add pins the sha served NOW (FakeStorage metadata differs from the assembly-era sha recorded on the agent row → response + DB entry carry the new sha); collision 409 both directions (local blocks catalog add, catalog blocks local PUT); unified delete updates JSON (reassigned list) for both classes; local GET/PUT happy + 404/422/409; 502 body copy `"couldn't reach your agent's files — try again"` when the github fake raises ClientError; non-ready agent gets the standard not-ready error.

- [ ] **Step 2: RED** — `venv/bin/python -m pytest tests/test_web_skills.py -q`.

- [ ] **Step 3: Implement the routes.** Pattern per route mirrors the knowledge routes verbatim (try/except SkillsError→422-or-mapped, ClientError→502 with the same copy). After a successful repo mutation, update the row: `agent.skills = [e for e in agent.skills if e["name"] != skill_id]` (+ append for adds: `{"name": skill_id, "team": team, "sha": source_sha, "source": "catalog"}` or `{"name": skill_id, "source": "local"}`), `session.commit()`. Collision check before any repo work: `any(e["name"] == skill_id for e in agent.skills)`.

- [ ] **Step 4: GREEN + full suite** — `venv/bin/python -m pytest -q` (expect 723 baseline + new).

- [ ] **Step 5: Vendor scan + commit** — `git add api/kobo_api/main.py api/tests/test_web_skills.py`, `git commit -m "kobo: skills routes — post-assembly catalog picks + local guardrails"`.

---

### Task 3: intake module + routes

**Files:**
- Create: `api/kobo_api/intake.py`
- Modify: `api/kobo_api/main.py` (routes after skills), `api/kobo_api/clients.py` (one new GitHubClient method, see below), `api/tests/fakes.py` (FakeGitHub support)
- Test: `api/tests/test_intake.py`, `api/tests/test_web_intake.py`

**Interfaces:**
- Consumes: `GitHubClient.clone/commit_push/local_head`, `KubectlClient.deploy_image`.
- Produces:
  - `INTAKE_FILENAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}\.(md|txt)$")`, `INTAKE_MAX_BYTES = 262144`
  - `class IntakeError(Exception)` (prefixes `"invalid intake filename: "`, `"intake file content"`, `"intake file not found: "`)
  - `list_intake(github, kubectl, *, repo, namespace, deploy) -> list[dict]` — each `{"name", "content_bytes": int, "status": "pending"|"ingested"}`.
  - `upload_intake_file(github, repo, filename, content) -> str` (commit sha; identical content no-ops but still returns head — same semantics as knowledge saves).
  - `delete_intake_file(github, repo, filename) -> str`.
  - New `GhCLI.commit_shas_for_paths(workdir, paths: list[str]) -> dict[str, str]` — `git log -1 --format=%H -- <path>` per path inside an existing clone, plus `GhCLI.is_ancestor(workdir, ancestor: str, descendant: str) -> bool` (`git merge-base --is-ancestor`, rc 0/1) and `GhCLI.rev_parse_prefix(workdir, prefix: str) -> str | None` (`git rev-parse <prefix>` tolerating unknown). Fakes model all three.
  - Routes: `GET /api/agents/{name}/intake` → `{"files": [...]}` per list_intake; `POST /api/agents/{name}/intake` body `{"filename", "content"}` → `{"sha"}`; `DELETE /api/agents/{name}/intake/{filename}` → `{"sha"}` with copy in the 200 body: `{"sha": ..., "note": "memories are kept — only the source file was removed"}`.

**Status derivation (the design's one subtle algorithm — implement exactly):** in `list_intake`, clone once; for each `bundle/intake/*` file get its last-touch commit via `commit_shas_for_paths`; read the live image tag via `kubectl.deploy_image(namespace, deploy)` and `rev_parse_prefix` the tag (12-hex) into a full sha inside the clone. A file is `"ingested"` iff the tag resolved AND `is_ancestor(file_commit, live_commit)` (equality counts — `merge-base --is-ancestor` accepts equal). Unresolvable tag (old image predating the clone's history is impossible — but an image built from a since-force-pushed sha is not) → `"pending"`. This is pure boot-ordering logic: if the live pod's commit contains the file, the seeder processed `intake/` before the agent served.

- [ ] **Step 1: Failing module tests** — status matrix (file at HEAD + image at HEAD → ingested; file newer than image → pending; tag unresolvable → pending), upload validation (`.md`/`.txt` only, 256KB cap, slug rules), delete not-found, identical-content upload returns head.
- [ ] **Step 2: RED.**
- [ ] **Step 3: Implement `intake.py` + the three GhCLI methods (+ fakes).** GhCLI methods are thin CommandRunner wraps in the existing style; `is_ancestor` treats rc 1 as False, other rcs raise ClientError.
- [ ] **Step 4: Failing route tests** (`test_web_intake.py`): shapes above, 502 copy `"couldn't reach your agent's files — try again"`, delete note copy verbatim, non-ready agent guard.
- [ ] **Step 5: RED, then implement routes, then GREEN + full suite.**
- [ ] **Step 6: Vendor scan + commit** — `git commit -m "kobo: memory intake — one-shot uploads with ancestor-derived ingest status"`.

---

### Task 4: seeder intake pass (daedalus, feature branch)

**Files:**
- Modify: `../daedalus/docker/seeder.py`
- Test: `../daedalus/docker/test_seeder.py`
- Branch: create `feat/intake-seeding` from the repo's current main-line HEAD. NO PR (standing rule).

**Interfaces:**
- Consumes: existing `_state_conn`, `parse_knowledge_file`, `type_for`, `store_fn` closure shape `store_fn(techne_db, content, type, tags, source) -> int`, `list_ids_fn(techne_db, source) -> list[int]`, `delete_fn(techne_db, entry_id)`.
- Produces: `ingest_intake(intake_dir: Path, techne_db: Path, state_db: Path, store_fn, delete_fn, list_ids_fn=None) -> dict` returning `{"ingested": n_entries, "skipped": n_files}`; a new `intake_state` table in the same `seed_state.db`; `main()` calls it after `sync()` and logs `"intake: ingested=%s skipped=%s"` only when the bundle has an `intake/` dir.

**Semantics (from the spec, strengthened):** one-shot append; ledger keyed by content sha; **filename reuse with new content is a new ingestion**; convergence NEVER removes intake-sourced rows; crash between stores and ledger-write converges WITHOUT duplicates because the techne `source` is sha-scoped: `f"intake:{filename}#{sha256[:12]}"` — a rerun finds the orphaned rows for that exact source via `list_ids_fn`, deletes them, re-stores, then writes the ledger row (duplicate-free version of the knowledge seeder's ground-truth pass; the spec's duplicate-tolerance line becomes moot).

- [ ] **Step 1: Failing tests** (stdlib-only, existing idioms: `FakeStore`, `_state_conn` inspection, `tmp_path` bundles):

```python
def test_intake_first_run_ingests_and_ledgers(...):
    # bundle/intake/notes.md with 2 sections -> 2 stores, tags "intake",
    # source "intake:notes.md#<sha12>", intake_state row (sha256, filename, entry_ids)
def test_intake_same_sha_skipped_across_runs(...):
def test_intake_filename_reuse_new_content_appends(...):
    # ledger keeps BOTH rows; first ingestion's ids untouched
def test_intake_file_deletion_removes_nothing(...):
    # run once, delete file, run again -> delete_fn never called, ledger intact
def test_intake_crash_window_converges_without_duplicates(...):
    # rows exist for the sha-scoped source but no ledger row -> old ids deleted,
    # re-stored, ledger written; total rows == one ingestion's worth
def test_intake_txt_files_ingest_whole_or_sectioned(...):
    # .txt goes through the same H2 split (a txt with no ## yields one section)
def test_main_logs_intake_counts_only_when_dir_exists(...):
def test_knowledge_convergence_ignores_intake_dir(...):
    # regression: files under intake/ never appear in seed_state or knowledge counts
```

- [ ] **Step 2: RED** — `python -m pytest docker/test_seeder.py -v -k intake`.
- [ ] **Step 3: Implement.** New `_INTAKE_STATE_SCHEMA` (`intake_state(sha256 TEXT PRIMARY KEY, filename TEXT NOT NULL, entry_ids TEXT NOT NULL)`) created by `_state_conn` alongside `seed_state`; `ingest_intake` globs `sorted(intake_dir.glob("*.md")) + sorted(intake_dir.glob("*.txt"))`; per file: sha256 → ledger hit = skip; else source = `f"intake:{path.name}#{digest[:12]}"`, ground-truth delete via `list_ids_fn` when provided, store each section (`type_for(path.name)`, tags `"intake"`), `INSERT OR REPLACE` ledger row, commit. `main()` wires the same closures it already builds and calls `ingest_intake(bundle / "intake", ...)`.
- [ ] **Step 4: GREEN full seeder suite** — `python -m pytest docker/test_seeder.py -v` (28 baseline + new all pass).
- [ ] **Step 5: Vendor scan + commit on the branch** — `git add docker/seeder.py docker/test_seeder.py`, `git commit -m "feat(seeder): one-shot intake pass — sha-ledgered, convergence-exempt"`.

**Controller follow-ups at milestone time (NOT this task):** build/push runtime image from the branch, bump `runtime_image` in the client org's config repo.

---

### Task 5: slack_operators gate

**Files:**
- Modify: `api/kobo_api/config.py` (field), `api/kobo_api/org.py` (ORG_KEYS + one line in the docs comment), `api/kobo_api/main.py` (gate helper + two slack routes + `get_agent`), `api/config.example.yaml`
- Test: `api/tests/test_web_slack_gate.py`

**Interfaces:**
- Produces: `Config.slack_operators: list[str] = []`; main.py helper:

```python
def _slack_operator_block(request: Request, config: Config) -> JSONResponse | None:
    """None when the caller may use slack routes; a 403 body otherwise.

    Empty list = ungated (dev parity). IAP disabled (no iap_audience) =
    operator: the room trusts its perimeter only when it has one.
    """
    if not config.slack_operators:
        return None
    if not config.iap_audience:
        return None
    email = (getattr(request.state, "iap_email", None) or "").lower()
    if email in {e.lower() for e in config.slack_operators}:
        return None
    return JSONResponse(
        {"error": "Slack setup is operator-only on this room"}, status_code=403
    )
```

  - Both slack routes call it first (before the 503 config checks). `get_agent` adds `"slack_visible": _slack_operator_block(request, config) is None` to the detail JSON (add `request: Request` to its signature).
- [ ] **Step 1: Failing tests:** empty-list open; non-empty + non-listed email 403 with exact copy; listed email (case-insensitive) passes through to the existing 503/flow; IAP-off dev mode passes; `slack_visible` true/false in detail accordingly; existing slack tests untouched-green.
- [ ] **Step 2: RED → implement → GREEN + full suite.**
- [ ] **Step 3: Add `slack_operators` to ORG_KEYS** (org config may carry it) + org round-trip test in the existing org test file's style; `config.example.yaml` gains a commented `# slack_operators: [ops@example.com]` line.
- [ ] **Step 4: Vendor scan + commit** — `git commit -m "kobo: slack_operators — route-enforced operator gate + slack_visible"`.

---

### Task 6: SkillsPanel (UI)

**Files:**
- Create: `ui/src/components/SkillsPanel.svelte`
- Modify: `ui/src/lib/api.ts` (types + calls), `ui/src/lib/queries.ts` (factories), `ui/src/pages/AgentDetail.svelte` (slot after MemoryPanel)
- Test: `ui/src/lib/queries.test.ts` (extend — factories only, per repo precedent; no component tests exist and this plan does not introduce a component-test layer)

**Interfaces:**
- Consumes: Task 2's route shapes verbatim; existing `deliveryQuery(name, sha)`, `deliveryRefetchInterval`, `errorMessage`, `skillsSearchQuery(q, team)`, `teamsQuery()`, `Modal`, `Skeleton`, `ErrorState`, `EmptyState`, `lineDiff`.
- Produces: `api.ts`: `AgentSkill = {name: string; team: string | null; source: "catalog"|"local"; sha: string | null; editable: boolean}`, `listSkills(name)`, `addCatalogSkill(name, skillName, team)`, `deleteSkill(name, skillId)`, `readLocalSkill(name, skillId)`, `saveLocalSkill(name, skillId, content)` (all returning parsed shapes; mutations return `{sha}`). `queries.ts`: `agentSkillsQuery(name)` (key `["agents", name, "skills"]`), `addCatalogSkillMutation(queryClient, name)`, `deleteSkillMutation(queryClient, name)`, `saveLocalSkillMutation(queryClient, name)` — each invalidating `["agents", name, "skills"]`.

- [ ] **Step 1: Failing factory tests** in `queries.test.ts` (existing `vi.stubGlobal("fetch")` + `callQueryFn` + fake-QueryClient idioms): URL/method/body per call, invalidation keys, error propagation through `parse()`.
- [ ] **Step 2: RED** — `make -C ui check` (with the PATH prefix).
- [ ] **Step 3: Implement api.ts + queries.ts additions; GREEN on the factory tests.**
- [ ] **Step 4: Build the panel.** Structure clones MemoryPanel: list rows show name + provenance line (`catalog · team/sha12` vs `local · editable`); catalog rows get Remove only; local rows Edit/Remove; an "Add from catalog" section reuses the debounced `writable`/`derived` search pattern from CreateModal (fresh inline markup, single-select — do NOT refactor CreateModal); a "New guardrail skill" flow opens the shared editor Modal with the same diff-preview + `beginDelivery(sha)` chip wiring MemoryPanel uses (copy the store-pair verbatim; `deliveryQuery` is shared). Slot into `AgentDetail.svelte`'s ready-block after `<MemoryPanel .../>`.
- [ ] **Step 5: Gate** — `svelte-check` 0 errors, vitest green, browser smoke deferred to the milestone.
- [ ] **Step 6: Vendor scan + commit** — `git commit -m "kobo: SkillsPanel — provenance-aware skill editing"`.

---

### Task 7: IntakePanel + Slack panel gating (UI)

**Files:**
- Create: `ui/src/components/IntakePanel.svelte`
- Modify: `ui/src/lib/api.ts`, `ui/src/lib/queries.ts`, `ui/src/pages/AgentDetail.svelte`
- Test: `ui/src/lib/queries.test.ts` (extend)

**Interfaces:**
- Consumes: Task 3's routes; Task 5's `slack_visible` on `AgentDetail`.
- Produces: `api.ts`: `IntakeFile = {name: string; content_bytes: number; status: "pending"|"ingested"}`, `listIntake(name)`, `uploadIntake(name, filename, content)`, `deleteIntake(name, filename)`; `AgentDetail` type gains `slack_visible: boolean`. `queries.ts`: `intakeQuery(name)` (key `["agents", name, "intake"]`, `refetchInterval` 5000 while any file is `"pending"`, else false — same shape as `deliveryRefetchInterval`), `uploadIntakeMutation`, `deleteIntakeMutation`.

- [ ] **Step 1: Failing factory tests → RED → implement → GREEN** (same cycle as Task 6, including the pending-only refetch-interval predicate test).
- [ ] **Step 2: Build the panel.** Upload = filename input (slug + `.md`/`.txt` hint) + textarea (paste-in; no binary file-picker in v1 — matches the text-only API) + size guard mirroring the server cap with client-side copy; rows show status chip (`pending` amber / `ingested` green, same tone classes as delivery chips); Remove enabled only when `ingested`, confirm copy: **"The file will be removed. Everything the agent learned from it stays."** Delete response's `note` renders as the success flash.
- [ ] **Step 3: Slack gating.** `AgentDetail.svelte`: `<SlackPanel .../>` renders only `{#if data.slack_visible}`. Final panel order in the ready-block: Chat, Memory (knowledge), Intake, Skills, Slack. Type change flows from api.ts.
- [ ] **Step 4: Gate green + vendor scan + commit** — `git commit -m "kobo: IntakePanel + operator-gated Slack panel"`.

---

### Task 8: Milestone — live run on the deployed room (controller-led)

**Files:** none (results committed to `docs/superpowers/` runbook afterward; screenshots under `.superpowers/sdd/smoke/`).

Pre-flight (controller): build kobo image from HEAD, roll the deployment; build the runtime image from the daedalus branch, push to the registry, bump `runtime_image` in the client org config repo (org-side value — the next delivery of the target agent picks it up).

Exit criteria, all against the production room, per the spec's milestone section:

- [ ] 1. Add an agent-local guardrail skill through the browser; after the delivery chip lands, a chat probe shows the behavior change.
- [ ] 2. Remove a catalog skill, add a different one; verify `bundle/skills/<id>/SKILL.md` bytes equal the bucket object at the pinned sha.
- [ ] 3. Upload an intake document; watch pending → ingested; chat proves recall; Remove; chat proves recall again (memories survived cleanup). Verify the intake ledger row in the pod (`seed_state.db`, `intake_state` table).
- [ ] 4. With `slack_operators` set to exclude the operator, the Slack panel disappears and a slack route 403s; re-listed, both restore.
- [ ] Results entry appended to the runbook + ledger; suites reported (API/UI/seeder counts).
