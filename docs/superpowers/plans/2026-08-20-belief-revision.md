# Belief Revision Implementation Plan (epistemics spec A)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Task 3 is controller-driven.

**Goal:** An agent taught a correction retires the stale fact by itself: the runtime drives the memory library's contradiction judge with the agent's own model and auto-promotes operator-attested retirements, leaving everything else staged.

**Architecture:** One new runtime module (the revision pass) wired as a non-blocking post-boot task, gated by a new optional `memory.revision` bundle field. The room composes the field. The memory library is called in-process as a pure library (its staging table is the audit trail); no upstream changes.

**Tech Stack:** The daedalus runtime (branch off the deployed lineage) + the techne library it already imports; kobo composer.

## Global Constraints

- NO em dashes in user-facing/model-visible strings; no vendor/client names in any tracked file in either repo (vendor scan its OWN pre-commit step per task, list at kobo `.superpowers/sdd/vendor-names.txt`).
- The pass NEVER gates or delays replies, NEVER crashes the agent (every failure logged and swallowed), and is idempotent across restarts.
- Policy in v1: ONLY candidates whose newer/focus fact has source starting `intake:chat-` are auto-promoted, `decided_by="operator"`, `decision_reason="taught correction"`. All other staged candidates remain pending untouched.
- Absent `memory.revision` = the pass never constructs or runs (byte-identical runtime behavior, pinned).
- TDD RED-first. Runners: daedalus `PYTHONPATH=src uv run --no-sync pytest -q` (baseline 196 passed / 5 skipped on the deployed lineage); kobo `venv/bin/python -m pytest -q` from api/ (baseline 1685).

## File Structure

- daedalus: new `src/daedalus/memory/revision.py`; `src/daedalus/bundle.py` (`MemorySpec.revision`); wiring in `src/daedalus/agent.py` or the run entrypoint (wherever transports come up; find the post-startup seam and report it); tests `tests/test_memory_revision.py`.
- kobo: `api/kobo_api/composer.py` (memory stanza emission + `update_agent_yaml_memory_revision(bundle_dir, *, revision)` helper), tests.

---

### Task 1: The revision pass (daedalus repo)

**Files:** `src/daedalus/memory/revision.py` (new), `src/daedalus/bundle.py`, the startup wiring site, `tests/test_memory_revision.py` (new).

**Interfaces (verified against techne source; confirm at the tag the runtime ships):**
- `techne.core.conflict.conflict_check_emit(config, *, max_items=None) -> list[dict]` (batches: focus item, comparison set, rendered prompt or the parts to render via `run_conflict_check(focus, comparisons)`, and a check hash; read the emit output shape and use it as-is).
- `techne.core.conflict.conflict_check_ingest(config, focus_id, comparison_ids, check_hash, response) -> dict` — stages `action='retire'` candidates, `origin='conflict'`, never applies. `Config(db_path=Path(...))` from `techne.config` suffices.
- `techne.core.reflect.promote_candidate(db_path, candidate_id, *, decided_by, decision_reason, force=False) -> int | None` — pending-guarded, race-safe; `retire` is not faithfulness-gated.
- The agent's resident model call: reuse the same completion client the task loop uses (trace how `Agent` invokes the model and call the same seam with no tools; the judge prompt is plain text in, JSON-ish verdict text out).
- Produces: `run_revision_pass(bundle, model_call, db_path, *, policy) -> RevisionReport` (counts: checks judged, staged, promoted, skipped, errors) + the wiring: when `bundle.spec.memory.revision == "operator"`, schedule the pass once as a background asyncio task after the serving loop is up; emit events (`revision_start`, `revision_judged`, `revision_promoted`, `revision_done`) through the existing event bus.
- `MemorySpec.revision: str | None = None` (accept `"operator"`; unknown values warn and disable, never crash).

- [ ] RED: gate-off byte-identity; full loop on a real temp techne db (two contradicting rows, scripted judge verdict, assert superseded_by set + search exclusion + attribution fields); non-operator source stays pending; judge/model failure and db failure isolation (pass reports errors, agent unaffected); idempotence (second run, nothing new staged or promoted); events emitted.
- [ ] Implement; GREEN full suite; lint; vendor scan (own step); commit on branch `feat/belief-revision` off the deployed lineage (currently feat/colleagues-tools head): "feat(memory): the agent retires what the operator corrects"; push.

### Task 2: The room composes the gate (kobo repo)

**Files:** `api/kobo_api/composer.py`, `api/tests/test_composer.py`.

**Interfaces:**
- Produces: composed agent.yaml memory stanza gains `revision: operator` whenever the memory provider is techne (unconditional for room agents; pin that a bundle composed without techne memory is untouched); `update_agent_yaml_memory_revision(bundle_dir, *, revision: str)` in-place helper, same regex idiom family as the other update helpers, idempotent, add-or-replace, preserves every other line; ClientError on missing file/stanza.

- [ ] RED: emission matrix; helper add/replace/idempotent/preserves; byte-identical for non-techne bundles.
- [ ] Implement; GREEN full suite; ruff; vendor scan (own step); commit "feat(api): agents are composed to change their minds".

### Task 3: Rollout + acceptance (controller-driven)

- [ ] Verify the deployed runtime's `MemorySpec` tolerates the unknown field (warn-not-fail) by reading the class config; regardless, apply the spec-B ordering: runtime image FIRST.
- [ ] Build `daedalus-runtime:<sha7>-t0140` from `feat/belief-revision` + techne v0.14.0; push; org `runtime_image` bump.
- [ ] Roll the room with Task 2; backfill both agents, one commit each: FROM bump + `revision: operator` via the helper.
- [ ] Acceptance, phase 1 (retrospective): the live agent already holds the two contradicting mascot facts; after its revision-enabled boot, verify in-pod that the stale row has `superseded_by` set and search excludes it, the event stream shows the judgment, and chat answers only the corrected fact.
- [ ] Acceptance, phase 2 (live): teach one fresh correction end to end; next boot retires its predecessor the same way.
- [ ] Upstream daedalus PR (stacked on the colleagues PR); ledger + memory close; spec B (quorum) queued for its own brainstorm.
