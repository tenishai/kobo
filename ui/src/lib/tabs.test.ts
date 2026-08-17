import { describe, expect, it } from "vitest";
import { TAB_LABELS, visibleTabs } from "./tabs";

describe("visibleTabs", () => {
  it("hides slack when not visible", () => {
    expect(visibleTabs(false)).toEqual(["memory", "skills", "tooling"]);
  });

  it("includes slack last when visible", () => {
    expect(visibleTabs(true)).toEqual(["memory", "skills", "tooling", "slack"]);
  });
});

describe("TAB_LABELS", () => {
  it("labels every tab", () => {
    expect(TAB_LABELS).toEqual({
      memory: "Memory",
      skills: "Skills",
      tooling: "Tooling",
      slack: "Slack",
    });
  });
});
