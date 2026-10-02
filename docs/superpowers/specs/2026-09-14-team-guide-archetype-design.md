# Team Guide Archetype + Per-Agent Observability Links — Design

**Date:** 2026-09-14
**Status:** Approved in conversation (Kyle), spec for implementation
**Scope:** kobo (api + ui + composer), the org's skills marketplace (one new skill), the org config repo (org.yaml keys)

## Why

Teams onboard onto kobo slowly because creating a first agent demands answers
(persona, tools, purpose) from people who don't yet know why they'd want one.
The team guide inverts that: one click, pick your group, name it, and a
fully-deployed informational agent appears — born knowing its job is to learn
the team and answer for it. It gives every team an immediately useful agent,
a place to practice the Teach gesture, and a gateway to building their real
agents later. One guide per team also becomes the substrate for cross-team
discovery: any team (or agent) can find any other team's guide by tag and ask
what that team does.

Separately: metrics, logs, and traces are live per agent in argus
(`{agent}-agent-health` dashboards). The agent card and detail page should
link there.

## Part 1 — the team guide archetype

### 1.1 The persona (built-in)

A new built-in persona `team-guide`, shipped beside `helpful-generalist` in
kobo's built-in persona set. Constitution, parameterized with `{team}` (group
display name), `{name}` (the agent's chosen name), and optionally `{voice}`:

- **Purpose:** "You are {name}, the informational guide for the {team} team.
  You exist to learn everything about this team and answer questions about it
  — for new teammates, for other teams, and for other agents."
- **Hunger to learn:** in every conversation with team members, actively
  solicit knowledge. Ask one question at a time. Encourage teammates to use
  the Teach gesture so knowledge is durably kept.
- **Never act:** you do not create tickets, open PRs, change systems, or take
  actions of any kind, for anyone. When asked, decline warmly and explain you
  are purely informational — and point at what you *can* do (answer, learn,
  introduce the team).
- **Provenance honesty:** answer from what you have been taught. When you
  don't know: say so, say you'd love to be taught, never invent. Never present
  uncertain knowledge as certain.
- **Cross-team etiquette:** answer other teams' questions about your team
  gladly. For anything sensitive or decision-shaped, direct the asker to the
  team itself.

**Voice overlay (optional):** when the creator supplies a voice, the persona
gains a final section: "Style: channel the conversational voice and cadence
of {voice} — the wit, warmth, and rhythm, not the identity. You are always
{name}, {team}'s guide; you never claim to be {voice}, and the style never
overrides your conduct: you still refuse actions, still cite what you've been
taught, still interview." When no voice is given, the section is omitted
entirely. Layering order is explicit: constitution first, voice as flavor —
style can never erode conduct.

### 1.2 The skill (marketplace)

One new skill, `team-guide` **1.0.0**, published to the org's skills
marketplace under the publish law (frontmatter carries version + profiles;
validate before push — a bad skill bricks all publishes). Auto-attached at
kiosk birth. Content, two halves:

**The interview.** A knowledge map the guide works through opportunistically
over many conversations (never as a form): team mission; services and systems
owned; repos; day-to-day tooling and vendors; technologies and languages;
oncall and escalation paths; rituals and meetings; team glossary; key roles
and who holds them; how to get access to the things the team uses. Rules:
one question at a time; follow the thread (an answer about tooling begets a
question about who runs it); periodically summarize what it has learned and
ask what's missing; nudge teammates toward the Teach gesture for anything
worth keeping.

**The conduct.** Refuse all actions with a friendly explanation; provenance
honesty (attribute knowledge to teaching, admit gaps, invite teaching);
cross-team answering etiquette; when the same question keeps arriving
untaught, tell the team it's a gap worth teaching.

### 1.3 The create lane (API)

`CreateAgentRequest` gains two optional fields:

- `archetype: Literal["team_guide"] | None = None`
- `voice: str | None = None` (bounded like other free-text create fields;
  only meaningful with an archetype)

When `archetype == "team_guide"`, the server **forces** (ignores/overrides
any client-supplied values for):

- persona → built-in `team-guide`, interpolated with team display name,
  agent name, and voice
- tools → colleagues (list/ask) + techne personal memory **only**: no
  github, jira, cloudtrail, slack_read, or liminal allow entries in the
  bundle. "Cannot act" is enforced in the composed bundle — which the
  warden already treats as law — not in UI defaults.
- purpose tags → `team-guide` + the group name (the canonical discovery
  tags; creator tags may be added but these two cannot be removed at create)
- visibility → shared (other teams and agents can find and chat with it)
- skills → `team-guide` attached
- model, runtime → org defaults

Everything downstream is the standard pipeline unchanged: compose, repo
push, gojira.yaml (`class: agent`), deploy, checklist progress, avatar at
birth (the existing create-identity flow supplies the face).

**Duplicate guard:** create with `archetype == "team_guide"` for a group
that already has a live agent tagged `team-guide` in that group → 409 with
the existing agent's name in the error body. One guide per team is an
invariant, not a convention.

MCP: `kobo_create_agent` gains the same two optional params, same forcing,
same 409.

### 1.4 The UI

- A secondary button under **Create agent**, copy: **"Deploy your team's
  guide"** (visually subordinate to Create agent).
- Click → a small modal, three fields: **Group** (picker, required),
  **Name** (text, required — the team names their own guide), **Voice**
  (text, optional, with the one-line explainer: "name a famous person,
  artist, or regional character your guide should sound like").
- Submit → the normal deploy progress checklist, then the open-chat
  affordance.
- If the chosen group already has a guide, the modal (on group select)
  swaps its submit for "Open {existing name}" linking to that agent.

## Part 2 — per-agent observability links

- `org.yaml` gains `argus_url` and `argus_project` (both strings; the
  standing convention: features are off when config is absent).
- `GET /api/catalog/observability` → `{"url": ..., "project": ...}` when
  both keys are set, `{"url": null, "project": null}` otherwise (same
  pattern as `GET /api/catalog/runtimes`; the UI queries it once and hides
  the links on nulls).
- **Agent card:** a small "Observability" link (icon + label consistent
  with existing card affordances).
- **Agent detail page:** a labeled row/link.
- Both link to `{argus_url}/projects/{argus_project}/dashboards/{agent_name}-agent-health`
  — the per-agent dashboard that now carries metrics, logs, and traces.
- No health probing, no iframe embedding, no per-signal deep links in v1 —
  one link to the dashboard that already aggregates all three signals.

## Out of scope (recorded so they aren't re-litigated)

- Pre-deploying guides for all groups (adoption stays opt-in, one click).
- slack_read / ambient absorption for the guide (knowledge comes from
  teaching only — provenance is the product; revisit after real usage).
- Agent-to-agent cross-team discovery UX (the tags + shared visibility +
  colleagues tools make it *possible* now; a dedicated discovery surface is
  its own future arc).
- Seeding techne with pre-baked team knowledge (persona carries the mission;
  the team supplies the knowledge).
- Voice moderation/allowlisting (free text, internal tool, style-not-identity
  framing in the persona is the guardrail).

## Testing

- **API:** archetype forces tools/tags/visibility regardless of
  client-supplied values; voice present → persona contains the overlay with
  the literal voice string; voice absent → overlay section absent; duplicate
  guard 409 names the existing agent; MCP param parity.
- **Composer:** kiosk bundle contains colleagues + memory only (no other
  provider blocks), tags present, persona interpolated.
- **UI:** button renders; modal validates required fields; existing-guide
  group swaps submit to "Open"; observability link renders only when both
  argus keys configured (card + detail).
- **Skill:** frontmatter validates under the marketplace gate before push.

## Dependencies (estate review, 2026-09-14)

- **Fleet runtime must include daedalus #49 before the first kiosk deploys.**
  #49 (merged 9/14) fixes `teach_authorized` refusing nothing — any verified
  caller was operator-grade on the teach/knowledge/tool lanes. The 9/10
  runtime pin predates it. A shared, teach-hungry agent must not ship on a
  runtime where any colleague can stuff its knowledge. Take #52 (enforcement
  denial fix) in the same re-pin.
- Deployed cabal is `7ce9d82-c1` (9/9): the delegation-guardrails arc
  (merged 9/12–14) is not live yet. The kiosk is compatible before and after
  that roll (light colleague user, well inside any budget).
- Post-deploy check: the kiosk appears in colleague lists (daedalus #38/#39
  now filter out colleagues that cannot answer).
- Liminal and gojira: unchanged since 9/10; no interaction.

## Rollout order

1. Skill published to marketplace (validate first — publish law).
2. kobo api + composer + ui in one image; Kyle rolls.
3. org.yaml: `argus_url` + `argus_project` (feature lights up on roll).
4. First live kiosk: deploy one for the SRE group as the dogfood fixture;
   its first interview conversation is the acceptance test.
