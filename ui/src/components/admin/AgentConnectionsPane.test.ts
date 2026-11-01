// @vitest-environment jsdom
// Admin > Agent connections (issue #371): one card per agent with what it holds
// at each vendor; Revoke on a held row calls the agent's existing revoke route,
// the one the Access tab uses, never a route of its own; the deep link puts
// that agent's card first; the search box narrows by agent, vendor or repository.
import { fireEvent, screen, within } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";

import AgentConnectionsPane from "./AgentConnectionsPane.svelte";
import { renderWithClient } from "../../test-utils/render";

const AGENTS = {
  fetched_at: "2026-11-01T10:00:00Z",
  agents: [
    {
      name: "observer",
      team: "platform",
      status: "ready",
      vendors: [
        {
          vendor: "github",
          label: "GitHub",
          connections: [{ id: "github-observer", name: "GitHub for observer", repositories: ["acme-eng/ledger", "acme-eng/billing"] }],
          grants: [
            { id: 41, integration: "github-observer", permission: "github_repos_read", tier: "read", expires_at: null, granted_by: "admin@example.com", state: "active" },
          ],
          leases: [
            { id: 7, integration: "github-observer", permissions: ["github_repos_read"], task: "list the open pull requests", issued_at: "2099-01-01T00:00:00Z", expires_at: "2099-01-01T01:00:00Z", via: "github" },
          ],
        },
      ],
    },
    { name: "quiet", team: null, status: "ready", vendors: [] },
  ],
};

function stub(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/admin/access/agents")) {
        return new Response(JSON.stringify(AGENTS), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      if (init?.method === "DELETE" && url.includes("/access/grants/")) {
        return new Response(JSON.stringify({ revoked: 41, leases_ended: 1 }), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      throw new Error(`unstubbed fetch: ${init?.method ?? "GET"} ${url}`);
    }),
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("AgentConnectionsPane", () => {
  it("lists every agent, what each holds and borrows, and revokes through the agent's own route", async () => {
    stub();
    renderWithClient(AgentConnectionsPane, { item: "quiet" });

    // The deep-linked agent comes first; the one with nothing is still a card.
    const cards = await screen.findAllByRole("article");
    expect(cards.map((c) => c.getAttribute("aria-label"))).toEqual(["quiet", "observer"]);
    expect(within(cards[0]).getByText("Holds nothing at any vendor.")).not.toBeNull();

    const observer = within(cards[1]);
    expect(observer.getByText("GitHub")).not.toBeNull();
    expect(observer.getByText(/GitHub for observer/).textContent).toContain("2 repositories");
    // The held row and the borrowed row both name the permission in words.
    expect(observer.getAllByText("repos read")).toHaveLength(2);
    expect(observer.getByText("list the open pull requests")).not.toBeNull();
    expect(cards[1].querySelector(".vendor-mark")).not.toBeNull();

    await fireEvent.click(observer.getByRole("button", { name: "Revoke" }));
    await vi.waitFor(() => {
      const call = vi.mocked(fetch).mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "DELETE");
      expect(call).toBeDefined();
      expect(String(call![0])).toBe("/api/agents/observer/access/grants/41");
    });
  });

  it("narrows the cards by agent, vendor or repository", async () => {
    stub();
    renderWithClient(AgentConnectionsPane);
    await screen.findAllByRole("article");
    const search = screen.getByLabelText("Find an agent, vendor or repository");

    await fireEvent.input(search, { target: { value: "billing" } });
    expect(screen.getAllByRole("article").map((c) => c.getAttribute("aria-label"))).toEqual(["observer"]);
    await fireEvent.input(search, { target: { value: "quiet" } });
    expect(screen.getAllByRole("article").map((c) => c.getAttribute("aria-label"))).toEqual(["quiet"]);
    await fireEvent.input(search, { target: { value: "nothing-like-this" } });
    expect(screen.queryAllByRole("article")).toHaveLength(0);
    expect(screen.getByText(/Nothing matches/)).not.toBeNull();
  });
});
