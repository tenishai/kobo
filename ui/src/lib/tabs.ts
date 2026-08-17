// The agent detail tab model. Pure so the visibility and default-selection
// rules are unit-tested; the component only maps over what these return.
export type TabId = "overview" | "chat" | "memory" | "skills" | "tooling" | "slack";

export const TAB_LABELS: Record<TabId, string> = {
  overview: "Overview",
  chat: "Chat",
  memory: "Memory",
  skills: "Skills",
  tooling: "Tooling",
  slack: "Slack",
};

export function visibleTabs(slackVisible: boolean): TabId[] {
  const tabs: TabId[] = ["overview", "chat", "memory", "skills", "tooling"];
  return slackVisible ? [...tabs, "slack"] : tabs;
}

/**
 * Which tab a fresh mount should land on: Chat when the agent is ready to
 * talk to (the whole point of the page at that point), Overview for every
 * other status (pending, provisioning, failed, destroying, destroy_failed,
 * or anything this room doesn't recognize yet) since that's where the
 * checklist and failure detail live.
 */
export function defaultTab(status: string): TabId {
  return status === "ready" ? "chat" : "overview";
}
