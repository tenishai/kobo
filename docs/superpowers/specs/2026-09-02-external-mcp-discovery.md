# External MCP access for agents -- EVALUATED, pending peer review

Status: full evaluation complete 2026-09-02 (three parallel
investigations: current MCP auth spec + industry landscape, vendor auth
surfaces, adversarial architecture review against shipped code). A
recommended design exists (section "THE RECOMMENDATION" below) and the
operator has provisionally shaped it; it now goes to a coworker for
review before anything is built. The original discovery notes are
preserved below the recommendation for full context.

## THE RECOMMENDATION (2026-09-02): "liminal gates, kobo governs, the vendor scopes"

### The shape

- Agents make ONE MCP connection, to liminal (the gateway -- note: the
  repo was renamed upstream, tenishai/liminal, package `liminal` v0.2.0;
  local clones of "limenal" are stale). daedalus gains a `liminal`
  SIDECAR block (like GitHubSpec), composing beside the colleagues
  provider.
- Agents authenticate to liminal with a NEW cabal-minted,
  liminal-audience, short-lived token -- NOT the colleague JWT. (The
  colleague JWT is 1-year TTL, no revocation list, aud "cabal-core", no
  persona claim -- it would not verify, and widening its audience is the
  confused-deputy kobo's own pr_broker.py:26-29 docstring warns about.)
- kobo governs: `ServiceAccess` rows in the RepoAccess mold (agent
  requests a named slice -> operator approves in the Access tab), synced
  into liminal's policy; plus a credential endpoint implementing
  liminal's EXISTING resolver contract (GET /admin/secrets/{persona}/
  {handler}). Secrets live in OpenBao -- room Postgres holds grant rows
  and a bao path, NEVER the credential.
- THE VENDOR does resource scoping (the correction to both earlier
  leanings): no gateway argument-filtering "resource guard" (proven
  unenforceable -- free-text JQL/CQL reaches any project; vendor tool
  catalogs auto-grow), and no OAuth arc in v1 (Atlassian's hosted MCP
  officially supports service-account API-key bearer auth for headless
  clients -- an org-admin toggle).

### The credential middle ground (operator-shaped, 2026-09-02)

Provisioning many vendor service accounts is operationally unrealistic,
and vendors differ in RBAC models. Three dials per vendor, cheapest safe
default:

1. ONE service account per vendor, permissioned read-only where
   account permissions are the vendor's wall (Atlassian: project/space
   roles).
2. A READ-ONLY SCOPED KEY on that account wherever the vendor scopes
   credentials (Atlassian scoped API tokens: read_*/search_* only;
   Datadog scoped app keys / service-account tokens). This makes
   "agents start read-only" VENDOR-ENFORCED: a prompt-injected agent
   holding a read-scoped key cannot be talked into writing.
3. kobo/liminal grants per agent, uniform across vendors: target access
   + per-tool allowlists defaulting to the read toolset (Atlassian's
   catalog is already split into read/search vs write/delete groups).

Every Connection records its honest scoping model: `principal`
(account permissions are the wall), `scoped-key` (the key is the wall),
or `none` (vendor has one all-powerful key; the gateway tool allowlist
is the ONLY wall -- said plainly in the Access tab). Principal growth is
DEMAND-DRIVEN: the first WRITE need for a team is an explicit operator
decision -- a write-scoped key on the same account (fast) or a dedicated
write account trimmed to that team's resources (when blast radius
warrants) -- never an upfront provisioning project. Writes are exactly
where per-principal vendor-audit attribution starts mattering.

### CRITICAL pre-build finding (proven by executing shipped code)

