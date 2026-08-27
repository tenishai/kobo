# Resilience: Postgres, agent memory, and a restore that has been performed (arc 5)

**Status:** Implemented (code complete; provisioning and drill pending)
**Depends on:** nothing in the fast-lane and discovery arc. Deliberately independent, and can proceed alongside it.
**Repos touched:** kobo, plus cluster and cloud configuration.

## Why this arc exists

The room is becoming a production tool that other teams will rely on. The
question that prompted this was practical: if a cluster fails, or someone
deletes the wrong thing, can we get the agents back?

Today the honest answer is partial, and the parts are unevenly protected.

## What is already safe, and what is not

Verified 2026-08-24, not assumed:

| Layer | Where it lives | Recoverable today |
|---|---|---|
| How an agent was built: persona, skills, tools, config | its own GitHub repo | **Yes.** Versioned, off-cluster. Destroy archives rather than deletes, so even destroyed agents are recoverable |
| Its image | Artifact Registry | Yes, rebuildable from the repo |
| Personas and skills catalogs | object storage | **Yes.** Both buckets have object versioning enabled |
| Room metadata: ownership, ACL group, status, invites, audit | SQLite on one PVC | **No backup of any kind** |
| **What an agent learned** | per-agent SQLite on its own PVC | **No backup of any kind** |

There are no volume snapshots and no backup plans in the cluster. Neither the
room nor any agent runs a backup sidecar.

The last row is the one that reconstructs from nothing. Everything an agent was
taught, plus colleague-learned facts with their provenance, lives in a
per-agent techne database and exists nowhere else.

**The in-house precedent:** the switchboard already runs a litestream sidecar
replicating its SQLite continuously. So the platform knows this pattern; the
room and the agents simply never got it. One gap even there: it replicates to a
file target on a neighbouring volume in the same zone, which survives
corruption and pod loss but not zonal loss.

## Operator ruling

**Postgres for the room** (ruled 2026-08-26, after the cost was laid out).
The reasoning was not backups alone: the room is pinned to exactly one replica
by a read-write-once volume and a single-writer file, and no backup strategy
raises that ceiling. Managed point-in-time recovery and the ability to run more
than one replica are both worth the migration.

**And the ruling does not cover everything**, which this spec states plainly so
it is not discovered later: Postgres protects the room's database. Agent memory
stays per-agent SQLite, because moving it would mean redesigning techne, a
separate product whose per-agent shape is deliberate. Agent memory needs its
own answer, in this same arc.

## The three deliverables

### 5.1 The room on Postgres

The ORM is already the boundary for most of the schema, but two things make
this more than a connection-string change, and both are the actual work:

- **Search is not on the ORM.** The catalog is a SQLite FTS5 virtual table with
  `MATCH` queries and a hand-written FTS5 escaper that turns hostile input into
  literal phrases. Postgres means porting that to its own full-text search and
  redoing the injection hardening with equal care. The escaping tests are the
  contract to preserve, not the implementation.
- **There is no migration framework.** Schema changes today are `create_all`
  plus hand-written `ALTER TABLE` run by an operator. A move to Postgres needs
  migrations introduced as part of the work, not bolted on after.

Also in scope: the step engine's threading story. The engine runs daemon
threads against a single file today, and a second process resuming a failed
step is untested territory. Multi-replica is the point of the migration, so
this must be answered rather than deferred.

### 5.2 Agent memory backup

Every agent's techne database replicates continuously off-cluster, borrowing
the switchboard's proven continuous-replication pattern but targeting object
storage rather than a neighbouring volume in the same zone.

This is per-agent, and the pod's shape belongs to the platform, not the room:
the room composes the bundle and the Dockerfile, and that is the lever it
actually has. A sidecar container cannot ride the bundle. So replication runs
inside the agent's container, supervised by the runtime entrypoint from the
composed image, replicating the techne database on the agent's volume to
object storage under a per-agent prefix. The agent's own service account gets
write access to that prefix and nothing wider, granted by the same access
step that provisions the rest of its identity.

A new agent is born with its memory already protected. Existing agents gain
it on their next natural rebuild.

### 5.3 A restore that has actually been performed

A backup nobody has restored from is a hypothesis. The deliverable is a drill,
run end to end, that proves the chain:

1. Restore the room's database to a new instance.
2. Re-provision an agent from its repo. The room already knows how; that is the
   eight-step checklist it runs at creation. One honest caveat: that checklist
   was built to CREATE agents, not to adopt them, so its repo step will refuse
   a repo that already exists. Closing that gap is part of this deliverable,
   because a restore that needs hand-editing is not a drill anyone runs at
   three in the morning.
3. Reattach that agent's memory from its replica.
4. Ask the restored agent something only its memory knows, and get it right.

Step 4 is the acceptance. The others are setup. The drill is written down as a
runbook afterward, but the runbook is the artifact of having done it, not a
substitute.

## Testing

- Every existing test passes against Postgres. The suite is the migration's
  regression net, and a green suite on both engines is the bar.
- The FTS escaper's hostile-input tests pass unchanged in behavior against the
  new backend. Hostile queries must still land as literal phrases.
- Migrations apply to an empty database and to a copy of the live schema.
- The step engine is exercised with two processes against one database,
  including resuming a failed step.
- A restored agent answers from restored memory, which is the drill's own
  assertion rather than a separate test.

## Rollout

1. Postgres instance provisioned with automated backups and point-in-time
   recovery, private networking, and credentials delivered the same way the
   room's other secrets are.
2. Migration runs against a copy first, and the drill is performed on that copy
   before anything touches the live room.
3. Cutover copies the live rows (agents, steps, invites, refresh tokens, the
   audit log); the search table is not copied, it is re-ingested from the
   buckets. Refresh tokens ride along so every connector survives the move
   without re-authorizing. Then a config change plus a restart, with the
   SQLite file retained read-only until the drill has been repeated against
   the live instance.
4. Memory replication ships in the bundle and reaches agents on rebuild.

## Registered, not built here

- Moving agent memory itself to Postgres. Rejected: techne's per-agent shape is
  deliberate and this arc should not redesign another product.
- Multi-replica operation of the room. The migration removes the blocker; the
  actual scale-out is its own change with its own testing.
- The switchboard's own same-zone replication target, which has the same
  weakness this arc fixes for the room and the agents.

## Execution amendments

1. Postgres in tests rides a POSTGRES_TEST_URL seam (skip-with-reason when
   absent); the search module and the migration baseline were proven
   against a real Postgres during execution.
2. The escaping contract gained a backslash input after review found the
   Postgres path 500ing on it; hostile inputs are engine-parameterized and
   pass on both engines.
3. The step engine's two-process safety is a plain-SQL row-level claim,
   proven by a test that spawns two real processes against one database.
4. Replication runs in-container via a wrapper that hardcodes the runtime
   child command (an entrypoint override resets the base image's CMD, so
   nothing may rely on it). The per-agent grant is two bindings: a
   prefix-conditioned object write plus an unconditioned bucket-level
   list, the second stating --condition=None explicitly because gcloud
   refuses to guess once a policy holds any conditional binding.
5. The restore drill remains the live proof and has NOT run yet; the
   feature ships dark behind the optional memory_replica_bucket key, and
   composed bundles are byte-identical to before when it is unset.
