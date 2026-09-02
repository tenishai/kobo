# External MCP access for agents -- DISCOVERY (decision deferred)

Status: discovery notes, 2026-09-02. NOT an approved design. The operator
wants the connection setup to be as easy as possible and wants to use
limenal, but the authentication model needs more thinking time before any
approach is locked. Nothing here is on a build clock.

## The ask

Kobo-deployed agents need to reach external / third-party MCP servers
(Atlassian's hosted MCP, Datadog, ...). Two identity models were floated:
per-agent org identities (agent@example.com -- an agent managed by a
human, granted org tooling like a person) versus room-held privileged
credentials with kobo-side RBAC scoping each agent down to approved
resources (specific Jira boards, Confluence spaces, Datadog dashboards).

## Operator rulings so far (2026-09-02)

- Per-agent org identities are HYPOTHETICAL today -- no blessed path. v1
  thinking builds on room-held credentials; org identities stay a future
  lane.
- First target: Atlassian's HOSTED MCP server (mcp.atlassian.com) -- which
  puts the OAuth 2.1 client dance in scope rather than deferring it via a
  self-hosted Atlassian MCP image.
- Token custody: KOBO owns connections -- consent flow, public callback,
  refresh-token storage, serving fresh access tokens to the gateway.
- The access model, in the operator's own words: most services get ONE
  higher-level service account attached to kobo; kobo manages what each
  agent can actually get; the agent REQUESTS the slice it needs (e.g.
  Datadog APM + dashboards for one team's stuff), an operator approves,
  and the agent's MCP access is scoped to just those resources. This is
  the PR broker's two-wall model generalized: wall 1 = what the service
  account itself may touch (set in the SaaS by its admins), wall 2 =
  kobo's per-agent grant, enforced on every call.

## What exists today (verified 2026-09-02)

### limenal (github.com/tenishai/limenal, pre-alpha)

An MCP gateway: an agent makes ONE MCP connection; limenal fans out to
targets -- five built-in in-process handlers (scratchpad, files, web,
memory, notify) plus federated upstream MCP servers over streamable HTTP
(`target/http.py` HttpTarget, declared in a versioned targets TOML,
`limenal serve --targets`). stdio targets are schema-validated but
rejected at startup.

- Inbound auth (agents -> limenal): RS256 JWT, JWKS-verified; single-
  issuer mode (default issuer "cabal-core", scope must contain
  "dispatch", optional per-token allowed_handlers) or sovereign
  multi-issuer mode (MultiIssuerJWKSVerifier + SQLite PolicyStore holding
  per-(issuer, persona) grants {scopes, allowed_handlers}, admin REST at
  /admin/policy/*, token-embedded scopes deliberately ignored). Real,
  built, tested multi-tenant RBAC -- at TARGET/TOOL granularity, not
  resource granularity.
- Outbound auth (limenal -> upstream MCP): static bearer in the targets
  TOML, or per-call `ctx.credentials[key]` resolved via
  CabalCoreCredentialResolver (HTTPS GET /admin/secrets/{persona}/{handler}
  against Cabal Core, bearer-authed, 15-min TTL cache). NO OAuth 2.1
  client flow anywhere; their own draft (docs/proposals/
  mcp-2026-migration.md) scopes MCP-spec auth (RFC 9728/8707/9207 +
  Client ID Metadata Documents) as the long pole of future work.
- Also has: fail-closed secret guard on write-path tool args
  (limenal#50/#52), prompt-injection inspection of untrusted upstream
  catalogs (target/inspect.py), per-call identity forwarding to upstreams
  via UNSIGNED x-limenal-persona/x-limenal-issuer headers (upstream must
  trust limenal's network position -- a gap if an upstream should verify
  callers itself).
- Zero code awareness of daedalus/kobo/A2A. Cabal is its only integration
  partner (JWT issuer + secrets source). Dockerfile: port 9100, nonroot
  10002, techne sibling build context.

### daedalus (the runtime kobo agents run)

- Tool providers: none | mcp (STDIO ONLY -- local subprocess servers) |
  liminal (an MCP client hardwired to ONE limenal URL, bearer token from
  env, `LiminalSpec{url, token_env}`) | colleagues; plus `github` as a
  COMPOSABLE SIDECAR (present whenever broker_url is set) and builtin
  files. MultiToolProvider merges them.
- THE GAP: no remote/streamable-HTTP MCP client for arbitrary servers,
  and `liminal` is a PRIMARY provider while kobo agents already use
  `colleagues` as primary -- limenal support must become a sidecar block
  like GitHubSpec (small, known seam).
- Gating: tools.allow fnmatch ceiling (empty = deny all) + approvals
  gates (DenyAll default on pods) + the separate bundle/tools enforcement
  hooks. The capability-seam proposal (Ember, unbuilt) sketches
  card-driven auto-gating.
- Secrets pattern: per-agent k8s Secrets ({name}-cabal, {name}-slack)
  wired via gojira secrets_from into pod env; CABAL_COLLEAGUE_TOKEN (a
  cabal-minted colleague JWT, iss cabal-core, sub agent:<name>) already
  doubles as the PR broker's auth -- the SAME issuer lane limenal
  verifies today. Claim alignment needed (limenal wants cabal_persona +
  scope "dispatch"), not new identity plumbing.

### kobo

- The PR broker two-wall precedent: org App key at the room; per-agent
  short-lived tokens minted against approved RepoAccess rows; requests
  approved in batch by operators in the Access tab. ServiceAccess rows
  for MCP would be the same shape plus a resource-scope field.
- Kobo already runs an OAuth 2.1 SERVER (its own MCP surface) -- the
  team knows the flows; the CLIENT dance (consent, DCR, PKCE, refresh)
  is net-new. Public HTTPS + IAP-exempt-route precedent (/broker) exists
  for the callback.

## Approaches considered (NOT decided)

1. **limenal enforces, kobo governs** (leading candidate). Kobo:
   Connections registry (OAuth custody for hosted MCPs; plain API keys
   where that's the service's model, e.g. Datadog), ServiceAccess
   request/approve rows + Access-tab UI, grant sync into limenal's
   PolicyStore, and a credential endpoint implementing limenal's EXISTING
   resolver contract (point --cabal-url style config at kobo). limenal:
   verifies the fleet's existing cabal JWTs, HttpTargets per service,
   pulls per-persona credentials from kobo, enforces grants -- target/
   tool level via PolicyStore today, plus a NET-NEW resource guard on
   HttpTarget (per-service scoping profiles mapping tool arguments ->
   resource ids, e.g. Jira project keys, Confluence space keys) for the
   operator's board/space-level scoping. daedalus: limenal sidecar block.
2. **Kobo proxies MCP itself** (no limenal): extends the broker into a
   gateway. Rejected in discussion -- rebuilds limenal's machinery,
   makes the room a data-plane hot path.
3. **Per-agent direct connections**: daedalus gains a remote MCP client,
   each agent holds its own credential. Rejected in discussion -- most
   services cannot mint resource-scoped tokens (the reason wall 2
   exists), credential sprawl, no choke point.

A natural wave decomposition IF approach 1 is later confirmed:
A) plumbing (limenal in-cluster, daedalus sidecar, JWT acceptance,
PolicyStore sync, proven on built-in handlers, zero external creds);
B) Connections + Atlassian hosted MCP (OAuth client flow, credential
endpoint, tool-level grants);
C) resource-level scoping (per-service profiles + HttpTarget resource
guard + request-a-slice UX). Future lane: org identities per agent,
per-owner consents.

## Open questions holding the decision

- Best-practice validation: is a gateway-enforced, room-governed model
  the industry direction for agent-to-SaaS access, or does the MCP auth
  spec's trajectory (2026-07-28 stateless era, Client ID Metadata
  Documents) favor per-agent OAuth clients? limenal's own migration
  draft should be read against whatever the spec looks like when this
  work starts.
- Where resource-level enforcement really belongs: per-service scoping
  profiles in the gateway are honest but each service needs hand-written
  argument rules; some services may offer scoped tokens that make wall 1
  fine-grained enough on its own.
- Consent identity for OAuth connections: a dedicated service USER per
  service (audits as the service user; its SaaS permissions are wall 1)
  vs the connecting operator's own account. Leaning service-user, not
  ruled.
- Security review of token custody in kobo (refresh tokens in room
  Postgres vs k8s Secrets; blast radius; rotation).
- Whether cabal should mint a limenal-scoped token distinct from
  CABAL_COLLEAGUE_TOKEN, or the colleague JWT grows limenal claims.
