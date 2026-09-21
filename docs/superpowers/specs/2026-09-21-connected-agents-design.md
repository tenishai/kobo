# Connected agents: bring your own repo -- design

Status: approved by the operator 2026-09-21; amended the same day after reading the code
(wiring by pull request, no waiting state, same organisation only, more than one column).
Plan: `docs/superpowers/plans/2026-09-21-connected-agents.md`.

## Why

Teams already have agents in their own repos, with their own code and runtime. Today the
only way into the room is the create flow, which composes a bundle onto the room's one
runtime. A team that wants its own code has no path, so it does the worst thing: creates an
agent in the room and then rewrites the generated repo underneath it. That happened this
month. The agent's owner replaced the runtime with a custom chat-bot loop an hour after
create; the room still believes it is a composed agent, shows tabs that cannot work, the
switchboard reports it unreachable, and it has crash-looped for days with nobody told.

The room should take those agents honestly: deploy them, show what is true about them, and
not pretend to manage what it does not.

## Operator rulings (2026-09-21)

- The room builds on ONE runtime. A team that wants a different runtime connects its own
  repo; there is no runtime choice inside create.
- A connected agent gets model access by default, the same as a composed one.
- The first version must support an agent with no HTTP port (a chat-only bot or a worker).

## Two kinds of agent

| | Composed (today) | Connected (new) |
|---|---|---|
| Repo | the room creates and owns it | the team's existing repo |
| Runtime | the room's runtime image | whatever their Dockerfile builds |
| What the room edits | the bundle, by commit | nothing after setup |
| Persona, skills, memory, learnings, teach, tools, colleagues, chat | yes | hidden |
| Deploy, build log, status, rollback, ownership, observability | yes | yes |
| Destroy | archives the repo | leaves the repo alone |

`agents.kind` is `composed` (default, every existing row) or `connected`. A connected row
also records `run_mode` (`http` or `headless`) and `secret_names`, because a pipeline step
only sees the row. One migration.

A later kind, "adopted" (a team repo that carries a real bundle on the room's runtime and
so can have every tab), is deliberately out of this version. The scan notices a `bundle/`
directory and says so; it offers nothing yet.

## The flow

"Connect your own agent" sits beside "Create agent". One form, four fields:

1. **Repository** -- `owner/name`. In this version it must be in the room's own GitHub
   organisation (`github_org`), and the room's GitHub App must be able to write to it. The
   room holds one App installation; reaching other organisations is later work.
2. **Name** -- the agent's name in the room. Same rules and the same collision check as
   create.
3. **Group** -- who owns it. Same control as create.
4. **How does it run?** -- "It serves HTTP on port 8080 and answers `/healthz`" or "It has
   no HTTP port (a chat bot or a worker)".

