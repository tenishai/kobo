# P0 E2E Runbook — Results (2026-08-01)

**Outcome: PASSED.** One API call to a laptop-run gojira (feature branch) took a
hand-written agent bundle repo to a running, memory-seeded agent on the dev GKE
cluster, which answered first contact from its seeded knowledge over a Vertex
model call — with no PR merged into gojira or daedalus, and no manual step
between the API call and the reply (beyond fixes noted below, re-run via the
retry endpoint).

Config values (cluster, projects, AR paths, model-access group) are deploy
configuration and live outside this repo.

## Verified sequence

1. `daedalus-runtime` base image built from the daedalus feature branch
   (`--platform linux/amd64`), pushed to Artifact Registry.
2. Exemplar repo (`agent-hello`) authored: `bundle/` (agent.yaml with
   `transports: [{kind: a2a, host: 0.0.0.0, port: 8080}]`, card, persona,
   `knowledge/gotchas.md`) + 2-line Dockerfile `FROM` the runtime image.
3. Per-agent GSA created; KSA↔GSA Workload Identity binding; GSA added to the
   model-access Google Group (the manual walk-through of P1's `access` step).
4. Branch gojira launched locally; cluster registered; **one call**:
   `POST /api/v1/apps {..., "auto_merge_setup_pr": true}` → `201 {"id":1,"status":"pending"}`.
5. Provisioner ledger: pre-existing project resources correctly **skipped**
   (WIF pool/provider, AR repo, DNS access — idempotency proven against a
   project that already hosts another gojira); namespace created; artifacts
   generated; setup PR #1 opened and **auto-merged**; app `active`.
6. Generated CI (keyless WIF) built the agent image and `kubectl apply`d;
   pod Ready via the a2a `/healthz` readiness probe; Service generated
   (the new agent-class opinion working).
7. Pod log: `seeder: seeded=1 removed=0 unchanged=0`.
8. Contract change (`service_account` + model env in gojira.yaml) →
   re-provision → regenerated artifacts → setup PR #2 auto-merged — live
   validation of the PR-specific merge guard (PR #1's merged history did not
   short-circuit PR #2).
9. First contact over `POST /a2a/tasks` (bare Task envelope: `id` +
   `history[]`): agent replied citing the seeded deployment-chain knowledge.

## Defects found live (all fixed on branches, none previously caught by tests)

| Found at | Defect | Fix |
|---|---|---|
| merge step | `gh pr view --json merged` — no such field; use `state == "MERGED"` | gojira branch `b4c4401` |
| first contact | runtime image lacked the vertexai SDK (`google-cloud-aiplatform`) that litellm's `vertex_ai/*` routes import at call time | daedalus branch `33a1567` |

## Operational findings (for PR bodies / P1 backlog)

- **Cluster `ar_repo` must be the full AR path** (`<loc>-docker.pkg.dev/<proj>/<repo>`), not the short name — misregistration produced a confusing empty-arg gcloud failure. Cortex's provisioner should validate/derive this.
- **Gojira's `EnsureWifProvider` check is existence-only**: it inherited a pre-existing provider whose `attributeCondition` pinned a different GitHub owner, failing CI auth downstream. Broadened additively in the dev project (revert command in the session ledger). Gojira should verify/manage the owner condition; multi-org projects need a per-owner condition list.
- **App-authored k8s resources must be registered in `kustomization.yaml`** — the generator preserves *existing* kustomization entries; a new app-authored file alone never gets applied (pod stuck: unknown ServiceAccount). Cortex must write both the manifest and the kustomization entry.
- Rollout gap during re-provision with `Recreate` + a not-yet-satisfiable pod spec left replicas at 0 until the fix landed — acceptable in dev; worth a cortex status surface.
- a2a bare-task envelope: `id` + `history[{role, parts[{kind:"text",text}]}]`; replies arrive in `status.message`. (Cortex's `hello` step + chat pane contract.)
- kubectl to the cluster requires the DNS endpoint flag when re-fetching credentials; a stale kubeconfig fell back to an unreachable private endpoint mid-session.

## PR-body obligations when the branches eventually merge

- gojira: `ALTER TABLE apps ADD COLUMN auto_merge_setup_pr BOOLEAN DEFAULT 0`
  for existing DBs; document `auth.api_token` (fail-closed on the onboarding
  API when IAP auth is enabled and no token configured).
- daedalus: none beyond the branch content (P1 TODO markers in-code: SIGKILL
  seed-convergence before PVC-backed memory; fsGroup when PVCs land).

## P1a results (2026-08-02)

**PASSED.** `POST /api/agents {"name":"clio","persona":"helpful-generalist"}` against
cortex-api produced a READY agent (7/7 steps) whose first reply cites its seeded
assembly-chain knowledge over a Vertex model call. PVC persistence verified: the
agent's memory volume survived pod deletion and the boot seeder converged
(`seeded=0 removed=0 unchanged=1`) instead of re-seeding.

One live incident, matching the final review's watchlist exactly: two CI runs
(the wire commit and the regeneration PR merge) applied within ~1s of each other
and the stale tree's `kubectl apply` landed last, leaving the live Deployment
without the model env — first contact failed 403 against the wrong project.
Remedy: re-ran the correct revision's CI, then one `POST /api/agents/clio/retry`
re-executed only the failed First-contact step (the ledger's resume semantics
working live). **P1b backlog (high):** serialize the generated CI per repo
(concurrency group) or have DeployStep verify the live spec matches the wired
contract before passing.

Still unverified live: the group-add already-a-member stderr wording (the retry
resumed after the access step; exercise a full re-run someday).

## P1b results (2026-08-02)

**PASSED.** The assembly room UI is live: the Playwright smoke covered the
dashboard, clio's detail page (7-step checklist + First words), the create
modal's validation states (taken / invalid / available), and the unknown-agent
error path. The exit criterion was met for real: an agent (`muse`) was created
entirely from the browser — one modal, four fields, Create — and walked the
checklist to READY, answering first contact from its seeded memory.

One live catch, now twice-observed (clio run #1, muse run #1): a brand-new
repo's first CI run can fail WIF auth with `PERMISSION_DENIED` on
`iam.serviceAccounts.getAccessToken` because the per-repo IAM binding hasn't
propagated yet. Remedy both times: re-run the CI once, then the product's
Retry resumes from the failed step. **Backlog (high):** tolerate first-build
propagation — BuildStep should be able to dispatch a fresh run (not just
re-poll), and/or the generated CI should retry its auth step.

## P1c results (2026-08-03)

**PASSED — and the milestone earned its keep by failing first.** The hardened
stack's first `echo` create hit a NEW compound failure: GitHub's concurrency
queue cancelled the newest pending run (the regeneration tree carrying the
model env), and BuildStep's entry-time head read raced the platform's merge —
so a stale tree deployed. The new DeployStep verification caught it loudly at
the right step with the race-remediation hint (contrast: muse failed the same
class silently at first contact a phase earlier).

Three fixes landed from the incident (BuildStep heals ANY completed
non-success conclusion including cancelled; re-resolves head every poll and
follows advances, capped at 2; post-rerun stale-read grace), each with the
live sequence encoded as a regression test that fails against pre-fix code.

The proof: echo's build ledger row was reset and the product's own Retry
re-run — BuildStep found the cancelled run at head, dispatched the rerun
itself (run attempt 2: success), the correct tree applied, DeployStep's
verification passed, and echo answered first contact. Zero manual CI/kubectl
commands in the recovery.

Carried to P2 (from the final review's register): post-incident items all
closed; remaining — pre-existing agent repos lack the concurrency block until
their workflows regenerate; sweep assumes a single API process; platform ruff
baseline burn-down before its PR; duplicate-skill-id validation; the ledgered
UX seams.
