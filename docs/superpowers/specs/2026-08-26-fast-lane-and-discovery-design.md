# Fast lane and smart discovery (arcs 3 and 4)

**Status:** Implemented (rollout pending)
**Depends on:** Fast memory lane (live), colleagues switchboard (live), access control (live), cleanup + colleague correctness (rolled 2026-08-26)
**Repos touched:** kobo, daedalus runtime, cabal (participation only). Specced alongside, but separate from, the resilience arc.

## Why these two are one arc

Arc 3 builds a delivery lane. Arc 4 builds the things that lane must deliver.
They were scoped apart and merged after the cleanup wave's final review proved
the seam: `self.md` and `colleagues.md` are written once at agent creation and
never refreshed, so today's roster goes stale the moment any agent is created
or destroyed. Discovery is only ever as good as the roster's freshness, and the
refresh mechanism IS the fast lane. Splitting them would ship a discovery
feature on a roster nobody updates.

The resilience arc (Postgres, memory backup, restore drill) shares no seam with
either and has its own spec.

## Operator rulings

- **Tools ride the fast lane via write-plus-restart, not hot-reload.** A tool
  is code that executes inside the agent as an enforcement hook. Installing
  code in a running process is a different security event from writing a fact,
  so the enforcement loader keeps loading code exactly one way: at boot.
- **The switchboard is a prerequisite, not an option.** Agent collaboration is
  the product, and it cannot exist without cabal. A second no-switchboard write
  door would mean two authz paths and two ledgers to keep honest, which is the
  shape every bug in the preceding waves came from.
- **The agent suggests, the human decides.** An agent that is out of its depth
  names a relevant colleague and offers to bring them in. It does not silently
  delegate, and it does not add participants on its own.
- **One shared thread, capability-scoped participants.** Not a broadcast of the
  same question to everyone, which fragments answers rather than combining
  them. A frontend agent does not belong in a networking thread.
- **A thread belongs to the human who started it.** It is readable by its
  initiator only, and appears in no agent's public conversation feed.

## Arc 3: the fast lane

### 3.1 What rides it today, and what does not

The fast lane exists and is proven: a signed system task through the
switchboard, the runtime writes techne in seconds, the repo commit is demoted
to a background archive, and a sha ledger prevents double-ingest. Exactly one
of three memory write paths uses it (teach). Knowledge edits and Memory-panel
uploads still pay commit, CI image build, rollout, and seeder convergence:
minutes plus a pod rotation for what is a memory write.

Fix: knowledge edits and uploads ride the same signed system task. The runtime
writes techne directly; the repo archives in the background; the seeder's sha
ledger keeps convergence honest across the next natural rebuild. This is the
existing arc applied to two more routes, not a new mechanism.

An edit is not an append, and the lane must say so. Teach is additive: new
entries, a sha stamped in the ledger. A knowledge EDIT replaces a file whose
earlier version already produced entries, and a DELETE removes one. The
seeder's own ledger already records which entries each ingested file produced
(sha, filename, entry ids), which is exactly the machinery replacement needs:
the runtime handler retires the entries recorded for that filename's earlier
sha, stores the new content, and stamps the new sha. Delete retires the
entries and removes the ledger row. If in-process retirement proves unsafe,
the honest fallback for destructive edits is the archive lane with its
existing copy, stated plainly in the UI, never a partial fast lane that
leaves ghost entries behind.

### 3.2 Tools: fast delivery, boot-time load

Tools are code. They reach the agent the same fast way, but they are not
injected into a running process, and they cannot be written into the image's
bundle either: the container filesystem resets to the image on every restart,
so a tool written there would vanish at exactly the moment it was supposed to
load. The image bundle stays read-only truth; fast-delivered tools land in a
persistent overlay directory on the agent's own volume, beside its memory.

1. kobo validates the tool (the existing tooling contract: size, UTF-8,
   non-empty, ast parse, import allowlist, entrypoint shape).
2. It is delivered over the fast lane. **The runtime re-validates it before
   writing** (the fast lane skips CI by definition, so the runtime cannot
   assume validation already happened) and writes it to the overlay, never
   the image bundle.
3. The pod restarts. At boot the enforcement loader loads image-bundle tools
   first, then overlay tools. Loading still happens exactly once, at boot.
4. **The overlay may never shadow an image tool.** An overlay file whose name
   collides with an image-bundle tool, the baseline pack above all, is
   refused at write AND skipped with a loud log at load. Without this rule
   the fast lane would be a door for silently replacing the room's
   enforcement hooks.
5. The background archive commits the tool to the repo, so the next natural
   rebuild bakes it into the image and the overlay copy becomes redundant.

### 3.3 The refresh lane, which is also arc 4's foundation

The same delivery path carries regenerated standing knowledge. When the roster
changes (an agent is created, destroyed, or its capability is distilled), the
room regenerates `colleagues.md` for the affected agents and delivers it. The
same lane redelivers an agent's own `self.md` when its capability distills
after birth, so its self-knowledge does not stay frozen at "not yet
distilled".

Scope discipline: "affected agents" means agents that can see the changed
agent, computed with the same visibility rule the roster itself uses. A room
with N agents does not redeliver to all N on every change.

## Arc 4: smart discovery and shared threads

### 4.1 Discovery: knowing, then suggesting

The roster already reaches agents as standing knowledge (rolled 2026-08-26),
so an agent knows who exists and what each is for without spending a tool call.
This arc adds the behavior on top:

