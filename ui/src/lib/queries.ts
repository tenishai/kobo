// TanStack Query v5 factories over the typed API client (api.ts). Kept as
// plain option-object factories (not `createQuery`/`createMutation` calls
// themselves) so components wire them up with `createQuery(agentsQuery())`
// etc., and so the queryKey conventions below are visible in one place for
// later tasks to invalidate.
//
// queryKey conventions:
//   ["agents"]          - the agent list
//   ["agents", name]    - one agent's detail
//   ["catalogs"]        - personas/skills/models catalog

import { queryOptions, type QueryClient } from "@tanstack/svelte-query";
import {
  ApiError,
  type AgentDetail,
  type AgentSummary,
  type Catalogs,
  type ChatMessage,
  chatWithAgent,
  createAgent,
  fetchCatalogs,
  getAgent,
  listAgents,
  retryAgent,
} from "./api";
import { detailRefetchInterval, listRefetchInterval } from "./status";

const CATALOGS_STALE_MS = 5 * 60 * 1000;

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

/** Persona/skill/model catalog. Static-ish, so no polling — just a long staleTime. */
export function catalogsQuery() {
  return queryOptions({
    queryKey: ["catalogs"] as const,
    queryFn: fetchCatalogs,
    staleTime: CATALOGS_STALE_MS,
  });
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

// Re-exported so consumers of the query factories don't need a separate
// import from api.ts just to type their component-level state.
export type { AgentDetail, AgentSummary, Catalogs, ChatMessage };
