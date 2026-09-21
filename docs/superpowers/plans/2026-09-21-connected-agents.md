# Connected Agents Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A team connects a repo it already owns; the room deploys it, gives it an identity and model access, shows what is true about it, and never pushes to its branches.

**Architecture:** One new row marker (`agents.kind`) read by four things: the pipeline selector, the destroy ledger, the read routes, and the UI's tab model. The connected pipeline reuses the existing steps where they are already kind-agnostic (onboard, access, build, deploy) and swaps three (connect instead of repo, a room-only avatar, wiring by pull request). Spec: `docs/superpowers/specs/2026-09-21-connected-agents-design.md`.

**Tech Stack:** FastAPI + SQLAlchemy 2 + alembic (api/), the `gh` CLI behind `GitHubClient`, Svelte 5 + tanstack-query (ui/), pytest with the fakes in `api/tests/fakes.py`.

## Global Constraints

- `kind` values are exactly `"composed"` and `"connected"`; `run_mode` values exactly `"http"` and `"headless"`. They live as named constants in a new `api/kobo_api/kinds.py` and are compared against those constants, never truth-tested (the `archetype` precedent, `composer.py:124-134`).
- Nothing about a composed agent changes: same step names, same order, same labels, same destroy. `ALL_STEP_NAMES` in `api/tests/test_api_agents.py` must pass untouched.
- The room NEVER calls `commit_push`, `create_repo_from_dir`, `rename_repo` or `archive_repo` on a connected agent's repo. Tests assert the fake recorded none of them.
- A connected repo must be `<config.github_org>/<anything>`. Anything else is refused with a plain sentence.
- Headless is accepted only when `Config.connected_headless_class` is non-empty (absent = feature off). Otherwise 422 with: `agents with no HTTP port are not available in this room yet`.
- Every user-facing string (route errors, step errors, UI copy): plain words, no em dashes (the UI copy test enforces it for `.svelte`; `destroy.py:12-14` states it for step errors), no infrastructure vocabulary the create flow does not already use.
- Step failure messages land verbatim in `AgentStep.error`, which any viewer of the agent can read: never put a token, a raw exception string from a client, or a file's contents in one.
- Tests never run alembic: a model column without a migration passes pytest and fails CI's schema tripwire. Every model change in this plan ships its migration in the same commit. New migration's `down_revision` is the current head (`f3c1d7b6a920` at the time of writing; re-check).
- Run API tests from `api/`: `.venv/bin/python -m pytest -q > /tmp/kobo-connected.txt 2>&1; echo "exit=$?"` then read the file. NEVER pipe a test or lint command into tail/head. Known unrelated local failure: `tests/test_engine.py::test_two_processes_racing_a_failed_steps_resume_run_it_exactly_once`. Ruff baseline is 45 errors (`.venv/bin/ruff check kobo_api tests`); do not add to it.
- UI: `npm run check` and `npm run build` in `ui/` (redirect to a file, echo the exit code). Do NOT run vitest locally (it hangs on this machine; CI runs it). Never `pkill -f vitest`.
- No client or organisation names anywhere. Comments state constraints and reasons in the surrounding style; they never narrate the change.
- `git add <named files>` only. Commits end with a blank line then `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

---

### Task 1: The row, the constants, and the read paths

**Files:**
- Create: `api/kobo_api/kinds.py`, `api/migrations/versions/<rev>_agent_kind.py`
- Modify: `api/kobo_api/models.py` (Agent), `api/kobo_api/main.py` (list + detail response dicts, ~3467-3555), `api/kobo_api/mcp/tools/read.py` (`kobo_list_agents`, `kobo_get_agent`), `api/kobo_api/config.py` (`connected_headless_class: str = ""`, deployment-local like `slack_tokens_secret`; not an org key), `ui/src/lib/api.ts` (`AgentSummary.kind`)
- Test: `api/tests/test_models.py` or the nearest model test, `api/tests/test_api_agents.py`, `api/tests/test_mcp_tools_read.py`, `api/tests/test_org.py` (the new config key is not org-settable)

**Interfaces:**
- Produces: `kinds.KIND_COMPOSED = "composed"`, `kinds.KIND_CONNECTED = "connected"`, `kinds.RUN_MODE_HTTP = "http"`, `kinds.RUN_MODE_HEADLESS = "headless"`, `kinds.is_connected(agent) -> bool`.
- Produces: `Agent.kind: Mapped[str]` `String(16)`, `nullable=False`, `default=KIND_COMPOSED`, `server_default="composed"`; `Agent.run_mode: Mapped[str | None]` `String(16)`; `Agent.secret_names: Mapped[list]` `JSON`, `nullable=False`, `default=list`, `server_default="[]"` (check how `skills` JSON is declared and migrated and follow it; if a JSON `server_default` is awkward on SQLite, make the column nullable and read it through a helper that returns `[]`).
- Produces: `"kind"` on both HTTP read routes and both MCP read tools.

- [ ] Failing tests: a new `Agent` row defaults to `kind == "composed"`, `run_mode is None`, `secret_names == []`; `GET /api/agents` and `GET /api/agents/{name}` carry `"kind": "composed"`; both MCP read tools carry it.
- [ ] Implement model + migration (one `op.add_column` per column, docstring in the style of `c7d3a9f21b84_agent_archetype_marker.py` saying what reads the column and why existing rows are `composed`).
- [ ] Run `alembic upgrade head` + `scripts/schema_tripwire.py` against a throwaway SQLite URL the way the previous migration's author verified it (see `.github/workflows/ci.yml` for the exact commands), and `alembic downgrade -1`.
- [ ] Full suite, ruff, commit: `feat(api): an agent row knows whether the room composed it or a team connected it`.

### Task 2: The connected pipeline

**Files:**
- Modify: `api/kobo_api/provisioner.py` (`CONNECTED_PIPELINE`, `pipeline_for(agent)`, `STEP_LABELS` built from the union, the READY hooks), `api/kobo_api/steps.py` (new `ConnectStep`, `ConnectedWireStep`; kind-aware `AvatarStep`, `OnboardStep`, `AccessStep`, `BuildStep`), `api/kobo_api/clients.py` (`GojiraClient.create_app(..., workload_class: str = "agent")` in the Protocol, `HttpxGojiraClient`, and `FakeGojira`; `GitHubClient.merge_pr(repo, pr_url) -> bool` in the Protocol, `GhCLI`, `FakeGitHub`), `api/kobo_api/main.py` (the two `steps=PIPELINE` call sites become `steps=pipeline_for(agent)`), `api/tests/fakes.py`
- Test: `api/tests/test_steps.py`, `api/tests/test_engine.py`, `api/tests/test_clients.py`, new `api/tests/test_connected_pipeline.py`

**Interfaces:**
- Consumes: `kinds.*` from Task 1.
- Produces: `CONNECTED_PIPELINE = [ConnectStep(), AvatarStep(), OnboardStep(), AccessStep(), ConnectedWireStep(), BuildStep(), DeployStep()]`; `pipeline_for(agent) -> list[Step]`.
- Step names: `ConnectStep.name = "connect"`, label `"Repository connected"`; `ConnectedWireStep.name = "wire"` (same name and label as `WireStep`: it is the same job done politely, and a shared name keeps the checklist vocabulary small).

Behaviour (this list is the test list):

- `ConnectStep`: refuses a repo outside `config.github_org`; refuses when `github.repo_exists` is false; otherwise returns a note. It creates nothing and never calls `create_repo_from_dir`. (The route has already set `agent.repo_full_name`.)
- `AvatarStep` for a connected agent: generates and stores `agent.avatar_png`, and does NOT call `set_avatar` (assert the fake recorded no clone and no push). It has no persona row: build the prompt from the agent's name and purpose tags only; if the existing prompt builder cannot work without a persona, skip with the step's existing fixed generic message.
- `OnboardStep`: passes `workload_class` = `"agent"` for `run_mode == "http"`, `config.connected_headless_class` for `"headless"`; composed agents pass `"agent"` exactly as today (assert the fake recorded it).
- `AccessStep` for a connected agent: service account, workload identity and model group exactly as today; NO memory bucket grant (assert `grant_bucket_prefix_write` not called).
- `ConnectedWireStep`: clones, writes `k8s/base/serviceaccount.yaml`, registers it in `k8s/base/kustomization.yaml`, sets `service_account`, the two model env vars, and appends each of `agent.secret_names` to `secrets_from` in `gojira.yaml` (reuse `_register_resources`, `_append_secrets_from`, and the service account template; factor shared code out of `WireStep` rather than copying it; NO pvc, NO data_mount, NO cabal secret, NO `deliver_pass`). Then `github.open_pr(...)` on branch `kobo/wire`, title `kobo: service account and model access`, then `github.merge_pr(...)`. Merged: `gojira.reprovision` + `_wait_active`, same as `WireStep`. Not merged: raise with exactly `Merge this pull request, then press Retry: <url>`. Idempotent on retry: an existing open PR from that branch is found with `find_pr` rather than reopened; if the default branch already carries the wiring (the PR was merged by a person), the step sees nothing to change and proceeds to reprovision + wait.
- `BuildStep` for a connected agent: a changed head between polls restarts the watch on the new head without counting toward `BUILD_MAX_HEAD_ADVANCES`; the overall timeout still bounds it. Composed behaviour is byte-for-byte unchanged.
- `_run_pipeline_body` READY hooks: `sync_dispatch_pairs_best_effort` and `refresh_colleague_rosters` are not called for a connected agent.
- `STEP_LABELS` contains every name from both lists; the existing label-vocabulary test still passes and a connected variant pins `["connect","avatar","onboard","access","wire","build","deploy"]`.
- `GhCLI.merge_pr`: `gh pr merge <url> --merge`; returns False (never raises) when GitHub refuses because of branch protection or required reviews, True when merged or already merged; any other failure raises `ClientError`. Read how `_gh_returned_404` and `destroy._is_already_archived` classify `gh` stderr and follow that pattern; test with `RecordingRunner`.

- [ ] Failing tests per bullet, then implement, smallest change first (clients, then steps, then selector).
- [ ] End-to-end test in `test_connected_pipeline.py` modelled on `test_create_agent_returns_201_pending_then_provisions_to_ready`: insert a connected `Agent` row directly (the route arrives in Task 4), launch `run_pipeline(..., steps=pipeline_for(agent))`, poll to `ready`, assert the step names, and assert `FakeGitHub` recorded no `create_repo_from_dir`, no `commit_push` to the default branch, no `rename_repo`, no `archive_repo`.
- [ ] Full suite, ruff, commit: `feat(api): the connected pipeline - connect, wire by pull request, follow the branch head`.

### Task 3: Destroy honours kind

**Files:** Modify `api/kobo_api/destroy.py` (`destroy-repo`, `destroy-directory`); Test `api/tests/test_destroy.py`, `api/tests/test_web_destroy.py`

- `destroy-repo` for a connected agent returns the note `the repository belongs to its team and is left as it is` and makes zero GitHub calls. This step does NOT self-neutralise today (`repo_full_name` is set for a connected agent), so this branch is the difference between a clean destroy and archiving a team's repo: test it first, and test that a composed agent still renames and archives.
- `destroy-directory` for a connected agent returns `never listed in the directory` with zero switchboard calls.
- Everything else (platform, access, slack, forget) runs unchanged; one end-to-end destroy test for a connected agent asserts the ledger reaches forget and the row is gone.
- [ ] Tests first, implement, full suite, commit: `feat(api): destroying a connected agent leaves its team's repository alone`.

