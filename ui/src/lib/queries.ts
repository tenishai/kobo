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
//   ["agents", name, "skills"]                      - an agent's skills (catalog + local)
//   ["agents", name, "intake"]                      - an agent's intake files
//   ["agents", name, "tooling"]                     - an agent's tooling files
//   ["agents", name, "slack", "manifest"]           - an agent's rendered
//                                                      Slack manifest (fetched
//                                                      lazily, once opened)
//   ["catalog", "models"]                           - the model list + default
//   ["catalog", "teams"]                             - the team-slug list
//                                                      (all catalog kinds,
//                                                      for search filtering)
//   ["teams"]                                        - the skill-owner team
//                                                      pool + default, for
//                                                      the create/Edit
//                                                      details team pickers
//   ["catalog", "skills", {q, team}]                - a skill catalog search
//   ["catalog", "personas", {q, team}]              - a persona catalog search
//   ["org", "status"]                               - org bootstrap/wiring status
//   ["names", "suggestions", {who_they_are, what_they_do}]
//                                                    - five name-suggestion cards for a committed persona
//   ["colleagues", "conversations", "list", {all}]  - the watching window's
//                                                      conversation list
//                                                      (Task 4/B4)
//   ["colleagues", "conversations", "detail", taskId]
//                                                    - one conversation's
//                                                      full detail + events

import { queryOptions, type QueryClient } from "@tanstack/svelte-query";
import {
  ApiError,
  type AgentDetail,
  type AgentMetadata,
  type AgentSkill,
  type AgentSlack,
  type AgentSummary,
  type CatalogEntry,
  type ChatMessage,
  type ColleagueConversation,
  type ColleagueConversationDetail,
  type IntakeFile,
  type KnowledgeFile,
  type NameSuggestion,
  type OrgStatus,
  type PersonaDraft,
  type PersonaForm,
  type TeamOptions,
  type ToolingEnforcement,
  type ToolingFile,
  type ToolingListResponse,
  addCatalogSkill,
  chatWithAgent,
  connectSlack,
  createAgent,
  createPersona,
  deleteIntake,
  deleteKnowledgeFile,
  deleteSkill,
  deleteTooling,
  destroyAgent,
  draftPersona,
  fetchColleagueConversation,
  fetchDeliveryState,
  fetchModels,
  fetchNameSuggestions,
  fetchOrgStatus,
  fetchSlackManifest,
  fetchTeamOptions,
  fetchTeams,
  generateAvatar,
  getAgent,
  importTooling,
  listAgents,
  listColleagueConversations,
  listIntake,
  listKnowledge,
  listSkills,
  listTooling,
  patchAgentMetadata,
  regenerateAgentMetadata,
  retryAgent,
  saveKnowledgeFile,
  saveLocalSkill,
  saveSlackChannels,
  searchPersonas,
  searchSkills,
  teachAgent,
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

/** The agent list. Polls at the adaptive cadence while any agent is active. */
export function agentsQuery() {
  return queryOptions({
    queryKey: ["agents"] as const,
    queryFn: listAgents,
    refetchInterval: (query) => listRefetchInterval(query.state.data),
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
export function modelsQuery() {
  return queryOptions({
    queryKey: ["catalog", "models"] as const,
    queryFn: fetchModels,
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
 * create station's team picker and the Edit details modal's team field —
 * a DIFFERENT query from `teamsQuery` above (which reads `/api/catalog/teams`,
 * spanning both catalog kinds for search filtering). No polling/staleTime
 * override: same "doesn't change on its own in the background" reasoning
 * as `modelsQuery`. CreateModal fetches this unconditionally, warmed by
 * `prefetchCatalogs` (App.svelte) same as `modelsQuery`/`teamsQuery`; the
 * Edit details modal (`MetadataDetails`) instead gates it on its own `open`
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

/** A persona catalog search for the given query/team. Keyed on both so each
 * distinct search has its own cache entry. */
export function personasSearchQuery(q: string, team: string | null) {
  return queryOptions({
    queryKey: ["catalog", "personas", { q, team }] as const,
    queryFn: () => searchPersonas(q, team),
    staleTime: CATALOG_SEARCH_STALE_MS,
  });
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
 * Five name-suggestion cards for the naming station (create flow v2 "naming
 * ceremony" addendum), derived from the just-committed persona's own
 * `who_they_are`/`what_they_do` text. `enabled` is false until both fields
 * are non-empty — there's nothing to suggest against before a persona is
 * picked, same "nothing to fetch yet" gate `deliveryQuery` uses for its own
 * sha. No polling, no extra staleTime: the naming station's "offer more"
 * action is a plain `refetch()` on this same query, not a second query key
 * or a timer.
 */
export function nameSuggestionsQuery(whoTheyAre: string, whatTheyDo: string) {
  return queryOptions({
    queryKey: [
      "names",
      "suggestions",
      { who_they_are: whoTheyAre, what_they_do: whatTheyDo },
    ] as const,
    queryFn: () => fetchNameSuggestions(whoTheyAre, whatTheyDo),
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
 * The watching window's conversation list (Task 4/B4) — agent-only by
 * default (`all: false`), every caller (room-human chats and service
 * callers included) when `all` is true. No polling of its own: live
 * updates come from the SSE stream instead (`ColleagueConversations.svelte`
 * refetches this query on any frame from `/api/colleagues/stream` — "on any
 * SSE message refetch the list", no client-side event merging).
 *
 * A 409 (this room has no switchboard wired) surfaces through `$query.
 * error` as an `ApiError` with `status === 409`; the component treats that
 * specifically as "render nothing" rather than an error state.
 */
export function colleagueConversationsQuery(all: boolean) {
  return queryOptions({
    queryKey: ["colleagues", "conversations", "list", { all }] as const,
    queryFn: () => listColleagueConversations(all),
  });
}

/**
 * One colleague conversation's full detail plus its event log (Task
 * 4/B4), fetched lazily once a row in the watching window is expanded —
 * disabled until a `taskId` is chosen, same "nothing to fetch yet" gate
 * `deliveryQuery`'s sha argument uses.
 */
export function colleagueConversationQuery(taskId: string | null) {
  return queryOptions({
    queryKey: ["colleagues", "conversations", "detail", taskId] as const,
    queryFn: () => fetchColleagueConversation(taskId as string),
    enabled: taskId !== null,
  });
}

// Re-exported so consumers of the query factories don't need a separate
// import from api.ts just to type their component-level state.
export type {
  AgentDetail,
  AgentMetadata,
  AgentSkill,
  AgentSlack,
  AgentSummary,
  CatalogEntry,
  ChatMessage,
  ColleagueConversation,
  ColleagueConversationDetail,
  IntakeFile,
  KnowledgeFile,
  NameSuggestion,
  OrgStatus,
  PersonaDraft,
  PersonaForm,
  TeamOptions,
  ToolingEnforcement,
  ToolingFile,
  ToolingListResponse,
};
