# Agent Content Lifecycle — skills editing, memory intake, Slack operator gate (design)

Approved in brainstorm 2026-08-13. Sequencing decision: this ships before the
tools/enforcement spec (which gets its own design next — see Non-goals).

## What this makes true

An operator can reshape a live agent without reassembling it: change which
catalog skills it carries, write agent-local guardrail skills that belong to
that agent alone, and feed it documents that become durable memories — then
throw the documents away. The Slack panel stops advertising an
operator-assisted flow to people who can't complete it.

All of it rides machinery this codebase has already proven live: the
knowledge-editing delivery loop (commit → delivery chip → pod rotation), the
catalog search/pin path from assembly, the seeder's boot-time convergence, and
the verified IAP email on every request.

## Skills: two provenance classes

`Agent.skills` (JSON column) today records catalog picks as `{id, team}`.
Each entry gains a `source` marker; absence means `catalog` (migration-free
read path for existing rows).

- **`catalog`** — today's kind. Post-assembly you can **add** (re-search the
  org catalog, pin the object's *current* source-sha, materialize into
  `bundle/skills/<id>/` exactly as `_stage_skills` does at assembly) and
  **remove** (drop the bundle directory + the JSON entry). Content is
  **read-only per-agent**: view and remove, never edit — the marketplace repo
  owns it. No unpin/fork path in this phase.
- **`local`** — new. Agent-only skill files at `bundle/skills/<slug>/SKILL.md`
  with `source: local` provenance in the JSON entry. Create, edit, delete
  through the room. Validation family mirrors knowledge plus the skill
  contract: 64KB cap, filename slug rules (reuse the reserved-name and
  NAME_RE conventions), and frontmatter must parse with at least `name` and
  `description` (what the runtime's skill loader needs). Local and catalog
  ids share one namespace per agent — collisions rejected (409), whichever
  class landed first wins.

Every mutation goes through the knowledge-editing delivery loop: commit to
the agent repo (named-files-only staging, as always), delivery status
tracked by sha, pod rotation loads the new bundle. Identical-content saves
no-op the same way knowledge saves do.

### API

- `GET /api/agents/{name}/skills` — unified list:
  `[{id, team?, source, sha?, editable}]`.
- `POST /api/agents/{name}/skills/catalog` — body `{id, team}`; pins current
  bucket sha; 404 if the catalog no longer serves it; 409 if already present.
- `DELETE /api/agents/{name}/skills/{id}` — either class; removing a skill
  the agent doesn't carry is 404.
- `GET/PUT /api/agents/{name}/skills/local/{id}` — content read/write for
  local skills, shaped like the knowledge routes (including the 502
  plain-language bodies and the delivery-sha response on writes). Deletion
  for both classes goes through the unified DELETE above — one removal path.

UI: the agent page gains a Skills panel beside Knowledge — catalog entries
render with team/sha provenance and a Remove affordance plus an Add flow
backed by the existing catalog search; local entries open in the same
editor UX knowledge uses (diff preview included, it's free from P2b).

## Memory intake: one-shot ingestion

A new content class with the opposite lifecycle from knowledge: knowledge
files ARE the memory source of truth (delete = forget on reseed); intake
files are a delivery vehicle (ingest once, memories persist, source is
disposable). The two must never blur — different directories, different
seeder semantics, different UI copy.

- **Upload**: `POST /api/agents/{name}/intake` — text only for v1 (`.md`,
  `.txt`), 256KB cap, filename slug-validated. Lands at
  `bundle/intake/<filename>` via the same commit + delivery-sha flow.
  Re-uploading an identical filename+content no-ops; same name with new
  content is a new ingestion (new content-sha).
- **Seeder intake mode** (runtime-kit change, daedalus repo): at boot, after
  knowledge convergence, the seeder walks `intake/`: any file whose
  content-sha is missing from the intake ledger is chunked and appended into
  techne with the same chunking rules knowledge seeding uses, then the sha is
  recorded in the ledger (a small table beside techne's existing state,
  written after the memories commit — a crash between the two re-ingests on
  next boot rather than losing content; duplicate memories from that rare
  window are acceptable and inspectable, silent loss is not).
  **Convergence never touches `intake/`**: deleting an intake file removes no
  memories, and intake rows are excluded from the removed/unchanged counts
  knowledge reports.
- **Status**: the Intake panel lists files as **pending** or **ingested**,
  derived from the signal knowledge chips already use — once the agent runs
  at (or past) the delivery sha that carried the file, boot ordering
  guarantees the seeder processed it. No new status channel from the pod.
- **Cleanup**: once *ingested*, a Remove button deletes the source file from
  the bundle (a normal delivery). Copy states plainly that memories are kept.
  No auto-cleanup policy in v1.
- **Runtime rollout**: ships as a runtime-image bump recorded in the org
  config's `runtime_image`; agents pick it up on their next delivery, and an
  intake upload IS a delivery — no special migration step. Agents on older
  runtimes simply carry unprocessed `intake/` files until their image
  catches up (visible as a stuck *pending* chip; acceptable, documented).

## Slack operator gate

New config key `slack_operators: list[str]` (empty default).

- Unset/empty: today's behavior — panel and routes open (dev parity).
- Non-empty: the Slack section renders and the slack routes respond only
  when the request's verified `iap_email` is on the list (case-insensitive).
  Route enforcement is a 403 with plain copy — hiding the panel is UX, the
  403 is the boundary.
- IAP disabled (local dev, no `iap_audience`): treated as operator.
- Delivered to the room via its deployed config; also honored from org
  config if present there (one more ORG_KEY), so a client org can manage its
  operator list in its config repo.

The agent detail response carries a `slack_visible` boolean so the UI never
guesses.

## Testing

House pattern per task: RED-first unit + route tests, fakes extended
(FakeStorage/FakeGitHub/fake seeder ledger as needed), svelte-check + vitest
for UI, suite counts reported. Specific pins the design demands:

- catalog-skill add pins the sha served *now*, not the assembly-era sha;
- local/catalog id collision 409s both directions;
- convergence excludes `intake/` (regression test against the P2b
  delete-last-file semantics — knowledge behavior unchanged);
- intake ledger idempotence: same sha never double-ingests across boots;
  crash-window re-ingest tolerated (test documents the duplicate-over-loss
  choice);
- slack routes 403 for non-operators the moment the list is non-empty, and
  keep working for operators and in no-IAP dev mode;
- knowledge routes and chips behave byte-identically before/after (the two
  classes must not bleed into each other).

## Milestone (exit criterion, on the deployed room)

Against the production deployment, in one session:

1. Add an agent-local guardrail skill to a live agent through the browser;
   after the delivery chip lands, a chat probe shows the behavior change.
2. Remove a catalog skill and add a different one; bundle bytes match the
   bucket object at its pinned sha.
3. Upload an intake document; watch pending → ingested; chat proves recall;
   Remove the file; chat proves recall *again* (memories survived cleanup).
4. A non-operator (fresh email in a test assertion, or the list set to
   exclude the operator temporarily) sees no Slack panel and gets 403 from a
   slack route; the operator sees both restored when re-listed.

## Non-goals (this phase)

- Tools/orchestrator enforcement (always-executed code hooks) — next spec,
  already constraint-ledgered; nothing here may preempt its contract.
- PDF/docx conversion on intake; binary uploads.
- Memory browsing/editing/deletion UI (one-shot intake's removal story
  beyond source-file cleanup lives there, later).
- Auto-cleanup policies for ingested files.
- Copy-on-write forking of catalog skills into local ones.
- Any change to assembly-time skill selection.
