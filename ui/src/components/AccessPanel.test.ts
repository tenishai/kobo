// @vitest-environment jsdom
// The Access tab opens on the vendors (issue #368): the visibility line, the
// identity the agent runs as, then what it can reach. People and Other
// agents are not here any more (OwnersSheet and AgentToAgentPanel).
import { screen } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import AccessPanel from "./AccessPanel.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

const ACCESS = {
  agent: "warden",
  team: "growth",
  grants: [],
  pending: [],
  vendors: [
    { id: "datadog", kind: "service", label: "Datadog", account_noun: null, organizations: [],
      accounts: [{ id: "datadog", name: "datadog", connected: true, ceiling: ["datadog_monitors_read"], organization: null, account_id: null, repositories: [], has_repo_set: false }],
      groups: [{ name: "Monitors", permissions: [{ name: "datadog_monitors_read", tier: "read", description: "Monitors", label: null }] }], tiers: [] },
  ],
  can_request: true,
  can_decide: false,
};

describe("AccessPanel", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("is the visibility line, the identity line and the vendors, nothing about people or other agents", async () => {
    stubFetch({ "/api/agents/warden/access": ACCESS });
    renderWithClient(AccessPanel, {
      agentName: "warden",
      groupName: "growth",
      identity: { service_account: "agent-warden", gsa_email: "agent-warden@example-project.iam.gserviceaccount.com", project: "example-project" },
    });

    expect(await screen.findByRole("heading", { name: "Access" })).not.toBeNull();
    expect(screen.getByText("Everyone in growth can see this agent.")).not.toBeNull();
    expect(screen.getByText("agent-warden@example-project.iam.gserviceaccount.com")).not.toBeNull();
    // The vendors come right after, with their logos.
    const datadog = await screen.findByRole("button", { name: /Datadog/ });
    expect(datadog.querySelector(".vendor-mark")).not.toBeNull();
    const headings = screen.getAllByRole("heading").map((h) => h.textContent?.trim());
    expect(headings).toEqual(["Access", "What this agent can reach"]);
    expect(screen.queryByText("People")).toBeNull();
    expect(screen.queryByText("Other agents")).toBeNull();
    expect(screen.queryByRole("button", { name: "Add person" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Request another team's agent" })).toBeNull();
    // Only the access route is read: no grants, no invites, no requestable.
    const urls = vi.mocked(fetch).mock.calls.map(([url]) => String(url));
    expect(urls.every((u) => u.includes("/access"))).toBe(true);
  });

  it("says who can see an agent that is in no group, pointing at Owners", async () => {
    stubFetch({ "/api/agents/warden/access": ACCESS });
    renderWithClient(AccessPanel, { agentName: "warden", groupName: null });

    expect(await screen.findByText(/This agent is not in a group\./)).not.toBeNull();
    expect(screen.getByText(/added under Owners/)).not.toBeNull();
  });
});
