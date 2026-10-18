# Cortex — Agent Assembly Room: POC Design

**Date:** 2026-08-01
**Status:** Approved design, pre-implementation
**Repo:** tenishai/cortex

## 1. What we're building

Cortex is an agent assembly room: a web UI where a user composes an AI agent — persona, skills, tools, mutation gates, seed memories — from catalogs of premade parts, and clicks **Create**. From that click, everything runs without further user action: a git repo is created, the deploy platform onboards it, CI builds an image, and the agent lands in a Kubernetes cluster with its own seeded memory store. The user watches a plain-English progress checklist and ends with a working agent they can talk to.

Cortex composes the existing tenishai platform pieces; it invents as little as possible:

| Concern | Owned by | Cortex's job |
|---|---|---|
| Bundle format + runtime | daedalus | Compose bundles against `docs/bundle.schema.json`; build the missing runtime image |
| Deploy to GKE | gojira | Drive gojira's repo-onboarding flow programmatically |
| Memory | techne | Seed `knowledge/*.md` into the agent's techne DB at boot |
| Skills | skills repo (SKILL.md standard) | Enumerate for the palette; materialize picks into bundles |
| Bundle editing UX | an earlier bundle editor (patterns) | Reuse editor/diff/node-taxonomy/chat patterns |
| Orchestration / tool gateway / secrets vault | cabal / limenal / ladon | **Deferred** — not in the POC |

## 2. Locked decisions

