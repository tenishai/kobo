// The agent detail tab model. Pure so the visibility rule is unit-tested;
// the component only maps over what this returns.
export type TabId = "memory" | "skills" | "tooling" | "slack";

export const TAB_LABELS: Record<TabId, string> = {
  memory: "Memory",
  skills: "Skills",
  tooling: "Tooling",
  slack: "Slack",
};

export function visibleTabs(slackVisible: boolean): TabId[] {
  const tabs: TabId[] = ["memory", "skills", "tooling"];
  return slackVisible ? [...tabs, "slack"] : tabs;
}
