# Agent PR Broker (the org PR GitHub App)

Ruled 2026-08-27. Agents gain the ability to open pull requests on the
org's real repositories, through one org-installed GitHub App whose tokens kobo
brokers per agent, per allowlist, per hour.

## Rulings (operator, 2026-08-27)

1. Repo-access requests are approved by the room's operator set (the
   room's configured admin groups) -- BATCH approval, many repos at once,
   never one-at-a-time-only.
2. PR-only. No direct branch pushes to a default branch; the app carries
   contents:write solely so a PR's own branch can exist. A human merges.
3. The app lives in the org's GitHub organization (where the code is);
   kobo and the agent deployments live in the org's cloud projects.

## The app (already created)

- Named for the org's AI-agents platform -- every PR it opens is authored
  as `<app-slug>[bot]`. App id / installation id live in the room's config
  (a secrets file), never in this repo.
- Permissions: contents read/write, pull_requests read/write, metadata read.
- Installed with "Only select repositories" -- the installation's own repo
  list is the OUTER wall; kobo's per-agent scoping is the inner one. A repo
  must be added to the installation before any broker token can reach it,
  no matter what the allowlist says.

## Two-wall token model

Wall 1 (GitHub, coarse): the installation only spans repos an org admin
added to it.

Wall 2 (kobo, fine): the broker mints installation access tokens with the
`repositories` parameter pinned to exactly the requesting agent's APPROVED
allowlist entries, and `permissions` pinned to `{contents: write,
pull_requests: write, metadata: read}`. An agent can never receive a token
wider than its own approvals, and never a token at all with zero approvals.
Tokens live ~1h and are never stored -- minted on demand, handed to the
caller, forgotten.

This reuses `kobo_api.github_app.InstallationTokenProvider`'s JWT
conventions (iat -60s, exp +9m) with one difference: the broker's mint POST
carries a body; the room's own repo-management app mints unscoped.

## Data: repo_access

One row per (agent, repository) grant request:

    agent_name TEXT, repository TEXT ("owner/name"),
    status TEXT (requested | approved | revoked),
    requested_by TEXT, decided_by TEXT,
    requested_at, decided_at

- Owner (or operator) files requests for MANY repos in one call.
- Operator approves/revokes in BATCH (ruling 1). Approving an already
  approved row is a no-op, not an error.
- Revoked rows stay (audit trail); a re-request flips them back to
  requested.

## Routes

- `POST /api/agents/{name}/repo-access` (owner-or-operator): body
  `{repositories: [..]}` -> rows in `requested`. Repo shape validated
  (`owner/name`).
- `GET /api/repo-access?status=...` (any signed-in room member): the review
  queue; also drives the agent's Access tab section.
- `POST /api/repo-access/decide` (operator only): body
  `{ids: [..], decision: "approved" | "revoked"}` -- the batch gate.
- `POST /broker/agents/{name}/github-token` -- THE BROKER. Its own
  IAP-exempt root (the /mcp doctrine: a root that carries its own auth),
  reachable in-cluster only -- the external ingress never routes /broker. Caller is the AGENT
  itself, not a human: authenticated by its cabal-issued colleague JWT
  (Authorization: Bearer), verified against the switchboard's own JWKS --
  the exact issuer/audience/jwks_url the composer already bakes into every
  bundle's a2a auth block, walked in reverse. Subject must match `{name}`.
  Returns `{token, expires_at, repositories}` scoped by wall 2. 404s for
  an agent with no approved rows, same anti-enumeration discipline as
  every agent route.

## Config (kobo config.yaml -- secrets file, NOT org.yaml)

    pr_app_id: "<the App ID>"
    pr_app_installation_id: "<the installation id>"
    pr_app_private_key_path: /etc/kobo/pr-app.pem

Key mounted from a new k8s secret `kobo-pr-app` (same shape as
`kobo-github-app`); OpenBao/ESO migration rides the same later decision as
the standing kobo-pg secret ruling. All three empty/None = feature off,
routes 503 -- the same absent-equals-off convention as cabal_url.

## Attribution (the room's law, extended to PRs)

The daedalus `github` tool provider (NEXT wave, separate repo) stamps every
PR: head branch `kobo/<agent>/<slug>`, body footer naming the agent and the
requesting human. The app identity marks the PLATFORM; the footer marks the
agent and requester. Enforcement is at the provider (convention), not
cryptographic -- the token cannot constrain a body -- recorded here so
nobody mistakes the footer for a security boundary. The broker's audit log
(who minted, for which repos, when) is the security record.

## Out of scope (this wave)

- The daedalus `github` provider itself (open_pr tool) -- next wave; the
  broker is curl-provable without it.
- Auto-adding repos to the installation (org-admin action, deliberate).
- Any write beyond PR mechanics (issues, reviews, merges).
