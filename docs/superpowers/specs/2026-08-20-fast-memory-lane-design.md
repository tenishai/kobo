# Fast Memory Lane: teach writes memory in seconds, the repo archives in the background

**Date:** 2026-08-20
**Status:** Approved (operator approved the full design in conversation; this document records it)
**Depends on:** colleagues switchboard + talk (2026-08-18/19), teach-from-chat (2026-08-19), belief revision + colleague quorum (2026-08-20)

## Problem

Teaching an agent a fact today takes minutes and a pod rotation. The teach
route commits a note into `bundle/intake/`, which triggers a full image
rebuild and rollout; the seeder ingests the note at the next boot. The
operator watched a teach save crawl and then hit "The switchboard did not
answer" because the agent's pod was mid-rotation from the very delivery the
teach caused. Memory updates must not cost a rebuild or a pod rotation.
Rebuilds are for code-shaped changes (skills, tools, locks, runtime pins)
only.

## Shape

Teaching becomes a **signed system task through the switchboard**. The
repo commit stays, demoted to a **background archive**. The agent's pod is
never rebuilt or rotated for a memory write.

1. **Teach rides the relay.** `POST /api/agents/{name}/teach` builds the
   note exactly as today (`format_note` / `note_filename`), then relays a
   task through cabal to the agent, marked as a system task via task
   metadata. The daedalus runtime intercepts it before any model call,
   writes the memory into techne immediately, and replies with a
   deterministic confirmation. Round trip is seconds. Because it rides the
   switchboard, the exchange is recorded in cabal conversations like any
   other.

2. **The repo commit becomes a background archive.** After the relay
   confirms, kobo commits the same note bytes to `bundle/intake/` in the
   background (same `upload_intake_file`, same runtime-image re-pin). The
   runtime's teach handler writes the seeder's content-sha ledger entry at
   teach time, so the next natural rebuild's seeder sees the sha already
   ledgered and skips the file. No double ingest, no rebuild triggered by
   teaching.

3. **Revision fires in-session.** Right after a taught write, the runtime
   kicks the existing revision pass (`run_revision_pass`) asynchronously,
   off the reply path. A taught correction retires its stale predecessor in
   seconds instead of at the next boot. The boot-time pass stays for
   backfill.

4. **Chat becomes delivery-aware.** When the chat relay fails and the
   agent's deployment is actually mid-rollout, the room says so:
   "Your agent is receiving a delivery right now. Try again in a minute."
   (503) instead of the generic switchboard 502.

5. **The teach modal confirms fast and honestly.** Instant lane:
   "Learned. Filed to the repo in the background." Archive fallback keeps
   today's copy: "Saved to memory. It arrives with the next delivery."

## Precedent and doctrine

- iris (infra-agent-iris) already does runtime `learn()` writes into an
  embedded techne on a PVC in prod, including runtime supersede. The
  pattern is proven; what iris lacks (an external teach door, judged
  epistemics, honest seeding) is exactly what kobo's build keeps.
