# Create Identity Polish + MCP Currency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Three persona-resonant name suggestions with real variety, a generated face during provisioning, and an MCP surface that tells the truth and covers learnings.

**Architecture:** All kobo. Naming: the prompt mines the persona (server-side `persona_id` body lookup), count drops to 3, an exclude list + round salt kill fallback repeats. Face: a best-effort `AvatarStep` after `RepoStep` reuses the existing generate/store lanes with an enriched shared prompt. MCP: doc-count fixes, `kobo_get_agent` field alignment, and a learnings tool trio that calls the existing route logic.

**Tech Stack:** FastAPI/SQLAlchemy (api), Vertex clients (name gemini-2.5-flash, image gemini-2.5-flash-image), Svelte 5 + svelte-query v6 (ui), FastMCP-style tool modules (mcp/tools).

**Spec:** `docs/superpowers/specs/2026-09-02-create-identity-and-mcp-currency-design.md`

## Global Constraints

- No client name in source, comments, or string literals; NO EM DASHES in kobo content (use "--"; copy.test.ts enforces .svelte).
- Exact values: `SUGGESTION_COUNT = 3`; `exclude` cap 20; `round` 0-20; request `what_they_do` max 2000; persona body clipped to first 2000 chars server-side; avatar caps unchanged (4 MiB generated, 2 MiB upload).
- AvatarStep: SKIPPED on any failure, never FAILED; idempotent (skips when `agent.avatar_png` already set); never blocks READY.
- API tests from `api/`: `uv run python -m pytest tests/<file> -x -q` (full: `uv run python -m pytest tests/ -q`; if the venv's `.pth` files go macOS-hidden again, `chflags nohidden .venv/lib/python3.12/site-packages/*.pth`). UI from `ui/`: `npx vitest run <file>`, full suite + `npm run check`.
- Branch: `feat/create-identity` from kobo `main`.
- This week's shipped behavior (learnings, tabs, skills reconcile) untouched.

Reference anchors (drift expected; locate by content): namegen.py `SUGGESTION_COUNT`:20, fallback pool:28-32, `_deterministic_order`:51-61, `finalize_suggestions`:94-155; clients.py `_NAME_PROMPT_TEMPLATE`:2156-2166, `VertexNameClient.suggest`:2355-2426; main.py `NameSuggestionsRequest`:244-252, name-suggestions route:2525-2571, avatar generate route:5504-5572 (persona_description lookup 5527-5534, prompt call 5536); avatars.py `avatar_prompt`:23-39, `set_avatar`:42-63; provisioner.py `PIPELINE`:385-394; ui CreateModal naming station effect:288-303, thin-signal substitution comment:255-262, name cards:764-786, offer-more:331-341, rerollNote:266-270; queries.ts `nameSuggestionsQuery`:405-416; mcp read.py `kobo_get_agent`:146-177; write.py counts:1-9,518,61-63, create_agent docstring:553-562.

---

### Task 1: namegen -- three, excludable, rotating

**Files:**
- Modify: `api/kobo_api/namegen.py`
- Test: `api/tests/test_namegen.py` (read first; adapt existing pins)

**Interfaces:**
- Produces: `SUGGESTION_COUNT = 3`; `finalize_suggestions(candidates, *, taken, reserved, persona_seed, exclude: set[str] | frozenset[str] = frozenset(), round_salt: str = "0") -> list[dict]`; `_deterministic_order(names, persona_seed, round_salt)` hashing `sha256(f"{persona_seed}:{round_salt}:{name}")`. Task 3 passes `exclude`/`round_salt` through from the route.

- [ ] **Step 1: Create the branch**

```bash
git -C . checkout main
git -C . pull --ff-only
git -C . checkout -b feat/create-identity
```

- [ ] **Step 2: Read `api/tests/test_namegen.py` and `api/kobo_api/namegen.py` in full.** Existing tests pin count 5 and deterministic ordering -- they will be updated, not weakened: every behavioral pin (validation, taken/reserved dedupe, padding, determinism-within-a-round) survives with the new count/params.

- [ ] **Step 3: Write the failing tests** (update pins to 3; add these):

```python
def test_exclude_drops_offered_names_from_model_and_fallback():
    candidates = [{"name": "ember", "reason": "warm"}, {"name": "cedar", "reason": "steady"}]
    out = finalize_suggestions(
        candidates, taken=set(), reserved=set(), persona_seed="p",
        exclude={"ember", "cedar"},
    )
    names = {s["name"] for s in out}
    assert names.isdisjoint({"ember", "cedar"})
    assert len(out) == SUGGESTION_COUNT


def test_round_salt_rotates_the_fallback_order():
    first = finalize_suggestions([], taken=set(), reserved=set(),
                                 persona_seed="p", round_salt="0")
    second = finalize_suggestions([], taken=set(), reserved=set(),
                                  persona_seed="p", round_salt="1")
    assert [s["name"] for s in first] != [s["name"] for s in second]


def test_same_round_stays_deterministic():
    a = finalize_suggestions([], taken=set(), reserved=set(),
                             persona_seed="p", round_salt="2")
    b = finalize_suggestions([], taken=set(), reserved=set(),
                             persona_seed="p", round_salt="2")
    assert a == b
```

- [ ] **Step 4: Verify failure** -- `cd api && uv run python -m pytest tests/test_namegen.py -x -q` -- FAIL (unexpected kwargs / count 5).

- [ ] **Step 5: Implement** -- `SUGGESTION_COUNT = 3`; thread `exclude` (normalized lowercase) into the same drop-set as taken/reserved; `_deterministic_order(names, persona_seed, round_salt)` with the salted hash; `finalize_suggestions` signature per Interfaces (keyword-only, defaults keep every existing caller working).

- [ ] **Step 6: Verify pass, run the file, commit**

```bash
git -C . add api/kobo_api/namegen.py api/tests/test_namegen.py
git -C . commit -m "feat(namegen): three suggestions, exclude set, round-salted fallback"
```

---

### Task 2: the persona-mining prompt (clients.py)

**Files:**
- Modify: `api/kobo_api/clients.py` (`_NAME_PROMPT_TEMPLATE` ~2156, `VertexNameClient.suggest` ~2355)
- Test: the existing VertexNameClient tests (grep `_NAME_PROMPT_TEMPLATE\|VertexNameClient` in api/tests/ -- read that file first)

**Interfaces:**
- Produces: `VertexNameClient.suggest(who: str, what: str, *, tags: list[str] | None = None, exclude: list[str] | None = None) -> list[dict]`. Task 3 calls it with all four.

- [ ] **Step 1: Read the existing client tests** and how they pin the prompt/params (fake transport? recorded body?). Update pins, never weaken: temperature 1.0, thinking off, maxOutputTokens 512 all stay pinned.

- [ ] **Step 2: Write the failing tests** (adapted to that harness):

```python
def test_prompt_mines_the_persona_and_asks_for_three():
    # invoke suggest(who="a batting coach modeled on a famous slugger",
    #   what="reviews swing mechanics...", tags=["baseball", "coaching"],
    #   exclude=["ember"]) against the fake transport; capture the prompt.
    assert "exactly three" in prompt
    assert "RESONATE" in prompt
    assert "baseball, coaching" in prompt
    assert "ember" in prompt          # exclude list rendered
    assert "craft workshop" not in prompt  # old theme gone
    assert "quiet trades" not in prompt


def test_empty_tags_and_exclude_render_cleanly():
    # tags=None, exclude=None -> prompt says "Its themes: (none)" and
    # "never any of: (none offered yet)" -- no dangling braces, no "None".
```

- [ ] **Step 3: Verify failure**, then **implement**: replace `_NAME_PROMPT_TEMPLATE` with the spec's template verbatim (spec section 1, "Cause 2" block), rendering `{tags}` as comma-joined or `(none)`, `{exclude}` as comma-joined or `(none offered yet)`; parse/validate exactly three objects (the existing five-object parse logic becomes three -- keep its strict-JSON handling and error paths).

- [ ] **Step 4: Run the client test file + commit**

```bash
git -C . add api/kobo_api/clients.py api/tests/
git -C . commit -m "feat(names): the prompt mines the persona -- three resonant candidates"
```

---

### Task 3: route + request contract (persona_id lookup)

**Files:**
- Modify: `api/kobo_api/main.py` (`NameSuggestionsRequest` ~244, route ~2525)
- Test: the existing name-suggestions route tests (grep `name-suggestions` in api/tests/)

**Interfaces:**
- Consumes: Tasks 1-2 signatures.
- Produces: `NameSuggestionsRequest` gains `persona_id: str | None = None`, `exclude: list[str] = []` (max_length 20), `round: int = 0` (ge 0, le 20); `what_they_do` max_length 500 -> 2000. Route behavior: when `persona_id` is set and resolves, the server uses the persona's REAL body (first 2000 chars) + its `purpose_tags`, ignoring client `what_they_do`; unresolved `persona_id` falls back to the client fields (never 404s -- naming must not break on a stale catalog). `exclude` is lowercased, unioned into the drop-set, passed to both `suggest(...)` and `finalize_suggestions(...)`; `round` becomes `round_salt=str(payload.round)`. `persona_seed` for fallback ordering stays `f"{who}\n{what}"` computed AFTER server-side resolution.

- [ ] **Step 1: Find the persona-body fetch.** The catalog FTS row carries only the label/description; the FULL `what_they_do` lives where the composer materializes personas from. Read `api/kobo_api/personas.py` and the composer/steps persona lane (grep `what_they_do` in api/kobo_api/) and identify the existing helper that returns a persona's full fields by id (the same source `PersonaFields` validates). Use THAT helper -- do not invent a new fetch. If the only lane is bucket-backed, wrap it in try/except with the client-fields fallback.

- [ ] **Step 2: Write the failing route tests** (mirror the existing route-test harness):

```python
def test_persona_id_lookup_feeds_the_real_body_and_tags():
    # seed/stub a persona whose body contains a distinctive token; POST
    # with persona_id + thin client fields; assert the fake name client
    # received the body token and the persona's tags, not the thin label.


def test_unresolved_persona_id_falls_back_to_client_fields():
    # persona_id="ghost" -> 200, suggestions still produced from
    # who/what in the payload.


def test_exclude_and_round_reach_the_generator_and_finalizer():
    # POST twice with the same persona, second time exclude=first names,
    # round=1 -> no overlap with the first response's names.


def test_returns_exactly_three():
    ...
```

- [ ] **Step 3: Verify failure, implement, verify pass** (route file + namegen/client tests all green), **run the full API suite**, commit:

```bash
git -C . add api/kobo_api/main.py api/tests/
git -C . commit -m "feat(names): persona_id server lookup, exclude + round ride the route"
```

---

### Task 4: UI naming station -- three cards, honest Offer more

**Files:**
- Modify: `ui/src/lib/api.ts` (suggestions fetch fn + request type), `ui/src/lib/queries.ts` (`nameSuggestionsQuery` ~405-416), `ui/src/components/CreateModal.svelte` (naming station)
- Test: `ui/src/components/CreateModal.test.ts` (read first)

**Interfaces:**
- Consumes: Task 3's contract.
- Produces: `nameSuggestionsQuery(params)` keyed on `{who_they_are, what_they_do, persona_id, exclude, round}`; the modal accumulates offered names into `exclude` and bumps `round` on each "Offer more".

- [ ] **Step 1: Read the naming-station code and tests.** Key facts to honor: the query fires on persona commit via the `$effect` (~288-303); catalog picks currently send description+label (the disclosed thin-signal substitution comment ~255-262 -- REWRITE that comment: catalog picks now send `persona_id` and the server does the lookup; the builder lane sends the drafted body's first 2000 chars as `what_they_do`); prefetch (~241-245) must carry the same params as the live query or it warms a dead key.

- [ ] **Step 2: Write failing tests** (adapt to the file's harness):

```ts
it("renders exactly three name cards", async () => { ... });

it("offer more excludes every name already shown and bumps the round", async () => {
  // stub two responses; click Offer more; assert the second fetch body
  // carries exclude=[first three names] and round=1.
});

it("catalog picks send persona_id instead of the label-as-what", async () => {
  // pick a persona; assert the suggestions POST body has persona_id and
  // does NOT pass the persona label as what_they_do.
});
```

- [ ] **Step 3: Implement**: api.ts request type + body; queries.ts params in the key; modal state -- `offeredNames: string[]` accumulated from every response, `offerRound` counter, both reset when the persona changes (the existing effect already resets naming state -- extend it); "Offer more" bumps round instead of bare `refetch()`; the `rerollNote` "Same five as before" copy becomes count-agnostic and only appears if the response genuinely equals the current set. Three radio cards.

- [ ] **Step 4: Run the modal tests, full UI suite, `npm run check`**, commit:

```bash
git -C . add ui/src
git -C . commit -m "feat(ui): three persona-driven name cards, offer-more that offers more"
```

---

### Task 5: enriched avatar prompt (shared)

**Files:**
- Modify: `api/kobo_api/avatars.py` (`avatar_prompt` ~23-39), `api/kobo_api/main.py` (generate route call site ~5536)
- Test: `api/tests/test_avatars.py` (or wherever avatar_prompt is pinned -- grep first)

**Interfaces:**
- Produces: `avatar_prompt(agent_name: str, persona_description: str, purpose_tags: list[str] | None = None) -> str` per the spec's template (section 2). Task 6 calls it with all three.

- [ ] **Step 1: Write the failing test**

```python
def test_avatar_prompt_carries_name_flavor_and_tags():
    prompt = avatar_prompt("wren", "a careful billing auditor", ["billing", "audits"])
    assert 'feel like a "wren"' in prompt
    assert "billing, audits" in prompt
    assert "no text in image" in prompt


def test_avatar_prompt_without_tags_stays_clean():
    prompt = avatar_prompt("wren", "a careful auditor")
    assert "themes:" not in prompt or "(none)" not in prompt  # tags clause simply omitted
```

- [ ] **Step 2: Implement** the spec template (omit the `themes:` clause entirely when tags are empty), update the generate route to pass `agent.purpose_tags` (the column already holds the list the detail serves). Verify pass, run the file + route tests, commit:

```bash
git -C . add api/kobo_api/avatars.py api/kobo_api/main.py api/tests/
git -C . commit -m "feat(avatars): the prompt carries name flavor and purpose tags"
```

---

### Task 6: AvatarStep -- a face at birth

**Files:**
- Modify: `api/kobo_api/steps.py` (new `AvatarStep`), `api/kobo_api/provisioner.py` (`PIPELINE` ~385-394)
- Test: `api/tests/test_steps.py` or the steps-test file the repo uses (grep `class RepoStep` usages in tests; read the harness first)

**Interfaces:**
- Consumes: Task 5's `avatar_prompt`; existing `clients.image.generate(prompt) -> bytes` lane (read the generate route ~5504-5572 for the exact client call + 4 MiB check + `set_avatar` + `Agent.avatar_png` caching -- the step mirrors that sequence exactly); the persona-description lookup the route uses (~5527-5534).
- Produces: `AvatarStep` with `name = "avatar"`, checklist label "Paint the portrait", slotted in PIPELINE immediately after `RepoStep()`.

- [ ] **Step 1: Read a small existing step** (e.g. `DirectoryStep` or `HelloStep`) for the Step contract (`name`, `label`, `run(ctx)` signature, how SKIPPED is signaled -- find one that skips, or how StepStatus.SKIPPED is produced by the engine) and the steps-test harness.

- [ ] **Step 2: Write the failing tests**

```python
def test_avatar_step_generates_and_stores_the_portrait():
    # fake image client returns valid PNG bytes; run the step; assert
    # set_avatar-equivalent repo write happened (capturing github fake)
    # and agent.avatar_png is cached (base64 of the bytes).


def test_avatar_step_skips_when_image_client_missing():
    # clients.image is None -> step result SKIPPED, agent proceeds.


def test_avatar_step_skips_on_generation_error():
    # image client raises -> SKIPPED with a note, never FAILED.


def test_avatar_step_skips_when_avatar_already_present():
    # agent.avatar_png pre-set (retry path) -> SKIPPED, no client call.


def test_pipeline_orders_avatar_right_after_repo():
    from kobo_api.provisioner import PIPELINE
    names = [step.name for step in PIPELINE]
    assert names.index("avatar") == names.index("repo") + 1
```

(Adapt the first four to the harness's fake-clients/session fixtures; the pipeline-order test's step-name attribute values come from Step 1's reading -- use the real attribute, e.g. `RepoStep().name`.)

- [ ] **Step 3: Implement** `AvatarStep.run`: persona description via the same lookup the route uses (fallback `f"A {agent.persona} agent."`); prompt via `avatar_prompt(agent.name, description, agent.purpose_tags)`; generate; enforce the 4 MiB cap; `set_avatar(...)`; cache `agent.avatar_png`; commit the session. EVERY failure path (no client, exception, oversize) -> the harness's skip signal with a one-line reason; pre-existing `avatar_png` -> skip before any client call. Insert into PIPELINE after `RepoStep()`.

- [ ] **Step 4: Run the steps tests + the FULL API suite** (provisioner tests must still pass with the longer pipeline -- fix any test that pins the step list or counts steps, honestly), commit:

```bash
git -C . add api/kobo_api/steps.py api/kobo_api/provisioner.py api/tests/
git -C . commit -m "feat(provisioner): AvatarStep -- every agent gets a face at birth, best-effort"
```

---

### Task 7: MCP doc fixes + kobo_get_agent alignment

**Files:**
- Modify: `api/kobo_api/mcp/server.py` (~225-227), `api/kobo_api/mcp/tools/write.py` (intro 1-9, count ~518, table ~61-63, create_agent docstring ~553-562), `api/kobo_api/mcp/tools/read.py` (`kobo_get_agent` ~146-177)
- Test: the MCP read-tools test file (grep `kobo_get_agent` in api/tests/)

**Interfaces:**
- Produces: `kobo_get_agent` result gains `pending_learnings`, `can_decide_learnings`, `skills` (name/team/source per entry), `slack` (verbatim as the HTTP detail serves), `has_avatar`, `repo_full_name`, `model_label`, `destroy_visible`.

- [ ] **Step 1: Read `read.py`'s get_agent** -- how it builds its dict today (does it call the HTTP route helper or re-query?). Extend along the SAME lane it already uses; if it re-queries, mirror the detail route's field derivations (main.py ~2886-2911) exactly, including the learnings count query.

- [ ] **Step 2: Failing test**: `kobo_get_agent` on a seeded ready agent with one pending learning returns `pending_learnings == 1`, `can_decide_learnings` boolean, `has_avatar` false, `model_label` present, `skills` list shape.

- [ ] **Step 3: Implement + the doc fixes** (exact edits): server.py comment stops counting ("the read tools and write tools registered by their modules"); write.py "eleven write tools" -> "fourteen write tools" AND the intro enumerates all fourteen; table (A) "seven" -> "eight" naming `kobo_request_repo_access`; create_agent docstring replaces the UI-parity sentence with: "More capable at create time than the room's own modal, which adds skills post-create in the Skills tab -- `skills` picks here are still honored."

- [ ] **Step 4: Run the MCP test files + commit**

```bash
git -C . add api/kobo_api/mcp api/tests/
git -C . commit -m "fix(mcp): honest counts, get_agent aligned with the real detail"
```

---

### Task 8: MCP learnings trio

**Files:**
- Modify: `api/kobo_api/mcp/tools/write.py` (three new tools; count comments 14 -> 17), possibly `read.py` if list-learnings belongs there (decide by the modules' own read/write split: `kobo_list_learnings` is a READ -- register it in read.py and keep the trio's write half in write.py; update BOTH modules' counts/intros: read 7 -> 8, write 14 -> 16)
- Test: the MCP write-tools test file (read first)

**Interfaces:**
- Consumes: the learnings routes' logic. Read how existing write tools invoke room behavior (the `_agent_route_result` table / route-helper pattern) and follow it -- the tools must ride the SAME gates (`_visible_agent_or_404` for list; ready + decide gate for verdicts) and the SAME teach-lane delivery, never a reimplementation.
- Produces: `kobo_list_learnings(agent: str, status: str = "all")` -> `{learnings: [...], can_decide: bool}`; `kobo_decide_learning(agent: str, learning_id: int, action: str)` (approve|deny) -> the route's own result shape; `kobo_decide_learnings_bulk(agent: str, ids: list[int], action: str)` (<= 50 ids) -> per-id status codes verbatim.

- [ ] **Step 1: Read write.py's dispatch pattern** (how kobo_teach reaches the teach behavior; how errors map through `_agent_route_result`) and the learnings route internals (`_decide_learning` is a closure inside create_app -- find how other tools handle closure-bound routes: TestClient-style internal call? app-level helper? Follow the file's established mechanism exactly; if the mechanism is calling module-level helpers, lift `_decide_learning`'s body into a module-level helper main.py's routes AND the tool both call -- a mechanical extraction, no behavior change, covered by the existing route tests).

- [ ] **Step 2: Failing tests**: list returns a seeded pending row + can_decide; approve flips the row and delivers via the (faked) teach lane; deny flips; bulk returns per-id codes; a caller who fails the decide gate gets the gate's refusal through the tool's error shape; all three appear in the audit log wrapper like every other tool.

- [ ] **Step 3: Implement, update counts/intros (read 8, write 16, trio documented), run the MCP tests + FULL API suite**, commit:

```bash
git -C . add api/kobo_api api/tests/
git -C . commit -m "feat(mcp): learnings trio -- list, decide, bulk ride the room's own gates"
```

---

## Self-review notes

- Spec coverage: names (Tasks 1-4: count, prompt, contract, UI), face (5-6: shared prompt, step), MCP (7-8: docs, alignment, trio; backlog table lives in the spec, deliberately unbuilt).
- Read-first steps stand in wherever a helper's exact name was unverified (persona-body fetch, Step contract/skip signal, MCP dispatch pattern, test harnesses) -- each names the file and what to look for.
- Count consistency: spec said "14 -> 17 write"; this plan splits the trio read/write (list is a read) -> read 7 -> 8, write 14 -> 16, total 21 -> 24. The implementer updates every count comment to match the SPLIT ACTUALLY SHIPPED and says so in the report.
