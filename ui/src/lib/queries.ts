// TanStack Query v5 factories over the typed API client (api.ts). Kept as
// plain option-object factories (not `createQuery`/`createMutation` calls
// themselves) so components wire them up with `createQuery(agentsQuery())`
// etc., and so the queryKey conventions below are visible in one place for
// later tasks to invalidate.
//
// queryKey conventions:
//   ["agents"]                                      - the agent list
//   ["agents", name]                                - one agent's detail
//   ["agents", name, "knowledge"]                   - an agent's knowledge files
//   ["agents", name, "knowledge", "delivery", sha]  - delivery state for one sha
//   ["agents", name, "learnings"]                   - an agent's learnings
//                                                      queue, every status
//                                                      (daily-learnings spec)
//   ["agents", name, "skills"]                      - an agent's skills (catalog + local)
//   ["agents", name, "intake"]                      - an agent's intake files
//   ["agents", name, "tooling"]                     - an agent's tooling files
//   ["agents", name, "slack", "manifest"]           - an agent's rendered
//                                                      Slack manifest (fetched
//                                                      lazily, once opened)
//   ["agents", name, "invites"]                     - an agent's cross-group
//                                                      colleagueship invites,
//                                                      incoming + outgoing
//                                                      (access-control Task
//                                                      C1/C2)
//   ["agents", name, "grants"]                      - an agent's per-user
//                                                      visibility grants
//                                                      (access-control Task
//                                                      C1/C2)
//   ["catalog", "models"]                           - the model list + default
//   ["catalog", "observability"]                    - the org's dashboard
//                                                      base URL + project
//                                                      (observability-catalog
//                                                      addendum Task 4/6)
//   ["catalog", "groups"]                           - the room's named ACL
//                                                      groups (access-control
//                                                      Task B2)
//   ["catalog", "teams"]                             - the team-slug list
//                                                      (all catalog kinds,
//                                                      for search filtering)
//   ["teams"]                                        - the skill-owner team
//                                                      pool + default, for
//                                                      the Edit details
//                                                      team picker
//   ["catalog", "skills", {q, team}]                - a skill catalog search
//   ["catalog", "personas", {q, team, scope}]       - a persona catalog search
//   ["personas", "movable"]                         - the persona ids the
//                                                      caller may re-file
//   ["personas", "archived"]                        - every archived persona
//                                                      (hardening wave archive
//                                                      lane), fetched lazily
//                                                      behind the "Show
//                                                      archived" toggle
//   ["personas", "detail", id]                      - one whole persona, for
//                                                      the hover card in the
//                                                      create dialog's list
//                                                      (fetched lazily, once
//                                                      a row has been rested
//                                                      on)
//   ["org", "status"]                               - org bootstrap/wiring status
//   ["whoami"]                                       - the signed-in identity
//                                                      this room's server has
//                                                      already verified
//   ["names", "suggestions", {who_they_are, what_they_do, persona_id, exclude, round}]
//                                                    - three name-suggestion cards for a committed persona
//                                                      (create-identity, Task 4: persona_id/exclude/round
//                                                      all ride the key so a persona switch, an "Offer more"
//                                                      round bump, or a new exclude set all address a
//                                                      distinct cache entry)
//   ["colleagues", "feed", agent]                   - the terminal's flattened,
//                                                      chronological feed of one
//                                                      agent's own colleague talk
//                                                      (terminal redesign,
//                                                      replaced the old card
//                                                      grid's list/detail pair)
//   ["agents", name, "requestable"]                 - the other ready
//                                                      agents `name` could
//                                                      ask for colleague
//                                                      access (access-tab
//                                                      refinement wave)
//   ["people", "search", q]                         - a directory search for
//                                                      the add-by-email
//                                                      autocomplete
//                                                      (access-tab
//                                                      refinement wave)
//   ["group-requests"]                              - the admin queue of
//                                                      filed group requests
//                                                      (shared-group +
//                                                      group-requests wave),
//                                                      fetched lazily once
//                                                      the admin card is
//                                                      expanded
//
// Threads (threads arc, Tasks 8-10) have no queryOptions/queryKey of their
// own: `GET /api/threads/{id}` returns only id/initiator_email/created_at/
// participants, never a transcript, so a thread's turn-by-turn exchange is
// local component state, same posture `chatMutation`'s own doc comment
// documents for single-agent chat. See `createThreadMutation`/
// `addThreadParticipantMutation`/`postThreadTurnMutation` below.

import { queryOptions, type QueryClient } from "@tanstack/svelte-query";
import {
  ApiError,
  type AgentDetail,
  type AgentEnv,
  type AgentEnvWriteResult,
  type AgentListResult,
  type AgentMetadata,
  type AgentSkill,
  type AgentSecrets,
  type AgentSecretsWrite,
  type ColleagueMode,
  type AgentSlack,
  type AgentSummary,
  type AlreadyRequested,
  type ArchivedPersona,
  type CatalogEntry,
  type ChatMessage,
  type ColleagueFeed,
  type ColleagueFeedEntry,
  type ConnectAgentBody,
  type Grant,
  type GroupOption,
  type GroupRequest,
  type IntakeFile,
  type Invite,
  type InvitesResult,
  type KnowledgeFile,
  type Learning,
  type NameSuggestion,
  type OrgStatus,
  type PeopleSearchResult,
  type Person,
  type PersonaDetail,
  type PersonaDistillResult,
  type PersonaDraft,
  type PersonaForm,
  type PersonaScope,
  type RequestableAgent,
  type RequestableResult,
  type ScanFinding,
  type ScanReport,
  type ScanRepoBody,
  type TeamOptions,
  type TeamPair,
  type Thread,
  type ThreadReply,
  type ToolingEnforcement,
  type ToolingFile,
  type ToolingListResponse,
  type Whoami,
  acceptInvite,
  addCatalogSkill,
  addGrant,
  addThreadParticipant,
  archivePersona,
  archivedPersonas,
  chatWithAgent,
  connectAgent,
  connectSlack,
  convertToConnected,
  createAgent,
  createInvite,
  createPersona,
  createThread,
  decideGroupRequest,
  decideLearning,
  decideLearningsBulk,
  declineInvite,
  deleteIntake,
  deleteKnowledgeFile,
  deleteSkill,
  deleteTooling,
  destroyAgent,
  distillPersona,
  draftPersona,
  fetchPersona,
  fetchColleagueFeed,
  fetchDeliveryState,
  fetchGroupRequests,
  fetchGroups,
  fetchGuide,
  fetchModels,
  fetchObservability,
  fetchRecommendedSkills,
  fetchNameSuggestions,
  fetchOrgStatus,
  fetchRequestable,
  fetchSlackManifest,
  fetchTeamOptions,
  fetchTeams,
  type AdminConnectIntegration,
  type AdminRoleBody,
  deleteAdminRole,
  fetchAdminRoles,
  putAdminRole,
  reloadAdminOrg,
  connectAdminIntegration,
  disconnectAdminIntegration,
  fetchAdminAddons,
  fetchAdminAudit,
  fetchAdminIntegrations,
  fetchAdminPeople,
  fetchWhoami,
  generateAvatar,
  getAgent,
  importTooling,
  listAgentEnv,
  listAgentSecrets,
  listAgents,
  listGrants,
  listIntake,
  listInvites,
  listKnowledge,
  listLearnings,
  listSkills,
  listTooling,
  movablePersonas,
  movePersona,
  patchAgentMetadata,
  patchAgentModel,
  patchAgentRuntime,
  putColleagueMode,
  postThreadTurn,
  regenerateAgentMetadata,
  removeGrant,
  requestGroup,
  restartAgent,
  retryAgent,
  revokeInvite,
  saveAgentEnv,
  saveAgentSecrets,
  saveKnowledgeFile,
  saveLocalSkill,
  saveSlackChannels,
  setSlackWake,
  type SlackWake,
  scanRepo,
  searchConnectRepos,
  searchPeople,
  searchPersonas,
  searchSkills,
  teachAgent,
  unarchivePersona,
  uploadAvatar,
  uploadIntake,
  uploadTooling,
} from "./api";
import { detailRefetchInterval, listRefetchInterval } from "./status";

