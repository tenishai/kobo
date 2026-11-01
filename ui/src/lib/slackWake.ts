// What the Wakes on choice says after a save, shared by the Slack tab
// (SlackPanel) and the Slack row on Access (SlackVendor), which call the
// same route (`PUT /api/agents/{name}/slack/wake`).
//
// The route commits the choice into the agent's files AND sets it on the
// running agent at once (`applied_now`, 2026-11-01), which restarts it. The
// page cannot see that restart directly; what it can see is the room's own
// health check of the agent (`AgentDetail.health`, taken once a minute).
// A check taken after the save that finds the agent up is the sign the
// choice is live, and it is told apart from the one in hand by its
// `checked_at`, never by the browser's clock.
import type { AgentHealth } from "./api";

export const WAKE_NOTE_APPLYING = "Saved. The agent restarts with this choice now; give it a minute.";
export const WAKE_NOTE_LATER = "Saved. The agent picks this up when it next restarts.";
export const WAKE_NOTE_LIVE = "Live.";

/** How often the page re-reads the agent while a change applies, and for
 * how long before it stops asking and leaves the "give it a minute" note. */
export const WAKE_POLL_MS = 5_000;
export const WAKE_POLL_LIMIT_MS = 180_000;

export function wakeSavedNote(result: { applied_now?: boolean }): string {
  return result.applied_now ? WAKE_NOTE_APPLYING : WAKE_NOTE_LATER;
}

/** A health reading taken since `baseline` (the `checked_at` in hand when
 * the save was made, or null when the room had none) that finds the agent
 * up. */
export function readsLiveAfter(health: AgentHealth | null | undefined, baseline: string | null): boolean {
  if (!health || health.state !== "up") return false;
  return health.checked_at !== baseline;
}

/**
 * Read the agent now and then every `WAKE_POLL_MS` until a reading says the
 * choice is live (`onLive`) or `WAKE_POLL_LIMIT_MS` passes (`onGiveUp`).
 * Returns the function that stops the watch; the caller runs it on unmount
 * and before starting another.
 */
export function watchWakeLive(opts: {
  baseline: string | null;
  read: () => Promise<AgentHealth | null | undefined>;
  onLive: () => void;
  onGiveUp: () => void;
}): () => void {
  let stopped = false;
  let timer: ReturnType<typeof setInterval> | null = null;
  let limit: ReturnType<typeof setTimeout> | null = null;
  const stop = () => {
    stopped = true;
    if (timer !== null) clearInterval(timer);
    if (limit !== null) clearTimeout(limit);
    timer = null;
    limit = null;
  };
  const check = async () => {
    let health: AgentHealth | null | undefined;
    try {
      health = await opts.read();
    } catch {
      return; // a read that failed says nothing; the next one may
    }
    if (stopped) return;
    if (readsLiveAfter(health, opts.baseline)) {
      stop();
      opts.onLive();
    }
  };
  timer = setInterval(() => void check(), WAKE_POLL_MS);
  limit = setTimeout(() => {
    stop();
    opts.onGiveUp();
  }, WAKE_POLL_LIMIT_MS);
  void check();
  return stop;
}
