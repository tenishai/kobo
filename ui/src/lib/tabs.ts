// The agent detail tab model. Pure so the visibility and default-selection
// rules are unit-tested; the component only maps over what these return.
//
// Five tabs on one row, no menu (harness wave, 2026-09-23). The row used to
// be four tabs and a More menu holding Learnings, Memory, Skills, Tooling
// and Secrets, which meant the five things that actually describe an agent
// were the five things nobody could see. They are SECTIONS of one Harness
// tab now -- what runs the agent: its model, its runtime, its persona, its
// skills, its tooling, its memory and what it has learned -- and Secrets
// moved into Settings, beside the build log and the destroy, where the rest
// of the back office already lives.
//
// Connected agents (design 2026-09-21) are the thing here that hides tabs by
// what an agent IS rather than by what the caller may do: the room did not
// compose a connected agent, so there is no bundle behind Chat or behind
// five of the six Harness sections, and it made no chat app for Slack. An
// absent tab is the honest answer; a disabled one would promise something
// that is never coming. Harness survives for one, reduced to Model and
// runtime -- a connected agent still runs on an image, and which one is the
// question this tab exists to answer. Access is the other exception and
// stays exactly as it is for a composed agent -- who may administer an agent
// is a question about the caller, and it has the same answer for both kinds.
import { KIND_COMPOSED, KIND_CONNECTED } from "./api";

export type TabId = "chat" | "slack" | "access" | "harness" | "settings";

export const TAB_LABELS: Record<TabId, string> = {
  chat: "Chat",
  slack: "Slack",
  access: "Access",
  harness: "Harness",
  settings: "Settings",
};

const TAB_IDS = Object.keys(TAB_LABELS) as TabId[];

/**
 * Every tab, in display order. There is no second group any more: the whole
 * row is what this returns.
 *
 * Access stays the one conditional tab, and deliberately NOT on anything the
 * client can be trusted to assert (never `whoami.operator`) -- `showAccess`
 * is the caller's own probe of whether `GET /api/agents/{name}/invites`
 * answered 200 for them.
 *
 * `kind` is the agent's own `kind` off either read route. A connected agent
 * keeps Harness (Model and runtime only, see `harnessSections`) and
 * Settings, plus Access when the caller may administer it: no Chat (the room
 * has no way to talk to it) and no Slack (the room did not make its chat
 * app).
 */
export function primaryTabs(showAccess = false, kind: string = KIND_COMPOSED): TabId[] {
  const talk: TabId[] = kind === KIND_CONNECTED ? [] : ["chat", "slack"];
  const access: TabId[] = showAccess ? ["access"] : [];
  return [...talk, ...access, "harness", "settings"];
}

/** One section of the Harness tab -- the left-hand list inside it. */
export type HarnessSectionId =
  | "runtime"
  | "persona"
  | "skills"
  | "tooling"
  | "memory"
  | "learnings";

export const HARNESS_SECTION_LABELS: Record<HarnessSectionId, string> = {
  runtime: "Model and runtime",
  persona: "Persona",
  skills: "Skills",
  tooling: "Tooling",
  memory: "Memory",
  learnings: "Learnings",
};

const HARNESS_SECTION_IDS = Object.keys(HARNESS_SECTION_LABELS) as HarnessSectionId[];

/** Where the Harness tab opens, for both kinds of agent: the one section
 * every agent has something to say in. */
export const DEFAULT_HARNESS_SECTION: HarnessSectionId = "runtime";

/**
 * The sections the Harness list offers, in display order. Model and runtime
 * first because it is true of any agent the room deploys; the rest read the
 * bundle the room composed, so a connected agent has none of them and its
 * Harness is a single section with one muted line explaining why.
 */
export function harnessSections(kind: string = KIND_COMPOSED): HarnessSectionId[] {
  return kind === KIND_CONNECTED ? [DEFAULT_HARNESS_SECTION] : [...HARNESS_SECTION_IDS];
}

/** Whether a hash segment names a tab. The hash is whatever somebody typed
 * or a stale bookmark kept, so an unknown segment has to read as "no
 * opinion" rather than select something that does not exist. */
export function isTabId(value: unknown): value is TabId {
  return typeof value === "string" && (TAB_IDS as string[]).includes(value);
}

/** Whether a hash segment names a Harness section. Same guard, same reason. */
export function isHarnessSection(value: unknown): value is HarnessSectionId {
  return typeof value === "string" && (HARNESS_SECTION_IDS as string[]).includes(value);
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
