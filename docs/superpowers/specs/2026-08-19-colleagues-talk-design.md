# They discover, they ask, you watch (colleagues spec B) — design

Approved 2026-08-19. Operator rulings: open discovery, NO allow-lists
or blocking between agents for now; conversations view lives under the
Chat section; pace over ceremony. Builds directly on spec A's live
switchboard; grounded by fresh sweeps of the switchboard's auth/guard
internals and the room's secret-delivery rails.

## What ships

1. **The colleague pass (switchboard-side).** The switchboard grows a
   second, humbler scope beside operator: `colleague` can read the new
   directory endpoint and dispatch a task to another agent, and
   nothing else (admin doors stay operator-only, pinned by tests).
   Mechanics verified in source: the dispatch re-mint gate is
   scope-only, so the colleague branch mirrors the operator one
   (re-mint to a per-target dispatch token); the guard layer gets the
   small `_require_scope` consolidation the code already wants (two
   copy-pasted guards exist; ours becomes the third consumer, not the
   third copy). Minted passes carry `sub = "agent:<name>"`, which is
   exactly what lands in the conversation records' caller column, so
   agent chatter and room-human chats are distinguishable by prefix.
2. **The directory endpoint (switchboard-side).** One read route
   listing ACTIVE agents with the card metadata the room bakes: name,
   team, purpose tags, capability line. Tolerant of both card
   envelope shapes (bare card and jws-wrapped). Colleague or operator
   scope required.
3. **Two abilities in the runtime.** A small tool provider giving
   every agent `list_colleagues` (the directory, minus itself) and
   `ask_colleague(name, question)` (a task through the switchboard,
   signed with its own pass; the answer returns as the tool result).
   Config rides agent.yaml: switchboard URL, token env var name, own
   name. Tool errors surface as plain model-visible text, never
   crashes. The provider must degrade softly when the token env is
   absent (the established token_env convention).
4. **Minting and delivery (room-side).** The room mints one pass per
   agent (its operator token can; sub `agent:<name>`, scope colleague,
   365 days) and delivers it on the proven slack-token rails: a k8s
   secret in the agent's namespace, a `secrets_from` entry in the
   deploy contract, one reprovision. New agents get the pass, the
   secret wiring, and the tools block at birth; the two live agents
   get a backfill. Destroy needs no new stage: the secret dies with
   the namespace and the directory listing already retires (the pass
   outliving its holder until expiry is accepted and noted; the
   switchboard has no revocation list by design).
5. **The watching window (room-side).** Inside the Chat tab, a
   colleague-conversations panel: who asked whom, state, snippet,
   expandable full exchange, live updates. The room proxies the
   switchboard's admin conversation surface (list, detail, SSE
   stream) with its operator token. Default filter: agent-to-agent
   traffic only (caller prefix `agent:`); a toggle shows everything
   including room-human chats.

## Rollout order

1. Switchboard image with the colleague scope deploys (additive; the
   deployed image stacks this on the two patches already running and
   both upstream PRs stay clean and separate).
2. Room rolls with mint/delivery/proxy code (inert until used).
3. Runtime image with the tool provider builds and pushes.
4. Backfill per live agent, ONE commit each: FROM bump to the new
   runtime + tools block in agent.yaml + secrets_from entry, with the
   secret applied and the pass minted BEFORE the commit lands (CI
   rolls once; the booting agent finds its pass waiting). Org runtime
   image key bumps so future scaffolds are born current.
5. Acceptance, live: an agent lists its colleagues with real
   metadata; one agent asks another a real question through the room
   chat ("ask your colleague network ...") and the answer comes back;
   the exchange appears in the conversations panel attributed
   `agent:<asker>`; a colleague pass is rejected by an admin route.

## Risks called out

- The runtime's tools config may be single-provider; the room's
  agents use none today, so colleagues can claim the slot, but the
  implementer verifies and reports (composition with other providers
  is a later problem, noted, not solved here).
- An unknown tools provider on an OLD runtime image fails the boot;
  the backfill's one-commit rule (FROM bump rides with the tools
  block) is what prevents that window, mirrored from spec A's locks.

## Non-goals

- No allow-lists, no inter-agent blocking (operator ruling).
- No multi-turn agent-to-agent conversations; ask is one question,
  one answer (the conversation record shows the full exchange).
- No revocation machinery for colleague passes.
- No changes to the jsonrpc dispatch surface (its known audience
  asymmetry is documented upstream, not fixed here).

## Testing

Switchboard: guard-refactor parity, directory auth matrix + card
shape tolerance, colleague re-mint path, colleague-cannot-admin pins.
Runtime: provider registration, both tools' happy paths and error
text, token-absent soft degrade, self-exclusion. Room API: mint call
shape, delivery idempotence, composer tools-block matrix (on/off,
byte-identical when off), proxy routes (filtering, SSE passthrough,
operator token never leaked to the browser). UI: panel rendering,
filter toggle, live-update handling, detail expansion.
