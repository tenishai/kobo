import { describe, expect, it } from "vitest";
import { serverDate } from "./time";

describe("serverDate", () => {
  // Wire shape: an offset-less timestamp from the API is UTC, not the viewer's zone (issue #306).
  it("reads an offset-less string as UTC, the same instant as its Z form", () => {
    expect(serverDate("2026-10-29T21:07:00").getTime()).toBe(Date.UTC(2026, 9, 29, 21, 7, 0));
    expect(serverDate("2026-10-29T21:07:00").getTime()).toBe(serverDate("2026-10-29T21:07:00Z").getTime());
  });

  it("leaves a string that names its offset alone", () => {
    expect(serverDate("2026-10-29T17:07:00-04:00").getTime()).toBe(Date.UTC(2026, 9, 29, 21, 7, 0));
  });
});
