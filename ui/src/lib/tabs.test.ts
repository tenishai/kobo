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
  it("is the four grow-it sections in order", () => {
    expect(advancedTabs()).toEqual(["learnings", "memory", "skills", "tooling"]);
    expect(ADVANCED_TABS).toEqual(advancedTabs());
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

  it("has no advanced tabs at all for a connected agent", () => {
    expect(advancedTabs("connected")).toEqual([]);
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
