# Access Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Visible identity, agent ownership, Okta-group namespaces, and consent-gated cross-group agent-to-agent talk, phased so the room stays whole after every task.

**Architecture:** Phase A (identity + ownership) needs no new secret. Phase B (groups + visibility) adds an Okta-backed GroupResolver behind a protocol, fed by the org's existing shared SSWS token. Phase C (invites + Access tab + cabal allow-pairs) closes the open-discovery window at the switchboard's re-mint seam, with kobo as the policy owner syncing flat caller->target pairs.

**Tech Stack:** FastAPI + SQLModel/sqlite, httpx, Svelte 5, pytest, vitest; cabal (FastAPI) for phase C's enforcement PR.

**Spec:** `docs/superpowers/specs/2026-08-21-access-control-design.md`

## Global Constraints

- All authorization decisions server-side from the VERIFIED assertion email (`request.state.iap_email`); the UI never carries authority.
- `operators` (org.yaml, existing) is the admin allowlist; the existing case-insensitive matcher in main.py (~698-708) is THE operator check, reuse it, never fork it.
- Fail-closed on grants: Okta unreachable means group-derived visibility vanishes (owner + operator rights remain); never wider.
- The Okta token is env-only (`OKTA_API_TOKEN`), never logged, never in the DB, never in a tracked file.
- `group_name` is the ACCESS axis; the existing `team` field (skills-catalog derived) is the capability axis. Never conflate them.
- Chat and teach stay open to any signed-in user who can SEE the agent; mutating surfaces (destroy, slack, tooling, skills, metadata, intake delete, avatar upload) gate owner-or-operator.
- NO client names in tracked files (vendor scan gates every commit, its own step; scan list `.superpowers/sdd/vendor-names.txt`). NO EM DASHES in user-facing copy (both guards stand).
- Copy, verbatim where fixed: forbidden-mutation 403 body `{"error": "Only this agent's owner or a room operator can change it."}`; groups-unavailable banner text `"Group lookups are unavailable right now. You are seeing only what you own."`.
- kobo work on `main`; cabal work on a fresh branch off cabal upstream main.

---

### Task A1: whoami route and visible identity

**Files:**
- Modify: `api/kobo_api/main.py` (new route near the healthz/org-status routes)
- Modify: `ui/src/components/` app shell sidebar component (find the sidebar/shell component the app-shell arc added; the email renders in its footer, muted style)
- Test: `api/tests/test_api_agents.py` or the web-route test file housing org-status tests; UI shell test file

**Interfaces:**
- Produces: `GET /api/whoami` -> `{"email": str | null, "operator": bool, "groups": []}` (groups always `[]` in phase A; B fills it). Email is `getattr(request.state, "iap_email", None)`; operator via the existing matcher.

- [ ] Failing tests: route returns the IAP email + operator flag when middleware set them; `{"email": null, "operator": false, "groups": []}` without IAP. UI: sidebar footer shows the email when whoami returns one, renders nothing when null.
- [ ] Implement route (10 lines, no session needed) + sidebar footer block with a `whoamiQuery` (queryOptions, staleTime Infinity - identity does not change mid-session).
- [ ] Suites green, vendor scan, commit `feat: the room shows who is signed in`.

### Task A2: ownership columns, create-time recording, migration

**Files:**
- Modify: `api/kobo_api/models.py` (Agent: `created_by: str | None = None`, `group_name: str | None = None`)
- Modify: `api/kobo_api/main.py` create route (record `created_by=getattr(request.state, "iap_email", None)`; accept optional `group_name` in CreateAgentRequest, validated in B - in A store verbatim-if-provided from the request but the UI does not send it yet)
- Create: migration note in the task report (the live DB gets `ALTER TABLE agents ADD COLUMN created_by TEXT` / `ADD COLUMN group_name TEXT` at rollout, the slack-arc pattern; SQLite tolerates both on a live table)
- Test: create-route records the caller; old rows read back with NULLs harmlessly.

**Interfaces:**
- Produces: `Agent.created_by`, `Agent.group_name` (A3, B2, C1 rely on both).

- [ ] Failing tests, implement, suites, vendor scan, commit `feat(api): agents remember who made them`.

### Task A3: owner-or-operator gates + owner on the card

**Files:**
- Modify: `api/kobo_api/main.py`: a single helper directly above the route sections:

