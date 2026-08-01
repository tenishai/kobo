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
