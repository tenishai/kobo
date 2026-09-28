---
card: arch-card:kobo-switchboard
updated: 2026-09-28
anchor: api/kobo_api/pairs.py sha=c415c7c022bdd2b2d238c233d0badb6bc4f0a7bf
anchor: api/kobo_api/clients.py sha=9821ad6907c2d2175a1a44d85f8c3da34639418d
---
# switchboard — kobo ↔ cabal: directory, relay, colleague passes, dispatch policy

WHAT / SEAM: `CabalClient` (clients.py, l.~4052) speaks cabal's admin API with the operator bearer `CABAL_OPERATOR_TOKEN` (env, read live, never an org.yaml key). kobo is the POLICY OWNER; cabal stores and enforces a flat set. Policy computation: pairs.py `implicitly_paired`, `allowed_pairs`, `sync_dispatch_pairs`, `sync_dispatch_pairs_best_effort`. Wrappers: directory.py `sync_directory`/`retire_directory`, colleagues.py `deliver_pass`. On/off: `cabal_enabled(config)` = `cabal_url` AND `agent_endpoint_template` AND the env token; also check `clients.cabal is not None`.
OPS / FLOW (cabal endpoints):
- `POST /admin/persona/{name}/graduate` {endpoint, audience=name, agent_card_json} — DirectoryStep; the card is read verbatim from the agent repo clone, never recomposed. `POST .../absorb` on destroy (404 tolerated).
- `POST /admin/token/revoke` {sub: agent:<name>} THEN `POST /admin/token/mint-service` {sub: agent:<name>, scope:["colleague"], aud:"cabal-core", ttl_days:365} — `deliver_pass` in WireStep; token lands as Secret `<name>-cabal`, key `CABAL_COLLEAGUE_TOKEN` (via ladon when configured, else `kubectl.apply_secret`).
- `POST /a2a/tasks` {target, task} (180s timeout) — every room chat, teach (`_build_teach_envelope`), learnings sweep, hello, roster refresh, threads.
- `PUT /admin/dispatch-pairs` {pairs:[[caller,target],...]} → {count}.
- `GET /admin/conversations[?limit,target,caller]`, `GET /admin/conversations/{task_id}`, `GET /admin/events/stream` (SSE, async client) — the colleagues feed.
INVARIANTS:
- Pair shape is `("agent:<caller>", "<target>")`: caller prefixed, target bare. Directional; an accepted `A2AInvite` adds exactly one direction.
- Implicit pairs: same non-null `group_name` on both sides AND not `SHARED_GROUP`. WHY: the commons is visible to everyone but its wiring stays consent-based. Group-less agents never pair. Only READY, non-connected agents appear, as caller or target.
- EMPTY pair set = WIDE OPEN in cabal (by cabal's design). `put_dispatch_pairs` still pushes it (a real revoke-all must not be swallowed) but logs a WARNING when shrinking from non-empty. Never add "skip if empty".
- Every push sends the FULL current set, recomputed from DB rows. WHY: that is the reconciliation story; a lost push heals on the next trigger. Triggers: READY flip (provisioner.py), invite accept/revoke and convert-to-connected (main.py), destroy forget step (destroy.py), process boot (`_boot_sync_dispatch_pairs`), operator `POST /api/admin/sync-pairs` (raising core, so the operator sees failure). All others use `_best_effort` (swallows `CabalError`/`httpx.HTTPError`; state already committed).
- STALE CLAIM IN CODE: pairs.py `sync_dispatch_pairs_best_effort` says `group_name` is immutable after create, so there is no group-change trigger. Since 2026-09-24 (commit 1ff5e6e) an operator can move an agent via `PATCH /api/agents/{name}/metadata {group}` (main.py ~l.8038-8100, also MCP `kobo_move_agent`), and that route does NOT resync pairs. Cabal keeps enforcing the old group's implicit pairs until some other trigger fires (read from code, not observed live).
- Revoke BEFORE mint on every re-wire (2026-09-24). WHY: cabal refuses tokens issued before the cutoff; minting first would let the new pass fall to its own revocation. A revoke failure stops the step before minting.
- Minted `scope` is always `["colleague"]`, never `operator`. Tokens and response bodies never appear in raised messages.
- `list_conversations` without `limit` is cabal's default 50 rows, clamped at 200, not unbounded.
- `/broker` accepts the same colleague JWT (aud `cabal-core`) verified against cabal's JWKS (pr_broker.py `verify_agent_token`); confused-deputy tradeoff accepted and documented there.
CONSUMERS: api/kobo_api/steps.py (WireStep, DirectoryStep, HelloStep, `_deliver_knowledge_refresh`), provisioner.py, destroy.py, learnings.py, main.py (chat, teach, threads, invites, colleagues feed/stream, admin resync), mcp/tools/read.py (conversation feed), composer.py (bakes cabal JWKS/issuer/audience into agent.yaml). Cross-repo: cabal's admin API (tenishai/cabal) is the other side; daedalus runtime reads `CABAL_COLLEAGUE_TOKEN` via `tools.colleagues.token_env`.
API: see endpoints above; kobo-side `allowed_pairs(session, config) -> set[tuple[str,str]]`, `sync_dispatch_pairs_best_effort(clients, config, session) -> int | None`, `CabalClient.dispatch_pairs_dirty`.
