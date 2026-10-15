# P1a — cortex-api Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A FastAPI backend where `POST /api/agents {"name","persona","skills","model"}` produces a running, memory-seeded, PVC-backed agent on the dev cluster (via gojira) and records its first reply — the whole P0 runbook as code.

**Architecture:** Greenfield package at `api/cortex_api/` in the cortex repo (family layout: `api/` now, `ui/` in P1b). Four layers: **catalog** (skills repo scan, persona templates, model list), **composer** (bundle tree generation + JSON-Schema validation), **clients** (Protocols with subprocess/httpx real impls and in-memory fakes: GitHub via `gh`, gojira via its JSON API, gcloud, kubectl/a2a), **provisioner** (persisted step ledger, fire-and-return, idempotent retry — the gojira shape). Steps encode the E2E-verified sequence exactly, including the post-scaffold "wire" commit (KSA + PVC + kustomization registration + gojira.yaml contract additions) that the runbook proved necessary. One daedalus-branch task closes the SIGKILL seed-convergence TODO before memory becomes persistent.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 (SQLite), pydantic 2 + pydantic-settings, jsonschema, PyYAML, httpx, pytest; subprocess `gh`/`gcloud`/`kubectl`/`git`.

## Global Constraints

- Never write the employer's name (any casing) into any file in this repo or sibling repos. Deploy-config values (projects, cluster, AR paths, group email) appear ONLY in `config.yaml` files that are gitignored (`config.local.yaml` pattern) or in test fixtures as neutral placeholders (`example-project`, `models@example.com`).
- Sibling-repo work stays on the existing branches: daedalus `feat/runtime-image` (Task 10). Never touch gojira in this plan (its branch is complete).
- TDD per task; run `make -C api check` (created in Task 1: ruff + pytest) before every commit; one commit per task minimum.
- The bundle contract is daedalus's published schema — vendored at `api/cortex_api/vendor/bundle.schema.json`, copied verbatim from `../daedalus/docs/bundle.schema.json`, never hand-edited.
- Every k8s manifest the composer or steps author MUST also be registered in `k8s/base/kustomization.yaml` (P0 finding: gojira preserves kustomization entries, it does not discover files).
- Transports are always `[{kind: a2a, host: 0.0.0.0, port: 8080}]` (P0 finding: loopback default is unreachable by probes).
- Fail-closed agent defaults are non-negotiable: `tools: {provider: none, allow: []}`, `approvals: {ceiling: {write: false}, gates: [{match: "*", require: plan_approval}], escalation: {route: cli}}`.
- API JSON errors follow gojira's idiom: `JSONResponse({"error": "..."}, status_code=...)`.

---

### Task 1: Package scaffold — config, DB, app factory

**Files:**
- Create: `api/pyproject.toml`, `api/Makefile`, `api/cortex_api/__init__.py`, `api/cortex_api/config.py`, `api/cortex_api/db.py`, `api/cortex_api/models.py`, `api/cortex_api/main.py`, `api/config.example.yaml`
- Test: `api/tests/test_config.py`, `api/tests/test_api_basics.py`
- Modify: `.gitignore` (append `api/config.local.yaml` and `api/*.db`)

**Interfaces (later tasks rely on these exactly):**
- `Config.load(path: Path | None = None) -> Config` — reads yaml (default `CORTEX_CONFIG` env or `config.local.yaml` beside cwd, falling back to pure defaults so tests never need a file). Pydantic model:
  ```python
  class ModelChoice(BaseModel):
      id: str          # LiteLLM route, e.g. vertex_ai/claude-sonnet-4-5
      label: str

  class Config(BaseModel):
      github_org: str = "example-org"
      runtime_image: str = "registry.example/agents/daedalus-runtime:latest"
      gojira_url: str = "http://127.0.0.1:8080"
      gojira_token: str | None = None
      gojira_cluster: str = "example-cluster"
      gcp_project: str = "example-project"
      vertex_project: str = "example-model-project"
      vertex_location: str = "us-east5"
      model_group: str = "models@example.com"
      models: list[ModelChoice] = [ModelChoice(id="vertex_ai/claude-sonnet-4-5", label="Claude Sonnet 4.5")]
      skills_repo_path: Path | None = None
      personas_dir: Path | None = None   # None -> package templates/personas
      workdir: Path = Path.home() / ".cortex"
      db_url: str = "sqlite:///cortex.db"
  ```
