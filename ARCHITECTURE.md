---
card: repo-card:kobo
updated: 2026-10-05
anchor: .github/workflows/ci.yml sha=c1671335ba55f8061df0983d49195ecd93e97e59
---
<!-- pyproject.toml is deliberately not anchored: Dependabot moves its pins weekly and the
     CI card check would fail every one of those PRs. The version named under STATE is kept
     by hand; the entry points and seams the card describes are anchored by their own files. -->
# kobo — repo card

WHAT: the agent assembly room. A web UI + API where a signed-in person composes an agent (persona, skills, group), clicks Create, and the room provisions a running daedalus agent on gojira; then chat/teach it, wire Slack, and govern who (people and other agents) may reach it.
STATE: main; tenish-kobo (the API package) 0.2.1 and kobo-ui 0.2.1 (both move with each release); alpha, deployed, very active (recent: connected agents = a team's own repo deployed by the room, per-agent secrets/env via sero, operator group moves, re-wire revokes old colleague passes, Postgres driver pin). Design record per arc in docs/superpowers/.
STACK: Python >=3.12, FastAPI + uvicorn, SQLAlchemy >=2,<2.1 (SQLite default, Postgres for multi-replica), alembic, pydantic v2, httpx, PyJWT, `mcp>=1.15,<2` (FastMCP 1.x surface), prometheus-client, OpenTelemetry (env-gated). UI: Svelte 5 + Vite + TanStack Query, hand-rolled router (ui/src/lib/router.svelte.ts), NOT SvelteKit. Shells out to `git`, `gh`, `kubectl`, `gcloud` (baked into the image, Dockerfile).
ENTRY:
- API app factory: api/kobo_api/main.py `build_default_app()` (real wiring: config → org.yaml merge → engine → `sweep_interrupted` → `Clients.real` → Slack-store probe → boot dispatch-pairs sync) and `create_app()` (all routes are closures inside this one function; tests call it with fakes)
- container: Dockerfile (UI build stage + python runtime, UID 1001) → docker/entrypoint.sh runs `alembic upgrade head` then `uvicorn --factory kobo_api.main:build_default_app` on :8080
- dev: `make -C api install && make -C api run` (:8100); `make -C ui dev` (:5173, proxies /api)
- bootstrap CLI: `python -m kobo_api.bootstrap` (api/kobo_api/bootstrap.py; stands up an org's config repo/buckets/personas)
- MCP server: /mcp + /oauth + /.well-known, mounted only when `config.mcp_enabled` (api/kobo_api/mcp/mount.py)
- role: `KOBO_ROLE` = `all` (default) | `api` (serves, enqueues) | `worker` (claims and runs pipelines, sweeps, nightly pass); see docs/arch/provisioning.md. health.py probes ready agents' Deployments once a minute and the UI shows Down over Ready when the pods are not running.
- agent-facing broker lane: /broker/agents/{name}/github-token, /jira/* (api/kobo_api/main.py, api/kobo_api/pr_broker.py; auth = the agent's cabal colleague JWT)
- config: YAML via `KOBO_CONFIG` or ./config.local.yaml (api/kobo_api/config.py `Config.load`), then org.yaml from the org config repo merged over it at boot (api/kobo_api/org.py `load_org_config`, keys in `ORG_KEYS`). Env secrets: CABAL_OPERATOR_TOKEN, OKTA_API_TOKEN, KOBO_MCP_JWT_KEY, and the OAuth client secret (env name from `oauth_client_secret_env`, default KOBO_MCP_GOOGLE_CLIENT_SECRET). Env switch, not a secret: OTEL_EXPORTER_OTLP_ENDPOINT (tracing is on only when set).

SEAMS → card:
- provisioning    api/kobo_api/provisioner.py + steps.py  (step engine, PIPELINE/CONNECTED_PIPELINE, claim/heartbeat/sweep/retry)  → docs/arch/provisioning.md
- platform-clients api/kobo_api/clients.py + tests/fakes.py (Protocol per external system; `Clients` bag; fakes)        → docs/arch/platform-clients.md
- switchboard     api/kobo_api/pairs.py + clients.py:CabalClient (cabal directory, relay, colleague passes, dispatch allow-pairs) → docs/arch/switchboard.md
- bundle          api/kobo_api/composer.py + vendor/bundle.schema.json (the daedalus agent bundle kobo writes)  → docs/arch/bundle.md
- access-control  api/kobo_api/iap.py + main.py gates + mcp/authz.py (who sees/mutates what; UI, MCP, broker)  → docs/arch/access-control.md
- (uncarded) sero.py — secrets-platform machine lane (per-agent secrets; no fallback to direct Secret write by design)
- (uncarded) agent_env.py — per-agent Secrets & env panel: `<agent>-env` Secret, gojira.yaml `secrets_from`/env edits
- (uncarded) destroy.py — teardown stages (gojira delete, directory absorb, pass revoke, GSA, secrets)
- (uncarded) knowledge.py / teach.py / intake.py / learnings.py — memory lanes: fast teach over the switchboard + background `[skip ci]` archive to bundle/intake; nightly learnings sweep (Postgres advisory lock); each run per agent recorded in `agent_system_tasks` (`record_system_task`, flagged after `system_task_flag_after` failures); candidates screened by `is_identity_only` and `learning_concerns` (unattributed, access_change), flagged rows need `confirm`
- (uncarded) search.py / catalog.py / marketplace.py / skills.py / tooling.py / personas.py — catalogs (FTS over GCS indexes), skills/tooling marketplaces, persona store
- (uncarded) models.py + migrations/: the tables; the alembic chain is the schema of record on Postgres
- (uncarded) mcp/ — OAuth 2.1 AS (Google OIDC + PKCE), own JWTs, refresh-token families, audit log, per-replica rate limit, tools in mcp/tools/{read,write}.py

DEPS: no Python imports of sibling repos (daedalus only via `pytest.importorskip` in tests/test_composer.py). Runtime contracts, all over the network or git:
- gojira: HTTP `/api/v1/apps` create/status/provision/delete + `/healthz` (clients.py `HttpxGojiraClient`); kobo edits each agent repo's `gojira.yaml` and `k8s/base/kustomization.yaml`.
- daedalus: bundle schema vendored verbatim at api/kobo_api/vendor/bundle.schema.json; agent images `FROM` `config.runtime_image` (the daedalus runtime).
- cabal: admin API (directory, relay, token mint/revoke, dispatch-pairs, conversations, SSE) with operator bearer; kobo is the policy owner.
- sero: machine-lane secrets, auth = pod's Google ID token (sero.py).
- liminal: `liminal_url` baked into each bundle's tools block; techne: each agent's memory store on a PVC (runtime side; kobo never imports techne).
- GitHub (gh CLI + GitHub App), Okta (groups/people), Vertex (names, persona drafts, avatars), GCS (catalogs), Slack (manifest API).
HEALTH: API tests live under api/tests (pytest, `-n auto` in CI; each test owns its SQLite file; `make -C api test` locally runs serial). UI: vitest + svelte-check. CI (.github/workflows/ci.yml): `api-tests`, `ui` (build, check, test), `migrations` (alembic to head on real Postgres 16 + scripts/schema_tripwire.py), and `test-lifecycle` (TESTING.md: scaffold tests must not reach a PR). CI builds and publishes NO image (since 2026-10-02): an operator builds one from a commit and rolls it where it runs. Ruff is NOT in CI. No branch protection: CI reports, never blocks.
GOTCHAS:
- CI installs with `uv sync --locked` against the committed `api/uv.lock` (2026-10-05); a
  drifted lock fails the job instead of silently resolving. The same job runs
  `scripts/check_arch_cards.py`, so a seam changed without its card fails CI (2026-10-05). The Dockerfile's `pip install ./api`
  is still unlocked, which is how SQLAlchemy 2.1.1 slipped in and crashed boot on 2026-09-25, so
  runtime floors and ceilings are still pinned in pyproject (comment there).
- Tests run on SQLite; production multi-replica runs Postgres. A model change needs an alembic migration or the pod crashloops; CI's `migrations` job is the only thing that catches it. `init_db` (`create_all`) still runs at boot on both backends.
- main.py is very large and every route is a closure inside `create_app`; helpers you need (`_visible_agents`, `_is_admin`, gates) are module-level above it. Don't add a second visibility check per route.
- "team" ≠ "group": `Agent.team` is the skills-catalog axis, `Agent.group_name` is the ACL namespace (composer.py `BundleSpec` comment). `SHARED_GROUP = "shared"` is the commons: visible to all, but NOT implicitly paired.
- Two agent kinds (kinds.py): `composed` (room owns the repo, pushes) vs `connected` (team's own repo; room proposes by PR, no switchboard identity, no pairs). Code tests `!= KIND_CONNECTED` (or `is_connected`), so a future kind behaves as composed, with one deliberate exception: destroy.py `_destroy_repo` tests `!= KIND_COMPOSED`, so an unknown kind is treated as not composed there and its repository is left alone.
- CORS is `allow_origins=["*"]` unconditionally (main.py `create_app`), not only in dev; the perimeter is IAP, not CORS.
- ROADMAP.md is stale: "Next" still lists Tools and Colleagues (P3), which README and code show shipped.
- Several integrations are "absent = off": cabal needs cabal_url + agent_endpoint_template + CABAL_OPERATOR_TOKEN; Okta groups need OKTA_API_TOKEN + okta_domain + groups; sero needs all three sero_* keys; MCP needs `mcp.oauth_client_id`.
RELATED DOCS: README.md (running, two-replica and sero ops notes), docs/superpowers/specs/ (one design per arc, dated), docs/superpowers/plans/, api/kobo_api/templates/guide/ (user guide served at /api/guide and by the `kobo_guide` MCP tool).
