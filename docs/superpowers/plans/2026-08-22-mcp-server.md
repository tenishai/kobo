# Kobo MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The room's tools reachable over MCP (claude.ai connectors, Claude Code) as the signed-in user, per `docs/superpowers/specs/2026-08-22-mcp-server-design.md`.

**Architecture:** Kobo runs its own OAuth 2.1 AS at `/oauth/*` (Google OIDC identity hop, Workspace-domain-checked, HS256 JWT tokens, rotating refresh families); the official `mcp` SDK's FastMCP mounts stateless streamable-HTTP at `/mcp`; ten thin tools call the EXISTING gates through a synthetic room request stamped with the verified email.

**Tech Stack:** FastAPI (existing app), `mcp` Python SDK, PyJWT (already a dep via IAP? verify — if not, add), SQLAlchemy models via the existing `Base`/`create_all` pattern.

## Global Constraints

- NO vendor names anywhere in tracked files (scan list: `.superpowers/sdd/vendor-names.txt`). The Workspace domain and OAuth client id live ONLY in org.yaml (private repo) — never in this repo, never in tests as literals (use `example.com` / `test-client` in fixtures).
- NO EM DASHES in user-facing strings (tool descriptions, error prose). Docstrings/comments follow house style.
- The feature flag is `config.mcp.oauth_client_id` being non-empty. Flag off ⇒ `mount_mcp()` is a no-op AND the `mcp` package is never imported (pinned by a `sys.modules` diff test).
- Error normalization: agent-scoped tools answer **404-shaped errors, never 403**, for both invisible and visible-but-not-owned resources.
- Identity seam: `kobo_api/mcp/authz.py:_room_request(email)` is THE bridge — a synthetic `starlette.requests.Request` whose `scope["app"]` is the real app and whose `state.iap_email` is the verified email. Tools never re-implement gate logic.
- All new tables via `kobo_api/models.py` + `create_all` (new tables need no manual ALTERs — see models.py's own comments).
- Suite commands: `cd api && venv/bin/python -m pytest -q` (full), `venv/bin/python -m ruff check kobo_api tests` (lint; NOTE pre-existing failures exist repo-wide from a ruff drift — the gate for this arc is "no NEW findings in touched files").
- Access token TTL 900 s; refresh 2 592 000 s (30 d); state 300 s; auth code 60 s. Rate limit 60/min/email. Audit error text truncated to 500 chars.
- Redirect URI allowlist: exactly `https://claude.ai/api/mcp/auth_callback`, plus any `http://localhost[:port]/...` or `http://127.0.0.1[:port]/...`.

---

### Task 1: Config — the `mcp` block

**Files:**
- Modify: `api/kobo_api/config.py` (add `MCPConfig` + field + ORG_KEYS entry)
- Test: `api/tests/test_config.py` (append)

**Interfaces:**
- Produces: `Config.mcp: MCPConfig` with `oauth_client_id: str = ""`, `oauth_client_secret_env: str = "KOBO_MCP_GOOGLE_CLIENT_SECRET"`, `workspace_domain: str = ""`, `jwt_key_env: str = "KOBO_MCP_JWT_KEY"`; helper `Config.mcp_enabled` property (`bool(self.mcp.oauth_client_id)`).
- `ORG_KEYS` grows `"mcp"` (find the existing tuple/list; append following its comment style).

- [ ] **Step 1: Failing tests** — `mcp` defaults off (`Config().mcp_enabled is False`); org overlay merges an `mcp` dict (mirror the existing admin_groups overlay test's shape); enabled when `oauth_client_id` set; `workspace_domain` empty + enabled ⇒ validator error (an AS with no domain check must refuse to boot, fail-closed).
- [ ] **Step 2: Run to verify fail.** `venv/bin/python -m pytest -q tests/test_config.py -k mcp` → FAIL (attribute missing).
- [ ] **Step 3: Implement.** `class MCPConfig(BaseModel)` with the four fields; `mcp: MCPConfig = Field(default_factory=MCPConfig)` on `Config`; `@model_validator` coupling: `if self.mcp.oauth_client_id and not self.mcp.workspace_domain: raise ValueError("mcp.oauth_client_id is set but mcp.workspace_domain is empty; the domain check is the org gate on the MCP path")`. Add `"mcp"` to ORG_KEYS.
- [ ] **Step 4: Pass + full config suite.** `pytest -q tests/test_config.py`.
- [ ] **Step 5: Commit** `feat(mcp): config block, org-overlayable, fail-closed on a missing domain`.

### Task 2: Token module — four JWT types, one key

**Files:**
- Create: `api/kobo_api/mcp/__init__.py` (empty), `api/kobo_api/mcp/jwt.py`
- Test: `api/tests/test_mcp_jwt.py`

**Interfaces (exact):**
```python
STATE_TTL_S = 300; CODE_TTL_S = 60; ACCESS_TTL_S = 900; REFRESH_TTL_S = 30*24*3600
def signing_key() -> bytes  # base64-decode env jwt_key_env; if unset: process-cached random 32B + log.warning once
def mint(typ: str, sub: str, aud: str, ttl_s: int, **extra) -> str  # HS256; claims typ/sub/aud/iat/exp/jti(uuid4 hex)+extra
def verify(token: str, expected_typ: str, aud: str) -> dict  # raises TokenError on any mismatch/expiry
class TokenError(Exception): ...
```
Use `jwt` (PyJWT). If PyJWT is not already in `api/pyproject.toml`/requirements (check — `kobo_api/iap.py` may use `google.auth` instead), add `PyJWT>=2.8` to the api dependencies.

- [ ] **Step 1: Failing tests.** Round-trip each typ; wrong `typ` rejected; wrong `aud` rejected; expired rejected (mint with ttl_s=-1); tampered payload rejected; `jti` unique across two mints; ephemeral-key path warns (caplog) and still round-trips within the process.
- [ ] **Step 2: RED run.** `pytest -q tests/test_mcp_jwt.py`.
- [ ] **Step 3: Implement** exactly the interface above (≈60 lines).
- [ ] **Step 4: GREEN + ruff on new files.**
- [ ] **Step 5: Commit** `feat(mcp): token mint/verify, four typed JWTs on one HS256 key`.

### Task 3: Refresh-token families — rotation and replay revocation

**Files:**
- Modify: `api/kobo_api/models.py` (add `MCPRefreshToken`)
- Create: `api/kobo_api/mcp/refresh_tokens.py`
- Test: `api/tests/test_mcp_refresh_tokens.py`

**Interfaces (exact):**
```python
class MCPRefreshToken(Base):  # models.py, table "mcp_refresh_tokens"
    jti: str (PK); family_id: str (indexed); email: str; issued_at: datetime
    rotated_at: datetime | None; revoked_at: datetime | None; revoked_reason: str | None

# refresh_tokens.py
def create_family(session, *, jti: str, email: str) -> str  # returns new family_id (uuid4 hex)
def consume(session, *, jti: str, email: str) -> str | None
    # Atomic rotation for sqlite: UPDATE mcp_refresh_tokens SET rotated_at=now
    # WHERE jti=:jti AND rotated_at IS NULL AND revoked_at IS NULL — check rowcount.
    # rowcount==1: insert new jti in same family, return it.
    # rowcount==0 and the row exists rotated ⇒ REPLAY: UPDATE the whole family
    # revoked_at=now, revoked_reason='replay_detected'; return None.
    # Row missing or family revoked ⇒ return None.
def revoke_family(session, *, family_id: str, reason: str) -> int
```
The rowcount-guarded UPDATE is the sqlite-safe equivalent of the reference pattern's `SELECT ... FOR UPDATE`; pin it with a comment.

- [ ] **Step 1: Failing tests.** create→consume returns a NEW jti in the same family; consuming the OLD jti afterwards revokes the ENTIRE family (both the old and the newly-issued jti rows get revoked_at + reason `replay_detected`) and returns None; consume of unknown jti → None; consume after family revocation → None; email mismatch → None (a stolen jti presented for another sub never rotates).
- [ ] **Step 2: RED.** — **Step 3: Implement.** — **Step 4: GREEN.**
- [ ] **Step 5: Commit** `feat(mcp): rotating refresh families, replay revokes the family`.

### Task 4: The OAuth 2.1 AS — routes

**Files:**
- Create: `api/kobo_api/mcp/oauth_as.py` (an `APIRouter`, included by mount.py in Task 9 — but unit-tested standalone here on a bare FastAPI app)
- Test: `api/tests/test_mcp_oauth.py`

**Interfaces:** router with `GET /oauth/authorize`, `GET /oauth/google/callback`, `POST /oauth/token`, `POST /oauth/register`, `GET /.well-known/oauth-authorization-server`, `GET /.well-known/oauth-protected-resource`. Google verification behind an injectable seam:
```python
class GoogleIdentity(Protocol):
    def auth_url(self, *, state_jwt: str, redirect_uri: str) -> str
    def exchange_and_verify(self, *, code: str, redirect_uri: str) -> dict  # returns verified ID-token claims incl. email, hd
```
Real impl (`_GoogleOIDC`) uses httpx + Google's token endpoint + JWKS verify; tests inject a fake. The `hd` claim is HARD-CHECKED against `config.mcp.workspace_domain` in the callback (the request-side `hd` param is only a hint). PKCE: store `code_challenge` in the state JWT, verify S256(code_verifier) with `hmac.compare_digest` at `/oauth/token`. Token responses per RFC 6749 JSON. Refresh grant calls Task 3's `consume` and re-mints both tokens on success; None ⇒ 400 `invalid_grant`.

- [ ] **Step 1: Failing tests (fake Google, real everything else).** Happy path end to end (authorize→callback→token→a verifiable access token whose sub is the email); wrong client_id 400; disallowed redirect_uri 400; missing PKCE 400; `hd` mismatch in CALLBACK claims → 403 even when the fake returns a valid email; tampered state → 400; PKCE verifier mismatch → 400; refresh happy path; refresh replay → 400 AND family revoked (assert via table); register echoes the public client; both well-known documents carry the right endpoints.
- [ ] **Step 2: RED.** — **Step 3: Implement.** — **Step 4: GREEN + ruff.**
- [ ] **Step 5: Commit** `feat(mcp): the OAuth AS — PKCE public client, domain-checked identity, rotating refresh`.

### Task 5: The authz shim — one synthetic request, zero new logic

**Files:**
- Create: `api/kobo_api/mcp/authz.py`
- Test: `api/tests/test_mcp_authz.py`

**Interfaces (exact):**
```python
def room_request(app, email: str) -> Request
    # starlette Request(scope={"type":"http","app":app,"headers":[],"method":"GET","path":"/","query_string":b""})
    # then req.state.iap_email = email; returns req.
class NotFound(Exception): ...  # tools translate to a plain "not found" ToolError
def visible_agents(session, app, email) -> list[Agent]   # main._visible_agents(session, room_request(...))[0]
def visible_agent_or_not_found(session, app, email, name) -> Agent  # 404/409/403-shaped JSONResponse ⇒ raise NotFound
def require_owner_or_operator(agent, app, email) -> None  # main._owner_or_operator_or_403 non-None ⇒ raise NotFound (403→404 normalization)
def is_admin(app, email) -> bool                           # main._is_admin(room_request(...))
```
Import the helpers from `kobo_api.main` lazily inside functions (main imports broadly; keep module import cheap and cycle-free).

- [ ] **Step 1: Failing tests.** Parity: seed two agents in different groups with the existing access-control fixtures (borrow from `tests/test_api_agents.py` / `test_api_grants.py` fixture style); a member email sees exactly its group's agents + shared; `visible_agent_or_not_found` raises NotFound for the other group's agent; `require_owner_or_operator` raises NotFound (NOT anything 403-shaped) for a VISIBLE agent owned by someone else — the normalization pin; operator email passes everything; admin-group member `is_admin` True (fake resolver, same fixture the AC suite uses).
- [ ] **Step 2: RED.** — **Step 3: Implement (≈70 lines).** — **Step 4: GREEN.**
- [ ] **Step 5: Commit** `feat(mcp): authz shim — the same gates, reached by email, 403 normalized to 404`.

### Task 6: Audit + rate limit

**Files:**
- Modify: `api/kobo_api/models.py` (add `MCPToolCallLog`: id PK autoinc, email, tool, status, duration_ms int, redacted_args JSON-as-TEXT, error nullable, created_at)
- Create: `api/kobo_api/mcp/rate_limit.py`, `api/kobo_api/mcp/audit.py`
- Test: `api/tests/test_mcp_audit.py`

**Interfaces:**
```python
# rate_limit.py
LIMIT = 60; WINDOW_S = 60.0
def check(email: str) -> bool   # sliding window on time.monotonic(); False = over limit
def reset(email: str | None = None) -> None
# audit.py
SENSITIVE_KEYS = {"note", "message", "bot_token", "app_token", "secret", "token", "value"}
def audited(tool_name: str):  # decorator; INNER of @mcp.tool; functools.wraps
    # over limit -> raise ToolError("Rate limit exceeded: 60 requests per minute.")
    # redact kwargs; time the call; write MCPToolCallLog via a session factory
    # handed in at registration (audit.configure(session_factory)); write failures
    # are swallowed with log.exception; error text truncated to 500 chars.
```

- [ ] **Step 1: Failing tests.** 60 pass/61st raises; window expiry via monkeypatched module `time`; redaction replaces exactly the sensitive keys and leaves others; a log row lands with status ok + duration; a tool that raises still logs status error with truncated message and re-raises; a broken session factory does NOT break the tool call.
- [ ] **Step 2: RED.** — **Step 3: Implement.** — **Step 4: GREEN.**
- [ ] **Step 5: Commit** `feat(mcp): per-email rate limit + redacted audit log, failures never break the call`.

### Task 7: The server + read tools

**Files:**
- Create: `api/kobo_api/mcp/server.py`, `api/kobo_api/mcp/tools/__init__.py`, `api/kobo_api/mcp/tools/read.py`
- Test: `api/tests/test_mcp_tools_read.py`
- Modify: `api/pyproject.toml` (add `mcp>=1.0` dependency)

**Interfaces:**
```python
# server.py
def build_mcp_server(app) -> FastMCP
    # FastMCP(name="kobo", stateless_http=True, streamable_http_path="/",
    #   token_verifier=_KoboTokenVerifier(app), auth=AuthSettings(...RS-metadata URLs...))
    # registers read + write tools; audit.configure(app's session factory)
class _KoboTokenVerifier:  # verifies via mcp/jwt.verify(..., "access_token", aud=client_id); returns AccessToken subclass with .user_email
def get_user_email(ctx) -> str  # ctx.request_context.request.user.access_token.user_email; the ONE reader
```
Read tools (all `ToolAnnotations(readOnlyHint=True)`, all `@audited`):
- `kobo_whoami()` → email, admin flag, resolved groups (fail-soft empty).
- `kobo_list_agents()` → for each visible agent: name, group, team, owner (created_by), status, purpose.
- `kobo_get_agent(name)` → detail incl. status checklist summary; NotFound → ToolError("not found").
- `kobo_feed(agent, limit=50)` → reuse the feed's flattening path scoped by the shim (call the same helpers `colleague_feed` uses — factor NOTHING; call `clients.cabal.list_conversations` twice scoped + `_flatten_conversation_entries` from main, mirroring the route body; cap at min(limit,200) entries, newest last).

- [ ] **Step 1: Failing tests.** Drive tools THROUGH the FastMCP tool-call path (`mcp_server._tool_manager.call_tool` or the SDK test pattern — implementer picks the SDK-blessed way) with a stubbed ctx carrying a user email; visibility parity (email A sees its agents, not B's); get on invisible → ToolError not leaking existence; whoami admin flag for admin-group member; feed returns flattened entries from FakeCabal (reuse fixtures from test_api_colleagues).
- [ ] **Step 2: RED.** — **Step 3: Implement.** — **Step 4: GREEN.**
- [ ] **Step 5: Commit** `feat(mcp): server + read tools — whoami, list, get, feed`.

### Task 8: Write tools

**Files:**
- Create: `api/kobo_api/mcp/tools/write.py`
- Test: `api/tests/test_mcp_tools_write.py`

Tools (all `@audited`, annotations honest):
- `kobo_create_agent(who_they_are, group, name=None, skills=None, tags=None)` — group-membership gate (mirror the create route's check via the shim's room_request against the same helper the route uses); drives the SAME create path the route does (call into the route's underlying composer/provision seam exactly as `POST /api/agents` does — the implementer reads `create_agent` in main.py and calls the same internals with the same defaults; NO new provisioning logic). Returns name + "watch the room" line.
- `kobo_teach(agent, note)` — visibility via shim; then the teach relay exactly as the teach route (relay-first, archive-after contract; reuse the route's internals).
- `kobo_chat(agent, message)` — visibility; relay; return reply text.
- `kobo_request_colleague(agent, colleague)` — owner-or-operator of `agent`; then the invite-create internals (dup → ToolError with the same copy the route returns).
- `kobo_accept_invite(invite_id)` — owner-or-operator of the RECEIVING agent; accept + pair sync exactly as the route.
- `kobo_wire_slack(agent, bot_token, app_token, channel_id)` — owner-or-operator; the slack wiring internals; tokens never in the return payload.

- [ ] **Step 1: Failing tests.** Per tool: the gate (non-owner on visible agent → not-found-shaped ToolError; non-member create → ToolError naming the group rule), the happy path against the existing fakes (FakeCabal, the create fixtures from test_api_agents), and one audit-redaction assertion for wire_slack (bot_token redacted in the logged row).
- [ ] **Step 2: RED.** — **Step 3: Implement.** — **Step 4: GREEN.**
- [ ] **Step 5: Commit** `feat(mcp): write tools — create, teach, chat, invites, slack`.

### Task 9: Mount — flag, 308, lifespan chain, wiring

**Files:**
- Create: `api/kobo_api/mcp/mount.py`
- Modify: `api/kobo_api/main.py` (call `mount_mcp(app)` after routers, BEFORE static_ui mounting; wire lifespan chaining)
- Test: `api/tests/test_mcp_mount.py`

**Interfaces:**
```python
def mount_mcp(app) -> bool
    # config.mcp_enabled False -> return False WITHOUT importing kobo_api.mcp.server/oauth_as/mcp SDK
    # else: include oauth router; build server; app.mount("/mcp", server.streamable_http_app());
    # add @app.api_route("/mcp", methods=["GET","POST","DELETE"]) -> RedirectResponse(url="/mcp/", status_code=308)
def lifespan_for(app, inner)   # wraps main's lifespan (or creates one) nesting the sub-app's router.lifespan_context
```
main.py: find how the app currently handles lifespan/startup (it may use on-startup events; adapt — if there is no lifespan today, create one that only nests the MCP context when mounted).

- [ ] **Step 1: Failing tests.** Disabled: `mount_mcp` False and `sys.modules` gains none of {`mcp`, `kobo_api.mcp.server`, `kobo_api.mcp.oauth_as`} (diff before/after); enabled (test config): `/mcp` POST → 308 with Location `/mcp/`; `/oauth/authorize` route exists; app boots with the chained lifespan (TestClient context manager enters cleanly).
- [ ] **Step 2: RED.** — **Step 3: Implement.** — **Step 4: GREEN + FULL suite** (`pytest -q`, expect ≥2276 + new).
- [ ] **Step 5: Commit** `feat(mcp): mount — flagged off by default, 308 for bare /mcp, chained lifespans`.

### Task 10: Docs + close-out

**Files:**
- Modify: `README.md` (one paragraph under "What the room does today": MCP access, per-user, what tools), `docs/superpowers/specs/2026-08-22-mcp-server-design.md` (status → implemented)
- Test: full suite + ruff on touched files + vendor scan

- [ ] **Step 1: Docs edits (vendor-clean).**
- [ ] **Step 2: Full API suite green; UI suite untouched (no UI in this arc).**
- [ ] **Step 3: Vendor scan with the REAL exit-flag gate over every touched file.**
- [ ] **Step 4: Commit** `docs: the room speaks MCP`.

## Explicitly NOT in this plan

Ingress manifests (deploy config repo, operator-applied), the Google OAuth client + two secrets (operator terminal), org.yaml `mcp` block (AFTER the image ships — the overlay rule), the live wire proof (controller runs it at rollout, mirroring the spec's Rollout section).
