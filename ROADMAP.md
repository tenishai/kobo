# Pnyx Roadmap

The phased plan at a glance. Details live in the
[design document](docs/superpowers/specs/2026-08-01-cortex-agent-assembly-poc-design.md).

## P0 — Foundations *(current)*

The chain works before the room exists: a hand-written bundle repo deploys end to end with no
manual step after one API call.

- daedalus runtime base image + convergent knowledge seeder (branch: `feat/runtime-image`)
- gojira: `agent` class readiness probe → generated Service; JSON connect/status API; opt-in
  setup-PR auto-merge (branch: `feat/agent-deploys`)
- Hand-written exemplar bundle repo + E2E runbook against the dev cluster

## P1 — The assembly room

Create → working agent, entirely from the UI.

- pnyx-api: composer (schema-validated bundles, skill materialization, diff-before-write),
  provisioner (persisted step pipeline incl. model access via group membership), GitHub/gojira
  integrations
- pnyx-ui: dashboard, single-modal create (name · persona · skills · model), plain-English
  progress checklist with adaptive polling, detail page with per-component panels
- Per-agent techne stores on PVCs

## P2 — Talk, learn & connect

- Chat/training pane against the deployed agent (a2a message/send + SSE trace)
- Knowledge editing that reseeds convergently on redeploy
- Slack integration: per-agent app from a scoped manifest template + one "Finish Slack setup" card
- Avatar generation in the persona panel
- Archive/Library extraction from per-agent memory stores

## Later

- cabal graduation (multi-agent links) · limenal tool gateway · ladon-backed secrets
- Reaction-based mutation approvals over Slack
- Observability wiring (argus) · UI auth & multi-tenancy · multi-environment promote flows
