# Cleanup and colleague correctness (arcs 1 and 2)

**Status:** Implemented (rollout pending)
**Depends on:** Hardening + stewardship wave (live), colleagues switchboard arc (live), fast memory lane (live)
**Repos touched:** kobo, daedalus runtime. Cabal is deliberately unchanged (see 2.2).

## Why these two arcs together

Both are prerequisites rather than features. Arc 1 removes leftovers from
agents destroyed before the destruction-completeness fix, plus one gap in
today's destroy lane. Arc 2 fixes the colleague behavior that issue #5
captured on the wire. Arc 2 must land before the smart-discovery arc:
encouraging agents to consult each other more, on top of today's behavior,
would amplify irrelevant hops and loops rather than help.

## The through-line

Every defect below is the same shape, and it is worth naming once because
it shapes the fixes: **the machinery already exists and is not wired.**
The loop guard is built and default-on but never receives the id it needs.
Capability data is published in the directory and formatted for the agent,
but sits behind a tool call the model can skip. Nothing here needs new
subsystems; it needs existing ones connected.

## Arc 1: cleanup

### 1.1 Orphan sweep

Agents destroyed before the 2026-08-17 destruction-completeness fix left
artifacts behind. That spec explicitly scoped out backfilling them, so
these are known leftovers, not a regression. Verified present:

- **Service accounts** (7): `agent-{sage,muse,hello,warden,clio,echo,runtime}` in the room's GCP project (full emails resolved at execution from live coordinates, never written into a tracked file)
- **Artifact Registry packages** (5): `clio`, `echo`, `hello`, `muse`, `sage`

**Explicitly out of scope, do not touch:** `iris` and `cinder`. Operator
ruling 2026-08-26: they belong to another team member, are live, and are
not kobo-built. `cabal`, `daedalus-runtime`, and `kobo` are platform
images. `ward` and `gemini-one` are live agents; their GSAs and images
stay.

Each deletion is verified against the live room first: an id is only
eligible when it has no agent row, no namespace, and is not on the
protected list above. The sweep reports what it would delete before
deleting, and the operator confirms the list once.

### 1.2 The destroy lane leaves colleague invites behind

`a2a_invites` rows are never cleaned. The destroy step list is platform,
access, slack, repo, forget (agent row + step rows) and invites are
absent. All three surviving rows reference destroyed agents (`relay` x3,
`canary` x1), including one from live `gemini-one` to dead `relay`.

Fix: destroy deletes every `a2a_invites` row where the departing agent is
either `from_agent` or `to_agent`, in the existing `forget` step (it
already owns row deletion, and the row is the terminal artifact). Existing
rows are cleaned by the same sweep as 1.1.

Cabal's own directory row is handled by the existing `absorb` call and is
not part of this change; the plan verifies that claim against a destroyed
name rather than assuming it.

## Arc 2: colleague correctness

