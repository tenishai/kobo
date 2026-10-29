# Changelog

All notable changes to kobo are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- **AWS connects as an organization with accounts under it.** Add integration › AWS creates the hub role in the organization's management account (trusting the room's identity; allowed only to assume each member account's role, list the accounts, read the bill). **Add account** on the organization lists its accounts through the hub; the account's role trusts the hub with an external id of its own; the room probes the two hops before saving. Integrations shows organizations with their accounts indented; the Permissions picker names each account's organization and offers the organization's other accounts as not connected yet. A second organization is a second row. The trust policies the page renders condition on `oaud`, which is where AWS puts a Google token's audience; the earlier `aud` condition refused the room's own token. Flat roles keep working. New: `GET /api/admin/integrations/{org}/accounts`; the setup page takes `mode=org|account`, `parent`, `account_id`.

## [0.3.5] - 2026-10-29

## [0.3.4] - 2026-10-29

### Changed

- **The admin panel follows the permission service.** Connect AWS names the permission service's identity as what the role must trust, asks it to try the role before saving, and connects the account there (the ceiling is every read permission the catalogue offers for the vendor); other vendors connect there too. Disconnect takes the connection out of it, revoking the grants on it. Integrations lists connections the service holds that the panel never made. Connections is shown only while the service is off. Review opens with **Borrowed now**, the credentials out in the room at this moment (`GET /api/admin/access/leases`), and Overview counts them.

### Changed