- `models.py` — SQLAlchemy `Base`, and:
  ```python
  class AgentStatus(enum.StrEnum):
      PENDING = "pending"; PROVISIONING = "provisioning"; READY = "ready"; FAILED = "failed"

  class StepStatus(enum.StrEnum):
      PENDING = "pending"; RUNNING = "running"; DONE = "done"; SKIPPED = "skipped"; FAILED = "failed"

  class Agent(Base):  # __tablename__ "agents"; name unique
      id, name (String(63), unique), persona (String(63)), model_id (String(255)),
      skills (JSON list of {"name","sha"}), status (default PENDING),
      repo_full_name (String(255), nullable), gojira_app_id (Integer, nullable),
      first_reply (Text, nullable), error (Text, nullable), created_at
  class AgentStep(Base):  # "agent_steps"; UniqueConstraint(agent_id, name)
      id, agent_id (FK), name (String(63)), status (default PENDING),
      error (Text, nullable), started_at, finished_at
      agent = relationship(back_populates="steps")  # Agent.steps ordered by id
  ```
- `db.py` — `make_engine(url)`, `make_session_factory(engine)`, `init_db(engine)` (`Base.metadata.create_all`). File-based SQLite with `check_same_thread=False` (gojira's threading pattern).
- `main.py` — `create_app(session_factory, *, config: Config, clients: "Clients | None" = None) -> FastAPI` storing `config`/`clients`/`session_factory` on `app.state`; `get_session` dependency; routes this task: `GET /api/healthz -> {"ok": true}`, `GET /api/catalog/models -> {"models": [{"id","label"}], "default": models[0].id}`. CORS middleware allowing all origins (dev; UI comes in P1b). `build_default_app()` wires real config/db (clients arrive Task 5).

- [ ] **Step 1:** Write failing tests. `api/tests/test_config.py`: `Config.load(None)` returns defaults without touching disk; a tmp yaml overrides `github_org` and coerces `workdir` to Path. `api/tests/test_api_basics.py`: build app via the fixtures below; `GET /api/healthz` → 200 `{"ok": True}`; `GET /api/catalog/models` → 200 with the default model listed and `default` set. Shared `api/tests/conftest.py`:

  ```python
  import pytest
  from fastapi.testclient import TestClient
  from sqlalchemy import create_engine
  from sqlalchemy.orm import sessionmaker

  from cortex_api.config import Config
  from cortex_api.db import init_db
  from cortex_api.main import create_app
  from cortex_api.models import Base


  @pytest.fixture
  def session_factory(tmp_path):
      engine = create_engine(
          f"sqlite:///{tmp_path}/cortex.db", connect_args={"check_same_thread": False}
      )
      Base.metadata.create_all(engine)
      return sessionmaker(bind=engine, expire_on_commit=False)


  @pytest.fixture
  def config(tmp_path):
      return Config(workdir=tmp_path / "work")


  @pytest.fixture
  def client(session_factory, config):
      return TestClient(create_app(session_factory, config=config))
  ```
- [ ] **Step 2:** Run `cd api && python -m pytest -q` (create a venv: `python3.12 -m venv venv && venv/bin/pip install -e ".[dev]"` once pyproject exists — write pyproject first, then tests fail on missing modules, not missing deps). Expected: ImportError failures.
- [ ] **Step 3:** Implement. `api/pyproject.toml` (hatchling; deps: fastapi, uvicorn, sqlalchemy>=2, pydantic>=2, pyyaml, httpx, jsonschema; `[dev]`: pytest, ruff). `api/Makefile`: `install` (venv pip install -e .[dev]), `test` (venv/bin/python -m pytest -q), `lint` (venv/bin/ruff check cortex_api tests), `check: lint test`, `run` (venv/bin/uvicorn --factory cortex_api.main:build_default_app --port 8100). Implement the four modules per the Interfaces block.
- [ ] **Step 4:** `make -C api check` → all pass.
- [ ] **Step 5:** Commit: `feat(api): cortex-api scaffold — config, models, app factory`

### Task 2: Catalogs — personas, skills, availability

**Files:**
- Create: `api/cortex_api/catalog.py`, `api/cortex_api/templates/personas/helpful-generalist.md`, `api/cortex_api/templates/personas/blank.md`
- Modify: `api/cortex_api/main.py` (three routes)
- Test: `api/tests/test_catalog.py`

**Interfaces:**
- Persona template file format: markdown with a comment-header first line `<!-- description: ... -->`; filename stem is the persona id. `helpful-generalist.md`:
  ```markdown
  <!-- description: A friendly, concise generalist assistant. -->
  You are {name}, a helpful assistant. Be concise, be warm, and say so plainly
  when you do not know something. When asked what you know, mention anything
  relevant from your memory.
  ```
  `blank.md`: `<!-- description: Start from scratch. -->` followed by `You are {name}.`
- `catalog.py`:
  ```python
  @dataclass
  class PersonaInfo: id: str; description: str; body: str      # body has {name} placeholder
  @dataclass
  class SkillInfo: id: str; description: str; path: Path; sha: str

  def list_personas(personas_dir: Path | None) -> list[PersonaInfo]   # None -> package templates
  def list_skills(skills_repo: Path | None) -> list[SkillInfo]
      # scans <repo>/skills/*/SKILL.md; parses yaml frontmatter between --- fences
      # for name + description; sha = `git -C <repo> rev-parse HEAD` (subprocess);
      # returns [] when skills_repo is None (skills optional in P1)
  NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,29}$")
  ```
- Routes: `GET /api/catalog/personas -> {"personas": [{"id","description"}]}`; `GET /api/catalog/skills -> {"skills": [{"id","description","sha"}]}`; `GET /api/availability?name=x -> {"available": bool, "reason": str | None}` (reason "invalid name" on regex fail, "name taken" when an Agent row exists; repo-level collision is the repo step's job).

- [ ] **Step 1:** Failing tests: personas listed from package templates with descriptions parsed; `{name}` present in body; skills scan against a tmp fixture repo (create `skills/echo/SKILL.md` with frontmatter `name: echo` / `description: Echoes things.` and `git init/commit` it so rev-parse works); `list_skills(None) == []`; availability: bad regex → available false + "invalid name"; fresh name → true; after inserting an Agent row → false + "name taken".
- [ ] **Step 2:** Run: expected ImportError/404 failures.
- [ ] **Step 3:** Implement per interfaces (frontmatter parse: split on `---` fences, `yaml.safe_load` the middle; description falls back to first body line).
- [ ] **Step 4:** `make -C api check` green.
- [ ] **Step 5:** Commit: `feat(api): persona + skill catalogs and availability check`

### Task 3: Composer — bundle tree + schema validation

**Files:**
- Create: `api/cortex_api/composer.py`, `api/cortex_api/vendor/bundle.schema.json` (verbatim copy: `cp ../daedalus/docs/bundle.schema.json api/cortex_api/vendor/`)
- Test: `api/tests/test_composer.py`

**Interfaces:**
- ```python
  @dataclass
  class BundleSpec:
      name: str
      persona_body: str            # already {name}-substituted by caller
      model_id: str
      skills: list[SkillInfo]      # may be empty
      runtime_image: str
      vertex_project: str
      vertex_location: str

  def compose(spec: BundleSpec, dest: Path) -> None
      # writes dest/: Dockerfile, bundle/{agent.yaml, agent-card.json, persona.md,
      # knowledge/gotchas.md, skills/<id>/** (copied trees)}
  def validate_agent_yaml(bundle_dir: Path) -> None   # jsonschema against vendor schema; raises ComposeError
  class ComposeError(Exception): ...
  ```
- Generated `agent.yaml` (dict → `yaml.safe_dump(sort_keys=False)`), exactly:
  ```yaml
  name: <name>
  card: agent-card.json
  persona: persona.md
  model:
    id: ${AGENT_MODEL:-<model_id>}
    max_tokens: 1024
    budget: {per_task_tokens: 50000}
  transports:
    - {kind: a2a, host: 0.0.0.0, port: 8080}
  tools: {provider: none, allow: []}
  memory: {personal: techne, home: /techne}
  checkpoints: sqlite
  approvals:
    ceiling: {write: false}
    gates: [{match: "*", require: plan_approval}]
    escalation: {route: cli}
  ```
  (Write the literal `${AGENT_MODEL:-...}` string. Validation substitutes the default before jsonschema: `re.sub(r"\$\{AGENT_MODEL:-([^}]*)\}", r"\1", text)`.)
- `agent-card.json`: `{"name", "description": "Assembled by cortex.", "version": "0.1.0", "capabilities": [], "skills": [{"id": s.id} for s in skills]}`
- `knowledge/gotchas.md`: `## Deployment chain\nYou were assembled in the cortex assembly room and deployed through the\nplatform chain: repo, onboarding, build, deploy, first contact. If someone\nasks how you got here, tell them that.\n`
- `Dockerfile`: `# Agent image: the shared runtime plus this agent's bundle.\nFROM <runtime_image>\nCOPY bundle/ /bundle\n`
- Skills: `shutil.copytree(skill.path.parent, dest/"bundle"/"skills"/skill.id)` (copy the whole skill dir, SKILL.md included — the sync-plugins copy-don't-symlink pattern).

- [ ] **Step 1:** Failing tests: compose into tmp → exact file set exists; agent.yaml parses and carries host `0.0.0.0`, port 8080, empty allowlist, catch-all gate, `${AGENT_MODEL:-vertex_ai/claude-sonnet-4-5}`; card lists picked skills; skill tree copied; `validate_agent_yaml` passes on the composed bundle and FAILS (ComposeError) when the test corrupts `transports` to a string; Dockerfile FROM matches runtime_image.
- [ ] **Step 2:** Run → failures. **Step 3:** Implement. **Step 4:** `make -C api check` + one extra check: load the composed bundle with real daedalus if available — add test guarded by `pytest.importorskip("daedalus")` that runs `Bundle.load` on the composed dir (install daedalus into the venv from the sibling checkout in Makefile `install`: `pip install -e ../../daedalus` — if that path complicates CI later, the importorskip keeps it optional). **Step 5:** Commit: `feat(api): bundle composer with vendored-schema validation`

### Task 4: Clients — Protocols, fakes, real implementations

**Files:**
- Create: `api/cortex_api/clients.py`, `api/tests/fakes.py`
- Test: `api/tests/test_clients.py`

**Interfaces (every step in Tasks 6–8 consumes these):**
```python
class CommandRunner:   # gojira's seam, copied in spirit: run(list[str]) -> str, raises ClientError on rc!=0
class ClientError(RuntimeError): ...

class GitHubClient(Protocol):
    def repo_exists(self, repo: str) -> bool: ...                 # gh repo view
    def create_repo_from_dir(self, repo: str, src: Path) -> None: ...  # git init/branch -M main/add/commit + gh repo create --private --source --push
    def clone(self, repo: str, dest: Path) -> None: ...
    def commit_push(self, workdir: Path, message: str) -> bool: ...    # False when nothing changed
    def latest_run(self, repo: str) -> dict: ...                  # {"id": int, "status": str, "conclusion": str|None} via gh run list --limit 1 --json

class GojiraClient(Protocol):
    def create_app(self, *, name, repo_full_name, cluster, namespace) -> int: ...   # POST /api/v1/apps auto_merge_setup_pr=True -> id; 409 -> GojiraConflict
    def app_status(self, app_id: int) -> dict: ...                # GET /api/v1/apps/{id}
    def reprovision(self, app_id: int) -> None: ...               # POST /apps/{id}/provision

class CloudClient(Protocol):
    def ensure_gsa(self, name: str) -> str: ...                   # describe-then-create; returns email
    def bind_workload_identity(self, gsa_email: str, namespace: str, ksa: str) -> None: ...
    def add_to_group(self, gsa_email: str, group: str) -> None: ...  # tolerate already-a-member

class AgentClient(Protocol):
    def hello(self, namespace: str, service: str, text: str) -> str: ...
    # real impl: kubectl port-forward svc/<service> :80 on a free local port (subprocess,
    # parse the "Forwarding from 127.0.0.1:PORT" line), POST /a2a/tasks bare envelope
    # {"kind":"task","id":"hello-<uuid4hex>","history":[{"kind":"message","role":"user",
    # "parts":[{"kind":"text","text":text}]}]}, return status.message text; always
    # terminate the port-forward (try/finally)

class KubectlClient(Protocol):
    def rollout_ok(self, namespace: str, deploy: str, timeout_s: int = 180) -> bool: ...

@dataclass
class Clients:
    github: GitHubClient; gojira: GojiraClient; cloud: CloudClient
    agent: AgentClient; kubectl: KubectlClient
    @classmethod
    def real(cls, config: Config) -> "Clients": ...
```
- Gojira auth: when `config.gojira_token` set, send `Authorization: Bearer <token>` on API calls.
- Fakes (in `api/tests/fakes.py`): in-memory recorders mirroring the P0-observed behaviors — `FakeGojira` walks an app through pending→active on the Nth poll and records reprovision calls; `FakeGitHub` records repos/pushes, `latest_run` returns a configurable sequence ending in success; `FakeCloud` records calls, idempotent; `FakeAgent.hello` returns a canned reply; `FakeKubectl.rollout_ok` True. Each fake takes constructor knobs to simulate the failure paths tests need.

- [ ] **Step 1:** Failing tests: real clients' *command construction* via a `RecordingRunner` (assert exact argv for `ensure_gsa` create path, `bind_workload_identity`, `add_to_group`, `gh repo create`, `gh run list`) — commands mirror the P0 runbook verbatim; `GojiraClient` request/URL/auth-header shapes via `httpx.MockTransport`; fakes satisfy the Protocols (isinstance-style structural checks by calling each method once).
- [ ] **Step 2:** Run → failures. **Step 3:** Implement (real gcloud argv: `gcloud iam service-accounts describe|create`, `add-iam-policy-binding ... --role roles/iam.workloadIdentityUser --member serviceAccount:<project>.svc.id.goog[<ns>/<ksa>]`, `gcloud identity groups memberships add --group-email <group> --member-email <gsa>` treating "already exists/member" stderr as success). **Step 4:** check green. **Step 5:** Commit: `feat(api): platform clients — protocols, real impls, test fakes`

### Task 5: Step engine — ledger, fire-and-return, retry

**Files:**
- Create: `api/cortex_api/provisioner.py` (engine only; steps arrive in 6–8)
- Test: `api/tests/test_engine.py`

**Interfaces:**
```python
class Step(Protocol):
    name: str
    def run(self, ctx: "Ctx") -> str: ...        # returns detail; raises to fail

@dataclass
class Ctx:
    agent: Agent; session: Session; config: Config; clients: Clients
    workdir: Path                                 # per-agent scratch under config.workdir

def run_pipeline(session_factory, agent_id: int, *, config, clients, steps: list[Step]) -> threading.Thread
    # daemon thread (gojira's start_provision shape). For each step: get-or-create the
    # AgentStep row; if already DONE/SKIPPED -> skip (resume semantics); else RUNNING ->
    # run() -> DONE(+detail in error column? no — detail discarded, error stays NULL) or
    # FAILED(error=str(exc), remaining steps -> FAILED "aborted: <failed step>").
    # Agent.status: PROVISIONING at start; READY when all steps DONE/SKIPPED; FAILED otherwise.
def reset_failed_steps(session, agent_id) -> None   # FAILED -> PENDING (retry primitive)
```

- [ ] **Step 1:** Failing tests with two toy steps: happy path (statuses DONE/DONE, agent READY); second step raises (FAILED with message, agent FAILED); resume (first DONE, re-run pipeline after reset_failed_steps → only second re-executes — assert via a counting step); thread joins within 5s (gojira's file-SQLite threading pattern from conftest).
- [ ] **Step 2:** Run → failures. **Step 3:** Implement. **Step 4:** check green. **Step 5:** Commit: `feat(api): provisioning step engine with resumable ledger`

### Task 6: Steps — repo, onboard, access

**Files:**
- Create: `api/cortex_api/steps.py`
- Test: `api/tests/test_steps.py`

**Interfaces:** each is a class with `name` and `run(ctx) -> str`:
- `RepoStep` (`name="repo"`, label "Repository"): repo = `f"{config.github_org}/agent-{agent.name}"`; if `repo_exists` → raise unless the DB row already recorded it (idempotent resume). Compose the bundle: build `BundleSpec` from the agent row + catalogs (persona body `{name}`-formatted, skills resolved by id), `compose()` into `ctx.workdir/"repo"`, `validate_agent_yaml`, `create_repo_from_dir`. Sets `agent.repo_full_name`.
- `OnboardStep` (`name="onboard"`, "Platform onboarding"): `create_app(name=agent.name, repo_full_name=..., cluster=config.gojira_cluster, namespace=agent.name)` → `agent.gojira_app_id`; on GojiraConflict with a matching recorded id, continue (resume). Poll `app_status` every 5s up to 300s until `active` (raise on `failed`, including the failed step names in the message).
- `AccessStep` (`name="access"`, "Model access"): `ensure_gsa(f"agent-{agent.name}")` → `bind_workload_identity(email, namespace=agent.name, ksa=f"agent-{agent.name}")` → `add_to_group(email, config.model_group)`.

- [ ] **Step 1:** Failing tests through fakes: happy path sets repo_full_name/gojira_app_id and calls fakes with exact arguments; OnboardStep raises with gojira's failed-step names surfaced; RepoStep resume tolerates existing repo when the row matches; AccessStep argv-level assertions live in Task 4, here assert fake call ordering.
- [ ] **Step 2:** Run → failures. **Step 3:** Implement. **Step 4:** check green. **Step 5:** Commit: `feat(api): repo, onboard, and model-access steps`

### Task 7: Steps — wire (the P0-gotcha step), build, deploy

**Files:**
- Modify: `api/cortex_api/steps.py`
- Test: `api/tests/test_steps_wire.py`

**Interfaces:**
- `WireStep` (`name="wire"`, "Deploy wiring"): clone the repo fresh into `ctx.workdir/"wire"`; then:
  1. Write `k8s/base/serviceaccount.yaml` (KSA `agent-<name>`, namespace `<name>`, WI annotation to the GSA email) and `k8s/base/pvc.yaml`:
     ```yaml
     # App-authored (preserved by the generator): the agent's memory volume.
     apiVersion: v1
     kind: PersistentVolumeClaim
     metadata: {name: agent-<name>-techne, namespace: <name>}
     spec:
       accessModes: [ReadWriteOnce]
       resources: {requests: {storage: 1Gi}}
     ```
  2. **Register both in `k8s/base/kustomization.yaml`** (parse yaml, append to `resources` if absent, dump back — never regenerate the file).
  3. Edit `gojira.yaml` (yaml round-trip): set `service_account: agent-<name>`, `env: {VERTEXAI_PROJECT: config.vertex_project, VERTEXAI_LOCATION: config.vertex_location}`, `data_mount: {pvc: agent-<name>-techne, path: /techne, fs_group: 10001}` (uid of the runtime image's `agent` user).
  4. `commit_push`; if it returned True, `reprovision(agent.gojira_app_id)` and poll to `active` (same poll helper as OnboardStep — factor `_wait_active(ctx) `).
- `BuildStep` (`name="build"`, "Image build"): poll `latest_run` every 10s up to 600s until `status == "completed"`; raise unless `conclusion == "success"`.
- `DeployStep` (`name="deploy"`, "Deploy"): `rollout_ok(namespace=name, deploy=name)` else raise.

- [ ] **Step 1:** Failing tests: WireStep writes both manifests AND kustomization gains exactly two new entries (assert the P0 gotcha is encoded: parse the fake-clone's kustomization after run); gojira.yaml carries service_account/env/data_mount with fs_group 10001; no-change clone (second run) skips reprovision; BuildStep failure conclusion raises with run id in message; DeployStep raises on rollout failure.
- [ ] **Step 2:** Run → failures. **Step 3:** Implement (fake clone: FakeGitHub.clone materializes a scaffolding fixture — deployment/service/podmonitoring/rules + generated kustomization + gojira.yaml, mirroring what gojira generated in P0). **Step 4:** check green. **Step 5:** Commit: `feat(api): wire, build, and deploy steps (kustomization registration encoded)`

### Task 8: Hello step + public API routes

**Files:**
- Modify: `api/cortex_api/steps.py` (`HelloStep`), `api/cortex_api/main.py` (routes), `api/cortex_api/provisioner.py` (`PIPELINE = [RepoStep(), OnboardStep(), AccessStep(), WireStep(), BuildStep(), DeployStep(), HelloStep()]`)
- Test: `api/tests/test_api_agents.py`

**Interfaces:**
- `HelloStep` (`name="hello"`, "First contact"): `clients.agent.hello(namespace=name, service=name, text="hello! how did you get here?")` → store `agent.first_reply`.
- Routes:
  - `POST /api/agents` body `{"name": str, "persona": str, "skills": list[str] = [], "model": str | None}` → validate: NAME_RE, persona exists, every skill exists, model in config (None → default). 409 name taken; 422 otherwise-invalid. Insert Agent (PENDING), start `run_pipeline`, return `201 {"name", "status": "pending"}` (status captured pre-thread — the gojira lesson).
  - `GET /api/agents` → `{"agents": [{name, status, model, created_at}]}`
  - `GET /api/agents/{name}` → full row + `steps: [{name, label, status, error}]` + `first_reply` (labels from a `STEP_LABELS` dict: repo→Repository, onboard→Platform onboarding, access→Model access, wire→Deploy wiring, build→Image build, deploy→Deploy, hello→First contact — the UI's checklist vocabulary).
  - `POST /api/agents/{name}/retry` → 409 unless status FAILED; `reset_failed_steps`, re-run pipeline, 202.
- `build_default_app()` now wires `Clients.real(config)`.

- [ ] **Step 1:** Failing tests (gojira e2e-through-fakes shape): POST → 201 pending; poll GET until READY (deadline loop, 5s) → all seven steps DONE, first_reply is the fake's canned text; duplicate POST → 409; unknown persona/skill/model → 422; failure injection (FakeGojira set to fail) → FAILED with the failing step's error surfaced in GET; retry after fixing the fake → READY, and previously-DONE steps did not re-execute (counting fake).
- [ ] **Step 2:** Run → failures. **Step 3:** Implement. **Step 4:** `make -C api check` — full suite green. **Step 5:** Commit: `feat(api): agent create/list/detail/retry — the one-click pipeline`

### Task 9: Runbook config + serve wiring

**Files:**
- Create: `api/config.example.yaml` (extend from Task 1 if stubbed): every Config field with placeholder values and comments explaining each (real values go in gitignored `config.local.yaml`)
- Modify: `README.md` (short "Running the API (dev)" subsection under Status: make install, copy config.example → config.local, `make -C api run`)
- Test: none beyond existing suite (docs task folded here because Task 10–11 depend on the config contract being documented)

- [ ] **Step 1:** Write both files. **Step 2:** `make -C api check` still green; vendor-name scan from the session ledger (the two employer/work-app terms) → no hits. **Step 3:** Commit: `docs(api): example config and dev run instructions`

### Task 10: daedalus — SIGKILL-safe seed convergence (ground truth)

**Files (daedalus repo, branch `feat/runtime-image`):**
- Modify: `docker/seeder.py`, `docker/test_seeder.py`

**Interfaces:**
- `sync(...)` gains keyword `list_ids_fn: Callable[[Path, str], list[int]] | None = None` — returns entry ids whose source equals the given string. When a file is seeded with NO prior state row and `list_ids_fn` is provided, first delete `list_ids_fn(techne_db, f"seed:{filename}")` ids (ground-truth convergence: a SIGKILL between stores and the state commit can no longer orphan duplicates). Replace the TODO(P1) comment with the implementation. `main()` wires it via `knowledge.list_entries(db_path, include_superseded=True)` filtered on exact source match (reuse the purge helper's enumeration, factored into `_ids_by_source`).
- Tests: simulate the SIGKILL aftermath — seed once via `store_fn` directly (entries in fake store, no state row), then `sync` with `list_ids_fn` → old ids deleted, new ids stored once, no duplicates; without `list_ids_fn` behavior unchanged (existing 11 tests pass unmodified).

- [ ] **Step 1:** Failing tests (2 new). **Step 2:** Run → fail. **Step 3:** Implement. **Step 4:** `uv run --extra dev python -m pytest docker/test_seeder.py -q` → 13 passed; `make check && ruff check docker/` green. **Step 5:** Commit in daedalus: `feat(docker): ground-truth seed convergence — survive SIGKILL between store and state commit`. Then rebuild/push the runtime image (`make image-push AR_IMAGE=<from deploy config>`) and record the new tag for Task 11.

### Task 11: E2E milestone — cortex-api drives a real agent (manual, with operator)

- [ ] Write `api/config.local.yaml` from the P0 runbook's deploy config (operator supplies values; file is gitignored). Point `runtime_image` at Task 10's new tag; `skills_repo_path` at the local skills checkout.
- [ ] Laptop gojira (branch) running as in P0. `make -C api run`.
- [ ] `curl -X POST localhost:8100/api/agents -d '{"name":"clio","persona":"helpful-generalist","skills":[],"model":null}'` → poll `GET /api/agents/clio` to READY; record `first_reply`.
- [ ] Verify PVC persistence: `kubectl -n clio get pvc` bound; `kubectl -n clio exec deploy/clio -- ls /techne` shows techne.db + seed_state.db; delete the pod, after restart logs show `seeded=0 removed=0 unchanged=1` (convergence over the PVC — the P1 memory goal).
- [ ] Ask clio a follow-up over a2a referencing earlier conversation? (No — checkpoints are per-task; just confirm a second hello works.)
- [ ] Append results to `docs/superpowers/plans/p0-e2e-runbook.md` under a `## P1a results` heading; commit.

---

## Self-review notes (applied)

- Spec §9 P1 coverage: composer/provisioner/access step/integrations (Tasks 3–8), model picker data (Task 1 models endpoint), per-agent techne on PVCs (Tasks 7, 10, 11). UI items are P1b by explicit scope split. Knowledge editing/chat/Slack/avatars remain P2 per spec.
- P0 findings encoded as constraints or step behavior: kustomization registration (Task 7, tested), a2a host/port (Task 3, tested), full-path ar_repo (N/A — cluster registration stays a gojira concern; documented in config comments), envelope shape (Task 4 AgentClient), status-before-thread (Task 8).
- Type consistency pass: `SkillInfo` flows catalog→composer→RepoStep; `Clients` field names match step usage; step `name` strings match `STEP_LABELS` and tests.
- No gojira changes anywhere (its branch is frozen pending PRs); daedalus changes confined to `docker/` on the existing branch.
