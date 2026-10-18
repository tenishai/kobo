# Kobo Roadmap

The phased plan at a glance. Details live in the
[design document](docs/superpowers/specs/2026-08-01-cortex-agent-assembly-poc-design.md) and the
[Phase A design](docs/superpowers/specs/2026-08-03-phase-a-org-catalogs-design.md).

## P0 — Foundations *(shipped)*

A hand-written bundle repo deploys end to end with no manual step after one API call:
daedalus runtime image + convergent knowledge seeder, gojira `agent` class with JSON
connect/status API, exemplar bundle + E2E runbook against the dev cluster.

## P1 — The assembly room *(shipped)*

Create → working agent, entirely from the UI: composer + provisioner + GitHub/gojira
integrations; dashboard, single-modal create, plain-English progress checklist,
detail page; per-agent techne stores on PVCs.

## P2 — Talk, learn & connect *(shipped)*

Chat pane over a2a · knowledge editing with convergent reseed · per-agent Slack app
from a scoped manifest + one "Finish Slack setup" card · avatar generation.

## Phase A — Org catalogs & client bootstrap *(shipped)*

One bootstrap command wires a client org: org-config repo, per-catalog buckets
(skills CI-published from the org's repo, personas room-written), FTS5 search behind
the create modal, inline persona builder, creating team recorded, read-only org
status with honest unwired states. Externally-managed publish pipelines are adopted,
never overwritten.

## Tools, hosting & identity, colleagues *(shipped)*

Team-authored tools loaded through one validator; the room behind the deployment's
identity proxy with group-scoped access; agents talking to agents over the switchboard
with allow lists and colleague modes. See the CHANGELOG from 0.1.0.

## Next

- **One published runtime, bundles as artifacts** — a composed agent stops being its own
  image: the runtime is one published image named in the deploy contract and the agent's
  files are published next to it and fetched at boot, so a data change is a restart, never
  a build (agora discussion #55).
- **Admin panel** — plugins (one base credential per vendor), connections (each agent's
  gateway lanes), roles and people, with every change a reviewed pull request (demo on
  PR #129, design in `docs/superpowers/specs/2026-10-12-admin-panel-design.md`).
- **Readiness that reflects access** — the card says when an agent is missing a gateway
  grant, a repository or a dispatch pair, and who can approve it (issues #10, #130).

## Later

- Reaction-based mutation approvals over Slack · Archive/Library extraction
- Observability wiring (noto) · multi-environment promote flows · sero-backed secrets
