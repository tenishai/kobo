import { describe, expect, it } from "vitest";
import { defaultTab, TAB_LABELS, visibleTabs } from "./tabs";

describe("visibleTabs", () => {
  it("always includes slack last, with no argument", () => {
    expect(visibleTabs()).toEqual([
      "overview",
      "chat",
      "memory",
      "learnings",
      "skills",
      "tooling",
      "slack",
    ]);
  });

  it("omits access when showAccess is false", () => {
    expect(visibleTabs(false)).toEqual([
      "overview",
      "chat",
      "memory",
      "learnings",
      "skills",
      "tooling",
      "slack",
    ]);
  });

  it("appends access last when showAccess is true", () => {
    expect(visibleTabs(true)).toEqual([
      "overview",
      "chat",
      "memory",
      "learnings",
      "skills",
      "tooling",
      "slack",
      "access",
    ]);
  });
});

describe("TAB_LABELS", () => {
  it("labels every tab", () => {
    expect(TAB_LABELS).toEqual({
      overview: "Overview",
      chat: "Chat",
      memory: "Memory",
      learnings: "Learnings",
      skills: "Skills",
      tooling: "Tooling",
      slack: "Slack",
      access: "Access",
    });
  });
});

describe("defaultTab", () => {
  it("lands on chat when the agent is ready", () => {
    expect(defaultTab("ready")).toBe("chat");
  });

  it.each(["pending", "provisioning", "failed", "destroying", "destroy_failed"])(
    "lands on overview for status %s",
    (status) => {
      expect(defaultTab(status)).toBe("overview");
    },
  );
});
