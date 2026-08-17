# Slack Self-Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Any user connects their agent to Slack by themselves: the room hands them the manifest and instructions, they paste back App ID plus two tokens, channels stay exactly as shipped.

**Architecture:** `render_manifest` (already a clean function) gets a GET route; the connect route loses its operator gate, gains `app_id`, and records self-service provenance in a new boolean column so destruction knows which apps it can delete (room-created) versus note-and-continue (user-created). The create-app route retires; the config-token client stays for legacy deletions. Executes AFTER the destruction-completeness plan (both touch destroy.py).

**Tech Stack:** FastAPI + SQLAlchemy (SQLite), Svelte 5 + the jsdom rig.

## Global Constraints

- Tokens never stored in the room DB, never echoed in responses (existing discipline, preserve verbatim).
- REAL-GATE RULE: App ID validation is a new `SLACK_APP_ID_RE` in `kobo_api/slack_state.py` beside the channel rule — A-prefixed uppercase alnum (`^A[A-Z0-9]{6,}$`), one source, main.py imports it. The manifest route returns EXACTLY what `render_manifest` produces (shared function, never duplicated).
- Destruction distinction is by PROVENANCE FLAG, not Slack error guessing: the new `slack_app_self_service` column decides skip-with-note vs attempt-delete. (Refines the spec's "refusal class grounded" line: provenance is strictly more truthful than error-code inference; note this refinement in the spec in Task 1's commit.)
- Done-with-note copy verbatim: "The Slack app belongs to its creator. Remove it in Slack."
- No em dashes user-facing (both guards live); no vendor names (own-step scans); TDD RED-first.
- Baselines at this plan's start: API 1294 + destruction-completeness additions (read the ledger for the current count), UI 303 / check 0 / build green.

## File Structure

- `api/kobo_api/models.py` — `slack_app_self_service: Mapped[bool]` (default False) + manual-ALTER docstring note.
- `api/kobo_api/slack_state.py` — `SLACK_APP_ID_RE`.
- `api/kobo_api/main.py` — GET manifest route; connect route (gate off, app_id in, provenance flag set, create-app 409 precondition REMOVED — the app now exists before the room hears of it); `POST .../slack/app` route deleted; payload keeps `slack.app_id` and drops `slack_admin_visible`.
- `api/kobo_api/destroy.py` — `_destroy_slack` provenance branch (skip-with-note).
- `ui/src/components/SlackPanel.svelte` — the ceremony (manifest block + instructions + three fields), gate removed; `ui/src/lib/{api,queries}.ts` — manifest query, connect body, types; `AgentDetail.svelte` — `adminVisible` prop gone.
- Tests: `test_api_slack.py`, `test_web_slack_gate.py` (route-gone + degated matrices), `test_destroy.py` (note branch), `SlackPanel.test.ts`, `queries.test.ts`, `api.test.ts`, `AgentDetail.test.ts` fixtures.
- Spec touch: `docs/superpowers/specs/2026-08-17-slack-self-service-design.md` (the provenance refinement sentence).

---

### Task 1: API — manifest route, degated connect with provenance, retired create-app

**Files:**
- Modify: `api/kobo_api/models.py`, `api/kobo_api/slack_state.py`, `api/kobo_api/main.py`, the spec (one sentence)
- Test: `api/tests/test_slack_state.py`, `api/tests/test_api_slack.py`, `api/tests/test_web_slack_gate.py`

**Interfaces:**
- Consumes: `render_manifest(agent_name, persona_first_line)` (slack_setup.py:78 — read how create_slack_app currently resolves `persona_first_line` and reuse that resolution in the new GET route); `wire_slack` unchanged.
- Produces (Task 2/3 contract): `GET /api/agents/{name}/slack/manifest` → 200 `{"manifest": <dict>}` (ready-gated, no operator gate); `SlackConnectRequest` = `{app_id, bot_token, app_token}`; connect stores `slack_app_id=app_id`, `slack_app_self_service=True`; 422 copies: app id → "that doesn't look like a Slack app ID (it starts with A)", tokens unchanged; `POST .../slack/app` returns 404 (route gone); payload: `slack` block unchanged in shape, `slack_admin_visible` key REMOVED.

