# Pnyx Roadmap

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

## Next

- **Tools** — team-authored enforcement code the runtime always executes around the
  reply; team-scoped loading, UI upload or repo-folder import through one validator
  (own spec; trust model locked).
- **Phase B — Client hosting & identity** — the room under a client's domain behind
  IAP; verified-identity header contract; client-onboarding mechanics (GitHub App,
  keyless DNS delegation).
- **P3 — Colleagues** — a2a links between agents (cabal/limenal), allow-list
  checkboxes with agent-card badges, dual enforcement.

## Later

- Reaction-based mutation approvals over Slack · Archive/Library extraction
- Observability wiring (argus) · multi-environment promote flows · ladon-backed secrets