liminal's SqlitePolicyStore FAILS OPEN in kobo's topology: the parent
admin row is a wildcard persona on the parent ISSUER, and resolve()'s
(issuer, '*') fallback does not exclude parent rows. In liminal's
designed federation the parent is a foreign issuer; for kobo, cabal-core
would be BOTH parent and the issuer of every agent token -- so any agent
with no grant row resolves to admin:* on ALL targets, can call the
/admin/policy/* API (mounted on the same port agents reach), and can
reverse its own narrowing (revoke() only refuses parent rows). Evidence:
policy.py:175-180, 190-202, 44-45, 239-250; admin.py:52-56;
app.py:246-247; cli.py:301-305. This lands in the "safe, zero external
credentials" plumbing wave -- it is a WAVE-A BLOCKER, not later polish.

### The hardening bill (nothing external goes live before ALL of these)

liminal: L1 exclude is_parent from wildcard fallback + distinct admin
issuer + boot-guard refusing parent==data-plane issuer; L2 admin API off
the agent-reachable listener (NetworkPolicy to kobo only); L3
AsyncMCPClient.initialize() must send Authorization, and a failed
catalog fetch must fail LOUD (today: silent zero-tool target -- the
outbound client cannot authenticate at all, so wave B is blocked on this
regardless); L4 kill the shared cookie jar (cross-persona upstream
session bleed); L5 --audience for single-issuer serve (liminal-specific
aud); L6 extend the scope map to HTTP-target tools (per-tool grants are
currently inert for the only tools that matter); L7 frame HttpTarget
tool RESULTS as untrusted data (catalog inspection covers descriptions
only); L8 empty resolved credential = hard dispatch failure (today:
fail-soft unauthenticated upstream call) + pod egress allowlisted.

TRACKED (2026-09-03, operator ruling: issues on the repo, not PRs --
work through and resolve there): tenishai/liminal #73 (L1), #74 (L2),
#75 (L3), #76 (L4), #77 (L5), #78 (L6), #79 (L7), #80 (L8). This also
answers peer-review question 5: the liminal-side bill lives upstream as
issues; kobo-side items (K1-K4) and cabal C1 stay with their own repos
when their waves start.

kobo: K1 OpenBao custody (grant rows + bao path in Postgres only); K2
credential endpoint on the resolver contract serving the approved
grant's credential, denying otherwise; K3 extend the resolver contract
so liminal forwards the CALLER's inbound JWT and kobo verifies liminal
serves the persona it names (without this, a liminal RCE harvests every
persona's credentials); K4 ServiceAccess model + Access-tab approvals.

cabal: C1 mint a liminal-audience short-TTL token distinct from
CABAL_COLLEAGUE_TOKEN (mint-service already supports persona/aud/scope).

daedalus: sidecar-ize the liminal block (not a primary provider);
re-read the token per call and drop the "no-auth" fallback; update the
stale local gateway clone / pin liminal v0.2.0 (the import name is
already correct against upstream main).

Ops note: Atlassian MCP rate limits are undocumented and tight (~20
parallel calls -> 429s, Retry-After only); liminal needs client-side
throttling/fair queueing since grants share principals.

### Deliberately NOT in v1

The OAuth 2.1 client arc (consent UI, public callback, DCR/PKCE,
refresh custody) -- revisit only for a vendor with no headless path,
and note EMA/ID-JAG (Okta ships it; we are an Okta shop) may remove
custody entirely for supporting vendors later. The resource guard /
argument filtering -- ever, ideally. identity="forward" unsigned
headers. Datadog (second vendor; same shape, after Atlassian proves).
Short catalog auto-refresh (pin it long; catalog changes are a reviewed
event). Grants broader than a read toolset by default. Sessions/
subscriptions/elicitation/sampling (migration debt).

### Waves (if approved)

A) liminal hardening (L1-L8) + deploy in-cluster + cabal liminal-token
   (C1) + daedalus sidecar + kobo ServiceAccess/policy sync -- proven
   end-to-end on liminal's BUILT-IN handlers, zero external credentials.
B) Atlassian: org-admin prerequisites (enable the MCP API-token auth
   toggle; one read-only service account + read-scoped API token; check
   service-account headroom -- 5 free/org, 250 with Guard Standard),
   OpenBao custody + K2/K3 endpoint, the mcp.atlassian.com HttpTarget,
   throttling, Access-tab grants live.
C) Demand-driven growth: write keys/principals per explicit ruling;
   Datadog; EMA/ID-JAG watch; per-agent org identities as the long lane.

### Questions for the peer reviewer

1. Any hole in "vendor scopes, gateway gates" -- especially the claim
   that gateway argument-filtering is not worth building?
2. The liminal-audience token design (C1) vs alternatives (separate
   issuer? kobo-signed?).
3. K3's resolver-contract extension -- is caller-JWT forwarding the
   right proof, or should liminal exchange tokens (RFC 8693) properly
   from day one?
4. OpenBao custody shape (one mount? per-vendor paths? rotation story).
5. Anything in the hardening bill that should be upstreamed to
   tenishai/liminal as PRs vs carried as kobo-side patches.

## The dogfood case, made concrete: ledger's four lanes (2026-09-03)

The audit agent this arc was always going to serve now EXISTS: `ledger`
(persona ledger-keeper, group infra-engineering, repo example-org/agent-ledger),
built 2026-09-03 entirely through the kobo MCP connector -- two marketplace
skills (gcp-access-audit, iam-binding-evidence), an enforcement tool
(audit-provenance: pre_task computes the evidence-chain decision points in
code and injects them as notices; post_draft blocks gapless bundles and
annotates identifiers that appeared in no input), and an approved repo
grant. Its four external lanes are the concrete requirements this arc's
decision unblocks. Every fact below is read from the shipped
infra-audit-mcp client code, not remembered.

**Lane 1 -- GitHub (READY, waiting only on the runtime roll).** The
room's PR-broker grant for example-org/global-iam-permissions is approved.
The github read quartet (read_file / list_prs / get_pr / list_releases)
is merged on upstream daedalus main (PR #23) but absent from the fleet
runtime c1992f2-t0161; the next canonical runtime pin lights this lane
with no further access work. No liminal dependency.

**Lane 2 -- AWS CloudTrail Lake, acme-it (access design settled by
precedent; tool surface is the open question).** The old server already
runs the exact pattern proposed: the workload's GCP service account gets
an OIDC token (audience=sts.amazonaws.com) and calls
sts:AssumeRoleWithWebIdentity against a role in acme-it (account
052257306451), then queries the CloudTrail Lake event data store
`acme-okta-logs` in us-east-2 via cloudtrail:StartQuery /
GetQueryResults. NOTE: this is CloudTrail Lake SQL, NOT Athena -- the
scoped policy needs cloudtrail:StartQuery, cloudtrail:GetQueryResults,
cloudtrail:DescribeQuery on the event-data-store ARN, not
athena:*/glue/s3 (unless we deliberately move to Athena federation, a
separate decision). Ledger's principal exists:
`agent-ledger@example-project.iam.gserviceaccount.com` (workload
identity, KSA agent-ledger, ns ledger). Execution is one of: add this
GSA to the existing audit role's trust policy, or mint a twin role
`ledger-audit-readonly` scoped identically. Both are one small PR once a
tool surface exists to consume it.

