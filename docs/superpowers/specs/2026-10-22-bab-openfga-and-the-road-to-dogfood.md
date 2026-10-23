# bab on OpenFGA, and the road to the dogfood

Date: 2026-10-22. Status: plan, agreed with Kyle; nothing built past phase 1.
Companion to `2026-10-18-one-runtime-and-admin-panel-design.md` (the panel,
its data model and the database). bab is treated as ours to build.

## What OpenFGA is, in the terms we need

Checked against the OpenFGA docs on 2026-10-22.

- **A store** holds the tuples and every version of the model. Data never
  crosses stores. One store per environment (dev, prod).
- **A model is immutable and versioned.** Every write creates a new model
  with its own id; nothing is edited or deleted. A request names the model
  it wants with `authorization_model_id`; unset, the server uses the newest.
  The docs say to pin the id in production. **This is the whole answer to
  "change the schema without breaking anything":** a new model is a new id,
  checks keep running on the old one until we flip the pin, and the flip is
  a settings change we can undo.
- **A tuple** is `user, relation, object`, optionally with a condition. A
  userset (`group:data#member`) can stand as the user, so a group's members
  get a relation in one tuple.
- **Conditions** are CEL expressions with typed parameters stored on the
  tuple or passed at check time (stored values win). They give time-bound
  grants and attribute checks without more tuples. Stored context is capped
  at 32 KB; a request at 512 KB; CEL cost at 100.
- **Contextual tuples** ride a single request and are never stored.
- **Queries:** `Check` (one question), `BatchCheck` (many at once; under
  ten questions parallel Checks may be faster), `ListObjects` and
  `ListUsers` (default deadline 3 s and cap 1000, both configurable),
  `Expand` (why, for debugging), `Read` (raw tuples, no model logic).
- **Consistency and caching.** Server-side caching is **off by default, and
  with it off every query is strongly consistent**. The optional check
  cache (`check-query-cache-enabled`, with a TTL, an iterator cache and a
  cache controller that invalidates on recent writes) trades freshness for
  latency; requests can ask for `HIGHER_CONSISTENCY` to skip it. There are
  no consistency tokens (zookies) today.
- **Datastore:** Postgres, MySQL, SQLite, or memory. `openfga migrate` on
  first install and every upgrade. Postgres can have a secondary URI for
  reads, with `HIGHER_CONSISTENCY` reads going to the primary.
- **Auth to the server:** none, pre-shared keys, or OIDC; TLS recommended.

## OpenFGA or SpiceDB

| | OpenFGA | SpiceDB |
|---|---|---|
| Model changes | immutable versioned models, pin an id, flip when ready | one live schema, writes are fully consistent, no versioned pin |
| Consistency | strong by default; optional cache with TTL and invalidation | caches by design; per-request `minimize_latency`, `at_least_as_fresh(zedtoken)`, `fully_consistent` |
| Revocation | immediate when the cache is off | immediate only on `fully_consistent`; otherwise bounded by the quantization window |
| Fit with bab | bab's catalogue already generates an OpenFGA model and its tests | would mean a second generator and a second API client |
| Operations | one Postgres database on our instance | its own datastore story and a GC window to tune |

**Decision: OpenFGA, with the server cache off.** A revoke must land on the
very next tool call, and a secrets or write lane is exactly where "a few
seconds stale" is wrong. The cost is a database read per check instead of a
cache hit, so the model and the code have to make each check cheap. That is
the efficiency rule below, not a cache.

## The efficiency rule: no cache, so the model is shallow

1. **One check is one or two tuple reads.** The dispatch question is
   `agent:<name> can_read|can_write integration:<id>`. The answer is a
   direct tuple on the integration, or a userset one hop away. No chains of
   roles into groups into roles at check time.
2. **Ceilings are enforced at write time, not check time.** A role's
   ceiling per integration (none, read, write, approve) is kobo's rule when
   it writes a grant tuple. bab holds the ceiling tuples so the rule can be
   audited and so `ListObjects` can show what a role may ever be granted,
   but the pod's check never walks them.
