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
});
