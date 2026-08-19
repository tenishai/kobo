# Colleagues Switchboard Implementation Plan (spec A)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Tasks 5-6 are controller-driven operations with evidence gates, not subagent work.

**Goal:** The switchboard (cabal) runs in the dev cluster; every room agent is registered in its directory at first ready and delisted at destroy; room chat relays through it; agents only accept switchboard-signed calls.

**Architecture:** A new `CabalClient` speaks three endpoints (graduate, absorb, task relay) with the room's operator token. A `directory.py` seam shares registration between a new checklist step, a destroy stage, the metadata tails, and an on-demand sync route. The composer emits the a2a auth block when org config carries switchboard coordinates. Rollout is strictly ordered: relay before locks.

**Tech Stack:** FastAPI + httpx + the existing step/destroy/composer machinery; docker buildx + kubectl for the operational tasks.

## Global Constraints

- NO em dashes in user-facing copy (UI guard and API guard both enforce; new error/detail strings stay plain).
- No vendor/client names in tracked files; vendor scan is its OWN pre-commit step per task.
- Checklist copy uses zero infra vocabulary: the step is "Joins the agent directory".
- Org config is read at BOOT ONLY; the two new keys (`cabal_url`, `agent_endpoint_template`) must be optional with safe defaults (absent = feature off, legacy behavior everywhere).
- The operator token env var is `CABAL_OPERATOR_TOKEN`; never logged, never in responses.
- Switchboard request shapes (verified against cabal source): `POST {base}/admin/persona/{name}/graduate` body `{"endpoint": str, "audience": str, "agent_card_json": dict}`; `POST {base}/admin/persona/{name}/absorb` body `{}`; relay `POST {base}/a2a/tasks` body `{"target": name, "task": <task envelope>}`; all with `Authorization: Bearer <operator token>`. JWKS at `{base}/.well-known/jwks.json`, issuer `cabal-core`.
- Relay HTTP timeout 180s (model round-trips; the curl -m 10 lesson).
- TDD RED-first. Baselines at arc start: API 1473, UI 337, check 0, build green. Runners: `venv/bin/python -m pytest -q` from api/, `npm test` from ui/.

## File Structure

- `api/kobo_api/clients.py` — `CabalClient` (graduate/absorb/relay) beside the other clients.
- `api/kobo_api/config.py` (or wherever org keys parse; trace `skills_bucket`) — `cabal_url`, `agent_endpoint_template`, both optional.
- `api/kobo_api/directory.py` (new) — `sync_directory`, `retire_directory`.
- `api/kobo_api/steps.py` — `DirectoryStep` after `HelloStep`.
- `api/kobo_api/destroy.py` — `_destroy_directory` stage before `forget`.
- `api/kobo_api/main.py` — `POST /api/agents/{name}/directory/sync`; metadata PATCH/regenerate tails; chat route relay selection.
- `api/kobo_api/composer.py` — a2a auth block at compose; `update_agent_yaml_auth(bundle_dir, *, jwks_url, issuer, audience)`.
- Tests: `test_clients.py`, `test_directory.py` (new), `test_steps.py`, `test_destroy.py`, web tests.

---

### Task 1: CabalClient + org keys

**Files:** `api/kobo_api/clients.py`, the org-config parse site, `api/tests/test_clients.py`.

**Interfaces:**
- Consumes: httpx idioms of the existing clients; org config load path (find where `skills_bucket` is read; add the two keys the same way, optional, default None/"").
- Produces: `CabalClient(base_url, token)` with `graduate(name, *, endpoint, audience, card) -> None` (raise on non-2xx), `absorb(name) -> None` (404 tolerated: already delisted), `relay(name, task_envelope) -> dict` (returns response JSON; timeout 180s; non-2xx raises a typed error whose message carries no token). Config object exposes `cabal_url: str | None`, `agent_endpoint_template: str | None`; a `cabal_enabled` truthiness helper (both keys + env token present).

- [ ] RED: request-shape matrix (paths, bodies, bearer header, timeout param), absorb-404 tolerance, error typing, enabled-helper truth table (missing key/env combinations).
- [ ] Implement; GREEN; full suite; ruff; vendor scan (own step); commit "feat(api): the room learns to speak switchboard".

### Task 2: The directory seam (step, destroy stage, tails, sync route)

