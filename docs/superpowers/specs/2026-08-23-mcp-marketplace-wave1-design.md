# MCP marketplace wave 1 -- skills and tooling on the connector surface

Status: approved (operator rulings encoded below). Wave 1 of the
full-marketplace decomposition; waves 2-4 are registered at the bottom and
are OUT of this spec's scope.

## Context

The MCP server arc gave the room a connector surface of ten tools, none of
which touch skills or tooling. Meanwhile the room already owns every
underlying mechanism this wave needs:

- a marketplace catalog reader for the versioned bucket dialect
  (`catalog/catalog.json`, `catalog/<id>/versions/<v>/SKILL.md`) behind
  `config.skills_bucket`, ingested into the FTS catalog;
- the per-agent skills door (`kobo_api.skills`: stage/delete
  `bundle/skills/<id>/` in the agent repo);
- the tooling door (`kobo_api.tooling`: the mechanical validator -- size,
  UTF-8, `ast.parse`, stdlib-only imports, top-level sync
  `pre_task`/`post_draft` -- plus upload and cross-repo import, both
  runtime-re-pinning);
- the GitHub App identity that already writes to agent repos, and
  `config.skills_repo` naming the marketplace repo (bare name, prefixed by
  `config.github_org`).

A live exercise (the canary agent) proved the repo lane end to end and
exposed the one seam this wave must close: staging skills by repo edit
leaves the room's DB picks empty, so the skills-derived team axis never
updates and the directory card stays undressed.

## Operator rulings (2026-08-23)

1. **Creation gate: direct commit, instant publish.** A created skill or
   tool commits straight to the marketplace repo's default branch; its CI
   publishes immediately. Accepted trade-off, recorded: no human review
   before org-wide catalog visibility. Standing mitigations: mechanical
   validation runs BEFORE any commit, the commit is attributed to the
   caller in the message, and every call lands in the MCP audit log.
2. **Owner team is membership-gated.** The caller must hold the named team
   in their resolved directory groups (fail-closed when the resolver is
   down); operators may name any team. New owner teams are permitted --
   any group slug may become an owner team.
3. **Shared tooling lives in the marketplace repo** under a new
   `tools/<id>/` tree beside `skills/<id>/`, same frontmatter dialect.
4. **Baseline tooling is auto-staged at create**, opt-out via
   `skip_baseline`.
5. **Approach: thin tools + client-side recommendation.** No server-side
   model calls; the recommendation protocol lives in tool descriptions.

## Tool surface

Every tool follows the MCP arc's existing pattern: the authz shim's
synthetic room request over existing gates, ToolError normalization
(403/404 -> "not found" where visibility applies), the shared audit log
and per-email rate limit. No logic is duplicated from the modules named.

1. `kobo_list_skills()` -- the marketplace skills catalog: id,
   description, when_to_use, tags, owner, version. Any signed-in caller;
   the catalog is org-visible by design.
2. `kobo_list_tools()` -- the marketplace `tools/` tree, read via the
   GitHub App: id, description, owner, tags from each tool's `TOOL.md`
   frontmatter. Any signed-in caller. (Wave 2 moves this into the FTS
   catalog; wave 1 reads the repo directly.)
3. `kobo_set_agent_skills(agent, add=[], remove=[])` -- stage marketplace
   skills into / remove them from a visible agent the caller owns (or
   operator). Writes through the skills door AND records the picks on the
   room's agent row in the same operation, so the skills-derived team axis
   and the card's skills list stay true -- closing the canary seam.
4. `kobo_add_agent_tool(agent, source)` -- `source` is either a
   marketplace tool id (the tool's `.py` fetched from the marketplace
   repo) or an inline `{filename, content}`. Both paths pass the tooling
   validator before anything lands; owner-or-operator on a visible agent.
