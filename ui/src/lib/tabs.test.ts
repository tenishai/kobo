import { describe, expect, it } from "vitest";

import {
  ADVANCED_TABS,
  TAB_LABELS,
  advancedTabs,
  defaultTab,
  primaryTabs,
} from "./tabs";

describe("primaryTabs", () => {
  it("is chat and slack with no argument", () => {
    expect(primaryTabs()).toEqual(["chat", "slack"]);
  });

  it("appends access last when the invites probe succeeded", () => {
    expect(primaryTabs(true)).toEqual(["chat", "slack", "access"]);
  });
});

describe("advancedTabs", () => {
  it("is the four bundle-backed grow-it sections, then Secrets", () => {
    expect(advancedTabs()).toEqual([
      "learnings",
      "memory",
      "skills",
      "tooling",
      "secrets",
    ]);
    // ADVANCED_TABS stays the bundle-backed four: it is what the page uses
    // to decide whether a selected tab belongs to the collapsible group,
    // and Secrets joins that group without joining that list's meaning.
    expect(ADVANCED_TABS).toEqual(["learnings", "memory", "skills", "tooling"]);
  });
});

describe("the tab model by kind (connected-agents design 2026-09-21)", () => {
  it("keeps every tab for a composed agent, named or defaulted", () => {
    expect(primaryTabs(false, "composed")).toEqual(primaryTabs());
    expect(primaryTabs(true, "composed")).toEqual(["chat", "slack", "access"]);
    expect(advancedTabs("composed")).toEqual(advancedTabs());
  });

  it("drops chat and slack for a connected agent", () => {
    expect(primaryTabs(false, "connected")).toEqual([]);
  });

  it("keeps access for a connected agent, exactly as for a composed one", () => {
    expect(primaryTabs(true, "connected")).toEqual(["access"]);
  });

  it("keeps only Secrets in the advanced group for a connected agent", () => {
    // Every other advanced tab reads the bundle the room composed. The
    // environment an agent runs with is not a bundle question, and a
    // team's own service has one exactly as much as an agent the room
    // built (secrets and environment design 2026-09-22).
    expect(advancedTabs("connected")).toEqual(["secrets"]);
  });

  it("puts Secrets last in the advanced group for a composed agent", () => {
    expect(advancedTabs("composed")).toEqual([
      "learnings",
      "memory",
      "skills",
      "tooling",
      "secrets",
    ]);
  });

  it("treats an unknown kind as composed, rather than hiding everything", () => {
    expect(primaryTabs(true, "adopted")).toEqual(["chat", "slack", "access"]);
    expect(advancedTabs("adopted")).toEqual(advancedTabs());
  });
});

describe("TAB_LABELS", () => {
  it("labels every tab and has no overview entry", () => {
    expect(TAB_LABELS).toEqual({
      chat: "Chat",
      learnings: "Learnings",
      memory: "Memory",
      skills: "Skills",
      tooling: "Tooling",
      slack: "Slack",
      secrets: "Secrets",
      access: "Access",
    });
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