/**
 * The single retry policy for every query in the app (wired into the
 * QueryClient's `defaultOptions.queries.retry` in App.svelte, where the
 * client is constructed, so all queries inherit it).
 *
 * 4xx responses are the server telling us the request itself is bad (not
 * found, conflict, validation, ...) — retrying won't change that, so burning
 * TanStack's default 3 retries against them just makes an already-final
 * result (e.g. a 404) take ~7s of backoff to render. Everything else
 * (5xx, network errors) is retried up to 3 times, same as the library
 * default.
 */
export function shouldRetry(failureCount: number, error: unknown): boolean {
  const isClientError =
    error instanceof ApiError && error.status >= 400 && error.status < 500;
  return !isClientError && failureCount < 3;
}

/** The agent list — server-filtered to what the caller may see
 * (access-control Task B2), plus the room's `groups_unavailable` banner
 * flag. Polls at the adaptive cadence while any VISIBLE agent is active
 * (`listRefetchInterval` only ever sees the `agents` array, same as
 * before this task — the poll cadence has nothing to do with the flag). */
export function agentsQuery() {
  return queryOptions({
    queryKey: ["agents"] as const,
    queryFn: listAgents,
    refetchInterval: (query) => listRefetchInterval(query.state.data?.agents),
  });
}

/** One agent's detail. Polls while that agent itself is non-terminal. */
export function agentQuery(name: string) {
  return queryOptions({
    queryKey: ["agents", name] as const,
    queryFn: () => getAgent(name),
    refetchInterval: (query) => detailRefetchInterval(query.state.data),
  });
}

/** How long a catalog search result (skill or persona) is considered fresh
 * before a refetch — short enough that a newly-created persona/skill shows
 * up promptly, long enough to avoid refetching on every keystroke-driven
 * re-render of the same query key. */
const CATALOG_SEARCH_STALE_MS = 15_000;

/** How long org status is considered fresh. Bootstrap wiring changes rarely
 * (a human ran a CLI step), so this can sit longer than a catalog search. */
const ORG_STATUS_STALE_MS = 30_000;

/** The model list + default. No polling — the model catalog doesn't change
 * on its own in the background. */
/** The room's user guide (`GET /api/guide`). Static for the life of the
 * process, so it never goes stale in the client. */
export function guideQuery() {
  return queryOptions({
    queryKey: ["guide"] as const,
    queryFn: fetchGuide,
    staleTime: Infinity,
  });
}

export function modelsQuery() {
  return queryOptions({
    queryKey: ["catalog", "models"] as const,
    queryFn: fetchModels,
  });
}

/** The org's observability catalog (Task 4 of the observability-catalog
 * addendum: `GET /api/catalog/observability`) -- a dashboard base URL +
 * project, both `null` unless the room config carries both. Long
 * `staleTime`, same "doesn't change on its own in the background" reasoning
 * as `modelsQuery`: this config only changes on a room
 * roll. Shared across every mounted `AgentCard` and the detail page under
 * one cache entry (`["catalog", "observability"]`), so many cards on the
 * dashboard grid cost one network request, not one each. */
export function observabilityQuery() {
  return queryOptions({
    queryKey: ["catalog", "observability"] as const,
    queryFn: fetchObservability,
    staleTime: 5 * 60_000,
  });
}

/** The room's named ACL groups (access-control Task B2) — `[]` when the
 * room hasn't configured any, in which case the create modal's group
 * picker doesn't render at all. Same "doesn't change on its own in the
 * background" reasoning as `modelsQuery`: no polling. */
export function groupsQuery() {
  return queryOptions({
    queryKey: ["catalog", "groups"] as const,
    queryFn: fetchGroups,
  });
}

/** The team-slug list, for the team picker in the create/persona-builder
 * flows. */
export function teamsQuery() {
  return queryOptions({
    queryKey: ["catalog", "teams"] as const,
    queryFn: fetchTeams,
  });
}

/**
 * The skill-owner team pool + default (persona-metadata plan), for the
 * Edit details modal's team field (the create form stopped asking; team
 * defaults server-side) —
 * a DIFFERENT query from `teamsQuery` above (which reads `/api/catalog/teams`,
 * spanning both catalog kinds for search filtering). No polling/staleTime
 * override: same "doesn't change on its own in the background" reasoning
 * as `modelsQuery`. The Edit details modal (`MetadataDetails`) gates it on its own `open`
 * state via a local writable/derived pair (same "only fetch once actually
 * needed" idiom `slackManifestQuery` documents), since most agent-detail
 * visits never open that modal.
 */
export function teamOptionsQuery() {
  return queryOptions({
    queryKey: ["teams"] as const,
    queryFn: fetchTeamOptions,
  });
}

/** A skill catalog search for the given query/team. Keyed on both so each
 * distinct search has its own cache entry. */
export function skillsSearchQuery(q: string, team: string | null) {
  return queryOptions({
    queryKey: ["catalog", "skills", { q, team }] as const,
    queryFn: () => searchSkills(q, team),
    staleTime: CATALOG_SEARCH_STALE_MS,
  });
}

/** Recommended skills for a committed persona (create-flow polish,
 * 2026-08-28). Key carries the persona text so a different persona pick
 * refetches; disabled until there is any text to match on. */
export function recommendedSkillsQuery(who: string, what: string) {
  return queryOptions({
    queryKey: ["catalog", "skills", "recommended", { who, what }] as const,
    queryFn: () => fetchRecommendedSkills(who, what),
    enabled: who.trim().length > 0 || what.trim().length > 0,
    staleTime: 60_000,
  });
}

/** A persona catalog search for the given query/team/scope. Keyed on all
 * three so each distinct search has its own cache entry. `scope` defaults
 * to "mine" (the shared personas plus the caller's own groups), the same
 * default the server takes. */
export function personasSearchQuery(
  q: string,
  team: string | null,
  scope: PersonaScope = "mine",
) {
  return queryOptions({
    queryKey: ["catalog", "personas", { q, team, scope }] as const,
    queryFn: () => searchPersonas(q, team, scope),
    staleTime: CATALOG_SEARCH_STALE_MS,
  });
}

/** The ids of the personas this caller may re-file (`GET
 * /api/personas/movable`): which personas get a Move
 * action. */
export function movablePersonasQuery() {
  return queryOptions({
    queryKey: ["personas", "movable"] as const,
    queryFn: movablePersonas,
  });
}

/** Re-files one persona under a new team (`PATCH /api/personas/{id}`). A
 * move changes which list a persona shows up in and the team on its card,
 * so every persona search, the team list, the movable list and the detail
 * cards are refreshed. */
export function movePersonaMutation(queryClient: QueryClient) {
  return {
    mutationFn: ({ id, team }: { id: string; team: string }) => movePersona(id, team),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["catalog", "personas"] });
      queryClient.invalidateQueries({ queryKey: ["catalog", "teams"] });
      queryClient.invalidateQueries({ queryKey: ["personas", "movable"] });
      queryClient.invalidateQueries({ queryKey: ["personas", "detail"] });
    },
  };
}

