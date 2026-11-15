import { describe, expect, it } from "vitest";

import {
  DEFAULT_HARNESS_SECTION,
  defaultTab,
  harnessSections,
  isHarnessSection,
  isTabId,
  primaryTabs,
  tabFromHash,
} from "./tabs";

describe("primaryTabs", () => {
  it("is chat, slack, harness and secrets with no argument", () => {
    expect(primaryTabs()).toEqual(["chat", "slack", "harness", "secrets"]);
  });

  it("appends access before harness when the invites probe succeeded", () => {
    // Five tabs on one row, in the order somebody reads them: talk to it,
    // put it in Slack, what it can reach, what runs it, then its secrets.
    expect(primaryTabs(true)).toEqual(["chat", "slack", "access", "harness", "secrets"]);
  });
});

describe("the tab model by kind (connected-agents design 2026-09-21)", () => {
  it("keeps every tab for a composed agent, named or defaulted", () => {
    expect(primaryTabs(false, "composed")).toEqual(primaryTabs());
    expect(primaryTabs(true, "composed")).toEqual([
      "chat",
      "slack",
      "access",
      "harness",
      "secrets",
    ]);
  });

  it("drops chat and slack from a connected agent's row, and keeps harness and secrets", () => {
    // Harness survives because a connected agent still runs on something,
    // and Secrets because a team's own service has exactly as many of
    // them. Chat and Slack do not: the room has no way to talk to it and
    // never made its chat app.
    expect(primaryTabs(false, "connected")).toEqual(["harness", "secrets"]);
  });

  it("keeps access for a connected agent, exactly as for a composed one", () => {
    expect(primaryTabs(true, "connected")).toEqual(["access", "harness", "secrets"]);
  });

  it("treats an unknown kind as composed, rather than hiding everything", () => {
    expect(primaryTabs(true, "adopted")).toEqual([
      "chat",
      "slack",
      "access",
      "harness",
      "secrets",
    ]);
  });
});

describe("the Slack tab when the room makes Slack apps (issue #371)", () => {
  it("hides the Slack tab for an agent without a paste-flow app", () => {
    // Slack is a vendor on the Access tab then; a second place would be
    // two ways to do one thing.
    expect(primaryTabs(true, "composed", { slackRelay: true, slackLegacy: false })).toEqual([
      "chat",
      "access",
      "harness",
      "secrets",
    ]);
  });

  it("keeps the Slack tab for an agent whose app came from the paste flow", () => {
    expect(primaryTabs(true, "composed", { slackRelay: true, slackLegacy: true })).toEqual([
      "chat",
      "slack",
      "access",
      "harness",
      "secrets",
    ]);
  });

  it("keeps the Slack tab for everyone while the switch is off", () => {
    expect(primaryTabs(false, "composed", { slackRelay: false, slackLegacy: false })).toEqual(primaryTabs());
  });
});

describe("harnessSections", () => {
  it("is model and runtime first, then everything the room composed", () => {
    expect(harnessSections()).toEqual([
      "runtime",
      "identity",
      "persona",
      "skills",
      "tooling",
      "memory",
      "learnings",
      "activity",
    ]);
  });

  it("adds Agent to agent after Tooling for a caller who may administer the agent (issue #368)", () => {
    // The Other agents block of the Access tab, as a Harness section. The
    // routes behind it answer only the agent's owner and the room's
    // operators, so it is offered only to them: the same probe that shows
    // the Access tab.
    expect(harnessSections("composed", { canAdminister: true })).toEqual([
      "runtime",
      "identity",
      "persona",
      "skills",
      "tooling",
      "a2a",
      "memory",
      "learnings",
      "activity",
    ]);
    expect(harnessSections("connected", { canAdminister: true })).toEqual([
      "runtime",
      "identity",
      "a2a",
      "activity",
    ]);
  });

  it("drops Identity and access when the permission service is on", () => {
    expect(harnessSections("composed", { accessOn: true, canAdminister: true })).not.toContain(
      "identity",
    );
  });

  it("is model and runtime alone for a connected agent: there is no bundle behind the rest", () => {
    expect(harnessSections("connected")).toEqual(["runtime", "identity", "activity"]);
  });

  it("treats an unknown kind as composed", () => {
    expect(harnessSections("adopted")).toEqual(harnessSections());
  });

  it("starts on model and runtime, which every agent has", () => {
    expect(DEFAULT_HARNESS_SECTION).toBe("runtime");
    expect(harnessSections()[0]).toBe(DEFAULT_HARNESS_SECTION);
    expect(harnessSections("connected")[0]).toBe(DEFAULT_HARNESS_SECTION);
  });
});

describe("isTabId / tabFromHash / isHarnessSection", () => {
  // All three guard the hash, which is whatever somebody typed or a stale
  // bookmark kept: an unknown segment has to read as "no opinion" rather
  // than select a tab or a section that does not exist.
  it("accepts the five tabs and refuses anything else", () => {
    for (const tab of ["chat", "slack", "access", "harness", "secrets"]) {
      expect(isTabId(tab)).toBe(true);
    }
    for (const junk of ["settings", "learnings", "overview", "", null]) {
      expect(isTabId(junk)).toBe(false);
    }
  });

  it("lands the retired settings segment on Secrets, and nothing else anywhere", () => {
    // Settings became Secrets (issue #368); a bookmark from before still
    // opens the tab it meant.
    expect(tabFromHash("settings")).toBe("secrets");
    expect(tabFromHash("secrets")).toBe("secrets");
    expect(tabFromHash("chat")).toBe("chat");
    for (const junk of ["overview", "permissions", "", null, undefined, 3]) {
      expect(tabFromHash(junk)).toBeNull();
    }
  });

  it("accepts every section and refuses anything else", () => {
    for (const section of ["runtime", "persona", "skills", "tooling", "a2a", "memory", "learnings"]) {
      expect(isHarnessSection(section)).toBe(true);
    }
    // Permissions moved to the Access tab (issue #365): a stale bookmark to
    // the old section reads as no opinion, like any other unknown segment.
    for (const junk of ["chat", "secrets", "permissions", "", null]) {
      expect(isHarnessSection(junk)).toBe(false);
    }
  });
});

describe("defaultTab", () => {
  it("is chat when ready", () => {
    expect(defaultTab("ready")).toBe("chat");
  });

  it.each(["pending", "provisioning", "failed", "destroying", "destroy_failed"])(
    "is null (no tab selected, the header owns the story) for %s",
    (status) => {
      expect(defaultTab(status)).toBeNull();
    },
  );

  it.each(["pending", "provisioning", "failed"])(
    "is null for a connected agent that is not ready (%s): the checklist is its page",
    (status) => {
      expect(defaultTab(status, "connected")).toBeNull();
      expect(defaultTab(status, "connected", true)).toBeNull();
    },
  );

  it("lands a ready connected agent on a tab it has, never on none", () => {
    // No Chat to land on. Access for the caller who is offered it, Harness
    // for everyone else: both are in `primaryTabs` for the same arguments.
    expect(defaultTab("ready", "connected", true)).toBe("access");
    expect(primaryTabs(true, "connected")).toContain("access");
    expect(defaultTab("ready", "connected")).toBe("harness");
    expect(primaryTabs(false, "connected")).toContain("harness");
  });

  it("is still chat for a ready composed agent when the kind is named", () => {
    expect(defaultTab("ready", "composed")).toBe("chat");
  });
});