5. `kobo_create_skill(skill_id, content, team)` -- membership-gated per
   ruling 2. Completes/validates frontmatter (`name` = skill_id, `owner` =
   team, `description` required, body non-empty), rejects an existing id
   with a conflict error (updates are wave 3's channel semantics, not
   wave 1), then direct-commits `skills/<skill_id>/SKILL.md`.
6. `kobo_create_tool(tool_id, content, team)` -- same lane into
   `tools/<tool_id>/`: a `TOOL.md` (frontmatter: name, description, owner,
   tags) plus `<tool_id>.py`, where the `.py` content must pass the
   tooling validator BEFORE the commit is attempted. Same membership gate
   and collision rule.
7. `kobo_create_agent` gains `skip_baseline: bool = false`. Compose stages
   the baseline pack into `bundle/tools/` unless skipped.

Slug law: skill and tool ids reuse the marketplace dialect's slug shape;
the tool's `.py` filename must also satisfy the tooling filename law.

### Recommendation protocol (tool descriptions, not code)

The descriptions of the list/create tools carry the protocol the operator
asked for, so any connected client model behaves the same way: before
creating, list and check fit; prefer an existing skill over a near-twin;
a new agent should always leave create with tooling (the baseline pack at
minimum, purpose-fit tools recommended on top); a created skill or tool
lands under the caller's own team.

## Baseline pack

Two generalized hooks seeded in the marketplace repo as ordinary tools:
`baseline-leakguard` (post_draft: redacts credential-shaped strings) and
`baseline-probe-guard` (pre_task: refuses credential-fishing asks). The
pack is discovered by the `baseline-` id prefix convention -- no new org
config key, no loader ripple. Compose fetches the pack's `.py` files at
create; if the marketplace repo is unreachable, the create proceeds
without the pack and the step records a visible warning (an agent must
not fail to exist because the marketplace hiccuped) -- fail-open for
availability, logged loudly, ledgered as such.

## Failure and edge behavior

- Marketplace repo unreachable: list/create tools fail plainly with the
  transport error class; never a partial commit (validate-then-commit
  ordering).
- Collision: byte-identical conflict error whether the id exists in
  skills/ or tools/ respectively; no update-in-place.
- Catalog latency: create responses state that `kobo_list_skills` shows a
  new entry only after the publishing CI runs and the room's catalog TTL
  (~5 minutes) rolls over.
- Audit: args land in the audit log under the existing top-level
  redaction; `content` is destined for an org-visible catalog, so it is
  not secret-classed, but the log stores it length-capped.
- Resolver down: membership checks fail closed (create refused), same
  posture as the room's group gates.

## Testing

House standard: per-tool suite coverage (membership matrix incl. operator
bypass and resolver-down, visibility 404 normalization, collision, the
validator rejecting a bad tool before any commit, baseline staging with
and without `skip_baseline`, unreachable-marketplace warning path) on
extended fakes; adversarial review with mutation verification per task;
wire acceptance from a live connector session (create a skill under the
caller's team, see it listed, stage it to a test agent, the agent reads
it; create a tool, add it to the agent, prove the hook on the wire; a
fresh agent ships with the baseline pack attested on its card).

## Waves 2-4 (registered, not in scope)

- **Wave 2, catalog depth:** tools/ joins the FTS catalog as its own kind;
  `kobo_search_catalog`; owner-axis browsing.
- **Wave 3, channels:** version iteration semantics -- updating an
  existing skill prereleases to the canary channel for the owning team,
  promote-to-stable bumps the version. Creation stays direct-commit per
  ruling 1; channels gate UPDATES, not creation.
- **Wave 4, UI parity:** marketplace browser panels in the room; the
  Skills/Tooling tabs gain marketplace pickers.

## Operator opens

- Grant the room's GitHub App write access to the marketplace repo
  (today it only reaches agent repos and the org config repo).
- Heads-up to the marketplace repo's owner: a `tools/` tree and
  bot-authored direct commits are arriving.