1. **Separate Svelte app** (Svelte 5 + Vite + TanStack Query frontend, FastAPI backend), deployed *by* gojira as a normal web app. Not a page inside gojira's UI.
2. **Daedalus runtime**, not any external/shared runtime.
3. **Repo per agent.** The bundle of record is a dedicated git repo per agent; gojira onboards each as a normal app (its data model is 1 app = 1 repo).
4. **Thin per-agent images**: one shared `daedalus-runtime` base image; each agent's Dockerfile is `FROM daedalus-runtime` + `COPY bundle/`. Built by gojira-generated GitHub Actions (keyless WIF) into Artifact Registry.
5. **Skill catalog lives in git** (the tenishai skills repo). Skills are materialized into the bundle at assembly time, pinned to a commit SHA. A bucket-backed distribution cache can be added later without rework; git stays the source of truth.
6. **Seed memories = boot-time convergent seeding.** `bundle/knowledge/*.md` ships in the image; an entrypoint seeder writes them into the techne DB on a PVC, content-hash convergent per file (editing one file and redeploying replaces only that file's seed entries; learned memories are never touched).
7. **Models via Vertex AI / model garden with group-based access.** Each agent gets a per-agent Google service account (GSA); cortex binds the agent's Kubernetes service account to it via Workload Identity and adds the GSA to a designated Google Group in the shared model-access GCP project — the group already carries the Vertex IAM. This makes every model family the group grants (Claude, GPT, Gemini, …) available with zero per-agent IAM and zero API-key secrets. (WIF-direct principals cannot join Google Groups, which is why a GSA is required.)
8. **One-click automation is a hard requirement.** After Create, no human step (including PR merges) stands between the user and a running agent. The single documented exception is Slack workspace installation (§7), which Slack does not expose an API for.
9. **Slack connectivity is opt-in per agent and semi-automated** (§7): cortex creates the Slack app programmatically from a consistent scoped manifest template; one "Finish Slack setup" action (workspace install + app-token paste) remains with the user.
10. Cabal and limenal integration deferred to post-POC phases.
11. **Sibling-repo work happens on feature branches only.** Gojira and daedalus changes live on branches; the whole loop is built and tested against those branches (branch-pinned refs/images, configurable), and PRs to main open only once the E2E works.

## 3. UX requirements

The UI must be simple enough that a user who has never touched Kubernetes can deploy an agent. Concretely:

- **Create is a single modal, not a wizard.** Four inputs, two of them pre-filled:
  1. **Agent name** — instant client-side format validation (lowercase/digits/hyphens) plus a debounced (~350ms) server-side availability check against the GitHub org, with inline "available / taken / checking" feedback.
  2. **Persona** — pick from a catalog of starter personas, or "blank".
  3. **Skills** — multi-select from the skill catalog (name + description from SKILL.md frontmatter).
  4. **Model** — a dropdown pre-selected to the latest Claude Sonnet; options are the model families the shared access group grants (Claude, GPT, Gemini, …), sourced from cortex config.
  Everything else is defaulted: transports, gates (fail-closed), memory config, Slack (off). Defaults are refined later on the detail page, never at create time.
- **Fire-and-return.** POST create writes a `pending` agent row and returns immediately; the UI toasts ("Creating <name>…"), closes the modal, and shows the new agent card with a provisioning badge. The user is never trapped watching a spinner.
- **Progress is a checklist, not a spinner.** The backend persists an ordered step array; the UI renders the same steps with plain-English labels and DONE / RUNNING / PENDING / FAILED states. Pending steps are styled identically to running (single pulse treatment) so the unstarted queue reads as "working", not "stalled".
- **Zero infrastructure vocabulary** at create time. Terms like WIF, kustomize, Artifact Registry never appear except as plain-English step names in the status checklist ("Access setup", "Image build") and in an advanced panel for power users.
- **Adaptive polling, no push infra.** TanStack Query with dynamic `refetchInterval`: 3–4s while any step is non-terminal, off once terminal. No SSE/websockets in the POC.
- **Agent detail page** panels mirror the composition model: **Persona · Config · Tools · Skills · Mutation gates · Memory** — plus a **Status/Deploys** panel. Edits produce a diff preview before commit (diff-before-write) and roll into a new deploy.
- Shared EmptyState / ErrorState-with-Retry / skeleton-loader components everywhere a query can fail; prefetch catalog data on hover of the Create button.

## 4. Architecture

### 4.1 Components

- **cortex-ui** (Svelte 5 + Vite + Tailwind + TanStack Query): dashboard (agent cards), create modal, agent detail page. Plain SPA, hand-rolled router; no SSR.
- **cortex-api** (FastAPI + SQLite via SQLAlchemy):
  - *Composer*: builds bundle directories; validates `agent.yaml` against daedalus's published `bundle.schema.json`; materializes skills (copy at pinned SHA); renders persona/knowledge files; diff preview for edits (ruamel round-trip, write-only-changed-files).
  - *Provisioner*: the ordered step pipeline (§5), run as a background task, each step persisted before/after, idempotent, resumable via a retry endpoint.
  - *Integrations*: GitHub App (create repo, commit, merge PR), gojira HTTP client (connect, status), agent a2a client (first contact, later chat).
- **daedalus-runtime image** (new; lives on a branch/PR in the daedalus repo as `docker/`): python + daedalus (`[a2a]` extra) + techne + a small seeder. Entrypoint: `seed /bundle/knowledge → $TECHNE_HOME` (convergent, per-file content hash) then `daedalus run /bundle`. Published to Artifact Registry as the base for all agent images.
- **Per-agent repos** (created by cortex, e.g. `tenishai/agent-<name>`):

  ```
  agent-<name>/
  ├── bundle/
  │   ├── agent.yaml          # daedalus wiring (validated against bundle.schema.json)
  │   ├── agent-card.json     # identity
  │   ├── persona.md
  │   ├── skills/<skill>/…    # materialized from the catalog @ pinned SHA
  │   └── knowledge/*.md      # seed memories (## sections; filename stem → memory type)
  ├── Dockerfile              # FROM daedalus-runtime:<tag>; COPY bundle/ /bundle
  └── (gojira setup PR adds: gojira.yaml, k8s/, .github/workflows/)
  ```

  Division of ownership: cortex owns `bundle/` + `Dockerfile`; gojira owns the deploy scaffolding it generates.
- **Catalogs**: skills from the tenishai skills repo (SKILL.md frontmatter drives the palette; `allowed-tools` informs `tools.allow` suggestions). Starter personas ship as templates inside cortex for the POC (`templates/personas/`); a dedicated persona catalog repo can come later.

### 4.2 Default agent.yaml (POC)

```yaml
name: <name>
card: agent-card.json
persona: persona.md
model:
  id: ${AGENT_MODEL:-vertex_ai/claude-sonnet-4-5}   # POC default; overridable per agent
  budget: { per_task_tokens: 50000 }
transports:
  - { kind: a2a, port: 8080 }
tools:
  provider: none          # detail page can enable builtin files / MCP servers
  allow: []               # empty = deny everything (daedalus invariant)
memory:
  personal: techne
  home: /techne
checkpoints: sqlite
approvals:
  ceiling: { write: false }
  gates: [{ match: "*", require: plan_approval }]   # catch-all until card-driven gating exists
  escalation: { route: cli }                         # slack route added post-POC
```

Fail-closed posture is the default and is not weakened by the UI: `tools.allow` starts empty, the daedalus `DenyAll` approver means nothing gated executes until a real approval route is wired (post-POC). The mutation-gates panel edits `allow` patterns and `gates` entries explicitly.

## 5. Provisioning pipeline

Step array (persisted per step; plain-English labels in parentheses):

1. `repo` (Repository) — create the agent repo via the GitHub App; commit `bundle/` + `Dockerfile`.
2. `bundle` (Agent bundle) — validate against schema; record pinned skill SHAs and knowledge manifest.
3. `onboard` (Platform onboarding) — call gojira connect for the repo; gojira's provisioner runs its own idempotent steps (APIs, deploy identity, registry, namespace).
4. `scaffold` (Deploy scaffolding) — wait for gojira's setup PR; **auto-merge it** (cortex created the repo; there is no human content at risk).
5. `access` (Model access) — create the per-agent GSA, bind the agent's KSA to it (Workload Identity annotation on the app-authored ServiceAccount manifest), add the GSA to the designated model-access Google Group (Cloud Identity API). Three idempotent calls.
6. `build` (Image build) — the merge triggers the generated CI; poll the workflow run to completion.
7. `deploy` (Deploy) — poll rollout status until the pod is Ready (TCP/HTTP probe on the a2a port).
8. `hello` (First contact) — send a hello message to the agent's a2a endpoint; display its first reply on the agent card. Proof of life, and the seed of the future training chat.

When Slack is enabled for an agent (opt-in, at create or later), two more steps append: `slack-app` (Slack app created from the manifest template via Slack's Manifest API) and `slack-finish` (a user-action card — see §7).

Failure model: on any step exception, remaining steps flip to FAILED, the error string is persisted, and the agent card shows a red banner with one **Retry** button that re-runs the whole idempotent sequence (`POST /api/agents/{name}/retry`). A failed first build does not block the agent record — the user can fix and redeploy from the detail page. Build failures surface the last ~50 log lines inline in the deploy row.

## 6. Changes required in sibling repos

- **daedalus** (branch/PR): `docker/` runtime image + convergent knowledge seeder + entrypoint. No format changes — the bundle schema already supports everything the POC writes.
- **gojira** (branch/PR, kept small):
  1. JSON API for connect (the existing form POST may suffice; formalize a `/api/v1/apps` create + status endpoint for programmatic use).
  2. An onboarding option to auto-merge the setup PR (or commit scaffolding directly to main) for caller-created repos.
  3. Give the `agent` workload class an HTTP readiness probe against the a2a `/healthz` so the generator emits a Service (today the class has only a TCP liveness probe, and no Service is generated without an HTTP readiness probe — the Service is required for first contact and the chat pane).
- **skills**: no changes; cortex only reads it.
- **techne**: no changes for the POC.
- **daedalus (Slack)**: no additional changes — the slack transport already exists; cortex only writes the `transports` stanza and the token env wiring.

All sibling work stays on feature branches until the E2E loop works; generated scaffolding pins branch refs/images via cortex config so cutting over to main after the PRs merge is a config change.

## 7. Slack, model access, and avatars

**Slack (opt-in per agent, semi-automated).** Cortex holds a workspace app-configuration token (deploy config) and creates one Slack app per agent from a consistent, scoped manifest template: Socket Mode on (no public ingress), bot scopes limited to `app_mentions:read`, `chat:write`, `reactions:read`, `reactions:write`, and channel history; bot display name = agent name. Slack exposes no public API for two things — installing the app to the workspace and creating the app-level Socket Mode token — so those remain a single **"Finish Slack setup"** card in the provisioning checklist: cortex deep-links the user to the install/token pages, accepts the pasted tokens, stores them as a Kubernetes Secret (referenced via `secrets_from`), enables the `slack` transport in `agent.yaml`, and redeploys. The `agent` workload class's Recreate strategy already satisfies Socket Mode's single-connection constraint.

**Model access.** The `access` pipeline step (§5) gives every agent a GSA that joins the designated Google Group in the shared model-access project; the group's existing IAM grants Vertex/model-garden access to all exposed model families. The detail page's Config panel offers the model picker (same list as the create modal); switching models is an `agent.yaml` edit + redeploy, never an IAM change.

**Avatar generation.** The Persona panel includes a "Generate avatar" action wired to an OpenAI-compatible image endpoint (deploy config). The generated image is stored in the bundle (`bundle/avatar.png`), referenced from `agent-card.json`, and shown on the agent card and detail page. Slack has no public API for setting an app icon, so applying the avatar to the Slack app is part of the "Finish Slack setup" card (the user is already on that page).

## 8. Environments and dev loop

1. **Inner loop — local, real cluster**: gojira runs in laptop mode and cortex runs locally; deploys land on the existing dev-project GKE cluster (real WIF, Artifact Registry, PVCs). Fastest way to debug the pipeline end to end.
2. **Sibling-repo branches** as needed (daedalus runtime image, gojira affordances) via normal PR flow.
3. **Closing milestone**: deploy gojira itself into the dev cluster (its documented bootstrap path), onboard cortex as a gojira app, and re-run the E2E entirely in-cluster.

The target GCP project/cluster, the shared model-access project and Google Group, the Slack app-configuration token, and the image-generation endpoint are deployment configuration, not part of this design; they are supplied via config at deploy time.

## 9. Phases

- **P0 — foundations**: daedalus-runtime image + seeder; gojira branch (API connect, auto-merge, agent-class Service). Exit: a hand-written bundle repo deploys to the dev cluster through the full chain with no manual steps after repo creation.
- **P1 — cortex**: cortex-api (composer + provisioner incl. the `access` step + integrations) and cortex-ui (dashboard, create modal with model picker, status checklist, detail page with diff-preview edits). Exit: create → working agent on the chosen model, entirely from the UI; the diagram's per-agent techne DBs exist on PVCs.
- **P2 — talk, learn & connect**: chat/training pane against the in-cluster a2a endpoint (message/send + SSE trace); knowledge-file editing that reseeds convergently on redeploy; Slack integration (manifest-API app creation + "Finish Slack setup" card); avatar generation in the Persona panel; Archive/Library extraction (team-memory export) from per-agent techne DBs.
- **Later**: cabal graduation (multi-agent links), limenal tool gateway, vault-backed secrets, reaction-based mutation approvals over Slack, observability wiring, UI auth (POC is dev-only behind whatever ingress protection the cluster provides).

## 10. Testing

- cortex-api: pytest; unit tests for composer (schema validation, skill materialization, diff), provisioner step engine (idempotency, resume-from-failure), and integration clients behind fakes (GitHub, gojira, a2a) — the sibling repos' stub-heavy test style.
- daedalus-runtime seeder: unit tests for convergence (edit one file → only its entries replaced; removed file → entries dropped; learned entries untouched).
- cortex-ui: component tests for the create modal validation states and the status checklist state mapping; `svelte-check` in CI.
- E2E: one scripted happy-path run against the dev cluster (create → hello), run manually per milestone rather than in CI for the POC.

## 11. Out of scope (POC)

Multi-environment promote flows; UI auth/multi-tenancy; cross-project data-access IAM gates; skill hot-reload at runtime; bulk memory import APIs; automatic mutation-tool gating from capability cards (tracked upstream in daedalus's capability-seam proposal); fully zero-touch Slack install (blocked on Slack's API surface — workspace install and app-level tokens have no public API).
