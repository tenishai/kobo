# Kobo MCP Server — design

**Date:** 2026-08-22
**Status:** approved-pending-review
**Arc:** the room's tools, reachable from any MCP client (claude.ai connectors, Claude Code) as the signed-in user

## Goal

A person who can use the room in a browser should be able to hand the same
powers to their AI tools: list the agents they can see, create one, teach
one, chat with one, wire Slack, and run the colleague-invite handshake —
from Claude, over MCP, **as themselves**. Not a service account with god
powers; not a second permission system. The operator ruling that anchors
everything: **per-user identity** — every tool call resolves ownership,
group visibility, and admin exactly the way the UI does, because it flows
through the same choke points with the same verified email.

The design follows a proven in-house MCP server pattern (an internal PaaS
whose MCP surface has been in production use with claude.ai connectors),
adapted onto kobo's existing access model. Where this document says "the
reference pattern," it means that implementation; its repo is deliberately
not named here.

## Identity and auth — kobo runs a small OAuth 2.1 server

MCP clients cannot ride an IAP browser session, so the MCP paths carry
their own auth. Kobo gains an OAuth 2.1 Authorization Server at
`/oauth/*`, with Google OIDC as the identity check in the middle — the
same identity source IAP uses, so the email that reaches the tools is the
same email the UI trusts.

**The flow** (public client + PKCE S256, pre-registered client id):

1. `GET /oauth/authorize` — requires PKCE S256; `client_id` must equal the
   configured `mcp.oauth_client_id`; `redirect_uri` allowlist is exactly
   `https://claude.ai/api/mcp/auth_callback` plus `localhost`/`127.0.0.1`
   (Claude Code). Mints a short-lived `state` JWT (5 min) carrying the
   redirect URI, code challenge, and the client's own state, then 302s to
   Google's OAuth with the org's Workspace domain as the `hd` hint.
2. `GET /oauth/google/callback` — verifies the state JWT, exchanges the
   Google code, verifies the ID token against Google's JWKS (cached), and
   **hard-checks the `hd` claim equals the configured Workspace domain**
   (the `hd` request parameter is a hint, never enforcement). Mints an
   `authorization_code` JWT (60 s, `sub` = verified email, carries the
   code challenge) and redirects back to the client.
3. `POST /oauth/token`, `grant_type=authorization_code` — verifies PKCE
   with a timing-safe compare, then issues an **access token** (15 min)
   and a **refresh token** (30 days, rotating).
4. `POST /oauth/token`, `grant_type=refresh_token` — atomic rotation
   (`SELECT ... FOR UPDATE`): the presented token's `jti` is marked
   rotated and a new `jti` is issued in the same **family**. Presenting an
   already-rotated `jti` is replay: the **entire family is revoked** and
   the caller must re-auth. This is what makes the connector session
   effectively long-lived (an active client refreshes forever; an idle one
   dies in 30 days; a stolen-and-replayed one dies immediately) while every
   token that crosses the wire on a tool call lives 15 minutes.

**Token mechanics:** all four token types (`state`, `authorization_code`,
`access_token`, `refresh_token`) are HS256 JWTs signed by one symmetric
key from the `KOBO_MCP_JWT_KEY` env var (base64, ≥32 bytes; absent = an
ephemeral random key with a loud warning, dev only). Only refresh tokens
are persisted (a `mcp_refresh_tokens` table: `jti`, `family_id`, `email`,
`issued_at`, `rotated_at`, `revoked_at`, `revoked_reason`); access tokens
are stateless with a 15-minute blast radius. Discovery endpoints for
client auto-configuration: `/.well-known/oauth-authorization-server`
(RFC 8414), `/.well-known/oauth-protected-resource` (RFC 9728), and a
`POST /oauth/register` (RFC 7591) that echoes the pre-registered public
client (`token_endpoint_auth_method: "none"`).

**Identity reaching tools:** the SDK's `TokenVerifier` seam verifies the
access token and attaches the email; a `_get_user_email(ctx)` helper is
the one place tools read it from. No contextvars, no header parsing in
tool bodies.

