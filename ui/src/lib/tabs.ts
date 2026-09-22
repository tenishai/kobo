// The agent detail tab model. Pure so the visibility and default-selection
// rules are unit-tested; the component only maps over what these return.
//
// Connected agents (design 2026-09-21) are the first thing here that hides
// tabs by what an agent IS rather than by what the caller may do: the room
// did not compose a connected agent, so there is no bundle behind Chat,
// Learnings, Memory, Skills or Tooling, and it made no chat app for Slack.
// An absent tab is the honest answer; a disabled one would promise
// something that is never coming. Access is the exception and stays exactly
// as it is for a composed agent -- who may administer an agent is a
// question about the caller, and it has the same answer for both kinds.
import { KIND_COMPOSED, KIND_CONNECTED } from "./api";

export type TabId =
  | "chat"
  | "learnings"
  | "memory"
  | "skills"
  | "tooling"
  | "slack"
  | "secrets"
  | "access";

export const TAB_LABELS: Record<TabId, string> = {
  chat: "Chat",
  learnings: "Learnings",
  memory: "Memory",
  skills: "Skills",
  tooling: "Tooling",
  slack: "Slack",
  secrets: "Secrets",
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
 *
 * `kind` is the agent's own `kind` off either read route. A connected agent
 * keeps Access and nothing else: no Chat (the room has no way to talk to
 * it) and no Slack (the room did not make its chat app).
 */
export function primaryTabs(showAccess = false, kind: string = KIND_COMPOSED): TabId[] {
  const base: TabId[] = kind === KIND_CONNECTED ? [] : ["chat", "slack"];
  return showAccess ? [...base, "access"] : base;
}

/** The four Advanced tabs that read the BUNDLE the room composed. Not the
 * Advanced group itself any more -- `advancedTabs` below is that, and the
 * page asks it rather than this constant, because the group's membership
 * now depends on the kind of agent. Never reordered. */
export const ADVANCED_TABS = ["learnings", "memory", "skills", "tooling"] as const;

/** The one Advanced tab a connected agent keeps (secrets and environment
 * design 2026-09-22). Every other tab in that group reads the bundle the
 * room composed, and a connected agent has none; this one reads the
 * agent's own environment, which a team's service has exactly as much of
 * as an agent the room built. It is the reason `advancedTabs` no longer
 * answers an empty list for a connected agent. */
const CONNECTED_ADVANCED_TABS = ["secrets"] as const;

/** Every Advanced tab, or just Secrets for a connected agent: the other
 * four read the bundle the room composed, and a connected agent has none.
 * The page hides the Advanced toggle itself when this is empty, which no
 * longer happens for either kind. */
export function advancedTabs(kind: string = KIND_COMPOSED): TabId[] {
  return kind === KIND_CONNECTED
    ? [...CONNECTED_ADVANCED_TABS]
    : [...ADVANCED_TABS, ...CONNECTED_ADVANCED_TABS];
}

/**
 * Which tab a fresh mount should land on: Chat when the agent is ready to
 * talk to (the whole point of the page at that point), and NO tab at all
 * for every other status -- pre-ready the header carries the checklist and
 * every tab is disabled, so selecting one would light up an empty panel.
 *
 * A connected agent never lands on a tab at any status: it has no Chat, and
 * what its page is for is the status, the checklist and the build log, all
 * of which render above the tab bar.
 */
export function defaultTab(status: string, kind: string = KIND_COMPOSED): TabId | null {
  if (kind === KIND_CONNECTED) {
    return null;
  }
  return status === "ready" ? "chat" : null;
}
