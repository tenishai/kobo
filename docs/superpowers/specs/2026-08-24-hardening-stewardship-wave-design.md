# Hardening + stewardship wave: marketplace frontmatter gate, audit bounding, persona archive, persona naming

**Status:** Implemented (rollout pending)
**Depends on:** MCP marketplace wave 1 (live, wire-accepted), Persona wave (live, wire-accepted)

## What this wave ships

1. A frontmatter-law gate inside `create_skill` (marketplace.py): pre-push
   completion + validation so one publish can never again brick the org
   catalog's publish pipeline. Closes the wave-2 must-fix register entry
   (the ~35-minute publish brick, lived 2026-08-23).
2. Audit `_bounded` hardening: element-count cap with an explicit marker,
   recursion into dict values, boundary tests. Closes the registered
   first fast-follow from the persona wave final review.
3. A persona archive lane (soft flag, UI-only, reversible), closing the
   no-delete gap the persona-wave acceptance surfaced when it minted three
   permanent fixture cards.
4. A persona naming law (the glance test) landed in the create tool's
   docstring, the persona-authoring skill (v1.0.1), and PersonaBuilder
   microcopy, plus a display hierarchy pass: the agent name is the real
   name; the persona renders as the agent's soul-line, not a co-equal title.
5. Riders while the files are open: the two wave-1 write.py minors --
   (o) indistinguishable did-not-answer match strings in transport tests,
   (q) loose "when absent" docstring wording on the unconditional name
   overwrite.

## Operator rulings (brainstorm, 2026-08-24)

