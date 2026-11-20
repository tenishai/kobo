import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { beginRestart, isRestarting, settleRestart } from "./delivery.svelte";

// A restart this page asked for ends on a reading taken well after it began
// that says the agent is up and not rolling (2026-11-20). A reading from the
// first moments, while the old pod still answers, must not end it.
describe("settleRestart", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-11-20T21:00:00Z"));
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("ends the watch only on a later reading that says the agent is back", () => {
    beginRestart("gate");
    const at = (seconds: number) => new Date(Date.parse("2026-11-20T21:00:00Z") + seconds * 1000).toISOString();

    settleRestart("gate", { state: "up", rolling: false, checked_at: at(2) });
    expect(isRestarting("gate")).toBe(true);
    settleRestart("gate", { state: "up", rolling: true, checked_at: at(20) });
    expect(isRestarting("gate")).toBe(true);
    settleRestart("gate", { state: "down", rolling: false, checked_at: at(25) });
    expect(isRestarting("gate")).toBe(true);
    settleRestart("gate", { state: "up", rolling: false, checked_at: at(30) });
    expect(isRestarting("gate")).toBe(false);
  });
});
