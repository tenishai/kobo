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
// A change that restarts the agent (`restarted` in lib/queries.ts, and a Wakes
// on save in SlackVendor and SlackPanel) says so here the moment it lands, so
// the header reads Restarting from the first second instead of Ready, or Down,
// until the room's health check catches up. Same honesty rules as above: only
// a restart this page saw start, forgotten after `STALE_MS` so a watcher that
// stopped without saying so can never mask a real outage for long.

type Restart = { id: number; at: number; rolled: boolean; mark?: string };

const restarts = $state<Record<string, Restart>>({});
let lastRestart = 0;

/** A change just restarted `agent`, or will within minutes. Returns the
 * watch's id, for `endRestart`. */
export function beginRestart(agent: string): number {
  lastRestart += 1;
  restarts[agent] = { id: lastRestart, at: Date.now(), rolled: false };
  return lastRestart;
}

/** The watch over that restart ended (live, gave up, or unmounted). Given an
 * id, only that watch ends, never one a later change began. */
export function endRestart(agent: string, id?: number): void {
  if (id === undefined || restarts[agent]?.id === id) {
    delete restarts[agent];
  }
}

/** True while a restart this page asked for is still being watched. */
export function isRestarting(agent: string, now: number = Date.now()): boolean {
  const restart = restarts[agent];
  return restart !== undefined && now - restart.at <= STALE_MS;
}

// How long a restart may take to begin. Most begin at once; one the deploy
// platform or a build delivers (a model, a runtime, the env panel, any change
// in a room that builds each one) rolls the agent minutes later. A watch that
// has seen none by then ends: none is coming.
const ROLL_WAIT_MS = 8 * 60_000;

type Reading = {
  state: string;
  rolling?: boolean;
  ready_since?: string | null;
  generation?: number | null;
  unread?: boolean;
};

// The agents whose last reading said rolling: the page asks for live readings
// while one is, as it does while it watches a restart of its own.
const rollingNow: Record<string, boolean> = {};

/** True while this page wants the room's live reading of `agent`. */
export function wantsLiveHealth(agent: string): boolean {
  return isRestarting(agent) || rollingNow[agent] === true;
}

/** Ends the watch over a restart this page asked for once the agent has been
 * through it and is back: up and settled after a reading that said it was
 * rolling or down, or after one whose generation or ready time moved on from
 * the first reading's (a restart between two polls). A reading the room could
 * not take counts for nothing, and no reading is compared with this browser's
 * clock (2026-11-20: the header kept saying Restarting until a hard refresh). */
export function settleRestart(
  agent: string,
  health: Reading | null | undefined,
  now: number = Date.now(),
): void {
  if (health && health.unread !== true) {
    rollingNow[agent] = health.rolling === true;
  }
  const restart = restarts[agent];
  if (restart === undefined || !health || health.unread === true) {
    return;
  }
  if (health.rolling === true || health.state !== "up") {
    if (!restart.rolled) {
      restart.rolled = true;
    }
    return;
  }
  const mark = `${health.generation ?? ""}/${health.ready_since ?? ""}`;
  if (restart.mark === undefined) {
    // The first reading: the agent as the change found it.
    restart.mark = mark;
  }
  if (restart.rolled || mark !== restart.mark || now - restart.at > ROLL_WAIT_MS) {
    delete restarts[agent];
  }
}