All four defects below are visible in one transcript (issue #5): asked
which project it runs in, gemini-one asked `probe` (a test fixture),
which asked gemini-one the same question back, five hops and 51 seconds
for a question it should have answered or declined immediately.

### 2.1 Self-referential delegation

**Not a routing hole.** Self-targeting is already blocked in both
directions: `list_colleagues` filters `self_name` out of the roster, and
`_ask_colleague` refuses `target == self.self_name`. What happened is
subtler: the agent asked a *third party* a question whose subject was
itself, which no target check can catch.

Two causes, both addressed:

- The agent has no facts about itself to answer from. It knows its name
  (the persona skeleton's `You are {name}`) and nothing else.
- Nothing tells it that questions about itself are not delegable.

Fix: the composer gives every agent its own card as context: name, group,
team, purpose tags, and capability, alongside a short standing
instruction that questions about itself are answered by it, and that
"I don't know yet" is the correct answer when the fact is genuinely not
available. This is the same house line the persona skeleton already
injects, extended to self-knowledge.

Deliberately NOT in scope: giving agents live introspection tools (which
GCP project am I in, how many facts do I hold). That is a capability
question, not a correctness one, and it belongs to its own arc.

### 2.2 The loop, and why cabal needs no change

Cabal already ships `DoomLoopGuardrail`, wired as a router input guardrail
**by default** since cabal#76 (`identity.toml [guardrails] doom_loop =
false` opts out). It groups hops by `contextId` and trips on repetition.

It never fires for colleague chains because nothing threads the id. Per
cabal's own guardrail documentation: without a caller-supplied
`contextId`, "each hop falls back to its fresh task uuid," so cross-hop
detection "requires the client to thread the id." Every colleague ask
mints `ask-<random hex>` with no context, so every hop looks like a brand
new conversation to the guard.

Fix, in two places, no cabal change:

- **kobo** stamps a stable `contextId` on the task envelope when it
  originates a chat (`relay`, main.py's chat and colleague paths).
- **daedalus** propagates the inbound task's `contextId` onto the
  envelope `_ask_colleague` builds, instead of leaving it unset.

The guard then sees gemini-one to probe to gemini-one as one context and
trips on its own existing threshold.

Note the residual, recorded rather than fixed here: the guard is a
repetition detector, so a loop whose wording drifts each hop may still
run. A structural participant-cycle check belongs with the multiparty arc,
which introduces an explicit participant set to check against.

### 2.3 Irrelevant routing

`_format_colleague` already returns name, team, purpose tags, and
capability, so the data an agent needs to choose well is available. It is
available only if the model calls `list_colleagues` first, which it can
skip, and in the transcript evidently did: probe's own capability line
reads "I confirm the room's persona and agent creation system is
functioning correctly," which no reasoning process would match to a GCP
hosting question.

Fix: the roster (name, team, purpose tags, capability, one line each)
is placed in the agent's context by the composer, written once at agent
creation alongside the rest of the bundle, so the agent knows who exists
and what they are for without spending a tool call. `list_colleagues`
remains for an explicit re-read; `ask_colleague` remains the action.

Scope discipline: this is the minimum that makes routing sane, and it is
deliberately not the smart-discovery arc. Proactive suggestion ("you might
also ask X"), capability-scoped participation, and shared threads all
build on this and are specified separately.

Staleness is accepted for this arc: the roster is a point-in-time snapshot
taken once, at agent creation, and `list_colleagues` remains the live
read. There is no refresh lane today (see the Execution amendments below);
the discovery arc decides whether one is needed.

### 2.4 The 502, which is not yet diagnosed

Honest status: **root cause unknown**, and this spec does not pretend
otherwise. Ruled out by direct check:

- Not a room-side timeout. `CabalClient.relay` deliberately uses 180s
  (its docstring names the reason), not the client's 30s default.
- Not agent instability. gemini-one had 39 hours uptime and zero restarts
  while the 502s were being reported.

Live candidate: the loop in 2.2 burns a full model roundtrip per hop. The
one captured exchange took 51 seconds for a question needing none; a
slower one plausibly crosses a timeout between cabal and the agent. This
predicts that 2.2's fix reduces or removes the 502s, which is a testable
claim rather than a diagnosis.

Plan of record: fix 2.2 first, then instrument. The task is to capture one
502 with its cabal-side error and the agent-side timing, and either close
it against the loop or open a separate arc with real evidence. No fix is
specified here for a cause that has not been established.

## Testing

House law: RED first, every gate mutation-pinned. Specific pins this work
needs:

- The invite cleanup deletes both directions (`from_agent` and
  `to_agent`) and leaves unrelated invites untouched.
- The orphan sweep refuses to delete a protected id, an id with a live
  agent row, and an id with a live namespace, each as its own test.
- `contextId` survives the whole chain: a test asserts the id kobo stamps
  is the id daedalus puts on the colleague envelope. Without this the
  guard silently returns to being inert, which is exactly today's bug.
- Self-card and roster injection are pinned by content, so a future
  composer change cannot quietly drop them.

## Rollout

1. Arc 1 is operator-run and reversible in effect but not in fact: deleted
   GSAs and images do not come back. The confirm-the-list step is the
   safeguard.
2. Arc 2 spans two repos. Daedalus changes ship on its branch-pinned ref
   per locked decision #11; kobo ships in its own image. Agents pick up
   composer changes on their next delivery.
3. Acceptance: re-run the issue #5 question against gemini-one. Correct
   behavior is a direct answer or an honest "I don't know yet", with no
   colleague hop at all. Then ask something genuinely outside its field
   and confirm it reaches for a *relevant* colleague.

## Registered, not built here

- Proactive expert suggestion, capability-scoped participation, shared
  multiparty threads (the discovery arc).
- Agent self-introspection tools (which project, how many facts).
- Structural participant-cycle detection.
- Capability quality: scoping participation by capability makes capability
  text load-bearing, and it is currently auto-distilled and sometimes
  empty. The discovery arc must address this.

## Execution amendments

Decisions made during execution, not previously written in the spec or plan:

1. **The chat context id is derived, not carried.** No existing conversation
   identity was in scope at the chat route, so the id is deterministic:
   `chat-{agent}-{sha256(agent:first-message)}`. The teach envelope call
   site was deliberately skipped: teach is intercepted pre-model by the
   runtime and never reaches a colleague hop, so threading an id there
   would buy nothing. Known limit: if a very long conversation is trimmed
   by the UI, the anchor message changes and one conversation splits into
   two contexts, weakening the guard.
2. **The roster's visibility rule reuses a predicate, not the helper.**
   `allowed_pairs` could not be called directly because it requires the
   caller to be READY, which a mid-provisioning agent never is at compose
   time. The roster reuses that helper's `implicitly_paired` predicate plus
   a direct ACCEPTED-invite query, which mirrors the same rule.
3. **`gotchas.md` was deleted rather than kept beside `self.md`** (operator
   ruling). No test anywhere asserted the old deployment-chain first-contact
   phrasing, so removing it broke nothing. New agents no longer recite the
   deployment chain on first contact, which is the intended outcome.
4. **The orphan sweep's list was corrected twice against live state.** The
   original audit named agents as orphaned on the strength of their service
   accounts alone, without checking for namespaces; several turned out to
   have running deployments and were excluded by the sweep's own filters.
   One further agent was excluded entirely because its namespace is shared
   with a newly deployed agent belonging to another person, and its image is
   what that live pod would pull on restart. The report-then-confirm design
   is what caught both.
5. **The runtime half shipped on its own branch.** The daedalus change was
   cut from `main` rather than the previously pinned feature branch, because
   that branch had already merged upstream. The runtime pin moves to the new
   branch's commit at rollout.
6. **There is no refresh lane, corrected from an earlier, inaccurate
   description.** Section 2.3 above previously said the roster is
   "refreshed on the same delivery path as its other bundle content,"
   which overstated what was built. In fact `compose()` (which writes both
   `self.md` and `colleagues.md`) has exactly one caller,
   `kobo_api.steps.RepoStep`, which runs once, at agent creation. Nothing
   re-composes an existing agent's bundle. Concretely: `self.md` and
   `colleagues.md` are frozen at creation time; an agent already live when
   this arc shipped does not gain either file, and never will, without
   being recreated outright or having the two files seeded manually
   through the knowledge editor. A refresh lane (recompose-and-redeliver
   on some trigger, e.g. roster change) is registered as follow-up work,
   not built here.
