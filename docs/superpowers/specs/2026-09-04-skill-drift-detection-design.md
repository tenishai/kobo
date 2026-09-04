# Skill and tooling drift detection -- design

**Date:** 2026-09-04
**Status:** approved in discussion (operator), spec for review
**Motivation:** twice this week a marketplace update had to be hand-carried
into an agent's bundle with a second commit (gcp-access-audit 1.0.1 for the
AREQ board migration; audit-provenance v2 and v3). The attach-time snapshot
is working as designed -- what is missing is any visibility that the catalog
has moved.

## The ruling this design serves

Attaching a skill or a catalog tool copies the marketplace content into the
agent's repo at that moment (`bundle/skills/<id>/SKILL.md`,
`bundle/tools/<file>.py`) -- the version-pinned path. Nothing watches the
marketplace afterward. That snapshot is a FEATURE, not a bug: the publish
lane has no review gate (kobo_create_skill / kobo_create_tool are direct
commits, org-wide), so auto-sync would let any marketplace edit silently
rewrite the standing procedure memory or the enforcement law of every agent
carrying the entry -- the exact supply-chain shape the room's "nothing
auto-learns" gate exists to prevent.

Therefore: **detect and surface drift; resync only on an explicit,
gated human action; never auto-sync.**

## Scope

- **In:** marketplace-provenance skills AND marketplace-provenance tools, on
  every agent the caller can see. Detection surfaces in the Skills tab, the
  Tooling tab, and one new MCP read tool. Resync actions for both kinds.