Then **Scan**, which reads the repo through the GitHub API (no clone; this is a new
capability for the room's GitHub client, which today can only clone) and shows a short
report, and **Connect**, enabled when there are no blockers.

### The scan

Blockers (Connect stays disabled; each says what to do):

- The room's GitHub App cannot see the repo, or has no write access.
- No `Dockerfile` at the repo root.
- The deploy platform already has an app for this repo, or the repo already carries a
  `gojira.yaml` naming a different app.
- The name is taken in the room.

Warnings (shown, never blocking):

- The Dockerfile's base image is not pinned by digest, or is not from the organisation's
  registry. (A stale floating base is how this room shipped known CVEs once already.)
- A `.env` file is committed.
- The default branch is protected, so the setup pull request will wait for a person.
- A `bundle/` directory exists: "this looks like a composed agent; connecting it means the
  room will stop editing it."
- "Serves HTTP" was chosen but nothing in the Dockerfile exposes 8080. The deploy will
  prove it either way; this only warns early.

The report always ends with the list of what will be added to the repo, by pull request:
`gojira.yaml`, `k8s/` (including one service account manifest), and one CI workflow.
Nothing else, ever.

### The pipeline

The existing step engine, with a shorter list for `kind = connected`:

| Step | Composed | Connected |
|---|---|---|
| repo | create from the composed bundle | **connect**: verify access, record the repo; creates nothing |
| avatar | generate, commit to repo | generate, store in the room only |
| onboard | deploy platform app, class `agent`, setup PR auto-merged | same call, with the class chosen by "how does it run"; setup PR auto-merged |
| access | service account, workload identity, model group | same |
| wire | pushes service account, memory volume, model env to the repo | **by pull request**: service account manifest plus `service_account`, model env and any secret names in `gojira.yaml`; no memory volume, no switchboard secret. The room tries to merge it and says so plainly if it cannot |
| build / deploy | watch CI, watch the rollout | same checks; the build watcher follows the branch head instead of treating a new commit as an anomaly, because other people push to a team's repo |
| directory / hello | list in the switchboard, first contact | **skipped** |

Rule that keeps this honest: the room never pushes to a connected repo's branches. Every
change it wants arrives as a pull request: the deploy platform's setup PR, then the room's
wiring PR. After those two it proposes nothing further unless the owner asks.

When a pull request cannot be merged by the room (a protected branch), the step fails with
the link and one instruction: merge it, then press Retry. The pipeline already resumes from
the failed step, so no waiting state is needed and no thread is held open for days.

### No HTTP port

The deploy platform's `agent` class probes `/healthz` over HTTP, and its `worker` class
needs an open TCP port. Neither fits a chat-only bot. This needs one small addition on the
deploy platform: a class for a single-replica, `Recreate`, no-HPA workload with NO network
probes (liveness by process exit). Working name `agent-headless`. It is the only change
outside this repo, it belongs to the deploy platform's owners, and it is the first thing
to agree with them. Until it exists, "no HTTP port" is shown but disabled with the reason.

### The agent page

A connected agent's page shows: status and checklist, the repo link, owner and group,
build log, deploy history and rollback, the observability links, and destroy. Every
bundle-backed tab is absent, not disabled. The card carries a small "connected" marker so
nobody wonders why it has no persona.

### Secrets

The room does not hold a connected agent's secrets in this version. The form has an
optional "Secret names" field (Kubernetes Secret names the owner will create in the
agent's namespace), passed to the deploy platform as `secrets_from`. The report says
plainly that the pod will not start until they exist. Holding and brokering these is the
Connections work, not this.

### Destroy

Same ledger as today, with two differences: the repo is never archived or renamed, and
there is no switchboard identity to retire. Platform app, namespace, service accounts,
group membership and observability are removed exactly as for a composed agent (including
the gaps listed in the destroy audit, which this work does not widen: a connected agent
has no colleague token, no chat app made by the room, and no memory bucket prefix).

### Converting an existing agent

One operator action, "Convert to connected", for a composed agent whose repo no longer
runs the room's runtime: flips `kind`, retires its switchboard identity, hides the tabs.
Nothing is redeployed. This is how the agent described at the top gets an honest page
without being destroyed and recreated.

## What this does not do

- No adopted kind, no colleagues or chat for connected agents, no gateway token. Each is a
  later, opt-in upgrade when an agent can prove it meets the requirement (an A2A card that
  answers a probe, for colleagues).
- No monorepos: one repo is one agent, as everywhere else in the suite.
- No network policy beyond what composed agents get today. Arbitrary code with model
  access is the real risk here; the mitigations in this version are ownership, the audit
  log entry for every connect, and the same group gate as create. A default-deny egress
  policy for connected namespaces is the first follow-up.

## Build order

1. The row and the engine: `kind`, `run_mode`, `secret_names` + migration; the pipeline
   picks its step list by kind; destroy honours kind; both read routes and the MCP read
   tools show `kind`. No UI, no new route. (Proves nothing composed changes.)
2. The GitHub client learns to read without cloning (permissions, one file, the root
   listing, branch protection, merge a pull request); the scan as a module and a route.
3. The connect route.
4. Connect form + the connected agent page (the first kind-driven tab model).
5. "Convert to connected".
6. The headless run mode: accepted by the API from day one but answered "not available
   yet" until a deployment names the deploy platform's class for it (absent = feature
   off). Enabling it is configuration once that class exists.

## Open questions

1. RESOLVED (operator, 2026-09-21): auto-merge by default. The opt-out is dropped from this
   version; a protected branch is the natural opt-out.
3. Later, with the deploy platform's owners: let its connect call carry the service
   account, env and secret names, so the two pull requests become one.
2. RESOLVED (operator, 2026-09-21): the same gate as create.
