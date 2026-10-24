# Operating kobo

How a room is run for real: the container, two replicas and the Slack Secret the room
rewrites at runtime, handing secrets to a secrets platform, and org wiring. The README's
Quickstart covers the laptop case.

**API (dev)**: `make -C api install`, copy `api/config.example.yaml` to `api/config.local.yaml`
and fill it in, then `make -C api run` (serves on `:8100`). The dev API is unauthenticated and
CORS-open — localhost only. **UI (dev)**: `make -C ui install` then `make -C ui dev` (`:5173`,
proxying to the API).

**Container**: one image, UI + API, from the repo-root `Dockerfile` (`docker build -t kobo .`).
No baked configuration or secrets — mount a config file and point `KOBO_CONFIG` at it, with
`ui_dist: /srv/ui` so the API serves the baked UI. Listens on `:8080`, runs non-root, with
`git`, `gh`, `kubectl`, and `gcloud` on `PATH` for the operations the API shells out to.

**Two replicas**: a room can run more than one pod once nothing it writes lives on a disk only
one pod can mount. Point `db_url` at Postgres, drop `slack_tokens_path` and the `/data` volume
with it, and set `slack_tokens_secret: <namespace>/<name>` instead. Create that Secret first,
with the two keys the room reads and rewrites:

```
kubectl -n <namespace> create secret generic <name> \
  --from-literal=config_token=... --from-literal=refresh_token=...
```

Create it imperatively, exactly like that, and leave it out of GitOps. **The room writes this
Secret at runtime**, every time Slack rotates the pair, so it must not be managed by External
Secrets, by Argo CD (self-heal or prune), by any other reconciler, or co-managed with someone
else's `kubectl apply`. Anything that reverts it puts a spent refresh token back — single-use,
already consumed — and the room is locked out of Slack until a human issues a new pair by hand.
The room's service account needs `get`, `create`, `patch`, and `update` on that one Secret; it
proves it can write it once at startup (an idempotent re-apply of the pair already there) and
logs an ERROR naming the Secret if it cannot, rather than discovering it half a day later when
the token expires.

