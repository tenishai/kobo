// Typed client over the kobo-api HTTP surface. Every function funnels its
// Response through `parse()`, which turns a non-2xx status into an ApiError
// (message taken from a JSON `{"error": "..."}` body when present, otherwise
// a readable fallback built from the status line) and turns a body that
// isn't valid JSON at all (an HTML proxy-error page, a plain-text 500 from
// some upstream that isn't kobo-api) into an ApiError too, instead of
// letting a raw SyntaxError leak out of `JSON.parse`.

/** Is the agent running right now, from the cluster (kobo_api.health):
 * `down` when its Deployment has fewer available pods than it wants, with
 * the count and the worst container's restarts and wait reason. `null`
 * until the room has asked, and never sent for an agent that is not
 * ready. Distinct from `status`, which is about setup, not today. */
export type AgentHealth = {
  state: "up" | "down";
  reason: string | null;
  checked_at: string;
};

export type AgentSummary = {
  name: string;
  status: string;
  model: string;
  model_label: string;
  created_at: string;
  has_avatar: boolean;
  health?: AgentHealth | null;
  /** System tasks (the nightly learnings pass) that have failed several
   * runs in a row for this agent (issue #142). Older rooms omit it. */
  system_tasks_failing?: string[];
  /** The verified email that created this agent (access-control Task A2/
   * A3), `null` for a pre-A2 row or one created while IAP was off. Drives
   * the "Author: <email>" line on the card and detail Overview, and is the
   * OWNER half of the server's owner-or-operator mutation gate -- shown
   * here for information only, never itself a source of authority. */
  created_by: string | null;
  /** The agent's ACL namespace (access-control Task A2), distinct from
   * `team` (the skills-catalog capability axis) -- `null` until Task B2's
   * group picker starts sending one. Not yet rendered anywhere; carried
   * here so Task B2's visibility filter has it on the wire already. */
  group_name: string | null;
  /** Team-guide UI (team-guide spec 2026-09-14, Task 5): `"team_guide"` for
   * an archetype-deployed agent, `null` for an ordinary one. Alone this
   * says only "is a guide," never WHICH team's -- a guide's own
   * `group_name` above is forced to the commons, not the team it serves --
   * so `purpose_tags` (below) carries the actual tag pair callers match
   * against. */
  archetype: string | null;
  /** Free-form purpose chips, same shape/semantics as `AgentDetail`'s own
   * field -- present here too (as of the same task) so a client can run
   * the team-guide pre-check (does group X already have a guide?) off the
   * list it already holds, with no per-agent detail fetch. `[]` when none. */
  purpose_tags: string[];
  /** Connected agents (design 2026-09-21): `"composed"` for an agent the
   * room built and whose repo it owns, `"connected"` for a team's own repo
   * the room only deploys. Present on the list as well as the detail so a
   * card can carry the marker without a per-agent fetch. */
  kind: string;
  /** The agent's one-sentence capability line, same field `AgentDetail`
   * carries. OPTIONAL here, and absent from `GET /api/agents` as it
   * stands: the card asks for it first (`agentDescription`) and falls
   * through to the tags the list does send, so a server that starts
   * sending it upgrades every card with no change on this side. */
  capability?: string | null;
  /** The persona's name, same shape and the same story as `capability`
   * above: the card's second choice for its description line, absent from
   * the list route today. */
  persona?: string | null;
};

/** The two kinds of agent (`kobo_api/kinds.py`, connected-agents design
 * 2026-09-21), mirrored here verbatim: the server is authoritative for what
 * these strings mean, the UI only has to agree with it. Compared against
 * these constants, never truth-tested, so a later kind reads as `composed`
 * until it asks for its own treatment. */
export const KIND_COMPOSED = "composed";
export const KIND_CONNECTED = "connected";

/** How a connected agent runs, same mirroring as the kinds above.
 * `RUN_MODE_HEADLESS` is only accepted when the room has a class for it;
 * the scan says so (finding code `headless_unavailable`) rather than the
 * client guessing. */
export const RUN_MODE_HTTP = "http";
export const RUN_MODE_HEADLESS = "headless";

export type StepView = {
  name: string;
  label: string;
  status: string;
  error: string | null;
};

/** An agent's Slack channel list plus wiring state (two-lane Slack,
 * 2026-08-17) — always present on `AgentDetail`, unlike the old two-shape
 * `slack: {...} | null`. `app_id` stays `null` until an operator creates the
 * Slack app; `channels`/`state` reflect the saved channel list and wiring
 * status regardless of whether an app exists yet. `state` mirrors the
 * server's `derive_state`: `"idle"` (no channels, not wired), `"waiting"`
 * (channels saved but not yet wired, or wired with no channels left),
 * `"connected"` (channels saved AND wired). */
/** How Slack wakes the agent in a channel (2026-10-06): only an @-mention,
 * a mention and then the thread it started, or every message. */
export type SlackWake = "mention" | "thread" | "channel";

export type AgentSlack = {
  app_id: string | null;
  channels: string[];
  state: "idle" | "waiting" | "connected";
  wake: SlackWake;
  /** False when the room has no Slack config-token store, so no app can
   * be created or reconnected from here (issue #262). */
  available: boolean;
  /** Whether this room makes Slack apps itself (issue #371): Slack then
   * lives on the Access tab and the Slack tab shows only for `legacy`. */
  relay: boolean;
  /** This agent has a Slack app from the Slack tab's paste flow (an
   * app-level token), so its Slack tab stays whatever `relay` says. */
  legacy: boolean;
};

/** The Access tab's Slack section (issue #371), `GET /api/agents/{name}/slack`:
 * `state` is `none` (no app yet), `created` (the app exists at Slack and
 * `install_url` is where the owner installs it) or `installed`. */
export type SlackAppView = {
  enabled: boolean;
  available: boolean;
  state: "none" | "created" | "installed";
  app_id: string | null;
  install_url: string | null;
  workspace: string | null;
  app_name: string | null;
  channels: string[];
  wake: SlackWake;
  legacy: boolean;
};

export type AgentDetail = AgentSummary & {
  persona: string;
  /** The agent's directory team (persona-metadata plan, 2026-08-18) — the
   * create request no longer sends one (the create form asks only the
   * Group question; the server defaults "shared"), editable afterward
   * through Edit details/`patchAgentMetadata`. Always a slug
   * (`TEAM_SLUG_RE` server-side). */
  team: string;
  /** Free-form purpose chips (persona-metadata plan), persona-drafted at
   * create time with a deterministic fallback, editable afterward. Always
   * present, `[]` when none. */
  purpose_tags: string[];
  /** One plain sentence describing what this agent can actually do
   * (persona-metadata plan) — earned room-side once the agent first
   * reaches ready (after its hello step), `null` until then. Editable
   * afterward, plus Regenerate. */
  capability: string | null;
  /** Raw `agent.skills` DB entries, verbatim: a catalog pick carries
   * `team`/`sha`; a locally-authored guardrail skill carries neither (just
   * `source: "local"`) — see `AgentSkill` (the richer, server-normalized
   * shape `GET /api/agents/{name}/skills` serves) for the shape both are
   * mapped into elsewhere in this room. */
  skills: { name: string; sha?: string; team?: string; source?: string }[];
  repo_full_name: string | null;
  first_reply: string | null;
  error: string | null;
  steps: StepView[];
  slack: AgentSlack;
  /** Whether the Danger zone should render at all, false when an operator
   * config block hides destruction from this room entirely (see the
   * server's `_operator_block`). The route itself is the real enforcement
   * boundary, this is UX only. */
  destroy_visible: boolean;
  /** Count of pending learnings awaiting a verdict; the Learnings tab
   * badge reads this, never a client-side inference. */
  pending_learnings: number;
  /** Whether this caller may approve/deny learnings, computed server-side
   * by the decide gate (destroy_visible precedent): the panel hides its
   * verdict buttons off this; the route stays the real enforcement. */
  can_decide_learnings: boolean;
  /** The image this agent runs on (harness wave, 2026-09-23). Already
   * resolved server-side: an agent that never moved off the org default
   * carries no image of its own, and the read route answers with the
   * room's rather than nothing. Absent on an older server. */
  runtime_image?: string;
  /** The image the room builds on today. The Harness tab's state line is
   * the comparison of the two: equal is "Current", different is "Behind".
   * Absent on an older server. */
  room_runtime_image?: string;
  /** Whether the agent may consult its colleagues (issue #131): `default`,
   * `roster-only` or `none`. Absent on an older server. */
  colleague_mode?: ColleagueMode;
  /** The identity the agent runs as: its Kubernetes service account and
   * the Google service account bound to it, plus the room's project. What
   * an access request to another project names. Absent on an older server. */
  identity?: AgentIdentity;
};

export type AgentIdentity = {
  service_account: string;
  gsa_email: string;
  project: string;
};

/** One catalog search result — a skill or a persona, both served by the
 * same shape (`GET /api/skills/search`, `GET /api/personas/search`). `sha`
 * is set for skills (their content's git blob sha); `builtin` is set for
 * personas (true for the shipped defaults, false/absent for user-created
 * ones). */
export type CatalogEntry = {
  id: string;
  name: string;
  description: string;
  team: string;
  tags: string[];
  sha?: string;
  builtin?: boolean;
};

/** The persona builder's form (Persona builder v2 addendum, then the
 * Persona v3 / teamless-create addendum) — posted verbatim as the body of
 * `POST /api/personas`. Four fields, matching the four fields the v3
 * preview actually shows: no `tags` (the server derives them from
 * name/who_they_are/what_they_do — `kobo_api.personas._derive_tags`), no
 * role/purpose/tone/tone_note (Rules absorbs what Boundaries used to), and
 * `team` (persona team scope): the group the persona is filed under,
 * picked on the builder's "Who is this persona for?" field. `"shared"` is
 * the commons; any other team takes membership (the server's team gate).
 * Optional on the type because the server still defaults an omitted key
 * to `"shared"` (`CreatePersonaRequest.team`). */
export type PersonaForm = {
  name: string;
  who_they_are: string;
  what_they_do: string;
  rules: string[];
  /** Create-flow polish (2026-08-28): tags persist from the draft or the
   * hand -- the server no longer derives any. */
  purpose_tags: string[];
  team?: string;
};

/** The Persona v3 "describe the job, approve the soul" draft step's result
 * (`POST /api/personas/draft`) — the same name/who_they_are/what_they_do
 * shape as `PersonaForm`'s first three fields, prefilled into the v2
 * preview for the operator to edit before Create. The server always
 * returns something creatable — a generator failure (unconfigured/erroring)
 * falls back to a deterministic, job-derived draft server-side
 * (`kobo_api.persona_draft.finalize_draft`) rather than failing the
 * request — so `draftPersona` below only ever rejects on a genuine
 * transport/route failure, never on "the generator was down". */
export type PersonaDraft = {
  name: string;
  who_they_are: string;
  what_they_do: string;
  /** Persona-drafted purpose chips (persona-metadata plan), ≤5, lowercased
   * and deduped server-side (`kobo_api.persona_draft.finalize_draft`) —
   * falls back to the persona's own derived tags when the model omits
   * them. The create station (CreateModal) pre-fills its chips editor from
   * this when the draft came from a persona actually built here; a
   * search-picked existing persona has no draft at all, so its chips start
   * empty instead. */
  purpose_tags: string[];
};

/** One name-suggestion card (create flow v2 "naming ceremony" addendum) —
 * the generator's best-effort candidate, or a pick from the deterministic
 * curated-pool fallback when the generator is down/unusable. `reason` is
 * already clamped to <=80 chars server-side (`kobo_api.namegen`'s
 * `_normalize_reason`) — nothing further to trim on this side. */
export type NameSuggestion = { name: string; reason: string };

export type OrgCheck = { id: string; label: string; ok: boolean; detail: string };

export type OrgStatus = { org: string | null; wired: boolean; checks: OrgCheck[] };

/** `GET /api/whoami`'s response (access-control Task A1, groups made real in
 * Task B2) — the verified identity this room already knows about the
 * caller. `email` is `null` whenever IAP isn't configured or verification
 * found no claim; `operator` is server-derived off `config.operators`,
 * never something the UI computes itself. `groups` is the caller's resolved
 * group membership when a directory resolver is wired, `[]` when the groups
 * feature isn't configured at all. `groups_unavailable` is true only when a
 * resolver IS wired but couldn't answer for this email right now (a
 * directory outage) — WATCH ITEM: no UI affordance keys off `operator`
 * either (a dev-parity room with no configured operators reads `false`
 * here while gates behave as if everyone were one) — every screen stays a
 * pure reflection of what the server's own routes actually do.
 *
 * `pending_group_requests` (shared-group + group-requests wave) is present
 * ONLY when the server has already decided the caller is an admin — absent
 * (not `0`) for everyone else. The group-requests admin card
 * (`GroupRequestsCard.svelte`) reads its PRESENCE and value directly,
 * deliberately never `operator` above, per the room's own watch item: no UI
 * surface may gate on `operator` for anything a dev room needs, but keying
 * a purely additive display off a field the server only ever populates for
 * admins is API-driven, not client-side gating. */