/**
 * One whole persona (`GET /api/personas/{id}`) — what the hover card in the
 * create dialog's persona list reads.
 *
 * Disabled until `id` is non-null, which is the whole point: the card's
 * request must not leave before a human has actually rested on a row for
 * the hover delay, so the caller passes null until then (the same
 * "don't fetch until someone asks" idiom `archivedPersonasQuery` above and
 * `groupRequestsQuery` both use).
 *
 * Five minutes of freshness because a persona's own text barely moves —
 * far longer than a catalog search, and long enough that sweeping back and
 * forth across the same list costs exactly one request per persona.
 */
export function personaQuery(id: string | null) {
  return queryOptions({
    queryKey: ["personas", "detail", id] as const,
    queryFn: () => fetchPersona(id as string),
    enabled: id !== null,
    staleTime: 5 * 60_000,
  });
}

/**
 * Every archived persona (hardening wave archive lane) — `GET
 * /api/personas/archived`. Disabled until the "Show archived" toggle is on
 * (Personas.svelte) — same "don't fetch until a human actually asks to see
 * it" idiom `slackManifestQuery`'s `opened` flag / `groupRequestsQuery`'s
 * `enabled` flag both already document in this file: most catalog visits
 * never open it.
 */
export function archivedPersonasQuery(enabled: boolean) {
  return queryOptions({
    queryKey: ["personas", "archived"] as const,
    queryFn: archivedPersonas,
    enabled,
  });
}

/**
 * Archives a persona (hardening wave archive lane); on success, invalidates
 * the active persona search (the server's own ingest already excludes an
 * archived entry — this just catches kobo's own cache up so it drops out of
 * the catalog immediately) and the archived list (so it shows up there).
 */
export function archivePersonaMutation(queryClient: QueryClient) {
  return {
    mutationFn: (id: string) => archivePersona(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["catalog", "personas"] });
      queryClient.invalidateQueries({ queryKey: ["personas", "archived"] });
    },
  };
}

/** Unarchives a persona; same invalidation as `archivePersonaMutation` above. */
export function unarchivePersonaMutation(queryClient: QueryClient) {
  return {
    mutationFn: (id: string) => unarchivePersona(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["catalog", "personas"] });
      queryClient.invalidateQueries({ queryKey: ["personas", "archived"] });
    },
  };
}

/** Org bootstrap/wiring status, shown on the admin/org-status view. */
export function orgStatusQuery() {
  return queryOptions({
    queryKey: ["org", "status"] as const,
    queryFn: fetchOrgStatus,
    staleTime: ORG_STATUS_STALE_MS,
  });
}

/**
 * The signed-in identity this room's server has already verified
 * (access-control Task A1) — `GET /api/whoami`. `staleTime: Infinity`
 * because identity does not change mid-session: once fetched, there's no
 * background event that would make a refetch show anything different (a
 * fresh sign-in is a fresh page load, which starts a fresh QueryClient
 * anyway), unlike `orgStatusQuery`'s wiring state which a human can change
 * out from under a long-lived tab.
 */
export function adminIntegrationsQuery() {
  return queryOptions({
    queryKey: ["admin", "integrations"] as const,
    queryFn: fetchAdminIntegrations,
    staleTime: 15_000,
  });
}

export function connectAdminIntegrationMutation(queryClient: QueryClient) {
  return {
    mutationFn: (input: AdminConnectIntegration) => connectAdminIntegration(input),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["admin", "integrations"] });
      queryClient.invalidateQueries({ queryKey: ["admin", "audit"] });
    },
  };
}

export function disconnectAdminIntegrationMutation(queryClient: QueryClient) {
  return {
    mutationFn: (id: string) => disconnectAdminIntegration(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["admin", "integrations"] });
      queryClient.invalidateQueries({ queryKey: ["admin", "audit"] });
    },
  };
}

export function adminRolesQuery() {
  return queryOptions({
    queryKey: ["admin", "roles"] as const,
    queryFn: fetchAdminRoles,
    staleTime: 15_000,
  });
}

export function putAdminRoleMutation(queryClient: QueryClient) {
  return {
    mutationFn: ({ id, body }: { id: string; body: AdminRoleBody }) => putAdminRole(id, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["admin", "roles"] });
      queryClient.invalidateQueries({ queryKey: ["admin", "audit"] });
    },
  };
}

export function deleteAdminRoleMutation(queryClient: QueryClient) {
  return {
    mutationFn: (id: string) => deleteAdminRole(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["admin", "roles"] });
      queryClient.invalidateQueries({ queryKey: ["admin", "audit"] });
    },
  };
}

export function reloadAdminOrgMutation(queryClient: QueryClient) {
  return {
    mutationFn: () => reloadAdminOrg(),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["admin"] });
      queryClient.invalidateQueries({ queryKey: ["catalog", "models"] });
      queryClient.invalidateQueries({ queryKey: ["whoami"] });
    },
  };
}

export function adminPeopleQuery() {
  return queryOptions({
    queryKey: ["admin", "people"] as const,
    queryFn: fetchAdminPeople,
    staleTime: 60_000,
  });
}

export function adminAuditQuery() {
  return queryOptions({
    queryKey: ["admin", "audit"] as const,
    queryFn: fetchAdminAudit,
    staleTime: 15_000,
  });
}

export function adminAddonsQuery() {
  return queryOptions({
    queryKey: ["admin", "addons"] as const,
    queryFn: fetchAdminAddons,
    staleTime: 60_000,
  });
}

export function whoamiQuery() {
  return queryOptions({
    queryKey: ["whoami"] as const,
    queryFn: fetchWhoami,
    staleTime: Infinity,
  });
}

/**
 * Three name-suggestion cards for the naming station (create flow v2
 * "naming ceremony" addendum, narrowed from five to three by
 * create-identity), derived from the just-committed persona's own
 * `who_they_are`/`what_they_do` text. `enabled` is false until both fields
 * are non-empty — there's nothing to suggest against before a persona is
 * picked, same "nothing to fetch yet" gate `deliveryQuery` uses for its own
 * sha. No polling. `staleTime: Infinity` because the builder path PREFETCHES
 * this key while the create-persona POST is still in flight (naming-latency
 * fix) — without it, the station's own mount would refetch a just-settled
 * prefetch and double the model call.
 *
 * `personaId`/`exclude`/`round` (create-identity, Task 4) all ride the
 * query key alongside `whoTheyAre`/`whatTheyDo`: a catalog pick sends
 * `personaId` and lets the server resolve the real body (see
 * `NameSuggestionsRequest`'s own doc comment in api.ts); the builder lane
 * has no persisted id yet at prefetch time, so it sends the drafted body
 * itself instead. `exclude`/`round` change on every "Offer more" click
 * (CreateModal's `offeredNames`/`offerRound`), so each round addresses its
 * own cache entry rather than colliding with (or silently reusing) the
 * previous one — the naming station's "Offer more" action bumps `round`
 * rather than calling a bare `refetch()`, since `refetch()` alone would
 * re-run the SAME key and ignore `exclude` entirely.
 */
export function nameSuggestionsQuery(params: {
  whoTheyAre: string;
  whatTheyDo: string;
  personaId?: string | null;
  exclude?: string[];
  round?: number;
}) {
  const whoTheyAre = params.whoTheyAre;
  const whatTheyDo = params.whatTheyDo;
  const personaId = params.personaId ?? null;
  const exclude = params.exclude ?? [];
  const round = params.round ?? 0;
  return queryOptions({
    queryKey: [
      "names",
      "suggestions",
      {
        who_they_are: whoTheyAre,
        what_they_do: whatTheyDo,
        persona_id: personaId,
        exclude,
        round,
      },
    ] as const,
    queryFn: () =>
      fetchNameSuggestions({ whoTheyAre, whatTheyDo, personaId, exclude, round }),
    staleTime: Infinity,
    enabled: whoTheyAre.trim().length > 0 && whatTheyDo.trim().length > 0,
  });
}

