# Colleague facts earn their way in (epistemics spec B) — design

Approved 2026-08-20 on the operator's standing rulings: bold for the
operator, cautious for colleagues; quorum of same-team specialists
(starting at two agreeing, tunable); a less-qualified agent's fact
must never displace a well-sourced one just because the asker talked
to someone different today.

## What ships

1. **A third colleague tool: remember.** Alongside list and ask, the
   provider gains `remember(fact, learned_from)`: the agent's own
   model decides which colleague statements are durable facts worth
   keeping (the room's explicit-gesture philosophy, applied at the
   agent level; raw answer prose is never bulk-captured). The
   provider stores it in the agent's memory with full provenance
   (`colleague:<name>#team=<team>`) at LOW confidence, and validates
   `learned_from` against the directory.
2. **The corroboration branch in the revision pass.** The boot-time
   pass already judges contradictions and stages retirements. It
   gains a second policy branch: when a staged retirement's SURVIVOR
   is colleague-sourced, the pass seeks corroboration before acting:
   it queries the directory for OTHER agents on the asserter's team,
   asks up to a bounded number of them to verify the fact (through
   the switchboard, with the agent's own colleague pass), and has
   the resident model judge each answer as agree or disagree. At two
   total agreeing specialists (the original asserter plus at least
   one independent), it promotes, attributed
   `quorum:<n>-<team>`. Below quorum it stays pending; on explicit
   disagreement the pass leaves the candidate pending and marks
   nothing (the old fact stands, the dispute is visible in the
   staging table).
3. **The operator shield (new protective default).** A colleague
   quorum can retire colleague-sourced and seed-sourced facts ONLY.
   If the fact being retired is operator-taught (`intake:chat-*`),
   the candidate stays pending forever regardless of quorum; only
   the operator's own corrections retire the operator's facts. The
   operator can loosen this later; it ships closed.
4. **No room-side changes.** The composer's `revision: operator`
   gate already covers the pass; the conversations panel already
   shows the verification asks as normal colleague traffic; kobo,
   cabal, and the UI are untouched.

## Policy summary (one table in prose)

Operator-taught survivor: promote immediately (spec A, unchanged).
Colleague-sourced survivor vs colleague/seed-sourced stale fact:
promote at quorum, else pending. Any survivor vs operator-taught
stale fact: pending forever (the shield). Everything else: pending,
untouched, visible.

## Non-goals

- No mid-session corroboration (boot-time pass, same as spec A).
- No confidence arithmetic beyond low-at-capture (promotion does not
  rewrite confidence in v1).
- No cross-team corroboration: only the asserter's own team
  qualifies as a specialist for quorum.
- No new UI; the audit trail is the staging table and events.

## Testing

Runtime: remember tool matrix (provenance string exact, low
confidence, directory validation, self and unknown names refused,
token-absent degrade); corroboration matrix against a fake directory
plus scripted colleague answers (quorum reached promotes with exact
attribution; single voice stays pending; disagreement stays pending;
no same-team colleagues stays pending; operator-shield pins: an
intake-sourced retiree never promotes on any quorum); bounded ask
count; failure isolation throughout. Live acceptance: a second
same-team specialist agent is created through the room (a full
create-flow exercise), one agent remembers a colleague fact, its
contradiction with an older colleague-sourced fact stages, the next
boot corroborates with the second specialist and promotes with
quorum attribution; a control case against an operator-taught fact
stays pending.
