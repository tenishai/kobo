// What this page knows about changes it pushed to an agent that have not
// landed yet. A panel that pushes a change (Skills today) reports the sha it
// got back and, as its delivery poll answers, what state that sha is in. The
// agent header reads `isUpdating` to say Updating instead of Down while a
// rebuild this room itself started is rolling (issue #268): the pod really
// is down, and the room knows exactly why.
//
// Honesty rules: only a delivery this page saw start counts (nothing here
// survives a reload); the newest sha per agent is the one that matters; and
// a delivery nobody has heard about for `STALE_MS` is forgotten, so a poll
// that stopped (the panel unmounted) can never mask a real outage for long.

type Phase = "pending" | "building" | "rolling" | "delivered" | "failed" | "unknown";

type Delivery = { sha: string; phase: Phase; at: number };

const STALE_MS = 15 * 60_000;

const deliveries = $state<Record<string, Delivery>>({});

/** A panel just pushed `sha` for `agent`; its state is not known yet. */
export function beginDelivery(agent: string, sha: string): void {
  deliveries[agent] = { sha, phase: "pending", at: Date.now() };
}

/** The delivery poll answered for `sha`. Ignored for a sha that is no longer
 * the newest one this page pushed for the agent. */
export function reportDelivery(agent: string, sha: string, phase: string): void {
  const current = deliveries[agent];
  if (current === undefined || current.sha !== sha) {
    return;
  }
  const known: Phase[] = ["building", "rolling", "delivered", "failed"];
  deliveries[agent] = {
    sha,
    phase: known.includes(phase as Phase) ? (phase as Phase) : "unknown",
    at: Date.now(),
  };
}

/** True while the newest change this page pushed for `agent` is still on
 * its way (not yet seen delivered or failed, and heard from recently). */
export function isUpdating(agent: string, now: number = Date.now()): boolean {
  const current = deliveries[agent];
  if (current === undefined || now - current.at > STALE_MS) {
    return false;
  }
  return current.phase === "pending" || current.phase === "building" || current.phase === "rolling";
}

// --- Restarts this page asked for -----------------------------------------
//
// A Wakes on save (SlackVendor, SlackPanel) restarts the agent at once. The
// room's own health check catches up within a minute and says `rolling`;
// until it does, this page already knows, so the header reads Restarting
// instead of Down from the first second. Same honesty rules as above: only
// a restart this page saw start, forgotten after `STALE_MS` so a watcher
// that stopped without saying so can never mask a real outage for long.

const restarts = $state<Record<string, number>>({});

/** A panel just restarted `agent` with a change. */
export function beginRestart(agent: string): void {
  restarts[agent] = Date.now();
}

/** The watch over that restart ended (live, gave up, or unmounted). */
export function endRestart(agent: string): void {
  delete restarts[agent];
}

/** True while a restart this page asked for is still being watched. */
export function isRestarting(agent: string, now: number = Date.now()): boolean {
  const at = restarts[agent];
  return at !== undefined && now - at <= STALE_MS;
}

// A reading counts as "back" only once it was taken this long after the
// restart began: the old pod answers for a moment after a restart is asked
// for, and an up, not-rolling reading from that moment would end the watch
// before the restart even started.
const SETTLE_MS = 15_000;

/** Ends the watch over a restart this page asked for once a reading taken
 * well after it began says the agent is up and not mid-restart (2026-11-20:
 * the header used to keep saying Restarting until a hard refresh). */
export function settleRestart(
  agent: string,
  health: { state: string; rolling?: boolean; checked_at: string } | null | undefined,
): void {
  const at = restarts[agent];
  if (at === undefined || !health) {
    return;
  }
  const checked = Date.parse(health.checked_at);
  if (checked - at >= SETTLE_MS && health.state === "up" && health.rolling !== true) {
    delete restarts[agent];
  }
}
