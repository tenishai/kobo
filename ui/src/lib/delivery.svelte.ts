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