**Lane 3 -- Slack #ask-access-request (scopes known; reuse available).**
The old server reads the channel with a bot token (GSM:
audit-mcp-slack-bot-token) holding channels:history, deliberately using
conversations.history + conversations.replies rather than
search.messages (bot-token search is unreliable and the thread ROOT
carries the emails -- documented in the client). Ledger can reuse that
bot or get a twin; the missing piece is again the tool surface, not the
access.

**Lane 4 -- Jira AREQ board (the arc's scoped-key model, live
precedent).** The old server authenticates as the shared service account
automation@example.com with a read-only PAT from GSM (basic auth, REST
/rest/api/3/issue). Ledger's need: read AREQ tickets and their comments
to verify a VP-or-above approval comment exists. That is exactly the
"one read service account per vendor + vendor-scoped key" middle ground
this doc recommends -- browse permission on AREQ only. Caveat for the
reviewer: verifying the approver is "VP or above" is a DIRECTORY
question, not a Jira question; Jira proves who commented, the org chart
proves rank. The bundle should carry the comment + author and name the
rank check as its own evidence leg.

**Cloud lanes are their own category (operator ask, 2026-09-03).** The
operator wants scoped role-based access to cloud services (CloudTrail
Lake SQL today; any AWS/Azure/GCP service tomorrow) with a SIMPLIFIED
way of granting it -- fold this into the arc as a first-class lane type.
The key observation: cloud providers are the one vendor class where the
"vendor scopes" half of the recommendation is already solved better than
any gateway could do it -- IAM roles, conditions, and resource ARNs are
the native scoping primitives -- and where NO stored credential is
needed at all: every kobo agent already has its own GCP service account
(workload identity, created at birth), GCP federates to AWS via
AssumeRoleWithWebIdentity (the shipped audit-mcp precedent) and to Azure
via federated credentials. So for cloud lanes, liminal's credential
custody problem (K1/K4, resolver, OpenBao) largely DISSOLVES: the
gateway would forward identity, not inject secrets. Proposed simplified
grant model: declare agent cloud access in example-org/global-iam-permissions
the same way team access is declared -- the agent's GSA as a principal
in the existing YAML+terraform machinery (per-service scoped role, PR
lane, drift detection, and the access-ledger diff chain all come free,
and ledger itself can audit agent grants with the same methodology it
uses for humans).

