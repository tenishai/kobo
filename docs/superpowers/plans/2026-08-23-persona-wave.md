# Persona Wave Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Two new MCP tools (kobo_list_personas, kobo_create_persona), a team-membership middle gate on POST /api/personas shared by UI and MCP, a render-ceiling bump to 200 lines / 49152 bytes, audit redaction for non-scalar args, and a persona-authoring skill for the org marketplace.

**Architecture:** The MCP tools are pure shims over the existing routes via the wave-1 `_endpoint(app, method, path)` lookup + `authz.room_request(app, email)` synthetic request, so every gate lives once, in the route. The middle gate is a new route-local helper in main.py (the MCP module's `_require_team_membership` has different semantics and stays untouched). Ceiling and redaction are constant/function changes with RED-first boundary tests.

**Tech Stack:** FastAPI + SQLModel (api/), mcp>=1.15,<2 FastMCP, pytest; Svelte UI (comment-only change).

**Spec:** `docs/superpowers/specs/2026-08-23-persona-wave-design.md`

## Global Constraints

- Ceiling values verbatim: `_MAX_RENDERED_LINES = 200`, `_MAX_RENDERED_BYTES = 49152`; `what_they_do` max_length `49152`.
- Gate semantics: team `"shared"` (the default) never consults the resolver and always passes; any other team requires resolver membership exactly, operator (`_is_admin(request)`) exempt; `None` resolver, unresolvable identity, or empty identity refuses 403 (fail-closed).
- No em dashes in any user-facing copy (tool descriptions, error messages, skill content).
- Vendor scan before EVERY commit, real exit-flag gate:
  `FAIL=0; while IFS= read -r t; do [ -z "$t" ] && continue; git diff --cached | grep -qi "$t" && { echo "VENDOR HIT: $t"; FAIL=1; }; done < .superpowers/sdd/vendor-names.txt; [ $FAIL -eq 0 ]`
- Suites run in the shared tree; never overlap a reviewer's run with a dirty tree.
- All tests: `cd api && python -m pytest tests/ -q` must end green before every commit; UI suite `cd ui && npx vitest run` for Task 1's comment-only change (expect no behavior delta).
- mcp pin stays `>=1.15,<2`. No new dependencies anywhere in this wave.

---

### Task 1: Render ceiling 200 lines / 49152 bytes

**Files:**
- Modify: `api/kobo_api/personas.py` (constants ~196-197, `what_they_do` Field ~99, docstrings ~94-104 and ~192-195, `create_persona` docstring ~303-305)
- Modify: `ui/src/components/PersonaBuilder.svelte:129-134` (comment copy only)
- Test: `api/tests/test_personas.py`

**Interfaces:**
- Consumes: `kobo_api.personas._check_rendered_size(body: str) -> None` (raises `PersonaError`), `PersonaFields` (existing).
- Produces: the same names with new bounds; every later task states "200 lines / 48KB" in copy. Task 5's tool docstring and Task 7's skill content cite these numbers.

- [ ] **Step 1: Write the failing boundary tests**

In `api/tests/test_personas.py`, find the existing render-gate boundary tests (`grep -n "150\|32768" api/tests/test_personas.py`) to see the local style, then add (and update the existing 150/32768 boundary tests to the new numbers in the same edit, deliberately, keeping their names' intent):

```python
def test_rendered_size_accepts_exactly_200_lines():
    _check_rendered_size("\n".join(["x"] * 200))


def test_rendered_size_refuses_201_lines():
    with pytest.raises(PersonaError, match=r"201 lines; the limit is 200"):
        _check_rendered_size("\n".join(["x"] * 201))


def test_rendered_size_accepts_exactly_49152_bytes():
    _check_rendered_size("x" * 49152)


def test_rendered_size_refuses_49153_bytes():
    with pytest.raises(PersonaError, match=r"49153 bytes; the limit is 49152"):
        _check_rendered_size("x" * 49153)
```

- [ ] **Step 2: Run to verify the new tests fail**

Run: `cd api && python -m pytest tests/test_personas.py -q -k "200_lines or 201_lines or 49152 or 49153"`
Expected: the two "accepts" tests FAIL (old gate refuses 200 lines? no -- 200 > 150 refused, 49152 > 32768 refused); the two "refuses" tests fail on message mismatch ("the limit is 150"/"the limit is 32768").

- [ ] **Step 3: Bump the constants and the mirroring char wall**

In `api/kobo_api/personas.py`:

```python
_MAX_RENDERED_LINES = 200
_MAX_RENDERED_BYTES = 49152
```

and on `PersonaFields.what_they_do` (the char wall mirrors the byte gate; keep the mirror honest):

```python
    what_they_do: str = Field(min_length=1, max_length=49152)
```

Sweep the numbers in prose: the comments/docstrings at ~94-104 ("32768 chars is a sane absolute wall", "150-line/32768-byte cap"), ~192-195 ("tops out at 150 lines and 32768 bytes (32KB...)"), and `create_persona`'s docstring ("150 lines or 32768 bytes") all say 200 / 49152 (48KB) now. `rules` stays `max_length=150` items (the render gate binds first; the item cap was never the real gate -- leave its comment saying exactly that).

- [ ] **Step 4: Sweep the UI comment**

`ui/src/components/PersonaBuilder.svelte` lines ~129-131: the comment citing "150-line/32768-byte gate is the real ceiling" becomes "200-line/49152-byte gate is the real ceiling". `RULES_MAX = 150` stays (it is the rules item cap, not the render gate).

- [ ] **Step 5: Run both suites**

Run: `cd api && python -m pytest tests/ -q` then `cd ui && npx vitest run`
Expected: all green (any other test asserting the old numbers gets updated deliberately in this commit -- find them with `grep -rn "the limit is 150\|the limit is 32768\|32768" api/tests/`).

- [ ] **Step 6: Vendor scan + commit**

```bash
git add api/kobo_api/personas.py ui/src/components/PersonaBuilder.svelte api/tests/test_personas.py
# vendor scan from Global Constraints, then:
git commit -m "feat(personas): render ceiling 150/32KB -> 200/48KB (spec: under double)"
```

---

### Task 2: Middle gate on POST /api/personas

**Files:**
- Modify: `api/kobo_api/main.py` (new helper directly above `create_persona_route` ~1936; route signature gains `request: Request`)
- Test: `api/tests/test_api_personas.py`

**Interfaces:**
- Consumes: `_is_admin(request)` (main.py ~1026), `request.state.iap_email` (IapMiddleware stamp), `request.app.state.clients.groups.groups_for(email) -> list[str] | None`.
- Produces: `_persona_team_gate(request: Request, team: str) -> JSONResponse | None` (None = pass). `POST /api/personas` now REQUIRES a `request` kwarg when its handler is called directly -- Task 5's tool passes `authz.room_request(app, email)`.

- [ ] **Step 1: Write the failing gate tests**

In `api/tests/test_api_personas.py`, mirror the file's existing client/identity fixtures (read its imports and first test before writing; the identity stamp and resolver stub follow the same conftest helpers the other gated-route tests in this file's neighborhood use). Cover exactly these six behaviors:

```python
def test_persona_create_shared_team_needs_no_membership(...):
    # team omitted entirely -> 201 even with a resolver that would refuse
    # (resolver returning [] or None must not matter on this arm)

def test_persona_create_shared_team_passes_with_resolver_down(...):
    # explicit team="shared", resolver groups_for returns None -> 201

def test_persona_create_named_team_member_passes(...):
    # team="sre", groups_for(email) == ["sre"] -> 201, entry.team == "sre"

def test_persona_create_named_team_non_member_403(...):
    # team="sre", groups_for(email) == ["other-team"] -> 403,
    # body["error"] == "you must be a member of team sre to file a persona under it"

def test_persona_create_named_team_unresolvable_403(...):
    # team="sre", groups_for returns None -> 403 (fail-closed)

def test_persona_create_named_team_operator_passes(...):
    # non-member caller who IS admin (operators email or admin group) -> 201
```

Also: empty identity (no iap_email stamped) + named team -> 403.

- [ ] **Step 2: Run to verify they fail**

Run: `cd api && python -m pytest tests/test_api_personas.py -q -k "team"`
Expected: the 403 tests FAIL (route currently accepts any team, returns 201).

- [ ] **Step 3: Implement the gate helper and wire the route**

In `api/kobo_api/main.py`, directly above `create_persona_route`:

```python
def _persona_team_gate(request: Request, team: str) -> JSONResponse | None:
    """Persona middle gate (persona-wave spec, operator ruling): team
    "shared" is the commons -- anyone files there and the resolver is
    never consulted, mirroring the teamless PersonaBuilder flow exactly.
    Any OTHER team requires the caller's resolver groups to contain that
    team exactly; operators are exempt; a missing identity, a missing
    resolver, or an unresolvable-right-now answer refuses (fail-closed).
    Deliberately NOT kobo_api.mcp.tools.write._require_team_membership:
    that gate is the marketplace axis (operator-only "shared") and its
    mutation pins must not churn here. Cross-referenced there.
    """
    if team == "shared":
        return None
    if _is_admin(request):
        return None
    refusal = JSONResponse(
        {"error": f"you must be a member of team {team} to file a persona under it"},
        status_code=403,
    )
    email = (getattr(request.state, "iap_email", None) or "").lower()
    if not email:
        return refusal
    clients: Clients | None = request.app.state.clients
    resolver = clients.groups if clients is not None else None
    resolved = resolver.groups_for(email) if resolver is not None else None
    if resolved is None or team not in resolved:
        return refusal
    return None
```

Wire it into `create_persona_route` -- the signature gains `request: Request` and the gate runs before any storage work:

```python
    @app.post("/api/personas", status_code=201)
    def create_persona_route(
        payload: CreatePersonaRequest,
        request: Request,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> JSONResponse:
        cfg: Config = app.state.config
        clients = app.state.clients

        gate = _persona_team_gate(request, payload.team)
        if gate is not None:
            return gate

        if clients is None or clients.storage is None or not cfg.personas_bucket:
            ...  # unchanged from here down
```

Add the reverse cross-reference: one sentence in `_require_team_membership`'s docstring in `api/kobo_api/mcp/tools/write.py` ("the persona route's `_persona_team_gate` is the deliberate looser sibling: commons-open, membership only for named teams").

- [ ] **Step 4: Run the suite**

Run: `cd api && python -m pytest tests/test_api_personas.py -q`
Expected: PASS, including every pre-existing create test (they omit `team`, landing on the shared arm).

- [ ] **Step 5: Mutation check (manual, recorded in the task report)**

Comment out the `gate = _persona_team_gate(...)` pair, run step 4 again, confirm `test_persona_create_named_team_non_member_403` FAILS, restore. The report names the killed mutant.

- [ ] **Step 6: Vendor scan + commit**

```bash
git add api/kobo_api/main.py api/kobo_api/mcp/tools/write.py api/tests/test_api_personas.py
git commit -m "feat(personas): team middle gate on create -- shared open, named team needs membership"
```

---

### Task 3: Audit redaction for non-scalar and unbounded args

**Files:**
- Modify: `api/kobo_api/mcp/audit.py` (`_redact` ~117-129, new `_bounded` + `_ARG_TEXT_CAP` beside `_REDACTED` ~80)
- Test: `api/tests/test_mcp_audit.py`

**Interfaces:**
- Consumes: `SENSITIVE_KEYS`, `_REDACTED`, `_redact(kwargs: dict) -> dict` (existing).
- Produces: `_ARG_TEXT_CAP = 512`; `_bounded(value)` applied to every non-name-redacted value; `_redact` semantics otherwise unchanged. Task 5 relies on this: `kobo_create_persona`'s `what_they_do`/`rules` audit rows are bounded.

- [ ] **Step 1: Write the failing tests**

```python
def test_redact_truncates_string_over_cap():
    long = "x" * 513
    out = _redact({"what_they_do": long})
    assert out["what_they_do"] == "x" * 512 + "...[truncated 1 chars]"


def test_redact_keeps_string_at_exact_cap():
    out = _redact({"what_they_do": "x" * 512})
    assert out["what_they_do"] == "x" * 512


def test_redact_bounds_list_per_element():
    out = _redact({"rules": ["short", "y" * 600]})
    assert out["rules"] == ["short", "y" * 512 + "...[truncated 88 chars]"]


def test_redact_name_redaction_still_wins():
    out = _redact({"note": "z" * 600})
    assert out["note"] == "***REDACTED***"


def test_redact_does_not_mutate_caller_list():
    rules = ["y" * 600]
    _redact({"rules": rules})
    assert rules == ["y" * 600]
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd api && python -m pytest tests/test_mcp_audit.py -q -k "truncat or bounds or mutate or exact_cap"`
Expected: FAIL (`_redact` currently passes values through whole).

- [ ] **Step 3: Implement**

In `api/kobo_api/mcp/audit.py`, beside `_REDACTED`:

```python
# Values are BOUNDED, not name-redacted: the persona wave's what_they_do
# (49152-char wall) would otherwise land whole in every audit row. Name
# redaction (SENSITIVE_KEYS) still wins first and redacts the value whole.
_ARG_TEXT_CAP = 512


def _bounded(value: Any) -> Any:
    """Cap string values at _ARG_TEXT_CAP with an explicit truncation
    marker; lists are bounded per element (a NEW list -- callers' kwargs
    are never mutated). Everything else passes through for json.dumps's
    default=str to handle."""
    if isinstance(value, str) and len(value) > _ARG_TEXT_CAP:
        return value[:_ARG_TEXT_CAP] + f"...[truncated {len(value) - _ARG_TEXT_CAP} chars]"
    if isinstance(value, list):
        return [_bounded(item) for item in value]
    return value
```

and in `_redact`, the non-sensitive branch changes from `redacted[key] = value` to:

```python
            redacted[key] = _bounded(value)
```

- [ ] **Step 4: Run the audit suite**

Run: `cd api && python -m pytest tests/test_mcp_audit.py -q`
Expected: PASS including the pre-existing redaction-no-mutate and wraps-signature tests.

- [ ] **Step 5: Vendor scan + commit**

```bash
git add api/kobo_api/mcp/audit.py api/tests/test_mcp_audit.py
git commit -m "feat(mcp): bound audit-log args -- 512-char truncation marker, lists per element"
```

---

### Task 4: kobo_list_personas (read tool)

**Files:**
- Modify: `api/kobo_api/mcp/tools/read.py` (new tool after `kobo_list_tools`)
- Test: `api/tests/test_mcp_tools_read.py`

**Interfaces:**
- Consumes: `_endpoint(app, "GET", "/api/personas/search")` imported from `kobo_api.mcp.tools.write` (deferred, inside `register` -- write imports nothing from read, so no cycle; verify with `python -c "import kobo_api.mcp.tools.read, kobo_api.mcp.tools.write"`). The search endpoint returns a plain dict `{"results": [...]}` (NOT a JSONResponse) with entries `id/name/description/team/tags/builtin`.
- Produces: MCP tool `kobo_list_personas(q: str = "", team: str | None = None) -> dict` returning `{"result": [...]}` with exactly the route's entry shape. Task 5's instant-visibility test and Task 8's wire acceptance call it.

- [ ] **Step 1: Write the failing tests**

In `api/tests/test_mcp_tools_read.py`, following the file's existing tool-invocation fixtures (read its `kobo_list_skills` tests first for the local idiom):

```python
def test_list_personas_mirrors_search_route_shape(...):
    # Seed one persona through POST /api/personas (shared team), then call
    # the tool; assert the entry carries exactly the keys
    # {id, name, description, team, tags, builtin} and matches the route's
    # own /api/personas/search answer for the same query.

def test_list_personas_q_and_team_filter_parity(...):
    # Two seeded personas, different teams; tool(q=..., team=...) returns
    # the same subset the route returns for identical params.

def test_list_personas_result_key_shape(...):
    # top-level shape is {"result": [...]} (house list-tool contract)
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd api && python -m pytest tests/test_mcp_tools_read.py -q -k personas`
Expected: FAIL ("no attribute kobo_list_personas" / tool not registered).

- [ ] **Step 3: Implement the tool**

In `read.py`'s `register`, after `kobo_list_tools`:

```python
    from kobo_api.mcp.tools.write import _endpoint

    search_personas_endpoint = _endpoint(app, "GET", "/api/personas/search")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    @audited("kobo_list_personas")
    async def kobo_list_personas(
        ctx: Context, q: str = "", team: str | None = None
    ) -> dict:
        """List every persona in the room's catalog: id, name,
        description, owning team, tags, and whether it is a room
        builtin. Optional q matches name/description; optional team
        filters to one team. Before creating a new persona, ALWAYS
        list and check fit here first -- prefer an existing persona
        over a near-twin. Any signed-in caller may list.
        """
        session = app.state.session_factory()
        try:
            body = search_personas_endpoint(q=q, team=team, session=session)
        finally:
            session.close()
        return {"result": body["results"]}
```

(The endpoint call rides `_maybe_refresh_catalogs` inside the route, so a persona created moments ago is already visible -- that is the point of shimming the route rather than querying the table directly.)

- [ ] **Step 4: Run the read-tools suite**

Run: `cd api && python -m pytest tests/test_mcp_tools_read.py -q`
Expected: PASS.

- [ ] **Step 5: Vendor scan + commit**

```bash
git add api/kobo_api/mcp/tools/read.py api/tests/test_mcp_tools_read.py
git commit -m "feat(mcp): kobo_list_personas -- room persona catalog over the search route"
```

---

### Task 5: kobo_create_persona (write tool) + tool count 18

**Files:**
- Modify: `api/kobo_api/mcp/tools/write.py` (endpoint lookup beside the others ~488-493; new tool after `kobo_create_tool`)
- Modify: the tool-inventory exact-set test (locate: `grep -rn "kobo_wire_slack" api/tests/` -- the exact-set assertion lists every tool name; wave 1 left it at 16)
- Test: `api/tests/test_mcp_tools_write.py`

**Interfaces:**
- Consumes: `_endpoint(app, "POST", "/api/personas")`, `authz.room_request(app, email)`, `get_user_email(ctx)`, `app.state.session_factory()`, `_create_result(response)` (all wave-1, unchanged), `CreatePersonaRequest` from `kobo_api.main` (Task 2's route signature: `payload`, `request`, `session`).
- Produces: MCP tool `kobo_create_persona(name, who_they_are, what_they_do, rules=None, team="shared") -> dict` returning the route's 201 body (`id/name/description/team/tags/builtin`). Tool inventory becomes 18.

- [ ] **Step 1: Write the failing tests**

In `api/tests/test_mcp_tools_write.py`, following the file's write-tool fixtures:

```python
def test_create_persona_shared_lands_and_is_instantly_listable(...):
    # tool(name="Night Auditor", who_they_are=..., what_they_do=...) ->
    # body["team"] == "shared"; then kobo_list_personas shows the id
    # (the route's inline ingest, inherited through the shim)

def test_create_persona_named_team_member_passes(...):
    # resolver stub: caller in ["sre"]; tool(..., team="sre") -> team "sre"

def test_create_persona_named_team_non_member_toolerror(...):
    # resolver stub: caller in ["other"]; tool(..., team="sre") raises
    # ToolError whose message is the route's own 403 copy verbatim:
    # "you must be a member of team sre to file a persona under it"

def test_create_persona_exists_conflict_verbatim(...):
    # create twice -> second raises ToolError("a persona with that name
    # already exists")

def test_create_persona_render_overflow_verbatim(...):
    # what_they_do of 201 newline-joined lines -> ToolError carrying the
    # route's 422 message ("renders to ... lines; the limit is 200")

def test_create_persona_slug_law_verbatim(...):
    # name="!!!" -> ToolError carrying the persona_id law message

def test_create_persona_audit_row_bounded(...):
    # what_they_do 2000 chars -> the mcp_tool_call_log row's redacted_args
    # holds the truncation marker, never the full text (Task 3's cap)
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd api && python -m pytest tests/test_mcp_tools_write.py -q -k create_persona`
Expected: FAIL (tool absent).

- [ ] **Step 3: Implement the tool**

In `write.py`'s `register`, beside the other lookups:

```python
    create_persona_endpoint = _endpoint(app, "POST", "/api/personas")
```

and after `kobo_create_tool`:

```python
    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    @audited("kobo_create_persona")
    async def kobo_create_persona(
        ctx: Context,
        name: str,
        who_they_are: str,
        what_they_do: str,
        rules: list[str] | None = None,
        team: str = "shared",
    ) -> dict:
        """Create a brand-new persona in the room's catalog -- the same
        create the room's own PersonaBuilder drives. Before creating,
        run kobo_list_personas and check fit first; prefer an existing
        persona over a near-twin. `name` becomes the catalog id via the
        room's slug law (must not already exist). `who_they_are` is ONE
        line; `what_they_do` is the persona's body; `rules` are optional
        always/never one-liners. The merged persona renders to at most
        200 lines and 48KB. `team` defaults to "shared", the commons
        anyone may file into; naming any other team requires that you
        are a member of it (a room operator may name any team). The
        persona is searchable and pickable by kobo_create_agent
        immediately.
        """
        from pydantic import ValidationError

        from kobo_api.main import CreatePersonaRequest

        email = get_user_email(ctx)
        req = authz.room_request(app, email)
        try:
            payload = CreatePersonaRequest(
                name=name,
                who_they_are=who_they_are,
                what_they_do=what_they_do,
                rules=rules or [],
                team=team,
            )
        except ValidationError as exc:
            raise ToolError(exc.errors()[0].get("msg", "invalid persona fields")) from exc

        session = app.state.session_factory()
        try:
            response = create_persona_endpoint(
                payload=payload, request=req, session=session
            )
            return _create_result(response)
        finally:
            session.close()
```

(Check how `kobo_create_agent` in this file handles `ValidationError` first; if it already has an idiom, mirror it exactly instead of the above `except` block.)

- [ ] **Step 4: Bump the tool-inventory exact-set test to 18**

Locate it (`grep -rn "kobo_wire_slack" api/tests/`), add `"kobo_list_personas"` and `"kobo_create_persona"` to the expected set, and update any asserted count.

- [ ] **Step 5: Run the write-tools suite + full suite**

Run: `cd api && python -m pytest tests/test_mcp_tools_write.py -q && python -m pytest tests/ -q`
Expected: PASS, full suite green.

- [ ] **Step 6: Mutation check (manual, recorded)**

Re-run the Task 2 gate mutant (comment the route's gate call): `test_create_persona_named_team_non_member_toolerror` must FAIL through the shim -- this is the inheritance proof. Restore.

- [ ] **Step 7: Vendor scan + commit**

```bash
git add api/kobo_api/mcp/tools/write.py api/tests/test_mcp_tools_write.py <exact-set-test-file>
git commit -m "feat(mcp): kobo_create_persona -- persona create through the gated route; inventory 18"
```

---

### Task 6: Docs

**Files:**
- Modify: `README.md` (the MCP bullet under What-the-room-does-today)
- Modify: `docs/superpowers/specs/2026-08-22-mcp-server-design.md:136` ("Ten tools, thin shims" -- now stale twice over)
- Modify: `docs/superpowers/specs/2026-08-23-persona-wave-design.md` (Status line)

**Interfaces:** none (copy only).

- [ ] **Step 1: README** -- extend the MCP bullet's plain-language capability list with persona listing and creation (match the bullet's existing voice; no infra vocabulary, no em dashes).

- [ ] **Step 2: mcp-server spec** -- replace "Ten tools, thin shims over already-tested internals." with "Eighteen tools (ten at this spec's writing, six added by the marketplace wave, two by the persona wave), thin shims over already-tested internals." (register item m closed).

- [ ] **Step 3: persona-wave spec** -- Status: `Approved` becomes `Implemented (rollout pending)`.

- [ ] **Step 4: mcp-server spec register note** -- locate the register line "create only selects existing catalog persona" (`grep -n "selects existing catalog persona" docs/superpowers/specs/2026-08-22-mcp-server-design.md`) and rewrite it as closed: the two-step flow (kobo_create_persona, then kobo_create_agent picking it) supersedes free-text create inside create_agent.

- [ ] **Step 5: Vendor scan + commit**

```bash
git add README.md docs/superpowers/specs/2026-08-22-mcp-server-design.md docs/superpowers/specs/2026-08-23-persona-wave-design.md
git commit -m "docs: persona wave -- README MCP bullet, tool inventory 18, spec statuses"
```

---

### Task 7: The persona-authoring skill content

**Files:**
- Create: `/private/tmp/claude-503/-Users-kgreiner-Documents-GitHub-kobo/2d361135-fc6b-48a3-8407-17268b8f2b5e/scratchpad/persona-authoring-SKILL.md` (scratchpad -- the content publishes via MCP in Task 8, never lands in this repo)
- Read: the four exemplar paths listed in `.superpowers/sdd/persona-exemplars.txt` (untracked; carries paths the vendor rule keeps out of this file)

**Interfaces:**
- Consumes: the four exemplar persona.md files; the ceiling numbers from Task 1.
- Produces: a complete SKILL.md body for Task 8's `kobo_create_skill(skill_id="persona-authoring", team="infra-engineering", content=<this file>)` call.

- [ ] **Step 1: Read all four exemplars end to end.**

- [ ] **Step 2: Write the skill.** Frontmatter VERBATIM (this is the lawful-from-the-start checklist; the marketplace validator requires name, description, version, tags with a domain tag, profiles non-empty, owner):

```markdown
---
name: persona-authoring
description: How to write a strong agent persona for the Kobo room. Identity with a reason, a defining quality anchored in real working conditions, modes over adjectives, boundaries as behavior, and fitting the room's 200-line/48KB render ceiling.
when_to_use: When creating or revising a persona in the Kobo room, via the PersonaBuilder or the kobo_create_persona MCP tool.
version: 1.0.0
tags: [agent-ops, operations]
profiles: [agent-sdk]
owner: infra-engineering
---
```

Body, about 120 lines, six sections in this order, each distilled from the exemplars with one concrete quoted-technique example per section (quote the TECHNIQUE shape, never an org name, channel id, or person's employer -- the vendor rule applies to catalog content published under the room's name):

1. **Kobo persona anatomy** (~15 lines): who_they_are is one line; what_they_do is the body; rules are 0-150 always/never one-liners; the merged render ceiling is 200 lines / 48KB; the name slugifies to the catalog id; filing under a named team requires membership, "shared" is open.
2. **Identity with a reason** (~15 lines): a name that means something and a lineage gives a consistent voice under pressure.
3. **A defining quality anchored in real working conditions** (~20 lines): derive the quality from who the agent serves and when (the write-for-the-morning-reader move: complete context, no dangling references, everything copy-pasteable).
4. **Modes over adjectives** (~25 lines): structure what-they-do as numbered operating modes with pick-the-right-one guidance; adjectives describe, modes decide.
5. **Boundaries as behavior** (~20 lines): ask-before-mutate, explain-before-act, audience calibration (bridge, not gatekeep); rules as testable one-liners with three good/bad rule pairs.
6. **Fit the ceiling** (~15 lines): what to cut first -- version headers, channel ids, runtime plumbing the room composes elsewhere; the ceiling exists so personas stay readable in one sitting.

- [ ] **Step 3: Verify the content against the room's own law**

Run (from `api/`): `python -c "from kobo_api.personas import _check_rendered_size; _check_rendered_size(open('<scratchpad path>/persona-authoring-SKILL.md').read())"`
Expected: no exception (the skill itself fits the persona ceiling -- a nice dogfood check even though skills are not personas), and `grep -c '—' <file>` returns 0 (no em dashes), and a vendor scan of the file is clean.

- [ ] **Step 4: No commit** (nothing in-repo changed; the scratchpad file is Task 8's payload). Report the file path and line count.

---

### Task 8: Rollout + wire acceptance (operator lane)

**Files:** none in this repo (image build, room roll, live MCP calls).

**Interfaces:**
- Consumes: everything above, deployed; the scratchpad SKILL.md from Task 7.

- [ ] **Step 1: Build + roll the kobo image** at this wave's HEAD via the standing build lane; digest-verify the deployed pod against the registry before proceeding (the house rule: digest-verify every tag before rolling).

- [ ] **Step 2: Wire acceptance from the connector (operator identity infra@), in order:**

1. Connector refresh so the inventory serves 18 tools (a cached connector shows 16 -- same drill as wave 1).
2. `kobo_list_personas` -> the seeded builtins appear with `builtin: true`.
3. `kobo_create_persona` (shared team) -> 201-shaped result; immediately visible in `kobo_list_personas`; `kobo_create_agent` can pick it (two-step flow closed). Use a throwaway name; the room has no persona delete, so pick something honest like "wire-probe-persona".
4. `kobo_create_persona` with `team="infra-engineering"` (infra@ is a member) -> lands with that team.
5. A what_they_do that renders past 150 lines but under 200 -> creates (the ceiling proof on the wire).
6. Non-member named-team refusal: unit-proven only (infra@ holds operator standing; no unprivileged wire identity). State this in the ledger, do not fake it.
7. UI check (operator's browser): PersonaBuilder still creates teamless.
8. `kobo_create_skill(skill_id="persona-authoring", team="infra-engineering", content=<Task 7 file>)` -> commit sha; bucket object under `catalog/persona-authoring/` after publish CI (check the bucket FIRST if it does not appear in list_skills -- the wave-1 runbook); `kobo_list_skills` shows it after the room's catalog TTL.

- [ ] **Step 3: Ledger the acceptance** in `.superpowers/sdd/progress.md` (every proof, every sha), including the two register closes (audit flat-scalar assumption, spec "Ten tools") and the still-open wave-2 must-fix (create_skill frontmatter law).