**What the shipped ClusterRole really grants.** `k8s/cluster/rbac.yaml` binds the room to a
ClusterRole (`kobo-room`) with `get, list, create, patch, update, delete` on Secrets in **every
namespace**, plus read on pods, pod logs, namespaces, services and ExternalSecrets, `patch` on
Deployments and `create` on `pods/portforward`. That is wider than the Slack Secret above: the
room also writes each agent's colleague pass and Slack tokens into the agent's own namespace
(`<agent>-cabal`, `<agent>-slack`), and that is why the grant is cluster-wide today. The cost is
that a bug or compromise in the room can read any Secret on the cluster. The intended shape is a
Role per agent namespace granted at onboard plus one Role on the Slack Secret, with the
cluster-wide Secret rule dropped (tenishai/kobo issue #204); until that lands, run the room on a
cluster where that blast radius is acceptable, or narrow the ClusterRole by hand to the
namespaces your agents use.

Slack's refresh token is single-use, so both replicas rotate against that one object: each
re-reads it before spending a refresh token and again after a failed rotation, and takes the
other's pair when it finds one. The nightly learnings sweep is claimed by a Postgres advisory
lock, so exactly one replica sweeps per round and the other logs that it skipped; the catalog
rebuild takes one too.

Roll with `maxSurge: 1`, `maxUnavailable: 0` (a `RollingUpdate` strategy, not `Recreate`), so a
new pod is ready before an old one goes away. Give the container a `preStop` hook that sleeps
for a few seconds before shutdown — the load balancer needs that long to stop sending the pod
new requests, and without it a roll drops the connections in flight.

Get onto this version first, though, before switching the strategy. A running provision proves
it is alive by writing a heartbeat, and versions before this one never wrote one — so a new pod
booting beside an old one would read the old pod's live provisions as interrupted and fail them.
Roll this version out under the existing `Recreate` strategy (or at a moment when nothing is
provisioning), and switch to rolling updates once every pod is beating.

Three more things to expect. On a cold start straight to two replicas, both pods run migrations
and one loses the race and restarts once; it is harmless and it settles by itself. Session-level
advisory locks belong to a database connection, so route the room through a direct connection or
a pooler in session mode — transaction pooling breaks them. The MCP rate limit counts per
replica, so two pods allow twice the configured rate overall.

**Secrets platform**: a room can hand the two secrets it creates for an agent — the pass that
lets agents talk to each other, and the Slack tokens an operator pastes in — to a secrets
platform instead of writing them into the agent's namespace itself. Set all three of
`sero_url` (the platform's base URL), `sero_audience` (the audience its machine lane expects
in an identity token) and `sero_namespace` (the team namespace this room's secrets live in
over there). With any of the three missing the room writes those Secrets directly, exactly as
it always has.

What changes with it on: the values are stored once on the platform, recorded against the
person whose action produced them (the creator of the agent, or whoever is wiring Slack), and
the platform delivers them into the agent's namespace as the same Secret names the deploy
contract already names — so a running pod, a repo's `gojira.yaml`, and a deploy see the same
contract as before. The room authenticates as its own workload identity; there is no token to
configure or store, which is also why this is a deployment setting and not something an org
config hands out, and why a local room leaves it off. There is deliberately no fallback: if the
platform refuses or is unreachable, the step fails with that platform's own sentence and is
retried, rather than quietly writing the Secret a second way and leaving two sources of truth.
The room's own Slack app-config token pair (`slack_tokens_secret` above) is unaffected and
stays where it is.

What also changes is the timing, which matters on a re-wire. Delivery through the platform is
eventually consistent: the value is stored immediately, but the agent's namespace only carries
it once the platform re-reads the path. The room asks for a refresh interval of a minute rather
than the platform's hour-long default, so a rotation lands within minutes — but it is not in
place the moment the call returns, the way the direct write was. A pod restarted inside that
window can still be holding the previous tokens until the next refresh; it needs no second
action, only the wait. A step reporting success means the value is stored, not that the cluster
has caught up.

With the platform on, every agent page also carries a **Secrets and environment** section, at
the top of its Settings tab, for anyone who may edit that agent, composed and connected alike.
Secrets are held on the platform
at `apps/<agent>/env` and delivered into the agent's namespace as the Kubernetes Secret
`<agent>-env`; environment variables live in the agent's own `gojira.yaml`, for values that are
not sensitive. A secret's value is sent once and is never readable again from the room: the list
shows key names and nothing else, and there is no reveal. Key names are upper snake case, 64
characters at most, 100 per list, and the names the room fills in itself (or the container
does) are refused. The first secret an agent gets adds `<agent>-env` to `secrets_from`,
committed for an agent the room composed and proposed as a pull request for one whose
repository belongs to its team; removing the last one takes that entry back out the same way.
Every stored row shows whether the platform has actually delivered it (live, syncing, or not on
the agent yet), read off the delivery object and never off the Secret itself, and the two
secrets the room sets for an agent are listed beside them as locked rows. A change reaches the
agent at its next boot, so the panel has a **Restart agent** button that rolls its pods now. The
MCP tools `kobo_list_agent_secrets`, `kobo_set_agent_secrets`, `kobo_set_agent_env` and
`kobo_restart_agent` do the same four things. Destroying an agent
now removes its stored secrets too, both the cluster objects and the paths, which closes the gap
this section used to name.

**Upgrading**: the room runs `alembic upgrade head` at boot. Read the release's Upgrading
section in `CHANGELOG.md` first, and back up the database before any release that adds a
migration (0.2.0 was the first to add one after the 0.1.0 tag). A version from before a
migration won't start on the migrated
database; restore the backup, or run the `alembic downgrade` the Upgrading section names.

Two rules keep that boot-time upgrade safe with more than one replica (decided 2026-10-18):

- **Migrations are additive.** A release adds a column or a table; a later release removes
  what nothing reads any more. Nothing is renamed in place, so the old image and the new one
  both run against the database during a rolling update.
- **One pod migrates at a time.** On Postgres the upgrade takes a session-level advisory lock
  (`migrations/env.py`), so the second pod waits for the first and then finds the database
  already at head. SQLite has one writer by construction.

**Switchboard**: the room relays chat, teach, threads and the learnings sweep through the
switchboard (cabal) when three things are set: `cabal_url` and `agent_endpoint_template` in
`org.yaml`, and `CABAL_OPERATOR_TOKEN` in the room's environment. Mint the token with
`cbl token mint-service --sub service:kobo --scope operator --aud cabal-core` and put it in a
Secret the room's deployment reads with `envFrom` (the deploy contract names it `kobo-cabal`).
With any of the three missing, the colleagues feed and directory sync routes, and the
`kobo_feed` MCP tool, refuse with a message that names all three.

**Learnings**: the nightly learnings pass (one replica per round, under its advisory lock)
records each run in `agent_system_tasks`. `system_task_flag_after` (default 3) failures in a row
mark the agent **learnings failing** on its card; a success clears it. By default a candidate
that names nobody is held for a second confirmation; set `learnings_require_attribution: true` to
drop it instead. Do that only once every agent runs daedalus 0.5.1 or later (the release with
daedalus issue #165, which marks turns from the room's chat as `user`), or everything taught
through the room's chat is dropped too.

**Org wiring**: a room serves one org. The org's config repo carries `org.yaml` — catalogs,
models, groups, admin groups, the runtime pin — cloned at boot and merged over local defaults.
The bootstrap CLI stands up a new org (config repo, buckets, seeded personas) idempotently.
