// The agent detail tab model. Pure so the visibility and default-selection
// rules are unit-tested; the component only maps over what these return.
export type TabId =
  | "overview"
  | "chat"
  | "memory"
  | "learnings"
  | "skills"
  | "tooling"
  | "slack"
  | "access";

export const TAB_LABELS: Record<TabId, string> = {
  overview: "Overview",
  chat: "Chat",
  memory: "Memory",
  learnings: "Learnings",
  skills: "Skills",
  tooling: "Tooling",
  slack: "Slack",
  access: "Access",
};

/**
 * The tab list, in display order. Two-lane Slack (2026-08-17): the Slack
 * tab is unconditional now — the channels lane is open to everyone in the
 * room, and the setup ceremony is self-service for everyone too
 * (slack self-service, same date) — nothing about Slack is gated in the
 * UI anymore.
 *
 * Access (access-control Task C2) is the one tab that stays conditional,
 * and deliberately NOT on anything the client can be trusted to assert
 * (never `whoami.operator` — see `Whoami`'s own WATCH ITEM doc comment in
 * api.ts) — `showAccess` is the caller's own probe of whether `GET
 * /api/agents/{name}/invites` answered 200 for them (AgentDetail.svelte
 * runs that query independently of which tab is selected and passes its
 * success/failure in here), so the tab is exactly as real as the server's
 * own owner-or-operator gate on that route, never a client-side guess.
 * Appended last, after Slack, so an existing screenshot/expectation of the
 * first six tabs' order never shifts.
 */
export function visibleTabs(showAccess = false): TabId[] {
  const base: TabId[] = ["overview", "chat", "memory", "learnings", "skills", "tooling", "slack"];
  return showAccess ? [...base, "access"] : base;
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
