import { describe, expect, it } from "vitest";
import { formatHash, parseHash } from "./router.svelte";

describe("parseHash", () => {
  it("maps the empty string to the dashboard", () => {
    expect(parseHash("")).toEqual({ page: "dashboard" });
  });

  it("maps the bare root hash to the dashboard", () => {
    expect(parseHash("#/")).toEqual({ page: "dashboard" });
  });

  it("maps a hash-less root to the dashboard", () => {
    expect(parseHash("#")).toEqual({ page: "dashboard" });
  });

  it("maps an agent hash to the agent page with its name", () => {
    expect(parseHash("#/agents/clio")).toEqual({ page: "agent", name: "clio" });
  });

  it("decodes a URI-encoded agent name", () => {
    expect(parseHash("#/agents/foo%20bar")).toEqual({
      page: "agent",
      name: "foo bar",
    });
  });

  it("tolerates a trailing slash on an agent hash", () => {
    expect(parseHash("#/agents/clio/")).toEqual({
      page: "agent",
      name: "clio",
    });
  });

  it("falls back to the dashboard for junk input", () => {
    expect(parseHash("not-a-hash-at-all")).toEqual({ page: "dashboard" });
  });

  it("falls back to the dashboard when the agent segment is missing", () => {
    expect(parseHash("#/agents")).toEqual({ page: "dashboard" });
  });

  it("falls back to the dashboard when the agent name segment is empty", () => {
    expect(parseHash("#/agents/")).toEqual({ page: "dashboard" });
  });

  it("falls back to the dashboard for an unrelated path", () => {
    expect(parseHash("#/settings")).toEqual({ page: "dashboard" });
  });

  it("falls back to the raw segment when it is malformed percent-encoding on its own", () => {
    expect(parseHash("#/agents/%")).toEqual({ page: "agent", name: "%" });
  });

  it("falls back to the raw segment when percent-decoding fails mid-string", () => {
    expect(parseHash("#/agents/100%")).toEqual({ page: "agent", name: "100%" });
  });
});

describe("formatHash", () => {
  it("formats the dashboard as the root hash", () => {
    expect(formatHash({ page: "dashboard" })).toBe("#/");
  });

  it("formats an agent route with its name", () => {
    expect(formatHash({ page: "agent", name: "clio" })).toBe("#/agents/clio");
  });

  it("encodes special characters in an agent name", () => {
    expect(formatHash({ page: "agent", name: "foo bar" })).toBe(
      "#/agents/foo%20bar",
    );
  });

  it("round-trips through parseHash", () => {
    const route = { page: "agent", name: "clio" } as const;
    expect(parseHash(formatHash(route))).toEqual(route);
  });
});
