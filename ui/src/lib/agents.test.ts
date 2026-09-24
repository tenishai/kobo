import { describe, expect, it } from "vitest";
import {
  CONTROLS_OVER,
  NO_DESCRIPTION,
  agentDescription,
  agentGroups,
  filterAgents,
} from "./agents";
import type { AgentSummary } from "./api";

const BASE: AgentSummary = {
  name: "warden",
  status: "ready",
  model: "m",
  model_label: "M",
  created_at: "2026-08-17T00:00:00Z",
  has_avatar: false,
  created_by: null,
  group_name: null,
  archetype: null,
  purpose_tags: [],
  kind: "composed",
};

function agent(fields: Partial<AgentSummary>): AgentSummary {
  return { ...BASE, ...fields };
}

describe("agentDescription", () => {
  it("prefers the capability sentence", () => {
    expect(
      agentDescription(
        agent({
          capability: "Answers billing questions.",
          persona: "Ward",
          purpose_tags: ["billing"],
        }),
      ),
    ).toBe("Answers billing questions.");
  });

  it("falls back to the persona name", () => {
    expect(
      agentDescription(agent({ persona: "Ward", purpose_tags: ["billing"] })),
    ).toBe("Ward");
  });

  // The list route sends purpose tags today and neither of the two above,
  // so this is the tier most cards actually land on.
  it("falls back to the purpose tags, in one line", () => {
    expect(agentDescription(agent({ purpose_tags: ["billing", "refunds"] }))).toBe(
      "billing, refunds",
    );
  });

  it("says so plainly when the agent has told us nothing", () => {
    expect(agentDescription(agent({}))).toBe(NO_DESCRIPTION);
    expect(agentDescription(agent({ capability: "   ", persona: "" }))).toBe(
      NO_DESCRIPTION,
    );
  });
});

describe("agentGroups", () => {
  it("lists each group once, sorted, ignoring the ungrouped", () => {
    expect(
      agentGroups([
        agent({ name: "a", group_name: "sre" }),
        agent({ name: "b", group_name: null }),
        agent({ name: "c", group_name: "design" }),
        agent({ name: "d", group_name: "sre" }),
      ]),
    ).toEqual(["design", "sre"]);
  });
});

describe("filterAgents", () => {
  const agents = [
    agent({ name: "warden", group_name: "sre", capability: "Watches the fleet." }),
    agent({ name: "ward", group_name: "design", persona: "Quiet librarian" }),
    agent({ name: "clio", group_name: "sre", purpose_tags: ["billing"] }),
  ];

  it("keeps everything when nothing is asked for", () => {
    expect(filterAgents(agents, "", "").map((a) => a.name)).toEqual([
      "warden",
      "ward",
      "clio",
    ]);
  });

  it("matches a name, however it is cased, and matches partially", () => {
    expect(filterAgents(agents, "WARD", "").map((a) => a.name)).toEqual([
      "warden",
      "ward",
    ]);
  });

  it("matches the group, the persona and the capability", () => {
    expect(filterAgents(agents, "design", "").map((a) => a.name)).toEqual(["ward"]);
    expect(filterAgents(agents, "librarian", "").map((a) => a.name)).toEqual([
      "ward",
    ]);
    expect(filterAgents(agents, "fleet", "").map((a) => a.name)).toEqual([
      "warden",
    ]);
    expect(filterAgents(agents, "billing", "").map((a) => a.name)).toEqual(["clio"]);
  });

  it("narrows to one group, and combines with the search", () => {
    expect(filterAgents(agents, "", "sre").map((a) => a.name)).toEqual([
      "warden",
      "clio",
    ]);
    expect(filterAgents(agents, "clio", "sre").map((a) => a.name)).toEqual(["clio"]);
    expect(filterAgents(agents, "clio", "design")).toEqual([]);
  });

  it("returns nothing rather than everything when nothing matches", () => {
    expect(filterAgents(agents, "nobody", "")).toEqual([]);
  });
});

describe("CONTROLS_OVER", () => {
  // Six cards fit on one screen and need no help; the seventh is when
  // looking becomes searching.
  it("is six", () => {
    expect(CONTROLS_OVER).toBe(6);
  });
});
