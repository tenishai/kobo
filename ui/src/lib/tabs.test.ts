import { describe, expect, it } from "vitest";

import {
  DEFAULT_HARNESS_SECTION,
  HARNESS_SECTION_LABELS,
  TAB_LABELS,
  defaultTab,
  harnessSections,
  isHarnessSection,
  isTabId,
  primaryTabs,
} from "./tabs";

describe("primaryTabs", () => {
  it("is chat, slack, harness and settings with no argument", () => {
    expect(primaryTabs()).toEqual(["chat", "slack", "harness", "settings"]);
  });

  it("appends access before harness when the invites probe succeeded", () => {
    // Five tabs on one row, in the order somebody reads them: talk to it,
    // put it in Slack, say who may use it, look at what runs it, then the
    // back office. Settings stays last: it is not one of the things an
    // agent is for.
    expect(primaryTabs(true)).toEqual(["chat", "slack", "access", "harness", "settings"]);
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
      "settings",
    ]);
  });

  it("drops chat and slack from a connected agent's row, and keeps harness and settings", () => {
    // Harness survives because a connected agent still runs on something,
    // and Settings because a build log and a destroy belong to any workload
    // the room deploys. Chat and Slack do not: the room has no way to talk
    // to it and never made its chat app.
    expect(primaryTabs(false, "connected")).toEqual(["harness", "settings"]);
  });

  it("keeps access for a connected agent, exactly as for a composed one", () => {
    expect(primaryTabs(true, "connected")).toEqual(["access", "harness", "settings"]);
  });

  it("treats an unknown kind as composed, rather than hiding everything", () => {
    expect(primaryTabs(true, "adopted")).toEqual([
      "chat",
      "slack",
      "access",
      "harness",
      "settings",
    ]);
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

describe("isTabId / isHarnessSection", () => {
  // Both guard the hash, which is whatever somebody typed or a stale
  // bookmark kept: an unknown segment has to read as "no opinion" rather
  // than select a tab or a section that does not exist.
  it("accepts the five tabs and refuses anything else", () => {
    for (const tab of ["chat", "slack", "access", "harness", "settings"]) {
      expect(isTabId(tab)).toBe(true);
    }
    for (const junk of ["secrets", "learnings", "overview", "", null]) {
      expect(isTabId(junk)).toBe(false);
    }
  });

  it("accepts the six sections and refuses anything else", () => {
    for (const section of ["runtime", "persona", "skills", "tooling", "memory", "learnings"]) {
      expect(isHarnessSection(section)).toBe(true);
    }
    for (const junk of ["chat", "secrets", "", null]) {
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

  it.each(["ready", "pending", "provisioning", "failed"])(
    "is null at every status for a connected agent (%s): the checklist is its page",
    (status) => {
      expect(defaultTab(status, "connected")).toBeNull();
    },
  );

  it("is still chat for a ready composed agent when the kind is named", () => {
    expect(defaultTab("ready", "composed")).toBe("chat");
  });
});
