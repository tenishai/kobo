<div align="center">

# Cortex

**The agent assembly room for [gojira](https://github.com/tenishai/gojira).**
Compose an AI agent — persona, skills, tools, mutation gates, seed memories — and deploy it with one click.

[![License](https://img.shields.io/badge/license-AGPL--3.0--or--later-blue.svg)](LICENSE)
[![Status](https://img.shields.io/badge/status-pre--alpha-orange.svg)](docs/superpowers/specs/2026-08-01-cortex-agent-assembly-poc-design.md)
[![Bundles: daedalus](https://img.shields.io/badge/bundles-daedalus-6f42c1.svg)](https://github.com/tenishai/daedalus)
[![UI: Svelte 5 · API: FastAPI](https://img.shields.io/badge/UI-Svelte%205%20%C2%B7%20FastAPI-ff3e00.svg)](docs/superpowers/specs/2026-08-01-cortex-agent-assembly-poc-design.md)

[Design](docs/superpowers/specs/2026-08-01-cortex-agent-assembly-poc-design.md) &nbsp;·&nbsp; [Roadmap](ROADMAP.md) &nbsp;·&nbsp; [P0 Plan](docs/superpowers/plans/2026-08-01-p0-foundations.md)

</div>

---

## Overview

Cortex makes what daedalus defines **deployable by anyone**. Where daedalus gives an agent its
anatomy (a bundle: persona, wiring, identity) and gojira gets workloads running on Kubernetes,
Cortex is the room where the two meet: a web UI where a user assembles an agent from premade
parts — a persona, a skill set, tools, approval-gated mutations, and jumpstart memories — and
clicks **Create**. Everything after the click is automatic: a repo, onboarding, a CI-built
image, a running pod with a seeded memory store, and a first reply from the agent.

Cortex is opinionated by design. Its goals:

- **One click** — after Create there is no human step between the user and a running agent.
- **Composed, not coded** — an agent is assembled from catalogs; the generated repo carries
  only the bespoke parts (persona, knowledge, gates). Everything else is the platform's.
- **Fail closed** — new agents start with an empty tool allowlist and deny-all approvals;
  capability is granted deliberately, never defaulted.
- **No infra vocabulary** — creating an agent never mentions the machinery; the plumbing shows
  up only as a plain-English progress checklist.
- **gojira-native** — cortex composes bundles and hands deployment to gojira's contract; it
  does not duplicate it.

## Part of a larger platform

Cortex is one piece of a shared, opinionated SRE/agent platform — sibling projects that each own
one concern and are built to compose. Cortex owns agent assembly and deployment UX:

- **[gojira](https://github.com/tenishai/gojira)** — the deploy platform (identity, CI/CD,
  manifests, sizing). Cortex's parent: every assembled agent deploys through it.
- **[daedalus](https://github.com/tenishai/daedalus)** — agent construction kit. The bundle
  (agent.yaml + persona + card) is cortex's unit of assembly; agents run on the daedalus runtime.
- **[techne](https://github.com/tenishai/techne)** — agentic memory. Each agent gets its own
  techne store; cortex seeds it at boot from the bundle's knowledge files.
- **[skills](https://github.com/tenishai/skills)** — the SKILL.md catalog. Cortex's skill
  palette; picks are materialized into the bundle at assembly time, pinned to a commit.
- **[kaminos](https://github.com/tenishai/kaminos)** — Interface Builder for bundles. Cortex
  borrows its editor patterns and its talk-to-the-agent test console.
- **[cabal](https://github.com/tenishai/cabal)** — agent orchestration. A deployed agent can
  graduate into a cabal as a federated peer.
- **[limenal](https://github.com/tenishai/limenal)** — the MCP gateway. The tool provider a
  deployed agent points at when it needs real capabilities.
- **[ladon](https://github.com/tenishai/ladon)** — secrets on OpenBao. The secrets path for
  agents that need credentials, via the External Secrets Operator.

Cortex composes with these as the platform comes together; it never duplicates what a sibling owns.

## How it works

```
cortex:  Create ──▶ repo (bundle + thin Dockerfile) ──▶ gojira onboarding ──▶ setup PR auto-merged
                                                                                     │
                                                                                     ▼
gojira:  GitHub Actions (keyless) ──▶ image ──▶ kubectl apply ──▶ pod: seed memories ──▶ agent answers hello
```

One shared runtime base image; each agent's image is `FROM` it plus a copied bundle. The bundle
repo is the agent's source of truth — editing a persona or a knowledge file is a commit, and the
seeder converges memory on the next boot without touching what the agent has learned.

## The stack

| Layer | Tool |
|---|---|
| UI | Svelte 5 + Vite + TanStack Query |
| API | FastAPI (composer · provisioner · integrations) |
| Bundle format | daedalus `agent.yaml` (published JSON Schema) |
| Memory | techne (embedded SQLite, per agent, convergent boot-time seeding) |
| Skills | Agent Skills (SKILL.md) catalog in git, pinned at assembly |
| Deploy | gojira → GitHub Actions (WIF) → Artifact Registry → GKE |
| Models | Vertex AI model garden via group-based access (Claude, GPT, Gemini) |

## Status

Cortex is in **pre-alpha**. The foundations (runtime image, programmatic onboarding) and the
cortex-api backend are working end to end — one `POST /api/agents` produces a running,
memory-seeded, PVC-backed agent. The web UI is next. See the
[design](docs/superpowers/specs/2026-08-01-cortex-agent-assembly-poc-design.md) for architecture
and decisions, and the [P0 plan](docs/superpowers/plans/2026-08-01-p0-foundations.md) for the
current work.

### Running the API (dev)

To run the Cortex API locally: `make -C api install` to set up the environment. Copy `api/config.example.yaml` to `api/config.local.yaml` and fill in your values (or use examples for testing). Then run `make -C api run` — the API serves on `:8100`. The dev API is unauthenticated and CORS-open, so bind it to localhost only and never expose it beyond your machine.

## Documentation

- [Roadmap](ROADMAP.md) — the phased plan (Foundations → Assembly room → Talk, learn & connect) at a glance
- [Design document](docs/superpowers/specs/2026-08-01-cortex-agent-assembly-poc-design.md) — architecture, locked decisions, UX requirements, provisioning pipeline
- [P0 implementation plan](docs/superpowers/plans/2026-08-01-p0-foundations.md) — the foundations tasks in flight

## License

[AGPL-3.0-or-later](LICENSE), matching the tenishai family (gojira, techne, limenal, cabal, daedalus, argus).
