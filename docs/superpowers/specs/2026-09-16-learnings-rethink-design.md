# Learnings, rethought: one sentence, one name, one tap

Supersedes the *format and surface* of Wave 1 in
`2026-09-01-learnings-ui-vision-design.md`. The pipeline skeleton (nightly
system task, human gate, approval rides the teach lane, denials tombstoned)
stands. What changes is what a learning **is**, how many reach the operator,
and what the surface looks like.

## Why (operator feedback, 2026-09-16)

The Learnings tab is confusing: every card is a long model-written
paragraph plus a source line plus a quote plus a type label; near-duplicates
arrive night after night; the "summary is guessing"; none of it reads at a
glance. It fights the room's simplicity.

Each symptom is structural:

- **Guessing**: the runtime prompt asks for "durable facts or procedures you
  learned" — an invitation to generalize. Approval then writes that
  generalization into memory byte-for-byte, and editing was deferred, so the
  only defence against a guess is Deny.
- **Duplicates**: the only dedupe is an exact sha256 of the text. A rephrase
  is a new candidate forever. The prompt is shown recalled memory but never
  what is already *pending or decided*.
- **Heavy reading**: 400-char bodies, a `fact`/`procedure` taxonomy that is
  our vocabulary not the operator's, always-visible source + evidence,
  grouped per day with per-day bulk buttons.

## Decisions (operator rulings)

1. **The human yes/no stays** — small, not a queue to work through.
2. **Anyone's statement qualifies**; attribution is the safeguard. The
   speaker's name leads every learning and the yes/no is the poisoning
   defence.
3. Built to the operator's brief: simple, understood at a glance, easy to
   add a memory.

## What a learning is now

> One plain sentence that a **person actually said or corrected**, with
> their name on it.

| field       | constraint | note |
|-------------|------------|------|
| `statement` | one sentence, ≤ 160 chars | stored in the existing `content` column |
| `said_by`   | ≤ 120 chars, who stated it as it appears in the conversation | **new column**, nullable |
| `quote`     | verbatim, ≤ 200 chars | existing `evidence` column |
| `where`     | plain origin, ≤ 120 chars ("Slack thread, Sep 4") | existing `source` column |
| `replaces`  | optional id of a **pending** learning this one refines | wire-only |

`kind` is no longer requested or shown. The column stays (nullable writes
store `"fact"`) so old rows and old runtimes keep working.

Extraction rules, enforced in the runtime prompt:

- Only something a human explicitly stated or corrected. Never the agent's
  own conclusions, never a tool result, never a generalization across
  threads. No human said it → not a candidate.
- One sentence, in plain words, specific enough to act on.
- Same topic twice in a day → one candidate.
- At most **5** per night (was 10).
- The task now carries what is already pending, remembered, or skipped
  (last 90 days, newest first, ≤ 40 statements). The agent must not repeat
  any of it; if today's conversation *refines* a pending one, it returns the
  refined sentence with `replaces: <id>`.

## Kobo side

### Envelope
`_build_learnings_envelope(on_date, known)` adds
`metadata.known = [{"id": 12, "status": "pending", "statement": "..."}]`.
Old runtimes ignore unknown metadata.

### Parse (two shapes, one wall)
`parse_reply` accepts v2 (`statement/said_by/quote/where/replaces`) and v1
(`content/kind/source/evidence`) candidates, normalizing both to one
internal dict. Agents roll runtimes at different times; both must land.
v2 caps are enforced on v2 candidates only; a v1 candidate keeps v1 caps.

### Store
In order, per candidate:
1. exact-hash tombstone check (unchanged);
2. `replaces` naming a **pending** row of the same agent → update that row
   in place (content, said_by, evidence, source, new hash; day unchanged).
   Anything else in `replaces` is ignored and the candidate is treated as new;
3. **near-duplicate guard**: token-set Jaccard ≥ 0.6 (lowercased, punctuation
   stripped, stopwords dropped) against the agent's pending + approved +
   denied statements from the last 90 days → dropped silently. Deterministic,
   no model call; the agent-side `known` list is the first wall, this is the
   second;