export type Whoami = {
  email: string | null;
  operator: boolean;
  groups: string[];
  groups_unavailable: boolean;
  pending_group_requests?: number;
  /** The account menu's two switches, both the server's call: the admin
   * panel is on for this room, and where Log out goes (null hides it). */
  admin_panel?: boolean;
  /** True when the permission service is on; the Harness then has no
   * Identity and access section (#292). */
  access_on?: boolean;
  /** True when the room holds a GitHub app for its own pull requests (skills,
   * tooling), from the room or the deployment config (#304). */
  github_available?: boolean;
  logout_url?: string | null;
};

/** A leak-safe reference to one side of an invite (access-control Task C1's
 * `_agent_ref`): a name plus that agent's group, nothing else -- the shape
 * every invite's `to_agent` renders through, since that side may sit
 * outside the caller's own visible set. */
export type InviteAgentRef = { name: string; group_name: string | null };

/** One cross-group colleagueship invite (`_invite_repr`, access-control
 * Task C1) -- `from_agent` is always a bare name (the caller of every route
 * that can see an invite at all already has full standing on that side);
 * `to_agent` is capped to `InviteAgentRef`. `status` moves pending ->
 * accepted/declined, or -> revoked from either pending or accepted. */
export type Invite = {
  id: number;
  from_agent: string;
  to_agent: InviteAgentRef;
  status: "pending" | "accepted" | "declined" | "revoked";
  requested_by: string | null;
  decided_by: string | null;
  created_at: string;
  decided_at: string | null;
};

/** One OTHER ready agent implicitly auto-paired with the viewed agent
 * through shared group membership (`kobo_api.main._team_pairs`, access-tab
 * refinement wave) -- read-only, never revocable (there's no invite behind
 * it to revoke). Note the field is `group`, not `group_name` like
 * `InviteAgentRef` -- a different route, a different (already-shipped)
 * server shape, not something this side gets to normalize away. */
export type TeamPair = { name: string; group: string | null };

/** One agent the viewed agent is paired with right now (`GET /api/agents/
 * {name}/pairs`, issue #289): ready, visible to the caller, and allowed to
 * share a thread with it. `via` says why: `group` is the automatic same-team
 * pair, `invite` an accepted request in either direction, `grant` an ask
 * permission approved in Review (issue #310). */
export type PairedAgent = { name: string; group: string | null; via: "group" | "invite" | "grant" };

/** `GET /api/agents/{name}/pairs`'s response. */
export type PairsResult = { pairs: PairedAgent[] };

/** One other READY agent the viewed agent could ask for colleague access
 * (`GET /api/agents/{name}/requestable`, access-tab refinement wave) --
 * capped to exactly these five fields, same card-metadata shape
 * `AgentSummary`/`AgentDetail` already expose, never status/owner/model.
 * `group` (not `group_name`) matches the route's own already-shipped
 * response shape. */
export type RequestableAgent = {
  name: string;
  group: string | null;
  team: string | null;
  capability: string | null;
  purpose_tags: string[];
};

/** One agent the viewed agent already has a pending or accepted invite
 * toward -- surfaced instead of a `RequestableAgent` row so the picker can
 * render a status chip rather than silently omitting it. */
export type AlreadyRequested = { name: string; status: string };

/** `GET /api/agents/{name}/requestable`'s response. */
export type RequestableResult = {
  requestable: RequestableAgent[];
  already: AlreadyRequested[];
  /** Agents that exist but are not ready to be asked, with their state (#273). */
  not_ready?: { name: string; group: string | null; status: string }[];
};

/** One directory match (`GET /api/people/search`, access-tab refinement
 * wave) -- a name plus an email, nothing else. */
export type Person = { name: string; email: string };

/** `GET /api/people/search`'s response -- `unavailable` is present (and
 * true) only when the directory couldn't be searched at all (not
 * configured, or a live failure); a genuine zero-match search reads as a
 * plain `{people: []}`, no `unavailable` key, per the route's own fail-soft
 * contract. */
export type PeopleSearchResult = { people: Person[]; unavailable?: boolean };

/** `GET /api/agents/{name}/invites`'s response: every invite naming this
 * agent, split by which side it's on, plus `team_pairs` -- the OTHER ready
 * agents already auto-paired with it through shared group membership
 * (access-tab refinement wave), rendered as non-revocable rows ahead of the
 * accepted-invite pairs. */
export type InvitesResult = {
  incoming: Invite[];
  outgoing: Invite[];
  team_pairs: TeamPair[];
};

/** How far a per-person grant reaches (issue #309): `view` sees and chats,
 * `manage` also edits the agent as its owner would, short of destroying it. */
export type GrantRole = "view" | "manage";

/** One per-person grant (`GET/POST /api/agents/{name}/grants`) --
 * `added_by` is the verified email that created the grant, `null` for one
 * added while IAP was off. */
export type Grant = { email: string; role: GrantRole; added_by: string | null };

export type ChatMessage = { role: "user" | "agent"; text: string };

/** One line of the terminal's flattened colleague feed (`GET /api/
 * colleagues/feed`, terminal redesign) — the operator's ruling replaced the
 * old per-conversation card grid ("truncated text") with "a command line
 * terminal... showed all the conversations... in order... and the text in
 * full." `role` is `"ask"` (the caller's message; `from` is the caller's
 * display name, `to` the target) or `"reply"` (the target's answer; `from`/
 * `to` reversed) — the direction reversal is what lets the terminal color a
 * line by outgoing vs incoming. `text` is always the FULL, untruncated
 * message (never a snippet). `conversation_id` groups an ask with its own
 * reply; `ts` is the ISO-8601 timestamp the terminal renders as `HH:MM:SS`. */
export type ColleagueFeedEntry = {
  ts: string;
  conversation_id: string;
  from: string;
  to: string;
  role: "ask" | "reply";
  text: string;
};

/** `GET /api/colleagues/feed`'s response: every entry across every
 * conversation `agent` participates in, sorted ascending by `ts` (so the
 * terminal renders it top-to-bottom, oldest first, same reading order as a
 * real shell scrollback) and capped to the most recent 200 -- `truncated`
 * is true whenever more existed than that. */
export type ColleagueFeed = {
  entries: ColleagueFeedEntry[];
  truncated: boolean;
};

export class ApiError extends Error {
  status: number;
  /** The parsed JSON error body, when the response had one (`undefined` for
   * a non-JSON body). `.message` above already extracts `.error` from it
   * for the common case; this is for a route that puts something ELSE
   * alongside `error` a caller needs (team-guide spec 2026-09-14: the
   * duplicate-guide 409's `existing` agent name, for "Open {name}"). */
  body?: unknown;

  constructor(message: string, status: number, body?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

/**
 * Human-readable message for a thrown error — `error.message` when there is
 * one, else `fallback`. Shared by every page/component that renders a failed
 * query or mutation (Dashboard, AgentDetail, CreateModal) so the copy and
 * the cleanup below are applied uniformly instead of each callsite rolling
 * its own.
 *
 * Quoted tokens in the message have their surrounding quote marks stripped
 * for display — a backend message quoting a name as `'foo-bar'` (or
 * `"foo-bar"`) reads as `foo-bar`. This is token-scoped (a matched pair of
 * quotes, not every quote character): the single-quote pass only opens a
 * token at an apostrophe that isn't preceded by a letter, so a contraction
 * like "isn't" can't be mistaken for the start of a token — without that
 * guard, a message containing both a contraction and a quoted token (e.g.
 * `"that name isn't available: 'foo'"`) would pair the contraction's
 * apostrophe with the token's opening quote, mangling the contraction and
 * leaving a stray quote behind.
 */
export function errorMessage(error: unknown, fallback: string): string {
  const message = error instanceof Error ? error.message : fallback;
  return message
    .replace(/(?<![A-Za-z])'([^']*)'/g, "$1")
    .replace(/"([^"]*)"/g, "$1");
}

/** Extract a `.error` string from a parsed JSON body, if it has one. */
function errorMessageFrom(data: unknown): string | null {
  if (data !== null && typeof data === "object" && "error" in data) {
    const message = (data as { error: unknown }).error;
    if (typeof message === "string") {
      return message;
    }
  }
  return null;
}

/**
 * Read a fetch Response's body as text, then attempt to parse it as JSON.
 * Throws ApiError (with the response's status) whenever:
 *  - the status is not ok and the body has a JSON `{"error": "..."}` message
 *    (that message is used), or
 *  - the status is not ok and the body isn't JSON at all (proxy error pages,
 *    plain-text 5xx bodies) — falls back to the status line, or
 *  - the status is ok but the body still isn't valid JSON (unexpected, but
 *    surfaced clearly rather than as a raw SyntaxError).
 */
async function parse<T>(res: Response): Promise<T> {
  const text = await res.text();

  let data: unknown = undefined;
  let parseFailed = false;
  if (text.length > 0) {
    try {
      data = JSON.parse(text);
    } catch {
      parseFailed = true;
    }
  }

  if (!res.ok) {
    const message = parseFailed
      ? `${res.status} ${res.statusText}`.trim()
      : (errorMessageFrom(data) ?? `${res.status} ${res.statusText}`.trim());
    throw new ApiError(message, res.status, parseFailed ? undefined : data);
  }

  if (parseFailed) {
    throw new ApiError(
      `expected a JSON response, got: ${text.slice(0, 200)}`,
      res.status,
    );
  }

  return data as T;
}

/** `GET /api/agents`'s response (access-control Task B2): the CALLER's
 * visible agents (server-filtered, never the full table — the UI only
 * ever reflects `_visible_agents`' own decision), plus whether group-
 * derived visibility is degraded right now for them. `groups_unavailable`
 * true means a directory outage narrowed this list down to only what the
 * caller owns — the dashboard's banner (Dashboard.svelte) reads this
 * verbatim, never derives it from anything else. */
export type AgentListResult = { agents: AgentSummary[]; groups_unavailable: boolean };

export async function listAgents(): Promise<AgentListResult> {
  const res = await fetch("/api/agents");
  return parse<AgentListResult>(res);
}

