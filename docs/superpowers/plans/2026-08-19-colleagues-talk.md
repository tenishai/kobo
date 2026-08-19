# Colleagues Talk Implementation Plan (spec B)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Task 5 is controller-driven operations with evidence gates.

**Goal:** Every room agent can list its colleagues (with the directory metadata) and ask one a question through the switchboard; humans watch those exchanges live in the Chat tab.

**Architecture:** The switchboard grows a `colleague` scope (directory read + dispatch re-mint, nothing else) and a directory endpoint. The runtime grows a colleagues tool provider. The room mints one pass per agent, delivers it on the slack-token rails, composes the tools block, and proxies the switchboard's conversation surface for the UI.

**Tech Stack:** Three repos. Switchboard: FastAPI + SQLAlchemy (its own test suite and idioms). Runtime: the daedalus tool-provider pattern. Room: FastAPI + Svelte 5, suites at API 1577 / UI 337.

## Global Constraints

- NO em dashes in user-facing copy anywhere (room UI copy, API detail strings, model-visible tool-result text in the runtime provider).
- No vendor/client names in ANY tracked file in ANY of the three repos; vendor scan (`grep -i -f <kobo>/.superpowers/sdd/vendor-names.txt` over touched files) is its OWN pre-commit step per task, in every repo.
- The colleague scope must NOT open any admin route (pinned by test: a colleague token against an admin route gets 403).
- Colleague pass mint shape: `POST /admin/token/mint-service` body `{"sub": "agent:<name>", "scope": ["colleague"], "aud": "cabal-core", "ttl_days": 365}`.
- Task envelope dialect (already encoded in the room's builder, reuse the convention): `status: {state: "submitted", message: <current message>}`, `history` = prior turns; replies read from `status.message.parts[0].text`.
- The room's operator token env is `CABAL_OPERATOR_TOKEN`; the agent pass env is `CABAL_COLLEAGUE_TOKEN`; neither is ever logged, echoed, or sent to the browser.
- TDD RED-first in every repo. Room runners: `venv/bin/python -m pytest -q` from api/, `npm test` from ui/.

## File Structure

- Switchboard repo: `src/cabal/core/routes/admin.py` (guard consolidation), new `src/cabal/core/routes/directory.py`, `src/cabal/core/routes/tasks.py` (re-mint branch), route registration in `core/app.py`, tests beside the existing suites.
- Runtime repo: new `src/daedalus/tools/colleagues.py`, config parsing where the tools block lives (`bundle.py`), registration beside the existing providers, tests.
- Room repo: `api/kobo_api/clients.py` (`CabalClient.mint_colleague`), `api/kobo_api/colleagues.py` (new: delivery seam), `api/kobo_api/composer.py` (tools block + in-place update helper), `api/kobo_api/steps.py` (WireStep additions), `api/kobo_api/main.py` (proxy routes), `ui/src/components/ColleagueConversations.svelte` (new) wired into the Chat tab, `ui/src/lib/api.ts`/`queries.ts`, tests throughout.

---

### Task 1: The colleague scope + the directory (switchboard repo)

**Files:** `src/cabal/core/routes/admin.py`, new `src/cabal/core/routes/directory.py`, `src/cabal/core/routes/tasks.py`, `src/cabal/core/app.py` (router include), tests.

**Interfaces:**
- Consumes (verified in source): `_require_operator_claims` (admin.py:41-52) and `_require_secrets_fetch` (admin.py:815-843) are the two copy-pasted guards; the dispatch re-mint gate lives in `tasks.py:_resolve_dispatch_token` (the scope check around lines 88-93); `bulletin_store.list_all(session)` returns rows with `name, status, kind, agent_card_json`.
- Produces:
  - `_require_scope(request, raw_token, *, audiences: set[str], any_of: set[str]) -> claims` in admin.py (or a shared module if the suite's layout prefers); `_require_operator_claims` and `_require_secrets_fetch` become thin wrappers with EXACTLY today's behavior (parity pinned by the existing tests passing unmodified).
  - `GET /directory` (new router): guard `audiences={"cabal-core"}, any_of={"operator", "colleague"}`; serializes ACTIVE rows only as `{"agents": [{"name", "team", "purpose_tags", "capability", "description"}]}`, reading the card's metadata block tolerantly: `card = agent_card_json.get("card", agent_card_json)`, `meta = card.get("metadata", {}) or {}`, missing fields become `""`/`[]`. Excludes nothing else (the built-in echo appears; that is fine and useful).
  - `tasks.py` re-mint branch: `if "operator" not in scope and "colleague" not in scope: 403` (message updated to name both), re-mint unchanged otherwise (sub passthrough preserved so `agent:<name>` lands in caller_agent). Do NOT touch `a2a_jsonrpc.py` (its audience asymmetry is documented upstream, out of scope).

- [ ] RED: guard parity (existing operator/secrets tests untouched and green); directory auth matrix (no token 401, dispatch-scoped 403, colleague 200, operator 200, wrong aud 403); serialization tolerance (bare card, jws-wrapped, missing metadata); ACTIVE-only filter; colleague re-mint dispatch path succeeds against a fake target; colleague token vs THREE admin routes (agents list, conversations, mint-service) all 403.
- [ ] Implement; GREEN (full switchboard suite, report the count); the repo's own lint if configured; vendor scan (own step); commit on a NEW branch `feat/colleague-scope` off main: "feat: the colleague scope, a directory for agents, nothing more".

### Task 2: The colleagues tool provider (runtime repo)

**Files:** new `src/daedalus/tools/colleagues.py`, the tools-config parse site in `src/daedalus/bundle.py`, provider registration (trace how the existing providers register; mirror it), tests.

**Interfaces:**
- Consumes: the existing provider pattern (`tools/limenal.py` is the model, including its `token_env` soft-degrade idiom: absent env -> a placeholder that fails politely at call time, never at boot); agent.yaml `tools:` block shape (VERIFY whether provider is singular or a list and report; the room's agents use none today, so claiming the slot is acceptable either way).
- Produces:
  - agent.yaml shape: `tools: {provider: colleagues, colleagues: {url: <switchboard base>, token_env: CABAL_COLLEAGUE_TOKEN, self: <agent name>}}`.
  - Tool `list_colleagues()` -> GET `{url}/directory` with the bearer; returns model-readable text listing each colleague EXCEPT `self`: name, team, purpose tags, capability. Empty directory or lone self -> "No colleagues are listed yet."
  - Tool `ask_colleague(name, question)` -> POST `{url}/a2a/tasks` `{"target": name, "task": <envelope>}` with the bearer; envelope per the dialect in Global Constraints (id prefix `ask-`); returns the reply text from `status.message.parts[0].text`. Errors (HTTP failure, missing reply, asking self, unknown name per a 404-ish response) return plain explanatory text as the tool result, never raise out of the provider. Timeout generous (180s).
  - Token env absent -> both tools return "The colleague pass is not configured." (soft, boot unaffected).

- [ ] RED: registration + config parse; both tools' happy paths (respx/httpx-mock per the repo's idiom); self-exclusion; empty directory; each error path's text (no em dashes); token-absent degrade; envelope shape pinned.
- [ ] Implement; GREEN (full runtime suite, report count); lint; vendor scan (own step); commit on branch `feat/colleagues-tools`: "feat(tools): agents can find and ask their colleagues".

### Task 3: Mint, deliver, compose (room repo, api only)

**Files:** `api/kobo_api/clients.py`, new `api/kobo_api/colleagues.py`, `api/kobo_api/composer.py`, `api/kobo_api/steps.py`, tests.

**Interfaces:**
- Consumes: `CabalClient` idioms; `KubectlClient.apply_secret(namespace, name, literals)` (generic, verified); the `secrets_from` append idiom (`slack_setup.py:_update_gojira_secrets_from` is the model, but write the colleague sibling generically named for its purpose); `gojira.reprovision` + `wait_active`; composer's agent.yaml emission + the `update_agent_yaml_auth` in-place-edit idiom.
- Produces:
  - `CabalClient.mint_colleague(name) -> str` (the mint shape from Global Constraints; raises CabalError on failure; token never logged).
  - `composer`: when `cabal_url` is configured, composed agent.yaml carries the tools block (exact shape from Task 2, `self` = agent name); byte-identical output when not configured (pin). `update_agent_yaml_tools(bundle_dir, *, url, token_env, self_name)` in-place helper for the backfill, idempotent, preserving all other lines (same discipline as `update_agent_yaml_auth`).
  - `colleagues.deliver_pass(kubectl, cabal, agent) -> None`: mint + `apply_secret(agent.name, f"{agent.name}-cabal", {"CABAL_COLLEAGUE_TOKEN": token})`. Idempotent by construction (apply_secret upserts; re-mint on re-run is acceptable and noted).
  - `WireStep`: when cabal is enabled, additionally appends `f"{name}-cabal"` to `secrets_from` (generic append helper, reuse or extract the slack one) and calls `deliver_pass` BEFORE the gojira.yaml push (so the single existing reprovision carries it). Disabled = byte-identical WireStep behavior (pin).
  - No new destroy stage (namespace deletion kills the secret; directory retire exists; pass expiry accepted).

- [ ] RED: mint request shape + error; deliver_pass secret contents + idempotence; composer matrix (on/off byte-identical, tools block exact, update helper add/replace/idempotent/preserves); WireStep matrix (enabled adds secrets_from + delivers before push; disabled untouched); token absent from every log/response (pin like the operator token).
- [ ] Implement; GREEN; full api suite; ruff; vendor scan (own step); commit "feat(api): every agent is born with a colleague pass".

### Task 4: The watching window (room repo, api proxy + ui)

**Files:** `api/kobo_api/main.py` (+`clients.py` if the proxy needs client methods), `ui/src/components/ColleagueConversations.svelte` (new), the Chat tab composition in `ui/src/pages/AgentDetail.svelte` or its chat panel component (trace where the chat panel lives; the conversations panel joins it, agent-agnostic room-wide view), `ui/src/lib/api.ts`, `ui/src/lib/queries.ts`, tests both sides.

**Interfaces:**
- Consumes: switchboard admin surface (verified shapes): `GET /admin/conversations?limit&state&target` -> `{conversations: [{task_id, target_agent, caller_agent, state, created_at, updated_at, result_snippet, ...}]}`; `GET /admin/conversations/{task_id}` -> full result + events; `GET /admin/events/stream` SSE (`hello` frame then `dispatch_start|dispatch_success|dispatch_failure`). Room-side: `CABAL_OPERATOR_TOKEN` + `cabal_url`, `cabal_enabled`.
- Produces:
  - `GET /api/colleagues/conversations?all=` : signed-in users; 409 plain when disabled; proxies the switchboard list; DEFAULT filters to `caller_agent` starting with `agent:`; `all=true` returns everything. Response passes through the switchboard fields minus nothing sensitive (there is nothing sensitive; the operator token itself obviously never appears).
  - `GET /api/colleagues/conversations/{task_id}` : same gating, full detail + events.
  - `GET /api/colleagues/stream` : SSE passthrough (httpx streaming -> StreamingResponse), forwarding event frames as received; client reconnects handle themselves (no replay upstream, mirror that honestly).
  - UI: `ColleagueConversations.svelte` in the Chat tab: list (asker -> target, state chip, snippet, relative time), expand for the full exchange, live updates by refetching the list on any SSE frame (simple and correct; no client-side event merging), the agent-only/everything toggle. Empty state copy: "No colleague conversations yet. They start talking once an agent asks for help." (plain, no em dash).

- [ ] RED: proxy matrix (disabled 409, filter default vs all, detail passthrough, SSE content-type + frame passthrough, token absent from responses/headers to the browser); UI component (render list from fixture, toggle, expand detail, SSE-triggered refetch via mocked EventSource, empty state).
- [ ] Implement; GREEN both suites; check + build; both copy guards; vendor scan (own step); commit "feat: the room watches its agents talk".

### Task 5: Rollout + acceptance (controller-driven)

- [ ] Switchboard image: local integration state = main + the two deployed patches + `feat/colleague-scope`; build/push `cabal:<sha7>-c1`; roll the deployment; verify directory 200 with a freshly minted colleague token and 403 on an admin route with the same token.
- [ ] Room: build/push/roll `kobo:<sha7>`; inert-path verification (chat + UI unaffected).
- [ ] Runtime image: `feat/colleagues-tools` + techne v0.14.0 -> `daedalus-runtime:<sha7>-t0140`; push; pull-verify.
- [ ] Backfill per live agent, ONE commit each (pod-exec script per the locks pattern): mint + apply secret FIRST, then commit FROM bump + tools block (`update_agent_yaml_tools`) + `secrets_from` append; reprovision (secrets_from changed); CI + reprovision roll; wait ready.
- [ ] Org `runtime_image` bump (App-token PUT) so future scaffolds are born current; room restart NOT required (compose reads the merged config at request time via RepoStep; verify by tracing, else restart).
- [ ] Acceptance: room-chat one agent into `list_colleagues` (the reply names the other agent with team/purpose/capability); room-chat it into `ask_colleague` with a real question; the answer arrives in chat; the exchange shows in the conversations panel attributed `agent:<asker>` with live update observed; colleague token 403 on `/admin/agents` re-verified against the live service.
- [ ] Upstream PRs: switchboard `feat/colleague-scope` PR (references the deployed evidence); runtime `feat/colleagues-tools` PR. Ledger close + memory update; note the follow-ups (pass expiry/rotation someday, provider composition if agents ever need two tool providers).