4. insert. Nightly cap 5 for v2 candidates, 10 for v1 (legacy).

### Approve with a fix
`POST …/learnings/{id}/approve` (and `kobo_decide_learning`) accept an
optional `content` (non-empty after strip, ≤ 200 chars, plain text). When
given, the edited sentence is what gets taught and what the row shows; the
row's original `content_hash` is kept so the original phrasing stays
tombstoned. Bulk approve never edits.

### Migration
One alembic revision: `agent_learnings.said_by VARCHAR(120) NULL`.
(Boot-time `alembic upgrade head` applies it; CI's schema tripwire proves
it.) Existing pending rows are left alone — they render in the new card,
clamped, and "Skip all" clears them in one tap.

## The surface

The tab stays "Learnings" with its pending-count badge. Inside:

```
┌───────────────────────────────────────────────────────────┐
│ Teach ledger something…                          [ Add ]  │
├───────────────────────────────────────────────────────────┤
│ ledger picked up 3 things            [ Remember all ]     │
│                                                           │
│  Suspend keeps group memberships; only deactivate or      │
│  delete removes them.                                     │
│  kgreiner · Slack thread, Sep 4          ✎  Skip  Remember│
│  ───────────────────────────────────────────────────────  │
│  CloudTrail Lake console queries take the bare event      │
│  data store ID, not the ARN.                              │
│  kgreiner · Slack thread, Sep 4          ✎  Skip  Remember│
│                                                           │
│  Remembered 12 · Skipped 3                          ▸     │
└───────────────────────────────────────────────────────────┘
```

- **One list**, newest first. No day groups, no type chips.
- Each row: the sentence (clamped to 2 lines; legacy long rows expand on
  tap), then one muted line `said_by · where`. Tapping the row reveals the
  quote. Nothing else is on screen.
- **Remember / Skip** replace Approve / Deny everywhere in the UI. One
  "Remember all" and (in the overflow) "Skip all" for the whole list.
- **✎** turns the sentence into an input; Remember then teaches the edited
  text.
- **Teach box** at the top: one line, reuses `teachAgent` (the existing
  teach lane). Adding a memory and accepting one live in the same place.
- Empty state: "Nothing new. When someone tells ledger something worth
  keeping, it shows up here."
- History is one collapsed line with two counts; expanded it lists
  sentence + who decided, nothing more.
- Viewers without `can_decide_learnings` see the list without buttons or
  the teach box (unchanged permission model).

## Runtime side (daedalus, branch `feat/learnings`, PR #26)

`memory/learnings.py`: new `_INSTRUCTIONS` implementing the extraction
rules above; `build_messages` gains `already: list[dict]` rendered as
"## Already proposed or decided (do not repeat)" with ids; v2 reply shape;
`parse_candidates` validates v2 (`MAX_CANDIDATES = 5`,
`STATEMENT_MAX_CHARS = 160`). `_handle_learnings` reads
`task.metadata["known"]`. The transcript already carries speaker names in
message text for Slack; `said_by` is optional on the wire but the prompt
requires it whenever the conversation shows a name. Lands as commits on
PR #26; the fleet gets it with the next runtime build + pin.

## Testing

- kobo: parse v1/v2/mixed/garbage; replaces → in-place update, foreign or
  non-pending id ignored; Jaccard guard (rephrase dropped, distinct kept,
  window respected); envelope `known` contents and cap; approve-with-edit
  (teaches edited text, keeps hash, rejects empty/over-long; bulk ignores);
  MCP parity for the `content` argument; migration round-trip.
- daedalus: prompt contains the rules and the already-block only when
  present; v2 parse caps and drops.
- UI: `npm run build && npm run check`.

## Explicitly not in v1

In-thread "should I remember this?" confirmation; an "updates something I
already know" marker (no delete lane in techne to honour it); Slack
notifications; a fleet-wide digest on the dashboard.