- **Out (deliberate):** personas (an agent's persona legitimately diverges
  from its catalog ancestor -- drift flags would be noise); auto-sync in any
  form; background sweeps and notifications (a later layer if drift proves
  frequent); marketplace-side reverse lookups ("which agents carry this
  skill"); support files beyond SKILL.md (the seeder ignores them today).

## Detection

**Truth is bytes, versions are display.** An entry is `drifted` when the
sha256 of its bundle copy differs from the sha256 of the current catalog
content for the same id. Version strings from frontmatter are carried for
display (`bundle_version`, `catalog_version`) but never decide drift --
the publish gate fills version defaults and nothing forces a bump on edit.

**Where it computes.** The existing per-agent skills listing (the
reconciling `list_skills` route: provenance-preserving merge over
`bundle/skills/`, from the wave-2 fix) gains per-entry fields:

```json
{
  "id": "gcp-access-audit",
  "provenance": "catalog",
  "bundle_version": "1.0.0",
  "catalog_version": "1.0.1",
  "drifted": true
}
```

Rules:
- `provenance: "local"` entries (direct-PR additions with no catalog entry
  of that id) are NEVER flagged; they show a `local` badge in the UI.
- A catalog entry that has been REMOVED from the marketplace while the
  bundle still carries it reads `catalog_version: null, drifted: false`
  with a `catalog_gone: true` field -- absence is surfaced, not treated as
  drift (resync would have nothing to pull).
- Catalog unreachable (the existing `ClientError` degrade on this route)
  computes NO drift fields rather than false ones -- same
  never-guess posture as the route's existing no-prune degrade.

The tooling listing (`list_tooling`) gains the same three fields for
catalog-provenance files. A tooling file is catalog-provenance when its
filename matches a catalog tool id (`<id>.py`); anything else is `local`.
Baseline pack files (`baseline-*.py`) are catalog-provenance like any other
-- a baseline update showing as drift fleet-wide is exactly the visibility
an operator wants.

**Caching.** The catalog listing already sits behind the 60-second TTL
cache (`_repo_skills_cache` pattern). Per-id catalog CONTENT fetches (needed
for the hash) get the same treatment: one content cache keyed by id with the
same TTL and the same bust hooks (the three mutation routes plus
kobo_create_skill / kobo_create_tool).

**The diff endpoint.** `GET /agents/{name}/skills/{id}/drift` (and
`/tooling/{filename}/drift`) returns `{bundle_content, catalog_content}` --
the UI renders a side-by-side or unified diff before any resync. This is
also the answer to "old version or locally edited?": the operator sees
exactly what resync would overwrite, so the distinction never needs to be
inferred by the server.

## Resync

`POST /agents/{name}/skills/{id}/resync` -- re-runs the existing
version-pinned attach path for that id: fetch current catalog content,
overwrite `bundle/skills/<id>/SKILL.md`, commit, push. The agent's CI
rebuilds; at next boot the seeder's hash convergence re-seeds the changed
file and replaces the prior memory entry (verified behavior: changed hashes
re-seed, removed files drop, learned entries untouched).

- **Gate:** owner of the agent, or room operator -- identical to
  `set_agent_skills`, because it IS that operation.
- **Truthful no-op:** byte-identical content returns the current head sha
  without an empty commit (the existing write path already behaves so).
- **Refusals:** a `local` entry (404 -- nothing to resync from), a
  `catalog_gone` entry (409 naming the situation), agent not READY (the
  existing conflict answer).

`POST /agents/{name}/tooling/{filename}/resync` -- same shape via the
tooling write path with the catalog tool as source. This requires fixing a
known route defect: the add-tool route currently REFUSES a same-name write
("already exists... delete first") even though its own contract documents
silent replacement. The fix: a catalog-sourced write to an existing
catalog-provenance filename overwrites (this is resync); an INLINE write to
an existing filename keeps the refusal (protecting a live enforcement hook
from accidental clobber stays correct). The tooling validators
(`_validate_content`: size cap, AST, import allowlist, entrypoint) run on
the incoming catalog content exactly as on any write.

## Surfaces

**Skills tab:** drifted entries get an amber chip reading
`v1.0.0 -> v1.0.2` (or `differs from catalog` when versions are equal but
bytes are not). Click opens the diff view; its single action is
"Resync from catalog", enabled per the gate. `local` entries show a neutral
`local` badge; `catalog_gone` a gray `removed from catalog` badge. No
em dashes in any copy (the copy test's law); use `->` and `--`.

**Tooling tab:** identical treatment on catalog-provenance files.

**MCP:** one new READ tool, `kobo_list_drift`:
- input: optional `agent` (default: every agent the caller can see);
- output: rows of `{agent, kind: "skill"|"tool", id, bundle_version,
  catalog_version, drifted, catalog_gone}` -- only rows where `drifted` or
  `catalog_gone` is true (an empty list means no drift anywhere visible);
- resync via MCP rides the EXISTING write tools, documented in their
  docstrings: `kobo_set_agent_skills` `add` of an attached id = resync;
  `kobo_add_agent_tool` with `tool_id` of an attached catalog tool = resync
  (post-fix). No new write tool.
- The counts law: this adds exactly one read tool, so every stated total
  (server instructions, docstrings, the count tests) moves by +1 read in
  the same change -- the implementation takes the CURRENT numbers from the
  code, never from this spec.

## Error handling summary

- Catalog unreachable: listing serves without drift fields; diff endpoint
  502s with the client error named; resync refuses rather than writing
  stale content.
- Malformed catalog content on tool resync: the tooling validators refuse
  it with the field named -- an unlawful catalog copy can never land on an
  agent through resync.
- Concurrent resync: last write wins on the same file; both are the same
  catalog content within a TTL window, so the race is benign.

## Testing

- Drift computation: identical content -> false; catalog moved -> true;
  version-equal-bytes-differ -> true with equal version display; local-only
  -> no fields; catalog removed -> catalog_gone; catalog unreachable -> no
  fields, no prune, no 500.
- Resync: overwrites and returns new head; byte-identical no-op; local 404;
  catalog_gone 409; gate (owner ok, stranger 403, operator ok); tool resync
  runs validators; inline same-name write still refused while catalog
  same-name write overwrites.
- MCP: kobo_list_drift visibility rules (caller sees only their agents'
  rows; operator sees all); empty-list truthfulness; counts updated
  everywhere (grep-level test exists for the stated totals).
- UI: chip renders on drifted, badge on local/gone; diff modal fetches the
  drift endpoint; copy test stays green (no em dashes).

## Follow-ups this design deliberately leaves behind

- Background drift sweep + room feed notification when a baseline-pack tool
  drifts fleet-wide.
- Bulk resync ("update all agents carrying X"), which wants the
  marketplace-side reverse lookup that is out of scope here.
- Persona lineage visibility (a different design conversation).
