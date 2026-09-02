// The agent detail tab model. Pure so the visibility and default-selection
// rules are unit-tested; the component only maps over what these return.
export type TabId =
  | "chat"
  | "learnings"
  | "memory"
  | "skills"
  | "tooling"
  | "slack"
  | "access";

export const TAB_LABELS: Record<TabId, string> = {
  chat: "Chat",
  learnings: "Learnings",
  memory: "Memory",
  skills: "Skills",
  tooling: "Tooling",
  slack: "Slack",
  access: "Access",
};

/**
 * The always-visible tabs, in display order (UI-simplification wave,
 * 2026-09-02: the old flat seven-tab row split into primary + Advanced,
 * and the "overview" tab retired -- its content is the page header now).
 *
 * Access stays the one conditional tab, and deliberately NOT on anything
 * the client can be trusted to assert (never `whoami.operator`) --
 * `showAccess` is the caller's own probe of whether
 * `GET /api/agents/{name}/invites` answered 200 for them. Appended last,
 * same as before the split.
 */
export function primaryTabs(showAccess = false): TabId[] {
  const base: TabId[] = ["chat", "slack"];
  return showAccess ? [...base, "access"] : base;
}

/** The Advanced group: the grow-it sections most visitors never need on
 * day one. Collapsed or expanded by the component (owner-signal +
 * active-tab membership), never reordered. */
export const ADVANCED_TABS = ["learnings", "memory", "skills", "tooling"] as const;

export function advancedTabs(): TabId[] {
  return [...ADVANCED_TABS];
}

/**
 * Which tab a fresh mount should land on: Chat when the agent is ready to
 * talk to (the whole point of the page at that point), and NO tab at all
 * for every other status -- pre-ready the header carries the checklist and
 * every tab is disabled, so selecting one would light up an empty panel.
 */
export function defaultTab(status: string): TabId | null {
  return status === "ready" ? "chat" : null;
}