### Task 4: Reading a repo without cloning, the scan, and the connect route

**Files:**
- Modify: `api/kobo_api/clients.py` (`GitHubClient` + `GhCLI` + `FakeGitHub`): `repo_access(repo) -> RepoAccessInfo` (dataclass: `visible: bool`, `can_write: bool`, `default_branch: str`), `read_file(repo, path) -> str | None` (None on 404; refuses files over 256 KB), `list_root(repo) -> list[str]`, `branch_protected(repo, branch) -> bool`. All via `gh api`; classify 404 vs 403 the way `_gh_returned_404` does; a private repo the App cannot see is `visible=False`, never an exception.
- Create: `api/kobo_api/connect_scan.py` with `scan_repo(github, gojira, session, config, *, repo, name, run_mode) -> ScanReport` (`blockers: list[Finding]`, `warnings: list[Finding]`, `adds: list[str]`; `Finding` = `code: str`, `message: str`).
- Modify: `api/kobo_api/main.py`: `POST /api/connect/scan` and `POST /api/agents/connect`.
- Modify: `api/kobo_api/mcp/tools/write.py` only to reword `kobo_create_agent`'s docstring sentence about the one runtime so it stays true (no new MCP tool in this plan).
- Test: `api/tests/test_clients.py`, new `api/tests/test_connect_scan.py`, new `api/tests/test_api_connect.py`