- **Law lives in kobo; vocabularies ride org config.** No execution of
  marketplace-repo code inside the room (refused on fail-closed posture:
  it would hand any marketplace-write teammate code execution in the room
  with the App's credentials). Structural law is hardcoded; org-specific
  vocabularies are optional org-config data. Drift is bounded: the publish
  pipeline stays the backstop, and a drifted rule degrades to today's
  behavior for that rule only.
- **Missing `version` completes to `"1.0.0"`.** Always honest: publishes
  never overwrite an existing slot, so a create is the first version by
  construction. **Missing `profiles` completes to `["agent-sdk"]`** -- the
  room's lane default, same complete-the-boring-parts philosophy as the
  existing name/owner/description completion. Malformed values always
  refuse with the field named.
- **Persona archive folds into this wave** (ruled after first being scoped
  separate). Archive, not delete (ruled 2026-08-24); UI-only lane, no MCP
  archive tool -- consistent with the agent-destroy-MCP-absent ruling.
  Builtin personas archive operator-only. Shared-team personas archive
  operator-only (no ownership axis exists to check). Team personas: team
  member or operator.
- **Persona `name` stays required.** No auto-derive: a derived name is
  exactly how opaque names happen; asking the author for a few honest
  hyphenated words is the design pressure the glance test wants.
- **The naming law is guidance, not a mechanical gate.** Self-explanatory
  cannot be validated; it can be taught at every authoring surface.

## Design

### 1. Frontmatter gate (marketplace.py `create_skill`)

Runs pre-clone, extending the existing completion step. Order: parse
frontmatter (existing) -> owner-conflict check (existing) -> completion --
`name`/`owner` (existing, unconditional), `description` from first body
line when absent (existing), `version: "1.0.0"` when absent (new),
`profiles: ["agent-sdk"]` when absent (new) -> validation (new) ->
non-empty body check (existing) -> clone/collide/push (existing).

Validation rules, structural (hardcoded beside the completion):

- All five required fields present and non-empty after completion:
  `name`, `description`, `version`, `tags`, `profiles`.
- `version` matches three-part semver shape (`\d+\.\d+\.\d+`, fullmatch).
- `tags` and `profiles` are lists of non-empty strings. `tags` has no
  honest completion (what a skill is about is the author's claim) -- an
  absent or empty `tags` refuses with an actionable message.

Validation rules, vocabularies (org-config, each optional):

- New optional config block `skills_frontmatter:` with keys
  `allowed_profiles: [...]` and `allowed_tags: [...]`. Each configured key
  adds a subset check; an unconfigured key adds nothing. The gate is fully
  functional with no block configured (structural checks only), so the
  room never blocks on config the org has not written, and vocabulary
  drift cannot brick publishing.
- Config model gains the block as an optional field (same lane the `mcp`
  block used). Overlay rule: image ships before org.yaml names the block.

Every refusal is a `MarketplaceError` naming the field, the expected
shape, and the allowed values when a vocabulary is configured. The
existing `ToolError` mapping in the MCP tool surfaces it verbatim -- the
failure is loud and actionable at the caller, never silent at the
pipeline.

Scope: `create_skill` only. `create_tool`'s lane is repo-direct with no
pipeline behind it -- no brick equivalent, deliberately untouched.

Named regression test: the exact 2026-08-23 brick shape (caller content
whose frontmatter lacks `version` and `profiles`) now publishes lawfully
via completion; a malformed `version` refuses; an unknown profile refuses
when the vocabulary is configured and passes when it is not.

### 2. Audit bounding (mcp/audit.py `_bounded`)

- Lists: bounded to the first 64 elements; when truncated, one final
  marker element `"[+N more]"` (N = dropped count) is appended. The cap
  applies per list at every depth.
- Dicts: recurse into values (new); keys untouched (schema-shaped, not
  user text).
- A defensive depth cap (8) stops the walk with a `"[depth capped]"`
  sentinel -- the audit path must never crash or hang a tool call,
  whatever shape a future tool's kwargs take.
- Caller kwargs are never mutated: every bounded list/dict is a new
  object (existing pin, extended to the new shapes).

Boundary tests: 64 exactly (no marker), 65 (64 + marker, N pinned),
marker format pinned, dict-in-list, nested dict, string cap applying
inside dict values, non-str scalars passing through, kwargs-not-mutated
on nested shapes. Closes the persona-wave amplification (refused
create_persona logging an unbounded-count rules list) and the wave-1
`skills: list[dict]` pass-through.

### 3. Persona archive (personas.py, routes, UI)

- The catalog index entry gains optional `archived: true`. Absent means
  active -- every existing index file stays valid unread.
- Two room routes: `POST /api/personas/{id}/archive` and
  `POST /api/personas/{id}/unarchive`. Gate per the ruling: team persona
  -> member of that team or operator; shared-team or builtin persona ->
  operator only. Refusal copy names the requirement. 404 for an id that
  does not exist.
- Effects when archived: dropped from the persona picker, dropped from
  `GET /api/personas` by default, dropped from `kobo_list_personas`
  always (the MCP tool gains no parameter). The route gains
  `include_archived` (default false) so the UI can render an archived
  section. Filtering happens at query time on the index flag -- the
  search catalog is not mutated (plan-time detail: verify the picker's
  search path reads through the same filter, with a test).
- `kobo_create_agent` / the create flow picking an archived persona
  refuses honestly: "persona {id} is archived" (422 at the route; the MCP
  shim inherits the message via the existing 422 mapping). No
  anti-enumeration fold -- the persona catalog is room-readable, so
  honesty beats indistinguishability here.
- Agents already built from a persona are untouched by construction: the
  rendered persona.md was baked into their bundle at provision time.
- **The resurrect trap, pinned:** `seed_default_personas` rewrites builtin
  index entries wholesale on every boot. The merge must preserve an
  existing entry's `archived` flag or an archived builtin silently
  returns every restart. RED-first test required.
- UI: Archive control on persona cards (shown per the caller's gate arm),
  a "Show archived" toggle revealing the archived section, Unarchive
  control there. Exact placement follows the existing persona-management
  markup, verified at plan time.
- No MCP archive tool this wave. `kobo_archive_persona` is registered as
  a candidate only if connector callers demonstrate the need.

### 4. Persona naming law + display hierarchy

**The glance test:** a stranger reading the persona's name plus its grey
who-line must know what picking it gets them. Three sanctioned patterns,
in recommended order:

1. **Trait-contract names** -- lowercase hyphenated trait words that ARE
   the behavior contract (`direct-factual-condensed-mellow`,
   `patient-thorough-teacher`). They slug perfectly under the existing
   slug law and the name doubles as documentation.
2. **Archetype names** -- an archetype word plus a qualifier
   (`stoic-mentor`, `skeptical-reviewer`).
3. **Figure-inspired names** -- named for the mannerism source
   (historical figures, characters), with two cautions encoded: invoking
   a well-known figure deliberately loads the model's knowledge of them
   (a feature, used knowingly), and the persona adopts mannerisms and
   tone only -- never identity claims, never verbatim protected
   expression.

Opaque codenames are explicitly deprecated in all guidance copy.

Where it lands:

- `kobo_create_persona` docstring: the glance test plus the three
  patterns, compressed to the docstring register (this is the in-band
  guidance every MCP caller sees at call time).
- persona-authoring skill v1.0.1: a naming section carrying the full
  taxonomy and cautions. **Publish lane note:** `create_skill` is
  create-only (collision-refusing), so the version bump lands via the
  operator SSH lane, the same lane wave-1's baseline seed and frontmatter
  fix used. An `update_skill` MCP lane is registered as a follow-up, not
  built this wave.
- PersonaBuilder: helper microcopy under the Name field teaching the
  convention with one example. House copy laws apply (no em dashes,
  vendor-clean).

Display hierarchy (the soul framing):

- Persona picker/cards: name primary, who-line as the grey secondary
  directly beneath. Largely the existing card shape -- verified and
  tightened at plan time.
- Agent card: the agent's name is the one real name; the persona renders
  as a secondary grey soul-line (persona name + who-line), never a
  co-equal title.
- `kobo_list_personas` output shape is already right (name + description
  ride together); unchanged.

### 5. Riders

- (o): the two marketplace did-not-answer constants get distinguishable
  match strings in the transport tests.
- (q): `create_skill`'s docstring says `name` is set "when absent"; the
  code sets it unconditionally. Wording made exact.

## Testing

House law: RED-first throughout; every gate arm mutation-pinned. Key
pins: each structural rule discriminates (drop the rule, a named test
fails); vocab checks configured vs unconfigured; the 2026-08-23 brick
regression; `_bounded` boundaries (64/65, marker, depth, no-mutate);
archive gate arms (team member, operator, shared operator-only, builtin
operator-only); the seeding resurrect trap; archived-persona pick refusal
through both the route and the MCP shim; include_archived default-false.

## Rollout

1. Build + roll the kobo image at wave HEAD. No DB migration -- the
   archive flag lives in the bucket index, not sqlite.
2. org.yaml gains the optional `skills_frontmatter` block only if and
   when vocabulary enforcement is wanted (image first, per the overlay
   rule). The gate is live without it.
3. persona-authoring v1.0.1 pushed via the operator SSH lane post-roll.
4. Wire acceptance (operator-provable this time, pleasantly: a
   frontmatter refusal is a validation error, not a permission denial):
   lawful publish passes; missing-version content publishes via
   completion; malformed-version content refuses with actionable copy;
   archive the three probe fixtures (wire-probe, infra-probe,
   ceiling-probe) in the UI; a create-flow pick of an archived persona
   refuses; unarchive one and it returns; `kobo_list_personas` never
   shows archived entries; picker and agent-card display hierarchy
   eyeballed in the browser.

## Follow-ups registered (not this wave)

- `update_skill` MCP lane (version-bump updates; owner-team gate,
  monotonic version law).
- `kobo_archive_persona` MCP tool, only on demonstrated connector need.
- Naming retrofits for pre-law catalog entries (the gemini-one persona):
  archive-and-recreate is the lane, operator's call per entry.

## Execution amendments

Plan-time refinements (deviations from spec letter, same intent, carried
over verbatim in substance from the plan's own header):

1. Spec said `GET /api/personas` gains `include_archived`. The room has no
   plain persona list route; every list surface is FTS-catalog-backed
   (`/api/personas/search`). Realized instead as: ingest drops archived
   entries (all active-list surfaces clean by construction) plus a new
   `GET /api/personas/archived` reading the bucket index directly for the
   UI's archived section.
2. Spec said archive controls follow "existing persona-management
   markup". No persona-management view exists. Realized as a new minimal
   Personas catalog page (Task 7), which also gives the naming-law
   display hierarchy a home.
3. The create-agent route already answers 422 "unknown persona" for
   anything not in the catalog. The honest "is archived" message is
   produced by checking the bucket index only on that failure arm (one
   storage read, failure path only).

Amendments discovered during execution, not previously written in the
spec or plan:

4. `_load_catalog_entries` in `api/kobo_api/search.py` rebuilds a fresh
   dict per entry and therefore silently drops unknown keys, so the
   archived filter required an `archived` passthrough there to be
   anything but dead code. Verified: without it, the filter leaves
   archived entries in the catalog.
5. The frontmatter gate also type-checks `description`, a required field
   the gate asserted but did not originally enforce; a non-string
   description would otherwise publish as a float, list, or mapping.
6. The semver pattern uses `[0-9]` rather than `\d`, because `\d` is
   Unicode-wide in Python and matched non-ASCII digit strings.
7. `seed_default_personas` preserves an existing entry's archived flag
   across its wholesale index rewrite; without this, an archived builtin
   silently returns on the next restart. This was anticipated in the spec
   as the "resurrect trap" and is now implemented and tested.
