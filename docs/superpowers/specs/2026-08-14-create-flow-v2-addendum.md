# Create flow v2 — the naming ceremony (addendum)

Operator decisions 2026-08-14: persona-first flow; five model-generated name
suggestions after persona commit (curated-pool fallback); "name it yourself"
escape hatch; search-first skills picker with chips; visual pass that
deepens the room's own workshop aesthetic rather than replacing it.

## Flow: stations at a bench

The modal becomes a staged flow — each station renders active only when the
previous commits; later stations sit muted (the existing "pick your team
above first" idiom, generalized):

1. **Persona** — search/pick, or the promoted Build-your-own (unchanged
   v2 builder). Committing a persona unlocks…
2. **The Naming** — five name cards appear (staggered reveal). Each card:
   the name in the room's mono, plus a one-line reason (≤ 8 words) from
   the generator. A quiet `reroll` action fetches five more; a smaller
   "name it yourself" link reveals the classic input for the unconvinced.
3. **Team** — as today.
4. **Skills** — collapsed by default: search box + team filter; results
   capped at 6 with "show more"; selections render as removable chips
   above the search. No wall of skills, ever.
5. **Create** — as today, enabled by the same validity rules.

## Name suggestions API

`POST /api/agents/name-suggestions` body
`{"who_they_are": str, "what_they_do": str}` →
`{"suggestions": [{"name": str, "reason": str} × 5]}`.

- Generator: a small Vertex text call (`name_model`, default
  `gemini-2.5-flash`, same project/token plumbing as the avatar client).
  Prompt asks for short, lowercase, real-name-flavored single words in the
  spirit of a craft workshop — each with a ≤ 8-word reason tied to the
  persona. Response parsed defensively (strict JSON asked for, tolerant
  extraction applied).
- Every candidate is validated (agent-name slug rules, 2-24 chars) and
  filtered against existing agent names AND the reserved-names list;
  filtered-out slots are refilled from the fallback pool so the answer is
  always exactly five.
- Fallback (generator error or unusable output): a curated in-code pool of
  ~24 workshop/nature/myth names, sampled deterministically (seed = hash of
  the persona text — same persona, same five; no wall-clock randomness),
  availability-filtered, reasons drawn from a fixed set ("a steady hand",
  "quiet and quick", …). The room never fails to offer names.
- Route guards: 422 on empty inputs; the standard 502 body on ClientError
  ONLY when the fallback also cannot produce five (effectively never).

## Visual direction (binding for the UI task)

Kobo's aesthetic is 工房 — a craftsman's workshop: warm paper, tools that
feel hand-set, mono for identifiers. The redesign commits to that harder
instead of borrowing anyone else's look:

- **One display font, one place**: a characterful serif (self-hosted;
  suggested: Fraunces or an equivalently warm, slightly flared serif) for
  the modal's station headings ONLY ("Persona", "The name", …). Body and
  identifiers keep the app's existing stacks — mono stays the voice of
  names.
- **Paper depth, not flat cream**: the modal gains a barely-there warm
  gradient + 2% noise texture (CSS-only) so it reads as a lit workbench,
  not a flat card. No purple, no glassmorphism, nothing cool-toned.
- **The one unforgettable beat**: the name cards' entrance — a staggered
  rise-and-settle (translateY + opacity, 60ms stagger, CSS-only), like
  five tags laid on the bench. Selecting one gently presses it (scale
  .98 → 1) and dims the others. `prefers-reduced-motion` collapses all of
  it to instant.
- **Stations, not sections**: each station carries a small numbered
  woodstamp-style marker (①-⑤ rendered as bordered mono digits, not
  emoji); inactive stations sit at reduced opacity with their marker
  hollow.
- Everything else — buttons, inputs, chips — inherits existing component
  styles; chips reuse the CreateModal selected-skill chip pattern.

## Tests

API: generator parse/validation/refill matrix, reserved+taken filtering,
deterministic fallback (same persona → same five), route guards.
UI: factory tests for the new call + mutation-free suggestion fetch;
existing create-flow factory tests updated for the reordered validity
rules (name can arrive after persona).

## Non-goals

Renaming existing agents; name uniqueness beyond the existing checks;
avatar tie-in; any change to the agent-name slug rules.
