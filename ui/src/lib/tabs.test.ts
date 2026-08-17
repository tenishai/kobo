import { describe, expect, it } from "vitest";
import { defaultTab, TAB_LABELS, visibleTabs } from "./tabs";

describe("visibleTabs", () => {
  it("always includes slack last", () => {
    expect(visibleTabs()).toEqual([
      "overview",
      "chat",
      "memory",
      "skills",
      "tooling",
      "slack",
    ]);
  });
});

describe("TAB_LABELS", () => {
  it("labels every tab", () => {
    expect(TAB_LABELS).toEqual({
      overview: "Overview",
      chat: "Chat",
      memory: "Memory",
      skills: "Skills",
      tooling: "Tooling",
      slack: "Slack",
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
