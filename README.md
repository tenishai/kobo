<div align="center">

# Kobo

**The agent assembly room for [gojira](https://github.com/tenishai/gojira).**
Compose an AI agent — persona, skills, memory, colleagues — and deploy it with one click. Then teach it, connect it, and govern who sees it.

[![CI](https://github.com/tenishai/kobo/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/tenishai/kobo/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-AGPL--3.0--or--later-blue.svg)](LICENSE)
[![Status](https://img.shields.io/badge/status-alpha%20%C2%B7%20live-green.svg)](docs/superpowers/specs/)
[![Bundles: daedalus](https://img.shields.io/badge/bundles-daedalus-6f42c1.svg)](https://github.com/tenishai/daedalus)
[![UI: Svelte 5 · API: FastAPI](https://img.shields.io/badge/UI-Svelte%205%20%C2%B7%20FastAPI-ff3e00.svg)](docs/superpowers/specs/)

[Specs](docs/superpowers/specs/) &nbsp;·&nbsp; [Roadmap](ROADMAP.md) &nbsp;·&nbsp; [Plans](docs/superpowers/plans/)

</div>

---

## Overview

Kobo makes what daedalus defines **deployable and governable by anyone**. Daedalus gives an
agent its anatomy (a bundle: persona, wiring, identity), gojira gets workloads running on
Kubernetes, and cabal gives agents a switchboard to talk through. Kobo is the room where they
all meet: a web UI where a person on any team assembles an agent, clicks **Create**, and gets
a running colleague — then keeps teaching it, wiring it to Slack, and deciding which other
agents and people may reach it.

Kobo is opinionated by design:

- **One click** — for an agent the room composes, there is no human step between Create and a
  running agent (the deploy platform's setup pull request auto-merges, `api/kobo_api/steps.py`).
  A repository you connect keeps its own setup pull request for your team to merge.
- **Composed, not coded** — an agent is assembled from catalogs; the generated repo carries
  only the bespoke parts. Everything else is the platform's.
- **Memory is instant, rebuilds are for code** — teaching an agent a fact lands in its memory
  through the switchboard without a rebuild; the repo archives in the background. Images rebuild
  for changes to the bundle's files (skills, tooling, persona, runtime pins).
- **Consent at the wire** — agents in the same group talk freely; cross-group talk requires an
  invite the receiving agent's owner accepts, enforced at the switchboard's token re-mint, not
  by UI politeness.
- **Fail closed** — a new agent's tools are only what its bundle lists: nothing outside the
  allowlist is visible to the model. With the switchboard on, that list is the colleague tools
  (and the PR broker's read tools when the broker is on) with no approval gate in front of them;
  the write ceiling is untouched. Group grants vanish when the directory is unreachable; a lost
  policy sync heals at the next boot, trigger, or operator action.
- **No infra vocabulary** — creating an agent never mentions the machinery; the plumbing shows
  up only as a plain-English progress checklist.

## Where this fits

Kobo is one piece of a set of sibling projects, each owning one concern. The room ties them
together; it imports none of them and talks to each over the network or git.

- **[daedalus](https://github.com/tenishai/daedalus)** (required): the bundle is kobo's unit of
  assembly, and every composed agent runs on the daedalus runtime image (`runtime_image`). The
  runtime carries the teach lane, the colleague tools and the belief-revision pass.
- **[gojira](https://github.com/tenishai/gojira)** (required for `delivery_backend: deployed`, the
  default): deploys and tears down each agent on Kubernetes (`api/kobo_api/steps.py`,
  `api/kobo_api/destroy.py`). `delivery_backend: directory` composes into a folder instead.
- **[techne](https://github.com/tenishai/techne)** (inside every agent): each agent embeds its own
  memory store on a PVC; the seeder converges knowledge at boot and the teach lane writes it live.
- **[cabal](https://github.com/tenishai/cabal)** (optional, `cabal_url`): the switchboard. Chat,
  teach and agent-to-agent consults relay through it; it enforces the room-synced dispatch
  allow-pairs and keeps the conversation log. Off means direct-to-agent paths and no colleagues.
- **[liminal](https://github.com/tenishai/liminal)** (optional, `liminal_url`): the tool gateway
  agents call for real tools. With `liminal_admin_url` too, admin-panel lanes apply live and the
  Activity view reads the gateway's audit.
- **[sero](https://github.com/tenishai/sero)** (optional, the three `sero_*` keys): secrets on
  OpenBao via External Secrets. Off means the room writes the agent's Secrets itself and the
  per-agent secrets panel offers variables only.
- **skills**: the SKILL.md catalog shape; kobo reads marketplace catalogs with pinned versions.

An assistant asked "what is kobo?" should call the `kobo_guide` MCP tool with
`section="what-kobo-is"`; a person reads the same page in the room under **Getting started**
(source: [`12-what-kobo-is.md`](api/kobo_api/templates/guide/12-what-kobo-is.md)).

## What the room does today

- **Create**: describe the job, pick a name, skills and a group, click Create. When the build and
  deploy finish, the agent answers its first hello.
- **Connect your own**: point the room at a repository your team already owns and it proposes
  the deploy setup by pull request.
- **Chat and teach**: talk to any agent you can see. *Teach this* puts a fact in its memory in
  seconds, stamped with your identity.
- **Memory**: upload notes or docs; taught facts archive to the repo quietly and outlive their
  source files.
- **Colleagues and threads**: agents find each other through the switchboard, consult each other
  mid-conversation, and share a thread with more than one agent in it. An owner can set an agent
  to `roster-only` (knows who is in the room, asks a human instead) or `none` (no colleagues).
- **Access**: verified identity, owners on every card, group-scoped visibility, per-agent
  grants, invite handshakes between agents, an admin team rather than an admin list.
- **Slack**: self-service wiring; per agent, it wakes on mentions, on a thread it was mentioned
  in, or on every message in a channel.
- **The agent page**: Chat, Slack, Access, Harness (model, runtime, persona, skills, tooling,
  memory, what it has learned, activity) and Settings (secrets, environment, the build log).
- **Personas**: the soul an agent is built from, owned by a group, archived rather than deleted.
- **Activity**: every call an agent made through the tool gateway, and every refusal, on its
  Harness tab (needs the gateway's admin plane, `liminal_admin_url`).
- **Admin panel** (org key `admin_panel`, off by default): Services, Integrations (one credential
  per vendor; AWS connects as a role, no key), Connections (each agent's `read` or `write` lane),
  Roles, People and Audit, for the room's admins.
- **MCP**: the room's tools reachable from MCP clients as the signed-in user, over the room's
  own OAuth, with the same visibility and ownership rules as the UI (`api/kobo_api/mcp/tools/`).

The long form, with every rule the room enforces, is in
[docs/what-the-room-does.md](docs/what-the-room-does.md).

## How it works

```
kobo:    Create ──▶ repo (bundle + thin Dockerfile) ──▶ gojira onboarding ──▶ setup PR auto-merged
                                                                                     │
                                                                                     ▼
gojira:  GitHub Actions (keyless) ──▶ image ──▶ kubectl apply ──▶ pod: seed memories ──▶ hello
                                                                                     │
                                                                                     ▼
kobo:    directory registration ──▶ colleague pass ──▶ pair sync ──▶ the agent is a colleague
```

One shared runtime base image; each agent's image is `FROM` it plus a copied bundle. The bundle
repo is the agent's archive of record — but memory no longer waits for it: teaching writes the
agent's store directly and ledgers the content hash so the next natural rebuild changes nothing.

## The stack

| Layer | Tool |
|---|---|
| UI | Svelte 5 + Vite + TanStack Query |
| API | FastAPI (composer · provisioner · access policy · integrations) |
| Bundle format | daedalus `agent.yaml` (published JSON Schema) |
| Memory | techne (embedded SQLite per agent, convergent seeding + live teach lane) |
| Skills | SKILL.md catalogs in git/GCS, pinned at assembly |
| A2A | cabal switchboard (JWT re-mint, dispatch allow-pairs, conversation log) |
| Identity | IAP-verified assertion; directory groups resolved live, cached, fail-closed |
| Deploy | gojira → GitHub Actions (WIF) → Artifact Registry → GKE |
| Models | Vertex AI (partner + first-party, per-model serving locations) |

## Quickstart

Run the room on your laptop. You need [uv](https://docs.astral.sh/uv/) and Node 22 (uv fetches
Python 3.12 for you).

```
git clone https://github.com/tenishai/kobo && cd kobo
cd api && uv sync --locked --extra dev --python 3.12
uv run uvicorn --factory kobo_api.main:build_default_app --port 8100
```

In a second terminal:

```
cd ui && npm ci && npm run dev
```

Open <http://localhost:5173>. With no config file the API runs on a SQLite database in `api/`,
unauthenticated, for localhost only. The room opens with no agents and the full in-app guide
(Getting started, in the sidebar), and every optional integration (Slack, the switchboard, the
secrets platform, the MCP connector) stays off until configured.

Creating an agent needs somewhere to put it: a [gojira](https://github.com/tenishai/gojira) and a
Kubernetes cluster it deploys to, plus a GitHub login (`gh auth login`) for the repositories the
room writes. Copy `api/config.example.yaml` to `api/config.local.yaml`, fill in `gojira_url` and
the cluster, and restart; the file's comments describe its keys.

kobo's API pins `mcp>=1.15,<2` (`api/pyproject.toml`; it is written against the 1.x FastMCP
surface), and `tenish-daedalus[claude-code]` needs `mcp>=2.2`. Give each its own Python
environment: they cannot share one.

Tests: `make -C api check` (ruff + pytest) and `make -C ui check` (svelte-check + vitest). CI
runs pytest, the UI build and checks, and the architecture-card checker
(`.github/workflows/ci.yml`).

## Deploying it

One image, UI and API together, from the repo-root `Dockerfile`. Nothing is baked in: mount a
config file and point `KOBO_CONFIG` at it. A room serves one org, whose config repo carries
`org.yaml` (catalogs, models, groups, the runtime pin), cloned at boot and merged over local
defaults; the bootstrap CLI stands a new org up idempotently. Reference manifests are under
[`k8s/`](k8s/). Two replicas, the Slack Secret the room rewrites at runtime, and handing an
agent's secrets to a secrets platform are covered in [docs/operating.md](docs/operating.md).

## Documentation

- The **Getting started** page inside the room is the user guide, one section per part of the
  room, packaged with the API in [`api/kobo_api/templates/guide/`](api/kobo_api/templates/guide/)
  and read by the `kobo_guide` MCP tool, so a person's own assistant reads the same words.
  "What kobo is" (`what-kobo-is`) is the overview: where the room fits and every choice a
  person makes, with why.
- [docs/operating.md](docs/operating.md) and [docs/what-the-room-does.md](docs/what-the-room-does.md),
  the operator's view and the long feature list.
- [ARCHITECTURE.md](ARCHITECTURE.md) and [docs/arch/](docs/arch/), the repo card and its seams.
- [docs/superpowers/specs/](docs/superpowers/specs/) and [plans/](docs/superpowers/plans/), the
  design record, in chronological order; [ROADMAP.md](ROADMAP.md), the phased plan.
- [CHANGELOG.md](CHANGELOG.md), [TESTING.md](TESTING.md), [CONTRIBUTING.md](CONTRIBUTING.md).

## How this was built

tenishai is a small team of engineers who build with AI coding agents. Most of
this code was written in pairing sessions with Claude models in Claude Code: a
person set the design, directed the work and reviewed what landed, and the
agent wrote much of the code and docs. Many commits name the agent in a
`Co-Authored-By:` trailer, either a Claude model or the name of the
agent one of us pairs with. A commit without a trailer may still
have had AI help.

## License

[AGPL-3.0-or-later](LICENSE), matching the tenishai family.

Copyright (C) 2026 tenish and the kobo contributors