## Transport and mounting

- Official `mcp` Python SDK (`mcp.server.fastmcp.FastMCP`), **stateless
  streamable-HTTP** only, mounted as an ASGI sub-app at `/mcp` on the same
  FastAPI process.
- **The 308 gotcha (load-bearing):** `app.mount("/mcp", ...)` matches only
  `/mcp/`, but real clients POST to bare `/mcp`. An explicit
  `api_route("/mcp", methods=["GET","POST","DELETE"])` issues a 308 (which
  preserves method and body) to `/mcp/`.
- **Lifespan chaining (load-bearing):** the sub-app's session manager has
  its own lifespan that must be nested inside the main app's, or the SDK
  raises at request time.
- **Feature-flagged with deferred imports:** when `mcp.oauth_client_id` is
  unset, `mount_mcp()` is a no-op and the `mcp` package is never imported
  (pinned by a `sys.modules` diff test). A room without the flag is
  byte-identical to one that never heard of MCP.
- Mounted after all API routers and **before** the SPA static catch-all.

## Ingress — path-scoped IAP split, same host

`/mcp`, `/oauth/*`, and `/.well-known/*` must be reachable **without**
IAP (clients do their own OAuth there; the Workspace-domain check is the
org gate on that path). The reference pattern provisions a dedicated
load balancer; kobo's GKE-native version is lighter:

- a second k8s `Service` pointing at the same kobo pods, with its own
  `BackendConfig` that has **no IAP block**;
- path rules on the existing ingress routing exactly `/mcp`, `/mcp/*`,
  `/oauth`, `/oauth/*`, `/.well-known/*` to that service;
- everything else keeps the IAP'd backend. Same hostname.

The manifests live with the room's deploy config, not in this repo. The
API keeps its standing behavior: `/api/*` still expects the IAP assertion
and is untouched by this arc.

## Authz — a thin shim onto the existing choke points

No new permission logic. One adapter module (`kobo_api/mcp/authz.py`)
turns a verified email into the same decisions the UI gets:

- **Visibility:** `_visible_agents` / `_visible_agent_or_404`, driven by
  the caller's resolver groups (same `OktaGroupResolver`, same fail-closed
  cache). List tools return only what the caller could see in the room.
- **Mutation:** the owner-or-operator rule, evaluated for the email.
  Where the UI would 403 an authenticated non-owner on a resource they
  can see, the MCP shim answers the same way the visibility layer does
  for invisible resources: **404, never 403**, so tool errors cannot be
  used to enumerate agents (the reference pattern pins this normalization
  with a dedicated test; so will we).
- **Admin:** the email-operators-OR-admin-groups union, same as
  `_is_admin`.
- **Create:** the group-membership gate (operator exempt), unchanged.

The existing helpers take a `Request` and extract the IAP assertion. The
shim introduces one seam — the identity-bearing callable the helpers
already share internally accepts an email resolved either from IAP or
from the MCP access token — so both surfaces converge before any
decision is made. No logic is duplicated.

## Tool surface

Ten tools, thin shims over already-tested internals. Every tool is
wrapped in the audit decorator (below); every agent-scoped tool passes
through the authz shim first. `ToolAnnotations` carry honest
`readOnlyHint`/`destructiveHint` values.

