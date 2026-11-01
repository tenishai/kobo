// What the Wakes on choice says after a save, shared by the Slack tab
// (SlackPanel) and the Slack row on Access (SlackVendor), which call the
// same route (`PUT /api/agents/{name}/slack/wake`).
//
// The route commits the choice into the agent's files AND sets it on the
// running agent at once (`applied_now`, 2026-11-01), which restarts it. The
// page cannot see that restart directly; what it can see is the room's own
// health check of the agent (`AgentDetail.health`, taken once a minute).
// Live means live: the sign the choice is in force is a check taken AFTER
// the save (its `checked_at` is past the save's `applied_at`; with no
// `applied_at` from an older room, a `checked_at` other than the one in
// hand) that finds the agent up and not restarting (`rolling` false), and
// then a short settle, because the agent reads as up a few seconds before
// it is listening to Slack. Never the browser's clock: both stamps are the
// room's.
import type { AgentHealth } from "./api";

export const WAKE_NOTE_APPLYING = "Saved. The agent restarts with this choice now; give it a minute.";
export const WAKE_NOTE_LATER = "Saved. The agent picks this up when it next restarts.";
export const WAKE_NOTE_LIVE = "Live.";

/** How often the page re-reads the agent while a change applies, how long
 * it rests after a good reading before saying Live, and for how long in all
 * before it stops asking and leaves the "give it a minute" note. */
export const WAKE_POLL_MS = 5_000;
export const WAKE_SETTLE_MS = 15_000;
export const WAKE_POLL_LIMIT_MS = 180_000;

export function wakeSavedNote(result: { applied_now?: boolean }): string {
  return result.applied_now ? WAKE_NOTE_APPLYING : WAKE_NOTE_LATER;
}

/** Where a watch starts: `since` is the save's own stamp from the room
 * (`applied_at`), `baseline` the `checked_at` in hand when the save was
 * made (or null when the room had none). */
export type WakeWatchStart = { since?: string | null; baseline: string | null };

/** A health reading taken after the save that finds the agent up and not
 * restarting. With `since`, a reading counts only when its `checked_at` is
 * later (RFC 3339 in UTC compares as text); without it, when it differs
 * from `baseline`. A rolling reading never counts, whatever its stamp. */
export function readsLiveAfter(health: AgentHealth | null | undefined, start: WakeWatchStart | string | null): boolean {
  if (!health || health.state !== "up" || health.rolling) return false;
  const s: WakeWatchStart = typeof start === "string" || start === null ? { baseline: start } : start;
  if (s.since) return health.checked_at > s.since;
  return health.checked_at !== s.baseline;
}

/**
 * Read the agent now and then every `WAKE_POLL_MS` until a reading says the
 * agent is back (`readsLiveAfter`), rest `WAKE_SETTLE_MS` more, then
 * `onLive`; or `WAKE_POLL_LIMIT_MS` passes first and `onGiveUp`. Returns the
 * function that stops the watch; the caller runs it on unmount and before
 * starting another.
 */
export function watchWakeLive(opts: {
  baseline: string | null;
  since?: string | null;
  read: () => Promise<AgentHealth | null | undefined>;
  onLive: () => void;
  onGiveUp: () => void;
}): () => void {
  let stopped = false;
  let timer: ReturnType<typeof setInterval> | null = null;
  let settle: ReturnType<typeof setTimeout> | null = null;
  let limit: ReturnType<typeof setTimeout> | null = null;
  const stop = () => {
    stopped = true;
    if (timer !== null) clearInterval(timer);
    if (settle !== null) clearTimeout(settle);
    if (limit !== null) clearTimeout(limit);
    timer = null;
    settle = null;
    limit = null;
  };
  const check = async () => {
    let health: AgentHealth | null | undefined;
    try {
      health = await opts.read();
    } catch {
      return; // a read that failed says nothing; the next one may
    }
    if (stopped || settle !== null) return;
    if (readsLiveAfter(health, { since: opts.since, baseline: opts.baseline })) {
      // Back, by the room's own check. Stop asking and let it settle.
      if (timer !== null) clearInterval(timer);
      timer = null;
      settle = setTimeout(() => {
        stop();
        opts.onLive();
      }, WAKE_SETTLE_MS);
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
