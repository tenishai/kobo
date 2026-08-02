import { describe, expect, it } from "vitest";
import { relativeTime } from "./time";

const NOW = new Date("2026-08-02T12:00:00.000Z");

function isoBefore(ms: number): string {
  return new Date(NOW.getTime() - ms).toISOString();
}

describe("relativeTime", () => {
  it("reads 'just now' for a timestamp seconds ago", () => {
    expect(relativeTime(isoBefore(30 * 1000), NOW)).toBe("just now");
  });

  it("reads 'just now' right at zero difference", () => {
    expect(relativeTime(NOW.toISOString(), NOW)).toBe("just now");
  });

  it("clamps a future timestamp (clock skew) to 'just now' instead of a negative duration", () => {
    const future = new Date(NOW.getTime() + 5000).toISOString();
    expect(relativeTime(future, NOW)).toBe("just now");
  });

  it("reads whole minutes once a minute has passed", () => {
    expect(relativeTime(isoBefore(60 * 1000), NOW)).toBe("1m ago");
    expect(relativeTime(isoBefore(5 * 60 * 1000), NOW)).toBe("5m ago");
    expect(relativeTime(isoBefore(59 * 60 * 1000), NOW)).toBe("59m ago");
  });

  it("reads whole hours once an hour has passed", () => {
    expect(relativeTime(isoBefore(60 * 60 * 1000), NOW)).toBe("1h ago");
    expect(relativeTime(isoBefore(3 * 60 * 60 * 1000), NOW)).toBe("3h ago");
    expect(relativeTime(isoBefore(23 * 60 * 60 * 1000), NOW)).toBe("23h ago");
  });

  it("reads whole days once a day has passed", () => {
    expect(relativeTime(isoBefore(24 * 60 * 60 * 1000), NOW)).toBe("1d ago");
    expect(relativeTime(isoBefore(2 * 24 * 60 * 60 * 1000), NOW)).toBe(
      "2d ago",
    );
    expect(relativeTime(isoBefore(30 * 24 * 60 * 60 * 1000), NOW)).toBe(
      "30d ago",
    );
  });

  it("defaults `now` to the current time when omitted", () => {
    const iso = new Date(Date.now() - 2 * 60 * 1000).toISOString();
    expect(relativeTime(iso)).toBe("2m ago");
  });
});