**Files:** `api/kobo_api/directory.py` (new), `api/kobo_api/steps.py`, `api/kobo_api/destroy.py`, `api/kobo_api/main.py`, tests (`test_directory.py` new, `test_steps.py`, `test_destroy.py`, web tests).

**Interfaces:**
- Consumes: Task 1's `CabalClient` + config keys; the GitHub client's single-file read (trace how `agent-card.json` or org.yaml is fetched today; reuse); step framework (DONE-skip, FAILED-only retry); destroy stage isolation pattern (`_destroy_slack` is the model, including skip-notes).
- Produces:
  - `directory.sync_directory(github, cabal, cfg, agent) -> None`: fetch `agent-card.json` from the agent's repo; endpoint = `cfg.agent_endpoint_template.format(name=agent.name)`; `cabal.graduate(agent.name, endpoint=..., audience=agent.name, card=...)`. Raises on card-fetch or graduate failure (callers decide tolerance).
  - `directory.retire_directory(cabal, agent) -> None`: absorb, 404-tolerant.
  - `DirectoryStep` (key `directory`, label "Joins the agent directory") after `HelloStep`: no-op DONE when `cabal_enabled` is false (rooms without a switchboard stay green); FAILED (retryable) on sync errors; NEVER blocks earlier steps' semantics.
  - `_destroy_directory` stage before `forget`: retire; when disabled or unreachable, done-with-note "The directory entry stays until the switchboard returns." (plain copy, no em dash).
  - Metadata PATCH + regenerate: after `commit_card_metadata` returns sha, best-effort `sync_directory` (log on failure, response shape unchanged).
  - `POST /api/agents/{name}/directory/sync`: ready-gated, any signed-in user; 409 when disabled; runs sync; 502 on failure with plain detail; returns `{"registered": true}`.
