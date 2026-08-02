# P1c — Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every backlog item that has bitten a live run (or would bite the next one): the concurrent-CI stale-apply race, first-build auth-propagation failures, interrupted-provision wedges, and the accumulated correctness/cleanup batch across cortex-api, cortex-ui, and the two sibling branches.

**Architecture:** Nine small tasks. Root-cause fixes land at the source (the deploy platform's generated CI gets a per-repo concurrency group; cortex's BuildStep pins to the expected commit and can re-dispatch a failed run so the product Retry is self-sufficient), with a defense-in-depth check in DeployStep (live spec must carry the wired contract). A startup sweep makes API restarts safe. The rest is the triaged minor batch, each item traceable to a ledgered review finding. Exit: a brand-new agent (`echo`) created against the hardened stack reaches READY **without any operator intervention**, even across a first-build auth-propagation failure.

**Tech Stack:** unchanged (Python/FastAPI, pytest; Svelte 5/vitest; Jinja2 golden tests in the deploy platform repo).

## Global Constraints

- Never the employer's name or work-app name in any tracked file; deploy values only in gitignored config.
- Sibling-repo work stays on the existing branches (deploy platform: `feat/agent-deploys`; runtime kit: `feat/runtime-image`); never their main.
- TDD per task; `make -C api check` / `make -C ui check` / each sibling repo's own suite green before every commit; **git add only named files**.
- All step/route behavior changes keep the existing 162 API + 85 UI tests passing unmodified unless a task explicitly sanctions an update (each such case is named).

---

### Task 1: Deploy platform — serialize generated CI per repo

**Repo:** ~/Documents/GitHub/gojira, branch `feat/agent-deploys`.
**Files:** Modify `gojira/generator/templates/workflows/ci.yaml.j2` and `promote.yaml.j2` (if it lacks one); update golden fixtures under `tests/golden/*/.github/workflows/`.

The live incident: the wire-commit run and the regeneration-PR-merge run applied `kubectl` within ~1s of each other and the stale tree landed last. Fix at the root: a workflow-level concurrency group so runs on the same repo queue instead of interleaving:

```yaml
concurrency:
  group: gojira-deploy-${{ github.repository }}
  cancel-in-progress: false
```