| Tool | Params | Gate | Notes |
|---|---|---|---|
| `kobo_whoami` | — | none | email, groups, admin flag — the identity mirror |
| `kobo_list_agents` | — | visibility filter | what the caller sees in the room, with owner + status |
| `kobo_get_agent` | `name` | visibility (404) | card, purpose, status checklist |
| `kobo_feed` | `agent` | visibility (404) | the terminal's conversation feed, both directions |
| `kobo_create_agent` | `who_they_are`, `group`, `name?`, `skills?`, `tags?` | group membership | the one-click create; returns the checklist handle |
| `kobo_teach` | `agent`, `note` | visibility (404) | the fast memory lane; teach is deliberately open to anyone who can chat, same as the room |
| `kobo_chat` | `agent`, `message` | visibility (404) | relay through the switchboard, reply text back |
| `kobo_request_colleague` | `agent`, `colleague` | owner-or-operator of the requesting `agent` (404) | the cross-group invite; the receiver's owner accepts |
| `kobo_accept_invite` | `invite_id` | owner-or-operator of the receiving agent (404) | completes the handshake; pairs sync |
| `kobo_wire_slack` | `agent`, `bot_token`, `app_token`, `channel_id` | owner-or-operator (404) | both tokens redacted in audit, never logged |

**Deliberately absent:** `destroy` (operator-only in the room, and it
stays a human act — not one tool call away from any model), group
requests, org administration, and the service-auth `on_behalf_of` grant
(a later arc if headless callers ever need it; the reference pattern has
a proven shape for it when we do).

## Audit and rate limit

- `@audited("tool_name")` wraps every tool (inside `@mcp.tool`, so the
  SDK's schema introspection sees the real signature — decorator order is
  load-bearing). Per call it: enforces the rate limit, redacts sensitive
  kwargs (`note`, `message`, `tokens`, `secret`-ish keys →
  `***REDACTED***`), times the call, and fire-and-forgets a row into
  `mcp_tool_call_log` (`email`, `tool`, `status`, `duration_ms`,
  `redacted_args`, `error` first-500). Audit failures never fail the
  tool call.
- Rate limit: 60 calls/min per email, in-memory sliding window on the
  monotonic clock. Per-process is acceptable at the room's single-replica
  scale; documented, not hidden.

## Config and secrets

- `org.yaml` gains an `mcp` block: `oauth_client_id`,
  `oauth_client_secret_env` (name only), `workspace_domain`. These are
  org facts and live in the private config repo — the kobo repo never
  carries the domain or client id. The ORG_KEYS overlay allowlist grows
  the `mcp` key (the deployed loader rejects unknown org keys, so the
  image ships **before** the org.yaml edit — the admin-groups rollout
  order, repeated).
- Two cluster secrets via the usual OpenBao/ESO flow, created at the
  operator's terminal per house rule: the Google OAuth client secret and
  `KOBO_MCP_JWT_KEY`.
- Feature flag: no `mcp.oauth_client_id` → the whole surface is off and
  unimported.

## Testing

- Token unit tests: mint/verify each type, expiry, wrong-`typ` rejection,
  PKCE mismatch, rotation, **replay revokes the family** (with the row
  lock exercised), ephemeral-key warning path.
- OAuth route tests with a faked Google verifier: full happy path,
  `hd`-claim mismatch rejected, state tampering rejected, redirect-URI
  allowlist enforced.
- Mount tests: disabled-flag imports nothing (`sys.modules` diff), 308
  preserves POST, lifespan chain starts the session manager.
- Authz shim tests: visibility parity with the UI (same fixtures as the
  access-control suite), 403→404 normalization pinned, admin union,
  create's group gate.
- Tool tests over the mounted app with a stubbed switchboard: each tool's
  gate, audit row shape, redaction keys, rate-limit trip.

## Rollout

1. Ship the image flagged-off (safe no-op).
2. Operator terminal: Google OAuth client + the two secrets; ingress
   split applied in the deploy config.
3. org.yaml `mcp` block (after the image, per the overlay rule); restart.
4. Live proof: connect from claude.ai and Claude Code as the operator —
   `kobo_whoami` returns the operator identity; `kobo_list_agents`
   matches the UI; a teach lands in seconds and the note appears in the
   room; a non-owner mutation on a visible agent answers 404; a second
   user sees only their groups' agents.

## Out of scope

Service-account on-behalf-of auth, destroy, pagination of large rooms,
resource/prompt MCP surfaces (tools only for now), and any UI for token
management beyond revocation-by-table (a "connected clients" panel is a
registered fast-follow).
