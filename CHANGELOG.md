# Changelog

All notable changes to kobo are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

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