/**
 * Drafts a persona from one plain job description (+ optional short rule
 * hints) — the Persona v3 "describe the job, approve the soul" step
 * (`POST /api/personas/draft`). No `onSuccess`/cache invalidation: the
 * route never touches storage (`kobo_api.main.draft_persona_route`'s own
 * docstring — nothing is created until the operator approves the preview
 * and `createPersonaMutation` below actually runs), so there's nothing in
 * the query cache for a pure generator call to invalidate. No
 * `queryClient` param for the same reason `chatMutation` above has none.
 */
export function draftPersonaMutation() {
  return {
    mutationFn: ({ job, rules }: { job: string; rules: string[] }) =>
      draftPersona(job, rules),
  };
}

/**
 * Distills discovery metadata from an operator-supplied persona document
 * (persona-first station addendum) — `POST /api/personas/distill`. Same
 * "no onSuccess/cache invalidation" shape as `draftPersonaMutation` above,
 * for the same reason: the route never touches storage
 * (`kobo_api.main.distill_persona_route`'s own docstring), so there's
 * nothing in the query cache for a pure generator call to invalidate.
 */
export function distillPersonaMutation() {
  return {
    mutationFn: (body: string) => distillPersona(body),
  };
}

/**
 * Creates a persona; on success, invalidates both the persona search
 * queries (so the new persona appears in results) and the team list.
 * Every persona now lands under whatever team the server defaults to
 * (`"shared"` — `CreatePersonaRequest.team`, Persona v3 / teamless-create
 * addendum) since the create flow no longer sends one, but the team-list
 * invalidation is left in place: a harmless no-op today, and still correct
 * if a persona can once again carry an explicit team. TanStack's
 * `invalidateQueries` match is a *prefix* match, so `["catalog",
 * "personas"]` covers every `["catalog", "personas", {q, team}]`
 * search-result cache entry, not just one.
 */
export function createPersonaMutation(queryClient: QueryClient) {
  return {
    mutationFn: (body: PersonaForm) => createPersona(body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["catalog", "personas"] });
      queryClient.invalidateQueries({ queryKey: ["catalog", "teams"] });
    },
  };
}

/** Creates an agent; on success, invalidates the agent list so it appears. */
export function createAgentMutation(queryClient: QueryClient) {
  return {
    mutationFn: createAgent,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
  };
}

/**
 * The Repository field's typeahead. A query, not a mutation: the same
 * search from two people is the same answer, and the room's own listing
 * behind it is cached for five minutes anyway.
 *
 * The empty query is a real one now: it answers with the repositories
 * pushed to most recently, which is what the field shows the moment
 * somebody clicks into it. `enabled` is the form's own open/shut state
 * rather than a rule about the text, so a modal nobody has opened still
 * asks the room nothing.
 */
export function connectReposQuery(q: string, enabled = true) {
  const trimmed = q.trim();
  return queryOptions({
    queryKey: ["connect", "repos", trimmed] as const,
    queryFn: () => searchConnectRepos(trimmed),
    enabled,
  });
}

/**
 * Reads a repository the room has been asked to connect (connected-agents
 * design 2026-09-21). A mutation rather than a query even though it changes
 * nothing: it runs when a person presses Scan, never on mount, and its
 * answer is about the repository at that moment -- a cached report is a
 * claim about a repository some minutes ago, which is exactly why the
 * connect route runs its own scan again rather than trusting this one.
 * Nothing to invalidate, so no QueryClient.
 */
export function scanRepoMutation() {
  return {
    mutationFn: scanRepo,
  };
}

/** Connects a team's own repository as an agent; on success, invalidates
 * the agent list so the new row appears, same as `createAgentMutation`. */
export function connectAgentMutation(queryClient: QueryClient) {
  return {
    mutationFn: (body: ConnectAgentBody) => connectAgent(body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
  };
}

/**
 * Retries a failed agent's provisioning; on success, invalidates the agent
 * list. TanStack's default `invalidateQueries` match is a *prefix* match
 * (see `matchQuery`/`partialMatchKey` in @tanstack/query-core), so
 * invalidating `["agents"]` already covers `["agents", name]` too — a
 * separate call for the detail key would just be a redundant no-op.
 */
export function retryAgentMutation(queryClient: QueryClient, name: string) {
  return {
    mutationFn: () => retryAgent(name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
  };
}

/**
 * Starts (or resumes) an agent's destroy ledger; on success, invalidates the
 * agent list. Same broad-key reasoning as `retryAgentMutation` above (a
 * prefix match already covers `["agents", name]`), the list needs it too,
 * since a destroying/destroy_failed agent's status shows there as well as
 * on its own detail page. This is the one mutation the Danger zone's
 * "Destroy this agent" and "Destroy again" buttons both call: the same
 * request either way, since resume is entirely server-side.
 */
export function destroyAgentMutation(queryClient: QueryClient, name: string) {
  return {
    mutationFn: () => destroyAgent(name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
  };
}

/**
 * Turns an agent the room composed into a connected one (connected-agents
 * design 2026-09-21): the operator action for an agent whose repository has
 * stopped running the room's runtime. Invalidates `["agents"]` on success,
 * the same broad key the two mutations above use and for the same reason:
 * the kind changes what the detail page offers at all, and the card in the
 * list carries the marker too.
 */
export function convertToConnectedMutation(
  queryClient: QueryClient,
  name: string,
) {
  return {
    mutationFn: (runMode: string) => convertToConnected(name, runMode),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
  };
}

/**
 * Moves one agent onto a runtime image (harness wave, 2026-09-23) -- in the
 * room's UI, always the room's own current one, from the Harness tab's
 * "Update to current".
 *
 * Invalidates `["agents", name]` so the state line under it stops saying
 * Behind: the runtime the detail route reports is the row this call moved.
 * Narrow rather than the broad `["agents"]` the three mutations above use,
 * because no dashboard card shows a runtime and the list route does not
 * serve one.
 */
export function updateAgentModelMutation(queryClient: QueryClient, name: string) {
  return {
    mutationFn: (model: string) => patchAgentModel(name, model),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents", name] });
      queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
  };
}

export function colleagueModeMutation(queryClient: QueryClient, name: string) {
  return {
    mutationFn: (mode: ColleagueMode) => putColleagueMode(name, mode),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents", name] });
    },
  };
}

export function updateAgentRuntimeMutation(queryClient: QueryClient, name: string) {
  return {
    mutationFn: (runtimeImage: string) => patchAgentRuntime(name, runtimeImage),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents", name] });
    },
  };
}

/**
 * Sends one chat turn to an agent. Unlike the mutations above, this has no
 * `onSuccess`/cache invalidation: a chat exchange doesn't change any agent
 * state the rest of the app reads (status, steps, ...), so there's nothing
 * in the query cache for it to invalidate — hence no `queryClient` param.
 */
export function chatMutation(name: string) {
  return {
    mutationFn: (messages: ChatMessage[]) => chatWithAgent(name, messages),
  };
}

/**
 * Turns one distilled chat exchange into a taught memory: the "Teach this"
 * gesture (Task 2) over Task 1's route, `POST /api/agents/{name}/teach`.
 * `TeachModal` posts the operator's EDITED textarea content verbatim (not
 * necessarily the original prefill) as `text`. On success, invalidates that
 * agent's intake query so the fresh one-shot upload (and its "pending"
 * status) shows up if the Memory tab happens to already be mounted —
 * AgentDetail keeps every tab's panel mounted, hidden rather than destroyed,
 * per its own tablist doc comment — same invalidate-the-list shape
 * `uploadIntakeMutation` uses for a plain intake upload, since this is just
 * another door into the same `bundle/intake/` files.
 */
export function teachMutation(queryClient: QueryClient, name: string) {
  return {
    mutationFn: (text: string) => teachAgent(name, text),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents", name, "intake"] });
    },
  };
}

