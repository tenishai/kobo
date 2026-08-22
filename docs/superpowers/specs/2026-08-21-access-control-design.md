# Access Control: who you are, whose agents you see, and who talks to whom

**Date:** 2026-08-21
**Status:** Draft for operator review
**Depends on:** colleagues switchboard + talk (2026-08-18/19), persona metadata (2026-08-18)

## Problem

The room has one implicit user. Every signed-in person sees every agent,
can edit anything, and every agent can talk to every other agent because
the operator allowed open discovery "for the beginning." Before more teams
onboard, the room needs: visible identity, agent ownership, group
namespaces, and a consent gate on cross-group agent-to-agent talk.

## What the benchmark actually does (verified in its source)

The internal PaaS the operator named as the UX model was read end to end.
Its real anatomy, which this spec copies where it is right and departs
from deliberately where it is not:

- Identity is the IAP-forwarded email, trusted at the network layer, shown
  bottom-left. (kobo already goes further: it verifies the signed IAP
  assertion, ES256 against Google's JWKS, fail-closed. We keep our
  stronger posture.)
- "The infra group sees everything" is NOT a directory group there: it is
  a static comma-separated admin email list in config. kobo already has
  exactly this shape: the `operators` list in org config.
- Ownership is a plain column set at create time, string-compared against
  the IAP email; owner-or-admin gates the destructive routes; an Owner
  chip renders in the UI.
- Its access panel's modes are enforced against the real system (it pushes
  IAM bindings), not just filtered in the UI.
- Its only directory integration is people-search via an Okta API token
  (SSWS scheme) shared with the org's Okta viewer service; fail-soft,
  TTL-cached.

The group piece the operator wants is genuinely new. The good news,
verified in the Okta viewer's source: the SAME shared token already reads
groups (`/api/v1/groups`, `/api/v1/users/{id}/groups`) in production
today. Group resolution needs no new Okta integration, only that existing
secret delivered into kobo's environment.

## Design

### 1. Identity, visible

- kobo keeps its verified-assertion middleware unchanged.
- New tiny route (`GET /api/whoami`) returns the verified email and the
  caller's resolved groups (section 3); the app shell shows the email
  bottom-left, the benchmark's own placement. No email means the room is
  running without IAP (local dev): show nothing.

### 2. Ownership

- `agents.created_by` (nullable text, the verified email at create time).
  Backfill: existing agents (network, ward, relay, gemini-one) get the
  operator's email.
- The agent card and detail Overview show the owner.
- Owner-or-operator gates on the mutating surfaces: destroy, Slack wiring,
  tooling upload/import, skills edit, metadata edit, intake delete.
  Chat and teach stay open to any signed-in group member (see 4) - talking
  to a colleague is not administration. `operators` (org.yaml, existing)
  remains the admin allowlist, exactly the benchmark's shape.

### 3. Groups, resolved from Okta

- org.yaml gains `groups:` - the list of engineering group names the room
  offers (display name + Okta group name). Config names the UNIVERSE of
  groups; Okta answers membership.
- New `OktaClient` in kobo (httpx, SSWS auth header), one call:
  user email -> group names, filtered to the configured universe.
  TTL cache 15 minutes, per-email. Fail-soft DOWNWARD only: if Okta is
  unreachable, a user keeps (a) agents they own and (b) operator rights if
  listed; group-derived visibility simply vanishes until Okta returns
  (fail closed on grants, never open), with an honest banner.
- Secret: the existing shared GSM token (`OKTA_API_TOKEN`), delivered into
  kobo's env by the operator (their terminal; same lane as every secret).
  Absent token = groups feature off, room behaves as today plus ownership.
- IDP-agnostic seam: `GroupResolver` protocol with the Okta implementation
  behind it; a future IdP is one new client class, nothing else moves.

### 4. Visibility and the create flow

- Create flow gains a group dropdown (from `groups:`), stored as
  `agents.group_name`. This is the ACCESS namespace; the existing `team`
  field (derived from the skills catalog owner axis) remains the
  capability/marketplace axis - two different questions, deliberately two
  fields.