An agent that cannot fully answer, and can see a colleague whose capability
matches, says so plainly and offers: names the colleague, says why they fit,
and asks whether to bring them in. It does not ask them silently, and it does
not pretend to know.

The instruction lives in the same standing-knowledge lane as the roster, so it
is delivered, not baked into a persona the operator wrote.

**Capability quality becomes load-bearing.** Matching is only as good as the
capability text, which is auto-distilled and sometimes empty (an undressed card
with an empty team was already observed in production). Two mitigations, both
required:

- Matching falls back to purpose tags and skill ids when capability is empty,
  so an undressed agent is still discoverable.
- The room surfaces undressed cards to their owners, so they can be fixed. An
  agent nobody can match is invisible to the feature.

### 4.2 Shared threads

A thread is a conversation with more than one agent in it, where every
participant sees the same messages rather than a relayed summary. The preceding
wave's transcript shows why summaries fail: one agent summarized another
agent's own answer back to that same agent.

- **Creation.** The initiating human accepts an agent's suggestion, or adds a
  participant directly.
- **Participation.** Same-group agents may join freely (they are already
  mutually visible). A cross-group agent may join only where an accepted invite
  already exists, which is the existing consent handshake, wire-enforced at
  cabal. This arc introduces no new consent concept and no bypass.
- **Visibility.** The thread is readable by its initiator only. It does not
  appear in any participant's conversation feed. This is deliberately narrower
  than today's rule for single-agent conversations, because a shared thread can
  span groups and the existing rule would leak one group's questions to the
  other.
- **Turn-taking.** Participants answer in sequence within one thread, each
  seeing what came before, so later answers can build on or correct earlier
  ones. The room does not fan the same question out in parallel.

### 4.3 What keeps threads from looping

The colleague loop guard bounds hops within a turn (shipped in the preceding
wave, on the runtime side). A shared thread introduces an explicit participant
set, which permits the structural check that guard cannot make: an agent is
never asked to answer twice in one turn, and the thread's participant list is
the authority on who is in it.

A thread also has what plain chat lacks: a real conversation identity. Every
relay for a thread passes the thread's own id as the envelope's context id,
using the parameter the room kept for exactly this caller. The guard then
groups a thread's hops under one honest key, with none of the false merges
that made the room drop its derived chat id.

## Testing

House law: RED first, gates mutation-pinned. Specific pins this work needs:

- A knowledge edit and an upload each land in techne within seconds and do NOT
  trigger an image build. The absence of the build is the assertion that
  matters.
- A tool delivered over the fast lane is re-validated by the runtime: a tool
  that would fail the tooling contract is refused at the runtime even when kobo
  is bypassed, so validation is not merely trusted from upstream.
- The refresh lane redelivers to agents that can see the changed agent and NOT
  to agents that cannot.
- Cross-group participation without an accepted invite is refused, proven from
  a non-operator identity.
- A thread is readable by its initiator and by nobody else, including a member
  of a participating agent's own group.
- Capability fallback: an agent with empty capability is still matched by
  purpose tags.

## Rollout

1. Runtime changes ship on a branch-pinned ref; kobo ships in its image; agents
   pick up delivery changes on their next natural rebuild.
2. The fast lane's acceptance is a stopwatch: edit a knowledge file, ask the
   agent about it seconds later, confirm no image build ran.
3. The discovery acceptance needs two agents with genuinely different
   capabilities and a question squarely in one's field, asked of the other.
4. The thread acceptance must include the negative: a second person who can see
   a participating agent must NOT be able to read the thread.

## Registered, not built here

- Persona-level tuning of when an agent should defer versus attempt an answer.
- Automatic participant removal when a thread's topic drifts.
- Any change to how capability text is distilled. This arc consumes capability
  and reports when it is missing; improving the distillation is its own work.

## Execution amendments

1. The system-task rail now carries three markers (teach, knowledge, tool)
   plus a delete flag whose key is exactly `delete`; kobo and the runtime
   pin the spellings on both sides.
2. Knowledge REPLACE retires prior entries from BOTH ledgers, the intake
   ledger and the boot seeder's own, after review proved intake-only
   retirement left seeded facts live as ghosts. Deletes leave a small
   tombstone entry (the memory store never hard-deletes).
3. The seeder gained a staleness protocol (lane and previous-sha columns,
   tombstone rows) so a pod restart on a stale image can never revert a
   fast-lane edit or resurrect a fast-lane delete. Repo-direct edits that
   genuinely rebuild in still win.
4. Instant-lane archive commits skip CI on knowledge, intake, and teach
   alike; the degraded lane keeps CI because there the commit IS the
   delivery. Consequence, accepted: image bundles drift from live memory
   between natural rebuilds, which is what the staleness protocol exists
   to make safe.
5. Tools land in an overlay under the agent volume's memory home; the
   shadow refusal exists on BOTH sides (kobo refuses before relay, the
   runtime refuses at write and skips loudly at load).
6. The hello retry wraps both first-contact paths (relay and direct),
   since IAM propagation is not transport-specific.
7. The roster refresh is best-effort relay only, never a rebuild; misses
   are logged. Capability distill also redelivers self.md. The roster
   carries a skills line so the capability fallback chain is reachable.
8. Thread exclusion covers the feed, the MCP feed, the conversations list
   (including all=true), the conversation detail (not-found), and the SSE
   stream. The SSE half requires the switchboard to echo task metadata
   into its published events, which shipped as a switchboard change on
   its own branch; until that deploys, thread frames on the stream carry
   metadata only, never transcript content.
9. Intake uploads now refuse non-md filenames at the room (they were
   previously accepted and silently never ingested).
