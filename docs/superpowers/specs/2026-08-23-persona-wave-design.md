# Persona Wave: MCP persona tools, the route team gate, and the persona-authoring skill

**Status:** Approved (brainstorm rulings encoded 2026-08-23)
**Depends on:** MCP server arc (live), MCP marketplace wave 1 (live, wire-accepted)

## What this wave ships

1. Two new MCP tools, `kobo_list_personas` and `kobo_create_persona`, closing the
   registered create-persona MCP gap. Tool count goes 16 to 18.
2. A team-membership gate on `POST /api/personas` itself, so the web UI and MCP
   share one posture (operator ruling: approach B, match the MCP and the UI).
3. A render-ceiling expansion for personas: 150 lines / 32768 bytes becomes
   **200 lines / 49152 bytes** (operator ruling: expandable, but not double).
4. A `persona-authoring` best-practices skill published to the org marketplace
   catalog through `kobo_create_skill` (dogfood), distilled from four exemplar
   personas.

## Operator rulings (brainstorm, 2026-08-23)

- **Personas stay room-local.** `kobo_create_persona` writes through the existing
  room personas bucket + index + instant ingest. No marketplace publishing lane
  for personas this wave; graduation stays open as a later wave.
- **Middle gate, applied at the route.** Team omitted or `"shared"`: anyone
  creates (today's UI flow, unchanged, and this arm stays open when the group
  resolver is down -- the commons-is-not-a-grant precedent). Team naming
  anything else: the caller's resolver groups must contain that team exactly;
  operator (`authz.is_admin`) exempt; unresolvable-right-now refuses
  (fail-closed). The gate lives in the route so UI and MCP inherit the same
  posture; the MCP tool is a pure shim.
- **Exemplars:** iris, rook, prudence, cora (ember skipped -- does not resolve).
  Local paths: `infra-agent-iris/iris/persona.md`, `agents/prudence/persona.md`,
  `infra-agent-cora/cora/persona.md`, and rook's `persona.md` in the org's
  agent-registry repo (operator supplies the path at execution; it is not named
  here by the vendor rule).
- **Ceiling:** the current limits are not a hard law, but the new ceiling must
  stay under double. 200 lines / 48KB chosen: the largest exemplar (169 lines)
  fits with headroom; both numbers are well under 2x.

## Design

### 1. Route gate on POST /api/personas

`create_persona_route` (main.py) gains a route-local middle-gate helper before
`create_persona()` runs:

- `payload.team` in (unset, `"shared"`): pass, no resolver consulted.
- Any other team slug: require `clients.groups.groups_for(email)` to contain the
  team exactly, where `email` is the request identity. `authz.is_admin`
  short-circuits first. A `None` resolver, an unresolvable identity, or an empty
  identity refuses. Refusal is a 403 whose message names the requirement
  ("you must be a member of team X to file a persona under it", exact copy at
  implementation).
- The helper is NEW and route-local. The MCP module's `_require_team_membership`
  (wave-1 security boundary, mutation-pinned) is deliberately untouched; its
  semantics differ (operator-only shared). Cross-reference comments both ways.
- UI impact: none. The PersonaBuilder never sends `team` (teamless-create
  addendum); the default-shared arm is byte-identical to today.

### 2. kobo_create_persona (write tool)

- Signature: `name` (slugified by the existing `persona_id` law), `who_they_are`
  (one line), `what_they_do`, optional `rules` (list of one-liners), optional
  `team` (default shared).
- Pure shim: calls the `POST /api/personas` handler via the `app.routes` lookup
  (wave-1 write-tool mechanism). Inherits the new gate, the render ceiling, the
  slug law, 409-on-exists, and the route's own instant catalog ingest. Zero
  duplicated logic.
- Error translation: the create-scoped table (no anti-enumeration fold -- there
  is no existing resource to protect). 403/409/422 route messages surface
  verbatim as ToolError; 5xx becomes the did-not-answer constant.
- Description carries the check-fit-first sentence pointing at
  `kobo_list_personas` (prefer an existing persona over a near-twin).

### 3. kobo_list_personas (read tool)

- Mirrors `GET /api/personas/search`: optional `q` and `team` filters; returns
  `id`, `name`, `description`, `team`, `tags`, `builtin` per entry. Any
  signed-in caller. Rides the same catalog-refresh path the route uses.

### 4. Render ceiling 200 lines / 49152 bytes

- `_MAX_RENDERED_LINES` 150 -> 200; `_MAX_RENDERED_BYTES` 32768 -> 49152
  (personas.py), with the gate's error copy updating automatically.
- `PersonaFields.what_they_do` max_length 32768 -> 49152 (the char wall mirrors
  the byte gate; keep the mirror honest). `rules` stays max 150 items -- the
  render gate binds first and the item cap was never the real gate.
