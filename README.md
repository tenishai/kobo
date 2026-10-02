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
- **Connect your own**: take on a repository your team already owns and let the room deploy
  it. The repository field searches the repositories the room's GitHub App can reach as you
  type, and the room proposes its setup by pull request.
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
- **Fast memory**: teach a fact, edit a knowledge file, upload a memory, or
  stage a tool, and it reaches the agent in seconds over the switchboard,
  not minutes over a rebuild. Tools load on the agent's next restart, with
  the runtime re-checking them itself.
- **Threads**: pull more than one agent into a single shared conversation.
  Participants answer in turn and see what came before; your agent suggests
  a fitting colleague when a question is outside its field, and names any
  colleague whose answer informed its own. A thread belongs to the person
  who started it.
- **The agent page**: five tabs in one row. Chat is where you meet it, Slack is its wiring,
  Access is who may use it, and Settings is the back office: secrets and environment, the
  build log, and the two changes there is no way back from. Harness is what runs the agent:
  the model and the runtime it builds on (with one line saying whether that is still the
  room's current one, and an operator's button to move it), its persona, its skills, its
  tooling, its memory, and what it has learned. A connected agent's Harness shows what it runs
  on and nothing else: the room composed none of the rest.
- **Personas**: the soul an agent is built from. Archive one from the Personas page when it
  has outlived its use, and restore it later; nothing is destroyed. Good persona names pass
  the glance test: a stranger reading the card knows what picking it gets them. The
  built-ins are the main set, one for each general kind of agent, so every list shows
  them first and the personas teams built appear under them.

  A persona belongs to the group that made it. The builder asks who it is for (one of your
  groups, or Everyone), and the create dialog lists your groups' personas plus the shared
  ones, with a quiet Show all groups link for the rest. Shared is the commons: anyone may
  file there, and built-ins always live there. That list is a filter, not a lock; every
  persona stays findable by anyone. To re-file one, use Move to... on the Personas page: the
  person who made it, a member of its group, or an operator may move it (a shared one, only
  its maker or an operator), and only into a group you belong to.
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
- **skills** — the SKILL.md catalog shape. Kobo reads
  marketplace catalogs (including externally published ones) with pinned versions.
- **kaminos** — Interface Builder for bundles; kobo
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

**Two replicas**: a room can run more than one pod once nothing it writes lives on a disk only
one pod can mount. Point `db_url` at Postgres, drop `slack_tokens_path` and the `/data` volume
with it, and set `slack_tokens_secret: <namespace>/<name>` instead. Create that Secret first,
with the two keys the room reads and rewrites:

```
kubectl -n <namespace> create secret generic <name> \
  --from-literal=config_token=... --from-literal=refresh_token=...
```

Create it imperatively, exactly like that, and leave it out of GitOps. **The room writes this
Secret at runtime**, every time Slack rotates the pair, so it must not be managed by External
Secrets, by Argo CD (self-heal or prune), by any other reconciler, or co-managed with someone
else's `kubectl apply`. Anything that reverts it puts a spent refresh token back — single-use,
already consumed — and the room is locked out of Slack until a human issues a new pair by hand.
The room's service account needs `get`, `create`, `patch`, and `update` on that one Secret; it
proves it can write it once at startup (an idempotent re-apply of the pair already there) and
logs an ERROR naming the Secret if it cannot, rather than discovering it half a day later when
the token expires.

Slack's refresh token is single-use, so both replicas rotate against that one object: each
re-reads it before spending a refresh token and again after a failed rotation, and takes the
other's pair when it finds one. The nightly learnings sweep is claimed by a Postgres advisory
lock, so exactly one replica sweeps per round and the other logs that it skipped; the catalog
rebuild takes one too.

Roll with `maxSurge: 1`, `maxUnavailable: 0` (a `RollingUpdate` strategy, not `Recreate`), so a
new pod is ready before an old one goes away. Give the container a `preStop` hook that sleeps
for a few seconds before shutdown — the load balancer needs that long to stop sending the pod
new requests, and without it a roll drops the connections in flight.

Get onto this version first, though, before switching the strategy. A running provision proves
it is alive by writing a heartbeat, and versions before this one never wrote one — so a new pod
booting beside an old one would read the old pod's live provisions as interrupted and fail them.
Roll this version out under the existing `Recreate` strategy (or at a moment when nothing is
provisioning), and switch to rolling updates once every pod is beating.

Three more things to expect. On a cold start straight to two replicas, both pods run migrations
and one loses the race and restarts once; it is harmless and it settles by itself. Session-level
advisory locks belong to a database connection, so route the room through a direct connection or
a pooler in session mode — transaction pooling breaks them. The MCP rate limit counts per
replica, so two pods allow twice the configured rate overall.

