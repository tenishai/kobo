# Persona metadata — the directory layer (design)

Approved in brainstorm 2026-08-18, second arc of the colleagues
decomposition. Operator rulings, including two mid-brainstorm
corrections that reshaped the first draft:

- Metadata enters at CREATE time with persona-drafted defaults;
  personas stay shared templates, agents carry instance truth.
- Teams are DERIVED, not curated: the external skills pipeline is
  adding an owner axis to every published skill, and the set of teams
  in the room is the set of owners found in the skills catalog. No org
  config key, no second list to maintain, automatic alignment with the
  marketplace taxonomy.
- The capability line is EARNED, not predicted: generated when the
  agent first reaches ready, from what it actually is (persona plus
  attached skills plus tooling), not guessed from the persona alone.
- Everything is editable afterward by any signed-in user (directory
  information, not a trust boundary).
- Purpose tags are free chips, lowercased and deduplicated, drafted
  from the persona at create.

## The three fields

| field | born | source | editable |
|---|---|---|---|
| team | create station | picker: distinct catalog owners plus "shared", with a typed escape for teams that have not published skills yet | yes |
| purpose tags | create station | drafted from the persona (model call shared with the persona draft; deterministic fallback from persona tags) | yes |
| capability | first ready | one room-side model call fed the persona, attached skill descriptions, and tooling filenames; deterministic fallback: the persona description's first sentence | yes, plus Regenerate |

## Catalog owner ingestion

- The skills catalog ingest learns to CARRY `owner` when a published
  skill's metadata includes it, and tolerates its absence (nothing
  breaks before the marketplace taxonomy lands upstream).
- The distinct owner values feed the team picker dynamically: a new
  team publishing its first skill appears in the room on the next
  catalog refresh, no room change needed.
- The skills picker in the create flow shows an owner chip on each
  skill so choosing a team's skills reads as such.

## Storage, payload, display

- Agent row: `purpose_tags` (JSON text) and `capability` (string)
  join the existing (revived) `team` column. Deployed database gets
  the matching two ALTERs before the roll, per the house pattern.
- The agent payload carries all three; the Overview composition card
  displays team, purpose chips, and the capability line.
- **Edit details** on Overview: a modal (any signed-in user,
  ready-gated like every mutation) editing all three, with Regenerate
  beside the capability field re-running the generation against the
  agent's CURRENT skills and tools.

## Capability generation timing

Generated once, room-side, when the agent first reaches ready (after
the hello step's first contact), stored on the row and baked onward.
Regenerate re-runs on demand. The model call uses the existing
model-client family and prompt discipline; failures fall back to the
deterministic tier and never block readiness.

## Into the card

The composer bakes `{team, purpose_tags, capability}` into the agent
card's metadata (additive; the colleague-protocol card model allows
extra metadata, verified in discovery). Because capability is born
after compose time, the first-ready generation and every Edit
details save re-commit the card through the bundle pipeline with the
usual delivery chips, so the card colleagues will read never drifts
from what the room believes.

## Backfill

The two live agents receive their metadata by hand through the new
edit modal; no automated migration for two rows.

## Testing

- Draft and generation fallback tiers (model absent, model erroring).
- Catalog ingest owner tolerance matrix (present, absent, mixed).
- PATCH matrix for the edit endpoint (validation, normalization,
  ready gating, card re-commit trigger).
- Card-bake assertions (compose-time fields, post-ready capability
  addition, edit re-commit).
- UI: create-station component tests (picker options from a stubbed
  catalog, typed escape, chips normalization), Overview display and
  edit modal, Regenerate flow, frozen matrix additions.

## Non-goals

- No discovery or search over the metadata yet (the colleagues arc
  reads it; this arc writes it).
- No coupling to the marketplace taxonomy's schema beyond tolerantly
  reading `owner` (their release versioning work is out of scope
  here).
- No persona-record schema changes (drafting reads personas, never
  writes them).
- No automatic capability refresh on every skill change (Regenerate
  is deliberate; silent drift of self-descriptions is exactly what we
  avoid).