- A base image, `ghcr.io/tenishai/kobo-base` (`docker/base/Dockerfile`, workflow `base-image.yml`), carries git, gh, kubectl and gcloud on its own cadence, and the product image builds on it by digest (`ARG BASE_IMAGE`), so a release does only the Python and UI layers.
- **Faster release builds.** The UI build and the lockfile export run once on the build host instead of once per target architecture (under arm64 emulation they were most of a release's build time), and unchanged layers come from a registry cache (`ghcr.io/tenishai/kobo:buildcache`) instead of being rebuilt. The image's contents do not change.

## [0.3.3] - 2026-10-29

### Changed

- **The Permissions picker walks vendor, account, service.** The first level is the vendor (AWS, Google Cloud, Azure, Datadog, Atlassian, ...); a cloud then lists its accounts, projects or subscriptions (connected, or listed in the new org key `cloud_accounts` and not connected yet); a service vendor opens straight on its permissions. The last level is a searchable list of services with read access and one **Confirm request**; write tiers are not offered there. Ticking a service no longer closes the list (the first version re-derived the accordion's open state on every click). `GET /api/access/picker` now returns `vendors` with `accounts` and the vendor's permission tree; the agent's Access payload carries the same `vendors`.

## [0.3.2] - 2026-10-29

### Added

- **Access through bab.** With `bab_url` in the org config and the console token in `KOBO_BAB_TOKEN`, the room is the console for its permission service: a **Permissions** section on the agent page (what the agent holds, with tier and expiry; the vendor's permission tree to ask for more, anything above the connection's ceiling greyed with the reason; Revoke now for admins), a **Review** pane under Admin (requests waiting, bulk Approve, Deny with a reason, Sync agents), borrowed credentials under Activity, and an **Access** row on Services. The room tells the service which agents exist (at boot, after every create and destroy, and from Review) with the pod identity that is each one, so a lease asked for by the pod lands under the agent's name. Routes: `GET /api/access/picker`, `GET /api/agents/{name}/access`, `POST /api/agents/{name}/access/requests`, `DELETE /api/agents/{name}/access/grants/{id}`, `GET /api/agents/{name}/leases`, `GET /api/admin/access/requests`, `POST /api/admin/access/decide`, `POST /api/admin/access/sync`. Everything is off, and the room runs as before, without `bab_url`.

## [0.3.1] - 2026-10-28

### Added

- A release workflow: a `vX.Y.Z` tag builds the multi-arch image (linux/amd64, linux/arm64), publishes it to ghcr under the tag and the short sha, and records the digest on the GitHub release. No image is built on a push to main.

### Changed

- Shared threads talk to one agent at a time (kobo #240). A turn goes to the thread's primary, the agent it was started with, or to the agent you pick (a "To" box, or `@name` at the start of the message); what that agent asked its colleagues while answering shows under its reply. "Ask everyone" is the old everyone-answers behaviour, on request. An agent that does not answer is a "didn't answer" entry and the turn still completes, where one down agent used to fail the whole turn with a 502. API: `POST /api/threads/{id}/turns` takes `to` and `broadcast`; each reply carries `error` and `consults`, and the response names `to`.

- The image is pinned end to end: base images by digest, Python packages from `api/uv.lock` with hashes verified by pip, kubectl by version with its checksum, gh and gcloud from per-arch apt repos.


## [0.3.0] - 2026-10-27

### Upgrading

- **Four additive migrations**, run at boot: `a1b2c3d4e5f6` (admin store), `b7c8d9e0f1a2`
  (integrations, roles, role groups, grants), `c4d5e6f7a8b9` (MCP redeemed codes and
  revocations) and `d7e8f9a0b1c2` (`agents.pass_minted_at`). The alembic head moves from
  `a8e2c4f6d913` to `d7e8f9a0b1c2`. Back up the database first; to go back, restore the backup
  or run `alembic downgrade a8e2c4f6d913`.
- **Check the config file before rolling.** `Config` and its `mcp:` block now refuse unknown
  keys, so a typo or a key from another product stops the room at boot instead of being
  dropped. `runtimes` is the one retired key still accepted, with a warning.
- **An open room must say so.** A room with no `iap_audience` now logs an ERROR at every boot
  unless `allow_unauthenticated: true` is set, and an MCP server on such a room (any
  `mcp.oauth_client_id`) refuses to start unless `mcp.allow_without_iap: true` is set too.
  Deployed rooms behind IAP change nothing.
- **New optional org keys**, all off when absent: `admin_panel` (the admin panel and the
  account menu's Admin entry), `logout_url` (the account menu's Log out, for Google's proxy
  `/_gcp_iap/clear_login_cookie`), `gateway_config_repo` and `gateway_namespace` (where a
  connected integration lands), and `liminal_admin_url` (the tool gateway's admin plane). The
  admin plane is used only when the switchboard is wired too, because the room's own gateway
  token (persona `kobo-room`, `admin:*`) is minted there: list `kobo-room` as an admin persona in
  the gateway's trust registry. The Activity view needs a gateway that writes its audit rows
  (tenishai/liminal#211); the AWS connect check needs `aws_identity` and `aws_probe` (liminal
  #209).
- **New deployment keys for ergon-style rooms**: `delivery_backend: directory` with
  `directory_root` (and optionally `directory_reload_url`), and `storage_backend: directory`
  with `storage_root`. The defaults (`deployed`, `gcs`) change nothing.
- **Names whose namespace already exists are refused** at create and connect (409), and the
  reserved names grow (see Fixed).
- For the api/worker split, apply `k8s/overlays/api` and `k8s/overlays/worker` (see
  `docs/operating.md`); `k8s/base` is still the one-replica shape.

### Added

- **"What kobo is", a guide section for an assistant explaining the room to its person**
  (`api/kobo_api/templates/guide/12-what-kobo-is.md`, id `what-kobo-is`): what the room is,
  where it sits beside daedalus, gojira, techne, cabal, liminal and sero (required or optional),
  what it can do, and every choice a person makes with why they would make it. The glossary
  moves to `13-glossary.md` (its id is unchanged). The `kobo_guide` tool's description now says
  to start there for "what is kobo", and its listing hint names it. README gains a "Where this
  fits" section.
- **Admin panel, Connections for real** (PR #191, issue #173 step 3). A lane is a grant row
  (`read` or `write`): `GET /api/admin/connections`, `PUT` and `DELETE
  /api/admin/connections/{agent}/{integration}`. Applying one recomputes the agent's whole
  gateway grant from its rows, merges the handlers the gateway already holds for that persona,
  writes it live through the gateway's admin plane with the room's own token, and rewrites the
  persona's block in the gateway repository's grants file as a pull request. Every agent is a
  row, so "no lane" is visible. New org key `liminal_admin_url`; empty means the pull request
  only.
- **Admin panel: reload the org config without a restart** (PR #187, issue #185). `POST
  /api/admin/org/reload` merges the org config over the boot-time local config again and swaps
  the live config, so a new model, group or people list lands at once; the response names what
  still needs a restart (add-on URLs). The button is on the Services page.
- **The admin panel behind a switch, and an account menu** (PR #174). Org key `admin_panel`
  (default off) turns on `#/admin`; its routes answer 404 while it is off and 403 to anyone who
  is not an admin. The sidebar's bare email becomes an account menu: the email, **Admin** (admins
  only, while the panel is on), **Guide**, and **Log out** when `logout_url` is set.

- **Activity: what an agent did through the tool gateway** (Kyle's ask 2026-10-25). A
  new Harness section on every agent, connected ones included, lists each call the gateway
  admitted and each one it refused, newest first: the tool, the outcome, the duration, the
  arguments in brief with secrets masked, and for a refusal what was missing. Filter by
  tool or widen the window to 30 days. Read live from the gateway's audit through its admin
  plane (`GET /api/agents/{name}/activity?days=`); the gateway writes the rows since
  tenishai/liminal#211, and without an admin plane the section says so instead of showing an
  empty list. Guide section 08 describes it.

- **Connect AWS without a key** (design 2026-10-25, cloud access for integrations). The
  Integrations page's AWS entry is now a sheet: see what the role allows (every read-only
  pack: `inventory`, `logs`, `security-audit`, `cost`; what each agent may do inside it is
  set per lane), create the role one of three ways
  (a Terraform module snippet, the CloudFormation template with its parameters
  pre-answered, or the trust policy alone), paste the role's ARN. The room asks the gateway
  to assume the role once before saving it, so "connected" is said only after AWS agreed;
  a refusal comes back as AWS's sentence and nothing is written. The account lands in the
  gateway's trust registry as `[tool_extras."aws.roles".<id>]`, not as a target, and agents
  reach it through the gateway's `aws` handler once a lane grants `<id>:read`. The page is
  rendered from a pinned release of `tenishai/cloud-access` vendored under
  `api/kobo_api/vendor/cloud_access/` (`VERSION` names it), so it works while that repo is
  private. New route `GET /api/admin/integrations/aws/setup?packs=`; the gateway admin
  client learns `aws_identity` and `aws_probe` (liminal #209).

- **A directory delivery backend** (issue #207, for ergon). With `delivery_backend: directory`
  and a `directory_root`, create composes the same bundle it would push to a repository into
  `<root>/<agent>/` in one rename, keeps the picture in the room, lists the agent in the
  switchboard directory when one is wired, and tells the running set: a POST to
  `directory_reload_url` with the agent's name and folder, or a `.ready` marker in the folder for
  a watcher. Destroy for that kind removes the folder and the listing and nothing else. A name
  whose folder already exists is refused at create. `deployed` stays the default.
- **A directory-backed catalog store** (issue #208, for ergon's laptop and team tiers). With
  `storage_backend: directory` and a `storage_root`, each "bucket" is a folder on disk and a key
  is a relative path under it; custom metadata rides in a `<key>.meta.json` sidecar. The persona
  and skill catalogs, the built-in persona seed at boot and create all work with no GCP
  credentials. `gcs` stays the default, so deployed rooms change nothing.

### Fixed

- The room's own INFO boot lines (the auth regime, the dispatch-pairs sync, the built-in persona
  seed) now reach the pod log: the deployed entry configures a handler for the `kobo_api` loggers.
  Library loggers are untouched.
- **A destroy is claimed, beats, and is swept when its pod dies** (issue #203). The DELETE route
  now takes the row with one conditional update (a second DELETE while a destroy is in flight
  answers 202 and launches nothing; a sweep racing it loses the claim), the destroy thread writes the same heartbeat a provision does, and
  the interrupted sweep flips a dead destroy to DESTROY_FAILED, which the next DELETE resumes. The
  waiting sweep claims a row with `UPDATE ... WHERE status = 'waiting'`, so it can no longer flip
  a row a DELETE just took back to provisioning and run the pipeline over the teardown.
- **A crash between an external create and its commit no longer strands the agent** (issue #201).
  `RepoStep` records the repository name and commits before asking GitHub to create it, so a retry
  finds the row naming the repo instead of refusing to adopt it. `OnboardStep` commits the platform
  app id the moment it exists, before the long wait for `active`, and when the platform reports a
  conflict for an agent with no recorded id it adopts the app the platform holds under the agent's
  own name and repository (the room's own), raising as before when there is none.
- **Shutdown drains inline pipelines too, and the split shape ships as overlays** (issue #202).
  Create, connect and retry now register the pipeline thread they start, so `drain_pipelines`
  waits for it like it waits for the worker's; before, only queue-started runs were drained and a
  roll in role `all` killed a running build at the grace period. `k8s/overlays/api` and
  `k8s/overlays/worker` carry the role, replica count, strategy and grace period the
  documented api/worker split needs, and `docs/operating.md` says which shape `k8s/base` deploys.
- **The PR and Jira brokers refuse a colleague pass the room has since replaced** (issue #199).
  A re-wire revokes the earlier passes at the switchboard, but the brokers verified a pass by
  signature and claims alone, so a pass that leaked from a pod kept minting GitHub tokens for the
  rest of its year. The room now records when it minted each agent's pass (`agents.pass_minted_at`,
  migration d7e8f9a0b1c2) and both brokers refuse a pass issued before that instant. The JWKS
  client is built once per switchboard instead of once per call.
- **A connected repository's CI log and environment are operator-only, and build logs are
  scrubbed** (issue #200). The room cannot check who owns a repository on GitHub, so whoever
  connected it became its owner in kobo and could read its CI output and `gojira.yaml` env.
  For connected agents those two reads now follow the operator gate (with its dev-parity arm);
  composed agents are unchanged. Every build log is run through `kobo_api.redact` first, which
  masks well-known token shapes, private-key blocks, bearers and values after secret-ish keys.
- **MCP authorization codes are spent once, and a person's MCP access can be cut off**
  (issues #196, #197). A code exchanged a second time within its minute of life now fails and
  revokes the tokens the first exchange produced; before, each exchange minted an independent
  refresh family. A refresh family now ends after thirty days, so the next refresh sends the
  person back through Google and the Workspace check. Admins can cut one person off from the
  admin API (`POST /api/admin/mcp/revocations`): every family they hold is revoked at once and
  access tokens issued before that instant are refused until they expire. Two additive tables
  (`mcp_redeemed_codes`, `mcp_revocations`, migration c4d5e6f7a8b9).

### Added

- **Refresh an attached skill in one commit** (issue #182): `POST .../skills/catalog` with
  `refresh: true`, and `kobo_set_agent_skills(refresh=[...])`, move an attached catalog skill to the
  version the catalog serves now in one commit and one roll, instead of a remove and an add. A
  skill the agent does not carry, or a local skill, is refused.
- **Admin panel, Roles for real** (issue #173 step 3): `GET /api/admin/roles` seeds the four built-ins
  (Admin, Operator, Member, Reader) from the org keys on first read and returns every role with the
  catalogue the editor offers (capabilities, admin permissions, the room's models and groups, the
  connected integrations); `PUT`/`DELETE /api/admin/roles/{id}` save and delete custom roles, every
  change in the audit log. A built-in cannot be deleted.
- **Admin panel, Integrations connect for real** (issue #173 step 3): `GET/POST /api/admin/integrations`
  and `DELETE /api/admin/integrations/{id}`. A connect records the row, stores the credential in the
  secrets add-on under `apps/kobo/integrations/<id>` synced into the gateway's namespace, and opens a
  pull request on the gateway's config repository adding the vendor's `[[target]]`; the card says how
  far that has gone. Two org keys: `gateway_config_repo`, `gateway_namespace`. Disconnect reverses it.
- **Admin panel tables for integrations, roles and grants** (issue #173 step 3): `admin_integrations`,
  `admin_roles`, `admin_role_groups`, `admin_grants` (migration `b7c8d9e0f1a2`), with store functions
  that write the row and its audit event together and refuse a lane wider than the integration's
  base access, an unknown role body key, or deleting a built-in role. No routes yet.
- **Admin panel, People and Audit read real data** (issue #173): `GET /api/admin/people` returns the
  room's admin groups, operators, reader groups and Slack operators from the org keys; `GET
  /api/admin/audit` returns the store's audit events newest first with their `landed` state.
  The People pane decides the real group-request queue; both panes say where a list is edited.
- **Admin panel store** (issue #173, data model): two additive tables, `admin_audit_events` and
  `admin_settings`, JSONB bodies with a `spec_version`; `kobo_api.admin_store` is the one way to
  write and records an audit event in the same transaction. Migration `a1b2c3d4e5f6`.
- **The boot-time migration takes a Postgres advisory lock**, so two replicas on a rolling
  update never run `alembic upgrade head` at once (`migrations/env.py`). `docs/operating.md`
  records the two rules: migrations are additive, one pod migrates at a time.
- **Admin panel, Add-ons pane reads the room** (issue #173 step 2): `GET /api/admin/addons` returns one
  card per add-on (deploy platform, secrets, observability, switchboard, tool gateway, memory,
  model gateway) with its state derived from the org keys that turn it on; the pane shows the
  cards, the keys, and whose add-on it is. Admins only, and only while `admin_panel` is on.
- **Bundle publisher** (`kobo_api.bundle_publish`, issue #172 step 1): pushes an agent's `bundle/`
  as a registry artifact tagged by commit sha and moves the per-agent `:current` pointer, only
  when the bundle's git tree id changed. Not wired into any lane yet; the one-runtime design
  (`docs/superpowers/specs/2026-10-18-one-runtime-and-admin-panel-design.md`) says where it goes.

### Changed

- **No "platform" anywhere a person reads it** (PR #228). The admin page is **Services** (old
  `#/admin/addons` and `#/admin/platform` links still open it), the deploy add-on is
  Deployment, the build step is **Deployment setup**, the destroy stage is **Deployment
  teardown**, the secrets service is "the secrets service", and an agent's identity is its cloud
  identity. The guide and glossary follow. Internal ids (`destroy-platform`, the
  `deploy-platform` check id, the secrets panel's `platform` flag) are unchanged.
- **Teach this on a chat turn saves that bubble's own words** (PR #168, issue #123 items 12 and
  13). It used to prefill "You said: ... / agent replied: ..." from both sides. The saved
  message now names the file it wrote (`Saved as intake/<file>`).
- **Integrations ask each vendor for the credential it really uses.** AWS is a role the gateway
  assumes (ARN, optional external ID), Google Cloud is workload identity with an optional service
  account to act as, Azure is a federated app registration (client and tenant IDs), Datadog and
  Wiz are service-account key pairs, Atlassian, Fastly and Okta are API tokens. The connect body
  carries `auth`, `identity` (plain fields) and `secrets` (named values stored in the secrets
  service, one env each at the gateway); the gateway target gets `auth_method` and a
  `[target.auth]` table. A kind with no secret stores nothing.
- **The admin panel reads like a settings screen.** Every page is a title, one sentence, a toolbar
  and a table. Overview is one row per page, each a link with the number that matters, read from the
  room (the demo store is gone). Add-ons is now Services: a table of what the room runs on,
  with the secrets service named as OpenBao through sero, and Reload org config asks before it acts.
  The integration catalogue is this room's vendors (Atlassian, Datadog, Google Cloud, AWS, Azure,
  Wiz, Fastly, Okta, Other). Roles, People and Audit use the same table. The rail's "This room"
  facts come from a new `GET /api/admin/room` instead of placeholder text.
- README's "fail closed" bullet says what ships: an explicit tool allowlist, and with the
  switchboard on, colleague (and broker read) tools with no approval gate (issue #205).
  `docs/operating.md` says what the `kobo-room` ClusterRole really grants, cluster-wide Secret
  access included, and the per-namespace shape it should become (issue #204, docs half).
- **The vendored daedalus bundle schema is the v0.5.5 copy** (issue #152). It was 0.4.0.dev0's;
  the newer schema adds the `engine` block and the GitHub, CloudTrail, Jira and Slack-read tool
  specs, allows `${VAR}` placeholders and no longer requires a skill `id`. Every shape kobo
  composes still validates, and a test pins which copy is vendored.

- **A commit that changes only the deploy contract skips CI.** The environment and model
  changes commit `gojira.yaml` alone; that commit failed the generated contract check and
  the setup pull request Gojira opens on reprovision was the deploy anyway. The push now
  carries `[skip ci]`, the same as the archive commits (agora discussion #55, item 2).
- **Composed agents no longer declare `approvals.ceiling.write: false`.** The runtime
  enforces only `ceiling.exec`, so the line promised a limit nothing applied (agora
  discussion #55, item 7). Existing agents keep theirs until they are recomposed.
- **The Harness tab's Behind line shows the tag of a `tag@digest` pin**, the same rule as
  the agent card's `version`, instead of the digest (issue #158).

### Fixed

- **A destroy that refuses a namespace still revokes the agent's colleague pass** (issue #193,
  follow-up to PR #213). The namespace check ran before everything in the secrets stage, so a
  refusal also skipped the pass revocation. The check now runs first in that stage whatever the
  secrets platform's state; a namespace older than the agent, or a cluster the room cannot ask,
  keeps the stored secrets, still revokes the pass, and fails the stage, so the rest of the
  destroy waits for an operator. An unreachable cluster is now reported as "could not check the
  namespace" rather than as someone else's namespace.
- **A typo in the config can no longer turn authentication off** (issue #198). `Config` and its
  `mcp:` block refuse unknown keys instead of dropping them (`iap_audiance: x` used to run the
  room open and say nothing); the retired `runtimes` key is dropped with a warning so old files
  still boot. An MCP server on a room with no IAP is refused at construction unless
  `mcp.allow_without_iap: true` says so. Every boot logs one line naming the auth regime, and an
  ERROR for each accidental opening: no IAP without `allow_unauthenticated: true`, IAP with no
  operators and no admin groups, or groups configured with no directory resolver.
- **The room-wide colleagues routes follow agent visibility** (issue #192). `GET
  /api/colleagues/conversations`, `GET /api/colleagues/conversations/{task_id}` and `GET
  /api/colleagues/stream` were gated only on the switchboard being wired, so any signed-in user could
  list every switchboard conversation and read the full transcript of another person's chat with an
  agent they could not see. Now the list keeps only rows with a participant the caller can see, the
  detail answers 404 otherwise, and the stream needs an `?agent=` the caller can see (400 without one,
  404 for one they can't). Operators, readers, and rooms without IAP or groups see everything as
  before. The UI already passes `?agent=` everywhere, so it is unchanged.
- **An agent created or connected under `KOBO_ROLE=api` is provisioned** (issue #195). The create
  and connect routes stamped a heartbeat on the new row in every role, and a worker's `claim_queued`
  takes only rows with no heartbeat, so under the api/worker split no worker ever claimed the agent and
  the sweep later failed it as "interrupted by an API restart". The row is now born without a
  heartbeat when the route leaves it for a worker, as `docs/arch/provisioning.md` already said. Role
  `all`, the shipped default, is unchanged.
- **An empty dispatch-pair set no longer opens the switchboard** (issue #194). Cabal reads an
  empty pair table as "every agent may call every agent", and the room pushed an empty set
  whenever nothing was allowed: a room of commons agents, one agent per group with no invites,
  the last invite revoked, or one of two paired agents destroyed. The room now sends one
  sentinel pair, `kobo:deny-all` to `kobo:deny-all`, which no real agent can match, and logs a
  warning on every such push instead of only after a non-empty one in the same process. Syncs
  in one process now compute and push one at a time, so an older set cannot overwrite a newer
  one; pushes from separate processes or replicas are still unordered.
- **An agent can no longer take over, or on destroy delete, a namespace the room does not own**
  (issue #193). An agent's name is its Kubernetes namespace, and the deploy platform adopts a
  namespace that already exists. Create and connect now refuse a name whose namespace already
  exists (409), and answer 503 when the cluster cannot be asked. Destroy's secrets, platform and
  access stages first check the agent's namespace: one older than the agent's row (beyond two
  minutes of clock skew) was there before the agent, so the stage fails with nothing deleted and
  an operator has to look. The reserved names now also cover kube-node-lease, the suite's
  products (cabal, sero, liminal, noto, techne, daedalus, agora, kaminos, agents) and
  cert-manager, external-secrets and ingress-nginx.

## [0.2.2] - 2026-10-14

### Upgrading

- No database migration. The alembic head is still `a8e2c4f6d913` from 0.2.0.
- Nothing to change. To give a digest-pinned agent's card a readable `version`, re-pin its
  runtime as `repo:tag@sha256:...` (see Fixed).

### Changed

- **The in-app guide and the operating doc follow the release** (PR #153): guide 11 says an
  owner or an operator can **set runtime image** (it said operators only), guides 03 and 08
  follow the code, and `docs/operating.md` gains an Upgrading section (migrations run at
  boot, so back up first).
- **CONTRIBUTING: on macOS, put the GNU tools first on PATH** (PR #155).

### Fixed

- **A connected agent no longer loops on reformatting pull requests.** `ConnectedWireStep`
  decided whether there was wiring to propose by rewriting its files and comparing bytes, so
  a file that already carried the wiring in Gojira's own formatting came back changed. The
  step opened a pull request of pure reformatting, and Gojira reformatted it back. Both
  writers now leave a file alone when its parsed content is unchanged (PR #151, issue #123
  item 11).
- **The 0.2.1 note on the card's `version` was only true for a tag pin.** `version` is the
  runtime image's tag when the reference has one, including `repo:tag@sha256:...`. A
  digest-only reference names no version, and the runtime images carry no version label, so
  its card shows the digest's first 12 hex (`sha256:a0b09334649d`). To keep an exact build
  and a readable version, pin `tag@digest`, for the room's default and for **set runtime
  image** alike. The full reference stays in `metadata.runtime_image` (issue #156).

## [0.2.1] - 2026-10-14

### Upgrading

- No database migration. The alembic head is still `a8e2c4f6d913` from 0.2.0.
- Agent cards change shape. `capabilities` becomes an object (`{}`), not a list, and
  `version` becomes the runtime image's tag, not `0.1.0`. Anything that reads the card from
  an agent's repository or from the switchboard and expects a list there needs updating.
- Existing cards are corrected one at a time, on each agent's next skill change, runtime
  change, **Edit details** save or other kobo commit that moves `FROM`. The room doesn't
  rewrite them at boot, because each rewrite is a push and each push redeploys the agent.

### Fixed

- **An agent's directory card lists its current skills and says what the agent is.** The
  card's `skills` were written once, at compose, so a skill added or removed later never
  reached it or the switchboard's copy; every card also said "Assembled by kobo." at version
  0.1.0 (issue #147). The card's skills, description, version and runtime are now derived
  from the repository in the same commit as any skill change, runtime change or **Edit
  details** save, and the switchboard is sent the new card. That includes any kobo commit
  that moves the Dockerfile's `FROM` (a teach note, intake upload, tool upload or import, or
  Slack wiring after a room runtime upgrade). The skill and runtime routes' responses carry
  `directory` (`synced`, `failed` or `off`); the dashboard doesn't show it yet.
  - The description is the capability sentence, or the persona's first sentence until there
    is one.
  - `version` is the runtime image's tag, and `metadata.runtime_image` names the full image.
  - `capabilities` is `{}`, the object A2A defines, instead of a list.
  - An existing card is corrected by its next such change.
  - A ready agent with no purpose tags shows **no purpose tags** on its dashboard card.

## [0.2.0] - 2026-10-13

### Upgrading

- **This release migrates the database: back it up first.** Alembic revision
  `a8e2c4f6d913` adds one table, `agent_system_tasks`, and the room runs `alembic upgrade
  head` at boot. A version from before this one won't start on a database that has the
  revision, because its own upgrade can't locate it. To roll back, restore the backup or run
  `alembic downgrade e5a9c3f17b20` first.
- Leave `learnings_require_attribution` off until every agent runs a daedalus release that
  includes daedalus issue #165. Older runtimes send turns from the room's chat unattributed,
  and the switch would drop them.

### Fixed

- **The destroy confirm closes once the room accepts.** After the 202 the dialog stayed
  open with the name still typed, and a second click started a second destroy (issue #123,
  item 6).
- **A failing nightly learnings pass shows on the agent's card.** An agent whose runtime
  refused the task ("Unknown system task 'learnings'") failed every night and still looked
  healthy; the failure was only a log line (issue #142). The room now records each run per
  agent in the new `agent_system_tasks` table. After `system_task_flag_after` failures in a
  row (default 3), the card shows "learnings failing" and the Learnings section shows the
  last error.
- **Unattributed and access-related learnings need a second confirmation.** A nightly
  candidate whose quote only introduces the speaker ("i'm Ana ana@example.com") is dropped.
  A candidate nobody can be attributed with, or one about approving, granting or revoking
  access, is marked on the Learnings tab and is remembered only on a second click (API:
  `confirm: true`; `kobo_decide_learning` takes `confirm`). Remember all leaves those rows
  pending (issue #143). `learnings_require_attribution` drops unattributed candidates
  outright. Leave it off until every agent runs a daedalus with issue #165, which marks
  turns from the room's chat as `user`; before that, everything taught through the room's
  chat arrives unattributed and would be dropped too.
- **A connected agent no longer waits forever on an install with no Gojira webhook.** The
  waiting sweep read the platform's status, which moves only when the webhook reports the
  setup PR's merge, so the agent stayed at "waiting" after the merge (issue #118). The sweep
  now asks Gojira to check the pull request (`POST /api/v1/apps/{id}/sync`) before it reads
  the status. With a webhook the call is a no-op.
- **A re-pinned runtime image is named, not silent.** Intake, teach, tooling and Slack
  commits put the Dockerfile `FROM` back to the image the room has recorded for the agent,
  which reverted a hand-pinned `FROM` with nothing said (issue #140). The re-pin stays,
  because the recorded image is the authority, but the commit message and a warning log now
  name both images. Change an agent's image with `kobo_set_runtime_image`.
- **The GitHub token broker says why it has nothing to mint.** After the agent's own token
  verified, its 404 said "no such agent" (issue #11). The status is still 404, but the
  message now says whether repository access was never requested, is waiting for an
  operator, or was revoked, and names the route that fixes each.
- **"The switchboard is not configured" names the fix.** The 409, and the `kobo_feed` MCP
  tool's error for the same gate, now list the three settings it checks: `cabal_url` and `agent_endpoint_template` in the org config, and
  `CABAL_OPERATOR_TOKEN` in the room's environment (issue #123, item 1).
- **A waiting agent's card says what it waits on.** The agents list showed a bare
  `waiting` chip; the card now reads "Waiting on a pull request. Open for details."
  (issue #123, item 7).
- **Composed agents with skills now get `skills.load`.** daedalus 0.5.0 shows assigned
  skills as an index in the system prompt plus a `skills.load(name)` tool, but only when
  `tools.allow` admits the tool, and the composer never added it, so every composed agent
  with skills logged a warning at boot and lost the index (issue #137). The composer adds it
  for any agent born with skills, and the self-card names it. Adding a skill later, from the
  catalog or as a local skill, adds `skills.load` to `tools.allow` in the same commit when
  the allow list doesn't already admit it. An agent that already has skills and gets none
  added keeps its allow list: add `skills.load` to `tools.allow` in its `bundle/agent.yaml`.
  Older runtimes ignore the entry.
- **Changing an agent's colleague mode no longer locks its row across a GitHub push.** The
  route set `colleague_mode` before rendering the colleague document, so the roster query
  flushed the UPDATE and Postgres held the agent's row lock through the clone and push. The
  document now renders for the new mode without touching the row, and the row is written
  after the push lands (issue #133).
- **Colleague mode is documented.** The guide, glossary, README and `docs/what-the-room-does.md`
  said every agent asks its colleagues; they now describe `roster-only` and `none`, and the
  guide says where an owner sets the mode. The route and MCP tool docstrings, and a note under
  the Harness tab's Colleagues row, now say that `roster-only` also stops the agent answering
  from general knowledge. A stranger-gets-403 test now covers the colleague-mode, runtime,
  model and Slack wake routes.
- **An agent can be created again under a destroyed agent's name.** Destroy renames the
  repository to `archived-agent-<name>-<id>`, and GitHub keeps answering at the old name
  through a redirect; the room read that as "a repository already exists and is not
  recorded for this agent" and refused. The existence check now compares the name GitHub
  answers with, so a redirected name counts as free, which GitHub allows a new repository
  to take.

### Changed

- The generated deploy workflow passes `AR_TOKEN` to the image build as a BuildKit secret
  instead of a build argument, which the build log printed (PR #145).

## [0.1.0] - 2026-10-12

The first tagged release, `0.1.0`, is the room as the README describes it: create, connect,
chat and teach, memory, colleagues and threads, access, Slack, the agent page, personas and the
MCP surface. Everything before the tag is in the git history and in the design record under
[docs/superpowers/](docs/superpowers/). The entries below cover the changes after this
changelog started (PR #103).

### Added

- **The deploy workflow reads its registry, deploy identity and cluster from repository
  variables** (`GOJIRA_DEV_*`, PR #115) and fails fast when any is unset, so a fork runs it
  without editing the file.
- **Colleague mode, set per agent by its owner** (PR #132). `default` is unchanged: the agent
  has the colleague tools and is told to consult colleagues first. `roster-only` keeps the
  roster as knowledge but drops `ask_colleague`, and the agent answers from its own tools and
  memory and asks its human when those fall short. `none` removes the colleague tools and the
  roster. Other agents can still ask the agent in every mode. An owner sets it on the Harness
  tab, through the API or with `kobo_set_colleague_mode`; each change is logged with who
  made it and what it was before.
- **Slack wake modes** (PR #111). Each agent wakes on mentions only, on a mention and then the
  rest of that thread (the default for new agents), or on every message in the channel. A
  direct message always reaches it, and the agent is told its own setting.
- **DCO sign-off is checked on every pull request** (PRs #106 and #112). Authors with write
  access to the repository are exempt; CONTRIBUTING.md says how to sign off.
- **How this was built and the AI-tools policy** in the README and CONTRIBUTING.md, and a test
  that fails if a CI job on a self-hosted runner loses its fork guard (PR #120).
- A quickstart that runs from a clean clone, community files (code of conduct, support,
  issue and pull request templates, CODEOWNERS) and this changelog, ahead of going public.

### Changed

- **Two platform names.** The secrets platform the room hands per-agent Secrets to is
  **sero** (named ladon until 2026-10-12) and the observability platform it links to is
  **noto** (named argus). The deployment config keys are `sero_url`, `sero_audience`,
  `sero_namespace`; the org keys are `noto_url`, `noto_project`; the module is
  `kobo_api.sero`. The old key names are still honoured, each with a boot WARNING,
  so no running room changes behaviour before its config files catch up.
- **The API package is published as `tenish-kobo`** (PR #113). The import package
  (`kobo_api`) and the deployment name are unchanged.
- **The deploy contract names the switchboard token.** The room reads `CABAL_OPERATOR_TOKEN`
  from a Secret named `kobo-cabal` (PR #122), and Gojira now binds the room's ServiceAccount to
  the `kobo-room` ClusterRole through `cluster_roles` (PR #116); `k8s/cluster/rbac.yaml` keeps
  only the ClusterRole.
- The README's claims are narrowed to what the code does (PR #128).
- CI runs the architecture-card check, Dependabot leaves semver-major bumps to a person, and
  SQLAlchemy is held below 2.1 (PRs #108 and #114).
- `docs/superpowers/` carries a README saying the specs and plans are a historical record,
  not a description of the room today.
- CI installs from the committed `api/uv.lock` with `uv sync --locked`.
- The README is a two-screen read; the operator's material moved to `docs/operating.md` and the
  long feature list to `docs/what-the-room-does.md`, both verbatim.
