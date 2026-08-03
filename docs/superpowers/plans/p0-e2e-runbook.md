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

## P2a results — chat pane live (2026-08-03)

Commits under test: `753c5b0` (converse) → `1fa0804` (chat endpoint + model labels) →
`2bfa064` (UI client) → `8d1f2da` (ChatPanel). Stack: cortex-api restarted at `8d1f2da`
(startup sweep found nothing — all rows terminal), UI dev server picked the new
component up via HMR, agents clio/muse/echo READY in the live cluster.

**Wire smoke (curl, before the browser run):**
`POST /api/agents/echo/chat` with one user message ("Reply with exactly the word: ping")
→ `{"reply": "ping"}` through the real port-forward → a2a → Vertex chain.

**Browser milestone (Playwright, headless chromium): PASSED — all assertions.**

echo, turn 1 — "what do you remember about how you got here?":

> I remember being assembled in the cortex assembly room and then deployed through a
> specific sequence called the platform chain. The steps were: 1. **repo** (presumably
> version control/source) 2. **onboarding** (initialization/setup) 3. **build**
> (compilation/construction) 4. **deploy** (putting me into production) 5. **first
> contact** (meeting you now!) That's how I got here to talk with you today.

echo, turn 2 — "say that again in five words":

> Assembled, built, deployed, met you.

Judgment call recorded: turn 2 is a five-word restatement of turn 1's content with no
re-prompting — the full-history envelope demonstrably carried (the server holds no
session state; the browser owned the transcript).

clio, one exchange — "in one sentence, what is your purpose?":

> I'm here to be a helpful, concise, and warm assistant who can answer your questions
> and support you with information, while being honest when I don't know something.

Also verified live: `model_label` ("Claude Sonnet 4.5") on the list API, the detail
header (raw id on hover), and the composition card; ChatPanel renders only on READY
agents; optimistic user bubbles + pending pulse behaved as specced. Screenshots in
`.superpowers/sdd/smoke/p2a-*.png` (gitignored).

**409 path:** no non-READY agent existed at run time (clio/muse/echo all READY), and the
panel is hidden by construction on non-ready agents — skipped live per the plan; the
endpoint's 409 is unit-covered.

**Notes:** one milestone-script fix (SPA hash navigation doesn't reload, so the clio
assertion needed an explicit wait — script-side, not a product defect). Cosmetic seam
for later: agent replies render as plain text, so model markdown (`**repo**`) shows its
asterisks in bubbles.

## P2b results — knowledge editing live (2026-08-03)

Commits under test: `0fcda43` (knowledge module) → `e1fb70f` (endpoints) → `1245fdd`
(UI lib + lineDiff) → `da28669` (MemoryPanel). Stack: cortex-api restarted at HEAD,
agents unchanged. Subject: clio (repo had only `gotchas.md`).

**Browser milestone (Playwright, headless chromium): PASSED — all assertions, two full
edit→deliver→converge→answer loops.**

Loop 1 — ADD `word-of-the-day.md` ("The word of the day is periwinkle…") from the
Memory panel, via the review-changes diff preview (2 added lines):

- Delivery chip: "Delivering changes…" → "Changes are in place"
- Pod rotated `clio-665c46569f-fgj4f` → `clio-75c4c577cf-j2g6q`
- Seeder (verbatim): `seeder: seeded=1 removed=0 unchanged=1` — the new file seeded,
  gotchas untouched: **convergence**
- Chat: "what is the word of the day?" → *"The word of the day is **periwinkle**! 🌸"*

Loop 2 — EDIT the same file (periwinkle → marmalade), diff preview showed exactly
1 removed + 1 added line:

- Chip delivered; pod rotated to `clio-5f89f94986-z8xgf`
- Seeder (verbatim): `seeder: seeded=1 removed=0 unchanged=1` — only the edited file's
  entries replaced, gotchas' seed rows never touched across both deliveries
- Chat (fresh transcript): → *"The word of the day is **marmalade**! 🍊"*

The delivery status was computed statelessly throughout (CI run for the pushed sha +
live image tag == sha[:12]); no new DB tables anywhere in P2b. Screenshots in
`.superpowers/sdd/smoke/p2b-*.png` (gitignored).

**Environment notes from the run (script/env, not product):** (a) the local kubeconfig
had been re-pointed at the cluster's private endpoint and kubectl hung — re-running
get-credentials with the DNS endpoint fixed it (the generated CI already uses the DNS
endpoint for the same reason); (b) the API process needs `gh`, `kubectl`, AND the GKE
auth plugin's SDK dir on PATH — a missing binary surfaces as a raw 500 through the
knowledge routes (CommandRunner FileNotFoundError is not wrapped as ClientError);
flagged to the final review.