export async function getAgent(name: string): Promise<AgentDetail> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}`);
  return parse<AgentDetail>(res);
}

export async function createAgent(body: {
  name: string;
  persona: string;
  /** The creating team — a slug, server-validated against `TEAM_SLUG_RE`,
   * rejected with the plain copy "that doesn't look like a team name" on
   * mismatch. Optional: the "team vs group" ruling retired the create
   * modal's own team picker (team is a derived marketplace fact, edited
   * later through Edit details, not a create-time choice), so this key is
   * never sent from the UI any more; the server's own default ("shared")
   * covers its absence exactly as it did before the picker briefly
   * existed. */
  team?: string;
  skills: { id: string; team: string }[];
  model: string | null;
  /** Purpose chips (persona-metadata plan) — normalized/capped (8)
   * server-side; an over-cap list (after de-dupe) 422s with "Eight tags at
   * most." */
  purpose_tags?: string[];
  /** The ACL namespace this agent belongs to (access-control Task B2,
   * always includes the commons as of the shared-group + group-requests
   * wave) — omitted (or `null`) only in the defensive case where
   * `GET /api/catalog/groups` came back `[]`; required and checked against
   * that same list once it has anything at all (the commons entry alone is
   * enough), both server-side (the real gate) and by the create modal's own
   * required `<select>` (UX only). */
  group_name?: string | null;
  /** Archetype create lane (team-guide spec 2026-09-14): set to
   * `"team_guide"` to deploy a team's guide instead of an ordinary agent —
   * the server then forces persona/tools/tags/visibility/skills and
   * ignores this request's own values for them (`persona` above is still
   * required by the request model, but ignored on this path; the UI sends
   * `"helpful-generalist"`). Omitted for every ordinary create. */
  archetype?: string;
  /** Optional style overlay for a `"team_guide"` archetype (style-not-
   * identity — see `kobo_api/archetypes.py`): "name a famous person,
   * artist, or regional character your guide should sound like." Ignored
   * outside the archetype path. Server rejects over 120 chars. */
  voice?: string;
}): Promise<{ name: string; status: string }> {
  const res = await fetch("/api/agents", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return parse<{ name: string; status: string }>(res);
}

/** One thing the scan found, as `kobo_api/connect_scan.py` serves it: a
 * `code` for this client to key on (the only one it does key on today is
 * `headless_unavailable`, which disables the second run-mode option) and a
 * `message` a person reads. The message is the server's own words, shown
 * verbatim -- the same sentence arrives in a scan report and in the connect
 * route's own refusal, so it is never reworded here. */
export type ScanFinding = { code: string; message: string };

/** `POST /api/connect/scan`'s response. `blockers` empty means Connect may
 * go ahead; `warnings` are true things about the repository its owner may
 * still choose. `adds` is everything the room will ever write into the
 * repository, and `adds_preamble` the sentence about the one thing the room
 * cannot check (who owns it). `repo` is GitHub's own spelling once it has
 * answered, so a repository named in one case cannot become two rows. */
export type ScanReport = {
  blockers: ScanFinding[];
  warnings: ScanFinding[];
  adds: string[];
  adds_preamble: string;
  repo: string;
  /** The name the repository already deploys under (its base Deployment's
   * own name), or null when it has none. A connected repository keeps its
   * own name: the form takes this as the Name and locks the field, and the
   * scan lists any other name among the blockers. */
  repo_name: string | null;
  repo_service_account: string | null;
  /** How the agent runs, decided by the scan from the repository: "http"
   * when it listens on a port, "headless" when it does not. */
  run_mode: string;
};

/** The three fields the scan reads: the repository, the name it checks
 * against the room, and how the agent runs. `group_name` rides along
 * because the scan reads private repositories with the room's own
 * credentials and is gated by the same group check the connect is. */
export type ScanRepoBody = {
  repo: string;
  name: string;
  group_name?: string | null;
};

/** The connect body: the scan's fields plus the names of Kubernetes
 * Secrets the owner will create in the agent's namespace (names only, at
 * most 8 -- the room holds no connected agent's secrets), plus the
 * caller's word that they may connect this repository. */
export type ConnectAgentBody = ScanRepoBody & {
  secret_names: string[];
  confirm_repo_owner: boolean;
};

/** One row in the Repository field's typeahead: a repository the room's
 * GitHub App can already reach. `private` is shown as a one-word marker so
 * a person knows which of two similarly named repositories they are
 * pointing at. The name and that marker are the whole row, and the whole
 * response: the connect route reads GitHub's own answer for everything
 * else rather than a browser's copy of it. */
export type ConnectRepoOption = {
  full_name: string;
  private: boolean;
};

/** `GET /api/connect/repos`'s answer: the rows to show, whether more
 * matched than came back, and how many repositories the room reaches in
 * all. `total` is on every answer and is what the field's footer says out
 * loud; it counts what could be connected, so nothing archived is in it. */
export type ConnectRepoMatches = {
  repos: ConnectRepoOption[];
  truncated: boolean;
  total: number;
};

/** `GET /api/connect/repos`: the repositories the room's GitHub App
 * installation can reach whose name starts with (or contains) `q`. An
 * empty `q` is answered too, with the few pushed to most recently: a
 * person who has not typed yet still needs to see that the room can
 * already reach their work. */
export async function searchConnectRepos(q: string): Promise<ConnectRepoMatches> {
  const res = await fetch(`/api/connect/repos?q=${encodeURIComponent(q)}`);
  return parse<ConnectRepoMatches>(res);
}

export async function scanRepo(body: ScanRepoBody): Promise<ScanReport> {
  const res = await fetch("/api/connect/scan", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return parse<ScanReport>(res);
}

/**
 * `POST /api/agents/connect`: takes on a repository a team already owns.
 * 201 `{name, status}` on success. The refusals a caller has to tell apart
 * all arrive as an `ApiError` through `parse`: 422 carries `blockers`
 * (a `ScanFinding[]`, on `ApiError.body`) when the server's own re-scan
 * found something the browser's report did not, 409 means the name was
 * taken between the scan and now, 429 means too many scans, 502 means
 * GitHub could not be read. Every one of them carries the server's `error`
 * sentence, which is what the form shows.
 */
export async function connectAgent(
  body: ConnectAgentBody,
): Promise<{ name: string; status: string }> {
  const res = await fetch("/api/agents/connect", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return parse<{ name: string; status: string }>(res);
}

/**
 * `POST /api/agents/{name}/convert-to-connected`: says out loud that the
 * room no longer edits an agent it once composed. Operator-only (403
 * otherwise), and only for a ready or failed agent that is not already
 * connected (409 otherwise); 422 when the run mode is one the room does not
 * have. Every refusal arrives as an `ApiError` through `parse`, carrying
 * the server's own sentence, which is what the page shows.
 */
export async function convertToConnected(
  name: string,
  runMode: string,
): Promise<{ name: string; kind: string; run_mode: string }> {
  const res = await fetch(
    `/api/agents/${encodeURIComponent(name)}/convert-to-connected`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run_mode: runMode }),
    },
  );
  return parse<{ name: string; kind: string; run_mode: string }>(res);
}

export async function retryAgent(name: string): Promise<void> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/retry`, {
    method: "POST",
  });
  await parse(res);
}

/**
 * `DELETE /api/agents/{name}`: starts (or resumes) the four-step destroy
 * ledger. 202 `{"status": "destroying"}` on success; safe to call again
 * while a prior destroy is still running or has failed a step ("Destroy
 * again" is the same call, resume is entirely server-side). The server
 * rejects with 403 (non-operator), 404 (unknown agent), or 409 (the agent
 * is still being created), all surfaced as an `ApiError` through `parse`,
 * same as every other mutation in this file.
 */
export async function destroyAgent(name: string): Promise<{ status: string }> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
  return parse<{ status: string }>(res);
}

/** `GET /api/catalog/models` — the one catalog endpoint that survived the
 * move to search: skills and personas are now `searchSkills`/`searchPersonas`
 * below, but the model list is small and fixed, so it's still fetched
 * whole. */
export async function fetchModels(): Promise<{
  models: { id: string; label: string; family?: string | null; tier?: string | null }[];
  default: string;
}> {
  const res = await fetch("/api/catalog/models");
  return parse<{
    models: { id: string; label: string; family?: string | null; tier?: string | null }[];
    default: string;
  }>(
    res,
  );
}

/** `GET /api/guide`: the room's own user guide, every section with its
 * markdown body, in reading order. One packaged source serves this page and
 * the `kobo_guide` MCP tool, so the two never drift. */
export type GuideSection = { id: string; title: string; summary: string; body: string };

export async function fetchGuide(): Promise<{ sections: GuideSection[] }> {
  const res = await fetch("/api/guide");
  return parse<{ sections: GuideSection[] }>(res);
}

/** `GET /api/catalog/observability` (Task 4 of the observability-catalog
 * addendum) -- the org's dashboard base URL + project, both `null` unless
 * the room config carries both keys. A caller builds one agent's dashboard
 * link itself (`${url}/projects/${project}/dashboards/${agent.name}-agent-
 * health`, the fixed URL shape the addendum specifies) only when both come
 * back non-null; either alone isn't enough to construct a working link. */
export async function fetchObservability(): Promise<{
  url: string | null;
  project: string | null;
}> {
  const res = await fetch("/api/catalog/observability");
  return parse<{ url: string | null; project: string | null }>(res);
}

/** `GET /api/catalog/groups` (access-control Task B2, sectioned by the
 * shared-group + group-requests wave, extended by the create-modal
 * non-engineering-wing ruling) -- the room's named ACL namespaces, config
 * names only (never the Okta group names the server maps them to
 * internally). `kind` is `"shared"` for the one commons entry (always
 * first, always present, even when the room has named no groups at all --
 * usable by every signed-in caller regardless of directory config),
 * `"engineering"` for one of the room's own configured groups, or
 * `"non-engineering"` for a configured group that renders in the dropdown's
 * separate non-engineering wing instead (`GroupChoice.kind` on the server).
 * The create modal's group `<select>` sections on this field rather than
 * hardcoding the commons entry's name client-side. `[]` is still possible
 * in a malformed/stubbed response; the create modal treats that
 * defensively as "no picker at all," same as before this wave. */
export type GroupOption = {
  name: string;
  kind: "shared" | "engineering" | "non-engineering";
};

export async function fetchGroups(): Promise<GroupOption[]> {
  const res = await fetch("/api/catalog/groups");
  const data = await parse<{ groups: GroupOption[] }>(res);
  return data.groups;
}

/** One row of the group-requests queue (shared-group + group-requests wave)
 * — the dropdown's escape hatch for "my team's group isn't listed," and the
 * admin card's (`GroupRequestsCard.svelte`) own list shape. `requested_by`/
 * `decided_by` are verified emails, `null` when IAP was off at the time.
 * `note` is `null` when the requester left it blank. Mirrors
 * `kobo_api.main._group_request_repr` verbatim. */
export type GroupRequest = {
  id: number;
  group_name: string;
  requested_by: string | null;
  note: string | null;
  status: "pending" | "done" | "declined";
  decided_by: string | null;
  created_at: string;
  decided_at: string | null;
};

/**
 * `POST /api/group-requests` — any signed-in caller may file one (no
 * owner/operator gate beyond the app-wide IAP middleware). `note` is only
 * sent when non-empty; the server's own field is optional and defaults to
 * `None`. 422 on an empty/over-100-char name, 409 when an equivalent
 * (case-insensitive, trimmed) request is already pending — the create
 * modal's mini-form (CreateModal.svelte) treats that status specifically as
 * "Already requested," not a generic error.
 */
export async function requestGroup(
  groupName: string,
  note?: string,
): Promise<GroupRequest> {
  const res = await fetch("/api/group-requests", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(
      note ? { group_name: groupName, note } : { group_name: groupName },
    ),
  });
  return parse<GroupRequest>(res);
}

/** `GET /api/group-requests` — admin only server-side (`_is_admin`,
 * no dev-parity "unconfigured room" escape hatch — see the API's own doc
 * comment on `_group_requests_admin_block`). Newest first, already sorted
 * server-side. */
export async function fetchGroupRequests(): Promise<GroupRequest[]> {
  const res = await fetch("/api/group-requests");
  const data = await parse<{ requests: GroupRequest[] }>(res);
  return data.requests;
}

/**
 * `POST /api/group-requests/{id}/done` or `/declined` — admin only,
 * same gate as `fetchGroupRequests`. "done" only closes kobo's own loop; the
 * admin is expected to have already added the group to org config out of
 * band (the route never touches org config itself). 404 for an unknown id,
 * 409 if the request isn't currently pending.
 */
export async function decideGroupRequest(
  id: number,
  action: "done" | "declined",
): Promise<GroupRequest> {
  const res = await fetch(`/api/group-requests/${encodeURIComponent(String(id))}/${action}`, {
    method: "POST",
  });
  return parse<GroupRequest>(res);
}

/** Builds a `?q=...&team=...` query string: `q` is always present (even
 * empty), `team` is omitted entirely when null. Shared by the two catalog
 * search endpoints below. */
function searchQueryString(q: string, team: string | null): string {
  const params = new URLSearchParams();
  params.set("q", q);
  if (team !== null) {
    params.set("team", team);
  }
  return params.toString();
}

export async function searchSkills(
  q: string,
  team: string | null,
): Promise<CatalogEntry[]> {
  const res = await fetch(`/api/skills/search?${searchQueryString(q, team)}`);
  const data = await parse<{ results: CatalogEntry[] }>(res);
  return data.results;
}

/** Which personas a search covers (persona team scope): `"mine"` is the
 * shared ones plus those filed under the caller's own groups (an operator
 * sees all), `"all"` is the whole catalog. A filter, not access control. */
export type PersonaScope = "mine" | "all";

export async function searchPersonas(
  q: string,
  team: string | null,
  scope: PersonaScope = "mine",
): Promise<CatalogEntry[]> {
  const params = new URLSearchParams(searchQueryString(q, team));
  params.set("scope", scope);
  const res = await fetch(`/api/personas/search?${params.toString()}`);
  const data = await parse<{ results: CatalogEntry[] }>(res);
  return data.results;
}

/** One whole persona (`GET /api/personas/{id}`) — the catalog entry a
 * search result already carries, plus the stored persona.md read back
 * apart server-side (`kobo_api.personas.parse_persona_body`). What the
 * hover card in the create dialog's persona list shows.
 *
 * `what_they_do` is cut at 4000 characters, with `truncated` saying so —
 * the card fades after about a dozen lines anyway and links to the full
 * persona. A packaged built-in has no sections in its file, so both prose
 * fields come back as the catalog's one-line description instead, and
 * `rules` is empty. `created_by`/`created_at` are null when the stored
 * object carries no metadata to read them from. */
export type PersonaDetail = {
  id: string;
  name: string;
  team: string;
  builtin: boolean;
  tags: string[];
  who_they_are: string;
  what_they_do: string;
  rules: string[];
  created_by: string | null;
  created_at: string | null;
  truncated: boolean;
};

/** `GET /api/personas/{id}` — see `PersonaDetail`. Ungated, same as
 * `searchPersonas`; an archived or unknown id answers 404. */
export async function fetchPersona(id: string): Promise<PersonaDetail> {
  const res = await fetch(`/api/personas/${encodeURIComponent(id)}`);
  return parse<PersonaDetail>(res);
}

/** One archived persona (`GET /api/personas/archived`, hardening wave archive
 * lane) — the same raw index-entry shape `searchPersonas` already projects
 * into `CatalogEntry`, plus `archived: true`. Archived is presence-based
 * server-side (`kobo_api.personas.set_persona_archived`): an active entry
 * never carries the key at all, so this listing only ever carries `true`. */
export type ArchivedPersona = CatalogEntry & { archived: true };

/**
 * `GET /api/personas/archived` — every archived persona, ungated (deliberate
 * parity with `searchPersonas` above, likewise readable by any signed-in
 * caller regardless of team membership — see `kobo_api.main.
 * archived_personas_route`'s own doc comment). The active `searchPersonas`
 * list already excludes anything archived at ingest time, so this is the
 * only place an archived persona is visible at all — the "Show archived"
 * toggle's own list.
 */
export async function archivedPersonas(): Promise<ArchivedPersona[]> {
  const res = await fetch("/api/personas/archived");
  const data = await parse<{ personas: ArchivedPersona[] }>(res);
  return data.personas;
}

/**
 * `POST /api/personas/{id}/archive` — archives one persona (hardening wave:
 * archive, not delete — the persona.md object is never touched, and
 * unarchive restores full standing). The gate is server-enforced
 * (`kobo_api.main._persona_archive_gate`): builtin or team "shared" is
 * operator-only; any other team takes a member of that team, or an
 * operator. A refusal surfaces as a 403 `{error}` naming the requirement,
 * through `parse` same as every other mutation here; 404 for an unknown id,
 * 502/503 on storage or an unwired catalog.
 */
