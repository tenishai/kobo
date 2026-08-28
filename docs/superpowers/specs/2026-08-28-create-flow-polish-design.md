# Create-flow polish: full-length drafts, human names, recommended skills

Ruled 2026-08-28, from operator feedback on the live create modal. Three
changes, one wave.

## 1. The auto-draft writes a real persona

Today "describe the job and Kobo drafts one" produces a starter card: a
one-line who_they_are and a what_they_do capped at 500 characters. The
rendered persona.md is a thin page next to the room's hand-built personas.

Change: the draft generator writes a FULL working brief as what_they_do --
markdown, roughly 40-120 lines, in the house voice (addresses the agent as
"you"): how they work, their boundaries (what they refuse or escalate),
and a few concrete example asks. `persona_draft`'s draft-acceptance bound
(_WHAT_MAX) rises from 500 to 12000 characters -- still far under the
48KB/200-line render ceiling, which stays the one real gate. The
deterministic fallback stays short on purpose: a fallback is a floor,
never fake depth. The v2 preview shows the whole drafted body, editable
before create, exactly as it already does for pasted text.

## 2. Persona naming, third law: names are names

The trait-contract law (direct-factual-condensed-mellow) produced
lint-rule names at scale (evidence-first-risk-ranked-auditor). Retired.

- A persona name is SHORT and memorable: one or two words, slug-valid,
  the shape of a craftsman's nickname (sentinel, ledger-keeper). Never a
  hyphenated trait list, never a company or client name.
- The DESCRIPTION line carries the traits -- the glance test moves there:
  "sentinel -- cites the exact resource before judging it, ranks findings
  by blast radius, never rubber-stamps."
- Auto-derived tags are RETIRED: noun-extraction produced tags like
  "embarrassment" and "everything". Tags come from the model's draft or
  the human's hand, or the persona has none. `_derive_tags` fallbacks go.
- The marketplace persona-authoring skill must be updated to this law
  (registered follow-up; the skill publish rides its own lane).

## 3. Skills step: recommend, then search -- never scroll

The flat 50-row list with "Show 44 more" is replaced by:

- A "Recommended for this persona" strip: once a persona is committed,
  the room matches its who/what/tags against the skills catalog (the
  existing FTS) and offers the top few as one-click adds. New route:
  `GET /api/catalog/skills/recommended?who=...&what=...` -- server-side
  keyword extraction over the persona text, OR-matched against the FTS,
  merged by rank, top 5.
- Below it, ONE search field with type-ahead results (the existing
  catalog search) -- picking from results adds a chip. No browse list,
  no show-more. The modal gets shorter.

## Out of scope

- Re-naming the EXISTING personas in the catalog (the operator can
  archive/recreate the ones that grate).
- The marketplace skill-doc update (follow-up, separate publish lane).
