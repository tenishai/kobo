# Changelog

All notable changes to kobo are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **Bedrock models through the AWS connection (kobo#390).** The Connect AWS sheet now offers
  **One account** beside **An organization**: a role in one account the room assumes directly,
  named by the person (a second direct account is a second row). An account's sheet (and a member
  account's role, through the API) carries **Use this account's Bedrock models in the room** with a
  region (us-east-1 unless said otherwise); ticked, the row's spec carries
  `models = {provider: "bedrock", region, role_arn}` and the role's snippets gain the Bedrock
  permissions block (an `aws_iam_role_policy` under the Terraform module, one JSON policy for
  CloudFormation and by hand; the trust policy is unchanged). The org's `models:` list names the
  Bedrock entries (`bedrock/<model id or inference profile id>`); the model gateway serves each
  through every ticked account by assuming its role with the room's own identity token
  (`aws_role_name`, `aws_web_identity_token`, `aws_session_name: kobo-gateway`), so nothing is
  stored. The catalogue lists them under **AWS** (naming the accounts when several serve), with
  "Ask a kobo admin to connect AWS with Bedrock" while none does; the Services row counts the
  accounts as providers. The permission service has no Bedrock read yet, so the row says the models
  are registered and not yet checked with AWS; an organization's hub never carries the tick.

- **Self-hosted models (kobo#390).** A fourth kind under Admin, Integrations, and the one that is
  only models: models you run yourself, on your own machines or clusters, at an address you give
  the room (vendor `self-hosted`, auth kind `self_hosted_endpoint`, `kobo_api.self_hosted`). One
  row per endpoint, named by the person; the form takes the base address (the room appends `/v1`
  when it is missing), an optional key, the models it serves one per line (`name` or
  `name = label`) and one optional block of request options (YAML or JSON) sent with every call to
  every model of the row, such as `chat_template_kwargs: {enable_thinking: false}`. On connect the
  room itself lists the models at the address and asks each named model for a short answer
  (`max_tokens: 8`, with the options); a model that fails is marked on the row with the server's
  sentence ("2 of 3 models answer; `old-model` failed: ...") and is not registered, and the row
  connects as long as one answers. **Check models** on the row runs the probe again. The key is
  held in the room's own Secret (`self-hosted-<row id>`), never echoed. With the model gateway on,
  each answering model is one deployment (`<row slug>-<model slug>`, LiteLLM's `openai/` route at
  the endpoint, the options as `extra_body`), the catalogue lists them under the row's name and the
  pickers group them there; the org's `models:` list is untouched. Nothing of this touches the
  permission service or the tool gateway: nothing is leased.

### Changed

- **The Create agent dialog in the room's look.** The dialog now wears the room's restyle: a title with one sentence under it, the persona list as hairline rows with the name and its line in ink under small-caps group labels and **Build your own persona** as a button on the header row, Name, Group and Model each the whole width with the model list's reasons beside the names and the no-model sentence under the list, Cancel and Create agent on the right of the footer. Focus lands on the persona search and Enter in Name creates. Phone width gets 16px gutters.

## [0.3.38] - 2026-11-01

## [0.3.37] - 2026-11-01

### Added

- **The room's model gateway (kobo#390).** Models now come from the clouds the room connects. With
  the new org key `models_gateway_url` set (the LiteLLM proxy the install deploys; its master key is
  read from the Secret the machine-local `models_gateway_master_key_secret` names, never from org
  config), a Google Cloud project connected with the Vertex AI box ticked becomes a model provider:
  the room registers every `vertex_ai/` entry of the org's `models:` list on the gateway as one
  deployment per project (`model_name` = the label as a slug, `model_info.id` = `<row id>:<entry
  id>`), at connect, disconnect and boot, adding and deleting only the difference; a gateway that
  refuses leaves the row connected with the refusal on its line. `GET /api/catalog/models` then
  carries `provider`, `provider_label`, `gateway_model`, `available` and `reason` for each model,
  and `default` is the first available one. New agent and Change model group the list by cloud and
  show a model nobody serves in grey with its reason (its cloud is not connected, or the model
  gateway is not reachable); with no model available New agent is off with one sentence pointing
  at Admin, Integrations, the create route refuses with 409 and the same sentence, and Change model
  refuses an unavailable model the same way. A new agent's bundle thinks through the gateway
  (`model: {id: openai/<gateway_model>, api_base: <gateway>/v1, api_key_env: MODELS_GATEWAY_KEY}`),
  the room mints it a key of its own (alias `agent:<name>`) and delivers it through the same lane
  the Slack values use as the Secret `<agent>-models`, the deploy contract names that Secret, the
  per-agent Vertex grant is skipped (the gateway's identity holds the role), and destroy revokes the
  key. Agents made before the key was set are untouched until an owner presses Change model, which
  moves them onto the gateway. The Services row **Model gateway** (a stub on a retired key) is now
  **Models**: off, Not reachable, or "N providers connected, M models available, default X". With
  `models_gateway_url` empty nothing changes.

- **Move an agent from the old Slack setup to the room-made app (kobo#304, phase 3).** On a
  legacy agent (one whose Slack app came from the Slack tab, with an app-level token), the
  Slack row on the Access tab reads **Connected the old way** with its app id and offers one
  button, **Move to the new Slack setup**. `POST /api/agents/{name}/slack/move`
  (owner-or-operator, behind `slack_relay`) deletes the old app with the room's config token,
  as destroy does, then runs Create's path: the relay id is minted, the app is created from the
  manifest with the agent's current Wakes on, the five create-time values go to the agent's
  Slack secret in the same write that takes `SLACK_APP_TOKEN` and the old `SLACK_BOT_TOKEN` out
  (so the runtime takes the HTTP way in at its next start), the open transport block and the
  wiring land in one commit, and the saved channels stay and still narrow it. The answer is
  Create's (`app_id`, `install_url`) plus `old_app_removed`; a failure deleting the old app is
  reported as `old_app_removed: false` and does not stop the move, since the old app may already
  be gone. The owner presses Install to Slack as usual. Audit action `slack.move`; 409 for an
  agent that is not legacy. Per the owner ruling of 2026-11-01 the dev agents still on Socket
  Mode may be moved freely. The Slack tab already hides itself for a moved agent; its full
  removal, along with the Slack tab's guide text, is a later cleanup once dev has moved.

- **Connect Google Cloud (kobo#390).** Admin > Integrations > Add integration > Google Cloud is
  its own sheet, modelled on the AWS one: pick **A project** (the common case) or **A folder or
  organization**, and the page shows the room's identity (the permission service's service
  account, email and id) and the roles to grant it three ways, `gcloud` commands, Terraform
  (`google_project_iam_member` and the folder and organization forms) or the plain list for the
  console, rendered from a vendored pack file (`packs-gcp.json`: `inventory`, every viewer role the
  catalogue's gcp read permissions name; `models`, `roles/aiplatform.user`). No key is made
  anywhere. Connect asks the permission service to read the project as that identity (`POST
  /probe/gcp`) and saves nothing until Google Cloud agrees; the row is held there with every gcp
  read permission as the ceiling and nothing goes to the gateway. A folder or organization is the
  hub shape: every project under it the identity can read connects by itself (`POST
  /integrations/{id}/discover`), the rest read **Not granted yet** until **Check projects** finds
  them granted. Tick **Use this project's Vertex AI models in the room** (location `us-east5` by
  default, `global` allowed) and the probe also lists Vertex AI models there; the row's spec then
  carries `models = {provider: vertex_ai, location}` and its line reads "Models: Vertex AI,
  us-east5". That spec is the hook the room's model gateway reads in the next lane; `GET
  /api/catalog/models` is unchanged here. New auth kind `gcp_parent`; `gcp_identity` now requires
  the project id. The permission service client gains generic `identity(vendor)` and
  `probe(vendor, **body)`; the picker reads a project row as a "project" under its folder.

## [0.3.36] - 2026-11-01

### Changed

- **Slack and GitHub are connections, not service rows (kobo#377).** The two rows that were
  always at the top of Admin > Integrations (**Slack**, the config token pair the room creates
  agents' Slack apps with; **GitHub**, the app behind the room's pull requests and agent
  repositories) are gone, and with them the `service` kind and the **Set up** button. Each is
  now an ordinary connection added through **Add integration**: pick Slack and paste the
  pair, or pick GitHub and paste the app id, installation id and private key, with the same
  checks as before; the row shows logo, name, kind `connection`, status, Added by and when,
  **Replace** and **Remove** like every vendor. The room's own needs read the credential from
  the connection through one accessor module (`kobo_api.room_credentials`); the credential
  stays in the room's own Secret, where the service rows kept it, so a rollback to 0.3.35
  reads the same bytes. A fresh room shows an empty table. An older install's deployment
  config (`slack_tokens_*`, `pr_app_*`) still stands behind a vendor with no row, shown as a
  row marked **set in the deployment config** with **Replace** to move it into the room. At
  boot, once and idempotent, a Secret left by a service row becomes the connection row
  (logged in one line); where both already exist the connection wins and adopts the Secret.
  The Slack row on an agent's Access tab and the Slack tab read **Ask a kobo admin to add
  the Slack connection** until one exists. Overview's **N vendors connected** counts them
  with the rest. GitHub keeps its permission service connection unchanged; Slack is not a
  leased vendor and gets no permission service row. The `GET`/`PUT`/`DELETE
  /api/admin/room-services*` routes are retired; `kobo_api.room_services` stays as the
  Secret store behind the connections for one release.

- **The Access tab stays one screen as vendors multiply (#382).** Every vendor is one compact
  row, closed by default: its logo, its name, one line on where things stand ("2 held, 1 waiting
  for review", "Nothing held yet", or Slack's Live / Applying your change) and the one action
  (Ask for more, or Add Slack). A row opens on click to show what it did before (the held table,
  the pending rows, Revoke and the picker, unchanged); one row is open at a time, and the row
  last opened stays open for the session. `?vendor=<id>` opens a row from a link (the Slack
  install's way back keeps working), Slack opens itself while installing or applying a change
  and shuts once it reads Live, rows with something held or waiting sort first and Slack is
  always last. The heading is one sentence; the "Runs as" line stays.

### Added

- **A failed attempt is explained on the vendor's row.** When the permission service could not
  mint or refused a credential for the agent in the last day, the vendor's row reads **Last
  attempt failed 12 min ago: <the service's reason>** in the warn tone, the whole reason on
  hover, so an owner no longer learns only "my credential did not resolve" from the agent. The
  access payload carries it as `last_failure` per vendor (`{at, kind, reason}`), from one read of
  the service's events; a lease at that vendor issued after the failure clears it, and the
  service being away leaves every vendor without one rather than failing the tab.

## [0.3.35] - 2026-11-01

### Fixed

- **Live means live.** After a Wakes on save, the Slack row (and the Slack tab's note) said
  **Live** on the room's first health reading after the save that found the agent up, which
  could be the old agent before it went, or another replica's older reading. The health probe
  now also reports `rolling` (the agent is mid-restart: the controller has not caught up with
  the latest spec, or the new pod is not ready while the controller says the rollout is still
  progressing) and `ready_since`; the wake route answers `applied_at`; and the row waits for a
  reading taken after the save that is up and not rolling, then settles fifteen seconds before
  saying Live, since the agent reads as up a few seconds before it listens. The three-minute
  give-up and the amber "takes effect when the agent next restarts" stay.
- **Restarting, not down.** While the agent is mid-restart (`health.rolling`, or a Wakes on
  change this page just saved), the agent page reads **This agent is restarting. Back in
  about a minute.** in amber instead of the red down banner, the header pill reads
  **Restarting**, and the Slack row reads **Applying your change** rather than Down. A pod
  that is simply down, including a crash loop after a finished rollout, still reads Down.

### Added

- **Agent connections on the admin Overview.** With the permission service on, a card after
  Integrations reads **N agents hold access** and opens Agent connections. Its figure comes
  from `GET /api/admin/access/summary`, which counts agents with a connection of their own, an
  unexpired lease or an approved request from the same three reads the pane starts with and
  none of its per-agent grant calls.

## [0.3.34] - 2026-11-01

### Fixed

- **The admin panel's "Deploys to" names the cluster, not the alias.** It read the name the
  deploy platform registered the cluster under; it now asks the platform where that alias
  points and shows the cluster, with the project, location and alias on hover. The alias
  stands in when the platform cannot say.

### Fixed

- **The Harness page now says what an agent really runs on.** The room records what each
  agent is built on (`Agent.built_runtime_image`, the ref its Dockerfile's `FROM` carries,
  migration `f2a7c4d9e1b3`): compose writes it at create, every fast-lane commit that re-pins
  the file (teach notes, uploads, tools, Slack) and `PATCH /api/agents/{name}/runtime` keep
  it current, and a boot backfill reads each older agent's Dockerfile once (unreadable
  repositories are retried next boot; never on the request path). The detail payload serves
  `runtime_image` as that fact when known (the old resolved value otherwise), plus
  `built_runtime_image` and `runtime_pinned`, and keeps `room_runtime_image`. The Harness
  page compares the fact with the room's pin: **Runs on v9. The room now builds on v10.**
  with **Update to current**; **Pinned to v9** with **Move to the room's runtime** for an
  agent deliberately held; **Checking what it runs on.** until the backfill lands; nothing
  for a connected agent, which brings its own. Before this the page compared the room's pin
  with itself for every agent that followed the room, so every agent read "Runtime is up to
  date" through a room runtime move and the button never appeared. Asking the runtime route
  for the room's own runtime now clears the agent's hold so it follows the room again,
  instead of pinning it to that day's runtime for good.

## [0.3.33] - 2026-11-01

### Changed

- **A Wakes on change reaches the agent at once.** Saving **Wakes on** used to write the
  choice into the agent's files and leave the running agent on the old one until the deploy
  platform next rolled it, minutes later, with the page saying only "Saved." Now the route
  (`PUT /api/agents/{name}/slack/wake`) also sets the value on the live deployment
  (`KubectlClient.set_env`, `kubectl set env`, which rolls the pods itself) and answers
  `applied_now`; the committed and live values agree, so the later apply changes nothing.
  Both the Slack tab and the Slack row on Access keep the choices locked while it applies, so
  two changes cannot race. The Slack tab's note reads "Saved. The agent restarts with this
  choice now; give it a minute." and then "Live." once the room's next check finds the agent
  up. The Slack row's header carries an honest state instead of "Installed in <workspace>":
  "Live" (green) when the agent is ready and nothing is pending, "Applying your change. Give
  it a minute." (amber) from the save until the agent is back up, "Installing" while the
  install is in flight, and "Saved. Takes effect when the agent next restarts." (amber) when
  the live change was refused. The guide's Slack section says the same. The channel list on
  the legacy Slack tab is unchanged: it is a bundle change that takes a rebuild.
- **No channel list on the Slack row.** A room-made app answers wherever it is invited, so
  the Slack row on Access drops the channel list and **Add channel**; one sentence takes their
  place: "Invite @<agent> to a channel in Slack and it answers there." The channels route and
  the legacy Slack tab's own list are untouched, and Create still wires the open entry.

- **A room-made Slack app answers wherever it is invited.** The Slack row on the Access tab
  now says so: invite the agent to any channel and it answers there; the channel list only
  narrows it, and the empty list reads "Answers in every channel it is invited to." Clearing
  the list on a room-made app writes the open entry back into the agent's files, so it keeps
  answering instead of going silent (`PUT /api/agents/{name}/slack/channels`). After
  **Install to Slack** the person lands on the Access tab with the Slack row scrolled into
  view and lit for a moment (`?vendor=slack`, read once and dropped), and an install that did
  not finish says so on the row itself. The guide's Slack section says the same. The Slack
  tab's own allowlist wording is unchanged.

## [0.3.32] - 2026-11-01

### Changed

- **Agent to agent picks by team.** On the Harness tab, **Request another team's agent**
  now opens a list grouped by team (name, how many agents, a chevron); a team opens into
  compact rows with a checkbox, the agent's name and one line on what it does. Tick one or
  many across teams and one **Request access** sends a request per agent, with a toast
  saying how many went out. An agent already asked, or not ready, shows its state in place
  of the box. The viewer's own team never appears. The table below gains Team and Date
  columns and a plain empty sentence. `GET /api/agents/{name}/requestable` answers `group`
  on each `already` row, leaves same-group agents out of `not_ready`, and names the group
  it leaves out as `paired_group`.

## [0.3.31] - 2026-11-01

### Added

- **Slack as a vendor on Access (#371, phase 2 of #304), behind `slack_relay`.** With the
  org key on, the Access tab gains a Slack section: pick **Wakes on**, **Create Slack app**
  (the room creates the app from the manifest with its config token, pointing Slack's events
  at the room's relay instead of Socket Mode, so no app-level token is ever made), then
  **Install to Slack** (Slack's own install page; the callback stores the bot token through
  the agent's secrets lane and restarts the agent). Installed: workspace, channels editor,
  Wakes on (applied with `apps.manifest.update`), Remove. Two public routes, `POST
  /slack/relay/{relay_id}` (signature verified with the app's signing secret inside a five
  minute window, challenge answered for a pending id, `event_id` dedupe, 200 at once, forward
  in the background with `X-Kobo-Relay-Secret`, token bucket per id) and `GET
  /slack/oauth/callback` (single-use state), exempt from the identity proxy like `/oauth`. New
  table `slack_relay_apps` (migration `b4d8e2f6a1c3`) holds the relay id, app id and the
  signing, relay and client secrets plain, the way the room's other held tokens are; the bot
  token never lands there. The Slack tab shows only for an agent with a paste-flow app
  (`slack.legacy`) or when the switch is off; with the switch off nothing changes.

## [0.3.30] - 2026-11-01

### Added

- **The Audit page has two tabs, People and Agents (#371, section 3).** People is the room's
  own log as before. Agents is what agents did with their connections, read live from the
  permission service and shown in the same row shape: a lease it minted (`lease.minted`,
  outcome `ok`), one it refused (`lease.refused`) and one it could not mint (`lease.failed`),
  each as one sentence ("ward borrowed read on AWS aws-prod for 1h: count instances"). The
  Actor and Action selects hide on Agents; the agent and outcome facets come from that
  answer; Export hands that window back as CSV or JSON. `#/admin/audit?tab=agents&agent=x`
  opens it filtered. `GET /api/admin/audit/agents` (admin gate; `agent`, `outcome`, `since`,
  `until`, `q`, `limit`, `format=csv|json`) is the route, `kobo_api.audit_agents` the merge
  and the sentence, `BabClient.events(...)` the new read (bab `GET /events`). The people
  route and its export are unchanged.

### Changed

- **Agent connections leave the Integrations table and get their own page (#371).** `GET
  /api/admin/integrations` no longer appends a permission-service row that has a `parent`
  (an agent's GitHub repository set, kobo#365), so the Integrations table is the room's
  connections only. A new admin page, **Agent connections** (the `connections` id with the
  permission service on; the older lanes page keeps the id when it is off), is one card per
  agent the room knows, newest activity first, with one row per vendor: the agent's own
  connections there, what it holds (permission, tier, expires, granted by) and what it is
  borrowing right now (permissions, task, issued, ends, via). **Revoke** calls the existing
  `DELETE /api/agents/{name}/access/grants/{grant_id}`; a search box narrows by agent, vendor
  or repository; `#/admin/connections/<agent>` puts that agent's card first. Served by `GET
  /api/admin/access/agents` (admin gate): one `integrations()` for the children, one
  `all_leases()` filtered to the unexpired, `grants()` per agent that has a child, a request
  or a lease, fanned out on a thread pool of six after the room's own table is read and the
  session closed (the 2026-10-01 pool rule). An agent with nothing is still listed.
- **The Access tab lists the vendors the room has connected, nothing else (#371).** `picker()`
  carries `connected` per vendor (a cloud with a connected account, a service or GitHub with a
  parentless connection); `PermissionsPanel` draws those alone. The "0 of 0 projects
  connected" subtitles are gone; GitHub keeps its repository count because that is about the
  agent. With nothing connected the tab says so and links the word Integrations into the admin
  panel for someone who may open it. `GET /api/access/picker` carries the same field; the
  Review pane is unchanged.

## [0.3.29] - 2026-11-01

### Changed

- **The repository picker reads the warm listing (#368).** `GET` and `PUT
  /api/agents/{name}/github/repositories` read the five-minute listing the connect form
  already keeps (`repo_search.RepoIndex.listing`, filled by the warmer at boot and every
  TTL) instead of walking the installation a page at a time on every open and again on
  every save; a room with no index still walks. The `GET` payload carries `total`, the
  count of repositories the room can see, so the picker can show the best matches for what
  is typed and still say how many there are.
- **The agent page's shape (#368).** The Access tab opens on **What this agent can reach**:
  the vendor list with logos, what the agent holds shown under each vendor, then pick a
  vendor, pick the level, Confirm; the runs-as identity line stays. People moved to an
  **Owners** button in the header beside Edit details (same sheet, same roles). Other agents
  moved to a Harness section, **Agent to agent**. The Settings tab is now **Secrets** and
  holds secrets and variables only; **Build log** is a header button beside Health;
  **Destroy this agent** sits at the bottom of Edit details behind the same confirm. Tabs:
  Chat, Slack, Access, Harness, Secrets. The guide's sections follow.

## [0.3.28] - 2026-11-01

### Changed

- **GitHub per agent: a repository set and a level (#365).** The room's GitHub connection is
  never granted to an agent. Each agent's GitHub access is a child integration
  `<connection>-<agent>` in the permission service, carrying the repository set its owner picks
  on the Access tab from the installation's repositories (`GET`/`PUT
  /api/agents/{name}/github/repositories`, owner or manager; strays and an empty set are 422,
  no GitHub connection is 404), and a level requested on it: Read (`github_repos_read`), Review
  pull requests (`github_pulls_review`) or Open pull requests (`github_pulls_write`). `GET
  /api/agents/{name}/access` shows GitHub as that agent's own set (or the connection as a
  placeholder while none is picked), never another agent's; every account entry carries
  `repositories` and `has_repo_set`, every permission carries its `label`; Review rows and
  decide results carry `repositories` for a request on a child.
- **Lanes per permission.** Read opens the read-only `github-mcp` target (`github:read`);
  Review pull requests opens `github-mcp-write` (`github:review`); Open pull requests adds
  `github:write`. The gateway's `github` handler is in no lane. `follow` recomputes on approve
  and revoke and now withdraws allow patterns the agent no longer earns (one commit, "kobo:
  allow the ... tools" or "kobo: withdraw the ... tools", one restart).
- **Two live targets per GitHub connection.** `<id>-mcp` (read-only header, `github:read`) and
  `<id>-mcp-write` (no header, `required_scope github:review`, `tool_scopes` sending the
  branch-, commit-, pull-request- and issue-creating tools to `github:write` and merge and
  delete to `github:never`), attached, written into the gateway pull request fallback and
  removed together. The connection's ceiling is every `github` catalogue permission; the
  room-level "Repositories agents may be granted" field is gone (an older row that has it is
  read as before).
- **Guide.** The Access tab holds who can reach the agent and what it can reach; Permissions
  moved there from the Harness; GitHub is repositories first, then a level.

### Removed

- **The pull request broker's lane for agents.** `POST /broker/agents/{name}/github-token`,
  `POST /api/agents/{name}/repo-access`, `GET /api/repo-access`, `POST /api/repo-access/decide`,
  the MCP tools `kobo_request_repo_access`, `kobo_list_repo_access` and
  `kobo_decide_repo_access`, and the composed `github` sidecar block (`BundleSpec.broker_url`).
  `pr_broker.py` keeps only the agent token verification the Jira lane shares. The Jira lane
  and the room's own GitHub client are untouched.

### Migration

- `repo_access.migrated_at` (alembic `e1f2a3b4c5d6`). At boot, with the permission service on
  and a GitHub connection present, every agent with approved repo-access rows and no GitHub
  child gets the child with those repositories and an approved Open pull requests grant, its
  lane brought in step, the rows stamped and an audit row written; an agent that already
  picked a set keeps it. Without a connection the rows wait for the next boot. Nothing an
  agent held is lost.

## [0.3.27] - 2026-10-31

### Changed

- **The room's look: the shadcn-svelte foundation, on the room's own tokens.** Tailwind v4 and
  shadcn-svelte (style Vega, Stone palette, Geist headings, Public Sans body, Geist Mono for
  names and labels, Phosphor icons) are wired in; `ui/src/lib/shadcn.css` maps shadcn's tokens
  onto app.css's, so the room still has one palette in one file and dark mode still follows
  the operating system. No production screen imports a shadcn component yet; the restyle below
  is the room's own CSS on the new tokens, and Tailwind's preflight is deliberately off until
  the hand-rolled components are gone.
- **Shell.** White page, a floating dark rail with the dot-matrix KOBO wordmark (the hammer
  lives on as the favicon), Phosphor icons, and the main content in a warm grey frame.
- **Dashboard.** Cards carry a portrait (or the agent's initial), a two-line description, an
  inset facts tile, and a Health button that opens the agent's dashboard. The filter bar is one
  row (Group, a new Status filter, Search). "Made by" reads "Author:"; a failed or waiting card
  shows the real age and author instead of "Open for details".
- **Agent page.** A header card (portrait is the portrait menu; Repository, Health and Edit
  details as buttons; labelled facts row), a back button to the cards, underline tabs with
  icons, the first reply as the chat's opening message, an inline composer, Shared thread and
  Colleague conversations side by side. Slack, Access and the Harness sections read as settings
  rows; permission lists and vendor rows line up; "Convert..." and "Destroy..." lost their
  ellipses; "Add someone" is "Add person".
- **Getting started.** One top bar (section picker, previous/next, a progress track) instead of
  a second side list; the section in a centred reading card with previous/next at the bottom.
- **Admin.** A page header, an icon navigation card, Overview as status cards, Review as
  Waiting/Everything tabs with a selection bar, the integration catalog as alphabetical vendor
  cards with logos, Audit's filters in one panel with Export beside the title, and the role
  editor as settings rows. Every native dropdown and field in the room shares one style.
- **Account menu** no longer lists the guide; the rail's Getting started entry is the one way in.

### Added

- `make -C api lab`: a local room over the test fakes with seeded agents in every state the UI
  draws, for working on the UI without a cluster (`api/scripts/ui_lab.py`).

### Removed

- The `FirstReply` component (its content moved into the chat log).

## [0.3.26] - 2026-10-31

### Changed

- **The GitHub MCP catalog authenticates with a token the gateway mints, not a stored one** (#360).
  The `github-mcp` target names `catalog_token_provider = "github-app"` (liminal 0.9.2) instead of
  a catalog `Authorization` header liminal refuses; the gateway mints its own App installation
  token before every catalog fetch, so no personal token is stored or rotated. The vendor
  checklist records the rule. Needs the gateway on liminal 0.9.2.

## [0.3.25] - 2026-10-31

## [0.3.24] - 2026-10-31

### Fixed

- Approving an agent's first permission on a vendor now also lets the agent see that vendor's gateway tools: its bundle's allow list gains the vendor's patterns as one commit and one restart. Before, the grant and the gateway lane were set and the runtime still offered none of the tools, because the bundle's allow list never named them (#353).

## [0.3.23] - 2026-10-31

### Changed

- Admin > Services is a status page with one action, Reload org config. The **Wire agents to the tool gateway** button from 0.3.22 and the **Sweep directory** button are gone, with their routes: new agents get the gateway at create, older ones are migrated by hand once, and the room retires stale switchboard directory entries itself each night, writing an audit row as the room (#351).

## [0.3.22] - 2026-10-30

### Added

- **Admin > Services: Wire agents to the tool gateway.** A room that named its tool gateway (\`liminal_url\`) after its agents were made had agents with permissions and no provider to use them with. The button gives every READY agent of the room's own the composer's gateway block, as one commit and one restart each, and leaves one that already carries it alone. \`GET/POST /api/admin/agents/gateway-wiring\` (#349).

### Fixed

- Admin > Overview no longer flashes the Connections row on a hard refresh; the delivery chip keeps polling while the tab is in the background, so it stops pulsing when the change lands rather than on the next reload (#348).

## [0.3.21] - 2026-10-30

### Changed

- Skills: the catalog and Suggestions rows read **Select**, the bar says the selected skills go out together as one update and one restart, and the one button reads **Add N skills**; before, each row read Add and invited one add, one save, one restart at a time (#345).

## [0.3.20] - 2026-10-30

### Fixed

- A change the room pushed counts as delivered once the agent's new pod is available, not once the manifest carries the commit. With one replica and a Recreate strategy that moment is the restart itself, so the header said Down for a roll the room had started; it now keeps saying Updating until the agent is back (#344).

## [0.3.19] - 2026-10-30

### Changed

- Admin > Integrations: the Slack and GitHub rows read Connected like every other integration. A value an older install's deployment config carries is marked so and offers Replace, which moves it into the room, where the room's value wins; a fresh room has nothing baked in and an admin pastes both (#337, #339).

## [0.3.18] - 2026-10-30

### Added

- **A connect adds the vendor's target to the gateway live.** With the gateway's admin plane wired (liminal 0.9.0), connecting Datadog, or a token vendor, puts its target on the gateway through `PUT /admin/targets/<name>` the moment the credential is accepted; the row reads Connected at once, with no pull request and no button. Disconnecting removes it the same way. A gateway that predates the lane, or a room with no admin plane, gets the pull request as before, and the row says which (#335, liminal #222).
- **A row waiting on a gateway pull request learns how it ended.** The Integrations listing reads each waiting row's pull request: merged makes it Connected and drops the link, closed without merging makes it Failed and says to add again (#334).

### Fixed

- Gateway add and remove are separate pull requests, on separate branches; opening one closes a stale opposite one, so a row never links to a change that would undo it. A connect whose edit changes nothing on the gateway repo reads Connected with "the gateway already carries the target" (#333).
- The Add button spins and names its step; the row reads Removing while a remove runs; a second remove is "already removed", not an error (#332, #333).

## [0.3.17] - 2026-10-30

### Fixed

- **Gateway edits go out as a pull request's branch, never as a push to the gateway's main.** The pull-request helper committed and pushed the checked-out default branch before creating the branch, so every connect landed on the gateway repo's main first and the pull request then failed with nothing to commit; a Datadog connect doubled the `datadog-mcp` stanza on main and the gateway refused to start. A gateway edit now pushes only its branch, and a test pins that (#330).
- Removing or replacing a target strips every stanza of that name with the comment lines above it, whoever wrote it, so a hand-written stanza is replaced rather than doubled (#330).
- The Datadog connect waits up to a minute for the permission service to see the Secret it just wrote (a mounted Secret takes the kubelet a moment), and the waiting sentence no longer asks for a restart (#330).

## [0.3.16] - 2026-10-30

### Added

- **Datadog's MCP server as a gateway target, on each agent's leased key.** Connecting Datadog through the permission service now also opens a pull request on the gateway's config repository adding the `datadog-mcp` target (Datadog's hosted MCP server for the site, `auth_method = "service-headers"`, liminal 0.8.0): every agent's call carries the application key the permission service leased it, the catalog fetch uses the gateway's own Datadog key. The row reads Pending until that merges and the gateway rolls; disconnecting opens the pull request that removes it. The Integrations form shows the Datadog MCP server address, prefilled from the site (#327).
- **The gateway lane follows the permission.** Approving an agent's Datadog permission sets its gateway grant too (`datadog:read`, handlers `datadog` and `datadog-mcp`), revoking its last one takes the lane back, and Review > Sync agents brings every agent in step; scopes and handlers the room did not put there are left alone. The permission is the only thing anyone grants (#327).

### Fixed

- Removing a gateway target now also removes its `[target.*]` sub-tables from `targets.toml`; before, a target with an auth table left those lines behind.

## [0.3.15] - 2026-10-30

### Fixed

- Datadog connect: the permission service's "cannot mint for datadog yet" answer (no Secret read yet) is the not-configured case, so the Secret stays and the row waits for a restart instead of being refused; the Datadog form hides the vendor address and the read/write choice, which do not apply (#322).

## [0.3.14] - 2026-10-30

### Added

- **Datadog through the permission service.** Integrations > Datadog takes a service account id, the API key and a management application key; the room writes them where the permission service reads them, probes, and connects Datadog with every read permission as the ceiling. Each agent then borrows a one-hour application key scoped to its grants, through the tool gateway (#322).

## [0.3.13] - 2026-10-30

### Fixed

- Admin > Integrations no longer lists the permission service's per-agent `agents` entries; they are plumbing behind Review and the Access tab (#310).

## [0.3.12] - 2026-10-30

### Added

- **Audit log.** One `audit_log` table of what people, the room and agents did: actor, action, target, a sentence, outcome, source, ip and bounded detail, landed state; 38 mutating paths record into it; 30-day retention (`audit_retention_days`) pruned nightly; `GET /api/admin/audit` with search, filters, cursor paging and facets, `GET /api/admin/audit/export` as CSV or JSON; the Audit page and a per-agent link from Activity (#316).
- **Asking another team's agent is a permission.** With the permission service on, Request another team's agent posts an `a2a_ask` request on the target agent, approved in Review; switchboard pairs follow those grants, the same group, and accepted invites. Every ready agent is an `agents` integration in the permission service. Needs bab 0.4.5 (#310, room half).

### Fixed

- Admin: Connections no longer flashes on a hard refresh before Access is known (#314). A Can manage grant does not reach colleague requests or agent asks. The Slack set-up sheet says to use a dedicated kobo Slack account (#304).

## [0.3.11] - 2026-10-30

### Added

- Access tab: **People** carries a role per grant, **Can see** or **Can manage** (owner-type actions, never destroy); **Other agents** is its own section for cross-team requests and pairs (#309).

### Changed

- Slack and GitHub are rows of the Integrations table with Set up / Set by deployment / Not set up; the agent's Slack tab and Repositories card grey out with "Ask a kobo admin to add the ... integration" until an admin sets them up (#304, phase 1).
- Every timestamp leaves the API as UTC with a Z and is read as such by the UI, so pages show the viewer's local time (#306).
- Audit events follow their row (Applied or Failed) and read as one plain sentence, newest first, with a kind filter (#307).
- Activity rows run newest first, open on click to the full arguments, and the table scrolls sideways (#308).

## [0.3.10] - 2026-10-30

### Changed

- Activity: a summary strip (calls, refused, errors, tools used), rows grouped by day with the newest and any day with a problem open, **Problems only**, and 25 rows at a time (#300).
- Permissions picker: a cloud lists only the accounts connected under Admin > Integrations, under their organization; accounts the hub can see but nobody connected are no longer offered, and the `cloud_accounts` org key is retired (#301).

## [0.3.9] - 2026-10-30

### Added

- **Room services** under Admin > Integrations: the Slack app config tokens and the GitHub app for pull requests are set from the room and read live, stored in a Secret the room owns in its own namespace; deployment config stays as the fallback and shows as "Set by deployment". The Slack tab and the Repositories card say plainly what is missing and where to add it (#284, #288).
- `GET /api/agents/{name}/pairs`: the agents an agent may share a thread with. The shared thread's **Add participant** lists only those; the Access tab says "Can join shared threads" on paired rows (#289).

### Changed

- Persona card reads Persona, Source (Built-in, or the team), Purpose, Capability; Edit details says Persona source. The colleagues row is **Asks other agents**: Yes, when it helps; No, but knows who they are; No. With the permission service on, the Harness has no Identity and access section; Permissions shows which identity the agent runs as (#285, #286, #292).
- Skills: **Save N skills** with no second confirm; rows show Pending, Building, Rolling, Active or Failed, the same for a new guardrail skill. While a change this page pushed is building or rolling, the whole agent page is frozen, with the banner saying why (#287, #290).
- Destroy fails loudly when the switchboard keeps the agent's entry, so Destroy again retries it; forget also deletes the user grants, thread seats, learnings and system tasks keyed by the agent's name and records which room projections did not catch up (#291, part).

## [0.3.8] - 2026-10-29

### Changed

- Owner QA pass of 2026-10-29. Slack: the tab says up front when the room has no Slack, and the first run picks channels and **Wakes on** before the manifest (#262). The Persona card and Edit details say **Persona team** and what it is (#263). The colleagues choice reads **Works with colleagues** with plain options (#264). A new guardrail skill starts from a template, and a plain body gets its frontmatter written for it (#265). **+ Tool** opens on a working example that states the rules (#267). The Repositories card treats a room with no pull request app as a state, not an error (#270). The Access picker lists agents that are not ready yet and says when same-group agents are already reachable (#273).
- Secrets and environment: room-set variables are locked rows and never re-sent, one name may appear once across secrets and variables, the new row has placeholders, and **Save** restarts the agent itself (#250, #271, #272).
- Teach something shows the taught line in the Learnings table at once, as a kept learning that survives reload (#269).
- Admin > Services > **Sweep directory** retires switchboard entries the room created for agents that no longer exist and do not run in the cluster, after a preview you confirm; destroy says when the switchboard still lists the agent; the agent's roster no longer defers to the live directory (#274, room side).
- **Skills are picked in one go.** The catalog and Suggestions lists share one picker: tick skills, press **Add N skills**, confirm once. One request (`POST /api/agents/{name}/skills/catalog/batch`, 1 to 20 picks) lands them in one change and one rebuild; a pick the agent already carries or the catalog no longer serves is refused on its own and the rest still land. The chosen rows show in the table at once as Delivering, the delivery chip sits beside the table, and errors show in both lists (#266).
- **Writes to one agent queue instead of racing.** The skills, tooling and intake lanes hold a per-agent lock from clone to push, so two changes at once no longer fail as "couldn't reach your agent's files" or rotate the agent twice. While a change this page pushed is still rolling, the agent header reads Updating rather than Down (#268).
- AWS organizations: one external id per organization, so one spoke template deploys the role to every member account (a StackSet). Connecting the hub checks every account through it and connects the ones that carry the role; **Check accounts** runs that again. No per-account Add account (#275).
- Connect AWS sheet: pick Terraform, CloudFormation or by hand first; the sheet shows only that method, for the management account and for the member accounts (#276).

## [0.3.7] - 2026-10-29

### Changed

- The vendored cloud-access release is 0.2.0: the `aws/hub` module the Integrations page points at for an organization's hub role, and the CloudFormation template's trust conditions on `oaud`.

## [0.3.6] - 2026-10-29

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
