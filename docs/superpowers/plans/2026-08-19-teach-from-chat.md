# Teach From Chat Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Task 3 is controller-driven.

**Goal:** An operator distills a chat moment into a permanent agent memory with one explicit gesture, riding the intake pipeline end to end.

**Architecture:** One thin API route formats an attributed note and hands it to the existing `upload_intake_file` seam. One UI round adds the per-bubble gesture and compose modal to the chat panel. Delivery, ledgering, and ingestion are all pre-existing.

**Tech Stack:** FastAPI + the existing intake seam; Svelte 5 + the jsdom rig.

## Global Constraints

- NO em dashes in user-facing copy (both guards enforce their scopes).
- No vendor/client names in tracked files; vendor scan is its OWN pre-commit step per task.
- Explicit gesture only: nothing is captured without the operator clicking Teach and Save.
- TDD RED-first. Baselines: API 1648, UI 355. Runners: `venv/bin/python -m pytest -q` from api/, `npm test` from ui/.

## File Structure

- `api/kobo_api/main.py` — `POST /api/agents/{name}/teach`.
- `api/kobo_api/teach.py` (new, tiny) — note formatting (header + slug + filename) so it is unit-testable without the web layer.
- `ui/src/components/ChatPanel.svelte` — per-bubble affordance; `ui/src/components/TeachModal.svelte` (new); `ui/src/lib/api.ts` / `queries.ts`.
- Tests: `api/tests/test_teach.py` (new) + web tests; `ui/src/components/TeachModal.test.ts` (new) + ChatPanel tests.

---

### Task 1: The teach route

**Files:** `api/kobo_api/teach.py` (new), `api/kobo_api/main.py`, tests.

**Interfaces:**
- Consumes: `upload_intake_file(github, repo, filename, content) -> sha` (existing; read its validation limits first and stay inside them); the ready-gate idiom the metadata routes use; whatever signed-in-user identity the request carries (trace the middleware; if an email/user is available use it, else the header line omits it gracefully).
- Produces:
  - `teach.format_note(text, *, taught_by: str | None, on_date: str) -> str`: a markdown note whose first lines attribute it ("Taught in chat" + date + optional user), body = the operator's text verbatim. Plain punctuation only.
  - `teach.note_filename(text, *, on_date: str) -> str`: `chat-<YYYYMMDD>-<slug>.md`, slug from the first few words (lowercase, hyphens, bounded length), collision-tolerant by including enough of the date/slug that the intake seam's own semantics handle re-teaching (same name + new content = new ingestion, per the intake door's design).
  - `POST /api/agents/{name}/teach` body `{"text": str}`: ready-gated, any signed-in user; 422 plain copy on empty/whitespace or oversized text (mirror intake's caps); formats, uploads via the intake seam, returns `{"filename", "sha"}`.

- [ ] RED: formatter matrix (header shape with and without user; date embedding; verbatim body; no em dash), filename matrix (slugging, bounds, weird input), route matrix (gates, 422s, response shape, intake seam called with the formatted artifacts).
- [ ] Implement; GREEN; full suite; ruff; vendor scan (own step); commit "feat(api): a chat moment becomes a taught memory".

### Task 2: The gesture and the compose box

**Files:** `ui/src/components/ChatPanel.svelte`, `ui/src/components/TeachModal.svelte` (new), `ui/src/lib/api.ts`, `ui/src/lib/queries.ts`, tests.

**Interfaces:**
- Consumes: Task 1's route; the transcript already held in ChatPanel state (each entry `{role, text}`); the modal idioms of the existing modals (MetadataDetails is the model).
- Produces: a quiet "Teach this" action on each bubble (both roles); clicking pre-fills the modal with the exchange pair (the clicked message plus its counterpart reply/question when adjacent) as editable text; Save posts `{text}` to the teach route with pending state; success shows exactly "Saved to memory. It arrives with the next delivery."; failure shows plain retryable copy; modal resets between uses; the affordance respects the frozen/pre-ready states the chat panel already honors.

- [ ] RED: affordance renders per bubble; prefill pairs correctly (user-then-agent, agent-first edge, single message); edited text is what posts; success and error copy exact; reset between opens; frozen matrix.
- [ ] Implement; GREEN; `npm test` + check + build; both copy guards; vendor scan (own step); commit "feat(ui): teach this, says the operator, and the agent remembers".

### Task 3: Rollout + acceptance (controller-driven)

- [ ] Build + roll the room; teach a real fact to a live agent (API-equivalent of the gesture); verify the intake commit lands, delivery completes (CI + roll), and the memory answers from inside the agent afterward (in-pod search for the taught fact, then a chat question it can only answer from the taught note).
- [ ] Ledger close; the operator exercises the UI gesture end to end at leisure.