RULED (operator, 2026-09-03): cloud access is a SEPARATE PATH from MCP
access -- an integration path, not a gateway path. An SRE creates the
access role per cloud: AWS = a scoped role in the specific account,
limited to the specific services needed, whose trust policy trusts the
agent's GCP service account (the shipped audit precedent); GCP = no new
identity at all, the agent's existing birth GSA gets cross-project role
grants; Azure = a scoped role/app with a federated credential trusting
the same GSA, mirroring the AWS shape. The MCP/liminal path remains for
SaaS vendors that lack workload federation (Atlassian, Slack, Datadog).
The former sub-questions, narrowed by the ruling: (a) cloud calls carry
NO gateway hop by default -- the audit trail for cloud lanes is the
provider's own (CloudTrail / GCP audit logs / Azure activity log),
stronger than a gateway log, and the central kill-switch is the IAM
grant itself (revoke the role or binding); (b) the tool surface for
cloud calls is the agent runtime's own (a provider like the github
sidecar, or a direct SDK tool), sized per integration; (c) a
`CloudAccess` row type in kobo (request -> operator approve ->
generated PR against global-iam-permissions) stays the target shape for
"simplified".

**The tool-surface decision (what actually blocks lanes 2-4).** Three
options, unchanged from this doc's body: (a) liminal gateway (the
recommendation; ledger is the designated first consumer), (b) native
daedalus sidecar providers per service (the github.py pattern --
fastest per-lane, but every lane is bespoke runtime code and the credential
custody/hardening bill still applies), (c) kobo-side broker per service
(the pr_broker two-wall pattern generalized). Recommendation stands:
(a), with lane 2 (AWS) as the first target after Atlassian OR instead of
it -- the AWS lane has the strongest precedent (the old role/trust
pattern already exists) and the clearest scoping story. Until the arc is
decided, ledger runs these lanes paste-in, and its teach notes say so
honestly.

---

# ORIGINAL DISCOVERY NOTES (2026-09-02, pre-evaluation -- kept for context;
# several claims below were CORRECTED by the evaluation above: the OAuth
# ruling, the one-account model, "real fail-closed multi-tenant RBAC",
# tool-granularity claims, and the JWT-verification claim)

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

## 2026-09-04 addendum: the AREQ tactical bridge shipped (and what it is not)

Ruling (operator, 2026-09-04): ledger reads AREQ now, on the
jiraautomation read PAT, while the liminal hardening PRs work through
review -- so the dogfood case keeps moving. The shape is deliberately the
PR broker's custody model applied to reads, NOT an env-secret in the
agent's namespace:

- kobo holds the one read-only tracker credential (mounted file, PR-App
  key convention) and exposes two GET routes on the IAP-exempt /broker
  root: `/broker/agents/{name}/jira/issues/{key}` and `.../jira/search`.
  Same walls as the github-token route (cabal JWT, anti-enumeration 404),
  plus a deployment-config agent allowlist (`jira_read_agents`) standing
  in for the ServiceAccess rows this arc will eventually grow.
- The pinning lives server-side and is not a parameter: `jira_projects`
  gates issue keys before any tracker call, and search accepts bare text
  only -- the JQL is assembled inside kobo from bounded inputs. No JQL
  crosses the boundary (the resource-guard-theater finding, honored).
- daedalus grew the client half (`tools.jira` sidecar) and, in the same
  wave, `tools.slack_read` -- the channel-allowlisted thread reader that
  closes the #ask-access-request lane with zero new credentials (the
  pod's own bot token + channel membership as Slack's second wall).

None of this displaces the liminal path: when the gateway lands, the
Atlassian hosted-MCP connection replaces these two routes, and the
custody plumbing (mounted credential, allowlist gate) carries forward as
K1/K2 groundwork. The bridge is a lane, not a fork.