export async function archivePersona(id: string): Promise<ArchivedPersona> {
  const res = await fetch(`/api/personas/${encodeURIComponent(id)}/archive`, {
    method: "POST",
  });
  return parse<ArchivedPersona>(res);
}

/** `POST /api/personas/{id}/unarchive` — the reverse of `archivePersona`,
 * same gate. The returned entry no longer carries `archived` at all
 * (presence-based flag, popped server-side on unarchive rather than set to
 * false). */
export async function unarchivePersona(id: string): Promise<CatalogEntry> {
  const res = await fetch(`/api/personas/${encodeURIComponent(id)}/unarchive`, {
    method: "POST",
  });
  return parse<CatalogEntry>(res);
}

/** `PATCH /api/personas/{id}` — re-files one persona under `team`. Two
 * server gates: the caller may move it (its maker, a member of its current
 * team, or an operator; built-ins never move), and may file it there (the
 * create route's own team gate). A refusal is a 403 `{error}` naming which,
 * through `parse`. Only the team changes. */
export async function movePersona(id: string, team: string): Promise<CatalogEntry> {
  const res = await fetch(`/api/personas/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ team }),
  });
  return parse<CatalogEntry>(res);
}

/** `GET /api/personas/movable` — the ids of the personas this caller may
 * re-file, worked out server-side so the persona sheet never copies the
 * rule. */
export async function movablePersonas(): Promise<string[]> {
  const res = await fetch("/api/personas/movable");
  const data = await parse<{ ids: string[] }>(res);
  return data.ids;
}

export async function fetchTeams(): Promise<string[]> {
  const res = await fetch("/api/catalog/teams");
  const data = await parse<{ teams: string[] }>(res);
  return data.teams;
}

/** The distinct skill-owner teams plus the server's own default
 * (`GET /api/teams`, persona-metadata plan Task 1) — a DIFFERENT endpoint
 * from `fetchTeams`/`/api/catalog/teams` above, which spans every catalog
 * kind (skills and personas) for filtering a catalog search. This one is
 * skill-owner-only, meant for "which team does this agent belong to": the
 * Edit details modal reads it (the create form stopped asking; team
 * defaults server-side and derives from the agent's skills). A
 * newly-published owner appears here without a room change, same
 * TTL-refresh hook every other catalog route shares. */
export type TeamOptions = { teams: string[]; default: string };

export async function fetchTeamOptions(): Promise<TeamOptions> {
  const res = await fetch("/api/teams");
  return parse<TeamOptions>(res);
}

/**
 * `POST /api/personas/draft` — turns one plain job description (plus
 * optional short "always/never" rule hints, a prompt hint only — never
 * stored) into a drafted name/who_they_are/what_they_do for the v2 preview
 * to edit. Always 200 from the server; nothing is created or searched
 * here — `createPersona` below (untouched by v3) is still the only call
 * that actually writes anything.
 */
export async function draftPersona(
  job: string,
  rules: string[],
): Promise<PersonaDraft> {
  const res = await fetch("/api/personas/draft", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ job, rules }),
  });
  return parse<PersonaDraft>(res);
}

/** The persona-first station's other entry point (persona-first station
 * addendum): given the operator's own ready-made persona document
 * (`POST /api/personas/distill`), the room reads it and pulls out discovery
 * metadata for the colleague directory. Deliberately no `what_they_do` -
 * unlike `PersonaDraft` above, the submitted document already IS the
 * persona body, so there's nothing to draft for that field; the caller
 * (`PersonaBuilder`) keeps its own `whatTheyDo` state untouched by this
 * response. Same "always 200, a generator failure just means a
 * deterministic fallback" contract as `draftPersona` — `distillPersona`
 * below only ever rejects on a genuine transport/route failure. */
export type PersonaDistillResult = {
  name: string;
  who_they_are: string;
  /** Distilled purpose chips, ≤5, lowercased/deduped server-side
   * (`kobo_api.persona_draft.finalize_distill`) — `[]` when the generator
   * omitted them or was unusable (a distill has no what_they_do to derive
   * a fallback tag set from the way `draftPersona`'s own fallback tier
   * does, so this is a plain empty list on any trouble, full stop). */
  purpose_tags: string[];
};

/**
 * `POST /api/personas/distill` — reads a persona document the operator
 * already wrote (pasted or uploaded) and pulls out discovery metadata:
 * name, one-line who_they_are, purpose tags. Always 200 from the server;
 * nothing is created or searched here, same as `draftPersona`.
 */
export async function distillPersona(body: string): Promise<PersonaDistillResult> {
  const res = await fetch("/api/personas/distill", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ body }),
  });
  return parse<PersonaDistillResult>(res);
}

export async function createPersona(body: PersonaForm): Promise<CatalogEntry> {
  const res = await fetch("/api/personas", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return parse<CatalogEntry>(res);
}

/** `POST /api/agents/name-suggestions`'s body (create-identity, Task 3's
 * route contract). `personaId`, when it resolves against the persona
 * catalog, makes the SERVER pull that persona's own real body (truncated)
 * and purpose tags in place of `whatTheyDo` -- an unresolved/absent id
 * falls back to `whatTheyDo` as submitted, so that field is always sent and
 * always non-empty regardless of which lane supplied it. `exclude` is every
 * name already offered this session (capped at 20 client-side, see
 * CreateModal's `offeredNames`); `round` is a monotonically increasing
 * "offer me more" counter that salts the server's fallback ordering. */
export type NameSuggestionsRequest = {
  whoTheyAre: string;
  whatTheyDo: string;
  personaId?: string | null;
  exclude?: string[];
  round?: number;
};

/**
 * `POST /api/agents/name-suggestions` — three name+reason candidates for
 * the agent being created. The server never fails to return three (a
 * curated fallback pool stands in when its generator errors out), so this
 * only ever rejects on a genuine transport/route failure. Wired as a query
 * factory (queries.ts's `nameSuggestionsQuery`), not a mutation, even
 * though the transport is POST — nothing is created or changed by calling
 * it, the free-text fields just don't fit a query string.
 */
export async function fetchNameSuggestions(
  req: NameSuggestionsRequest,
): Promise<NameSuggestion[]> {
  const res = await fetch("/api/agents/name-suggestions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      who_they_are: req.whoTheyAre,
      what_they_do: req.whatTheyDo,
      persona_id: req.personaId ?? null,
      exclude: req.exclude ?? [],
      round: req.round ?? 0,
    }),
  });
  const data = await parse<{ suggestions: NameSuggestion[] }>(res);
  return data.suggestions;
}

export async function fetchOrgStatus(): Promise<OrgStatus> {
  const res = await fetch("/api/org/status");
  return parse<OrgStatus>(res);
}

/** `GET /api/whoami` — the signed-in identity this room's server has
 * already verified (or `{email: null, ...}` when IAP isn't configured). */
/** One Add-ons card (admin panel): what the room runs on and whether it is
 * on, derived server-side from the org keys that turn it on. */
export type AdminAddon = {
  id: string;
  name: string;
  summary: string;
  state: "on" | "off" | "error";
  detail: string;
  keys: string[];
  owner: string;
};

/** One integration: a vendor the room holds a base credential for, with how
 * far its projections (the vault, the gateway) have caught up. */
export type AdminIntegration = {
  id: string;
  vendor: string;
  name: string;
  base_access: "read" | "write";
  state: "pending" | "applied" | "failed";
  state_detail: string | null;
  credential_ref: string | null;
  spec: { address?: string; trust?: string; pull_request?: string; source?: "access"; ceiling?: string[]; kind?: "org" | "account"; parent?: string; account_id?: string };
  created_by: string;
  created_at: string;
  updated_at: string;
};

export type AdminConnectIntegration = {
  vendor: string;
  name: string;
  auth?: "token" | "api_keys" | "aws_role" | "aws_hub" | "aws_spoke" | "gcp_identity" | "azure_federated" | "datadog_service_account" | "github_app";
  identity?: Record<string, string>;
  secrets?: Record<string, string>;
  base_access: "read" | "write";
  credential?: string;
  address: string;
  trust: "trusted" | "untrusted";
};

export async function fetchAdminIntegrations(): Promise<AdminIntegration[]> {
  const res = await fetch("/api/admin/integrations");
  const body = await parse<{ integrations: AdminIntegration[] }>(res);
  return body.integrations;
}

export async function connectAdminIntegration(input: AdminConnectIntegration): Promise<AdminIntegration> {
  const res = await fetch("/api/admin/integrations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  return parse<AdminIntegration>(res);
}

/** The Connect AWS page: who the gateway is, which packs exist, and the role
 * rendered three ways from the cloud-access release the room ships. */
export type AdminAwsSetup = {
  version: string;
  /** `role`: one flat role the room assumes; `org`: an organization's hub role in its
   * management account; `account`: a member account's role that the hub assumes. */
  mode: "role" | "org" | "account";
  role_name: string;
  audience: string;
  /** account mode: the organization and the hub the account's role must trust. */
  parent?: string;
  account_id?: string;
  hub_role_arn?: string;
  external_id?: string;
  /** org mode: what the hub may do, for an owner creating it by hand. */
  permissions_policy?: string;
  /** org mode (#275): the member accounts' half. One external id for the whole
   * organization, one spoke template, deployable everywhere at once. */
  spoke_role_name?: string;
  spoke_terraform?: string;
  spoke_trust_policy?: string;
  stackset?: string;
  /** Who the role must trust: the permission service (`access`) when it is on, else the gateway. */
  principal: "access" | "gateway";
  identity: { issuer: string; subject: string; email: string | null; audience: string } | null;
  identity_note: string | null;
  packs: { id: string; purpose: string; required: boolean; grants: string[] }[];
  chosen: string[];
  terraform: string;
  cloudformation: { template: string; parameters: Record<string, string> };
  trust_policy: string;
  /** The room can ask the gateway to try the role before saving it. */
  checked: boolean;
};

export type AwsSetupRequest = {
  packs: string[];
  mode?: "role" | "org" | "account";
  integration?: string;
  parent?: string;
  account_id?: string;
};

export async function fetchAdminAwsSetup(input: AwsSetupRequest): Promise<AdminAwsSetup> {
  const q = new URLSearchParams({ packs: input.packs.join(","), mode: input.mode ?? "role" });
  if (input.integration) q.set("integration", input.integration);
  if (input.parent) q.set("parent", input.parent);
  if (input.account_id) q.set("account_id", input.account_id);
  const res = await fetch(`/api/admin/integrations/aws/setup?${q.toString()}`);
  return parse<AdminAwsSetup>(res);
}

/** One account of an AWS organization as its hub sees it, and whether it is connected. */
export type OrgAccount = {
  id: string;
  name: string | null;
  email: string | null;
  status: string | null;
  integration: string | null;
  connected: boolean;
};

/** The outcome of checking an organization's accounts through its hub (#275):
 * the ones connected by this check, the ones already connected, and the ones
 * without the spoke role yet. */
export type OrgAccountsCheck = {
  organization: string;
  connected: { id: string; name: string; integration: string | null; reason: string | null }[];
  already: { id: string; name: string; integration: string | null; reason: string | null }[];
  without_role: { id: string; name: string; integration: string | null; reason: string | null }[];
};

export async function checkOrgAccounts(integrationId: string): Promise<OrgAccountsCheck> {
  const res = await fetch(`/api/admin/integrations/${encodeURIComponent(integrationId)}/accounts/check`, { method: "POST" });
  return parse<OrgAccountsCheck>(res);
}

export async function fetchOrgAccounts(integrationId: string): Promise<OrgAccount[]> {
  const res = await fetch(`/api/admin/integrations/${encodeURIComponent(integrationId)}/accounts`);
  const body = await parse<{ accounts: OrgAccount[] }>(res);
  return body.accounts;
}

export async function disconnectAdminIntegration(id: string): Promise<{ id: string; secret: string; gateway: string }> {
  const res = await fetch(`/api/admin/integrations/${encodeURIComponent(id)}`, { method: "DELETE" });
  return parse<{ id: string; secret: string; gateway: string }>(res);
}

/** Room services (#284): the room's own Slack config tokens and the GitHub
 * app for pull requests. Status only; a value is never sent back. */
export type RoomServiceId = "slack" | "github";
export type RoomServiceStatus = {
  configured: boolean;
  source: "room" | "deployment" | "none";
  updated_at: string | null;
  updated_by: string | null;
};
export type RoomServices = {
  can_store: boolean;
  note: string | null;
  services: Record<RoomServiceId, RoomServiceStatus>;
};
export type RoomServiceValues = Partial<Record<"config_token" | "refresh_token" | "app_id" | "installation_id" | "private_key", string>>;

export async function fetchRoomServices(): Promise<RoomServices> {
  const res = await fetch("/api/admin/room-services");
  return parse<RoomServices>(res);
}

export async function putRoomService(service: RoomServiceId, values: RoomServiceValues): Promise<RoomServices> {
  const res = await fetch(`/api/admin/room-services/${service}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(values),
  });
  return parse<RoomServices>(res);
}

export async function removeRoomService(service: RoomServiceId): Promise<RoomServices> {
  const res = await fetch(`/api/admin/room-services/${service}`, { method: "DELETE" });
  return parse<RoomServices>(res);
}

