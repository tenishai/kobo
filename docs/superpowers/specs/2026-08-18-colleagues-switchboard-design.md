# The switchboard goes live (colleagues spec A) — design

Approved in brainstorm 2026-08-18. Operator rulings: the colleagues
work ships as two specs (A: switchboard + membership, B: talk +
watch); room chat routes through the switchboard (one door, complete
conversation records); pace over ceremony, but the rollout ordering
below is non-negotiable.

Grounded by fresh code sweeps of the switchboard service (cabal) and
the runtime (daedalus); every claim below was verified against
current source, not notes.

## What ships

1. **The switchboard deployed.** The cabal service runs in the dev
   cluster as a pure relay: no LLM personas, single replica, SQLite
   on a persistent volume with the built-in backup sidecar. Verified:
   it boots fine with no identity config; only the built-in echo
   persona is seeded, which doubles as our relay smoke target.
2. **The room holds an operator pass.** At first boot the service
   prints a one-shot bootstrap code; we exchange it and mint the room
   a long-lived operator token, delivered to the room as env through
   the existing secrets path. That token lets the room register
   agents, relay chat (operator scope gets per-target tokens minted
   automatically at dispatch), and read conversations in spec B.
3. **Membership.** New checklist step between deploy and first
   contact: **"Joins the agent directory"** — the room calls the
   switchboard's graduate endpoint with the agent's in-cluster
   address, its name as the audience, and its full card (including
   the team/purpose_tags/capability metadata block, which the
   runtime serves untouched). *(Amended 2026-08-19, operator ruling:
   the step originally sat after first contact, but a locked-at-boot
   agent can only be greeted through the switchboard, and the
   switchboard can only reach agents it lists. So: join first, then
   hello rides the relay like every other conversation, and the
   hello step's capability tail re-registers the card so the
   directory copy gains the earned capability line.)*
   Destroy gains a matching stage that retires the listing, skipping
   with a note when the switchboard is unreachable. Edit details and
   Regenerate re-register after their card commit so the directory
   never drifts. A small sync route lets us re-register on demand and
   backfill the two live agents.
4. **Locks on the doors.** The composer writes the a2a auth block
   (JWKS URL, pinned issuer, audience = agent name) into agent
   configs when the org config carries switchboard coordinates. The
   two live agents get the block via the usual one-commit re-pin and
   CI roll. Verified: GET endpoints (health, card) stay open under
   auth, so probes are unaffected; an unauthenticated agent ignores
   bearer headers, so the relay can flip on before the locks do.
5. **Chat reroutes.** When the switchboard is configured, room chat
   posts the task envelope to the switchboard with a target name and
   the operator token; the switchboard relays to the agent and the
   exchange lands in its conversation records. The hello step's
   first contact travels the same way (it is just the room's first
   chat with the agent). Without configuration the legacy direct
   path still works everywhere, which is what makes the rollout
   gradual.

## New seams

- `CabalClient` beside the other clients: `graduate`, `absorb`,
  `relay`. Base URL and endpoint template come from two new org
  config keys (`cabal_url`, `agent_endpoint_template` with a `{name}`
  placeholder); the operator token comes from env. Org config is
  boot-time, so the keys land only after code that reads them is
  deployed (the house write-then-roll rule).
- `directory.py`: `sync_directory` (fetch the card from the agent's
  repo, graduate) and `retire_directory`, shared by the step, the
  destroy stage, the metadata tails, and the sync route.
- Composer: auth block emission at compose plus an in-place
  `agent.yaml` update helper for the backfill commits.

## Rollout order (each step reversible, verified safe)

1. Deploy the switchboard; mint and store the room's operator token.
2. Roll the room image that understands the new org keys.
3. Write the org keys; restart the room. Chat now relays; agents
   still accept everything, so nothing breaks if the relay misfires.
4. Register both agents; verify relay chat and the directory listing.
5. Confirm the runtime image carries the JWT dependency (joserfc);
   if absent, rebuild the runtime image first.
6. Re-pin both agent configs with the auth block; CI rolls; verify
   relay still works and a raw unsigned POST is rejected.

## Risks called out

- The switchboard's outbound relay timeout must tolerate a model
  round-trip; verified against echo plus one long prompt before the
  locks flip. If too short, fix upstream before step 6.
- The runtime's auth extra (joserfc) may be missing from the live
  runtime image; step 5 gates on it.

## Non-goals

- No discover/ask tools for agents, no conversations UI (spec B).
- No cabal code changes unless the timeout risk forces one.
- No cloud-bucket replica for the switchboard database yet; the
  volume replica ships now, hardening later.

## Testing

Unit: client request shapes; directory sync tiers (card fetch
failure leaves the step FAILED and retryable); destroy-stage
skip-note; metadata-tail best-effort re-register; chat relay
selection and fallback plus plain-copy errors; composer auth block
matrix (with and without org keys, backfill helper preserves the
rest of the file). Live acceptance: directory lists both agents,
relay chat round-trips, unsigned direct POST rejected, destroy on a
scratch agent retires its listing.