- [ ] Failing golden test first (goldens won't contain the block), then template edit, regenerate goldens per that repo's documented golden-update flow (read how its generator tests bless fixtures — follow it), full suite green (285+).
- [ ] Commit: `fix(generator): serialize CI runs per repo — concurrent applies raced in the field`

### Task 2: BuildStep — pin to expected commit + self-sufficient retry

**Repo:** cortex. **Files:** `api/cortex_api/clients.py` (GitHubClient additions + fake), `api/cortex_api/steps.py` (BuildStep), tests.

Two ledgered defects: BuildStep watches `latest_run` (can latch a stale run), and on retry it re-polls the same failed run forever (muse needed a manual `gh run rerun`). Fix:
- `GitHubClient` gains `head_sha(repo) -> str` (`gh api repos/<repo>/commits/<default-branch> --jq .sha` — or `gh run list`'s headSha; pick one, argv-test it), `runs_for_commit(repo, sha) -> list[dict]` (`gh run list --repo R --commit SHA --json databaseId,status,conclusion`), and `rerun(repo, run_id) -> None` (`gh run rerun <id> --repo R`). Fake mirrors with knobs (a run sequence per sha; rerun flips the failed run to a fresh queued/success sequence).
- `BuildStep.run`: resolve the expected sha once (head of default branch at step entry); poll `runs_for_commit`; no runs yet → keep polling (registration lag); run completed+failure → **rerun it once per step invocation** (log + detail notes the rerun) and continue polling; second failure in the same invocation → raise with run id. Completed+success → done. Cadence constants unchanged.
- Tests: stale-run immunity (older sha's success ignored — runs_for_commit keyed by sha does this structurally; test the empty-then-appearing sequence), propagation self-heal (failure → rerun → success), double-failure raises, rerun-once-per-invocation.
- [ ] Commit: `fix(api): BuildStep pins to head commit and self-heals first-run auth failures`

### Task 3: DeployStep — verify live spec carries the wired contract

**Repo:** cortex. **Files:** `api/cortex_api/clients.py` (KubectlClient addition + fake), `api/cortex_api/steps.py` (DeployStep), tests.

Defense-in-depth for the stale-apply class: after `rollout_ok`, assert the live Deployment actually has the wire step's contract. `KubectlClient` gains `deploy_env_names(namespace, deploy) -> list[str]` (jsonpath over `.spec.template.spec.containers[0].env[*].name`) and `deploy_service_account(namespace, deploy) -> str`. DeployStep raises with a remediation hint ("live spec is missing the wired model env — a concurrent CI apply may have raced; retry after the latest run completes") when `VERTEXAI_PROJECT`/`VERTEXAI_LOCATION` absent or SA != `agent-<name>`. Tests: happy, missing-env raises with hint, wrong-SA raises.
- [ ] Commit: `fix(api): DeployStep verifies live spec matches the wired contract`

### Task 4: Startup sweep + thread-launch guard

**Repo:** cortex. **Files:** `api/cortex_api/provisioner.py` (sweep fn), `api/cortex_api/main.py` (call in build_default_app + wrap both run_pipeline launches), tests.

- `sweep_interrupted(session_factory) -> int`: agents in PENDING/PROVISIONING → FAILED with error "interrupted by an API restart — retry when ready"; their RUNNING steps → FAILED("interrupted"). Called once from `build_default_app` (NOT from create_app, so tests' fixtures are unaffected).
- Both routes wrap `run_pipeline(...)` in try/except → revert agent to FAILED with the launch error (closes the thread-launch wedge, ledgered twice).
- Tests: sweep flips the right rows and counts; launch-failure test via monkeypatched run_pipeline raising → 500-free JSON error response (pick 503 {"error": ...}) and agent FAILED (retryable).
- [ ] Commit: `fix(api): startup sweep for interrupted provisions; launch failures land FAILED`

### Task 5: API hardening batch

**Repo:** cortex. **Files:** `api/cortex_api/{catalog,config,clients,main}.py`, `api/config.example.yaml`, tests.

Each item is a ledgered finding — implement all, test each:
1. Name rules: max length 24 (GSA account-id headroom), no trailing hyphen, reserved list `{"default","kube-system","kube-public","gojira","cortex"}` — enforce in the availability check AND create validation with distinct reasons. **Sanctioned test updates:** NAME_RE boundary tests move from 30→24; UI's validate.ts already forbids trailing hyphens (leave UI; server now matches).
2. `Config.models` gains `min_length=1`; non-dict YAML in `Config.load` raises a clear ValueError naming the file.
3. `ensure_gsa`: discriminate NOT_FOUND (stderr contains "NOT_FOUND" or "does not exist") from other describe failures — only NOT_FOUND falls through to create; other errors raise.
4. `gsa_email` becomes prefix-agnostic (`f"{name}@{project}..."`); both callers build the full `agent-<name>` themselves; fake and real now behave identically for any name.
5. A2A client: after `proc.kill()` on TimeoutExpired, follow with `proc.wait(timeout=2)` best-effort (zombie reap).
6. `config.example.yaml`: comment on gojira_cluster noting the platform's cluster registration needs the FULL AR path (the P0 ar_repo finding).
7. Cleanup: consolidate the three expanduser validators into one multi-field validator; fix the two test-idiom nits (pytest.raises, unused monkeypatch params); type the untyped `config, agent` helper params in steps.py; remove the redundant retry invalidation + correct its comment.
- [ ] Commit: `fix(api): name limits + reserved names, config validation, gsa/client hardening, cleanup batch`

### Task 6: Skill pinning — materialize at the recorded sha

**Repo:** cortex. **Files:** `api/cortex_api/composer.py` or `catalog.py` (pick the seam: pinning belongs where the copy happens), `api/cortex_api/steps.py` (RepoStep passes recorded shas on retry), tests.

The README claims "pinned at assembly"; today a retry re-copies from the working tree. Fix: when a skill carries a recorded sha, materialize via `git -C <skills_repo> archive <sha> -- skills/<id> | tar -x -C <dest-tmp>` (subprocess, argv-tested through CommandRunner) instead of copytree from the tree; fall back with a clear error if the sha is unreachable. First composition records HEAD sha as today (composer unchanged there); RepoStep's re-resolution passes the row's recorded shas through. Tests: archive argv exact; retry after the fixture repo advances HEAD still ships the OLD content (assert file bytes); unreachable sha raises with the sha in the message.
- [ ] Commit: `fix(api): skills materialize at their recorded sha — pinning enforced`

### Task 7: UI cleanup batch

**Repo:** cortex. **Files:** `ui/src/lib/{toast.svelte.ts,api.ts}`, `ui/src/components/{Toasts,AgentCard,CreateModal}.svelte`, `ui/src/pages/{Dashboard,AgentDetail}.svelte`, tests.

Ledgered minors, all in one pass:
1. Toast timer handles stored + cleared on manual dismiss (Map by id); test.
2. Persistent `aria-live` container in Toasts (region exists before toasts insert).
3. `errorMessage(err)` hoisted to `lib/api.ts`; both pages use it; quote-stripping becomes token-scoped (`/'([^']*)'/g → $1` — contractions like "isn't" survive; test in a new small lib test) and applies uniformly (modal + pages).
4. AgentCard uses `agentTone(status) === "fail"` instead of the raw literal.
5. CreateModal: on a 409 submit error, availability state flips to unavailable ("name taken") so the two indicators can't contradict.
6. Remove the vestigial `step-checklist__summary` class; give the idle status div a fixed min-height (kills both the gap sliver and layout shift).
- [ ] Commit: `fix(ui): toast/live-region hardening, unified error copy, modal consistency`

### Task 8: Runtime kit — corrupt-path combination test

**Repo:** ~/Documents/GitHub/daedalus, branch `feat/runtime-image`. **Files:** `docker/test_seeder.py` only.

The ledgered gap: `purge_fn` + `list_ids_fn` are always wired together by `main()`, but no test exercises corrupt-state recovery with BOTH present. Add one test: corrupt state db + both fns → quarantine happens, purge called once, per-file `list_ids_fn` path returns empty post-purge (no double-delete, counts correct), full reseed. Suite `uv run --extra dev python -m pytest docker/test_seeder.py -q` → 14 passed; `make check` unchanged.
- [ ] Commit: `test(docker): corrupt-state recovery with purge + ground-truth convergence combined`

### Task 9: E2E — the self-healing create (milestone)

After Tasks 1-6 land: rebuild nothing (runtime image unchanged); restart cortex-api (picks up the new code — the startup sweep will mark any stale rows); the deploy-platform branch change (Task 1) affects only NEWLY generated repos — perfect, the test agent gets it.

- [ ] `curl -X POST localhost:8100/api/agents -d '{"name":"echo","persona":"helpful-generalist","skills":[],"model":null}'` → poll to terminal. **Pass criteria:** READY 7/7 with first words and **zero operator interventions** — if the first CI run hits the propagation failure, BuildStep's self-heal must absorb it (check the step detail notes a rerun happened); the generated workflow in echo's repo must contain the concurrency block.
- [ ] Append results to the runbook (`## P1c results`), commit: `P1c results: self-healing create verified`
- [ ] Note for the operator: hello/clio/muse/echo all remain deployed; teardown is manual (namespace + repo + GSA) and deliberately out of scope.

---

## Self-review notes (applied)

- Every task maps to a ledgered finding; nothing new invented. Items deliberately left for P2 (per the final reviewers' own triage): model labels on cards/detail, Modal initialFocus, component test harness, knowledge editing + skill re-pin UX.
- Cross-task types: BuildStep's new client methods (Task 2) and DeployStep's (Task 3) both extend the same Protocols + fakes established in P1a Task 4; Task 6's archive path reuses CommandRunner.
- Task 1 is the only deploy-platform change; Task 8 the only runtime-kit change — both test-gated by their own suites, both stay on their branches.
