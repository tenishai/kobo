# Agent Destruction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An operator can fully destroy an agent through a resumable four-step ledger: platform teardown (namespace, per-app cloud identity, all platform rows), Slack app deletion, repo rename+archive, then the room forgets the row — nothing orphaned, every step tolerant of already-gone.

**Architecture:** Gojira gains real teardown (new gcp.py delete methods, orphan-row fix, token-gated `DELETE /api/v1/apps/{id}`); kobo gains a destroy ledger mirroring provisioning (same step table, reversed), an `operators` gate, three client capabilities (platform delete, Slack manifest delete, repo rename+archive), and a Danger-zone UI with a typed-name confirm and live checklist. Spec: `docs/superpowers/specs/2026-08-17-agent-destruction-design.md`.

**Tech Stack:** Python/FastAPI both repos; gcloud/gh/kubectl via the existing CommandRunner seams; Svelte 5 UI.

## Global Constraints

- Vendor rule: NO client names in tracked files; scan every touched file with `grep -i -f ~/Documents/GitHub/kobo/.superpowers/sdd/vendor-names.txt <files>` as its OWN gating step before commit, never batched with commit/push.
- NO EM DASHES in user-facing copy (UI strings, reply/notice text, API error bodies). Code comments exempt.
- Validate against the REAL downstream gate, never a lookalike (e.g. teardown role list must be THE constant `grant_iam_roles` uses, not a re-typed copy).
- Stage named files only. TDD RED-first with evidence. Report suite counts.
- gojira: `git fetch origin && git switch -c feat/app-teardown origin/main` (main carries the merged #34/#35; the local checkout may sit on an older branch). NO PR until the operator says; push the branch when done. Suite: `venv/bin/python -m pytest -q` from the repo root (mirror how existing runs invoke it; the sizing tests' 8 pre-existing local-env failures are known baseline noise, verify unchanged, do not fix).
- kobo API baseline 1168 (`cd api && venv/bin/python -m pytest -q`); UI baseline 242 (`PATH="~/Documents/google-cloud-sdk/bin:/opt/homebrew/bin:/usr/local/bin:$PATH" make -C ui check`).
- Discovery anchors (gojira): delete route `gojira/web/__init__.py:223-236`; `service.delete_app` `gojira/service.py:385-412`; `delete_namespace` `gojira/k8s.py:105-109`; orphan tables `gojira/models.py:171,195,235` (drift_findings, usage_profiles, sizing_recommendations); `gcp.py` has creation/check methods only.

---

### Task 1: gojira gcp.py teardown methods

**Files:**
- Modify: `gojira/gcp.py` (GCPClient ABC + GcloudClient)
- Test: `tests/test_gcp.py` (additive, mirror the existing argv-pinning idioms)

**Interfaces:**
- Consumes: existing `GcloudClient._run`/CommandRunner conventions, existing `service_account_exists` checker (reuse; add one only if absent).
- Produces (Task 2 relies on these exact signatures; ALL are check-then-act and return `bool` = "did anything", never raising on absent resources):
  - `remove_project_binding(self, project: str, member: str, role: str) -> bool` — `gcloud projects remove-iam-policy-binding {project} --member={member} --role={role} --quiet`; a "not found / no matching policy" error is caught and returns False (read the actual gcloud error text conventions from the existing add path's tests; match on a stable substring).
  - `delete_service_account(self, project: str, email: str) -> bool` — checks existence first (existing checker), then `gcloud iam service-accounts delete {email} --project={project} --quiet`; absent → False.
  - `delete_app_images(self, ar_repo: str, name: str) -> int` — lists digests via `gcloud artifacts docker images list {ar_repo}/{name} --format=value(DIGEST)` (empty/`NOT_FOUND` list → 0), then per digest `gcloud artifacts docker images delete {ar_repo}/{name}@{digest} --delete-tags --quiet`; returns count deleted.
- WIF repo-member unbinding needs NO method: the binding lives on the deploy GSA's own IAM policy, so `delete_service_account` destroys it. Document this in the ABC docstring.

- [ ] **Step 1: Failing tests** pinning each method's argv sequences via the recorded-runner idiom, including: absent SA → no delete call, False; empty image list → no delete calls, 0; remove-binding "not found" stderr → False without raising; happy paths return True/count with exact argv.
- [ ] **Step 2: RED → implement → GREEN** (`venv/bin/python -m pytest tests/test_gcp.py -q`, then full suite; sizing-noise baseline unchanged).
- [ ] **Step 3: Vendor scan (own step) → commit** `git add gojira/gcp.py tests/test_gcp.py && git commit -m "feat(gcp): teardown verbs, check first, never rage at the already-gone"`.

---

### Task 2: gojira delete service extension + API route

**Files:**
- Modify: `gojira/service.py:385-412` (delete_app), `gojira/web/__init__.py` (new API route after the provision route), `gojira/models.py` ONLY if an import is needed (no schema changes)
- Test: `tests/test_service.py` (extend the two existing delete tests), `tests/test_web.py` (API route battery)

**Interfaces:**
- Consumes: Task 1's three methods exactly; the SAME role-list constant `grant_iam_roles` uses (find it near the provisioning steps; import/reference it, never re-type).
- Produces:
  - `service.delete_app(session, app_id, kubectl_for, gcp, delete_namespaces=True) -> dict | None` — None for missing app (unchanged semantics); otherwise a summary dict `{"app": name, "namespaces_deleted": [...], "namespace_failures": [{"namespace", "reason"}], "identity": {"bindings_removed": int, "gsa_deleted": bool, "images_deleted": int, "failures": [str]}, "rows_deleted": true}`. Teardown order inside: namespaces (existing loop) → per-role `remove_project_binding` for the app's deploy GSA member → `delete_service_account` → `delete_app_images` → DB deletes now covering ALL SEVEN tables (existing ProvisionStep+Event PLUS `DriftFinding`, `UsageProfileRow`, `SizingRecommendation` — fix the orphan bug) → app row. Every GCP failure appends to `identity.failures` and continues (best-effort, spec-mandated).
  - Route `DELETE /api/v1/apps/{app_id}` under `auth.api_token_error` (the exact gate the other /api/v1 routes call first), JSON body optional `{"delete_namespaces": bool}` default true → 404 `{"error": "not found"}` for missing app; 202 + the summary dict otherwise. The UI form route now calls the same extended service function (passing the gcp client; check how `request.app.state.clients` exposes it).
- The deploy GSA email derivation: read how provisioning names it (the ensure_service_account step) and derive identically (same helper/constant, real-gate rule).

- [ ] **Step 1: Failing tests**: service level — seven-table cleanup (seed rows in all seven, assert all empty after), summary shape, GCP-failure-continues (raising fake gcp → failures recorded, rows still deleted), missing-app None; route level — bearer matrix (no token 401 / wrong 401 / correct 202), 404, body flag honored, repeat delete → 404 (idempotent from the caller's view).
- [ ] **Step 2: RED → implement → GREEN full suite** (baseline + new; sizing noise unchanged).
- [ ] **Step 3: Vendor scan (own step) → commit** `git commit -m "feat(api): apps can die whole, seven tables, one identity, zero orphans"` → push branch `feat/app-teardown`.

---

### Task 3: kobo clients — platform delete, Slack delete, repo rename+archive

**Files:**
- Modify: `api/kobo_api/clients.py` (GojiraClient protocol + HttpxGojiraClient; SlackClient + real impl; GitHubClient protocol + GhCLI), `api/tests/fakes.py`
- Test: `api/tests/test_clients.py`

**Interfaces (Task 4 relies on these exactly):**
- `GojiraClient.delete_app(self, app_id: int, *, delete_namespaces: bool = True) -> dict` — `DELETE {base}/api/v1/apps/{app_id}` with bearer via `_headers()`, JSON body `{"delete_namespaces": ...}`; 202 → response dict; 404 → `{"already_gone": True}`; anything else → ClientError with the standard message shape.
- `SlackClient.delete_app(self, app_id: str) -> bool` — `apps.manifest.delete` with the config token (same rotate-on-401 discipline as create; read the existing create/rotate flow and reuse its helper); Slack `ok: false` with `app_not_found`-class errors → False (already gone); other failures raise per existing conventions. Returns True when deleted.
- `GhCLI.rename_repo(self, repo: str, new_name: str) -> None` — `gh api -X PATCH repos/{repo} -f name={new_name}` (env token overlay as everywhere); `GhCLI.archive_repo(self, repo: str) -> None` — `gh api -X PATCH repos/{repo} -F archived=true`; both raise ClientError on failure; a 404 raises a ClientError whose message starts with `"repo not found: "` (Task 4 branches on it).
- Fakes: FakeGojira gains delete_app with knobs (summary/already-gone/raise); FakeSlack gains delete_app; FakeGitHub gains rename_repo/archive_repo with recorded calls + raisable knobs + a `renamed`/`archived` state the destroy tests can assert.

- [ ] **Step 1: RED tests**: argv/URL/method/body pins for all four (MockTransport for gojira/slack HTTP, recorded-runner for gh), 404→already_gone mapping, slack app_not_found→False, rotate-on-401 exercised for slack delete, repo-not-found prefix pinned.
- [ ] **Step 2: Implement → GREEN full API suite.**
- [ ] **Step 3: Vendor scan (own step) → commit+push** `kobo: clients learn to end things, politely`.

---

### Task 4: kobo destroy ledger + route + operators gate

**Files:**
- Create: `api/kobo_api/destroy.py`
- Modify: `api/kobo_api/config.py` (+`operators: list[str] = []`), `api/kobo_api/org.py` (ORG_KEYS + bootstrap ripple — expect the `_ORG_YAML_TOKENS` drift assertion, update template+golden exactly as the slack_operators task did), `api/kobo_api/main.py` (route + gate helper + detail fields), `api/config.example.yaml`
- Test: `api/tests/test_destroy.py`, `api/tests/test_web_destroy.py`, org round-trip additions

**Interfaces:**
- `_operator_block(request, config) -> JSONResponse | None` in main.py — same semantics family as `_slack_operator_block` (empty open, IAP-off open, case-insensitive match, else 403) with copy: `{"error": "Destroying agents is operator-only on this room"}`.
- destroy.py produces `DESTROY_STEPS = ("destroy-platform", "destroy-slack", "destroy-repo", "destroy-forget")` and `run_destroy(session_factory, agent_id, clients, config)` executed on a background thread by the route (mirror how provisioning launches; reuse its thread conventions). Steps write rows into the SAME agent-step table provisioning uses (named per DESTROY_STEPS), so the UI checklist machinery reads them for free. Semantics per step:
  - destroy-platform: no `gojira_app_id` → done("no platform app"); client 202 summary → done (summary JSON-dumped into the step detail); `already_gone` → done("already gone").
  - destroy-slack: no `slack_app_id` → done; True → done; False → done("already gone").
  - destroy-repo: target name `archived-agent-{agent.name}-{agent.id}`; if current repo 404s AND the archived name exists → done("already archived"); else rename (repo-not-found + archived-name-exists → proceed as done), then archive the renamed repo; record the final archived name in the step detail.
  - destroy-forget: delete the agent's step rows and the agent row; terminal.
  - Resume: `run_destroy` skips steps whose row is already `done`; a failed step's row resets to running on re-invoke. Agent.status = "destroying" while running; a step failure leaves status "destroy_failed" (new literal; UI copy plain).
- Route `DELETE /api/agents/{name}`: operator gate FIRST (before existence lookup, info-safe like the slack gate), 404 unknown agent, 409 `{"error": "the agent is still being created, retry or wait"}` when status is a provisioning-in-flight state, 202 `{"status": "destroying"}` and thread launch otherwise; idempotent re-DELETE while destroying → 202 (re-invoke resumes). Agent detail JSON gains `"destroy_visible"` (gate check) — and existing detail already serializes step rows, verify destroy-steps ride along.

- [ ] **Step 1: RED tests**: full already-gone matrix per step (fakes' knobs), resume-from-first-incomplete (fail slack once, re-run, platform skipped, slack retried), forget only after 1-3 done, gate matrix (mirror the slack-gate test file's real-ES256 idiom), route codes (403/404/409/202/202-resume), org round-trip for `operators`.
- [ ] **Step 2: Implement → GREEN full API suite.**
- [ ] **Step 3: Vendor scan (own step) → commit+push** `kobo: the destroy ledger, four steps down, nothing left behind`.

---

### Task 5: kobo Danger-zone UI

**Files:**
- Create: `ui/src/components/DangerZone.svelte`
- Modify: `ui/src/lib/api.ts` (destroyAgent call + `destroy_visible` on AgentDetail), `ui/src/lib/queries.ts` (destroyAgentMutation invalidating `["agents"]`), `ui/src/pages/AgentDetail.svelte` (slot last, gate on destroy_visible; render panels read-only with a one-line notice while status is destroying/destroy_failed)
- Test: `ui/src/lib/queries.test.ts`

**Interfaces:** consumes Task 4's route + detail fields. Copy VERBATIM (no em dashes): confirm line `This destroys the running agent, its platform identity, and its Slack presence. The repo is archived, not deleted. Memories die with the namespace.`; armed button `Destroy this agent`; failed-state button `Destroy again`; read-only notice `This agent is being destroyed.`

- [ ] **Step 1: RED factory tests** (URL/method pin, invalidation, 403/409 propagation) → implement: typed-name input arms the button (exact match), destroying state renders the existing checklist idiom from the agent's destroy-steps, failed step shows its detail + Destroy again, polling via the existing agent-detail query cadence (check how provisioning progress polls and reuse).
- [ ] **Step 2: Gate green (svelte-check 0, vitest 242+new) → vendor scan (own step) → commit+push** `kobo: a danger zone that reads you the whole sentence first`.

---

### Task 6: rollout + live acceptance (controller-led)

- [ ] Gojira: after review, deploy handoff per the established pattern (their shared instance; comment on the PR when the operator says open it) OR operator green-light to roll the image directly, matching how #34/#35 shipped. Until the platform runs the new route, kobo's destroy-platform step will fail plainly (ClientError surfaces in the step detail) — acceptable staging, but the acceptance run waits for the platform roll.
- [ ] Kobo: build + roll at the Task 5 commit.
- [ ] Acceptance A (full kill): create a scratch agent end to end through the four-station flow, wire nothing extra, destroy it from the Danger zone. Verify empty by direct inspection: namespace gone (kubectl), deploy GSA gone + no lingering project bindings for its member (gcloud), zero images under the registry path (gcloud), both DBs clean (all seven gojira tables + kobo rows), repo archived under `archived-agent-<name>-<id>` (API read).
- [ ] Acceptance B (resume): second scratch agent; start destroy; restart the kobo pod mid-run (rollout restart) to induce a partial ledger; Destroy again; verify completion identical to A.
- [ ] Ledger + runbook results entry; suites reported.