/** A role as the server stores it, plus the catalogue the editor offers. */
export type AdminRole = {
  id: string;
  name: string;
  description: string;
  builtin: boolean;
  groups: string[];
  capabilities: string[];
  admin: string[];
  connectors: { plugin: string; ceiling: "none" | "read" | "write" | "approve" }[];
  models: string[];
  updated_at: string;
};

export type AdminRoleCatalogue = {
  capabilities: { id: string; label: string; desc: string }[];
  admin: { id: string; label: string; desc: string }[];
  models: { id: string; label: string }[];
  groups: string[];
  connectors: { id: string; name: string; base_access: "read" | "write" }[];
};

export type AdminRoleBody = Omit<AdminRole, "id" | "builtin" | "updated_at">;

export async function fetchAdminRoles(): Promise<{ roles: AdminRole[]; catalogue: AdminRoleCatalogue }> {
  const res = await fetch("/api/admin/roles");
  return parse<{ roles: AdminRole[]; catalogue: AdminRoleCatalogue }>(res);
}

export async function putAdminRole(id: string, body: AdminRoleBody): Promise<AdminRole> {
  const res = await fetch(`/api/admin/roles/${encodeURIComponent(id)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return parse<AdminRole>(res);
}

export async function deleteAdminRole(id: string): Promise<{ deleted: string }> {
  const res = await fetch(`/api/admin/roles/${encodeURIComponent(id)}`, { method: "DELETE" });
  return parse<{ deleted: string }>(res);
}

export async function reloadAdminOrg(): Promise<{ wired: boolean; detail: string; models: string[]; note: string }> {
  const res = await fetch("/api/admin/org/reload", { method: "POST" });
  return parse<{ wired: boolean; detail: string; models: string[]; note: string }>(res);
}

/** One agent's row on the Connections pane: its lanes to integrations. */
export type AdminLane = {
  id: number;
  agent: string;
  integration: string;
  access: "read" | "write";
  state: "pending" | "applied" | "failed" | "revoked";
  state_detail: string | null;
  spec: { pull_request?: string };
  requested_by: string;
  decided_by: string | null;
  created_at: string;
  updated_at: string;
};

export type AdminConnections = {
  agents: { agent: string; group: string | null; lanes: AdminLane[] }[];
  integrations: AdminIntegration[];
  /** The gateway's admin plane is wired: a lane applies at once. */
  live: boolean;
  gateway_repo: boolean;
};

export async function fetchAdminConnections(): Promise<AdminConnections> {
  const res = await fetch("/api/admin/connections");
  return parse<AdminConnections>(res);
}

export async function putAdminLane(agent: string, integration: string, access: "read" | "write"): Promise<AdminLane & { outcome: Record<string, string> }> {
  const res = await fetch(`/api/admin/connections/${encodeURIComponent(agent)}/${encodeURIComponent(integration)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ access }),
  });
  return parse<AdminLane & { outcome: Record<string, string> }>(res);
}

export async function revokeAdminLane(agent: string, integration: string): Promise<AdminLane & { outcome: Record<string, string> }> {
  const res = await fetch(`/api/admin/connections/${encodeURIComponent(agent)}/${encodeURIComponent(integration)}`, { method: "DELETE" });
  return parse<AdminLane & { outcome: Record<string, string> }>(res);
}

export type AdminPeople = {
  admin_groups: string[];
  operators: string[];
  reader_groups: string[];
  slack_operators: string[];
};

export async function fetchAdminPeople(): Promise<AdminPeople> {
  const res = await fetch("/api/admin/people");
  return parse<AdminPeople>(res);
}

/** One row of the room's audit log (issue #316): who did what to which
 * target, in one sentence, with where the request came from and how far
 * the action has landed where it projects. */
export type AuditRow = {
  id: number;
  at: string;
  actor: string;
  actor_kind: "person" | "room" | "agent";
  action: string;
  target_kind: string;
  target_id: string;
  agent_name: string | null;
  summary: string;
  outcome: "ok" | "failed" | "refused";
  source: string;
  request_ip: string | null;
  user_agent: string | null;
  detail: Record<string, unknown> | null;
  landed: "pending" | "applied" | "failed" | null;
  landed_detail: string | null;
};

/** The Audit page's filters, as the query string carries them. */
export type AuditFilters = {
  q?: string;
  actor?: string;
  action?: string;
  agent?: string;
  outcome?: string;
  since?: string;
  until?: string;
};

/** One page of the log plus the facets the page's selects offer within the
 * same window. `next_cursor` is null on the last page. */
export type AuditPage = {
  rows: AuditRow[];
  next_cursor: string | null;
  total_in_window: number;
  actors: string[];
  agents: string[];
  families: string[];
};

export function auditQueryString(filters: AuditFilters, extra: Record<string, string> = {}): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries({ ...filters, ...extra })) {
    if (value) {
      params.set(key, value);
    }
  }
  const text = params.toString();
  return text ? `?${text}` : "";
}

export async function fetchAdminAudit(filters: AuditFilters, cursor: string | null = null): Promise<AuditPage> {
  const res = await fetch(`/api/admin/audit${auditQueryString(filters, cursor ? { cursor } : {})}`);
  return parse<AuditPage>(res);
}

/** The link the Export buttons open: the current filter as a file. */
export function auditExportUrl(filters: AuditFilters, format: "csv" | "json"): string {
  return `/api/admin/audit/export${auditQueryString(filters, { format })}`;
}

/** The facts the panel's rail shows about this room. */
export type AdminRoom = {
  org_repo: string | null;
  runtime_image: string;
  cluster: string;
  delivery_backend: string;
};

export async function fetchAdminRoom(): Promise<AdminRoom> {
  const res = await fetch("/api/admin/room");
  return parse<AdminRoom>(res);
}

export async function fetchAdminAddons(): Promise<AdminAddon[]> {
  const res = await fetch("/api/admin/addons");
  const body = await parse<{ addons: AdminAddon[] }>(res);
  return body.addons;
}

/** The directory sweep (issue #274): the switchboard's directory against the
 * room's agents. `retired` is what the room registered for agents it no
 * longer has (would retire on a dry run, did retire otherwise); `kept` is
 * every entry with an agent behind it; `live` has no agent row but runs in
 * the cluster and is never touched; `unknown` was registered by hand or by
 * another room and is never touched; `failed` is what the switchboard
 * refused to retire. `note` says why nothing was retired when the cluster
/** Access through bab (design 2026-10-27). One permission in a vendor's tree;
 * the tier is a column. Whether an account's connection honours it is the
 * account's `ceiling`, so the same tree serves every account of the vendor. */
export type AccessPermission = {
  name: string;
  tier: "read" | "mutate" | "destroy" | string;
  description: string | null;
  /** What the row is called on the page (GitHub's "Read", "Review pull
   * requests", "Open pull requests"); null for a row named by its id. */
  label: string | null;
};

/** One account, project or subscription of a cloud vendor, or the single
 * connection of a service vendor. `connected` is whether the permission
 * service can mint for it yet; `ceiling` is the most any agent may hold there.
 *
 * GitHub (issue #365) has exactly one account per agent: the agent's own
 * repository set (`has_repo_set` true, `id` the child integration the
 * requests target, `repositories` filled) or, before one is saved, a
 * placeholder carrying the connection's id with no repositories. */
export type AccessAccount = {
  id: string;
  name: string;
  connected: boolean;
  ceiling: string[];
  /** The organization row this account sits under, when it does. */
  organization: string | null;
  account_id: string | null;
  repositories: string[];
  has_repo_set: boolean;
};

/** One vendor in the picker: a cloud (accounts underneath) or a service (one
 * connection), with its permissions grouped the vendor's way. */
export type AccessVendor = {
  id: string;
  kind: "cloud" | "service" | string;
  label: string;
  account_noun: string | null;
  organizations: { id: string; name: string }[];
  accounts: AccessAccount[];
  groups: { name: string; permissions: AccessPermission[] }[];
  tiers: { name: string; lease_seconds: number; validity_days: number | null }[];
};

export type AccessGrant = {
  id: number;
  agent: string;
  integration: string;
  permission: string;
  tier: string | null;
  gated: boolean;
  /** RFC 3339, or null for a standing grant. */
  expires_at: string | null;
  granted_by: string;
  created_at: string;
  request_id: number | null;
  state: "active" | "expired" | string;
};

export type AccessRequest = {
  id: number;
  agent: string;
  integration: string;
  permissions: string[];
  tiers: Record<string, number>;
  kind: "standing" | "lease" | string;
  status: "pending" | "approved" | "refused" | "denied" | string;
  requested_by: string;
  note: string | null;
  created_at: string;
  decided_at: string | null;
  decided_by: string | null;
  reason: string | null;
  outcome: Record<string, string>;
  /** Added by the room on the Review pane: the agent's owner and team. */
  owner?: string | null;
  team?: string | null;
  /** A GitHub request's repository set (issue #365), for the reviewer. */
  repositories?: string[];
};

export type AgentAccess = {
  agent: string;
  team: string | null;
  grants: AccessGrant[];
  pending: AccessRequest[];
  vendors: AccessVendor[];
  can_request: boolean;
  can_decide: boolean;
};

export type AccessLease = {
  id: number;
  agent: string;
  integration: string;
  permissions: string[];
  task: string | null;
  minter: string;
  issued_at: string;
  expires_at: string;
};

export type AgentLeases = { available: boolean; leases: AccessLease[]; note?: string };

/** One repository the room's GitHub installation can see (issue #365). */
export type GithubRepository = {
  full_name: string;
  private: boolean;
  default_branch: string | null;
};

/** `GET /api/agents/{name}/github/repositories`: the installation's
 * repositories with the agent's current set marked. `available` false means
 * the room cannot list them (no GitHub connection, or the vendor refused)
 * and `note` says so in a sentence. */
export type GithubRepositories = {
  available: boolean;
  repositories: GithubRepository[];
  selected: string[];
  note: string | null;
  /** How many repositories the room can see in all (issue #368); the
   * picker's search placeholder says it. Absent on an older server, where
   * the list's own length is the answer. */
  total?: number;
};

export async function fetchGithubRepositories(name: string): Promise<GithubRepositories> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/github/repositories`);
  return parse<GithubRepositories>(res);
}

/** Writes or replaces the agent's repository set. `integration` is the child
 * the agent's GitHub permission requests then target. 422 with a sentence on
 * an empty set or a repository the installation cannot see. */
export async function putGithubRepositories(
  name: string,
  repositories: string[],
): Promise<{ integration: string; repositories: string[]; changed: boolean }> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/github/repositories`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ repositories }),
  });
  return parse(res);
}

export async function fetchAgentAccess(name: string): Promise<AgentAccess> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/access`);
  return parse<AgentAccess>(res);
}

export async function requestAgentAccess(
  name: string,
  input: { integration: string; permissions: string[]; note?: string | null },
): Promise<AccessRequest> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/access/requests`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  return parse<AccessRequest>(res);
}

export async function revokeAgentAccess(name: string, grantId: number): Promise<{ revoked: number }> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/access/grants/${grantId}`, { method: "DELETE" });
  return parse<{ revoked: number }>(res);
}

export async function fetchAgentLeases(name: string, days: number): Promise<AgentLeases> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/leases?days=${days}`);
  return parse<AgentLeases>(res);
}

export async function fetchAccessRequests(status: "pending" | "all"): Promise<AccessRequest[]> {
  const res = await fetch(`/api/admin/access/requests?status=${status}`);
  const body = await parse<{ requests: AccessRequest[] }>(res);
  return body.requests;
}

export type AccessDecision = { id: number; status: string; outcome?: Record<string, string>; reason?: string | null };

export async function decideAccessRequests(
  ids: number[],
  verdict: "approve" | "deny",
  reason?: string | null,
): Promise<AccessDecision[]> {
  const res = await fetch("/api/admin/access/decide", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ids, verdict, reason: reason ?? null }),
  });
  const body = await parse<{ results: AccessDecision[] }>(res);
  return body.results;
}

export async function fetchAccessLeases(): Promise<AccessLease[]> {
  const res = await fetch("/api/admin/access/leases");
  const body = await parse<{ leases: AccessLease[] }>(res);
  return body.leases;
}

export async function syncAccessTopology(): Promise<{ agents: number; removed: number }> {
  const res = await fetch("/api/admin/access/sync", { method: "POST" });
  return parse<{ agents: number; removed: number }>(res);
}

export async function fetchWhoami(): Promise<Whoami> {
  const res = await fetch("/api/whoami");
  return parse<Whoami>(res);
}

export async function chatWithAgent(
  name: string,
  messages: ChatMessage[],
): Promise<{ reply: string }> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ messages }),
  });
  return parse<{ reply: string }>(res);
}

/**
 * `POST /api/agents/{name}/teach` (Task 1's "a chat moment becomes a taught
 * memory" route, fronted by Task 2's per-bubble "Teach this" gesture): posts
 * the operator's distilled/edited note text verbatim as `{text}`. The route
 * formats and names it server-side before pushing it either straight to the
 * repo (Task 5's `mode: "instant"`, no `sha` yet since nothing pushed) or as
 * a one-shot `bundle/intake/*` upload (`mode: "archive"`, same as
 * `uploadIntake` below, `sha` is the pushed commit's). `mode` missing
 * entirely (an older server) reads as `"archive"` too. 422 on empty/oversized
 * text, 409 when the agent isn't ready, both surfaced as an `ApiError`
 * through `parse`, same as every other mutation in this file.
 */
