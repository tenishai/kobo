# Persona v3 (drafted identity) + the teamless create flow (addendum)

Operator decisions 2026-08-14, after first real use of create-flow v2: the
Team field leaves the create flow entirely, and the persona builder becomes
one plain question the workshop answers with a drafted identity.

## Team: dropped from the flow, defaulted underneath

- The create modal loses its Team station and the builder loses its team
  gate. Nothing else changes shape: agents and personas still RECORD a team
  — the API defaults it to `shared` when the client omits it.
- Rationale: team is honor-system provenance today; nearly everything in
  both catalogs is `shared` already. Team returns in Phase B as an
  IAP-derived fact (identity → team), which is what the tools trust
  boundary wants anyway — a derived property, never a typed field.
- API: create-agent and create-persona payloads make team optional,
  default `"shared"`. Search/team filters keep working (they already
  handle the one-team world).

## Persona v3: describe the job, approve the soul

The builder's two prose boxes overlap in practice (live evidence: a real
user wrote what-they-do content into both). v3 collapses authorship into
one question and moves identity-writing to the model:

- **Form**: one required textarea — label "What should this agent do for
  you?", hint "A sentence or two, plain." — plus the existing optional
  Rules lines. Nothing else.
- **Draft step**: `POST /api/personas/draft` body `{"job": str (1-500),
  "rules": [str] ≤8 optional}` → `{"name": str, "who_they_are": str,
  "what_they_do": str}`. A Vertex text call (the namegen client family —
  same token seam, model `name_model` unless a separate `draft_model` is
  warranted; keep one config key if reuse is clean) prompts for: a
  lowercase slug-valid persona name; ONE vivid who-line (≤200 chars); 2-4
  plain sentences of what-they-do (≤500 chars) — all in the house voice,
  first person never (personas address the agent as "you"). Response
  strictly-JSON-asked, tolerantly parsed, every field validated against
  PersonaFields' real bounds; invalid fields individually fall back.
- **Fallback** (generator down/unusable): deterministic template — name =
  slugified leading words of the job text; who_they_are = "an agent whose
  whole job is {job, first clause}"; what_they_do = the job text verbatim
  (clamped to bounds). The room always drafts SOMETHING editable.
- **Preview**: the drafted name/who/what land in the SAME four v2 fields,
  now presented as an editable preview ("Here's their draft — edit
  anything.") with Create persona beneath. `create_persona`'s contract,
  validation, render template, and auto-tags are UNTOUCHED — v3 is purely
  a drafting layer in front of v2.
- The naming ceremony then feeds from the approved persona exactly as
  today.

## Station order after both changes

1 Persona (search, or describe-the-job → draft → edit → create) →
2 The Naming → 3 Skills → 4 Create. Four stations. The "Pick your team
above first." copy dies everywhere.

## Tests

Draft endpoint: parse/validation/per-field-fallback matrix, bounds
clamping, deterministic fallback, 422 on empty job. Team defaulting:
create-agent and create-persona without team → recorded as "shared"
(route + DB assertions); existing with-team payloads still honored.
UI factories: draft call pins; create payloads lose the team key.

## Non-goals

Editing existing personas; team management UI; renaming `shared`;
any change to the v2 render skeleton or tag derivation.