Blockers and warnings are exactly the spec's lists, with these codes: blockers `repo_not_in_org`, `repo_not_visible`, `repo_not_writable`, `no_dockerfile`, `already_deployed` (gojira has an app for the repo: add `GojiraClient.app_for_repo(repo) -> int | None` only if gojira exposes a lookup; if it does not, detect it from a `gojira.yaml` whose `app` differs from `name`, and let the onboard step's existing conflict handling cover the rest; say which you did), `name_taken`, `headless_unavailable`; warnings `base_not_pinned`, `base_outside_registry` (registry root derived from `config.runtime_image` the way `validate_runtime_image` derives it), `env_file_committed`, `branch_protected`, `looks_composed` (a `bundle/` directory), `no_port_exposed`.

Connect route: request `{repo, name, group_name, run_mode, secret_names: list[str] = []}`. Order: name validation (the create route's `validate_name`), the group gate (factor the create route's gate at `main.py:3040-3117` into one helper both routes call; do not copy it), run the scan server-side and refuse with 422 listing blocker codes if any, name collision last (same 409 and the same `IntegrityError` handling as create), then insert the row (`kind=connected`, `persona` a fixed constant such as `"connected"` since the column is NOT NULL, `model_id` the room default, `repo_full_name`, `run_mode`, `secret_names`, `created_by`, `group_name`, `run_heartbeat_at`), launch `run_pipeline(..., steps=pipeline_for(agent))`, answer 201 `{"name","status"}`. Secret names are validated as DNS-1123 subdomain names, at most 8. The MCP audit log is for MCP calls; for this route write one `log.warning` line naming the caller, the repo and the agent (WARNING because INFO does not reach pod logs here; see `org.py`'s `_disclose_overrides`).

- [ ] Client methods test-first with `RecordingRunner`; scan test-first with `FakeGitHub` knobs for each finding; route tests for every refusal and the happy path through to `ready`.
- [ ] Full suite, ruff, commit(s): `feat(api): read a repository without cloning it`, `feat(api): scan and connect a team's own repository`.

### Task 5: The UI

**Files:** Modify `ui/src/lib/tabs.ts` (+ `tabs.test.ts`), `ui/src/lib/api.ts`, `ui/src/lib/queries.ts`, `ui/src/components/Sidebar.svelte`, `ui/src/App.svelte`, `ui/src/pages/AgentDetail.svelte`, the agent card component on the dashboard; Create `ui/src/components/ConnectModal.svelte` (+ test mirroring `TeamGuideModal`'s test if one exists)

- `primaryTabs` / `advancedTabs` / `defaultTab` take the agent's `kind`. For `connected`: no `chat`, no advanced tabs at all; `access` as today; `slack` absent (the room did not make its chat app). The page therefore opens on the status and checklist view that already renders when there is no active tab. This is the first kind-driven tab model here (team guides do NOT hide tabs today); keep it in `tabs.ts`, pure and unit-tested, not inline in the page.
- A third sidebar action under "Quick-start team agent": `Connect your own agent`. `ConnectModal` follows `TeamGuideModal`'s shape: repository, name, group (reuse `groupsQuery`), run mode as two radio options with the second disabled and explained when the scan says `headless_unavailable`, optional secret names. `Scan` calls the scan route and renders blockers (each with its message) above warnings, then the "what will be added to your repository" list; `Connect` is disabled until a scan for the CURRENT field values has come back with no blockers (any edit invalidates the scan).
- Agent card and page header show a small `connected` marker and the repo as a link.
- Copy: plain words, no em dashes, no infra vocabulary beyond what the create flow already says.
- [ ] `npm run check`, `npm run build`; update or add vitest files by reading neighbours carefully (they cannot be run locally); commit: `feat(ui): connect your own agent`.

### Task 6: Convert to connected

**Files:** Modify `api/kobo_api/main.py` (`POST /api/agents/{name}/convert-to-connected`, operator-only via the existing `_operator_block`/`_is_admin` helpers, ready-or-failed agents only), `ui/src/pages/AgentDetail.svelte` (operator-only action with a confirm that says what stops working); Test `api/tests/test_api_connect.py`

- Flips `kind` to `connected`, sets `run_mode` from the request, retires the switchboard identity best-effort (the existing `retire_directory`), recomputes dispatch pairs. Redeploys nothing, touches no repo. Refuses an agent that is mid-provision or destroying.
- [ ] Tests first, implement, commit: `feat: convert a composed agent whose repo left the runtime into a connected one`.

---

## After the plan

- Headless goes live by configuration (`connected_headless_class`) once the deploy platform has the class (coordination thread: agora issue 17).
- With the deploy platform's owners, later: let its connect call carry service account, env and secret names so the two pull requests become one.
- First follow-up inside the room: a default-deny egress policy for connected namespaces.
