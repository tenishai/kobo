# Colleague Quorum Implementation Plan (epistemics spec B)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Task 3 is controller-driven.

**Goal:** An agent remembers what specialists tell it (explicitly, with provenance, at low confidence), and such a fact can retire an older colleague-sourced fact only after a quorum of same-team specialists corroborates it; operator-taught facts are untouchable by any quorum.

**Architecture:** Both tasks live in the daedalus runtime on a branch stacked on `feat/belief-revision`. Task 1 adds the `remember` tool to the colleagues provider (memory db access wired in from the agent). Task 2 adds the corroboration branch to the revision pass (directory query, bounded verification asks through the switchboard, model-judged agreement, quorum promotion with attribution, the operator shield). The room, switchboard, and UI are untouched.

**Tech Stack:** daedalus + techne (in-process), the existing colleagues provider client, the existing revision pass seams.

## Global Constraints

- NO em dashes in model-visible or log strings authored by us; no vendor/client names in tracked files (vendor scan its OWN pre-commit step, list at kobo `.superpowers/sdd/vendor-names.txt`).
- Policy constants, single source: quorum size 2 (asserter + >=1 independent same-team agree); verification asks bounded at 3 colleagues per candidate per pass; the operator shield (retiree source starts `intake:chat-` => never promoted by this branch) checked FIRST.
- The pass's contracts from spec A are inviolate: never raises, never on the reply path, idempotent, every failure isolated and counted.
- Provenance format exact: `colleague:<name>#team=<team>` (team from the directory at remember time; empty team omits the `#team=` suffix).
- TDD RED-first. Runner: `PYTHONPATH=src:../techne uv run --no-sync pytest -q` (baseline 212 passed / 5 skipped on feat/belief-revision).

## File Structure

- `src/daedalus/tools/colleagues.py` — the `remember` tool (+ db_path/team plumbing from Agent when constructing the provider).
- `src/daedalus/memory/revision.py` — the corroboration branch + policy constants.
- Tests: `tests/test_tools_colleagues.py`, `tests/test_memory_revision.py`.

---

### Task 1: The remember tool

**Files:** `src/daedalus/tools/colleagues.py`, `src/daedalus/bundle.py` only if config needs a field (it should not: db_path comes from the agent's memory store at wiring time in `agent.py:_tools_from_spec` — trace how the provider is constructed and thread `db_path` through the constructor), `src/daedalus/agent.py` (constructor wiring), tests.

**Interfaces:**
- Consumes: `techne.core.knowledge.store(db_path, content, type="fact", tags=..., confidence="low", source=...)`; the provider's existing directory fetch (for validating `learned_from` and resolving its team).
- Produces: tool `colleagues.remember(fact, learned_from)`: refuses self, refuses names absent from the directory (plain text results, never raises); stores fact verbatim with `source=f"colleague:{learned_from}#team={team}"` (or without suffix when team empty) and `confidence="low"`; returns plain confirmation text naming who it credits. Token-absent and db-absent degrade politely like the sibling tools. Tool joins the allow-list expectation: NOTE in the report that the room's composed `tools.allow` must gain `colleagues.remember` at rollout (Task 3 handles it; do not touch kobo).

- [ ] RED: store call args exact (provenance string both shapes, confidence low, type fact); self/unknown refusals; directory-fetch failure text; db-path-absent degrade; envelope of the directory call unchanged.
- [ ] Implement; GREEN full suite; lint; vendor scan (own step); commit on branch `feat/colleague-quorum` off `feat/belief-revision`: "feat(tools): agents remember what specialists tell them"; push.

### Task 2: The corroboration branch

**Files:** `src/daedalus/memory/revision.py`, tests.

**Interfaces:**
- Consumes: the pass's existing candidate iteration and promote seam (`promote_candidate(db_path, id, decided_by=..., decision_reason=...)`); the colleagues provider's directory + ask capabilities (construct or receive a client the same way the provider does: url/token from the same bundle config; the pass runs inside the agent which already holds the wired provider — prefer REUSING the agent's provider instance if reachable, else construct from the same spec; report the choice).
- Produces, inside the pass after verdict ingestion, for each pending retire candidate:
  1. Shield: if the RETIREE's source starts `intake:chat-` -> skip (count as shielded), never promote here.
  2. Spec-A branch unchanged: survivor operator-sourced -> promote as before.
  3. New: survivor source `colleague:<asserter>...` -> parse asserter+team; directory-query same-team others (exclude asserter and self); ask up to 3: a verification prompt containing the fact and asking for agreement or disagreement with reasons; resident model judges each reply as agree/disagree/unclear; tally: asserter counts as one agree; at >=2 agrees -> `promote_candidate(..., decided_by=f"quorum:{agrees}-{team}", decision_reason="corroborated by same-team specialists")`; any disagree -> leave pending (count disputed); no same-team others -> leave pending (count uncorroborable).
  4. Events: `revision_corroborating` (per candidate), `revision_quorum` (per promotion), counts folded into the report and `revision_done`.
- All network/model failures isolated per candidate (counted, never raised); the bounded ask count is a hard cap per candidate per pass.

- [ ] RED: full matrix per the spec's testing section (quorum promote w/ exact attribution; single-voice pending; disagreement pending; no-colleagues pending; shield pins incl. quorum-vs-operator-fact; bounded asks; failure isolation; idempotence across passes; events).
- [ ] Implement; GREEN full suite; lint; vendor scan (own step); commit "feat(memory): a quorum of specialists can change this mind, the operator's word cannot be outvoted"; push.

### Task 3: Rollout + acceptance (controller-driven)

- [ ] Runtime image from `feat/colleague-quorum` + techne v0.14.0 -> push; org `runtime_image` bump.
- [ ] Allow-list: extend both live agents' `tools.allow` with `colleagues.remember` via the composer update helper (one commit each with the FROM bump; kobo's composer `_tools_block` gains the third entry in a tiny follow-up commit so future agents are born with it).
- [ ] Acceptance stage 1 (the room exercised end to end): create a SECOND same-team specialist agent through the create flow (a networking persona on the same team as the existing specialist).
- [ ] Acceptance stage 2 (the choreography): agent W remembers a colleague fact F1 from specialist A (chat-driven: ask then remember); teach nothing; later W remembers contradicting F2 from specialist B; W's next boot stages the conflict, corroborates with the OTHER same-team specialist, promotes with `quorum:` attribution; verify chain + attribution in-pod and the verification asks visible in the conversations panel.
- [ ] Acceptance stage 3 (the shield): a candidate whose retiree is operator-taught stays pending across boots despite available quorum.
- [ ] Upstream PR (stacked); ledger + memory close; scratch specialist destroyed through the room (destruction pipeline re-exercised) unless the operator wants to keep it.
