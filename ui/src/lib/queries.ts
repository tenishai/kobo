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
  type AgentDetail,
  type AgentSummary,
  type Catalogs,
  createAgent,
  fetchCatalogs,
  getAgent,
  listAgents,
  retryAgent,
} from "./api";
import { detailRefetchInterval, listRefetchInterval } from "./status";

const CATALOGS_STALE_MS = 5 * 60 * 1000;

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
 * Retries a failed agent's provisioning; on success, invalidates both the
 * list (its status changed) and its own detail query (steps reset/resume).
 */
export function retryAgentMutation(queryClient: QueryClient, name: string) {
  return {
    mutationFn: () => retryAgent(name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents"] });
      queryClient.invalidateQueries({ queryKey: ["agents", name] });
    },
  };
}

// Re-exported so consumers of the query factories don't need a separate
// import from api.ts just to type their component-level state.
export type { AgentDetail, AgentSummary, Catalogs };