3. **Lists are batched.** The tool gateway asks once per session with
   `BatchCheck` (or `ListObjects` for "which integrations may this agent
   reach"), then checks per call only what it has not already answered in
   that request. A request-scoped memo is the only cache we write, and it
   dies with the request.
4. **Writes are idempotent and batched.** kobo computes the desired tuple
   set from its rows, reads the current set, and writes the difference in
   batches of up to one hundred. A sync that runs twice changes nothing.
5. **Reads go to the replica when we have one.** The Cloud SQL replica from
   the database plan serves checks; writes and `HIGHER_CONSISTENCY` reads go
   to the primary.

## The model

The catalogue bab already generates from stays; this is the shape it
generates for a room.

```
model
  schema 1.1

type user

type group
  relations
    define member: [user]

type role
  relations
    define holder: [user, group#member]

type agent
  relations
    define owner: [group#member]
    define operator: [user, group#member]

type integration
  relations
    # what the room holds the vendor credential as (write-time rule input)
    define read_ceiling: [role#holder]
    define write_ceiling: [role#holder]
    define approve_ceiling: [role#holder]
    # the lanes, one tuple per agent, optionally time-bound
    define reader: [agent, agent with non_expired_grant]
    define writer: [agent, agent with non_expired_grant]
    # what the gateway asks
    define can_read: reader or writer
    define can_write: writer
    define can_approve: approve_ceiling

condition non_expired_grant(grant_time: timestamp, grant_duration: duration, current_time: timestamp) {
  current_time < grant_time + grant_duration
}
```

What is deliberately **not** in it: kobo's group ACL (who sees an agent in
the room), which stays in kobo; cabal's dispatch pairs, which read from
this store in a later phase; and any tool-level relation, because the lane
is per integration and the gateway's own scopes narrow tools within it.

## How the three tuple sources map to kobo's tables

bab allows exactly three writers. kobo is all three for a room.

| Source | kobo rows | Tuples |
|---|---|---|
| identity sync | the directory's group membership (resolver) | `user:<email> member group:<name>` |
| topology sync | `agents`, `admin_integrations` | `group:<g>#member owner agent:<a>`; `user:<ops> operator agent:<a>` |
| console | `admin_roles` + `admin_role_groups`, `admin_grants` | `group:<g>#member holder role:<r>`; `role:<r>#holder <ceiling>_ceiling integration:<i>`; `agent:<a> reader|writer integration:<i>` |

A write path in kobo (`admin_store`) stays the source of truth; the tuple
sync is a projection with `landed` on the audit event, like every other
projection in the panel.

## Changing the model without breaking anything

1. bab generates the new model from the catalogue; its assertion tests run
   against the candidate tuples (bab's own gate).
2. kobo writes the model to the store: a new id. Nothing changes yet;
   checks still name the pinned id.
3. If the change needs new tuples (a new relation), kobo's sync writes them
   now. The old model ignores relations it does not know.
4. kobo flips the pin: `admin_settings` key `bab.model_id`, an audit event,
   and the gateway reads the pin from kobo's `/api/admin/bab` on its next
   refresh. Rollback is the previous id, one settings write.
5. If the change drops a relation, the tuples for it are deleted after the
   flip, never before.

Additive changes (a new capability, a new vendor, a new ceiling) are steps
1, 2 and 4 only. The model id, the store id and the catalogue version live
together in `admin_settings` so one read says what is live.

## Phases

- **Phase 1 (shipped, 2026-10-18):** roles, integrations and grants in
  kobo's own tables; the gateway's grants file as the projection.
- **Phase 2a:** OpenFGA deployed (its own database on the room's Postgres
  instance, pre-shared key auth, TLS, cache off); bab's generator produces
  the room model; kobo's `admin_bab` writes tuples from the rows above and
  records the pinned model id; the Connections pane shows "live in bab".
- **Phase 2b:** liminal asks bab at dispatch (tenishai/liminal#188), with
  the grants file kept as a fallback for one release; then the fallback
  goes.
- **Phase 3:** cabal's dispatch pairs read the same store.

## The order of work

1. **Finish the panel's phase 1** with the Connections pane on the
   grants-file projection (no gateway admin token needed), so every pane
   is real. Roll dev, turn `admin_panel` on, use it for a week, fix what
   reads wrong.
2. **Dogfood on our own GCP project**, the way Shaz ran his: Gojira's
   `make install`, then `stack install` for litellm, cabal, liminal and
   kobo, from the procedure on agora #54. Goal: kobo's base version up from
   nothing, by us, in a project no one hand-tuned. Every stop is an issue
   on the repo whose step stopped.
3. **Add the tenish add-ons through the panel.** On that project, connect
   sero, noto, cabal and liminal from the Add-ons pane. Today the pane only
   reads; this step gives each add-on its connect flow (the org keys it
   needs, written as the pull request the pane already promises) and is
   where other teams' add-ons get their card too.
4. **bab phase 2a and 2b** as above, on the dogfood project first.
5. **Arc 1 step 2 onward** (one runtime, published bundles), once
   daedalus's boot fetch (tenishai/daedalus#190) exists to test against.

Steps 2 and 3 come before bab because they are what bab has to secure, and
because a panel that cannot install the room's own add-ons is not a working
admin panel.