/** What a teach lands as. `learning` (issue #269) is the approved,
 * `source: "taught"` row the room recorded behind the note, so the
 * Learnings table can show it at once; an older room omits it. */
export type TeachResult = {
  filename: string;
  mode?: "instant" | "archive";
  sha?: string;
  learning?: Learning;
};

export async function teachAgent(name: string, text: string): Promise<TeachResult> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/teach`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  return parse<TeachResult>(res);
}

/** The terminal's one flattened, chronological feed (`GET /api/colleagues/
 * feed`, terminal redesign) -- replaces the old card grid's `
 * listColleagueConversations`/`fetchColleagueConversation` pair (and their
 * "show everything" toggle) entirely: one call, full text, already sorted.
 * `agent` is always sent, never optional -- the caller is always mounted
 * under one specific agent's Chat tab, so a viewer only ever sees the
 * conversations THAT agent participates in, same "server decides, UI just
 * carries the filter" discipline as everywhere else in this room. A 409
 * (the switchboard isn't wired on this room) surfaces as an `ApiError`
 * carrying that status, same as any other route; the caller
 * (`ColleagueConversations.svelte`) treats that status specifically as
 * "render nothing" rather than a scary error banner. */
export async function fetchColleagueFeed(agent: string): Promise<ColleagueFeed> {
  const res = await fetch(`/api/colleagues/feed?agent=${encodeURIComponent(agent)}`);
  return parse<ColleagueFeed>(res);
}

export async function checkAvailability(
  name: string,
): Promise<{ available: boolean; reason: string | null }> {
  const res = await fetch(`/api/availability?name=${encodeURIComponent(name)}`);
  return parse<{ available: boolean; reason: string | null }>(res);
}

export type KnowledgeFile = { name: string; content: string };

export async function listKnowledge(name: string): Promise<KnowledgeFile[]> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/knowledge`);
  const data = await parse<{ files: KnowledgeFile[] }>(res);
  return data.files;
}

/**
 * `PUT /api/agents/{name}/knowledge/{filename}` (fast-memory-lane,
 * arc3c Task 3): relays through the switchboard first, same instant-lane/
 * archive-lane shape `teachAgent` above documents. `mode: "instant"` (the
 * relay landed live) carries NO `sha` at all -- there is nothing to poll
 * `fetchDeliveryState` for, since the write already happened, not through a
 * rebuild. `mode: "archive"` (or a pre-fast-lane server that never sends
 * `mode`) carries the pushed commit's `sha` as before.
 */
export async function saveKnowledgeFile(
  name: string,
  filename: string,
  content: string,
): Promise<{ sha?: string; mode?: "instant" | "archive" }> {
  const res = await fetch(
    `/api/agents/${encodeURIComponent(name)}/knowledge/${encodeURIComponent(filename)}`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    },
  );
  return parse<{ sha?: string; mode?: "instant" | "archive" }>(res);
}

/** `DELETE /api/agents/{name}/knowledge/{filename}` -- same instant-lane/
 * archive-lane response shape as `saveKnowledgeFile` above. */
export async function deleteKnowledgeFile(
  name: string,
  filename: string,
): Promise<{ sha?: string; mode?: "instant" | "archive" }> {
  const res = await fetch(
    `/api/agents/${encodeURIComponent(name)}/knowledge/${encodeURIComponent(filename)}`,
    { method: "DELETE" },
  );
  return parse<{ sha?: string; mode?: "instant" | "archive" }>(res);
}

export async function fetchDeliveryState(
  name: string,
  sha: string,
): Promise<{ state: string; detail: string | null }> {
  const res = await fetch(
    `/api/agents/${encodeURIComponent(name)}/knowledge/delivery/${encodeURIComponent(sha)}`,
  );
  return parse<{ state: string; detail: string | null }>(res);
}

/** One candidate memory snippet awaiting a verdict (daily-learnings
 * spec): proposed nightly by the agent from its own conversation journal,
 * parked by the room, inert until a human approves it into memory. */
export type Learning = {
  id: number;
  day: string;
  content: string;
  kind: "fact" | "procedure";
  source: string;
  evidence: string;
  said_by: string | null;
  /** Why remembering this needs a second yes (issue #143): "unattributed"
   * (nobody it can be traced to) and/or "access_change" (it is about who
   * may access what). Older rooms omit it. */
  concerns?: string[];
  status: "pending" | "approved" | "denied" | "expired";
  decided_by: string | null;
  created_at: string | null;
};

/** How a system task the room sends this agent last went (issue #142). */
export type SystemTaskStatus = {
  task: string;
  last_run_at: string | null;
  last_success_at: string | null;
  last_error: string | null;
  consecutive_failures: number;
  failing: boolean;
};

export type LearningsList = { learnings: Learning[]; system_tasks?: SystemTaskStatus[] };

/** What the agent did through the tool gateway (admitted calls and refusals). */
export type AgentActivityRow = {
  ts: number | null;
  handler: string;
  tool: string;
  outcome: "ok" | "error" | "denied" | string;
  args: string;
  ms: number | null;
  detail: string;
};
export type AgentActivity = { available: boolean; note: string | null; rows: AgentActivityRow[] };

export async function fetchActivity(name: string, days = 7): Promise<AgentActivity> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/activity?days=${days}`);
  return parse<AgentActivity>(res);
}

export async function listLearnings(name: string): Promise<LearningsList> {
  const res = await fetch(
    `/api/agents/${encodeURIComponent(name)}/learnings?status=all`,
  );
  return parse<LearningsList>(res);
}

export async function decideLearning(
  name: string,
  id: number,
  action: "approve" | "deny",
  content?: string,
  confirm = false,
): Promise<void> {
  const body: { content?: string; confirm?: boolean } = {};
  if (action === "approve" && content !== undefined) body.content = content;
  if (action === "approve" && confirm) body.confirm = true;
  const res = await fetch(
    `/api/agents/${encodeURIComponent(name)}/learnings/${id}/${action}`,
    Object.keys(body).length > 0
      ? {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }
      : { method: "POST" },
  );
  await parse(res);
}

export async function decideLearningsBulk(
  name: string,
  ids: number[],
  action: "approve" | "deny",
): Promise<void> {
  const res = await fetch(
    `/api/agents/${encodeURIComponent(name)}/learnings/bulk`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids, action }),
    },
  );
  await parse(res);
}

/** One entry of `GET /api/agents/{name}/skills` — a catalog pick or a
 * locally-authored guardrail skill, both served by the same shape. `team`
 * and `sha` are set for a catalog pick (the catalog team it came from, and
 * the catalog's current pin for it); both are `null` for a local skill,
 * which carries no catalog provenance at all. `editable` is `true` exactly
 * when `source === "local"` — a catalog pick's content lives in the
 * catalog, not something this room edits in place. */
export type AgentSkill = {
  name: string;
  team: string | null;
  source: "catalog" | "local";
  sha: string | null;
  editable: boolean;
};

export async function listSkills(name: string): Promise<AgentSkill[]> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/skills`);
  const data = await parse<{ skills: AgentSkill[] }>(res);
  return data.skills;
}

/** Whether a skill or runtime change re-registered the agent's card with
 * the switchboard (issue #147): `off` when the room has no switchboard. */
export type DirectorySync = "synced" | "failed" | "off";

/** What the three skill mutations answer with: the pushed commit's sha and
 * the card's re-registration (issue #147). */
export type SkillChange = { sha: string; directory: DirectorySync };

/**
 * Picks a skill from the org catalog for this agent (`team`/`skillName`
 * identify it there). Returns the pushed commit's sha and whether the
 * switchboard got the updated card.
 */
export async function addCatalogSkill(
  name: string,
  skillName: string,
  team: string,
): Promise<SkillChange> {
  const res = await fetch(
    `/api/agents/${encodeURIComponent(name)}/skills/catalog`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: skillName, team }),
    },
  );
  return parse<SkillChange>(res);
}

/** What a batch add answers with (issue #266): one pushed sha for every
 * skill that landed (`added`), and the picks the room turned away one by
 * one (`refused`, each with its reason) while the rest still landed. */
export type SkillBatchChange = SkillChange & {
  added: string[];
  refused: { name: string; reason: string }[];
};

/** Picks several skills from the org catalog for this agent in one change
 * (one push, one rebuild). 1 to 20 picks. */
export async function addCatalogSkills(
  name: string,
  picks: { name: string; team: string }[],
): Promise<SkillBatchChange> {
  const res = await fetch(
    `/api/agents/${encodeURIComponent(name)}/skills/catalog/batch`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ skills: picks }),
    },
  );
  return parse<SkillBatchChange>(res);
}

/** Removes a skill — catalog pick or local guardrail alike, the server's
 * single unified delete path for both classes. */
export async function deleteSkill(
  name: string,
  skillId: string,
): Promise<SkillChange> {
  const res = await fetch(
    `/api/agents/${encodeURIComponent(name)}/skills/${encodeURIComponent(skillId)}`,
    { method: "DELETE" },
  );
  return parse<SkillChange>(res);
}

/** The current content of one local (guardrail-authored) skill. Never
 * served for a catalog pick — the server 404s that case instead. */
export async function readLocalSkill(
  name: string,
  skillId: string,
): Promise<{ name: string; content: string }> {
  const res = await fetch(
    `/api/agents/${encodeURIComponent(name)}/skills/local/${encodeURIComponent(skillId)}`,
  );
  return parse<{ name: string; content: string }>(res);
}

/** Saves (creates or overwrites) one local skill's content. */
export async function saveLocalSkill(
  name: string,
  skillId: string,
  content: string,
): Promise<SkillChange> {
  const res = await fetch(
    `/api/agents/${encodeURIComponent(name)}/skills/local/${encodeURIComponent(skillId)}`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    },
  );
  return parse<SkillChange>(res);
}

/** One `bundle/intake/*` file — a one-shot upload meant to be ingested into
 * the agent's memory exactly once by a runtime seeder. `status` is derived
 * fresh by the server from git ancestry vs. the live image on every list
 * call, never kobo-side state: `"pending"` until the live pod's commit
 * descends from (or is) the commit that last touched this file,
 * `"ingested"` once it does. */
export type IntakeFile = {
  name: string;
  content_bytes: number;
  status: "pending" | "ingested";
};

export async function listIntake(name: string): Promise<IntakeFile[]> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/intake`);
  const data = await parse<{ files: IntakeFile[] }>(res);
  return data.files;
}

/** Uploads (creates or overwrites) one intake file. Unlike knowledge/skill
 * saves, the filename travels in the request body, not the URL path — this
 * is a one-shot upload, not an edit of a named existing resource.
 *
 * Fast-memory-lane (arc3c Task 4): filenames must satisfy the runtime's own
 * teach-lane rule (lowercase letters/digits/hyphens, 40 chars max, `.md`
 * only) — the server checks this unconditionally, before ever touching the
 * repo, so a `.txt` upload the older, looser server-side rule used to admit
 * now 422s immediately. Same instant-lane/archive-lane response shape as
 * `saveKnowledgeFile`/`teachAgent`: `mode: "instant"` carries NO `sha` (the
 * write already landed live), `mode: "archive"` carries the pushed commit's
 * `sha`. */
export async function uploadIntake(
  name: string,
  filename: string,
  content: string,
): Promise<{ sha?: string; mode?: "instant" | "archive" }> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/intake`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ filename, content }),
  });
  return parse<{ sha?: string; mode?: "instant" | "archive" }>(res);
}

/** Removes an intake file's source upload. `note` is the server's
 * plain-language reminder that this only removes the source file — whatever
 * the runtime seeder already ingested from it into the agent's actual
 * memory stays. */
export async function deleteIntake(
  name: string,
  filename: string,
): Promise<{ sha: string; note: string }> {
  const res = await fetch(
    `/api/agents/${encodeURIComponent(name)}/intake/${encodeURIComponent(filename)}`,
    { method: "DELETE" },
  );
  return parse<{ sha: string; note: string }>(res);
}

/** One `bundle/tools/*` file — the staged door for an agent's LAW-to-be
 * (2026-08-13 addendum): stored and delivered the same way knowledge/skills
 * are. `delivered` is derived fresh by the server from the same git-ancestry
 * check `IntakeFile.status` uses, never kobo-side state — true once the live
 * pod's commit descends from (or is) the commit that last touched this
 * file. `status` (Task 4: card probe + banner graduation) is present, and
 * always exactly `"enforced"`, only when the server's card probe attests
 * the runtime executes tooling hooks AND isn't currently locked AND this
 * file is delivered — see `ToolingEnforcement`. A pending file, or any file
 * when the runtime isn't attested (or is locked), carries no `status` key
 * at all: delivery truth is never overwritten by enforcement status. */
export type ToolingFile = {
  name: string;
  content_bytes: number;
  delivered: boolean;
  status?: "enforced";
};

/** Whether the running pod actually enforces `bundle/tools/*` hooks around a
 * reply — probed fresh on every list fetch from the pod's own
 * `/.well-known/agent-card.json` (Task 4), never guessed or cached
 * kobo-side. `attested` is true iff that fetch succeeded AND the card's
 * `enforcement.version` is >= 1; false for a pre-enforcement runtime, an
 * unreachable/still-booting pod, or a card kobo couldn't parse — every one
 * of those reads identically, on purpose. `locked`/`locked_file` are
 * independent of `attested`: a runtime can attest to enforcement and still
 * be locked (a hook file failed to load, so it's serving refusals until
 * fixed or removed). */
