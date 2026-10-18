# One runtime, published bundles, and the admin panel: design

Date: 2026-10-18. Status: agreed direction (agora discussions #55 and #57, Kyle's
rulings of 2026-10-18); kobo's half is Kyle's team. Nothing here is deployed.
Supersedes the pane list in `2026-10-12-admin-panel-design.md`; the model and
write path there still hold.

## Why

Every composed agent is its own Docker image today, so a persona edit, a skill
toggle or a runtime bump is a build and a rollout per agent. One agent measured
25 CI runs in twelve hours for what netted to one change (agora #55). The admin
panel exists as a demo with an in-memory store; the room has no place to see
or change the plugins, grants and roles that the janus outage (2026-10-07) and
the readiness issue (#10) keep pointing at.

## Arc 1: one runtime, bundles as artifacts

### The shape

- **The runtime is one published image**, `daedalus-runtime`, named in each
  agent's deploy contract as `image:` (gojira #122), **pinned by digest**.
  Kobo writes it per agent, in the order it chooses, so a runtime bump is a
  short deploy per agent with no build, and an unplanned restart never
  upgrades an agent off schedule (Kai's point 3 on #55).
- **The agent's files are a published artifact.** After every commit that
  changes `bundle/`, kobo pushes the directory as an OCI artifact to the same
  registry, tagged with the commit sha, and moves a per-agent pointer tag
  (`<registry>/agents/<name>-bundle:current`). The runtime resolves the
  pointer at boot, pulls by digest, unpacks into the directory it already
  loads from, and logs the sha and digest (Rich's shape on #55; the daedalus
  issue carries the boot fetch).
- **Publish is not restart.** Every bundle change publishes and moves the tag,
  so a later restart loads the right thing. Only files the runtime reads once
  at boot (persona, skills, tools, wiring) trigger a restart. Files the relay
  already delivered live (teach notes, knowledge) move the tag silently.
- **Two lanes stay.** Connected repos (bring your own, with their own
  Dockerfile) keep today's build path untouched. This arc is composed agents
  only.

### What kobo changes

| Piece | Today | After |
|---|---|---|
| Composer | writes `Dockerfile`, `litestream.yml`, `entrypoint.sh` | writes `bundle/` only when the org switch is `published`; the contract carries `image:` and the bundle env |
| Contract (`gojira.yaml`) | `service_account`, `env`, `data_mount`, `secrets_from` | plus `image: <runtime digest>` and `env.KOBO_BUNDLE_REF` |
| Fast lanes (intake, teach, tooling, Slack, skills, persona, colleague mode) | commit, sometimes `[skip ci]`, re-pin `FROM` | commit, publish if `bundle/` changed, move the pointer, restart only for boot-read files |
| Runtime bump (`PATCH /runtime`, org default change) | rewrite `FROM`, rebuild, roll | rewrite `image:` in the contract per agent, in canary order; Gojira's digest receipt is the record |
| Identity | pods pull nothing from the registry | pod service account gets `artifactregistry.reader` on the agents repository (gojira `identity:` at creation); the room's service account gets writer |
| Memory replication | litestream baked into every agent image | gone from kobo; a `data_mount` option in gojira renders the sidecar (Shaz, #55) |
| Harness tab / API | stored image + room default | Gojira's deployed digest, the bundle sha the pod annotated, flagged when they disagree (#140 part 2) |

The org switch is `composed_delivery: image | published`, default `image`,
so main stays deployable at every commit and dev flips when tested. Existing
agents move one at a time through a per-agent **Move to the published
runtime** action (Harness tab and MCP tool), which publishes the current
bundle, writes the contract, removes the Dockerfile and asks Gojira to
redeploy.

### Order of work

1. `bundle_publish` module: publisher protocol, `oras` implementation, fake;
   publish-if-changed keyed on the tree id of `bundle/`; pointer move. Tests.
2. Org switch, config keys (`bundle_registry`), room service-account writer
   grant in the bootstrap. Composer emits the published shape under the
   switch; contract writer sets `image:` and the bundle env.
3. Fast lanes call publish-if-changed and the restart rule above.
4. The migrate action, per agent, with the readiness it needs (pod can pull).
5. Runtime bump as a contract write in canary order; Harness tab shows the
   deployed digest and bundle sha. #140 closes here.

Dependencies outside kobo: daedalus boot fetch by tag and digest (Rich's
team), gojira `data_mount` sidecar and the reader grant in `identity:`
(Shaz), both filed from #55.

## Arc 2: the admin panel, for real

### Entry

An account menu in the sidebar footer, where the email sits today: the email,
the room's name, **Admin** (kobo admins only, `whoami.operator`), **Guide**,
**Log out** (the identity proxy's sign-out). Same shape as a desktop app's
account menu; nothing else in the room changes for non-admins.

### Panes

| Pane | What it holds | Backed by |
|---|---|---|
| Overview | counts, what is on, what is missing a grant | the other panes |
| **Add-ons** | the room's own plumbing, one card each: deploy platform (gojira), secrets (sero), observability (noto), switchboard (cabal), tool gateway (liminal), memory (techne), and add-ons other teams ship. Each shows reachable / not configured / error, its version, and the org keys that turn it on. Absent means off. | org config keys that already exist (`sero_url`, `noto_url`, `cabal_url`, `liminal_url`, `gojira_cluster`) plus a health read |
| **Integrations** | one base credential per vendor (Slack baseline, GitHub App, Atlassian, Datadog, and a catalog): a service account or token with read or read/write, held once by the room. Connect = the credential goes to sero, the gateway gets a `[[target]]` for it, the room records the integration. | sero machine lane (`POST /service/secrets`, `POST /service/sync`), a pull request to the gateway's config repo, a new `integrations:` org key |
| Connections | every agent, its lanes per integration (read / write), granted or not, so "no lane" is visible before a reply fails | liminal `POST /admin/policy/grant` and `/revoke`, `GET /admin/policy`; a pull request to the grants file for the durable copy |
| Roles | built-ins (Admin, Operator, Member, Reader) and custom roles: capabilities, admin permissions, a ceiling per integration (none / read / write / approve), models; groups hold roles | a `roles:` org key, mirrored into bab tuples (below) |
| People | admin groups, operators, reader groups, the group-request queue | `admin_groups`, `operators`, `reader_groups`, the existing access-request flow |
| Audit | every change from the panel and whether it landed | kobo's audit log plus the pull-request state |

Every write is a pull request to the org config repo or the gateway's repo,
the lane the bootstrap already uses, except a grant applied now through the
gateway's admin plane and a credential written to sero, both of which the
pane names as applied-now.

### Add-ons versus integrations, in one line

An add-on is something the room runs on. An integration is something an agent
reaches out to. Sero is an add-on; the Datadog token stored in sero is an
integration.

### Roles with bab, in phases

bab decides actor → action → object with allow / approve / deny, from a
catalogue that generates the OpenFGA model, fed by three tuple sources
(identity sync, topology sync, console). The runtime path is one `check` at
tool dispatch, and liminal's dispatch is that chokepoint.

- **Phase 1 (kobo only).** Roles live in the `roles:` org key and the panel
  edits them. Kobo enforces what it already enforces (group ACL, operator
  gates) from the role, and writes liminal grants from a role's connector
  ceilings when a lane is granted. No bab call yet.
- **Phase 2 (bab as the store).** Kobo becomes bab's console tuple source:
  a role save writes tuples, people sync writes group membership, the
  topology sync writes agents and integrations as objects. Liminal asks bab
  at dispatch (a `check` with the agent, the tool's action and the target),
  fail-closed, and the grants file becomes a cache bab regenerates. The
  catalogue for the room (actions per integration, read / write / destroy
  tiers) is generated from the Integrations pane.
- Kobo's group ACL stays kobo's: it answers who may see and change an agent
  in the room, which is a different question (Kai's note on #57).

### Plugin deployment when connected

Connecting an integration does four things, in order, each visible on the
card: write the credential to sero under the room's app path and sync it to
the gateway's namespace as a Kubernetes Secret; open a pull request to the
gateway's config repo adding the `[[target]]` (name, address, `bearer_token_env`
naming the synced key, trust tier, scopes) so the gateway serves it after
its next roll; record the integration in the `integrations:` org key with
its base access (read or read/write); and seed the ceiling row in each role
to `none`. Disconnect reverses the four. An agent's lane is then a grant
(Connections), never wider than the base access.

### Order of work

1. Account menu and the Admin entry, admin gate on `/admin`, org switch
   `admin_panel: false`. Port the demo panes onto main behind it with the
   Add-ons / Integrations split; the store stays in-memory for one PR.
2. Add-ons pane reads real state (org keys plus health). People pane reads
   and writes the real groups through pull requests. Audit reads kobo's log.
3. `integrations:` and `roles:` org keys with loaders; Integrations connect
   flow through sero and the gateway repo; Connections through liminal's
   admin plane.
4. Phase 2 bab, after bab's team confirms the catalogue and tuple shapes.

## Arc 2: the data model and the database

Decided 2026-10-18 with Kyle. The panel's state needs a store that stays
additive as bab, liminal, add-ons and integrations grow, and the database
under it needs to be the production-grade one, not the local lane.

### One server, one database per product, one prefix per concern

- The room already runs on managed Postgres (Cloud SQL, Postgres 16, one
  database `kobo`). SQLite is the local and test lane and stays there.
- One instance per environment. One **database per product** on it: `kobo`
  as today; bab's OpenFGA store gets its own database in phase 2; sero gets
  its own when it leaves SQLite (a secrets control plane on a single replica
  with a local file is the estate's weakest point). Products never share
  tables; they talk through their APIs.
- Inside `kobo`, the panel's tables carry the `admin_` prefix. A Postgres
  schema would draw the same line but SQLite has no schemas, and the lane
  that runs the tests must run the same models.

### Additive comes from the table shape

A new vendor, a new bab capability or a new add-on setting must not need a
migration. Typed columns hold what the panes query on; one JSON body holds
what varies by kind, JSONB on Postgres, JSON text on SQLite, with a
`spec_version` so code upgrades old rows on read. A JSON Schema per kind in
code validates on write, the pattern the composer already uses for bundles.

| Table | Typed columns | JSON body |
|---|---|---|
| `admin_audit_events` | at, actor, action, object_type, object_id, landed, landed_detail | before, after |
| `admin_settings` | key (from a registry in code), spec_version, version, updated_at, updated_by | value |
| `admin_integrations` (step 3) | id, vendor, kind, base_access, state, credential_ref, created_by, timestamps | vendor fields, the gateway target spec |
| `admin_roles` + `admin_role_groups` (step 3) | id, name, builtin, timestamps; role_id, group | capabilities, admin permissions, ceilings, models |
| `admin_grants` (step 3) | agent, integration, access, state, requested_by, decided_by, timestamps | pull request link, applied digest |

`admin_settings` is the escape hatch for what no table names yet; its keys
come from `kobo_api.admin_store.SETTING_KEYS`, never free text. No table per
vendor or per plugin, and no event sourcing: the audit table with before and
after is the history.

### The database is the source of truth; outside systems are projections

This narrows the earlier "every write is a pull request". A pane writes its
row **and an audit event in one transaction** (`kobo_api.admin_store` is the
one way to write), then a sync step pushes the result where it must live: a
liminal grant, a sero secret, a bab tuple, a pull request adding a gateway
target. Every event carries `landed` (`pending`, `applied`, `failed`) and the
pane shows it: desired versus observed, the shape the provisioner already
uses. Pull requests stay for the two things that live in git, the gateway's
target file and org-level keys. Add-ons stay read-only and derived.

### Running Postgres for the long term (the deployment's side)

1. Regional availability (standby in a second zone, automatic failover),
   point-in-time recovery kept 14 days, backups copied to a second region.
2. Migrations are additive only, and the boot-time `alembic upgrade head`
   takes a Postgres advisory lock so two replicas never race (`migrations/
   env.py`).
3. Cloud SQL's managed connection pooling when the panel adds traffic, not
   our own PgBouncer; the API and worker split keeps the pool honest.
4. IAM database authentication for the room's service account, private IP
   only.
5. Noto watches the instance: connections, lag, storage, slow queries.
6. None of this becomes a code requirement: kobo works against any Postgres
   through one URL, so a self-hoster runs an in-cluster operator with
   replicas and the stack installer can offer it.

## Not in scope

Cabal as the workplace, the minder, personal agents as members (agora #57,
Rich's proposal), and any change to how connected repos build.
