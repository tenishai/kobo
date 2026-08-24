<div align="center">

# Kobo

**The agent assembly room for [gojira](https://github.com/tenishai/gojira).**
Compose an AI agent — persona, skills, memory, colleagues — and deploy it with one click. Then teach it, connect it, and govern who sees it.

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

- **One click** — after Create there is no human step between the user and a running agent.
- **Composed, not coded** — an agent is assembled from catalogs; the generated repo carries
  only the bespoke parts. Everything else is the platform's.
- **Memory is instant, rebuilds are for code** — teaching an agent a fact lands in its memory
  in seconds through the switchboard; the repo archives in the background. Images rebuild only
  for code-shaped changes (skills, tooling, runtime pins).
- **Consent at the wire** — agents in the same group talk freely; cross-group talk requires an
  invite the receiving agent's owner accepts, enforced at the switchboard's token re-mint, not
  by UI politeness.
- **Fail closed** — new agents start with an empty tool allowlist and deny-all approvals;
  group grants vanish when the directory is unreachable; a lost policy sync heals at the next
  boot, trigger, or operator action.
- **No infra vocabulary** — creating an agent never mentions the machinery; the plumbing shows
  up only as a plain-English progress checklist.

## What the room does today

- **Create**: describe the job (or paste a full persona document — the discovery metadata
  distills itself in the background), pick a name from the ceremony, pick skills, pick a
  group, click Create. Minutes later the agent answers its first hello.
- **Chat & teach**: talk to any agent you can see. Press *Teach this* on any exchange and the
  fact is in the agent's memory in seconds, provenance-stamped with your identity. Corrections
  retire stale facts in-session via the agent's own belief-revision pass.
- **Memory**: upload notes or docs for one-shot learning; taught chat notes archive to the
  repo quietly (`[skip ci]`) and fold away in the UI. Memories outlive their source files.
- **Colleagues**: agents discover each other through the switchboard directory and consult
  each other mid-conversation (`ask_colleague`), with colleague-sourced facts held at low
  confidence until quorum cross-checking earns them in. A terminal-style feed shows one
  agent's whole conversation history, in order, in full.
- **Access**: verified identity in the corner; owners on every card; owner-or-operator gates
  on every mutating surface; team-scoped visibility from your directory's groups; a shared
  commons anyone can create into; per-agent grants for people and invite handshakes for
  agents; an admin team, not an admin list.
- **Slack**: self-service wiring with a manifest ceremony — the agent answers mentions in
  threads, with long replies chunked at safe boundaries.
- **Faces**: a generated portrait per agent, or upload your own.
- **MCP**: the room's tools are reachable from MCP clients (claude.ai connectors, Claude Code)
  as the signed-in user, over the room's own OAuth — no service account, no second permission
  system. Every call resolves the same visibility, ownership, and admin as the UI: check who
  you're signed in as, list and inspect the agents you can see, read an agent's conversation
  feed, browse and create personas, create an agent, teach it, chat with it,
  run the colleague-invite handshake, and wire it to Slack. Browse the org's
  skills and tooling marketplaces, stage either onto an agent you own, and
  publish new entries under your own team (publishing is instant and org-wide;
  tool publishing runs the same mechanical law the room enforces, and baseline tool ids are
  reserved for operators). Every new agent ships with the baseline tooling pack, a leak guard
  and a probe guard, unless created with skip_baseline. Off until the org turns it on.
  Personas can be archived and restored from the Personas page, and good persona names pass
  the glance test: a stranger reading the card knows what picking it gets them.

## Part of a larger platform

Kobo is one piece of a shared, opinionated agent platform — sibling projects that each own one
concern and compose:

- **[gojira](https://github.com/tenishai/gojira)** — the deploy platform (identity, CI/CD,
  manifests, teardown). Every assembled agent deploys and dies through it.
- **[daedalus](https://github.com/tenishai/daedalus)** — agent construction kit and runtime.
  The bundle is kobo's unit of assembly; the runtime carries the teach lane, the colleague
  tools, and the belief-revision pass.
- **[cabal](https://github.com/tenishai/cabal)** — the switchboard. Chat, teach, and every
  agent-to-agent consult relay through it; it enforces the room-synced dispatch allow-pairs
  and records every conversation.
- **[techne](https://github.com/tenishai/techne)** — agentic memory. Each agent embeds its own
  store on a PVC; the seeder converges knowledge at boot and the teach lane writes it live.
- **[skills](https://github.com/tenishai/skills)** — the SKILL.md catalog shape. Kobo reads
  marketplace catalogs (including externally published ones) with pinned versions.
- **[kaminos](https://github.com/tenishai/kaminos)** — Interface Builder for bundles; kobo
  borrows its editor patterns and test console.
- **[liminal](https://github.com/tenishai/liminal)** — the MCP gateway agents point at for
  real tool capabilities.
- **[ladon](https://github.com/tenishai/ladon)** — secrets on OpenBao via External Secrets.

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

## Running it

**API (dev)**: `make -C api install`, copy `api/config.example.yaml` to `api/config.local.yaml`
and fill it in, then `make -C api run` (serves on `:8100`). The dev API is unauthenticated and
CORS-open — localhost only. **UI (dev)**: `make -C ui install` then `make -C ui dev` (`:5173`,
proxying to the API).

**Container**: one image, UI + API, from the repo-root `Dockerfile` (`docker build -t kobo .`).
No baked configuration or secrets — mount a config file and point `KOBO_CONFIG` at it, with
`ui_dist: /srv/ui` so the API serves the baked UI. Listens on `:8080`, runs non-root, with
`git`, `gh`, `kubectl`, and `gcloud` on `PATH` for the operations the API shells out to.

**Org wiring**: a room serves one org. The org's config repo carries `org.yaml` — catalogs,
models, groups, admin groups, the runtime pin — cloned at boot and merged over local defaults.
The bootstrap CLI stands up a new org (config repo, buckets, seeded personas) idempotently.

## Documentation

The design record lives in-repo and is the source of truth:

- [docs/superpowers/specs/](docs/superpowers/specs/) — every arc's design document, from the
  original assembly-room POC through access control, in chronological order
- [docs/superpowers/plans/](docs/superpowers/plans/) — the matching implementation plans
- [ROADMAP.md](ROADMAP.md) — the phased plan at a glance

## License

[AGPL-3.0-or-later](LICENSE), matching the tenishai family (gojira, techne, liminal, cabal, daedalus, argus).
