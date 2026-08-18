# Persona Metadata Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every agent carries directory truth — team (derived from skill owners), purpose tags (persona-drafted), and a capability line earned at first ready — displayed on Overview, editable by anyone, and baked into the agent card colleagues will someday read.

**Architecture:** Upstream `owner` metadata flows into the FTS index's EXISTING `team` column (skills kind); the team picker reads distinct values. Purpose tags ride the persona-draft model call with a deterministic fallback. Capability generates room-side after the hello step stores first words, then re-commits the card; Edit details and Regenerate keep row and card in lockstep through the bundle pipeline.

**Tech Stack:** FastAPI + SQLAlchemy + FTS5, the existing Vertex model-client family, Svelte 5 + the jsdom rig.

## Global Constraints

- NO em dashes in user-facing copy (both guards enforce their scopes; new API error/detail strings and all UI copy stay plain).
- No vendor/client names in tracked files; the vendor scan is its OWN pre-commit step per task.
- Tokens/credentials untouched; the card metadata addition is ADDITIVE (the colleague-protocol card model tolerates extra metadata — verified in discovery).
- Team values are slugs, validated by the EXISTING team-slug shape (`^[a-z][a-z0-9-]{0,23}$` — the same rule CreatePersonaRequest documents; single source, import it, never restate).
- Capability generation NEVER blocks readiness: model failure falls to the deterministic tier (persona description's first sentence, clamped); the hello step's own success/failure semantics are untouched.
- TDD RED-first. Baselines at arc start: API 1314, UI 310, check 0, build green. Runners: `venv/bin/python -m pytest -q` from api/, `npm test` from ui/.

## File Structure

- `api/kobo_api/search.py` — `_load_catalog_entries` learns `owner` (skills kind) feeding the existing `team` column; new `distinct_skill_teams(engine) -> list[str]`.
- `api/kobo_api/main.py` — `GET /api/teams`; create route accepts `team` + `purpose_tags`; agent payload grows `purpose_tags` + `capability`; `PATCH /api/agents/{name}/metadata`; `POST /api/agents/{name}/metadata/regenerate`.
- `api/kobo_api/models.py` — `purpose_tags` (JSON text), `capability` (String) + manual-ALTER docstring lines.
- `api/kobo_api/persona_draft.py` — draft response gains `purpose_tags` (model + fallback from persona tags).
- `api/kobo_api/steps.py` — HelloStep tail triggers capability generation (delegating to a new small module function, not inline).
- `api/kobo_api/metadata.py` (new) — capability generation (model prompt + deterministic fallback), normalization helpers for purpose tags, the card re-commit flow (uses the composer card-update helper).
- `api/kobo_api/composer.py` — `_agent_card` bakes `metadata: {team, purpose_tags, capability}` (capability empty at compose); new `update_agent_card_metadata(bundle_dir, *, team, purpose_tags, capability)` editing `agent-card.json` in place, preserving every other field.
- `api/kobo_api/clients.py` — the model-client family gains `capability(...)` beside `suggest`/`draft` (same fallback discipline).
- UI: `CreateModal.svelte` (final station grows team picker + purpose chips), `AgentDetail.svelte`/Overview (display + Edit details modal + Regenerate), `api.ts`/`queries.ts` (types, teams query, metadata mutations), tests throughout.

---

### Task 1: Owner-aware catalog + the teams endpoint

**Files:** `api/kobo_api/search.py`, `api/kobo_api/main.py` (route only), tests `api/tests/test_search.py` + the web test file that covers catalog routes (find it — grep for search_skills tests).

**Interfaces:**
- Consumes: the existing catalog index JSON shape (read `_load_catalog_entries` first — skills entries carry name/description/tags/sha; `owner` arrives as a NEW OPTIONAL key from the upstream pipeline).
- Produces: skill FTS rows carry `team` = entry's `owner` when present, `""` when absent (personas keep their existing team semantics untouched); `distinct_skill_teams(engine) -> list[str]` (sorted, non-empty values only); `GET /api/teams` → `{"teams": [...distinct...], "default": "shared"}` (signed-in only via middleware, no other gate).

- [ ] RED: ingest tolerance matrix — index entries with owner, without owner, mixed; distinct extraction; the endpoint shape; search_skills response carries team for owner-tagged skills (check whether the response already includes team — the FTS insert has it; if the route's row-shaping drops it, include it).
- [ ] Implement; GREEN; full suite; ruff; vendor scan (own step); commit "feat(api): the catalog learns whose skills these are".

### Task 1b: The marketplace dialect + the repoint (added after the operator corrected the source of truth)

The room must read the canonical marketplace bucket (prod project; exact coordinates live in the org config and the untracked runbook, never here). Grounded live: its `catalog/catalog.json` is `{"skills": {<name>: {name, version, description, when_to_use, tags, profiles, content_hash, size}}, "count": N}`; skill bodies live at `catalog/<name>/SKILL.md` (head) and `catalog/<name>/versions/<version>/SKILL.md` (immutable). `owner` and release metadata arrive in this same file when the marketplace taxonomy ships upstream.

**Files:** `api/kobo_api/search.py` (a second loader dialect), the skill-fetch path used at attach time (trace who reads skill content from the bucket — the assembly/attach flow with sha pins), `api/kobo_api/config.py`/org handling only if a format marker is needed (prefer sniffing: a top-level `count` + dict `skills` IS the marketplace dialect; the legacy list-shaped index stays as-is), tests.

**Interfaces:**
- Produces: `_load_catalog_entries` (or a sibling it dispatches to) reads BOTH dialects, sniffed by shape; marketplace entries map id=key, name, description, tags, team=`owner` when present (same precedence rules as Task 1, including the empty-string-owner guard from Task 1's review: a blank owner is garbage, logged, falls through), sha=`content_hash`; the attach/fetch path resolves marketplace content at `catalog/<name>/versions/<version>/SKILL.md` (immutable pin) with head fallback ONLY when the version object is absent, logged.
- The org config flip (skills_bucket to the marketplace bucket) happens at ROLLOUT (Task 5), gated on the operator's cross-project read grant for the room's service account; the code ships dialect-ready before any flip.

- [ ] RED: dialect sniffing matrix (legacy fixture untouched, marketplace fixture ingested, malformed marketplace entries tolerated per the Task 1 rules); fetch-path version pinning + head fallback logged; empty-string owner guard (closes the Task 1 review Minor).
- [ ] Implement; GREEN; full suite; ruff; vendor scan (own step); commit "feat(api): the room learns the marketplace's tongue".

### Task 2: Storage, draft, create, payload

**Files:** `api/kobo_api/models.py`, `api/kobo_api/persona_draft.py`, `api/kobo_api/main.py`, tests (`test_persona_draft.py`, the agents web tests).

**Interfaces:**
- Consumes: Task 1's teams endpoint (create-time team validation cross-checks nothing against it — any valid slug is accepted, the picker is UX not law).
- Produces: `Agent.purpose_tags` (JSON text, ALTER line `ALTER TABLE agents ADD COLUMN purpose_tags VARCHAR;`), `Agent.capability` (String, `ALTER TABLE agents ADD COLUMN capability VARCHAR;`); draft response gains `purpose_tags: [...]` (≤5, lowercased, deduped; fallback = persona's own tags); create request accepts optional `team` (slug-validated, default "shared") and `purpose_tags` (normalized server-side, cap 8); agent payload carries `team` (already), `purpose_tags`, `capability` (null until earned).

- [ ] RED per behavior (draft tags incl. fallback tier; create acceptance + normalization + slug rejection copy plain; payload shape) → implement → GREEN → gates → vendor scan (own step) → commit "feat(api): agents are born knowing whose they are and why".

### Task 3: Capability earned + card lockstep

**Files:** `api/kobo_api/metadata.py` (new), `api/kobo_api/clients.py`, `api/kobo_api/composer.py`, `api/kobo_api/steps.py` (HelloStep tail only), `api/kobo_api/main.py` (PATCH + regenerate routes), tests (`test_metadata.py` new, `test_composer.py`, `test_steps.py`, web tests).

**Interfaces:**
- Consumes: Tasks 1-2; the composer's `_agent_card` (line ~145) and the bundle clone/commit idiom (`update_dockerfile_runtime_image`'s callers show it).
- Produces:
  - `composer._agent_card` output gains `"metadata": {"team": spec.team, "purpose_tags": [...], "capability": ""}` (spec fields added to BundleSpec accordingly — trace who builds it).
  - `composer.update_agent_card_metadata(bundle_dir, *, team, purpose_tags, capability) -> None` — in-place JSON edit preserving all other card fields byte-faithfully (json round-trip with the same indent; assert unknown fields survive).
  - `metadata.generate_capability(model_client, persona_body, skill_descriptions, tooling_filenames) -> str` — one plain sentence, clamped ≤160 chars, plain punctuation enforced (strip em dashes defensively); deterministic fallback tier.
  - `metadata.commit_card_metadata(github, repo, *, team, purpose_tags, capability) -> sha` — clone, update card, commit "kobo: directory details updated", push (delivery chips ride the sha).
  - HelloStep tail: after `agent.first_reply = reply`, call the generation seam; store `agent.capability`; trigger the card commit; NEVER raise into the step (failures log + leave capability null; Regenerate recovers later).
  - `PATCH /api/agents/{name}/metadata` body `{team?, purpose_tags?, capability?}` — ready-gated, any signed-in user, normalizes, stores, card-commits, returns `{team, purpose_tags, capability, sha}`.
  - `POST /api/agents/{name}/metadata/regenerate` — ready-gated; re-runs generation against CURRENT skills + tooling listings; stores + card-commits; returns the same shape.

- [ ] RED matrix (generation tiers; card update preserves unknown fields; hello-tail failure isolation — a raising model client must not fail the hello step, test pins it; PATCH/regenerate matrices incl. card-commit sha in responses) → implement → GREEN → gates → vendor scan → commit "feat(api): capability is earned, and the card never drifts".

### Task 4: UI — the station, the chips, the details

**Files:** `ui/src/components/CreateModal.svelte`, `ui/src/pages/AgentDetail.svelte` (Overview block + new details modal — extract `MetadataDetails.svelte` if AgentDetail grows unwieldy; implementer's call, stated in report), `ui/src/lib/api.ts`, `ui/src/lib/queries.ts`, tests (component + factories).

**Interfaces:**
- Consumes: Task 1's `GET /api/teams`, Task 2's create fields + draft tags, Task 3's PATCH/regenerate.
- Produces: create flow's final station shows team picker (options = teams query ∪ typed escape, default "shared"), purpose chips (pre-filled from draft, add/remove, normalized display); Overview composition card displays team, purpose chips, capability line (or a plain "Not written yet. It arrives after first contact." when null); **Edit details** modal (three fields, Regenerate button beside capability with pending state, delivery chip on the returned sha); frozen matrix covers the new controls.

- [ ] RED component cases (station renders picker/chips with stubbed teams+draft; typed team escape; Overview display incl. null-capability copy; edit modal save + regenerate flows; frozen additions) → implement → GREEN → `npm test`/check/build → vendor + em-dash scans → commit "feat(ui): every agent says whose it is, what for, and what it can do".

### Task 5: Rollout + backfill (controller-driven)

- [ ] Full gates both sides; ALTERs on the deployed DB (the two columns, BEFORE the roll, old code ignores unknown columns — the proven pattern); build + roll per the untracked runbook.
- [ ] Backfill: the operator sets metadata for the two live agents through the new Edit details modal; capability via Regenerate (their skills and tooling are live, so the earned line is real). Verify each card commit landed (delivery chips) and the card JSON in each repo carries the metadata block.
- [ ] Ledger close with evidence; note for the colleagues arc: the directory layer is live, discovery can begin.
