# The agent changes its mind properly (epistemics spec A) — design

Approved 2026-08-20. Operator rulings: A bold, B cautious — operator
corrections auto-supersede immediately; colleague facts (spec B,
later) always stage and only a quorum of same-team specialists
promotes. Zero new ceremony: no toggles, no magic phrasing.

Grounded by a fresh source sweep: the memory system's contradiction
judge, staging table, and attributed promotion are all plain
importable functions (the runtime's seeder already imports the same
library in-process); the retire action is not faithfulness-gated;
nothing anywhere auto-mutates live memory today, by explicit
upstream design ("memory sovereignty"). This spec adds the first
policy-holding caller, in the runtime, with the staging table kept
as the audit trail.

## What ships

1. **The revision pass (runtime-side).** A new module in the agent
   runtime that, shortly after boot (async, after transports are up,
   never on the reply path), drives the memory library's own
   machinery end to end: emit the pending contradiction checks, have
   the agent's OWN resident model judge each prompt, ingest the
   verdicts (which stage retire candidates linking old fact to new),
   and then apply policy: candidates whose newer fact is
   operator-attested (source `intake:chat-*`) are promoted
   immediately, attributed `decided_by: operator`. Everything else
   stays pending, untouched (spec B's quorum will own those).
   Failures are logged and swallowed; the pass never blocks or
   crashes the agent; each run is idempotent (the library's own
   check-hash dedup rails).
2. **The config gate.** The bundle's memory stanza gains an optional
   `revision` field (absent = off, full back-compat for other
   runtime users); the room composes `revision: operator` for its
   agents. The runtime warns-not-fails on unknown memory fields
   (verified before rollout; the rollout order below assumes the
   spec-B lesson regardless).
3. **The room composes it.** Composer emits the field at create;
   an in-place update helper backfills the two live agents in the
   same one-commit-with-FROM-bump discipline as always.
4. **Observability.** The pass emits runtime events (revision
   started, candidates judged, promoted, left pending) so the trace
   stream shows the agent changing its mind, not just having done
   so.

## Rollout order (spec-B lesson applied)

Runtime image first, org runtime bump second, room composer third,
backfills last (one commit each). The acceptance is already waiting
in the world: a live agent currently holds two contradicting
operator-taught facts about the workshop mascot; the first boot with
revision on must find that pair by itself, retire the stale fact
(superseded_by set, excluded from every search thereafter,
dependents cascaded), and the agent must answer with only the
corrected fact. Then one fresh correction taught end to end proves
the loop live rather than merely retrospective.

## Non-goals

- No colleague-sourced writes, no quorum, no confidence policy
  (spec B).
- No mid-session revision (boot-time pass only in v1; the task-end
  seam is known and banked for later).
- No changes to the memory library upstream; the runtime is a pure
  caller of its public functions.
- No UI; the audit trail is the staging table and the event stream.

## Testing

Runtime: pass gated off when the field is absent (byte-identical
behavior); full loop against a real temporary memory db (store two
contradicting facts, fake the judge model's verdict, assert the
stale row's superseded_by is set and search excludes it); policy
matrix (operator-source promotes with attribution; non-operator
sources stay pending); model-failure and db-failure isolation (agent
boots and serves regardless); idempotence (second run stages
nothing new). Room: composer emission matrix and update-helper
round-trip, the usual pins. Live: the waiting mascot contradiction
resolves at first boot, proven from inside the pod and in chat.
