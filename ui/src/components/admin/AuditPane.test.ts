// @vitest-environment jsdom
// The Audit pane's two tabs (kobo#371): People is the room's own log with
// its actor and action selects; Agents reads what agents did with their
// connections from its own route, and those two selects are not there.
import { fireEvent, screen, waitFor } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import AuditPane from "./AuditPane.svelte";
import { renderWithClient, stubFetch } from "../../test-utils/render";

const PEOPLE = {
  rows: [], next_cursor: null, total_in_window: 0,
  actors: ["ops@example.com"], agents: ["ward"], families: ["agent"],
};
const AGENTS = {
  rows: [{
    id: "lease:7", at: "2026-10-30T12:00:00Z", actor: "agent:ward", actor_kind: "agent", action: "lease.minted",
    target_kind: "integration", target_id: "aws-prod", agent_name: "ward", outcome: "ok", source: "permission-service",
    summary: "ward borrowed read on AWS aws-prod for 1h: count instances", request_ip: null, user_agent: null,
    detail: {}, landed: null, landed_detail: null,
  }],
  total_in_window: 1, agents: ["ward"], outcomes: ["ok"],
};

describe("AuditPane", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("hides the actor and action selects on Agents and reads that tab's own route", async () => {
    // The agents needle comes first: the people one is a prefix of it.
    stubFetch({ "/api/admin/audit/agents": AGENTS, "/api/admin/audit": PEOPLE });
    renderWithClient(AuditPane);

    expect(await screen.findByRole("tab", { name: "People", selected: true })).not.toBeNull();
    expect(screen.getByLabelText("Actor")).not.toBeNull();
    expect(screen.getByLabelText("Action")).not.toBeNull();

    await fireEvent.click(screen.getByRole("tab", { name: "Agents" }));

    expect(screen.getByRole("tab", { name: "Agents", selected: true })).not.toBeNull();
    expect(screen.queryByLabelText("Actor")).toBeNull();
    expect(screen.queryByLabelText("Action")).toBeNull();
    expect(screen.getByLabelText("Agent")).not.toBeNull();
    expect(screen.getByLabelText("Outcome")).not.toBeNull();
    await waitFor(() => expect(screen.getByText(/ward borrowed read on AWS aws-prod/)).not.toBeNull());
    const urls = vi.mocked(fetch).mock.calls.map(([url]) => String(url));
    expect(urls.some((u) => u.startsWith("/api/admin/audit/agents"))).toBe(true);
    const csv = screen.getByRole("link", { name: "CSV" }) as HTMLAnchorElement;
    expect(csv.getAttribute("href")).toContain("/api/admin/audit/agents?format=csv");
  });
});
