# Persona builder v2 (addendum to Phase A's persona builder)

Operator feedback 2026-08-13: the seven-field form is confusing, its rendered
personas don't match the persona style the client org already writes by hand,
"blank" confuses, and Build-your-own hides. Decisions locked in-session.

## The form: four fields

| Field | Copy | Maps to |
|---|---|---|
| Name | same rules as today | slug + identity line |
| Who they are | "One vivid line — who is this agent?" placeholder shows the pattern: *an unflappable network detective who's seen every outage twice* | identity line |
| What they do | 2-4 sentences, plain prose | the what-you-do section |
| Rules (optional) | "One per line — things they always or never do." up to 8 | the rules section |

Cut: Role, Purpose, Tone dropdown, Tone note, Boundaries (Rules absorbs
them), Tags (auto-derived — see below). No new fields.

## The render: house skeleton

The deterministic template (same brace-safe `.replace` machinery) emits the
structure the org's hand-written personas share:

```
You are {name}, {who_they_are}.

Speak in the first person, plainly. Lead with the answer; skip
throat-clearing. Say "I don't know yet" when it's true.

## What you do

{what_they_do}

## Rules

- {rule}
- ...
```

The voice paragraph is fixed template text — structural, org-neutral, no
client branding. When Rules is empty the section is omitted (same
conditional-section rule the old Boundaries used). Existing bucket personas
are immutable objects; they keep their old shape — only new creations use
v2.

## Tags: auto-derived, field hidden

`create_persona` derives 1-6 tags server-side: lowercase alphanumeric words
from Name + Who-they-are + What-they-do, minus stopwords, longest-first,
capped at 6, minimum 1 (fallback: the persona slug). The persona API
contract (tags required, searchable) is unchanged — only who authors them.
The UI sends no tags.

## Blank persona: dropped

`seed_default_personas` stops seeding "blank" (helpful-generalist stays).
Existing "blank" objects in client buckets are removed operationally at
rollout (bucket delete + index rewrite), not by code.

## Build-your-own prominence

In CreateModal, "Build your own persona" renders as a primary-styled action
above the persona search results, not a text afterthought below them.

## Tests

Form-payload round-trip (four fields → template render golden), tag
derivation (stopwords, cap, fallback), rules-empty omission, seeding no
longer emits blank, UI factory tests for the changed payload shape.
