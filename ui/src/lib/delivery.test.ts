import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { beginRestart, endRestart, isRestarting, settleRestart } from "./delivery.svelte";

// A restart this page asked for ends once the agent has been through it and is
// back (2026-11-20). Readings are compared with each other, never with this
// browser's clock, and one the room could not take counts for nothing.
describe("settleRestart", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-11-20T21:00:00Z"));
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  const up = (since: string) => ({ state: "up", rolling: false, ready_since: since });

  it("ends the watch once the agent went down and came back", () => {
    beginRestart("gate");
    settleRestart("gate", up("2026-11-20T09:00:00Z"));
    expect(isRestarting("gate")).toBe(true);
    settleRestart("gate", { state: "up", rolling: true, ready_since: "2026-11-20T09:00:00Z" });
    settleRestart("gate", { state: "down", rolling: true, ready_since: null });
    expect(isRestarting("gate")).toBe(true);
    settleRestart("gate", up("2026-11-20T21:00:40Z"));
    expect(isRestarting("gate")).toBe(false);
  });

  it("ends the watch on a restart that happened between two polls", () => {
    beginRestart("gate");
    settleRestart("gate", up("2026-11-20T09:00:00Z"));
    settleRestart("gate", up("2026-11-20T09:00:00Z"));
    expect(isRestarting("gate")).toBe(true);
    settleRestart("gate", up("2026-11-20T21:00:40Z"));
    expect(isRestarting("gate")).toBe(false);
  });

  it("waits minutes for a restart that starts late, and ends when none comes", () => {
    beginRestart("gate");
    settleRestart("gate", { state: "up", unread: true, ready_since: null });
    settleRestart("gate", up("2026-11-20T09:00:00Z"));
    vi.advanceTimersByTime(5 * 60_000);
    settleRestart("gate", up("2026-11-20T09:00:00Z"));
    expect(isRestarting("gate")).toBe(true);
    vi.advanceTimersByTime(4 * 60_000);
    settleRestart("gate", up("2026-11-20T09:00:00Z"));
    expect(isRestarting("gate")).toBe(false);
  });

  it("lets a panel end only the restart it began", () => {
    const first = beginRestart("gate");
    beginRestart("gate");
    endRestart("gate", first);
    expect(isRestarting("gate")).toBe(true);
    endRestart("gate");
    expect(isRestarting("gate")).toBe(false);
  });
});
