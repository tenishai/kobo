// The agent detail tab model. Pure so the visibility and default-selection
// rules are unit-tested; the component only maps over what these return.
//
// Five tabs on one row, no menu (harness wave, 2026-09-23). The row used to
// be four tabs and a More menu holding Learnings, Memory, Skills, Tooling
// and Secrets, which meant the five things that actually describe an agent
// were the five things nobody could see. They are SECTIONS of one Harness
// tab now -- what runs the agent: its model, its runtime, its persona, its
// skills, its tooling, which other agents it may ask, its memory and what it
// has learned.
//
// Settings became Secrets (issue #368): the tab holds the agent's secrets
// and variables and nothing else. The build log is a button in the page
// header, and the two changes there is no way back from (convert, destroy)
// sit at the bottom of Edit details. `tabFromHash` still reads the old
// `settings` segment, so a bookmark from before lands on Secrets.
//
// Connected agents (design 2026-09-21) are the thing here that hides tabs by
// what an agent IS rather than by what the caller may do: the room did not
// compose a connected agent, so there is no bundle behind Chat or behind
// most of the Harness sections, and it made no chat app for Slack. An
// absent tab is the honest answer; a disabled one would promise something
// that is never coming. Harness survives for one, reduced to Model and
// runtime -- a connected agent still runs on an image, and which one is the
// question this tab exists to answer. Access is the other exception and
// stays exactly as it is for a composed agent -- who may administer an agent
// is a question about the caller, and it has the same answer for both kinds.
import { KIND_COMPOSED, KIND_CONNECTED } from "./api";

export type TabId = "chat" | "slack" | "access" | "harness" | "secrets";

export const TAB_LABELS: Record<TabId, string> = {
  chat: "Chat",
  slack: "Slack",
  access: "Access",
  harness: "Harness",
  secrets: "Secrets",
};

const TAB_IDS = Object.keys(TAB_LABELS) as TabId[];

/** Hash segments that named a tab once and still have to land somewhere:
 * `settings` was the Secrets tab's name until issue #368. */
const RETIRED_TAB_IDS: Record<string, TabId> = {
  settings: "secrets",
};

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
 * Secrets, plus Access when the caller may administer it: no Chat (the room
 * has no way to talk to it) and no Slack (the room did not make its chat
 * app).
 */
export function primaryTabs(
  showAccess = false,
  kind: string = KIND_COMPOSED,
  opts: { slackRelay?: boolean; slackLegacy?: boolean } = {},
): TabId[] {
  // Slack as a vendor on Access (issue #371): when the room makes Slack
  // apps itself (`slackRelay`), Slack lives on the Access tab and the Slack
  // tab shows only for an agent whose app came from the paste flow
  // (`slackLegacy`), which keeps its tab exactly as it was.
  const slack: TabId[] = opts.slackRelay && !opts.slackLegacy ? [] : ["slack"];
  const talk: TabId[] = kind === KIND_CONNECTED ? [] : ["chat", ...slack];
  const access: TabId[] = showAccess ? ["access"] : [];
  return [...talk, ...access, "harness", "secrets"];
}

/** One section of the Harness tab -- the left-hand list inside it. */
export type HarnessSectionId =
  | "runtime"
  | "identity"
  | "persona"
  | "skills"
  | "tooling"
  | "a2a"
  | "memory"
  | "learnings"
  | "activity";

export const HARNESS_SECTION_LABELS: Record<HarnessSectionId, string> = {
  runtime: "Model and runtime",
  identity: "Identity and access",
  persona: "Persona",
  skills: "Skills",
  tooling: "Tooling",
  a2a: "Agent to agent",
  memory: "Memory",
  learnings: "Learnings",
  activity: "Activity",
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
 *
 * Agent to agent (issue #368) is the Other agents block that lived on the
 * Access tab: which agents from other teams this one may ask, and who has
 * asked to reach it. Pairs are about reaching agents, not about a bundle,
 * so both kinds have the section -- but only for a caller who may
 * administer the agent (`canAdminister`, the same invites probe that shows
 * the Access tab), since the routes behind it answer nobody else.
 */
export function harnessSections(
  kind: string = KIND_COMPOSED,
  opts: { accessOn?: boolean; canAdminister?: boolean } = {},
): HarnessSectionId[] {
  // Identity is true of any agent the room deploys, connected included:
  // both kinds run as the service account the room made for them. With the
  // permission service on, access is asked for on the Access tab (what this
  // agent can reach, issue #365) and the identity facts move to one line
  // there, so the section goes (#292). Permissions was a section here until
  // that wave; the Harness is what runs the agent, and what it may reach is
  // an Access question.
  const all: HarnessSectionId[] =
    kind === KIND_CONNECTED
      ? [DEFAULT_HARNESS_SECTION, "identity", "a2a", "activity"]
      : [...HARNESS_SECTION_IDS];
  return all.filter(
    (id) => !(opts.accessOn && id === "identity") && !(!opts.canAdminister && id === "a2a"),
  );
}

/** Whether a hash segment names a tab. The hash is whatever somebody typed
 * or a stale bookmark kept, so an unknown segment has to read as "no
 * opinion" rather than select something that does not exist. */
export function isTabId(value: unknown): value is TabId {
  return typeof value === "string" && (TAB_IDS as string[]).includes(value);
}

/** The tab a hash segment names, today's ids and the retired `settings`
 * alike (it lands on Secrets); null for anything else. */
export function tabFromHash(value: unknown): TabId | null {
  if (isTabId(value)) {
    return value;
  }
  return typeof value === "string" ? (RETIRED_TAB_IDS[value] ?? null) : null;
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
 * A ready connected agent has no Chat to land on, and landing on nothing
 * left the page blank under its tab bar. It lands on Access when the caller
 * is offered it (`showAccess`, the same probe `primaryTabs` takes): what the
 * agent may reach, and its Slack row, are what an owner comes here to
 * change. Anyone else lands on Harness, which every caller has.
 */
export function defaultTab(
  status: string,
  kind: string = KIND_COMPOSED,
  showAccess = false,
): TabId | null {
  if (status !== "ready") {
    return null;
  }
  if (kind === KIND_CONNECTED) {
    return showAccess ? "access" : "harness";
  }
  return "chat";
}
