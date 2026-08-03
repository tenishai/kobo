# Phase A — Org Catalogs, Persona Builder & Client Bootstrap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One bootstrap command wires a client org into pnyx (org-config repo, per-catalog buckets, skills publish workflow, seeded personas), and members then assemble agents from their org's own catalogs — bucket-published skills behind instant FTS5 search, and personas that are shipped defaults or built on the spot from a fixed form.

**Architecture:** Two GCS buckets per org (`<org>-skills` CI-written, `<org>-personas` room-written), each fronted by an `index.json` the API ingests into an SQLite FTS5 table behind `/api/skills/search` + `/api/personas/search`. Org-level configuration moves into a `pnyx-config` repo in the client org, fetched and merged over local config at boot (org wins for org concerns; a missing org repo leaves the room honestly "unwired"). Skill materialization at provision time reads the bucket, not a git clone. The bootstrap is an idempotent CLI (`python -m pnyx_api.bootstrap`) whose steps report done/skipped/failed and converge on re-run; `GET /api/org/status` is the room's read-only visibility into the wiring. New storage access goes through a `StorageClient` Protocol (GCS JSON API over httpx, `gcloud auth print-access-token` token seam — the `VertexImageClient` pattern verbatim).

**Tech Stack:** unchanged (FastAPI/pytest/SQLAlchemy+SQLite; Svelte 5 runes/@tanstack/svelte-query 5/vitest). No new API deps (GCS via httpx JSON API). The publish workflow's script uses `google-cloud-storage` — but only inside the client repo's Action; pnyx's suite tests its pure functions with a duck-typed fake and never imports the SDK.

## Global Constraints

- Never the employer's name or work-app name in any tracked file (plain `grep -i` the vendor names on files BEFORE staging — git grep misses untracked files). Zero infra vocabulary in UI copy. Operator vocabulary is permitted in the bootstrap CLI's output ONLY.
- TDD; `make -C api check` and `make -C ui check` fully green before every commit; **git add only named files**.
- Catalog machinery is `(bucket, prefix)`-generic — prefix defaults to `""` everywhere; the tools phase adds a third catalog without rework.
- Skills bucket is CI-written only; personas bucket is room-written. The room never writes the skills bucket.
- Persona rendering is deterministic string templating — no model call; the rendered template is byte-pinned in a test.
- FTS5, not Elastic. UI depends only on the search endpoints. The FTS table is SQLite-only by design (dev runs SQLite; documented scale-up path is swapping internals behind the same endpoints).
- Bootstrap idempotence is pinned by test: second run reports every wiring step `skipped`.
- Config precedence: org-repo values override local values for org concerns; machine-local keys (`workdir`, `db_url`, `slack_tokens_path`, `org_repo`, `api_base_url`, `gojira_url`, `gojira_token`) never come from the org repo.
- House error contract: `except ClientError` → 502 with plain-language `{"error": ...}`; unconfigured client → 503; the token-safe `RequestValidationError` handler already covers new routes.
- Existing agents' bundles are NOT migrated (spec non-goal). Dev DB gets a manual `ALTER TABLE agents ADD COLUMN team VARCHAR;` (controller follow-up before Task 5 lands on the running stack).

---

### Task 1: API — StorageClient (GCS JSON API) + FakeStorage

**Files:** `api/pnyx_api/clients.py` (Protocol + `HttpxStorageClient` + `StorageNotFound` + `Clients` wiring), `api/tests/fakes.py` (`FakeStorage`), `api/tests/test_clients.py` (MockTransport request-shape tests).

**Interfaces:**
```python
class StorageNotFound(ClientError):
    """404 from object storage — callers branch on 'absent' vs 'broken'."""

@runtime_checkable
class StorageClient(Protocol):
    def read_bytes(self, bucket: str, key: str) -> bytes: ...
        # GET https://storage.googleapis.com/storage/v1/b/{bucket}/o/{quote(key, safe='')}?alt=media
        # 404 -> StorageNotFound; other non-200 -> ClientError
    def object_metadata(self, bucket: str, key: str) -> dict:
        """Full object resource JSON; custom metadata under result.get('metadata', {}),
        server fields like 'updated' at top level. 404 -> StorageNotFound."""
    def write_bytes(self, bucket: str, key: str, data: bytes, *,
                    metadata: dict[str, str] | None = None,
                    content_type: str = "application/octet-stream") -> None: ...
        # POST https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o?uploadType=multipart
        # multipart/related: part 1 JSON {"name": key, "contentType": ..., "metadata": {...}}, part 2 media
    def list_keys(self, bucket: str, prefix: str) -> list[str]: ...
        # GET .../b/{bucket}/o?prefix=...&fields=items(name),nextPageToken — follow pagination; [] on empty
        # bucket missing (404) -> StorageNotFound
    def ensure_bucket(self, bucket: str) -> str: ...
        # GET .../b/{bucket} -> "exists"; on 404: POST .../b?project={self._project}
        #   body {"name": bucket, "versioning": {"enabled": true}} -> "created"
    def ensure_versioning(self, bucket: str) -> bool: ...
        # GET .../b/{bucket}?fields=versioning; if not enabled:
        #   PATCH .../b/{bucket} body {"versioning": {"enabled": true}} -> True (changed); else False

class HttpxStorageClient:
    def __init__(self, project: str, http_client: httpx.Client | None = None,
                 token_provider: Callable[[], str] | None = None): ...
    # token_provider defaults to _gcloud_print_access_token (existing seam);
    # Authorization: Bearer per request; timeout 20s; errors wrap like the other httpx clients —
    # never echo response bodies into user-visible messages, log-only.
```
- `Clients` gains `storage: StorageClient | None = None`; `Clients.real()` wires `storage=HttpxStorageClient(config.gcp_project)` unconditionally (same move as `image`).
- `FakeStorage`: `buckets: dict[str, dict[str, tuple[bytes, dict]]]` seeded via constructor kwarg; records `.calls`; per-method error knobs (`read_error`, `write_error`, ...) raising `ClientError`; `read_bytes`/`object_metadata`/`list_keys` raise `StorageNotFound` for absent keys/buckets; `ensure_bucket` creates-or-reports; `object_metadata` returns `{"metadata": stored_meta, "updated": "2026-08-03T00:00:00Z"}`.
- Tests (MockTransport, house pattern): exact URL/auth-header/quoting per method (key with `/` quoted), multipart body carries metadata JSON + media bytes, 404→`StorageNotFound`, non-200→`ClientError`, pagination followed, ensure_bucket create body includes versioning, ensure_versioning PATCH only when disabled.