```python
    def _owner_or_operator_or_403(agent: Agent, request: Request) -> JSONResponse | None:
        """Mutating surfaces belong to the agent's owner and the room's
        operators. Read surfaces never call this. A legacy agent with no
        recorded owner is operator-only to mutate (fail closed, and the
        backfill in the rollout task closes the gap immediately)."""
        email = (getattr(request.state, "iap_email", None) or "").lower()
        if _is_operator(request):  # reuse the existing matcher's seam
            return None
        if agent.created_by and email == agent.created_by.lower():
            return None
        return JSONResponse(
            {"error": "Only this agent's owner or a room operator can change it."},
            status_code=403,
        )
```

  (Adapt the operator-check call to the real existing function's signature; read main.py:695-710 first.) Wire it into: destroy, slack channels/tokens/finish, tooling upload/import/delete, skills edit, metadata patch/regenerate, intake upload/delete, avatar upload, teach EXCLUDED (open), chat EXCLUDED (open).
- Modify: agent card + detail Overview (UI) to show `created_by` when present ("Made by <email>", muted).
- Test: the gate matrix per route class (owner passes, operator passes, stranger 403 with the verbatim copy, ownerless agent operator-only); UI renders the owner line.

- [ ] Failing tests (parameterized across the gated routes), implement, suites, vendor scan, commit `feat: mutating an agent belongs to its owner and the operators`.

### Task B1: GroupResolver protocol + OktaClient

**Files:**
- Create: `api/kobo_api/groups.py`
- Modify: `api/kobo_api/config.py` (`groups: list[GroupChoice] = []` where `GroupChoice{name: str, okta_group: str}`; `okta_domain: str = ""`), `api/kobo_api/org.py` (ORG_KEYS += `groups`, `okta_domain`), `api/kobo_api/clients.py` (Clients gains `groups: GroupResolver | None`, wired in `Clients.real` iff `OKTA_API_TOKEN` env and config.groups non-empty)
- Test: `api/tests/test_groups.py`

**Interfaces (B2/C rely on):**

```python
class GroupResolver(Protocol):
    def groups_for(self, email: str) -> list[str] | None: ...
    # list of CONFIGURED group names the user belongs to;
    # None means the resolver failed (fail-closed signal, distinct from []).

class OktaGroupResolver:
    def __init__(self, domain: str, token: str, universe: dict[str, str],
                 ttl_s: int = 900, client: httpx.Client | None = None,
                 now: Callable[[], float] | None = None): ...
    # universe: config group name -> okta group name.
    # GET https://{domain}/api/v1/users/{email}/groups with
    # Authorization: SSWS {token}; match returned profile.name values
    # against universe's okta names; per-email TTL cache; ANY error -> None
    # (never raises to callers), the error logged WITHOUT the token.
```

- [ ] Failing tests: happy path filters to the universe; cache honors TTL (fake now); 4xx/5xx/network -> None; token never in log records (caplog scan); empty universe -> resolver not constructed.
- [ ] Implement, suites, vendor scan, commit `feat(api): group membership resolved from the directory, cached, fail-closed`.

### Task B2: visibility filtering + create-flow group dropdown

**Files:**
- Modify: `api/kobo_api/main.py`: whoami fills `groups`; a `_visible_agents(session, request)` helper filters the list route (operator -> all; else union of owned + agents whose `group_name` is in the caller's resolved groups; resolver None -> owned only + the banner flag in the response `{"groups_unavailable": true}`); the agent-detail fetch and every sub-route 404 (not 403) for agents outside visibility (existence is also information); create route validates `group_name` against config.groups and REQUIRES it when groups are configured.
- Modify: UI: create modal group dropdown (from whoami/groups-config endpoint - add `groups` to `GET /api/catalog/models`' sibling or a tiny `GET /api/catalog/groups`); dashboard renders the banner copy (verbatim, Global Constraints) when `groups_unavailable`; agents list simply reflects the filtered response.
- Test: the visibility matrix (operator / member / owner-only / stranger / resolver-down), create-route validation, UI dropdown + banner.

- [ ] Failing tests, implement, suites, vendor scan, commit `feat: the room shows each person their own aisle`.

### Task B3 (controller, not a subagent): secret + groups config + rollout + acceptance

- [ ] Fetch the shared token from the org's secret manager project (the operator named the project; the secret id matches the okta-viewer terraform reference) and create k8s secret `kobo-okta` (`OKTA_API_TOKEN`) in ns kobo; add to the kobo deployment env (envFrom). If the harness blocks secret creation, hand the exact commands to the operator (standing protocol).
- [ ] Seed `groups:` in org.yaml (surgical text edit, comments preserved) from a live query of the directory's engineering groups; the operator edits the list at will afterward. Add `okta_domain`.
- [ ] Backfill: `UPDATE agents SET created_by='<operator email>', group_name='<operator group>' WHERE created_by IS NULL` in-pod (except gemini-one -> its real creator's email from the issue reporter).
- [ ] Build + roll kobo; live acceptance: whoami through real IAP (operator sees email+groups); visibility matrix with a second account if available, else the resolver unit evidence + operator path; create an agent with a group through the UI.

### Task C1: invites + user grants schema and API

**Files:**
- Modify: `api/kobo_api/models.py`: `A2AInvite(id, from_agent, to_agent, requested_by, status: pending|accepted|declined|revoked, decided_by, created_at, decided_at)`; `AgentUserGrant(id, agent_name, email, added_by, created_at)`
- Modify: `api/kobo_api/main.py`: routes `POST /api/agents/{name}/invites` (body `{"to_agent": ...}`; caller must be owner-or-operator of the FROM agent; to_agent must exist), `GET /api/agents/{name}/invites` (incoming + outgoing, visible to owner/operator), `POST /api/invites/{id}/accept|decline` (owner-or-operator of the TO agent), `DELETE /api/invites/{id}` (revoke, either side's owner), user-grant add/remove under `/api/agents/{name}/grants` (owner-or-operator; grants extend VISIBILITY to a named email).
- Create: `api/kobo_api/pairs.py`: `allowed_pairs(session, config) -> set[tuple[str, str]]` - same-group agent pairs implicit (both directions) + accepted invites (directional, from->to); destroyed agents drop out naturally.
- Test: lifecycle matrix, permission matrix per route, pair computation (same-group symmetry, invite directionality, revoke removes).

- [ ] Failing tests, implement, suites, vendor scan, commit `feat(api): cross-group colleagueship is asked for and granted`.

### Task C2: Access tab UI

**Files:**
- Create: `ui/src/components/AccessPanel.svelte` (+ tab wiring in the agent detail tab bar, owner/operator only)
- Test: its test file.

Sections: People (the group fact line "Everyone in <group> can see this agent."; user grants list + add via email input; remove); Agents (incoming invites with Accept/Decline, outgoing with state, active pairs with Revoke); a Request-access affordance on OTHER agents' detail pages (visible-not-yours) creating the invite with a from-agent picker limited to YOUR agents. Copy plain, no em dashes.

- [ ] Failing tests (render matrices per role, invite actions call the right endpoints), implement, suites, vendor scan, commit `feat(ui): the access tab, people and agents, ask and grant`.

### Task C3: cabal allow-pairs enforcement (cabal repo, fresh branch)

**Files (cabal repo):**
- Modify: the colleague re-mint path in `src/cabal/core/routes/tasks.py` (the dispatch re-mint that lets `agent:X` reach target Y): consult a `dispatch_pairs` table; missing pair -> 403 with a readable body; OPERATOR-scoped callers unaffected (the room's own relays never gate).
- Create: `PUT /admin/dispatch-pairs` (operator scope): body `{"pairs": [["agent:a","b"], ...]}` replaces the whole set atomically (kobo owns policy; cabal stores dumbly). `GET /admin/dispatch-pairs` for inspection.
- Migration: additive table; EMPTY TABLE MEANS OPEN (deploy-safe: enforcement begins the first time kobo syncs; decision-record this explicitly in CABAL-DECISIONS.md as the D-series entry).
- Test: enforcement matrix (pair allowed / missing / operator caller / empty table open), sync endpoint auth + atomic replace.

- [ ] Failing tests, implement, cabal suite green (~557+), vendor scan (STILL the same list - cabal is a family repo), commit, push branch, open PR titled `Dispatch pairs: the switchboard checks who may call whom`.

### Task C4: kobo syncs pairs + rollout + wire acceptance (controller finishes)

**Files:**
- Modify: `api/kobo_api/clients.py` `CabalClient.put_dispatch_pairs(pairs)`; call it (fail-soft, logged) from every pair-mutating seam: agent create/destroy completion, invite accept/revoke, group change.
- Test: sync fires on each trigger; cabal error logged not surfaced.
- [ ] Implement via subagent, review, then controller: deploy the cabal image (integration branch pattern), roll kobo, live acceptance: same-group ask_colleague works; cross-group ask 403s at the wire pre-invite; invite accepted -> the same ask succeeds; revoke -> 403 again. Close the open-discovery window in the ledger.

## Execution notes

- Task order strict: A1 -> A2 -> A3 -> B1 -> B2 -> B3 -> C1 -> C2 -> C3 -> C4. Per-task reviews as always; final whole-arc review before C4's rollout.
- B3 and C4 are controller tasks (secrets, org.yaml, cluster, acceptance).
- The cabal PR (C3) rides review upstream like #123/#124; deploys don't wait for merge (integration-image pattern, ledgered).
