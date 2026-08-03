# Phase A — Org Catalogs, Persona Builder & Admin Section (design)

Decisions in this spec were made with Kyle on 2026-08-03, following the P2 close and the
pnyx rename. Phase B (hosting the room under a client's DNS with SSO) is deliberately out
of scope here and gets its own spec after a discovery pass on the reference deployment
whose UX this room already benchmarks against.

## What this phase makes true

A pnyx admin can wire a client organization into the room from an admin section — catalog
sources, cluster, runtime — and a member of that org can then assemble an agent from
**their org's own catalogs**: team-authored skills served from a bucket with instant
search, and personas that are either shipped defaults or built on the spot from a short
consistent form. tenishai itself is the first client; the flow must be exactly what a
future client (e.g. an org like the employer's) would experience.

## The org model

One org per pnyx deployment (multi-org is a non-goal). Org-level configuration —
GitHub org name, skills repo name, catalog bucket name, model catalog, cluster,
runtime image, model-access group — lives in the client org's **`agent-config`
repo**: config-as-repo, versioned, and the admin section is its editor (admin edits
are commits, the pattern knowledge editing proved). The room's gitignored local
config shrinks to machine-local concerns: file paths, token locations, `db_url`,
`admin_enabled`, and the org-config repo coordinates (the one pointer that can't
live inside what it points to).

Boot: read local config → clone/fetch `agent-config` → merge (org values win for org
concerns; a missing org repo leaves the room in "unwired" state where only the admin
section is useful).

## The catalog bucket

One GCS bucket per client (in the client's GCP project), holding both catalogs under
prefixes, with object versioning enabled:

```
gs://<org>-pnyx-catalog/
  skills/<team>/<skill-id>/...        # published from git by CI
  skills/index.json                   # search index, maintained by the publish CI
  personas/<persona-id>/persona.md    # written by the room
  personas/index.json                 # search index, maintained by the room
```

**Skills — git-authored, CI-published.** Teams push `SKILL.md` folders to the org's
skills repo (tenishai uses its existing `skills` repo; the repo name is org config, not
convention). A publish workflow — installed from a tenishai template, keyless WIF like
every agent repo's CI — validates frontmatter on merge and publishes each skill folder
to `skills/<team>/<id>/` with the source commit sha in object metadata, then rewrites
`skills/index.json` (id, name, description, team, tags, sha per skill). Both the
team-foldered repo layout (`teams/<team>/<id>/SKILL.md`) and the flat legacy layout are
supported; flat items publish under team `shared`. Assembly-time pinning carries over:
the bundle records the source sha from object metadata, and materialization reads the
bucket, not a clone.

**Personas — defaults + built, bucket-native.** No personas repo exists. The room ships
pregenerated default personas (seeded into `personas/` when the admin wires the org),
and new personas are created by the **persona builder** (below), written by the room
straight to `personas/<id>/persona.md` with creator/team metadata. Rollback is GCS
object versioning. Every persona in the bucket is available org-wide.

## Search — the bucket's front

The room's API ingests both `index.json` objects into an **SQLite FTS5** table (the API
already runs SQLite) and serves `/api/skills/search` and `/api/personas/search`:
ranked, as-you-type full-text over names, descriptions, teams, and tags, with a team
filter parameter. The index refreshes on a short TTL plus an admin "refresh catalogs"
action. This is deliberately not Elasticsearch: at catalog scale (dozens to hundreds of
entries) FTS5 gives equivalent lookup quality with zero new infrastructure. The UI
depends only on the search endpoints, so a standing search service (Elastic, or GCP's
managed search) can replace the internals later without the UI changing — that is the
documented scale-up path, not the starting point.

## The persona builder

Creating a persona is a form, not a file. Fixed fields (all short, all required unless
noted):

- **Name** — the persona's title (e.g. "Networking Navigator").
- **Role** — one line: what this agent is (e.g. "an engineering-network specialist").
- **Purpose** — 2–3 sentences: what it helps people do.
- **Tone** — a choice among a fixed set (warm · direct · playful · formal) plus an
  optional free line.
- **Boundaries** — things it must not do or claim (optional, bulleted).
- **Specialty tags** — a few words used for badges and search.

pnyx renders `persona.md` from one fixed template with consistent sections (Identity /
What you do / How you speak / Boundaries). Consistency comes from the template — every
persona has the same structure and quality floor. Rendering is deterministic string
templating (no model call). The built persona is written to the bucket (org-wide
reuse) and referenced by the new agent's bundle like any catalog persona.

## Create-modal changes

- **Persona step**: shipped defaults and org-built personas presented together (badged
  by team/origin), plus "Build your own" opening the persona-builder form inline.
- **Skills step**: search box backed by `/api/skills/search`, team filter, team badges
  on results. Selection behavior and sha-pinning unchanged.

## The admin section

A config-flag-gated `/admin` route (`admin_enabled` in local config; real identity
arrives with Phase B, where it is unavoidable). It shows the org connection as a
checklist with actions:

1. Org set (name from `agent-config`; or the create-from-template button when the repo
   is missing).
2. Skills repo reachable (+ "install publish workflow" action that opens a PR adding
   the template workflow to the skills repo).
3. Catalog bucket exists (+ "create bucket" action; enables versioning).
4. Defaults seeded (personas published; runs idempotently).
5. Cluster registered / runtime image pinned / model group set (edits committing to
   `agent-config`).
6. "Refresh catalogs" (re-ingest the indexes).

The admin section is the one surface where operator vocabulary is permitted; the rest
of the room keeps the no-infra-words rule.

## Testing

House pattern: TDD throughout; fakes for GCS (a `StorageClient` Protocol beside the
existing client Protocols), the publish workflow's validator covered in the template
repo, FTS5 search unit-tested against seeded indexes, persona template rendering pinned
byte-for-byte, admin actions exercised route-level with fakes. Live milestone below is
the integration gate.

## Milestone (exit criterion)

Through the admin section alone, wire tenishai as pnyx's first client: create
`agent-config` from template, create the catalog bucket, install the publish workflow
into `tenishai/skills` (merge its PR), watch the existing skills publish and appear in
search, and see default personas seeded. Then create a brand-new agent using a
form-built persona and a bucket-searched, team-filtered skill — it reaches READY with
first words. That is a client onboarding, end to end, minus DNS.

## Non-goals (this phase)

Multi-org deployments; Elasticsearch/managed search; personas in git; editing shipped
default personas; Phase B (gojira-deploys-pnyx under a client domain, SSO/identity,
the ingress recipe from the reference deployment); migrating the four existing agents'
bundles.