/** An agent's knowledge files. No polling — edits are user-driven, not
 * something that changes on its own in the background. */
export function knowledgeQuery(name: string) {
  return queryOptions({
    queryKey: ["agents", name, "knowledge"] as const,
    queryFn: () => listKnowledge(name),
  });
}

/**
 * Saves (creates or updates) one knowledge file; on success, invalidates
 * that agent's knowledge query so the saved content (and its new sha) is
 * picked up.
 */
export function saveKnowledgeMutation(queryClient: QueryClient, name: string) {
  return {
    mutationFn: ({
      filename,
      content,
    }: {
      filename: string;
      content: string;
    }) => saveKnowledgeFile(name, filename, content),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["agents", name, "knowledge"],
      });
    },
  };
}

/**
 * Deletes one knowledge file; on success, invalidates that agent's
 * knowledge query so the removed file drops out of the list.
 */
export function deleteKnowledgeMutation(
  queryClient: QueryClient,
  name: string,
) {
  return {
    mutationFn: (filename: string) => deleteKnowledgeFile(name, filename),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["agents", name, "knowledge"],
      });
    },
  };
}

/** The agent's learnings queue, every status -- the panel splits pending
 * from history itself. No polling; verdicts invalidate. */
export function learningsQuery(name: string) {
  return queryOptions({
    queryKey: ["agents", name, "learnings"] as const,
    queryFn: () => listLearnings(name),
  });
}

/** One verdict. Invalidates ["agents", name] -- prefix-matching covers
 * both the learnings list and the detail (whose pending_learnings badge
 * count changed). */
export function decideLearningMutation(queryClient: QueryClient, name: string) {
  return {
    mutationFn: ({
      id,
      action,
      content,
      confirm,
    }: {
      id: number;
      action: "approve" | "deny";
      content?: string;
      confirm?: boolean;
    }) => decideLearning(name, id, action, content, confirm),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents", name] });
    },
  };
}

/** The select-all gesture: many verdicts, one request, same invalidation. */
export function decideLearningsBulkMutation(
  queryClient: QueryClient,
  name: string,
) {
  return {
    mutationFn: ({ ids, action }: { ids: number[]; action: "approve" | "deny" }) =>
      decideLearningsBulk(name, ids, action),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents", name] });
    },
  };
}

/** Poll cadence while a knowledge delivery is still in flight. */
const DELIVERY_POLL_MS = 3500;

/**
 * Refetch interval for the delivery-status query. Polls while the latest
 * known state is still mid-flight (building/rolling), and stops once it
 * reaches a terminal state (delivered/failed). `data` is undefined before
 * the first fetch resolves (or while the query is disabled) — nothing to
 * poll for yet, so that's `false` too.
 */
export function deliveryRefetchInterval(data?: {
  state: string;
  detail: string | null;
}): number | false {
  if (data === undefined) {
    return false;
  }
  return data.state === "building" || data.state === "rolling"
    ? DELIVERY_POLL_MS
    : false;
}

/**
 * A knowledge delivery's rollout state for a given content sha. Disabled
 * until a sha exists (before the first save, there's nothing to check yet).
 */
export function deliveryQuery(name: string, sha: string | null) {
  return queryOptions({
    queryKey: ["agents", name, "knowledge", "delivery", sha] as const,
    queryFn: () => fetchDeliveryState(name, sha as string),
    enabled: sha !== null,
    refetchInterval: (query) => deliveryRefetchInterval(query.state.data),
  });
}

/** An agent's skills — catalog picks and local guardrail files alike. No
 * polling — same reasoning as `knowledgeQuery`: edits here are user-driven,
 * never something that changes in the background on its own. */
export function agentSkillsQuery(name: string) {
  return queryOptions({
    queryKey: ["agents", name, "skills"] as const,
    queryFn: () => listSkills(name),
  });
}

/**
 * Picks a skill from the org catalog for this agent; on success, invalidates
 * that agent's skills query so the new pick (and its provenance line) shows
 * up.
 */
export function addCatalogSkillMutation(queryClient: QueryClient, name: string) {
  return {
    mutationFn: ({ skillName, team }: { skillName: string; team: string }) =>
      addCatalogSkill(name, skillName, team),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents", name, "skills"] });
    },
  };
}

/**
 * Removes a skill — catalog pick or local guardrail alike (the server's one
 * unified delete path for both); on success, invalidates that agent's
 * skills query so the removed entry drops out of the list.
 */
export function deleteSkillMutation(queryClient: QueryClient, name: string) {
  return {
    mutationFn: (skillId: string) => deleteSkill(name, skillId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents", name, "skills"] });
    },
  };
}

/**
 * Saves (creates or overwrites) one local guardrail skill's content; on
 * success, invalidates that agent's skills query so the saved content (and
 * its new sha) is picked up.
 */
export function saveLocalSkillMutation(queryClient: QueryClient, name: string) {
  return {
    mutationFn: ({ skillId, content }: { skillId: string; content: string }) =>
      saveLocalSkill(name, skillId, content),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents", name, "skills"] });
    },
  };
}

/** An agent's intake files — one-shot uploads awaiting the runtime seeder.
 * No polling of its own here; the refetch cadence lives in
 * `intakeRefetchInterval` below, wired through `refetchInterval` the same
 * way `deliveryQuery` wires `deliveryRefetchInterval`. */
export function intakeQuery(name: string) {
  return queryOptions({
    queryKey: ["agents", name, "intake"] as const,
    queryFn: () => listIntake(name),
    refetchInterval: (query) => intakeRefetchInterval(query.state.data),
  });
}

/** Poll cadence while at least one intake file is still awaiting the
 * runtime seeder. */
const INTAKE_POLL_MS = 5000;

/**
 * Refetch interval for the intake list query. Polls while any file in the
 * list is still `"pending"` (the seeder hasn't ingested it yet), and stops
 * once every file has reached `"ingested"` — same shape as
 * `deliveryRefetchInterval`. `data` is undefined before the first fetch
 * resolves (nothing to poll for yet), and an empty list has no pending file
 * by definition — both are `false`.
 */
export function intakeRefetchInterval(data?: IntakeFile[]): number | false {
  if (data === undefined) {
    return false;
  }
  return data.some((file) => file.status === "pending") ? INTAKE_POLL_MS : false;
}

/**
 * Uploads (creates or overwrites) one intake file; on success, invalidates
 * that agent's intake query so the new file (and its fresh "pending"
 * status) shows up.
 */
export function uploadIntakeMutation(queryClient: QueryClient, name: string) {
  return {
    mutationFn: ({ filename, content }: { filename: string; content: string }) =>
      uploadIntake(name, filename, content),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents", name, "intake"] });
    },
  };
}

/**
 * Removes an intake file's source upload; on success, invalidates that
 * agent's intake query so the removed entry drops out of the list.
 */
export function deleteIntakeMutation(queryClient: QueryClient, name: string) {
  return {
    mutationFn: (filename: string) => deleteIntake(name, filename),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents", name, "intake"] });
    },
  };
}

