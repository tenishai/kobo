// What the dashboard has to decide about a list of agents, as pure
// functions over the rows `agentsQuery` already returns: the one line a
// card says about an agent, the groups a picker can offer, and the filter
// the search box applies. No fetching and no state -- the page owns the
// two strings a person typed or picked, and hands them here.

import type { AgentSummary } from "./api";

/** What a card says when an agent has told us nothing about itself. */
export const NO_DESCRIPTION = "No description yet.";

/** More agents than this on the dashboard and the controls appear. Six fit
 * on a screen; the seventh is when looking turns into searching. */
export const CONTROLS_OVER = 6;

function trimmed(value: string | null | undefined): string {
  return (value ?? "").trim();
}

/**
 * The one line under an agent's name: what it can do, in its own words if
 * it has earned them.
 *
 * Capability first -- one plain sentence, written room-side once the agent
 * first reaches ready. Then the persona's name, which at least says who is
 * answering. Then the purpose tags, which is the tier most cards land on
 * today: `GET /api/agents` sends tags but neither of the first two, so
 * without this a whole dashboard would read "No description yet." Both
 * `capability` and `persona` are optional on the wire for exactly that
 * reason -- when the list route starts sending them, cards move up a tier
 * with no change here.
 */
export function agentDescription(agent: AgentSummary): string {
  return (
    trimmed(agent.capability) ||
    trimmed(agent.persona) ||
    agent.purpose_tags.join(", ") ||
    NO_DESCRIPTION
  );
}

/** Every group present in the list, once each, sorted. Agents with no
 * group of their own are not a group. */
export function agentGroups(agents: AgentSummary[]): string[] {
  const groups = new Set<string>();
  for (const agent of agents) {
    if (agent.group_name) groups.add(agent.group_name);
  }
  return [...groups].sort();
}

/**
 * The list a person is actually looking at: `query` matched against the
 * name, the group, the persona and the capability (plus whatever the card
 * is showing in their place, so searching for what you can read works),
 * and `group` narrowing to one group. Either can be empty, meaning "no
 * opinion". Order is never changed.
 */
export function filterAgents(
  agents: AgentSummary[],
  query: string,
  group: string,
): AgentSummary[] {
  const needle = query.trim().toLowerCase();
  return agents.filter((agent) => {
    if (group && agent.group_name !== group) return false;
    if (!needle) return true;
    const haystack = [
      agent.name,
      agent.group_name ?? "",
      trimmed(agent.persona),
      trimmed(agent.capability),
      agentDescription(agent),
    ]
      .join(" ")
      .toLowerCase();
    return haystack.includes(needle);
  });
}