- Sweep every place the old numbers appear as copy: personas.py docstrings,
  main.py request-model docstrings, UI strings if any name 150/32KB, the
  mcp-server spec, and the two new tools' descriptions (they state the ceiling).

### 5. Audit redaction: the flat-scalar assumption comes due

Wave-1 register: audit-log arg redaction is non-recursive, accepted because all
10 (then 16) tools passed flat scalars. `kobo_create_persona` passes `rules`
(a list) and `what_they_do` (large text). This wave must:

- Extend redaction: list-of-strings args are redacted per element; string args
  over a fixed cap are truncated with an explicit truncation marker. The audit
  row must never store an unredacted or unbounded copy of `what_they_do`.
- Add the register's exact-instant/boundary-style tests for the new arg shapes.

### 6. The persona-authoring skill

- Published via the `kobo_create_skill` MCP tool (dogfood; re-proves the lane
  post-fix), team `infra-engineering`.
- Frontmatter fully lawful FROM THE START (the create_skill validation gap is a
  registered wave-2 item, not fixed here, so the content itself must comply):
  `version: 1.0.0`, `tags: [agent-ops, operations]` (agent-ops is the domain
  tag), `profiles: [agent-sdk]`, `when_to_use` filled; name/owner auto-set.
- Content (~120 lines), distilled from the four exemplars plus the room's law:
  1. Kobo persona anatomy: who_they_are / what_they_do / rules; the 200-line/
     48KB render ceiling; the slug law; the team middle gate.
  2. Identity with a reason: names that mean something, lineage; a persona with
     a story keeps a consistent voice under pressure.
  3. A defining quality anchored in real working conditions (the
     patience-across-time-zones, write-for-the-morning-reader exemplar move).
  4. Modes over adjectives: structure what-they-do as numbered operating modes
     with pick-the-right-one guidance.
  5. Boundaries as behavior: ask-before-mutate, explain-before-act, audience
     calibration; rules as testable always/never one-liners.
  6. Fit the ceiling: what to cut first (version headers, channel ids, runtime
     plumbing the room composes elsewhere).
- The skill never names the client org or any of its repos (vendor rule applies
  to authored content going into the catalog under the room's name).

### 7. Docs

- README MCP bullet gains persona list/create in plain language.
- mcp-server spec: tool inventory 16 -> 18 AND the stale "Ten tools" line
  (register item m) fixed in the same touch.
- The "create selects existing catalog persona only" register note updates to
  describe the closed two-step flow: create a persona, then create an agent
  picking it.

## Testing (house adversarial style, mutation-verified gates)

- Route gate: shared/omitted-team creates for anyone incl. resolver-down;
  named-team member passes; non-member 403; unresolvable 403; empty identity
  403; operator passes any team. The gate-deleted mutant is killed by a named
  route test AND by an MCP-shim test (inheritance proof, T5 style).
- Ceiling RED-first: 201 lines refused, exactly 200 accepted; 49153 bytes
  refused, exactly 49152 accepted. Existing 150-line tests updated deliberately
  in the same commit, never silently.
- kobo_create_persona: 409-on-exists; 422 render-overflow and slug-law messages
  verbatim; created persona instantly visible via kobo_list_personas; audit row
  present with redaction holding for list + large-text args.
- kobo_list_personas: shape parity with the search route; q/team filter parity;
  builtin flag present; any-signed-in.
- Suites green (API + UI); reviewer runs suites in the shared tree; vendor scan
  with a real exit-flag gate on every commit.

## Rollout and wire acceptance

Image build + roll only: no DB migration, no org config change, no new secrets.
From the connector (operator identity infra@):

1. `kobo_list_personas` returns the seeded builtins.
2. `kobo_create_persona` shared-team lands, is instantly listable, and
   `kobo_create_agent` can pick it (two-step flow closed).
3. Named-team create as infra@ under its own team lands. The non-member refusal
   stays unit-level only (infra@ holds operator standing; no unprivileged
   identity available on the wire).
4. UI PersonaBuilder still creates teamless post-roll.
5. A >150-line persona (under 200) creates successfully -- the ceiling proof.
6. `persona-authoring` publishes via MCP, bucket object appears after publish
   CI, catalog entry after the room TTL (bucket-checked first per the runbook).

## Explicitly out of scope

- Marketplace-published personas (fork 1 ruled room-local; graduation later).
- The create_skill frontmatter-law validation (registered wave-2 must-fix; the
  skill published here complies by hand).
- Free-text persona creation inside kobo_create_agent itself (the two-step flow
  supersedes it; the create_agent signature stays).
- Gating changes to any other persona route (draft/distill stay as they are).