- [ ] TDD; commit: `feat(api): storage client — bucket reads/writes over the JSON API, fake included`

### Task 2: API — org config repo (pnyx-config) load + merge

**Files:** `api/pnyx_api/org.py` (new), `api/pnyx_api/config.py` (key changes), `api/pnyx_api/main.py` (`build_default_app` wiring + `app.state.org`), `api/config.example.yaml`, `api/tests/test_org.py` (new), `api/tests/test_config.py`.

**Config changes** (`Config` keeps its full runtime surface — downstream code unchanged):
- ADD: `org_repo: str | None = None` (full name, e.g. `tenishai/pnyx-config`), `api_base_url: str = "http://127.0.0.1:8100"`, `skills_repo: str | None = None`, `skills_bucket: str = ""`, `personas_bucket: str = ""`, `skills_publish_wif_provider: str = ""`, `skills_publish_service_account: str = ""`, `skills_publish_external: bool = False` (True = the org runs its own publish pipeline into the skills bucket; pnyx must never install or modify anything in that repo).
- REMOVE: `skills_repo_path`, `personas_dir` (bucket catalogs replace both; delete from `config.example.yaml` and fix every in-repo consumer/test — Tasks 4/5 own the catalog/steps consumers, this task only removes the keys' definitions last after grep confirms remaining uses live in files those tasks touch; if ordering bites, keep the fields deprecated-unused here and delete in Task 5 — disclose which).

**Interfaces:**
```python
# org.py
ORG_KEYS = frozenset({
    "github_org", "skills_repo", "skills_bucket", "personas_bucket",
    "gojira_cluster", "runtime_image", "gcp_project", "vertex_project",
    "vertex_location", "model_group", "avatar_model", "avatar_location",
    "models", "skills_publish_wif_provider", "skills_publish_service_account",
    "skills_publish_external",
})

@dataclass
class OrgState:
    wired: bool
    repo: str | None
    error: str | None = None   # plain sentence when load failed / repo unset

def load_org_config(local: Config, github: GitHubClient) -> tuple[Config, OrgState]:
    # org_repo None -> (local, OrgState(False, None, "no org configured"))
    # clone org_repo into a TemporaryDirectory (github.clone — FakeGitHub already materializes
    # files on clone), read org.yaml at repo root, validate keys ⊆ ORG_KEYS (unknown key ->
    # wired=False with the key named), pydantic-validate values by constructing
    # Config.model_copy(update=org_values) -> (merged, OrgState(True, org_repo))
    # clone/parse/validation failure -> (local, OrgState(False, org_repo, "<plain reason>"))
```
- `build_default_app()`: after `Config.load(None)` construct `GhCLI(runner=CommandRunner())` once, call `load_org_config`, use the merged config for everything downstream (engine, `Clients.real`), and pass `OrgState` into `create_app(session_factory, config=..., clients=..., org=OrgState(...))` (new keyword, default `OrgState(False, None, "no org configured")` so every existing test keeps working) → stored at `app.state.org`.
- Boot never crashes on org problems: unwired room serves honest empty catalogs (Tasks 3/8 consume `app.state.org`).
- Tests: merge precedence (org overrides local for org keys; `db_url`/`workdir` untouched even if present in org.yaml → unknown-key failure), unwired when `org_repo` None, clone `ClientError` → unwired with reason, bad yaml → unwired, `models` list round-trips through `ModelChoice`.

- [ ] TDD; commit: `feat(api): org config repo — clone, validate, merge over local at boot`

### Task 3: API — FTS5 catalog search + endpoints

**Files:** `api/pnyx_api/search.py` (new), `api/pnyx_api/main.py` (routes), `api/tests/test_search.py` (new), `api/tests/test_api_search.py` (new).

**Index contract** (both buckets, key `index.json` under the catalog prefix):
```json
{"generated_at": "<iso8601>", "entries": [
  {"id": "code-review", "name": "Code review", "description": "...", "team": "shared",
   "tags": ["review", "quality"], "sha": "<source git sha — skills only>",
   "builtin": false}
]}
```

**Interfaces:**
```python
# search.py
CATALOG_TTL_S = 60

@dataclass
class CatalogEntry:
    kind: str            # "skill" | "persona"
    id: str
    name: str
    description: str
    team: str
    tags: list[str]      # stored space-joined in FTS, split on read
    sha: str = ""
    builtin: bool = False

def ensure_fts(engine) -> None
    # CREATE VIRTUAL TABLE IF NOT EXISTS catalog_fts USING fts5(
    #   id, name, description, team, tags, kind UNINDEXED, sha UNINDEXED, builtin UNINDEXED)
def ingest_indexes(engine, storage: StorageClient, *, skills_bucket: str,
                   personas_bucket: str, prefix: str = "") -> dict
    # read <prefix>index.json from each named bucket (empty bucket name or StorageNotFound ->
    # that catalog contributes 0 entries, noted); malformed JSON/entry -> ClientError with the
    # bucket named. Rebuild atomically: BEGIN; DELETE FROM catalog_fts; INSERT rows; COMMIT.
    # returns {"skills": n, "personas": n}
def search(engine, kind: str, q: str, team: str | None = None, limit: int = 50) -> list[CatalogEntry]
    # q empty/whitespace -> all of kind (team-filtered), ORDER BY name
    # else MATCH built from q tokens, each escaped ("" doubling) and prefix-starred:
    #   '"revi"* "co"*' — implicit AND; ORDER BY bm25(catalog_fts); WHERE kind=? AND (team=? or 1)
def get_entry(engine, kind: str, id: str, team: str) -> CatalogEntry | None
def teams(engine) -> list[str]        # SELECT DISTINCT team ... ORDER BY team
```
- Routes (all inline in `create_app`, house shape):
  - `GET /api/skills/search?q=&team=` and `GET /api/personas/search?q=&team=` → `200 {"results": [entry...]}` (entry = CatalogEntry fields minus `kind`; skills include `sha`, personas include `builtin`). Before querying, TTL refresh: if `app.state.catalog_loaded_at` is None or older than `CATALOG_TTL_S` and storage+buckets configured, run `ingest_indexes` (a `threading.Lock` on app.state guards concurrent refreshes; failures log + serve the stale table — search never 500s on a bucket blip).
  - `GET /api/catalog/teams` → `200 {"teams": [...]}` (same TTL hook).
  - `POST /api/catalogs/refresh` → force `ingest_indexes`; `200 {"skills": n, "personas": n}`; 503 `{"error": "catalogs aren't set up yet"}` when storage is None or both bucket names empty; `ClientError` → 502.
- `/api/catalog/models` unchanged. The old `/api/catalog/skills` + `/api/catalog/personas` listing routes are REMOVED in Task 5 together with their consumers (create-route validation swaps to `get_entry` there) — this task only adds.
- Tests: `search.py` unit (seeded FakeStorage indexes → ingest counts; ranked prefix match as-you-type; team filter; empty-q ordering; quote-escape hostile q like `foo" OR 1`; missing index → 0 + note; malformed → ClientError); route-level (TTL refresh happens once under the lock — two immediate calls, one ingest recorded in FakeStorage.calls; refresh endpoint counts + 503 unwired + 502 on storage error).

- [ ] TDD; commit: `feat(api): catalog search — FTS5 over bucket indexes, search/teams/refresh endpoints`

### Task 4: API — persona builder (deterministic template → personas bucket)

**Files:** `api/pnyx_api/personas.py` (new), `api/pnyx_api/main.py` (route), `api/tests/test_personas.py` (new), `api/tests/test_api_personas.py` (new).

**Interfaces:**
```python
# personas.py
TONES = ("warm", "direct", "playful", "formal")
PERSONA_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")

class PersonaFields(BaseModel):
    name: str            # 1-60 chars, must slugify to a valid id
    role: str            # 1-120, one line (no newlines)
    purpose: str         # 1-500
    tone: Literal["warm", "direct", "playful", "formal"]
    tone_note: str = ""            # <=200, optional free line
    boundaries: list[str] = []     # <=8 items, each 1-200, optional
    tags: list[str] = []           # 1-6 items, each ^[a-z][a-z0-9-]{0,23}$

def persona_id(name: str) -> str    # lower, non-alnum runs -> "-", strip, cap 40; invalid -> PersonaError
def render_persona(fields: PersonaFields) -> str
    # Deterministic; byte-pinned test. Shape (exact — the literal {name} token survives for
    # provision-time substitution; NEVER str.format over user text):
    #   <!-- description: {fields.role} -->
    #   # Identity
    #   You are {name} — <role>.
    #
    #   # What you do
    #   <purpose>
    #
    #   # How you speak
    #   Your tone is <tone>.<" " + tone_note if set>
    #
    #   # Boundaries
    #   - <each boundary>            (section only when boundaries non-empty)
    # Built by concatenation, not .format (purpose/boundaries may contain braces).

class PersonaError(Exception): ...          # invalid fields beyond pydantic (bad slug)
class PersonaExists(Exception): ...

def create_persona(storage: StorageClient, engine, *, bucket: str, prefix: str = "",
                   fields: PersonaFields, team: str) -> CatalogEntry
    # id = persona_id(fields.name); read <prefix>index.json (StorageNotFound -> empty index);
    # id present -> PersonaExists. write_bytes <prefix><id>/persona.md
    # (metadata {"team": team, "creator": "room"}, content_type text/markdown);
    # append entry {id, name, description=role, team, tags, builtin: False} to index,
    # write_bytes index.json; then search.ingest_indexes(...) so the new persona is
    # instantly searchable. Read-modify-write on index.json is last-write-wins —
    # acceptable at catalog scale, noted here deliberately.

def seed_default_personas(storage: StorageClient, *, bucket: str, prefix: str = "") -> dict
    # For each packaged pnyx_api/templates/personas/*.md (parsed with the existing
    # catalog._parse_persona_file): object <prefix><id>/persona.md absent -> write with
    # metadata {"team": "shared", "creator": "pnyx"}; present -> skip. Rewrite index.json
    # merging entries (builtin: True, team "shared", tags []). returns
    # {"seeded": n, "skipped": n}   # bootstrap's step and its idempotence hang off this
```
- Route: `POST /api/personas` body `{...PersonaFields, "team": str}` (team `^[a-z][a-z0-9-]{0,23}$`) → `201 {persona entry}`; duplicate → `409 {"error": "a persona with that name already exists"}`; storage None or `personas_bucket` empty → `503 {"error": "catalogs aren't set up yet"}`; `ClientError` → 502 plain.
- Tests: `persona_id` (spacing/punctuation/length/invalid), `render_persona` byte-pinned golden (one full-featured, one minimal without boundaries/tone_note), create writes both objects with exact keys+metadata and returns the entry, duplicate raises, seed twice → second all-skipped, route 201/409/422 (field limits)/503/502.

- [ ] TDD; commit: `feat(api): persona builder — fixed form, deterministic template, bucket-native storage`

### Task 5: API — assembly from buckets + creating team

**Files:** `api/pnyx_api/models.py` (`Agent.team`), `api/pnyx_api/main.py` (create route + remove old catalog listing routes), `api/pnyx_api/steps.py` (`_resolve_skills` → bucket, `_resolve_persona_body` → bucket), `api/pnyx_api/composer.py` (materialize from staged dirs), `api/pnyx_api/catalog.py` (delete `list_skills`/`list_personas` + git-archive helpers; keep `validate_name` + persona-file parsing), `api/tests/` (fixtures + affected suites: `test_api_agents.py`, `test_steps.py`, `test_composer.py`, `test_catalog.py`, `conftest.py`).

**Interfaces:**
```python
# models.py
team: Mapped[str | None] = mapped_column(String(63), nullable=True)
# comment, house pattern: postdates create_all on the running dev DB —
# manual `ALTER TABLE agents ADD COLUMN team VARCHAR;` (controller follow-up)

# main.py
class CreateAgentRequest(BaseModel):
    name: str
    persona: str
    team: str                     # required; ^[a-z][a-z0-9-]{0,23}$ -> else 422
    skills: list[SkillPick] = []  # class SkillPick(BaseModel): id: str; team: str
    model: str | None = None
# create route validation swaps to the FTS catalog (TTL-refresh first, same hook as search):
#   persona: search.get_entry(engine, "persona", payload.persona, team=<entry's own team — look
#     up by id across teams: personas are org-wide, id is unique in the personas index>) -> 422
#     "unknown persona" when absent
#   each skill: get_entry(engine, "skill", pick.id, pick.team) -> 422 naming the miss
#   Agent.skills JSON entries become {"name": id, "team": team, "sha": entry.sha}
#   agent.team = payload.team
# GET /api/agents/{name} detail gains "team"; list cards gain "team" (nullable for old rows).

# steps.py
def _resolve_persona_body(config, storage, engine, agent) -> str
    # bucket persona: storage.read_bytes(personas_bucket, f"{agent.persona}/persona.md"),
    # strip the description-header line, then body.replace("{name}", agent.name)
    # (.replace, not .format — closes the ledgered P1a brace hazard)
def _stage_skills(storage, config, agent, staging: Path) -> list[StagedSkill]
    # per Agent.skills entry: keys = storage.list_keys(skills_bucket, f"{team}/{id}/");
    # empty -> RuntimeError("skill no longer in the catalog: <team>/<id>") (fails the step honestly);
    # download each preserving the relative path under staging/<id>/;
    # metadata source-sha of SKILL.md compared to the recorded pin — mismatch: proceed with
    # current content, UPDATE the row's recorded sha, log.warning (catalog moved between pick
    # and assembly; shipping current is the honest choice — deliberate semantic, do not fail)
@dataclass
class StagedSkill:
    id: str
    dir: Path
# composer.py: SkillInfo/git-archive materialization path replaced — compose()'s skills input
# becomes list[StagedSkill]; _materialize_skill copies staged dir -> bundle/skills/<id>/
# (containment/symlink rejection checks kept verbatim). agent-card.json skills list unchanged.
```
- RepoStep passes `clients.storage` + engine through existing ctx plumbing; unwired org (empty bucket names) → create route 422s before any row is written: `{"error": "catalogs aren't set up yet"}` on persona/skill lookups against an empty FTS table — same copy as the 503s, deliberate.
- Fixture rework (once, here): `conftest.py` gains `fake_storage` seeded with the two packaged personas (via `seed_default_personas` against `FakeStorage`) + one sample skill (`shared/word-games/SKILL.md`, metadata `source-sha: "seedsha"`) + both `index.json`s; `config` fixture gains `skills_bucket="skills-bucket" personas_bucket="personas-bucket"`; `clients` fixtures gain `storage=fake_storage`. `test_api_agents.py` create payloads gain `team` + `SkillPick` shape.
- Tests: create 201 with team persisted + detail/list expose it; 422s (bad team slug, unknown persona, unknown skill (id,team)); pipeline to READY materializes staged skill files into the pushed repo (assert via FakeGitHub's recorded push tree) with persona body `{name}`-substituted; sha-drift path (fake metadata sha ≠ recorded) → proceeds + row updated + no failure; skill-vanished path → step fails with the plain message; brace-in-purpose persona body survives (regression for the `.replace` swap).

- [ ] TDD; commit: `feat(api): assembly reads the catalogs — bucket skills/personas, creating team recorded`

### Task 6: skills publish workflow template (Action + publisher script)

**Files:** `api/pnyx_api/templates/skills_publish/publish-skills.yml` (new), `api/pnyx_api/templates/skills_publish/publish_skills.py` (new), `api/tests/test_publish_skills.py` (new — imports the template script by path via `importlib.util`, never installs it).

**Script contract** (stdlib + pyyaml at parse time; `google-cloud-storage` imported lazily ONLY inside `_real_client()` so pnyx's suite never needs it):
```python
# publish_skills.py — runs inside the client repo's Action
SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,39}$")

@dataclass
class Skill:
    id: str; team: str; dir: Path; files: list[Path]   # files relative to dir
    name: str; description: str; tags: list[str]

def discover(root: Path) -> list[Skill]
    # layout 1: teams/<team>/<id>/SKILL.md   layout 2 (legacy/flat): skills/<id>/SKILL.md -> team "shared"
    # both may coexist; duplicate (team, id) -> ValidationError
def validate(skill_dir: Path) -> tuple[str, str, list[str]]   # (name, description, tags)
    # ---‑fenced YAML frontmatter in SKILL.md: name required + SKILL_NAME_RE; description
    # required (fallback: first non-blank body line); tags optional list of slugs
def build_index(skills: list[Skill], sha: str) -> dict       # the Task 3 index contract, entries sorted by (team, id)
def sync(client, bucket: str, skills: list[Skill], sha: str) -> dict
    # client duck-type: get_bucket(name) -> bucket; bucket.blob(key) -> blob with
    # .upload_from_string(data, content_type=...) + .metadata setter; bucket.list_blobs(prefix=...)
    # upload every skill file to "<team>/<id>/<relpath>" with metadata {"source-sha": sha};
    # delete bucket keys not in the new set (index.json excluded from the prune — versioning is
    # the safety net); finally upload index.json. returns {"uploaded": n, "deleted": n}
def main(argv) -> int   # --bucket B [--check]; repo root = cwd; sha = env GITHUB_SHA
    # --check: discover+validate only (PR gate). exit 1 with each error printed on any failure.
```
**Workflow template** (`publish-skills.yml`, rendered by bootstrap via literal token replacement — tokens `__PNYX_BUCKET__`, `__PNYX_WIF_PROVIDER__`, `__PNYX_SERVICE_ACCOUNT__`; no `{}` templating so GH `${{ }}` expressions pass through untouched):
- `on: pull_request` (runs `--check`) + `push: branches: [main]` (publishes); `permissions: contents: read, id-token: write`; steps: checkout → `google-github-actions/auth@v2` (workload_identity_provider/service_account from tokens) → setup-python 3.12 → `pip install google-cloud-storage pyyaml` → `python .github/scripts/publish_skills.py --bucket __PNYX_BUCKET__` (+`--check` on the PR job).
- Tests (pure functions + duck-typed `_FakeBucketClient` defined in the test file): discover both layouts + coexistence + duplicate error; frontmatter validation (missing name, bad slug, description fallback); index golden (sorted, exact shape); sync uploads with metadata + prunes stale skill + spares index.json + returns counts; `--check` exits nonzero on a bad repo, zero on a good one; token replacement in the yml has no leftover `__PNYX_` after render (rendering itself is Task 7's, but the yml's token inventory is pinned here).

- [ ] TDD; commit: `feat(api): skills publish template — validate, sync to the bucket, rewrite the index`

### Task 7: bootstrap CLI (+ `GitHubClient.open_pr`)

**Files:** `api/pnyx_api/bootstrap.py` (new, runnable via `python -m pnyx_api.bootstrap`), `api/pnyx_api/templates/org_config/org.yaml.tmpl` + `README.md` (new), `api/pnyx_api/templates/skills_repo/README.md` (new), `api/pnyx_api/clients.py` (`open_pr` on Protocol + `GhCLI`), `api/tests/fakes.py` (FakeGitHub.open_pr), `api/tests/test_bootstrap.py` (new), `api/tests/test_clients.py` (open_pr argv), `api/Makefile` (`bootstrap` target).

**Interfaces:**
```python
# clients.py — GitHubClient Protocol addition
def open_pr(self, workdir: Path, *, repo: str, branch: str, title: str, body: str) -> str:
    """From a dirty clone: checkout -b, add -A, commit, push -u, `gh pr create`; returns PR URL."""
# GhCLI argv: git -C {workdir} checkout -b {branch} / add -A / commit -m {title} /
#             push -u origin {branch}; gh pr create --repo {repo} --title {title} --body {body} --head {branch}

# bootstrap.py
@dataclass
class StepResult:
    name: str
    status: str      # "done" | "skipped" | "failed"
    detail: str = ""

def run_bootstrap(config: Config, clients: Clients, *, out=print) -> list[StepResult]
def main(argv: list[str] | None = None) -> int      # loads local config + org merge exactly like
    # build_default_app (org repo may not exist yet — unwired is fine, bootstrap is what fixes it),
    # Clients.real, run_bootstrap, prints one line per step ("done"/"skipped"/"failed: <detail>"),
    # exit 1 iff any failed. Operator vocabulary allowed here and only here.
```
**Steps, in order (each isolated: an exception → `failed` with the message, later steps still run):**
1. `org-config repo` — `github.repo_exists(f"{config.github_org}/pnyx-config")` → skipped; else render `org.yaml.tmpl` from the CURRENT merged config values (github_org, skills_repo defaulting to `f"{github_org}-skills"` when unset, buckets defaulting to `f"{github_org}-skills"` / `f"{github_org}-personas"`, cluster/runtime/vertex/model keys verbatim) + README into a tmpdir, `github.create_repo_from_dir` → done. Detail reminds: point `org_repo` at it in local config.
2. `skills bucket` — name from merged config (default as above); `storage.ensure_bucket` → created=done/exists→`ensure_versioning` changed?done:"skipped".
3. `personas bucket` — same.
4. `skills repo + publish workflow` — `config.skills_publish_external` → skipped("publish pipeline managed by the org") without touching GitHub at all (a real client already runs its own publisher into its bucket; pnyx adopts, never overwrites). Else: repo = `config.skills_repo or f"{github_org}-skills"`; if `github.repo_exists` false → scaffold tmpdir (README + `.github/workflows/publish-skills.yml` + `.github/scripts/publish_skills.py`, yml tokens replaced from config `skills_publish_*` + bucket) → `create_repo_from_dir` → done. Else clone; workflow file already present → skipped; else write both files + `open_pr(workdir, repo=..., branch="pnyx-skills-publish", title="pnyx: publish skills to the catalog bucket", body=<what it does + merge instruction>)` → done(detail=PR URL).
5. `default personas` — `personas.seed_default_personas(...)` → seeded>0 ? done : skipped (detail carries counts).
6. `catalog ingest` — POST `{config.api_base_url}/api/catalogs/refresh` via httpx (timeout 5s); connect error → fall back to direct `search.ingest_indexes` against `make_engine(config.db_url)`; → done with counts (always done — re-running converges, the idempotence test asserts steps 1-5 skipped on run 2, step 6 done both times).
- `Makefile`: `bootstrap:\n\tvenv/bin/python -m pnyx_api.bootstrap`.
- Tests (all against fakes; no network): fresh-org run → every step done, org.yaml rendered values pinned (golden), scaffold/PR bodies contain no leftover `__PNYX_` tokens; second run → 1-5 skipped; existing-repo-without-workflow → PR path with recorded open_pr call; bucket exists+versioning off → step 2 done via ensure_versioning; a failing storage knob → that step failed, later steps still ran; exit code 1 on any failure, 0 otherwise; open_pr argv exact (test_clients).

- [ ] TDD; commit: `feat(api): bootstrap CLI — wire a client org in one idempotent command`

### Task 8: API — GET /api/org/status

**Files:** `api/pnyx_api/main.py` (route), `api/pnyx_api/clients.py` (`GojiraClient.ping` + impl + fake knob), `api/tests/test_api_org_status.py` (new), `api/tests/test_clients.py` (ping).

**Interfaces:**
```python
# clients.py — GojiraClient Protocol addition
def ping(self) -> bool:
    """GET {base}/api/v1/apps (auth'd); True iff 200. Never raises — any failure is False."""

# GET /api/org/status ->
{"org": <config.github_org or null when unwired>, "wired": <app.state.org.wired>,
 "checks": [
   {"id": "org-config",      "label": "Organization settings",  "ok": bool, "detail": str},
     # ok = org.wired; detail = org.error or the repo name
   {"id": "skills-bucket",   "label": "Skills catalog storage", "ok": bool, "detail": str},
     # storage.list_keys(skills_bucket, "") succeeds (empty bucket name -> ok False, "not configured")
   {"id": "personas-bucket", "label": "Personas storage",       "ok": bool, "detail": str},
   {"id": "skills-index",    "label": "Skills index",           "ok": bool, "detail": str},
     # object_metadata(skills_bucket, "index.json") -> ok; detail "updated <updated field>";
     # StorageNotFound -> ok False, "no skills published yet"
   {"id": "personas-seeded", "label": "Personas",               "ok": bool, "detail": str},
     # personas index.json present AND entries non-empty -> ok, detail "<n> personas"
   {"id": "deploy-platform", "label": "Deploy platform",        "ok": bool, "detail": str},
     # clients.gojira.ping() + config.gojira_cluster non-empty; detail names the cluster
 ]}
```
- Every check individually try/excepted (a `ClientError` → that check `ok: False` with a plain sentence; route itself always 200). This endpoint is read-only visibility — no admin routes, no flags (spec).
- Tests: all-green against seeded fakes; each degraded case flips exactly its check (unwired org, empty bucket names, missing indexes, empty personas entries, ping False); ping argv/URL via MockTransport.

- [ ] TDD; commit: `feat(api): org status — read-only wiring checks for the room`

### Task 9: UI — catalog/search/org client + queries

**Files:** `ui/src/lib/api.ts`, `ui/src/lib/queries.ts`, `ui/src/lib/api.test.ts`, `ui/src/lib/queries.test.ts`.

**Interfaces:**
```ts
// api.ts
export type CatalogEntry = { id: string; name: string; description: string; team: string;
                             tags: string[]; sha?: string; builtin?: boolean };
export type PersonaForm = { name: string; role: string; purpose: string;
                            tone: "warm" | "direct" | "playful" | "formal";
                            tone_note: string; boundaries: string[]; tags: string[]; team: string };
export type OrgCheck = { id: string; label: string; ok: boolean; detail: string };
export type OrgStatus = { org: string | null; wired: boolean; checks: OrgCheck[] };
export async function searchSkills(q: string, team: string | null): Promise<CatalogEntry[]>   // GET /api/skills/search
export async function searchPersonas(q: string, team: string | null): Promise<CatalogEntry[]> // GET /api/personas/search
export async function fetchTeams(): Promise<string[]>                                          // GET /api/catalog/teams
export async function createPersona(body: PersonaForm): Promise<CatalogEntry>                  // POST /api/personas
export async function fetchOrgStatus(): Promise<OrgStatus>                                     // GET /api/org/status
// fetchCatalogs shrinks to models only: fetchModels(): Promise<{models: {id,label}[]; default: string}>
// createAgent body changes: { name, persona, team, skills: {id, team}[], model }
```
```ts
// queries.ts — keys extend the documented convention:
// ["catalog","models"] ["catalog","teams"] ["catalog","skills",{q,team}] ["catalog","personas",{q,team}] ["org","status"]
export function modelsQuery()
export function teamsQuery()
export function skillsSearchQuery(q: string, team: string | null)     // staleTime 15_000
export function personasSearchQuery(q: string, team: string | null)
export function orgStatusQuery()                                       // staleTime 30_000
export function createPersonaMutation(queryClient)   // onSuccess: invalidate ["catalog","personas"] + ["catalog","teams"]
// createAgentMutation unchanged apart from the body type.
```
- URL building must escape `q`/`team` (`URLSearchParams`) — closes the ledgered URI-escape gap; omit `team` param when null.
- Tests (house fetch-mock harness): URL/param escaping incl. a `q` with `&` and spaces; body shapes (createPersona, new createAgent); errorMessage passthrough on a 409; query-key/staleTime/invalidation shapes exercised the way `queries.test.ts` already exercises `shouldRetry`/mutation delegation.

- [ ] TDD; commit: `feat(ui): catalog search + persona builder + org status client layer`

### Task 10: UI — CreateModal rework (search, badges, builder, team) + honest empty states

**Files:** `ui/src/components/CreateModal.svelte`, `ui/src/components/PersonaBuilder.svelte` (new), `ui/src/pages/Dashboard.svelte` (unwired banner only if trivially placeable — else modal-only, disclose).

**Behavior:**
- **Team** (new, first field after name): `<input list>` datalist fed by `teamsQuery` — pick an existing team or type a new one (honor-system until identity lands; lowercase slug hint mirrors the name field's). Chosen team is sent as `team` and pre-fills the persona builder's team.
- **Persona section**: results of `personasSearchQuery(personaQ, null)` as the existing radio-card list — each card gains a badge: `Built-in` when `builtin`, else the team name (reuse the BEM chip idiom). A small search input above (debounced 250ms via the existing `debounce.ts`). A final card **"Build your own"** swaps the section to `PersonaBuilder` inline: the six fixed fields (name, role, purpose, tone select of the four options, optional tone note, optional boundaries textarea one-per-line, tags input comma/space-split) with a Create button → `createPersonaMutation`; on success select the returned id, collapse back to the list (it now contains the new persona), toast "Persona ready". 409 → inline message "A persona with that name already exists." Cancel collapses without creating.
- **Skills section**: search input (debounced 250ms) + team `<select>` (All teams + `teamsQuery`) over `skillsSearchQuery(q, team)`; results as the existing checkbox rows, each with a team badge. Selection is a `Map<string, {id, team, name}>` keyed `team/id` so picks persist while the visible results change; selected skills render as removable chips above the search box. Submit sends `skills: [{id, team}]`.
- **Unwired/empty states** (drive off `orgStatusQuery`): when `wired` is false or a catalog check is not ok, persona/skills sections render the shared `EmptyState` with copy `"Catalogs aren't set up yet — ask whoever runs your room."` instead of broken pickers; create stays disabled. Zero results with a query → `"Nothing matches your search."`; empty catalog while wired → existing `"No skills in the catalog yet"` copy retained.
- Model picker, name/availability flow, submit gating: unchanged apart from `canSubmit` also requiring a valid team slug.
- Gate: `make -C ui check` (svelte-check + vitest). Pure logic already covered in Task 9; no component-mount tests (house has none — node env).

- [ ] Implement; commit: `feat(ui): create from the org's catalogs — search, team badges, inline persona builder`

### Task 11: Live milestone — tenishai onboarded as the first client

Exit criterion, per spec: a client onboarding end to end, minus DNS.

- [ ] Controller pre-flight: `ALTER TABLE agents ADD COLUMN team VARCHAR;` on the dev DB; local `config.local.yaml` gains `api_base_url` and (after step 1 below) `org_repo`; API restarted at HEAD with the full PATH recipe.
- [ ] Run `make -C api bootstrap` against the tenishai org (org values from current local config; buckets `tenishai-skills`/`tenishai-personas` in the dev GCP project — exact names in the ledger). Verify each step reports done; re-run immediately → all wiring steps `skipped` (idempotence, live).
- [ ] Set `org_repo: tenishai/pnyx-config` in local config, restart API, confirm boot merges org values (log line) and `GET /api/org/status` shows org-config green.
- [ ] Merge the publish-workflow PR on the tenishai skills repo (Kyle or controller via gh). Watch the Action publish; `GET /api/skills/search?q=` returns the repo's skills with team `shared` + shas; status `skills-index` green with freshness.
- [ ] `GET /api/org/status`: every check green. Screenshot the JSON (or curl transcript) for the runbook.
- [ ] Browser: create a brand-new agent — type a team (e.g. `platform`), build a persona in the inline form (all six fields), search skills with the team filter (`shared`), pick one, create. Agent reaches READY 7/7; first words recorded; detail shows the team; the pushed repo's `bundle/persona.md` is the rendered template with the agent's name substituted and `bundle/skills/<id>/` matches the bucket content. Screenshots: modal (builder open), search with badges, READY card.
- [ ] Unwired honesty check (cheap, live): point a scratch config at no org (`org_repo` unset), boot a second API port, confirm the modal shows the catalogs-not-set-up empty state. Kill it.
- [ ] Append `## Phase A results` to the runbook (bootstrap transcript, status JSON, create story verbatim); README status line + ROADMAP gain Phase A (org catalogs live; ROADMAP's stale "*(current)*" marker moves); commit: `Phase A results: tenishai onboarded, agent assembled from org catalogs`

---

## Self-review notes (applied)

- **Spec coverage walked section by section**: org model → Task 2; catalog buckets/layout/adopt-don't-create → Tasks 1, 6, 7; skills git→CI→bucket with sha metadata + both repo layouts → Task 6; personas defaults+builder bucket-native → Task 4; FTS5 search endpoints + TTL + refresh + teams filter → Task 3; persona builder fields/template → Task 4; create-modal persona/skills/team changes → Tasks 5, 9, 10; bootstrap steps + idempotence + make target → Task 7; org status + honest empty states → Tasks 8, 10; testing prescriptions (StorageClient Protocol fake, validator covered in-template, FTS unit tests on seeded indexes, byte-pinned persona render, module-level bootstrap idempotence, route-level status) → matching tasks; milestone → Task 11. Non-goals respected: no admin UI, no Elastic, no personas-in-git, no editing shipped defaults, no bundle migration for the four live agents.
- **Deliberate semantics locked here** (implementers must not "fix" these): skills sha drift at provision proceeds with current content and updates the pin (never wedges); persona/skill create-time validation happens against the FTS table after a TTL refresh; personas index read-modify-write is last-write-wins; search serves stale on refresh failure rather than 500ing; `{name}` substitution is `.replace`, never `.format` (P1a brace-hazard closure); team is honor-system free entry via datalist (spec says picker — free entry is the disclosed deviation, because a fresh org's only discoverable team is `shared` and the milestone needs a real team name; revisit when Phase B identity arrives).
- **Type consistency checked**: `CatalogEntry` field set identical across search.py, personas.py return, both search endpoints, and ui/api.ts; `SkillPick {id, team}` matches `Agent.skills` JSON `{"name", "team", "sha"}` consumers in `_stage_skills`; `StepResult` statuses match the CLI printer and the idempotence test; `open_pr` consumed only by bootstrap step 4 with the exact signature defined in Task 7; bucket-name defaults (`<org>-skills`/`<org>-personas`) appear identically in Tasks 2 (config), 7 (bootstrap), and the spec.
- **Sequencing**: 1→2→3→4→5 build the serving path; 6→7 the publishing/wiring path (7 consumes 4's `seed_default_personas`, 6's templates, 1's storage); 8 needs 2+3; 9→10 need 3/4/5/8; 11 needs all. Tasks 3 and 4 both touch `main.py` route additions — sequential per house rule (no concurrent implementers in one repo).
- **Config-shrink ordering hazard** (Task 2 note): `skills_repo_path`/`personas_dir` deletion may need to trail until Task 5 removes their last consumers — Task 2 discloses which path it took; either way `config.example.yaml` ends the phase carrying only the new key set.