**P2b post-review addenda:** (1) Final-review fixes landed (`3cc472d`, re-review
approved): missing binaries now surface as the house 502 instead of a raw 500, the
failed-delivery message points at the working remedy (change something and save), and
the knowledge routes return fixed plain-language 502 bodies with the real error logged
server-side. (2) The owed live delete validation PASSED: removing `word-of-the-day.md`
from clio delivered and the new pod's seeder logged `seeded=0 removed=1 unchanged=1` —
removal convergence proven; clio is back to `gotchas.md` only. (3) One real gap found by
the review and confirmed in the runtime kit's source: deleting the LAST knowledge file
strands its seeded rows (git drops the empty dir; the seeder early-returns when
`knowledge/` is absent). Fix is specced into P2c Task 1, which touches that branch and
rebuilds the image anyway.

## P2c results — slack connect live (2026-08-03)

Commits under test: `52ecfb2` (runtime kit: slack extra + seeder last-file fix) →
`938262c`/`530c514` (SlackClient + rotation) → `eb5cd19`/`061231f` (endpoints + wiring)
→ `534016e`/`bf8a272` (UI lib) → `4458e3b`/`ae9fc0f` (SlackPanel) → `3c89ff6` (private
channels) → `819482d` (connect delivers the current runtime).

**The proof (screenshot from the sandbox workspace):** a human mentioned
`@echo what do you remember about how you got here?` in the private channel
`echo-test` — echo answered IN THREAD from seeded memory ("I remember being assembled
in the cortex assembly room, then deployed through a specific platform chain: repo →
onboarding → build → deploy → first contact…"), with the reaction lifecycle completing
(✅ on the parent) and markdown rendering as Slack bold. Immediately after, the browser
chat path answered `"both doors open"` — both transports live on one pod.

**The flow as shipped:** one click created the Slack app (A0BMGKNB14M) from the rendered
manifest; the finish-setup card walked the three no-API steps (install, app-level token,
channel ID); Finish setup delivered the tokens as a namespace Secret, wired the
transport + secrets into the repo, and the P2b delivery machinery tracked the rebuild.

**Two live incidents, both converted to product fixes:**
1. The sandbox channel was PRIVATE — and modern private channels use C-prefixed IDs, so
   the public-only assumption was wrong. Fixed live via Slack's manifest-update API and
   permanently in `3c89ff6` (manifests now carry both channel types' grants; channel
   copy updated). Note: the mention flow works in private channels on the base scopes
   (`app_mention` is channel-type-agnostic); reading plain messages needs the updated
   grants, which existing installs pick up on reinstall.
2. **The connect crashlooped echo** (predicted minutes before impact): the agent repo's
   Dockerfile pins its runtime base at compose time, so the rebuild shipped the old
   slack-less base and the runtime's fail-closed transport check crashed the pod —
   taking a2a chat down with it. Hot-fixed by an operator commit bumping echo's base
   (`b976303`, delivered and verified), and fixed permanently in `819482d`: every
   connect now rewrites the Dockerfile to the current runtime, with tests. Verbatim
   crash: `BundleError: transport 'slack' requires the slack extra`.

Suites at close: API 403, UI 142, runtime kit 16. Seeder on the new base converged
(`seeded=0 removed=0 unchanged=1`). Setup-card screenshots in
`.superpowers/sdd/smoke/p2c-slack-state*.png`; the thread screenshot is Kyle's.

## P2d results — avatars live (2026-08-03)

Commits under test: `6d64dea`/`abab2e5` (ImageClient) → `3a7781a` (avatar module +
routes + display cache) → `edfcde4` (UI faces). Endpoint per discovery:
`gemini-2.5-flash-image` via `:generateContent` (us-central1) — worked first try, no
enablement needed (the Imagen family is entitlement-blocked here and being retired;
Google's own migration path points at this model).

**Browser milestone (Playwright): PASSED — all assertions.**

- "Give clio a face" → real generation → 802,564-byte PNG served from the display
  cache, header img cache-busted with the delivery sha
  (`?v=1629a92…`); dashboard card shows the face.
- "New face" → "Painting…" pending state observed → NEW sha (`?v=ba7bb6d…`), different
  bytes (760,435) — overwrite + cache-bust proven.
- Repo record: `bundle/avatar.png` in the agent repo is 760,435 bytes — byte-identical
  to the served cache. The repo stays the record; the DB column is a display copy.
- Chat sanity after both rebuild-deliveries queued: *"still here"*.

Screenshots in `.superpowers/sdd/smoke/p2d-*.png`. The avatar prompt is the
deterministic template in `avatars.py` (name + persona description, flat minimal
square portrait, no text in image).