/** Poll cadence while at least one tooling file is still undelivered. */
const TOOLING_POLL_MS = 5000;

/**
 * Refetch interval for the tooling list query. Polls while any file in the
 * response is still `delivered: false`, and stops once every file has
 * reached `delivered: true` — same shape as `intakeRefetchInterval`. `data`
 * is undefined before the first fetch resolves (nothing to poll for yet),
 * and an empty file list has no undelivered file by definition — both are
 * `false`. Driven purely by `files` delivery, same as before Task 4 —
 * `enforcement` (attested/locked) never affects polling cadence.
 */
export function toolingRefetchInterval(data?: ToolingListResponse): number | false {
  if (data === undefined) {
    return false;
  }
  return data.files.some((file) => file.delivered === false)
    ? TOOLING_POLL_MS
    : false;
}

/** An agent's tooling files plus the runtime's enforcement envelope (Task 4:
 * card probe + banner graduation) — the staged door for its LAW-to-be
 * (2026-08-13 addendum). Polls while any file is undelivered, stops once all
 * are delivered — same cadence as `intakeQuery` polls pending files. */
export function toolingQuery(name: string) {
  return queryOptions({
    queryKey: ["agents", name, "tooling"] as const,
    queryFn: () => listTooling(name),
    refetchInterval: (query) => toolingRefetchInterval(query.state.data),
  });
}

/**
 * Uploads (creates or overwrites) one tooling file; on success, invalidates
 * that agent's tooling query so the saved file (and its new sha/delivery
 * state) is picked up.
 */
export function uploadToolingMutation(queryClient: QueryClient, name: string) {
  return {
    mutationFn: ({ filename, content }: { filename: string; content: string }) =>
      uploadTooling(name, filename, content),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents", name, "tooling"] });
    },
  };
}

/**
 * Imports every `.py`/`.go` file that passes validation from some other
 * repo's folder in one delivery; on success, invalidates that agent's
 * tooling query so any newly-imported files show up. Invalidates
 * unconditionally, even when nothing passed (`sha: null`) — a harmless
 * no-op refetch, and it keeps this mutation's success handling uniform with
 * upload/delete rather than branching on the result shape.
 */
export function importToolingMutation(queryClient: QueryClient, name: string) {
  return {
    mutationFn: ({ repo, path }: { repo: string; path: string }) =>
      importTooling(name, repo, path),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents", name, "tooling"] });
    },
  };
}

/**
 * Removes a tooling file; on success, invalidates that agent's tooling query
 * so the removed file drops out of the list.
 */
export function deleteToolingMutation(queryClient: QueryClient, name: string) {
  return {
    mutationFn: (filename: string) => deleteTooling(name, filename),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents", name, "tooling"] });
    },
  };
}

/**
 * Finishes Slack connect (app id + tokens — Slack self-service, 2026-08-17
 * added `app_id`; two-lane Slack, 2026-08-17 had already moved the channel
 * list off this call, see `connectSlack`'s own doc) for the agent; on
 * success, invalidates that agent's detail query so the new `slack.app_id`/
 * `slack.state` become visible.
 */
export function connectSlackMutation(queryClient: QueryClient, name: string) {
  return {
    mutationFn: (body: { app_id: string; bot_token: string; app_token: string }) =>
      connectSlack(name, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents", name] });
    },
  };
}

/**
 * An agent's rendered Slack app manifest (Slack self-service, 2026-08-17) —
 * disabled until `opened` is true. The setup section now renders for every
 * signed-in user (no more `adminVisible` gate), and most agent-detail visits
 * aren't setup visits, so this stays unfetched until the human actually
 * clicks "Show the manifest" (`SlackPanel`'s own local `opened` state, wired
 * through a `writable`/`derived` pair the same way `deliveryQuery`'s sha
 * argument is — see that query's doc comment, or `MemoryPanel`'s, for why a
 * plain `$derived` options object wouldn't do under the installed
 * @tanstack/svelte-query adapter). No polling and no `staleTime` override:
 * a manifest render is static per agent, and once shown there's nothing
 * background-refreshing it.
 */
export function slackManifestQuery(name: string, opened: boolean) {
  return queryOptions({
    queryKey: ["agents", name, "slack", "manifest"] as const,
    queryFn: () => fetchSlackManifest(name),
    enabled: opened,
  });
}

/**
 * Saves the agent's full replacement Slack channel list; on success,
 * invalidates that agent's detail query so the new `slack.channels`/
 * `slack.state` (and, when a rewire fired, the fresh delivery sha) become
 * visible. Same PUT-then-invalidate shape as `saveKnowledgeMutation` above.
 */
export function saveSlackChannelsMutation(
  queryClient: QueryClient,
  name: string,
) {
  return {
    mutationFn: (channels: string[]) => saveSlackChannels(name, channels),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents", name] });
    },
  };
}

/**
 * Saves how Slack wakes the agent in a channel; invalidates the agent
 * detail (so `slack.wake` follows) AND the rendered manifest, whose event
 * list depends on the mode.
 */
export function setSlackWakeMutation(queryClient: QueryClient, name: string) {
  return {
    mutationFn: (wake: SlackWake) => setSlackWake(name, wake),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents", name] });
    },
  };
}

/**
 * Which secrets an agent holds, BY NAME (secrets and environment design
 * 2026-09-22) -- `GET .../secrets`, owner-or-operator only server-side.
 *
 * No polling by default, the same "edits are user-driven" reasoning
 * `knowledgeQuery` documents: nothing changes this list except a save
 * from this same panel, and the save's own `onSuccess` invalidates it.
 *
 * `refetchInterval` is the ONE exception, and it is not about the list:
 * a saved secret takes a little while to reach the agent, and the panel
 * polls for a short window after a save so the lock catches up on its
 * own rather than waiting for somebody to reopen the tab. The panel owns
 * that window and closes it; this factory only carries the number.
 *
 * `enabled` is whether the Secrets tab is the selected one, and it is not
 * an optimisation. Every tabpanel on the agent page stays MOUNTED when
 * another tab is picked, so without it, opening any agent page at all
 * would ask the secrets platform about an agent nobody was looking at and
 * leave a row in its audit trail saying somebody did. Same shape
 * `slackManifestQuery` uses for its own deferred read.
 */
export function agentSecretsQuery(
  name: string,
  enabled = true,
  refetchInterval: number | false = false,
) {
  return queryOptions({
    queryKey: ["agents", name, "secrets"] as const,
    queryFn: () => listAgentSecrets(name),
    enabled,
    refetchInterval,
  });
}

/**
 * Rolls the agent's pods -- `POST .../restart`, owner-or-operator only
 * server-side. Invalidates that agent's secrets query on success: the
 * ExternalSecret's own state is what the lock reads, and a restart is
 * exactly the moment somebody wants a fresh look at it.
 */
export function restartAgentMutation(queryClient: QueryClient, name: string) {
  return {
    mutationFn: () => restartAgent(name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents", name, "secrets"] });
    },
  };
}

/**
 * Adds, changes and removes secrets in one call; on success, invalidates
 * that agent's secrets query so the new key list is picked up. The values
 * passed here leave the browser once and nothing reads them back.
 */
export function saveAgentSecretsMutation(queryClient: QueryClient, name: string) {
  return {
    mutationFn: ({ set, remove }: { set: Record<string, string>; remove: string[] }) =>
      saveAgentSecrets(name, set, remove),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents", name, "secrets"] });
    },
  };
}

/** An agent's plain environment variables, keys and values --
 * `GET .../env`. Same no-polling reasoning as the secrets query above, and
 * the same `enabled`: this one reads the agent's repository, so an
 * unselected tab would spend a GitHub call per agent page opened. */