- List/detail visibility: operators see all; group members see their
  groups' agents; everyone sees agents they created. Enforced in the
  query/route layer (the UI only reflects it).
- Existing agents backfill into the operator's own group at migration.

### 5. Agent-to-agent policy: same group open, cross-group by invite

Ruling encoded verbatim: agents within one group talk freely by default;
an agent from another group must request access, and the RECEIVING agent's
owner accepts before the handshake exists.

- New kobo tables: `a2a_invites` (from_agent, to_agent, requested_by,
  status pending/accepted/declined, decided_by, timestamps) and the
  derived allow-pairs set (same-group pairs are implicit; accepted invites
  add explicit pairs).
- ENFORCEMENT lives at the switchboard, not in agents: cabal grows a
  per-target dispatch allow-list checked at colleague re-mint time
  (upstream PR; decision-record it as the D-series entry). kobo, as the
  policy owner, syncs the pair set to cabal whenever it changes (agent
  created, invite accepted/revoked, agent destroyed). Directory reads stay
  global (agents can SEE the family; talking across groups is what gates).
  Cabal knows nothing about groups or Okta - it stores flat caller->target
  pairs kobo computes. Until the cabal PR lands and deploys, the room
  UI/API enforce invites and the switchboard remains open at the wire -
  the spec ships honest about that window (same "for the beginning"
  posture as today, now with a visible plan to close it).

### 6. The Access tab

Per agent, visible to its owner and operators:

- User access: the resolved group (read-only fact: "everyone in
  <group>"), plus per-user additions (emails, people-search backed by the
  same Okta token's user search, the benchmark's autocomplete pattern) for
  cross-group humans who need to see/chat with this agent.
- Agent access: incoming invites (accept/decline), outgoing requests
  (state), current allow-pairs, revoke.
- Requesting: from another agent's (visible but not-yours) detail page, a
  "Request colleague access" action creates the invite as your agent ->
  their agent.

## What does NOT change

- IAP assertion verification, the teach lane, colleagues tools, quorum,
  the shield, the seeder, avatars, skills/marketplace, Slack lanes.
- `slack_operators` and `operators` keep their current meanings.
- No daedalus changes at all in this arc.

## Security

- All authorization decisions server-side from the verified assertion
  email; the UI never carries authority.
- Okta token: never logged, never in the DB, env-only, operator-delivered.
- Fail-closed on grants (Okta down = less visibility, never more).
- Invites are consent records: acceptance is by the receiving agent's
  owner (or an operator), recorded with who decided and when.
- No client names in tracked files (standing); no em dashes in copy.

## Testing

- Unit: group resolver (cache, TTL, fail-soft downward, filtered
  universe); visibility matrix (operator / member / owner-only /
  stranger); owner-or-operator gates per route; invite lifecycle;
  pair-set computation (same-group implicit + accepted invites).
- Cabal PR: allow-list enforcement at re-mint (allowed pair passes,
  unlisted pair 403s, operator unaffected), sync endpoint auth.
- UI: whoami display; group dropdown; filtered lists; Access tab flows;
  copy guards.
- Live acceptance: two test users (operator + a non-member), the
  visibility matrix exercised through IAP; an invite requested, accepted,
  and the cross-group ask_colleague succeeding only after acceptance
  (once cabal enforcement is deployed, re-verified at the wire with a
  pre-acceptance 403).

## Open items for the operator

1. Deliver the shared Okta token into kobo's env (operator terminal;
   feature is dark until then, everything else ships).
2. Name the initial `groups:` list for org.yaml.
3. The cabal enforcement PR needs upstream review like every cabal change.

## Rollout

Phase the plan: (A) identity + ownership + gates (no new secret needed),
(B) groups + visibility (needs the token), (C) invites + Access tab +
cabal pair enforcement. Each phase leaves the room whole; A ships alone
safely, C closes the open-discovery window for good.