- Step label: follow how existing steps surface labels to the UI (server-driven step metadata; trace `HelloStep`'s label). If a UI-side label map exists, add the entry there too and extend the UI factory/tests accordingly.

- [ ] RED: step matrix (disabled no-op DONE, sync failure FAILED retryable, success DONE), destroy-stage matrix (retire, skip-note branches), tail best-effort isolation (raising cabal client must not change PATCH response or status), sync-route matrix (gating, 409, 502, success), card fetched from repo not recomposed.
- [ ] Implement; GREEN; full suite; ruff; vendor scan (own step); commit "feat(api): agents join the directory and leave it politely".

### Task 3: Chat rides the relay

**Files:** `api/kobo_api/main.py` (chat route), `api/kobo_api/clients.py` only if the envelope builder needs exposing, tests.

**Interfaces:**
- Consumes: Task 1's `relay` + `cabal_enabled`; the existing `KubectlAgentClient._post_task` envelope builder (`_build_task_envelope`) — reuse it verbatim, do not fork the shape.
- Produces: the chat route builds the same envelope, then: `cabal_enabled` → `cabal.relay(name, envelope)`; else legacy `_post_task`. Reply extraction handles the relayed response (the switchboard returns the completed task; trace the result shape from the relay test fixture and extract the same text part as the direct path). Relay failure returns 502 with plain copy "The switchboard did not answer. Try again shortly."

- [ ] RED: selection matrix (enabled/disabled), envelope byte-parity between paths, relayed-reply extraction, 502 copy plain.
- [ ] Implement; GREEN; full suite; ruff; vendor scan (own step); commit "feat(api): chat goes through the switchboard".

### Task 4: Composer writes the locks

**Files:** `api/kobo_api/composer.py`, `api/tests/test_composer.py`.

**Interfaces:**
- Consumes: org keys from Task 1; the existing agent.yaml emission in the composer and the `update_dockerfile_runtime_image` in-place-edit idiom (ruamel or byte-faithful text edit; match whichever the composer already uses for agent.yaml).
- Produces: when `cabal_url` is configured, composed bundles' a2a transport carries `auth: {jwks_url: "{cabal_url}/.well-known/jwks.json", issuer: "cabal-core", audience: <agent name>}`; when not configured, output is byte-identical to today (pin with a test). `update_agent_yaml_auth(bundle_dir, *, jwks_url, issuer, audience) -> None` adds or replaces the auth block on the existing a2a transport entry, preserving every other line (round-trip test on a fixture with comments).

- [ ] RED: compose matrix (with/without keys, byte-identical when off), update helper preserves unknown content, replaces existing block idempotently.
- [ ] Implement; GREEN; full suite; ruff; vendor scan (own step); commit "feat(api): composed agents expect signed callers".

### Task 4b: The birth ritual reorders (whole-arc review C-1; operator ruling 2026-08-19)

**Files:** `api/kobo_api/provisioner.py` (pipeline order), `api/kobo_api/steps.py` (`DirectoryStep` placement semantics, `HelloStep` contact path + tail), `api/kobo_api/directory.py` (format-error hardening), tests.

**Interfaces:**
- Consumes: everything Tasks 1-4 built, unchanged signatures.
- Produces: `DirectoryStep` runs BEFORE `HelloStep` (join first; the card graduated at this point legitimately lacks a capability line). `HelloStep` sends first contact through `clients.cabal.relay` when `cabal_enabled` (same envelope builder, same parts[0] extraction as the chat route; extract via the same helper, do not fork it), legacy direct path when disabled. `HelloStep`'s capability tail, after `commit_card_metadata` succeeds, best-effort re-runs `sync_directory` so the directory card gains the capability (log-and-swallow, hello semantics untouched). `sync_directory` wraps `agent_endpoint_template.format(...)` errors (`KeyError`/`IndexError`) into `ClientError` so the sync route 502s instead of 500ing on a bad template.

- [ ] RED: pipeline order pinned; hello relay selection matrix (enabled relays, disabled unchanged direct); hello-tail resync isolation (raising cabal never fails hello, never alters capability storage); bad-template 502 on the sync route.
- [ ] Implement; GREEN; full suite; ruff; vendor scan (own step); commit "fix(api): join the directory first, then say hello through the switchboard".

### Task 5: Deploy the switchboard (controller-driven)

- [ ] Build the image: cabal repo, clean tree; techne sibling at the tag the runtime uses; `docker buildx build --platform linux/amd64 --build-context techne=../techne -t <registry>/cabal:<sha7> --push .` (registry = the same one the runtime lives in; read from live coordinates, never typed into tracked files).
- [ ] Manifests: adapt the cabal repo's k8s set into the client-side infra location used for the room's other direct-applied services: own namespace, our image tag, drop litellm entirely, PVC + litestream file replica, `CABAL_MASTER_KEY` from a k8s secret delivered through the house secrets path. `replicas: 1`, `strategy: Recreate` (single-writer SQLite).
- [ ] Apply with the explicit dev-cluster context; wait healthy; capture the one-shot operator bootstrap code from boot logs; exchange it; `POST /admin/token/mint-service` a long-lived operator token for the room (sub kobo, operator scope, aud cabal-core).
- [ ] Store the token through the secrets path feeding the room's env as `CABAL_OPERATOR_TOKEN`; restart the room to pick it up.
- [ ] Evidence gate: `GET /.well-known/jwks.json` serves keys; relay smoke against the built-in echo persona (`POST /a2a/tasks {"target":"echo",...}`) round-trips; record image digest, namespace, token expiry, backup file present on the volume.

### Task 6: Rollout, backfill, locks (controller-driven)

- [ ] Roll the room to the image carrying Tasks 1-4 (build from local source, set image, explicit context). Org config still lacks the keys: verify chat still works on the legacy path (feature off).
- [ ] Write-then-restart: App-token PUT adds `cabal_url` + `agent_endpoint_template` to org.yaml; restart the room; IMMEDIATELY backfill both live agents via `POST /api/agents/{name}/directory/sync` (chat has no direct fallback once the keys flip, so registration must precede the relay check); verify both listed via the switchboard's admin agents endpoint with their metadata blocks intact; THEN verify chat relays (switchboard conversation record appears for a room chat).
- [ ] Gate: `python -c "import joserfc"` inside the live runtime image. If missing: rebuild the runtime image with the auth extra, push a new tag, and fold the FROM bump into the next step's re-pin commits.
- [ ] Relay timeout gate: one long-prompt chat through the relay to a real agent; confirm no switchboard-side timeout. If it times out, STOP and fix upstream before proceeding.
- [ ] Locks: one commit per agent repo via `update_agent_yaml_auth` (App-token script, composer helper); CI builds and rolls; verify relay chat still round-trips and a raw unsigned in-cluster POST to an agent's `/a2a/tasks` returns 401.
- [ ] Acceptance + ledger close: directory lists both agents; relay chat proven; unsigned rejected; destroy dry-run note behavior confirmed on a scratch agent if cheap, else deferred to the next scratch destroy; record image digests, org commit, re-pin commits, token coordinates (not the token), rollback pins.