export function agentEnvQuery(name: string, enabled = true) {
  return queryOptions({
    queryKey: ["agents", name, "env"] as const,
    queryFn: () => listAgentEnv(name),
    enabled,
  });
}

/** Saves environment variables the same way; on success, invalidates that
 * agent's env query. */
export function saveAgentEnvMutation(queryClient: QueryClient, name: string) {
  return {
    mutationFn: ({ set, remove }: { set: Record<string, string>; remove: string[] }) =>
      saveAgentEnv(name, set, remove),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents", name, "env"] });
    },
  };
}

/**
 * An agent's cross-group colleagueship invites (access-control Task C2) --
 * `GET /api/agents/{name}/invites`, owner-or-operator only server-side. No
 * polling: this is the same "edits are user-driven" reasoning `knowledgeQuery`
 * documents, plus every mutation below invalidates this key on success. The
 * Access tab's own visibility (AgentDetail.svelte) is driven by whether THIS
 * query succeeds at all -- a 403/404 here means the tab doesn't render for
 * the current caller, per the access-control design spec's "visible to its
 * owner and operators" rule for the Access tab.
 */
export function invitesQuery(name: string) {
  return queryOptions({
    queryKey: ["agents", name, "invites"] as const,
    queryFn: () => listInvites(name),
  });
}

/**
 * Requests colleague access from one of the CALLER's OWN agents to another
 * agent -- both names travel with `mutate`, not bound into the factory like
 * every other agent-scoped mutation here, because the "Request colleague
 * access" affordance (RequestAccessCard.svelte) lets the human pick which
 * of their own agents is asking via a `<select>`, so the FROM side can
 * change between renders of the same mounted component. The SAME factory
 * backs AccessPanel's own browsable picker (access-tab refinement wave) --
 * there the FROM side is fixed (the viewed agent), but it's still passed
 * with `mutate` rather than bound in, so one mounted mutation serves every
 * row's "Request access" button. On success, invalidates the FROM agent's
 * own invites query so the fresh outgoing row shows up if its Access tab
 * happens to already be open elsewhere, and its requestable query so a
 * freshly-requested candidate moves from the browsable list into "already".
 */
export function createInviteMutation(queryClient: QueryClient) {
  return {
    mutationFn: ({ fromAgent, toAgent }: { fromAgent: string; toAgent: string }) =>
      createInvite(fromAgent, toAgent),
    onSuccess: (_result: Invite, variables: { fromAgent: string; toAgent: string }) => {
      queryClient.invalidateQueries({
        queryKey: ["agents", variables.fromAgent, "invites"],
      });
      queryClient.invalidateQueries({
        queryKey: ["agents", variables.fromAgent, "requestable"],
      });
    },
  };
}

/**
 * The other READY agents `name` could ask for colleague access (access-tab
 * refinement wave) -- `GET /api/agents/{name}/requestable`, owner-or-
 * operator only server-side, same visibility probe discipline `invitesQuery`
 * documents. No polling: `createInviteMutation` above invalidates this key
 * on a successful request, same as it already does for `invitesQuery`.
 */
export function requestableQuery(name: string) {
  return queryOptions({
    queryKey: ["agents", name, "requestable"] as const,
    queryFn: () => fetchRequestable(name),
  });
}

/**
 * A directory search for the "Add someone by email" autocomplete (access-tab
 * refinement wave) -- `GET /api/people/search`, disabled below two
 * characters so a bare keystroke or two never rounds trips (and never risks
 * the route's own empty-`q` 422). The route itself is fail-soft (a directory
 * outage or none configured both read as `{people: [], unavailable: true}`,
 * never a thrown error), so callers only ever branch on `data.unavailable`,
 * never `isError` for this one.
 */
export function peopleSearchQuery(q: string) {
  const trimmed = q.trim();
  return queryOptions({
    queryKey: ["people", "search", trimmed] as const,
    queryFn: () => searchPeople(trimmed),
    enabled: trimmed.length >= 2,
  });
}

/**
 * Accepts a pending incoming invite; on success, invalidates `name`'s own
 * invites query so the accepted row (and its new state) shows up.
 */
export function acceptInviteMutation(queryClient: QueryClient, name: string) {
  return {
    mutationFn: (inviteId: number) => acceptInvite(inviteId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents", name, "invites"] });
    },
  };
}

/** Declines a pending incoming invite; same invalidation as `acceptInviteMutation`. */
export function declineInviteMutation(queryClient: QueryClient, name: string) {
  return {
    mutationFn: (inviteId: number) => declineInvite(inviteId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents", name, "invites"] });
    },
  };
}

/**
 * Revokes a pending or accepted invite from either side; same invalidation
 * as `acceptInviteMutation` -- the one query every invite state change here
 * needs to refresh.
 */
export function revokeInviteMutation(queryClient: QueryClient, name: string) {
  return {
    mutationFn: (inviteId: number) => revokeInvite(inviteId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents", name, "invites"] });
    },
  };
}

/** An agent's per-user visibility grants (access-control Task C2) -- `GET
 * /api/agents/{name}/grants`, owner-or-operator only server-side. No polling,
 * same reasoning as `invitesQuery` above. */
export function grantsQuery(name: string) {
  return queryOptions({
    queryKey: ["agents", name, "grants"] as const,
    queryFn: () => listGrants(name),
  });
}

/**
 * Grants an email visibility into this agent; on success, invalidates this
 * agent's grants query so the new row (or the existing one, on the
 * idempotent re-grant path) shows up.
 */
export function addGrantMutation(queryClient: QueryClient, name: string) {
  return {
    mutationFn: (email: string) => addGrant(name, email),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents", name, "grants"] });
    },
  };
}

/** Removes a per-user visibility grant; same invalidation as `addGrantMutation`. */
export function removeGrantMutation(queryClient: QueryClient, name: string) {
  return {
    mutationFn: (email: string) => removeGrant(name, email),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents", name, "grants"] });
    },
  };
}

/**
 * Files a group request (shared-group + group-requests wave) -- the create
 * modal's "My group is not listed..." mini-form. No `queryClient`/`onSuccess`
 * invalidation: the filer is very often not an admin at all, so there's
 * nothing in THEIR cache this touches (the admin queue -- `groupRequestsQuery`
 * below -- lives behind a gate this caller may not even pass), and the
 * modal's own success handler drives the mini-form's confirmation copy
 * directly off the mutation result, not a refetched query.
 */
export function requestGroupMutation() {
  return {
    mutationFn: ({ groupName, note }: { groupName: string; note?: string }) =>
      requestGroup(groupName, note),
  };
}

/**
 * The admin queue of filed group requests (shared-group + group-requests
 * wave) -- `GET /api/group-requests`, admin-only server-side. `enabled`
 * gates the fetch behind the admin card's own expanded state
 * (`GroupRequestsCard.svelte`), same "don't fetch until a human actually
 * asks to see it" idiom `slackManifestQuery`'s `opened` flag uses -- the
 * card's mere presence already implies the caller is an admin (whoami only
 * ever sends `pending_group_requests` to one), but there's still no reason
 * to fetch the full list before it's expanded.
 */
export function groupRequestsQuery(enabled: boolean) {
  return queryOptions({
    queryKey: ["group-requests"] as const,
    queryFn: fetchGroupRequests,
    enabled,
  });
}