export type ToolingEnforcement = {
  attested: boolean;
  locked: boolean;
  locked_file: string | null;
};

export type ToolingListResponse = {
  files: ToolingFile[];
  enforcement: ToolingEnforcement;
};

export async function listTooling(name: string): Promise<ToolingListResponse> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/tooling`);
  return parse<ToolingListResponse>(res);
}

/** Uploads (creates or overwrites) one tooling file. Like `uploadIntake`,
 * the filename travels in the request body, not the URL path — this is a
 * fresh file pick from a form, not an edit of an already-named resource. */
export async function uploadTooling(
  name: string,
  filename: string,
  content: string,
): Promise<{ sha: string }> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/tooling`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ filename, content }),
  });
  return parse<{ sha: string }>(res);
}

/** `POST /api/agents/{name}/tooling/import`'s partial-pass result: every
 * candidate `.py`/`.go` file under `path` in `repo` is validated
 * independently, so one bad file never sinks the rest. `sha` is `null` when
 * nothing passed (nothing was pushed at all — see `kobo_api.tooling.
 * import_tooling`'s docstring). */
export type ToolingImportResult = {
  sha: string | null;
  imported: string[];
  rejected: { name: string; reason: string }[];
};

/** Imports every `.py`/`.go` file that passes validation from `path` in
 * `repo` (cloned via kobo's own GitHub App identity — no separate auth
 * needed) into this agent's `bundle/tools/`, in one delivery. */
export async function importTooling(
  name: string,
  repo: string,
  path: string,
): Promise<ToolingImportResult> {
  const res = await fetch(
    `/api/agents/${encodeURIComponent(name)}/tooling/import`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repo, path }),
    },
  );
  return parse<ToolingImportResult>(res);
}

/** Removes a tooling file. These are files, not memories — deleting removes
 * the file, full stop (no "already ingested elsewhere" caveat like
 * `deleteIntake`'s `note`). */
export async function deleteTooling(
  name: string,
  filename: string,
): Promise<{ sha: string }> {
  const res = await fetch(
    `/api/agents/${encodeURIComponent(name)}/tooling/${encodeURIComponent(filename)}`,
    { method: "DELETE" },
  );
  return parse<{ sha: string }>(res);
}

/**
 * `GET /api/agents/{name}/slack/manifest`: the agent's rendered Slack app
 * manifest (Slack self-service, 2026-08-17) — any signed-in user, ready-
 * gated server-side, same as `connectSlack` below. Unwraps the response's
 * `{"manifest": {...}}` envelope into the plain dict; the caller
 * (`SlackPanel`'s "Show the manifest" control, via `slackManifestQuery` in
 * queries.ts) renders it verbatim, pretty-printed, in a copyable block.
 * Fetched lazily, never on mount: most agent-detail visits aren't setup
 * visits.
 */
export async function fetchSlackManifest(
  name: string,
): Promise<Record<string, unknown>> {
  const res = await fetch(
    `/api/agents/${encodeURIComponent(name)}/slack/manifest`,
  );
  const data = await parse<{ manifest: Record<string, unknown> }>(res);
  return data.manifest;
}

/**
 * Finishes Slack connect: hands the app id plus the two tokens (all copied
 * by the human from Slack's own app site, per the self-service ceremony) to
 * the server, which validates and stores them. Slack self-service
 * (2026-08-17) added `app_id` to this body — the app now already exists by
 * connect time (a human created it themselves from the manifest above), so
 * its id arrives here instead of getting minted by a retired create-app
 * route. Two-lane Slack (2026-08-17) had already moved the channel list off
 * this call entirely — the channels an agent listens on are whatever's
 * already saved via `saveSlackChannels` below, not something connect
 * carries. Every field passes straight through in the request body and is
 * never logged or otherwise inspected here.
 */
export async function connectSlack(
  name: string,
  body: { app_id: string; bot_token: string; app_token: string },
): Promise<{ sha: string }> {
  const res = await fetch(
    `/api/agents/${encodeURIComponent(name)}/slack/connect`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  );
  return parse<{ sha: string }>(res);
}

/**
 * Saves the full replacement channel list (not a delta — the server's
 * `SlackChannelsRequest` doc is explicit about this). `sha` is present only
 * when the save fired a rewire (the agent was already wired and the list
 * changed) — see `kobo_api.main.save_slack_channels`. An empty list is the
 * deliberate disconnect path, not an error.
 */
export async function saveSlackChannels(
  name: string,
  channels: string[],
): Promise<{ channels: string[]; state: "idle" | "waiting" | "connected"; sha?: string }> {
  const res = await fetch(
    `/api/agents/${encodeURIComponent(name)}/slack/channels`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ channels }),
    },
  );
  return parse<{
    channels: string[];
    state: "idle" | "waiting" | "connected";
    sha?: string;
  }>(res);
}

/**
 * `PUT /api/agents/{name}/slack/wake`: how Slack wakes the agent in a
 * channel. `manifest_changed` is true when the Slack app's own event list
 * changed with it (crossing between `mention` and the other two), which only
 * a re-applied manifest at Slack can carry out; the panel says so.
 */
export async function setSlackWake(
  name: string,
  wake: SlackWake,
): Promise<{ wake: SlackWake; previous_wake: SlackWake; manifest_changed: boolean; wiring: string }> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/slack/wake`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ wake }),
  });
  return parse<{
    wake: SlackWake;
    previous_wake: SlackWake;
    manifest_changed: boolean;
    wiring: string;
  }>(res);
}

/** `GET /api/agents/{name}/slack`: the Access tab's Slack section. */
export async function fetchSlackApp(name: string): Promise<SlackAppView> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/slack`);
  return parse<SlackAppView>(res);
}

/**
 * `POST /api/agents/{name}/slack/app` (issue #371): the room creates the
 * agent's Slack app with the Wakes on choice it is born with, and answers
 * with the install link; the owner opens it in the same tab.
 */
export async function createSlackApp(
  name: string,
  wake: SlackWake,
): Promise<{ app_id: string; install_url: string }> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/slack/app`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ wake }),
  });
  return parse<{ app_id: string; install_url: string }>(res);
}

/** `DELETE /api/agents/{name}/slack/app`: the app at Slack, its values and
 * the relay row go; the agent restarts. Channels stay saved. */
export async function removeSlackApp(name: string): Promise<{ removed: boolean }> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/slack/app`, { method: "DELETE" });
  return parse<{ removed: boolean }>(res);
}

/** The three directory fields an Edit details save/Regenerate hands back,
 * always alongside the pushed commit's sha (persona-metadata plan Task 3) —
 * `PATCH .../metadata` and `POST .../metadata/regenerate` share this exact
 * response shape. */
export type AgentMetadata = {
  team: string;
  purpose_tags: string[];
  capability: string | null;
};

/**
 * Edit details' save: every field optional, the server leaves whatever's
 * omitted at its current stored value (`kobo_api.main.AgentMetadataPatch`).
 * `capability` is only ever sent non-empty here — the server has no way to
 * distinguish "clear this field" from "field omitted", so the caller
 * (`MetadataDetails`) simply never sends an empty string. 422 on a bad team
 * slug ("that doesn't look like a team name"), an over-cap tag list ("Eight
 * tags at most."), or an over-160/unusable capability string; 502 if the
 * card commit itself fails (delivery is the caller's to retry).
 */
export async function patchAgentMetadata(
  name: string,
  body: { team?: string; purpose_tags?: string[]; capability?: string },
): Promise<AgentMetadata & { sha: string }> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/metadata`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return parse<AgentMetadata & { sha: string }>(res);
}

/**
 * Regenerates the agent's capability line against its CURRENT skills and
 * tooling (persona-metadata plan Task 3) — `team`/`purpose_tags` are
 * untouched by this call and simply echoed back at their existing stored
 * value, same response shape as the PATCH above.
 */
export async function regenerateAgentMetadata(
  name: string,
): Promise<AgentMetadata & { sha: string }> {
  const res = await fetch(
    `/api/agents/${encodeURIComponent(name)}/metadata/regenerate`,
    { method: "POST" },
  );
  return parse<AgentMetadata & { sha: string }>(res);
}

/** What `PATCH /api/agents/{name}/runtime` answers with: the image the
 * agent runs on now, the one it left, the commit that re-pinned its
 * Dockerfile, and whether the switchboard got the card naming the new
 * runtime (issue #147). The push IS the deploy -- the agent's own CI rebuilds and
 * rotates the pod on it -- so there is no second call after this one. */
export type AgentRuntimeChange = {
  runtime_image: string;
  previous_runtime_image: string | null;
  sha: string;
  directory: DirectorySync;
};

/**
 * Moves one agent onto a different runtime image (`PATCH
 * /api/agents/{name}/runtime`). The room only ever offers its OWN current
 * image here, from the Harness tab's Model and runtime section, so a
 * caller never types a ref; the server validates the registry root anyway
 * and answers 422 for anything outside it, 409 for an agent that is not
 * ready, 403 for a caller who may not change it.
 */
/** `PATCH /api/agents/{name}/model`: move a deployed agent onto another
 * catalog model. The room writes `AGENT_MODEL` and the model's region into
 * the agent's deploy contract, commits, and the platform regenerates and
 * rolls it. `wiring` says how the change landed. */
export type AgentModelChange = {
  model: string;
  model_label: string;
  previous_model: string;
  wiring: string;
};

export async function patchAgentModel(name: string, model: string): Promise<AgentModelChange> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/model`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model }),
  });
  return parse<AgentModelChange>(res);
}

/** The owner's colleague mode (issue #131). `default` is every colleague
 * tool and the consult-first doctrine; `roster-only` keeps the roster but
 * not the ask tool; `none` drops both. */
export type ColleagueMode = "default" | "roster-only" | "none";

export type ColleagueModeChange = {
  colleague_mode: ColleagueMode;
  previous_colleague_mode: ColleagueMode;
  sha: string;
};

/** `PUT /api/agents/{name}/colleague-mode`: one commit to the agent's
 * bundle (its allow list and colleague document); the CI rebuild rolls it. */
export async function putColleagueMode(
  name: string,
  mode: ColleagueMode,
): Promise<ColleagueModeChange> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/colleague-mode`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode }),
  });
  return parse<ColleagueModeChange>(res);
}

export async function patchAgentRuntime(
  name: string,
  runtimeImage: string,
): Promise<AgentRuntimeChange> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/runtime`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ runtime_image: runtimeImage }),
  });
  return parse<AgentRuntimeChange>(res);
}

/**
 * Generates (or regenerates) an agent's picture: the server derives a
 * prompt from the agent's persona, generates a square image, commits it to
 * the agent's repo, and caches it on the agent row. Returns the commit sha
 * so the caller can cache-bust the `<img src>` that reads the picture back
 * (see `GET /api/agents/{name}/avatar`, a plain URL — no client fn needed
 * for that side, an `<img>` tag fetches it directly).
 */
export async function generateAvatar(name: string): Promise<{ sha: string }> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/avatar`, {
    method: "POST",
  });
  return parse<{ sha: string }>(res);
}

/**
 * Human-provided counterpart to `generateAvatar` above: PUTs the raw PNG
 * bytes as the entire request body (no JSON envelope, no multipart form) —
 * matching `PUT /api/agents/{name}/avatar`'s own contract. Same return shape
 * (`{sha}`) and same cache-busting use (the caller re-derives the `<img
 * src>` from the returned sha, same as after a generate).
 */
export async function uploadAvatar(name: string, png: ArrayBuffer): Promise<{ sha: string }> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/avatar`, {
    method: "PUT",
    headers: { "Content-Type": "image/png" },
    body: png,
  });
  return parse<{ sha: string }>(res);
}

// --- Cross-group colleagueship: invites + per-user grants (access-control
// arc, Task C1 API / Task C2 UI) ---------------------------------------------

/** `GET /api/agents/{name}/invites`: every invite naming this agent, split
 * into incoming (this agent is the TO side) and outgoing (this agent is the
 * FROM side). Owner-or-operator only server-side -- a 403/404 here is how
 * the Access tab (AgentDetail.svelte) decides it isn't allowed to show at
 * all for the current caller. */
export async function listInvites(name: string): Promise<InvitesResult> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/invites`);
  return parse<InvitesResult>(res);
}

/** `GET /api/agents/{name}/pairs`: the agents `name` may share a thread
 * with right now, capped to those the caller can also see. Open to anyone
 * who can see `name`, same as starting a thread from it. */
export async function listPairs(name: string): Promise<PairsResult> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/pairs`);
  return parse<PairsResult>(res);
}

/** `POST /api/agents/{name}/invites`: requests cross-group colleague access
 * from `name` (the FROM agent, must be owned/operated by the caller) to
 * `toAgent` (the TO agent, may sit outside the caller's own visible set --
 * that's the feature, not a hole in it). */
