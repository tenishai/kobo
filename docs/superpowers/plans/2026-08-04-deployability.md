# Deployability & Identity (Phase B-lite, kobo-side) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Kobo becomes deployable off the laptop: one container serves the API and the built UI, requests are verified against the identity proxy's signed assertion when enabled, and GitHub operations can authenticate as an installed GitHub App instead of the operator's personal CLI login.

**Architecture:** Three env-gated seams, all no-ops in dev: (1) the API mounts the built UI as static files when `ui_dist` is set (same-origin — no CORS in production); (2) an ASGI middleware verifies the `x-goog-iap-jwt-assertion` header (ES256 against Google's published JWKS, issuer + audience pinned) when `iap_audience` is set — fail closed, health path exempt because LB health checks bypass the proxy; (3) a GitHub App installation-token provider (App JWT → installation token, cached to expiry) feeds `GH_TOKEN` into every `gh`/`git` subprocess when app credentials are configured, with the container baking `credential.helper = !gh auth git-credential`. A root Dockerfile builds UI + API into one image. The operator-side deployment (cluster, LB, IAP wiring, DNS) is deliberately out of this plan — it lives in operator notes, not this repo.

**Tech Stack:** adds `PyJWT[crypto]` to the API (ES256 verify + RS256 App JWT). Everything else unchanged (FastAPI/pytest; Svelte 5/vitest).

## Global Constraints

- Never any client organization's name in any tracked file — the Dockerfile, configs, and tests use neutral placeholders only (plain `grep -i` the client-name list before staging).
- Fail closed: with `iap_audience` set, any request without a VALID assertion is 401 — no header, bad signature, wrong issuer, wrong audience, expired all reject. With it empty, behavior is byte-identical to today.
- The health path (`/api/healthz`) is exempt from assertion checks (LB health checks bypass the identity proxy by design) and must never leak more than today's `{"ok": true}`.
- Static serving must never shadow `/api/*`; unknown non-API paths fall back to `index.html` (SPA routing); `ui_dist` unset = no static serving (dev unchanged).
- Token provider: installation tokens are never logged, never in exception messages (house CommandRunner redaction rules apply), refreshed before expiry with a safety margin.
- TDD; `make -C api check` and `make -C ui check` fully green before every commit; git add named files only.

---

### Task 1: API serves the built UI (`ui_dist`)

**Files:** `api/kobo_api/static_ui.py` (new), `api/kobo_api/main.py` (mount hook in create_app), `api/kobo_api/config.py` (`ui_dist: Path | None = None`, _expand_user validator list), `api/config.example.yaml`, `api/tests/test_static_ui.py` (new).

**Interfaces:**
```python
# static_ui.py
def mount_ui(app: FastAPI, dist_dir: Path) -> None
    # Mounts dist assets and registers a catch-all GET that returns index.html for any
    # path NOT starting with /api (SPA fallback). Registered AFTER all API routes so
    # /api/* always wins. Missing index.html at startup -> RuntimeError (fail loud at boot,
    # not per-request). Assets served with normal static headers; no directory listings.
# create_app: if config.ui_dist: mount_ui(app, config.ui_dist)  — last thing before return.
```
- Tests (route-level, tmp_path dist fixture with index.html + `assets/app.js`): `/` and `/agents/whatever` return index.html bytes; `/assets/app.js` served; every existing `/api/*` route unaffected (spot-check healthz + one agents route); `ui_dist` unset → `/` 404 exactly as today; missing index.html → create_app raises.

- [ ] TDD; commit: `feat(api): serve the built UI when ui_dist is set — one origin, no CORS`

### Task 2: identity-proxy assertion middleware (`iap_audience`)

**Files:** `api/kobo_api/iap.py` (new), `api/kobo_api/main.py` (middleware registration), `api/kobo_api/config.py` (`iap_audience: str = ""`), `api/pyproject.toml` (`PyJWT[crypto]`), `api/config.example.yaml`, `api/tests/test_iap.py` (new).

**Interfaces:**
```python
# iap.py
IAP_ISSUER = "https://cloud.google.com/iap"
IAP_JWKS_URL = "https://www.gstatic.com/iap/verify/public_key-jwk"
ASSERTION_HEADER = "x-goog-iap-jwt-assertion"
EXEMPT_PATHS = frozenset({"/api/healthz"})

class IapVerifier:
    def __init__(self, audience: str, jwks_fetcher: Callable[[], dict] | None = None): ...
        # default fetcher: httpx GET IAP_JWKS_URL (10s timeout); keys cached 12h;
        # on a kid miss, refetch ONCE (key rotation) before rejecting.
    def verify(self, token: str) -> dict: ...
        # PyJWT: ES256 only, audience=self.audience, issuer=IAP_ISSUER, require exp/iat.
        # Any failure -> IapError (message NEVER includes the token).

# ASGI middleware (pure function or BaseHTTPMiddleware — match house style):
# active only when config.iap_audience is non-empty; skips EXEMPT_PATHS and static/SPA
# paths? NO — it covers EVERYTHING except EXEMPT_PATHS (the UI is behind the proxy too).
# Missing/invalid assertion -> 401 {"error": "you need to sign in to use this room"}.
# On success, stash verified email claim on request.state.iap_email (future team mapping).
```
- Tests: disabled (empty audience) → all routes work with no header (existing suites already prove this — one explicit test); enabled → 401 on no header / garbage token / wrong audience / wrong issuer / expired (generate a real ES256 keypair in the test, serve its JWK via an injected fetcher — sign real JWTs; no mocking of the verify path); valid token → 200 and `request.state.iap_email` set (assert via a tiny probe route registered in the test app); healthz exempt with no header; kid-rotation refetch (fetcher called twice when kid unknown first pass).

- [ ] TDD; commit: `feat(api): verify the identity proxy's signed assertion when enabled — fail closed`

### Task 3: GitHub App installation tokens behind the client seam

**Files:** `api/kobo_api/github_app.py` (new), `api/kobo_api/clients.py` (CommandRunner env overlay + GhCLI wiring), `api/kobo_api/config.py` (`github_app_id: str = ""`, `github_app_installation_id: str = ""`, `github_app_private_key_path: Path | None = None`), `api/config.example.yaml`, `api/tests/test_github_app.py` (new), `api/tests/test_clients.py` (env-overlay coverage).

**Interfaces:**
```python
# github_app.py
class InstallationTokenProvider:
    def __init__(self, app_id: str, installation_id: str, private_key_path: Path,
                 http_client: httpx.Client | None = None, now: Callable[[], float] | None = None): ...
    def token(self) -> str: ...
        # Cached until 5 minutes before expiry. Refresh: RS256 App JWT (iss=app_id,
        # iat/exp ±/+9min) -> POST https://api.github.com/app/installations/{id}/access_tokens
        # (Accept: application/vnd.github+json, Authorization: Bearer <app jwt>)
        # -> {"token", "expires_at" iso8601}. Non-201 -> ClientError (body NEVER echoed
        # into the message — status code only; full detail to log.debug).

# clients.py
# CommandRunner.run/run_bytes gain keyword `env: dict[str, str] | None = None` —
# subprocess receives {**os.environ, **env} when set (unchanged otherwise).
# GhCLI.__init__ gains `token_provider: Callable[[], str] | None = None`; every gh/git
# invocation passes env={"GH_TOKEN": provider()} when a provider is set. Clients.real():
# construct InstallationTokenProvider iff all three config values are set, else None
# (dev = operator's gh login, unchanged).
```
- Tests: JWT claims/headers exact via MockTransport capture (Authorization Bearer decodes as RS256 with iss=app_id — verify against the test keypair's public half); token cached (one HTTP call for two token() calls); refresh after simulated expiry via injected now(); non-201 → ClientError without body text; GhCLI argv tests extended: env carries GH_TOKEN when provider set, absent when not (CommandRunner records/receives env — extend the fake runner used by argv tests); token value never appears in ClientError messages from failed commands (redaction test with a failing command).

- [ ] TDD; commit: `feat(api): GitHub App installation tokens — the room stops borrowing the operator's login`

### Task 4: container image

**Files:** `Dockerfile` (new, repo root), `.dockerignore` (new), `api/config.example.yaml` (document container-relevant keys together), `README.md` (one "Container" paragraph).

**Behavior (no unit tests — the operator's build is the gate; keep it boring):**
- Stage 1 `node:22-slim`: `npm ci` + `npm run build` in ui/.
- Stage 2 `python:3.12-slim`: install git, curl, ca-certificates; install `gh` (official apt repo), `kubectl` (stable channel binary), and the cloud SDK CLI (apt package — needed for the token seam's CLI fallback and group-membership operations); `pip install ./api`; copy ui/dist to `/srv/ui`; bake `git config --system credential.helper '!gh auth git-credential'`; non-root user; `ENV PORT=8080`; ENTRYPOINT `uvicorn --factory kobo_api.main:build_default_app --host 0.0.0.0 --port 8080`.
- Config arrives via a mounted file + `KOBO_CONFIG` env pointing at it (`ui_dist: /srv/ui` set there, not in the image).
- `.dockerignore`: venv, node_modules, .git, .superpowers, *.db, *.local.yaml (defense in depth for local secrets).

- [ ] Build sanity is the operator's step (cloud build); commit: `feat: container image — UI + API in one deployable`

### Task 5: operator milestone (controller-run, environment-specific — details live in operator notes, not here)

- [ ] Build + push the image with the operator's cloud builder; deploy behind the operator's identity-proxied load balancer per their environment's pattern; DNS + certificate + proxy grants per operator runbook; smoke: signed-in browser reaches the room, unauthenticated request rejected, an end-to-end agent action works from the deployed room.

---

## Self-review notes (applied)

- Fail-closed ordering: the middleware registers before static mounting so SPA paths are covered; EXEMPT_PATHS is an allowlist of exactly one path.
- Token hygiene: both new token surfaces (assertion, installation token) pin "never in error messages" with tests; CommandRunner's env overlay deliberately does NOT log env.
- Dev parity: all three seams default off; the existing 667-test suite runs unchanged proving the no-op paths.
- Deliberate scope cuts: no team mapping from the verified email yet (stash only); no JWKS disk cache; no multi-audience; DB stays sqlite-on-volume via existing `db_url` (no code).
- Type consistency: config keys ↔ Clients.real wiring ↔ example yaml checked name-for-name; EXEMPT path literal matches the existing healthz route string.
