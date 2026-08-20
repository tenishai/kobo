# Teach from chat (design)

Approved 2026-08-19 (originally sketched 2026-08-17, parked pending the
memory-system upgrade, which shipped; refreshed against verified seams).
Operator rulings: explicit gesture only, never silent capture; rides the
existing intake pipeline and its honest delivery states.

## What ships

1. **The gesture.** Every bubble in an agent's chat gets a quiet
   "Teach this" action. It opens a small compose box pre-filled with
   the exchange (the user message and the agent reply that answers
   it), fully editable: the operator distills what is actually worth
   remembering, then saves.
2. **The save.** A new thin route formats the note server-side: a
   dated attribution header (taught in chat, date, the signed-in
   user when the request carries one) above the operator's text,
   written as `chat-<date>-<slug>.md` into the agent's
   `bundle/intake/` through the EXISTING intake upload seam
   (`upload_intake_file`): same validation, same commit, same sha,
   same delivery states. At the next delivery the boot seeder
   ingests it once, sha-ledgered, source-attributed; the file can
   later vanish without the memory going with it, which is the
   intake door's whole design.
3. **The confirmation.** The chat panel answers the save with plain
   copy: "Saved to memory. It arrives with the next delivery." plus
   the existing delivery-chip affordance where the intake panel
   already shows it. No new delivery machinery.

## Deliberately not in v1

- No silent or automatic capture; no agent self-reflection lane (the
  memory system's candidate rails support it; separate arc, parked).
- No chat persistence or message ids; the transcript stays
  client-side and the gesture carries the text it needs.
- No editing or retracting taught notes beyond what the intake panel
  already offers.

## Testing

API: route matrix (ready-gating, empty/oversized content, slug and
date in the filename, attribution header exact shape, response carries
filename + sha, intake seam called verbatim, no em dash anywhere).
UI: affordance renders on bubbles, prefill pairs the exchange
correctly, edited text is what posts, success and error copy exact,
modal resets between uses. Live acceptance: teach a real fact through
the room, watch delivery, then prove the memory answers from inside
the agent after its next boot.