export async function createInvite(name: string, toAgent: string): Promise<Invite> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/invites`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ to_agent: toAgent }),
  });
  return parse<Invite>(res);
}

/** `POST /api/invites/{id}/accept`: owner-or-operator of the invite's TO
 * agent only. */
export async function acceptInvite(id: number): Promise<Invite> {
  const res = await fetch(`/api/invites/${encodeURIComponent(String(id))}/accept`, {
    method: "POST",
  });
  return parse<Invite>(res);
}

/** `POST /api/invites/{id}/decline`: owner-or-operator of the invite's TO
 * agent only. */
export async function declineInvite(id: number): Promise<Invite> {
  const res = await fetch(`/api/invites/${encodeURIComponent(String(id))}/decline`, {
    method: "POST",
  });
  return parse<Invite>(res);
}

/** `DELETE /api/invites/{id}`: owner-or-operator of EITHER side may walk
 * away from a pending or accepted invite. */
export async function revokeInvite(id: number): Promise<Invite> {
  const res = await fetch(`/api/invites/${encodeURIComponent(String(id))}`, {
    method: "DELETE",
  });
  return parse<Invite>(res);
}

/** `GET /api/agents/{name}/grants`: every per-user visibility grant on this
 * agent, owner-or-operator only. */
export async function listGrants(name: string): Promise<Grant[]> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/grants`);
  const data = await parse<{ grants: Grant[] }>(res);
  return data.grants;
}

/** `POST /api/agents/{name}/grants`: grants `email` access to this agent
 * with `role`. Idempotent server-side -- re-posting an already-granted
 * email returns the one row, its role set to the posted one. */
export async function addGrant(name: string, email: string, role: GrantRole): Promise<Grant> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/grants`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, role }),
  });
  return parse<Grant>(res);
}

/** `DELETE /api/agents/{name}/grants/{email}`: removes a per-person grant. */
export async function removeGrant(name: string, email: string): Promise<{ removed: boolean }> {
  const res = await fetch(
    `/api/agents/${encodeURIComponent(name)}/grants/${encodeURIComponent(email)}`,
    { method: "DELETE" },
  );
  return parse<{ removed: boolean }>(res);
}

/** `GET /api/agents/{name}/requestable`: every other READY agent `name`
 * could ask for colleague access, bypassing the caller's own personal
 * visibility on purpose (the a2a directory of bare names is deliberately
 * global) -- owner-or-operator of `name` only, server-side. Backs the
 * Access tab's own browsable picker (AccessPanel.svelte), the primary way
 * to start a cross-group request now; `RequestAccessCard`'s from-agent
 * picker on other agents' detail pages still works for a visible-not-yours
 * agent. */
export async function fetchRequestable(name: string): Promise<RequestableResult> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/requestable`);
  return parse<RequestableResult>(res);
}

/** `GET /api/people/search`: a signed-in-only proxy of the room's directory
 * search (access-tab refinement wave) -- backs the "Add someone by email"
 * autocomplete in AccessPanel.svelte. Fail-soft server-side: a directory
 * outage or a room with no directory wired both answer `{people: [],
 * unavailable: true}`, never a non-2xx, so this never throws for that
 * reason -- the caller reads `unavailable` to show a quiet note rather than
 * treating it as an error. */
export async function searchPeople(q: string): Promise<PeopleSearchResult> {
  const res = await fetch(`/api/people/search?q=${encodeURIComponent(q)}`);
  return parse<PeopleSearchResult>(res);
}

// --- Threads (threads arc, Tasks 8-10): a shared multi-agent conversation --
// the initiator starts, seats further participants into, and drives one
// turn at a time. A thread carries no persisted message log of its own --
// `GET /api/threads/{id}` (unused here; nothing in this surface ever needs
// to re-fetch a thread's metadata after creating/seating it, since both of
// those routes already hand back the fresh `Thread`) returns only id/
// initiator_email/created_at/participants, never a transcript -- so, same
// as `chatWithAgent`'s own single-agent chat, the turn-by-turn exchange is
// local component state: each `postThreadTurn` call resends the FULL
// transcript so far, and the caller is the one place any of it is ever
// remembered.

/** A thread's own metadata (`kobo_api.main._thread_repr`) -- both
 * `createThread` and `addThreadParticipant` return this shape fresh on
 * success, `participants` already reflecting the just-seated agent. */
export type Thread = {
  id: number;
  initiator_email: string | null;
  created_at: string;
  participants: string[];
};

/** `POST /api/threads`: starts a new thread naming one agent the caller can
 * see -- open to any signed-in caller, never an ownership gate (a thread is
 * the initiator's own space, not a mutation of the named agent itself).
 * 409 when that agent isn't ready yet, the room's own anti-enumeration 404
 * when the caller can't see it at all -- both surfaced as an `ApiError`
 * through `parse`, same as every other mutation in this file. */
export async function createThread(agent: string): Promise<Thread> {
  const res = await fetch("/api/threads", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ agent }),
  });
  return parse<Thread>(res);
}

/** `POST /api/threads/{id}/participants`: seats one more agent into an
 * already-created thread -- the thread's own initiator only. A caller who
 * didn't start `threadId` gets the identical not-found body a bogus id
 * gets (never a distinguishable 403); an agent the caller can't see gets
 * the room's own anti-enumeration 404; a cross-group agent with no accepted
 * invite already standing toward every current participant, an already-
 * seated agent, or a not-ready agent all 409/422 with a plain-language
 * reason. Every one of those surfaces as an `ApiError` through `parse` --
 * the caller (`ThreadPanel`) shows that message as a toast rather than an
 * inline error, since adding a participant is a quiet aside to the
 * conversation, not the main flow a form error would block. */
export async function addThreadParticipant(threadId: number, agent: string): Promise<Thread> {
  const res = await fetch(`/api/threads/${encodeURIComponent(String(threadId))}/participants`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ agent }),
  });
  return parse<Thread>(res);
}

/** One participant's answer to a thread turn (`kobo_api.main.post_thread_
 * turn`'s own `replies` entries) -- `agent` is the bare name that answered,
 * always one of `Thread.participants`. */
/** One ask/reply line of a colleague conversation the addressee had while
 * answering a thread turn, in the colleague feed's own entry shape. */
export type ThreadConsult = {
  ts: string;
  conversation_id: string;
  from: string;
  to: string;
  role: "ask" | "reply";
  text: string;
};
/** One participant's answer to a turn. `error` set (and `text` null) means
 * that agent did not answer; the turn itself still completed. */
export type ThreadReply = {
  agent: string;
  text: string | null;
  error: string | null;
  consults: ThreadConsult[];
};
export type ThreadTurnOptions = { to?: string | null; broadcast?: boolean };

/**
 * `POST /api/threads/{id}/turns`: delivers one turn to every participant IN
 * SEQUENCE, each seeing every message in `messages` plus every reply
 * already given earlier in this same turn -- `messages` is the FULL
 * transcript so far (this caller's own messages, `role: "user"`, interleaved
 * with every participant's past replies, `role: "agent"`), the exact same
 * "resend everything every time" contract `chatWithAgent` above already
 * documents, since a thread turn is that same mechanic run once per
 * participant instead of once against a single agent. The last entry in
 * `messages` must be `role: "user"` (a fresh ask), or the server 422s.
 * Initiator-only, same not-found-not-403 gate `addThreadParticipant`
 * documents; 409 when the thread has no participants yet; 502 (copy: "The
 * switchboard did not answer. Try again shortly.") on a relay failure to
 * ANY participant, matching how far the sequence actually got being
 * unrecoverable mid-turn.
 */
export async function postThreadTurn(
  threadId: number,
  messages: ChatMessage[],
  options: ThreadTurnOptions = {},
): Promise<{ replies: ThreadReply[]; to: string[] }> {
  const res = await fetch(`/api/threads/${encodeURIComponent(String(threadId))}/turns`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      messages,
      to: options.to ?? null,
      broadcast: options.broadcast ?? false,
    }),
  });
  return parse<{ replies: ThreadReply[]; to: string[] }>(res);
}

/** One workflow run's identity, as the build-log route reports it. */
export interface BuildRun {
  id: number;
  status: string;
  conclusion: string | null;
  head_sha: string;
}

export interface BuildLog {
  run: BuildRun;
  log: string;
}

/** The tail of the agent repo's most recent build log (build-log viewer,
 * 2026-08-27 feedback). Owner-or-operator on the server. */
/** Top skills for a persona (create-flow polish, 2026-08-28): the server
 * keyword-matches the persona's own text against the skills catalog. */
export async function fetchRecommendedSkills(
  who: string,
  what: string,
): Promise<CatalogEntry[]> {
  const params = new URLSearchParams({ who, what });
  const res = await fetch(`/api/skills/recommended?${params}`);
  const data = await parse<{ results: CatalogEntry[] }>(res);
  return data.results;
}

export async function fetchBuildLog(name: string): Promise<BuildLog> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/build-log`);
  return parse<BuildLog>(res);
}

// --- Secrets and environment (design 2026-09-22) -----------------------------
//
// Two lists on one panel. A SECRET's value is write-only: it goes up in a
// `set` map and never comes back, so nothing below has a type that could
// hold one on the way down. An environment variable's value does come
// back, which is the difference between the two lists rather than an
// oversight: a list you cannot read is not one you can edit.

/** How a change reached the agent's repository. `committed` for an agent
 * the room composed, `pull_request` for one whose repository belongs to
 * its team, `unchanged` when the file already said this. */
export type EnvWiring = "committed" | "pull_request" | "unchanged";

/** Whether what a name promises is actually on the agent, read off the
 * platform's own delivery object. `live` is the only one that means the
 * running agent can see it; `unwired` means nothing is delivering it at
 * all, and `syncing` covers both "not settled yet" and "the room could
 * not ask the cluster just now" (the message says which). */
export type SecretSyncState = "live" | "syncing" | "error" | "unwired";

export type SecretSync = {
  state: SecretSyncState;
  /** One plain sentence the ROOM wrote. The platform's own condition
   * message is deliberately not here: it is a sentence about a request
   * the room made, and a provider that echoed what it was asked to store
   * would put a value on somebody's screen. */
  message: string | null;
};

/** One secret the ROOM set on this agent rather than its owner: the
 * colleague pass, the Slack values. Shown as a locked row so a list that
 * refuses these names does not also hide them. */
export type ManagedSecret = {
  name: string;
  purpose: string;
  secret_name: string;
  sync: SecretSync;
};

export type AgentSecrets = {
  /** Key names, sorted. Never a value, masked or otherwise. */
  keys: string[];
  /** When the whole list was last written, not per key: the platform
   * versions a path as a whole. Null for an agent with no secrets yet. */
  updated_at: string | null;
  /** False in a room with no secrets platform configured, where the panel
   * shows one sentence instead of the list. */
  platform: boolean;
  /** The Kubernetes Secret the platform delivers these into, for the line
   * that says where they end up. Null when `platform` is false. */
  secret_name: string | null;
  /** True when there IS a platform and the room could not ask it on
   * anybody's behalf. An empty list would say the same wrong thing for a
   * different reason, so the panel has its own line for this. */
  unreadable: boolean;
  /** Whether the stored list is actually on the agent. One state for the
   * whole list: the platform delivers the whole path as ONE Kubernetes
   * Secret, so there is no per-key answer to give. */
  sync: SecretSync;
  /** Whether the agent's own files name that Secret, on its default
   * branch. Null when the room could not read the repository -- which is
   * not the same as the line being absent, and must not be shown as it. */
  wired: boolean | null;
  /** The secrets the room set itself, if any. */
  managed: ManagedSecret[];
};

export type AgentSecretsWrite = {
  keys: string[];
  platform: boolean;
  secret_name: string | null;
  wiring: EnvWiring;
  /** True when that save removed the last secret and the stored Secret is
   * actually gone. */
  emptied: boolean;
  /** True when that save removed the last secret and the stored Secret is
   * NOT gone: the agent's files still name it, and the room cannot merge
   * its own pull request. Saving again after that request merges is what
   * finishes the removal. */
  pending_unwire: boolean;
  pull_request?: string;
};

export type AgentEnv = {
  /** The owner's own variables, editable. */
  env: Record<string, string>;
  /** The variables the room set itself (the model, how Slack wakes the
   * agent), shown as locked rows and never sent back in a save. */
  room?: Record<string, string>;
};

export type AgentEnvWriteResult = {
  env: Record<string, string>;
  wiring: EnvWiring;
  pull_request?: string;
};

export async function listAgentSecrets(name: string): Promise<AgentSecrets> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/secrets`);
  return parse<AgentSecrets>(res);
}

/** Add, change or remove secrets in one call. The values in `set` leave
 * the browser once and are never read back by anything. */
export async function saveAgentSecrets(
  name: string,
  set: Record<string, string>,
  remove: string[],
): Promise<AgentSecretsWrite> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/secrets`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ set, remove }),
  });
  return parse<AgentSecretsWrite>(res);
}

export async function listAgentEnv(name: string): Promise<AgentEnv> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/env`);
  return parse<AgentEnv>(res);
}

export async function saveAgentEnv(
  name: string,
  set: Record<string, string>,
  remove: string[],
): Promise<AgentEnvWriteResult> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/env`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ set, remove }),
  });
  return parse<AgentEnvWriteResult>(res);
}

export type AgentRestart = { restarted: boolean; deployment: string };

/** Roll the agent's pods now. A saved secret reaches a running agent at
 * its next boot and not before, so this is what makes a save take
 * effect. */
export async function restartAgent(name: string): Promise<AgentRestart> {
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/restart`, {
    method: "POST",
  });
  return parse<AgentRestart>(res);
}