**Secrets platform**: a room can hand the two secrets it creates for an agent — the pass that
lets agents talk to each other, and the Slack tokens an operator pastes in — to a secrets
platform instead of writing them into the agent's namespace itself. Set all three of
`ladon_url` (the platform's base URL), `ladon_audience` (the audience its machine lane expects
in an identity token) and `ladon_namespace` (the team namespace this room's secrets live in
over there). With any of the three missing the room writes those Secrets directly, exactly as
it always has.

What changes with it on: the values are stored once on the platform, recorded against the
person whose action produced them (the creator of the agent, or whoever is wiring Slack), and
the platform delivers them into the agent's namespace as the same Secret names the deploy
contract already names — so a running pod, a repo's `gojira.yaml`, and a deploy see the same
contract as before. The room authenticates as its own workload identity; there is no token to
configure or store, which is also why this is a deployment setting and not something an org
config hands out, and why a local room leaves it off. There is deliberately no fallback: if the
platform refuses or is unreachable, the step fails with that platform's own sentence and is
retried, rather than quietly writing the Secret a second way and leaving two sources of truth.
The room's own Slack app-config token pair (`slack_tokens_secret` above) is unaffected and
stays where it is.

What also changes is the timing, which matters on a re-wire. Delivery through the platform is
eventually consistent: the value is stored immediately, but the agent's namespace only carries
it once the platform re-reads the path. The room asks for a refresh interval of a minute rather
than the platform's hour-long default, so a rotation lands within minutes — but it is not in
place the moment the call returns, the way the direct write was. A pod restarted inside that
window can still be holding the previous tokens until the next refresh; it needs no second
action, only the wait. A step reporting success means the value is stored, not that the cluster
has caught up.

With the platform on, every agent page also carries a **Secrets and environment** section, at
the top of its Settings tab, for anyone who may edit that agent, composed and connected alike. Secrets are held on the platform
at `apps/<agent>/env` and delivered into the agent's namespace as the Kubernetes Secret
`<agent>-env`; environment variables live in the agent's own `gojira.yaml`, for values that are
not sensitive. A secret's value is sent once and is never readable again from the room: the list
shows key names and nothing else, and there is no reveal. Key names are upper snake case, 64
characters at most, 100 per list, and the names the room fills in itself (or the container
does) are refused. The first secret an agent gets adds `<agent>-env` to `secrets_from`,
committed for an agent the room composed and proposed as a pull request for one whose
repository belongs to its team; removing the last one takes that entry back out the same way.
Every stored row shows whether the platform has actually delivered it (live, syncing, or not on
the agent yet), read off the delivery object and never off the Secret itself, and the two
secrets the room sets for an agent are listed beside them as locked rows. A change reaches the
agent at its next boot, so the panel has a **Restart agent** button that rolls its pods now. The
MCP tools `kobo_list_agent_secrets`, `kobo_set_agent_secrets`, `kobo_set_agent_env` and
`kobo_restart_agent` do the same four things. Destroying an agent
now removes its stored secrets too, both the cluster objects and the paths, which closes the gap
this section used to name.

**Org wiring**: a room serves one org. The org's config repo carries `org.yaml` — catalogs,
models, groups, admin groups, the runtime pin — cloned at boot and merged over local defaults.
The bootstrap CLI stands up a new org (config repo, buckets, seeded personas) idempotently.

## Documentation

**For people using the room:** the Getting started page inside the room is the
full user guide, one section per part of the room (first agent, the agent page,
secrets, Slack, access, the harness, agents talking to agents, personas, the
connector, a glossary). Its text is packaged with the API in
[`api/kobo_api/templates/guide/`](api/kobo_api/templates/guide/), served at
`GET /api/guide`, and read by the `kobo_guide` MCP tool, so a person's own
assistant reads the same words the page shows. Edit a section there and both
surfaces change.

The design record lives in-repo and is the source of truth:

- [docs/superpowers/specs/](docs/superpowers/specs/) — every arc's design document, from the
  original assembly-room POC through access control, in chronological order
- [docs/superpowers/plans/](docs/superpowers/plans/) — the matching implementation plans
- [ROADMAP.md](ROADMAP.md) — the phased plan at a glance

## License

[AGPL-3.0-or-later](LICENSE), matching the tenishai family (gojira, techne, liminal, cabal, daedalus, argus).

Copyright (C) 2026 tenishai

Tests: see [TESTING.md](TESTING.md) for what earns a place in the suite and the scaffold rule.
