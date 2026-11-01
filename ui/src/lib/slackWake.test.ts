// Live means live (2026-11-01): after a Wakes on save, the row says Live only
// on a health reading taken after the save that finds the agent up and not
// restarting, and then only after a short settle, because the agent reads
// as up a few seconds before it is listening. A rolling reading, or one an
// older check produced, never counts.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AgentHealth } from "./api";
import { WAKE_POLL_MS, WAKE_SETTLE_MS, watchWakeLive } from "./slackWake";

const reading = (checked_at: string, extra: Partial<AgentHealth> = {}): AgentHealth => ({
  state: "up",
  reason: null,
  checked_at,
  rolling: false,
  ...extra,
});

/** `read` answers from the queue in order and repeats the last one. */
function watch(readings: AgentHealth[], since: string | null = "2026-11-01T10:00:30Z") {
  const queue = [...readings];
  const onLive = vi.fn();
  const onGiveUp = vi.fn();
  const stop = watchWakeLive({
    baseline: "2026-11-01T10:00:00Z",
    since,
    read: async () => (queue.length > 1 ? queue.shift()! : queue[0]),
    onLive,
    onGiveUp,
  });
  return { onLive, onGiveUp, stop };
}

describe("watchWakeLive", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("does not flip on a rolling reading, and flips after a settled up reading plus the settle", async () => {
    const { onLive, stop } = watch([
      reading("2026-11-01T10:01:00Z", { state: "down", reason: "0 of 1 running", rolling: true }),
      reading("2026-11-01T10:01:05Z", { rolling: true }),
      reading("2026-11-01T10:01:10Z"),
    ]);
    try {
      // Two rolling readings (now, and the first poll): nothing.
      await vi.advanceTimersByTimeAsync(WAKE_POLL_MS);
      expect(onLive).not.toHaveBeenCalled();
      // The settled reading lands on the second poll; it is not Live yet.
      await vi.advanceTimersByTimeAsync(WAKE_POLL_MS);
      expect(onLive).not.toHaveBeenCalled();
      await vi.advanceTimersByTimeAsync(WAKE_SETTLE_MS - 1);
      expect(onLive).not.toHaveBeenCalled();
      await vi.advanceTimersByTimeAsync(1);
      expect(onLive).toHaveBeenCalledTimes(1);
    } finally {
      stop();
    }
  });

  it("ignores an up reading another check took before the save, by the room's own stamps", async () => {
    const { onLive, onGiveUp, stop } = watch([reading("2026-11-01T10:00:00Z"), reading("2026-11-01T10:00:29Z")]);
    try {
      await vi.advanceTimersByTimeAsync(WAKE_POLL_MS * 3 + WAKE_SETTLE_MS);
      expect(onLive).not.toHaveBeenCalled();
      expect(onGiveUp).not.toHaveBeenCalled();
    } finally {
      stop();
    }
  });
});