- techne's documented deployment is embedded-library-in-your-own-agent
  process, SQLite-only by decision (techne#135). No techne server, no
  sidecar. The PVC-backed embedded store IS the fast lane; only the
  delivery path around it changes.

## Design

### Daedalus: the teach system task

**Wire contract.** A teach task is a normal a2a task envelope whose task
`metadata` carries `{"system_task": "teach", "filename": "<chat-*.md>"}`
and whose current message text is the full formatted note (the exact bytes
kobo will commit to `bundle/intake/<filename>`). `task_from_a2a` already
passes `metadata` through verbatim (`src/daedalus/transports/a2a.py:101`)
and `Task.metadata: dict` already exists (`src/daedalus/task.py:47-52`).

**Authorization: the marker is honored only from the operator.** The
colleague quorum arc's whole point is that colleague facts earn their way
in at low confidence; a colleague able to forge a teach task would mint
operator-grade `intake:chat-*` facts and pierce the operator shield. So:

- `verify_header` already returns the verified JWT claims dict
  (`src/daedalus/transports/a2a_auth.py:82-119`); the transport currently
  discards it. `build_a2a_app` now captures the claims and stamps
  `task.metadata["_verified_sub"] = claims.get("sub")` after parsing, on
  BOTH POST surfaces (`/a2a/tasks` and the JSON-RPC `message/send`). The
  stamp is transport-owned: any wire-supplied `_verified_sub` is deleted
  or overwritten unconditionally, including when no verifier is configured
  (key absent). It cannot be forged from outside.
- The teach handler honors the marker only when `_verified_sub` is present
  and does NOT carry the colleague convention prefix `agent:`. Operator
  relays arrive with a cabal re-mint of kobo's service subject; colleague
  dispatches arrive as `agent:<name>`. Unauthorized teach tasks get a
  polite refusal reply, write nothing, and log a warning. Fail closed: no
  verified sub (unlocked agent) means no teach lane.

**The write.** A new `src/daedalus/memory/teach.py` module (pure, no HTTP):

- Validates `filename` against the intake alphabet (`^[a-z0-9-]{1,40}\.md$`
  shape, mirroring kobo's intake rule) and rejects anything else.
- Computes `sha256` over the note's UTF-8 bytes; `source =
  f"intake:{filename}#{digest[:12]}"` — byte-for-byte the seeder's own
  convention (`docker/seeder.py:228-233`), because kobo commits these same
  bytes as the file the seeder would otherwise read.
- Sections and types the note the same way the seeder does (split on
  `## ` headings, whole-stripped-file when none; `type_for` stem mapping,
  chat notes land as "fact") and calls techne `knowledge.store(db, content,
  type=..., tags="intake", confidence="high", source=source)` per section —
  the seeder's exact store call.
- Inserts the ledger row into `intake_state` (`sha256` PK, `filename`,
  `entry_ids`) in `$TECHNE_HOME/seed_state.db` — the same DB and schema the
  seeder owns (`docker/seeder.py:70-74`, path from `main()` at 341/359).
  A sha already ledgered is an idempotent no-op ("already known" reply):
  re-teaching identical text same-day double-taps safely.
- Parity is enforced by test, not by import: a parity test loads
  `docker/seeder.py` and asserts that ingesting a file written from the
  same note yields identical sections, types, sources, and ledger row as
  the teach handler wrote, for both a flat note and a note containing
  `## ` headings. Drift between the two implementations fails the suite.

**Interception point.** `Agent.handle_task`
(`src/daedalus/agent.py:247-282`), before the enforcement pre-task check
and before `self._loop` ever runs: teach tasks never reach the model, never
consume a completion, never trip approvals. The reply is a deterministic
completed task ("Learned. I will remember that." / refusal / already-known
variants). Agents without a techne memory configured refuse politely.

**In-session revision.** After a successful teach write, the agent fires
the existing `run_revision_pass` asynchronously (fire-and-forget task,
reference retained against GC), serialized behind a new `asyncio.Lock` so a
boot pass and a teach-triggered pass never run concurrently — the pass is
already re-runnable and rescans standing pendings by design
(`src/daedalus/memory/revision.py:36-41`, `398-414`). Failures in the pass
never touch the teach reply (it already went out).

### Kobo: relay-first teach, background archive

`POST /api/agents/{name}/teach` (main.py) keeps all current validation
(empty, oversize, ready-gate) and note construction, then:

- **Instant lane** (when `cabal_enabled(cfg)` and the relay succeeds):
  build the teach envelope — a new `_build_teach_envelope(note, filename)`
  beside `_build_task_envelope` (clients.py:983), same task shape plus the
  metadata marker — relay it via `CabalClient.relay`, then schedule the
  repo commit as a FastAPI background task (same `upload_intake_file`
  call with `runtime_image` re-pin; failure is logged, never surfaced —
  the memory is already saved; the file simply shows up in the intake
  panel when the commit lands). Response: `{"filename": ..., "mode":
  "instant"}`.
- **Archive lane** (cabal off, or the relay fails for any reason,
  including the agent mid-rollout): today's synchronous behavior,
  unchanged. Response: `{"filename": ..., "mode": "archive", "sha": ...}`.
  Teach therefore never 502s on a delivery window; it degrades to the
  archive lane.

### Kobo: delivery-aware chat copy

New helper `rollout_in_progress(kubectl, *, namespace, deploy) -> bool` in
`api/kobo_api/knowledge.py`: reads the deployment and reports true when a
rollout is genuinely in flight (`observedGeneration < generation`, or
`updatedReplicas`/`availableReplicas` short of spec). The chat route's
relay-failure handler (main.py:1432-1445) consults it — errors in the
probe itself fall through to the existing copy — and returns 503
`{"error": "Your agent is receiving a delivery right now. Try again in a
minute."}` when true, else the existing 502 switchboard copy. The UI
already surfaces server-provided error text verbatim, so no ChatPanel
change is needed.

### UI: the teach modal

`TeachModal.svelte` keys its success copy on the response `mode`:
`instant` shows "Learned. Filed to the repo in the background."; `archive`
keeps "Saved to memory. It arrives with the next delivery." No other UI
changes.

## What does NOT change

- Rebuilds remain the delivery for code-shaped changes: skills, tooling,
  locks, runtime pins, metadata that composes into the bundle.
- The seeder, its convergence semantics, and boot-time ingest are
  untouched — the teach handler only pre-fills the same ledger.
- Cabal needs no changes; the teach task is an ordinary relayed task.
- The colleagues `remember` tool, quorum, and shield are untouched; teach
  provenance stays `intake:chat-*` and keeps its operator-shield standing.
- The intake panel still lists archived notes with ancestor-derived
  status; a taught note reads "pending" there until the next natural
  rebuild even though the memory is live. Accepted wart (the panel
  describes the archive lane); registered as a follow-up, not gold-plated
  now.

## Security

- Teach interception is operator-only, gated on the transport-stamped
  verified JWT subject; the stamp is unforgeable from the wire and absent
  means refused.
- Tokens: nothing new is logged or persisted; the teach lane carries note
  text only.
- No client names in any tracked file of kobo or daedalus (standing rule);
  vendor scan gates every commit.
- No em dashes in user-facing copy (UI and API copy guards stand).

## Testing

- Daedalus: teach module unit tests (validation, sha/source parity,
  ledger idempotence, sectioning); the seeder-parity test; transport tests
  for `_verified_sub` stamping on both POST surfaces (wire-forgery
  overwrite, verifier-absent deletion); agent tests for interception
  (pre-model, pre-enforcement), authorization matrix (operator sub /
  agent: sub / absent), refusal replies, revision fire + lock
  serialization.
- Kobo API: envelope shape test for the metadata marker; teach route mode
  branches (instant, archive fallback on relay failure, cabal-off),
  background-commit scheduling and logged failure; `rollout_in_progress`
  truth table; chat route 503-vs-502 branch.
- Kobo UI: TeachModal mode-keyed copy; copy guards.
- Live acceptance on ward: teach a fact via the UI, confirm the techne row
  and ledger row exist within seconds via kubectl exec, pod start time
  unchanged (no rotation), confirmation visible in chat and in the
  conversations panel, and a taught correction retires its predecessor
  in-session without a boot.

## Rollout

Daedalus changes land on a new branch stacked on `feat/colleague-quorum`
(PR stacks 13 -> 14 -> 15 -> new). New runtime image built with buildx
(machine's gcloud is broken), org runtime pin bumped, agents re-pinned and
rebuilt once — fittingly, the last memory-shaped rebuild — then the kobo
image rolls. Digest-verify every tag before rolling.
