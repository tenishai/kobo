# Phase A — Org Catalogs, Persona Builder & Client Bootstrap (design)

Decisions in this spec were made with Kyle on 2026-08-03, following the P2 close and the
pnyx rename. Phase B (hosting the room under a client's DNS with SSO) is deliberately out
of scope here and gets its own spec after a discovery pass on the reference deployment
whose UX this room already benchmarks against.

## What this phase makes true

An operator can wire a client organization into pnyx with one bootstrap command — catalog
sources, cluster, runtime — and a member of that org can then assemble an agent from
**their org's own catalogs**: team-authored skills served from a bucket with instant
search, and personas that are either shipped defaults or built on the spot from a short
consistent form. tenishai itself is the first client; the flow must be exactly what a
future client (e.g. an org like the employer's) would experience.

## The org model

One org per pnyx deployment (multi-org is a non-goal). The `pnyx-config` repo
configures the ROOM and nothing else — team-authored content (skills today; enforced
tools in their own upcoming phase) never lives there. Org-level configuration —
GitHub org name, skills repo name, catalog bucket name, model catalog, cluster,
runtime image, model-access group — lives in the client org's **`pnyx-config`
repo**: config-as-repo, versioned — org changes are commits to it (the pattern
knowledge editing proved), authored directly or via the bootstrap tool. The room's gitignored local
config shrinks to machine-local concerns: file paths, token locations, `db_url`,
and the org-config repo coordinates (the one pointer that can't
live inside what it points to).

Boot: read local config → clone/fetch `pnyx-config` → merge (org values win for org
concerns; a missing org repo leaves the room in the "unwired" state with honest
empty states pointing at the bootstrap).

## The catalog bucket

One GCS bucket per client (in the client's GCP project), holding the catalogs under
prefixes, with object versioning enabled. The layout, publish workflow, index shape,
and search are prefix-generic by design: the upcoming tools phase adds a `tools/`
prefix without reworking any of this phase's machinery.

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
pregenerated default personas (seeded into `personas/` by the bootstrap),
and new personas are created by the **persona builder** (below), written by the room
straight to `personas/<id>/persona.md` with creator/team metadata. Rollback is GCS
object versioning. Every persona in the bucket is available org-wide.

## Search — the bucket's front

The room's API ingests both `index.json` objects into an **SQLite FTS5** table (the API
already runs SQLite) and serves `/api/skills/search` and `/api/personas/search`:
ranked, as-you-type full-text over names, descriptions, teams, and tags, with a team
filter parameter. The index refreshes on a short TTL plus a refresh endpoint the
bootstrap invokes after publishing. This is deliberately not Elasticsearch: at catalog scale (dozens to hundreds of
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

## The bootstrap tool and org status

Wiring an org is a once-per-client operator task, so it is a tool, not a UI. pnyx
ships an idempotent, re-runnable bootstrap CLI (`python -m pnyx_api.bootstrap`,
wrapped by a make target) that: creates `pnyx-config` from the tenishai template
(when missing), creates the catalog bucket with object versioning, opens the PR that
installs the publish workflow into the org's skills repo, seeds the default personas,
and triggers the first index ingest. Each step reports done/skipped/failed plainly;
re-running converges. The same module layer could later be wrapped by a self-service
admin page once Phase B brings real identity — deliberately not built now.

The room keeps the recurring half: **visibility**. `GET /api/org/status` reports each
wiring check (config repo reachable, bucket reachable, skills index present + age,
personas seeded, cluster registered), and the UI shows honest plain-language empty
states when catalogs are absent ("catalogs aren't set up yet") instead of a broken
picker. No admin route, no admin flag; the status endpoint is read-only. Operator
vocabulary is permitted in the bootstrap CLI's output only; the room keeps the
no-infra-words rule everywhere.

## Testing

House pattern: TDD throughout; fakes for GCS (a `StorageClient` Protocol beside the
existing client Protocols), the publish workflow's validator covered in the template
repo, FTS5 search unit-tested against seeded indexes, persona template rendering pinned
byte-for-byte, bootstrap steps exercised module-level with fakes (idempotence pinned:
second run all-skipped), org-status endpoint route-level. Live milestone below is the
integration gate.

## Milestone (exit criterion)

With the bootstrap tool alone, wire tenishai as pnyx's first client: one command
creates `pnyx-config` and the catalog bucket, opens the publish-workflow PR against
`tenishai/skills` (merge it), the existing skills publish and appear in search, and
the default personas seed. `GET /api/org/status` reports every check green. Then
create a brand-new agent using a form-built persona and a bucket-searched,
team-filtered skill — it reaches READY with first words. That is a client onboarding,
end to end, minus DNS.

## Non-goals (this phase)

Multi-org deployments; an in-UI admin section (the bootstrap module is designed so a
Phase B self-service page could wrap it, if ever wanted); Elasticsearch/managed
search; personas in git; editing shipped default personas; Phase B
(gojira-deploys-pnyx under a client domain, SSO/identity via IAP, the ingress recipe
from the reference deployment); migrating the four existing agents' bundles.
