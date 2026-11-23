// Status vocabulary, as returned by the API:
//   agents: pending | provisioning | ready | failed
//   steps:  pending | running | done | skipped | failed
//
// These are pure functions over that vocabulary — no fetching, no timers —
// so the query layer (queries.ts) and any view code can share one place
// that decides what "still working" and "done" mean.

/** Agent statuses that will never change again on their own. */
export const TERMINAL = ["ready", "failed"] as const;

const TERMINAL_STATUSES: readonly string[] = TERMINAL;

/** True once an agent has reached a status that won't change by itself. */
export function isTerminal(status: string): boolean {
  return TERMINAL_STATUSES.includes(status);
}

/** True when at least one agent in the list is still mid-flight. */
export function anyAgentActive(agents: { status: string }[]): boolean {
  return agents.some((agent) => !isTerminal(agent.status));
}

/** Poll cadence while something is actively changing. */
const ACTIVE_POLL_MS = 3500;
/** Poll cadence once everything has settled: the room re-asks the cluster
 * for each ready agent's health once a minute (kobo_api.health), and a
 * dashboard left open should show a pod that went down without a reload. */
const HEALTH_POLL_MS = 60_000;

/**
 * Refetch interval for the agent list query. Polls while any agent is
 * non-terminal so the list picks up provisioning progress, and stops once
 * everything has settled into ready/failed. `agents` is undefined before
 * the first fetch resolves — nothing to poll for yet, so that's `false`
 * too (TanStack Query's `refetchInterval` treats `false` as "don't poll").
 */
export function listRefetchInterval(
  agents?: { status: string }[],
): number | false {
  if (agents === undefined) {
    return false;
  }
  return anyAgentActive(agents) ? ACTIVE_POLL_MS : HEALTH_POLL_MS;
}

/**
 * Refetch interval for a single agent's detail query. Polls fast while that
 * agent itself is non-terminal (its steps are still progressing), while the
 * cluster says it is mid-restart (`health.rolling`), or while this page is
 * watching a restart it asked for (`restarting`, lib/delivery.svelte), so
 * the header leaves Restarting as soon as the agent is back rather than up to
 * a minute later (2026-11-20). `agent` is undefined before the first fetch
 * resolves.
 */
export function detailRefetchInterval(
  agent?: { status: string; health?: { rolling?: boolean } | null },
  restarting = false,
): number | false {
  if (agent === undefined) {
    return false;
  }
  if (!isTerminal(agent.status) || restarting || agent.health?.rolling === true) {
    return ACTIVE_POLL_MS;
  }
  return HEALTH_POLL_MS;
}

/**
 * Visual tone for a provisioning step:
 *  - "done"    -> ok
 *  - "skipped" -> skipped, its own tone -- not a failure and not still
 *    running, but not a success either: the step's own reason (e.g.
 *    "picture generation is not available") is real information, never
 *    folded into "ok" the way a plain no-op would be.
 *  - "failed"  -> fail
 *  - "pending" | "running" -> active -- a pending step is queued behind
 *    an active one, and the UI shows both as "working" (spinner), not as
 *    an inert/neutral state.
 *  - anything else (a status this code doesn't recognize) -> active, the
 *    safest default: it doesn't falsely claim success or failure.
 */
export function stepTone(
  status: string,
): "ok" | "fail" | "active" | "skipped" | "waiting" {
  switch (status) {
    case "done":
      return "ok";
    case "skipped":
      return "skipped";
    case "failed":
      return "fail";
    // Parked on a person (a connected repo's setup pull request nobody
    // could merge for them): not running, not failed, not done. Its own tone,
    // so the row can show what it waits for and the page can offer Resume.
    case "waiting":
      return "waiting";
    case "pending":
    case "running":
      return "active";
    default:
      return "active";
  }
}

/**
 * Statuses meaning "a destroy was in progress the last time this page had
 * real data" (Task 5, AgentDetail.svelte). Exported so the component reads
 * from this single set too, rather than keeping its own copy that could
 * drift from `isDestroyComplete`'s notion of the same thing.
 */
export const DESTROY_STATUSES = new Set(["destroying", "destroy_failed"]);

/** Whether the agent page renders read-only: panels visible, controls disabled. */
export function isFrozen(status: string): boolean {
  return DESTROY_STATUSES.has(status);
}

/**
 * True when a 404 for an agent means "destruction finished" rather than
 * "unknown/mistyped name". A 404 alone is ambiguous: it also fires for a
 * name that never loaded anything (`lastKnownStatus` stays `null`). This is
 * only true when the last real data the page had already showed the agent
 * mid-destroy, and the error in hand is actually a 404 (any other status,
 * or no error at all, is false).
 */
export function isDestroyComplete(
  lastKnownStatus: string | null,
  isError: boolean,
  errorStatus: number | undefined,
): boolean {
  return (
    isError &&
    errorStatus === 404 &&
    lastKnownStatus !== null &&
    DESTROY_STATUSES.has(lastKnownStatus)
  );
}

/**
 * Visual tone for an agent's overall status (distinct from stepTone: an
 * agent's own vocabulary — pending/provisioning/ready/failed — isn't the
 * same as a step's):
 *  - "ready"  -> ok
 *  - "failed" -> fail
 *  - "pending" | "provisioning" -> warn — still working, and (unlike a
 *    step's plain "active") rendered with the amber pulse so an in-progress
 *    agent card draws the eye on the dashboard.
 *  - anything else (a status this code doesn't recognize) -> warn, the
 *    safest default: it doesn't falsely claim success or failure.
 */
export function agentTone(status: string): "ok" | "fail" | "warn" {
  switch (status) {
    case "ready":
      return "ok";
    case "failed":
      return "fail";
    case "pending":
    case "provisioning":
    case "waiting":
      return "warn";
    default:
      return "warn";
  }
}
