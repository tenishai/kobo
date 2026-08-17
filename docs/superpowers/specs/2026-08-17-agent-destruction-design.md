# Agent destruction — ordered teardown, nothing orphaned (design)

Approved in brainstorm 2026-08-17. Operator decisions: destruction is
operator-only (new general `operators` config key); the agent's repo is
archived, not deleted. Discovery (gojira source, 2026-08-17) found the
platform's own delete is UI-only, leaves per-app cloud identity in place,
and orphans three tables' rows; this design fixes all of it.

## The two sides

### Platform side (gojira, one small PR in the #35 pattern)

`DELETE /api/v1/apps/{app_id}` under the existing bearer gate, accepting
`{"delete_namespaces": bool}` (default true), returning 202 with a
teardown summary. It extends today's `service.delete_app`:

1. **Orphan fix**: delete `drift_findings`, `usage_profiles`, and
   `sizing_recommendations` rows for the app alongside the existing
   `provision_steps`/`events` deletes. (Today they dangle against a
   deleted app id.)
2. **Cloud identity teardown** (new, best-effort, each failure logged AND
   recorded in the response summary, never aborting the rest): the
   per-app pieces the docstring's "shared identity" rationale does not
   actually cover:
   - remove the app's project IAM role bindings for its deploy GSA,
   - delete the deploy GSA (`<name>-deploy@<project>`),
   - remove the app repo's WIF principalSet member binding,
   - delete the app's images under the shared registry path
     (`<ar_repo>/<name>` tags and digests).
   `gcp.py` gains the matching delete/unbind methods (it has none today);
   the shared pool/provider/AR repo and cluster resources stay untouched.
3. Namespace deletion stays best-effort per environment (`--wait=false`,
   `--ignore-not-found`), as today.
4. Idempotent: unknown app id returns 404 (kobo tolerates it as
   already-gone); a repeat delete after partial cloud failure re-runs
   only what still exists (every teardown method is check-then-delete).

The UI form route stays for humans; both call the same service function.

### Room side (kobo)

`DELETE /api/agents/{name}`, gated by the new `operators` config key
(list of emails; same semantics family as `slack_operators`: empty list
open for dev parity, IAP-off open, otherwise verified-email match or 403
with plain copy). ORG_KEYS gains `operators`.

Destruction is a **step ledger, not a transaction** — the mirror of
provisioning. The agent row gains status `destroying` and destroy steps
recorded like provisioning steps:

| # | step | what it does | already-gone tolerance |
|---|---|---|---|
| 1 | platform | `DELETE /api/v1/apps/{gojira_app_id}` | gojira 404 = done; row has no app id = done |
| 2 | slack | `apps.manifest.delete` via the config-token client when `slack_app_id` set | Slack "app not found" = done; no id = done |
| 3 | repo | rename to `archived-agent-<name>-<row_id>`, then archive (both via the room's GitHub App) | already renamed = proceed to archive; already archived = done; a bare not-found is never proof (the hosting API renders lost access identically), so the step corroborates via the archived name, and when both names are missing it fails with plain copy pointing at repo access (amended 2026-08-17, ratified) |
| 4 | forget | delete the agent row + its step rows (kobo DB) | terminal |

Order rationale: the platform step kills the running workload first (the
agent visibly stops existing); Slack and repo are paper; the row goes
last so a failed run always leaves a resumable record. Any step failure
marks the run failed at that step with a plain reason; **Destroy again**
resumes from the first incomplete step. The row never disappears until
steps 1-3 are each done or proven already-gone.

Renaming before archiving frees the `agent-<name>` name for future
agents; the row id suffix makes archive names collision-proof forever
without clocks.

## UI

A **Danger zone** card at the bottom of the agent page's Overview tab
(amended 2026-08-17 with the app-shell tab ruling), rendered only for
operators (route enforcement is the boundary; hiding is UX, same
discipline as the Slack gate):

- Type the agent's exact name to arm the button (the familiar pattern).
- Destroying renders the four-step checklist live (the provisioning
  checklist idiom, reversed), polling the destroy status.
- A failed step shows its reason and a **Destroy again** button.
- Copy is plain, no em dashes. The confirm line: "This destroys the
  running agent, its platform identity, and its Slack presence. The
  repo is archived, not deleted. Memories die with the namespace."

While `destroying`, the rest of the page's panels render read-only
(chat/uploads disabled with a one-line notice). (The first shipped
implementation unmounted panels behind the notice; the app-shell arc,
2026-08-17, restores this requirement as written.)

## What dies and what survives (the honest inventory)

Dies: namespace (pods, PVC, techne memories, secrets), deploy GSA and
its bindings, WIF repo binding, registry images, Slack app, gojira rows
(all seven tables), kobo rows.
Survives: the archived repo (bundle, persona, knowledge history, tools —
read-only), catalog content (org-owned, never agent-scoped), the shared
platform identity (pool, provider, AR repo, cluster).

## Testing

- gojira PR: teardown-method unit tests (check-then-delete argv pins,
  absent-resource no-ops), route auth matrix (bearer/401/404), orphan-fix
  regression (all seven tables empty after delete), partial-failure
  summary shape.
- kobo: step-ledger state machine (each step's already-gone matrix,
  resume-from-first-incomplete, row survives failed runs), operator gate
  matrix, route shapes, UI factories for destroy status polling.
- Live acceptance: create a scratch agent end to end, destroy it, verify
  the full "dies" inventory empty by direct inspection (kubectl, gcloud,
  registry, Slack, both DBs) and the repo archived under its new name;
  then induce a mid-run failure on a second scratch agent and prove
  Destroy again completes it.

## Non-goals

- Bulk destruction; org offboarding.
- Un-archiving / resurrection flows (the archived repo makes them
  possible later; nothing here builds them).
- Destroying agents the room did not create (no gojira app id and no
  room row means nothing to destroy).
- PVC snapshot/export before namespace deletion.
