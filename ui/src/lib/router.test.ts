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

  // Harness wave (2026-09-23): the agent page's tab, and the Harness tab's
  // own section, are segments three and four. The router stays dumb about
  // what they mean -- it hands back whatever is there and `tabs.ts` decides
  // whether it names anything.
  it("reads the tab off the third segment", () => {
    expect(parseHash("#/agents/clio/harness")).toEqual({
      page: "agent",
      name: "clio",
      tab: "harness",
    });
  });

  it("reads the harness section off the fourth segment", () => {
    expect(parseHash("#/agents/clio/harness/skills")).toEqual({
      page: "agent",
      name: "clio",
      tab: "harness",
      section: "skills",
    });
  });

  it("hands back an unknown tab verbatim rather than guessing", () => {
    expect(parseHash("#/agents/clio/nonsense")).toEqual({
      page: "agent",
      name: "clio",
      tab: "nonsense",
    });
  });

  it("ignores anything past the section", () => {
    expect(parseHash("#/agents/clio/harness/skills/extra")).toEqual({
      page: "agent",
      name: "clio",
      tab: "harness",
      section: "skills",
    });
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

  it("formats an agent route with a tab", () => {
    expect(formatHash({ page: "agent", name: "clio", tab: "settings" })).toBe(
      "#/agents/clio/settings",
    );
  });

  it("formats an agent route with a tab and a harness section", () => {
    expect(
      formatHash({ page: "agent", name: "clio", tab: "harness", section: "skills" }),
    ).toBe("#/agents/clio/harness/skills");
  });

  it("drops a section with no tab in front of it, so the name segment stays second", () => {
    expect(formatHash({ page: "agent", name: "clio", section: "skills" })).toBe(
      "#/agents/clio",
    );
  });

  it("round-trips a tab and a section through parseHash", () => {
    const route = {
      page: "agent",
      name: "clio",
      tab: "harness",
      section: "skills",
    } as const;
    expect(parseHash(formatHash(route))).toEqual(route);
  });
});

it("parses #/getting-started", () => {
  expect(parseHash("#/getting-started")).toEqual({ page: "getting-started" });
});

it("parses #/getting-started/<section> and hands the section back verbatim", () => {
  expect(parseHash("#/getting-started/slack")).toEqual({
    page: "getting-started",
    section: "slack",
  });
  // A third segment is not a guide address.
  expect(parseHash("#/getting-started/slack/extra")).toEqual({ page: "dashboard" });
});

it("formats the getting-started route with and without a section", () => {
  expect(formatHash({ page: "getting-started", section: "the-harness" })).toBe(
    "#/getting-started/the-harness",
  );
});

it("formats the getting-started route", () => {
  expect(formatHash({ page: "getting-started" })).toBe("#/getting-started");
});

it("still falls back to dashboard for unknown paths", () => {
  expect(parseHash("#/getting")).toEqual({ page: "dashboard" });
});

it("falls back to dashboard for the retired personas page", () => {
  // The page went in the personas-in-place wave (2026-09-30); an old
  // bookmark lands on the dashboard rather than a blank main.
  expect(parseHash("#/personas")).toEqual({ page: "dashboard" });
});