- [ ] **Step 1: RED matrix** (each against the files' real fixtures):

```python
# slack_state: SLACK_APP_ID_RE accepts A0123456 / rejects lowercase, C-ids, short
# manifest route: 200 + body equals render_manifest's output for the agent;
#   anonymous blocked by middleware (reuse the existing IAP matrix idiom);
#   non-operator identity 200 (no operator gate)
# connect: non-operator 200 happy path (gate gone); app_id malformed 422 with
#   the exact copy; success stores slack_app_id + slack_app_self_service True;
#   the create-app-first 409 precondition is GONE (connect works with
#   slack_app_id previously null); channels-required 409 preserved
# create-app route: 404 (deleted); its old tests removed/adjusted with the
#   original non-route assertions (manifest content checks) MOVED to the
#   manifest-route tests so nothing proven is lost
# payload: slack_admin_visible absent
```

- [ ] **Step 2: Implement** (column + docstring ALTER note `ALTER TABLE agents ADD COLUMN slack_app_self_service BOOLEAN DEFAULT 0;`; regex; routes; spec sentence). GREEN. Full suite. Ruff.

- [ ] **Step 3: Vendor scan (own step), commit** — "feat(api): the manifest comes to you"

---

### Task 2: Destruction — the provenance note

**Files:**
- Modify: `api/kobo_api/destroy.py` (`_destroy_slack`)
- Test: `api/tests/test_destroy.py`

**Interfaces:**
- Consumes: `agent.slack_app_self_service`; the existing SlackClient.delete_app path for legacy apps.
- Produces: self-service + app id present → NO delete attempt, step done, detail note verbatim "The Slack app belongs to its creator. Remove it in Slack." (recorded the same way other step details/logs are — read how existing steps record their result strings); legacy path byte-identical.

- [ ] RED: three cases — self-service app skips delete (fake Slack sees zero calls) and completes with the note; legacy app still deletes; self-service with NO app id keeps the existing no-id-done tolerance. Then implement, GREEN, full suite, ruff, vendor scan (own step), commit — "feat(api): destruction tips its hat to apps it does not own", push origin main.

---

### Task 3: UI — the ceremony

**Files:**
- Modify: `ui/src/components/SlackPanel.svelte`, `ui/src/lib/api.ts`, `ui/src/lib/queries.ts`, `ui/src/pages/AgentDetail.svelte`
- Test: `ui/src/components/SlackPanel.test.ts`, `ui/src/lib/queries.test.ts`, `ui/src/lib/api.test.ts`, `ui/src/pages/AgentDetail.test.ts`

**Interfaces:**
- Consumes: Task 1's routes/payload.
- Produces: SlackPanel props `{agentName, slack, frozen}` (adminVisible GONE); a `slackManifestQuery(name)` fetched lazily when the user opens the manifest block (not on mount — the setup section renders for everyone now, and most visits are not setup visits).

- [ ] **Step 1: RED component cases**: setup section renders for a NON-admin mount (no gate); "Show the manifest" reveals a copyable block whose content came from the manifest query (stub it); the numbered instructions render (spot-assert two lines verbatim from the spec's ceremony section); three fields (App ID first) + Finish setup with existing disabled/validation idioms; app-id client-side format hint matches the server copy; frozen matrix still disables everything (update the CASES entry: adminVisible prop gone).
- [ ] **Step 2: Implement** — keep the create-app button/flow REMOVED (the ceremony replaces it), keep the tokens-cleared-on-failure behavior, keep the delivery chip. AgentDetail drops the adminVisible pass-through; fixtures drop `slack_admin_visible`. GREEN; `npm test` / `check` (0) / `build`; grep ui/src for `slack_admin_visible` → zero.
- [ ] **Step 3: Vendor scan (own step), em-dash scan of added lines, commit** — "feat(ui): anyone can walk the slack ceremony now", push origin main.

---

### Task 4: Rollout + live acceptance

- [ ] Full gates both sides; counts to the ledger.
- [ ] Deployed-DB ALTER (before the roll, same discipline as the channels rollout): `ALTER TABLE agents ADD COLUMN slack_app_self_service BOOLEAN DEFAULT 0;` — legacy rows (network) stay 0 = room-created, correct by construction.
- [ ] Build + roll per the untracked runbook (carries the destruction-completeness plan's code too).
- [ ] Live acceptance: the operator walks the ceremony on `ward` AS A USER — manifest out, app created in Slack's UI, tokens + App ID pasted, channels already saved, Finish setup, the agent answers in every listed channel. Ledger every observation. (The destroy-with-note proof lands with the next scratch-agent destroy, alongside the destroy-access live check.)