/**
 * Closes a pending group request out, "done" or "declined" (shared-group +
 * group-requests wave); on success, invalidates the admin queue itself so
 * the decided row drops off, and `whoami` so the sidebar/dashboard badge's
 * `pending_group_requests` count catches up -- `whoami`'s own `staleTime:
 * Infinity` only suppresses automatic background refetches, not an explicit
 * invalidation like this one, so an admin who just acted on a request sees
 * the count move without a page reload.
 */
export function decideGroupRequestMutation(queryClient: QueryClient) {
  return {
    mutationFn: ({ id, action }: { id: number; action: "done" | "declined" }) =>
      decideGroupRequest(id, action),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["group-requests"] });
      queryClient.invalidateQueries({ queryKey: ["whoami"] });
    },
  };
}

/**
 * Edit details' save (persona-metadata plan Task 3/4): PATCHes whichever of
 * team/purpose_tags/capability are submitted; on success, invalidates the
 * agent detail query so the saved values (and the card's fresh delivery
 * sha) become visible. The response itself already carries the saved
 * values plus `sha`, so `MetadataDetails` re-seeds its own draft state from
 * the result directly rather than waiting on the invalidated refetch.
 */
export function patchMetadataMutation(queryClient: QueryClient, name: string) {
  return {
    mutationFn: (body: {
      team?: string;
      purpose_tags?: string[];
      capability?: string;
    }) => patchAgentMetadata(name, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents", name] });
    },
  };
}

/**
 * Regenerates the agent's capability line against its current skills/
 * tooling; on success, invalidates the agent detail query, same shape as
 * `patchMetadataMutation` above. No arguments beyond the agent name itself —
 * `team`/`purpose_tags` are untouched by regenerate.
 */
export function regenerateMetadataMutation(
  queryClient: QueryClient,
  name: string,
) {
  return {
    mutationFn: () => regenerateAgentMetadata(name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents", name] });
    },
  };
}

/**
 * Generates (or regenerates) the agent's picture; on success, invalidates
 * the agent list query. Unlike `connectSlackMutation`/`saveSlackChannelsMutation`
 * above (which invalidate only the detail key, since the list carries no
 * slack state), `has_avatar` is shown on BOTH the list (AgentCard) and the
 * detail (AgentDetail header) — so both need to pick up the flip from
 * false to true. TanStack's `invalidateQueries` match is a *prefix* match
 * (see `retryAgentMutation` above), so invalidating `["agents"]` alone
 * already covers `["agents", name]` too; a second call for the detail key
 * would be a redundant no-op, same reasoning as `retryAgentMutation`.
 */
export function generateAvatarMutation(queryClient: QueryClient, name: string) {
  return {
    mutationFn: () => generateAvatar(name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
  };
}

/**
 * Uploads a human-provided picture (raw PNG bytes, read client-side from a
 * file input). Same invalidation as `generateAvatarMutation` above and for
 * the same reason: `has_avatar` is rendered on both the list (AgentCard) and
 * the detail (AgentDetail header), so the broad `["agents"]` key covers both.
 */
export function uploadAvatarMutation(queryClient: QueryClient, name: string) {
  return {
    mutationFn: (png: ArrayBuffer) => uploadAvatar(name, png),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
  };
}

/**
 * The terminal's one flattened, chronological feed (terminal redesign,
 * replacing the old card grid's `colleagueConversationsQuery`/
 * `colleagueConversationQuery` pair). No polling of its own: live updates
 * come from the SSE stream instead (`ColleagueConversations.svelte`
 * refetches this query on any frame from `/api/colleagues/stream` — "on any
 * SSE message refetch the feed", no client-side event merging, same
 * discipline the old card grid used).
 *
 * A 409 (this room has no switchboard wired) surfaces through `$query.
 * error` as an `ApiError` with `status === 409`; the component treats that
 * specifically as "render nothing" rather than an error state.
 *
 * `agent` is always passed -- the caller (`ColleagueConversations.svelte`)
 * is mounted under one specific agent's Chat tab, so the terminal only ever
 * shows conversations THAT agent participates in (also the server's own
 * `_visible_agent_or_404` gate: an agent this caller can't see 404s here,
 * unlike the room-wide routes the old card grid read from).
 */
export function colleagueFeedQuery(agent: string) {
  return queryOptions({
    queryKey: ["colleagues", "feed", agent] as const,
    queryFn: () => fetchColleagueFeed(agent),
  });
}

// --- Threads (threads arc, Task 10): a shared multi-agent conversation ----
// started from an agent's own chat context. No query/cache invalidation on
// any of the three mutations below -- same "nothing in the query cache to
// invalidate" reasoning `chatMutation` above documents, since a thread's
// turn-by-turn exchange is local component state (see the module doc
// comment's own note on this), not something any `queryOptions` here reads
// back.

/**
 * Starts a new thread naming one agent (`ThreadPanel`'s own "Start a shared
 * thread" affordance) -- `POST /api/threads`. Open to any signed-in caller
 * who can see `agent`, never an ownership gate on it (a thread is the
 * initiator's own space, not a mutation of the named agent).
 */
export function createThreadMutation() {
  return {
    mutationFn: (agent: string) => createThread(agent),
  };
}

/**
 * Seats one more agent into an already-created thread -- `POST /api/
 * threads/{id}/participants`. Both `threadId` and `agent` travel with
 * `mutate`, not bound into the factory, the same "neither side is fixed for
 * the mounted component's lifetime" reasoning `createInviteMutation` above
 * documents -- here because a thread's id doesn't exist until
 * `createThreadMutation` above has already resolved, well after this
 * mutation is created. A refusal (not-found, not-ready, no shared consent,
 * already seated) surfaces as an `ApiError`; `ThreadPanel` shows that as a
 * toast per the threads arc's own ruling, not an inline form error.
 */
export function addThreadParticipantMutation() {
  return {
    mutationFn: ({ threadId, agent }: { threadId: number; agent: string }) =>
      addThreadParticipant(threadId, agent),
  };
}

/**
 * Delivers one turn to a thread's seated participants, in sequence -- `POST
 * /api/threads/{id}/turns`. Same "both travel with mutate" shape as
 * `addThreadParticipantMutation` above; `messages` is the FULL transcript
 * so far (this caller's own asks plus every participant's past replies),
 * the same "resend everything, the server has no memory of past turns"
 * contract `chatMutation` documents for single-agent chat.
 */
export function postThreadTurnMutation() {
  return {
    mutationFn: ({ threadId, messages }: { threadId: number; messages: ChatMessage[] }) =>
      postThreadTurn(threadId, messages),
  };
}

// Re-exported so consumers of the query factories don't need a separate
// import from api.ts just to type their component-level state.
export type {
  AgentEnv,
  AgentEnvWriteResult,
  AgentSecrets,
  AgentSecretsWrite,
  AgentDetail,
  AgentListResult,
  AgentMetadata,
  AgentSkill,
  AgentSlack,
  AgentSummary,
  AlreadyRequested,
  ArchivedPersona,
  CatalogEntry,
  ChatMessage,
  ColleagueFeed,
  ColleagueFeedEntry,
  ConnectAgentBody,
  Grant,
  GroupOption,
  GroupRequest,
  IntakeFile,
  Invite,
  InvitesResult,
  KnowledgeFile,
  Learning,
  NameSuggestion,
  OrgStatus,
  PeopleSearchResult,
  Person,
  PersonaDetail,
  PersonaDistillResult,
  PersonaDraft,
  PersonaForm,
  RequestableAgent,
  RequestableResult,
  ScanFinding,
  ScanRepoBody,
  ScanReport,
  TeamOptions,
  TeamPair,
  Thread,
  ThreadReply,
  ToolingEnforcement,
  ToolingFile,
  ToolingListResponse,
  Whoami,
};
