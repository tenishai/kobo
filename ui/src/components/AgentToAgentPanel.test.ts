// @vitest-environment jsdom
// Agent to agent (issue #368): the Other agents block of the Access tab as a
// Harness section. Same routes (invites, pairs, requestable, access). The
// picker is grouped by team and sends one request per ticked agent.
import { fireEvent, screen, waitFor, within } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import AgentToAgentPanel from "./AgentToAgentPanel.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

const EMPTY_INVITES = { incoming: [], outgoing: [], team_pairs: [] };
const EMPTY_REQUESTABLE = { requestable: [], already: [], not_ready: [], paired_group: "growth" };

/** Three teams' worth of agents as the server lists them for `warden`
 * (group growth): two other teams, one same-group ready agent that must
 * not show, one same-group agent still building, one already asked. */
const THREE_TEAMS = {
  requestable: [
    { name: "payroll-bot", group: "payments-eng", team: "payments", capability: "Handles refund inquiries.", purpose_tags: ["billing", "refunds"] },
    { name: "ledger", group: "finance", team: null, capability: null, purpose_tags: ["scheduling", "reminders"] },
    { name: "same-team-bot", group: "growth", team: null, capability: "Should never show.", purpose_tags: [] },
  ],
  already: [{ name: "iris", group: "payments-eng", status: "pending" }],
  not_ready: [{ name: "relay", group: "growth", status: "provisioning" }],
  paired_group: "growth",
};

/** Open the "Request another team's agent" disclosure. */
async function openPicker(): Promise<void> {
  await fireEvent.click(
    await screen.findByRole("button", { name: "Request another team's agent" }),
  );
}

function teamRow(name: RegExp): HTMLElement {
  return screen.getByRole("button", { name });
}

function postsTo(path: string): unknown[] {
  return vi
    .mocked(fetch)
    .mock.calls.filter(
      ([url, init]) => String(url) === path && (init as RequestInit | undefined)?.method === "POST",
    )
    .map(([, init]) => JSON.parse((init as RequestInit).body as string));
}

describe("AgentToAgentPanel", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("opens with the one sentence, the picker shut, and the table's empty sentence", async () => {
    stubFetch({
      "/api/agents/warden/invites": EMPTY_INVITES,
      "/api/agents/warden/requestable": EMPTY_REQUESTABLE,
    });
    renderWithClient(AgentToAgentPanel, { agentName: "warden" });

    expect(await screen.findByRole("heading", { name: "Agent to agent" })).not.toBeNull();
    expect(
      screen.getByText(/Which agents from other teams this one may ask for help, and who has asked to reach it\. Agents in the same group need nothing here\./),
    ).not.toBeNull();
    expect(
      await screen.findByText("No agent is paired with this one yet, and nobody has asked to reach it."),
    ).not.toBeNull();
    const picker = screen.getByRole("button", { name: "Request another team's agent" });
    expect(picker.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText(/Nobody to ask/)).toBeNull();

    await openPicker();
    expect(picker.getAttribute("aria-expanded")).toBe("true");
    expect(await screen.findByText(/Nobody to ask/)).not.toBeNull();
  });

  it("groups the picker by team, sorted by name, and leaves the viewer's own team out", async () => {
    stubFetch({
      "/api/agents/warden/invites": EMPTY_INVITES,
      "/api/agents/warden/requestable": THREE_TEAMS,
    });
    const { container } = renderWithClient(AgentToAgentPanel, { agentName: "warden" });

    await openPicker();
    await screen.findByText("finance");
    const teams = Array.from(container.querySelectorAll(".a2a__team-name")).map((el) => el.textContent?.trim());
    expect(teams).toEqual(["finance", "payments-eng"]);
    // Nothing from the viewer's own team, ready or not, and no agent row
    // shows before its team is opened.
    expect(screen.queryByText("growth")).toBeNull();
    expect(screen.queryByText("same-team-bot")).toBeNull();
    expect(screen.queryByText("relay")).toBeNull();
    expect(screen.queryByText("payroll-bot")).toBeNull();
    expect(within(teamRow(/payments-eng/)).getByText("2 agents")).not.toBeNull();

    await fireEvent.click(teamRow(/payments-eng/));
    expect(await screen.findByText("payroll-bot")).not.toBeNull();
    const description = screen.getByText("Handles refund inquiries.");
    expect(description.getAttribute("title")).toBe("Handles refund inquiries.");
    expect(screen.getByRole("checkbox", { name: "Pick payroll-bot" })).not.toBeNull();

    await fireEvent.click(teamRow(/finance/));
    expect(await screen.findByText("scheduling, reminders")).not.toBeNull();
  });

  it("ticking two agents across teams and pressing Request access sends one request each, then shuts", async () => {
    stubFetch({
      "/api/agents/warden/invites": EMPTY_INVITES,
      "/api/agents/warden/requestable": THREE_TEAMS,
    });
    renderWithClient(AgentToAgentPanel, { agentName: "warden" });

    await openPicker();
    await screen.findByText("finance");
    const request = screen.getByRole("button", { name: "Request access" });
    expect((request as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText("0 agents picked")).not.toBeNull();

    await fireEvent.click(teamRow(/payments-eng/));
    await fireEvent.click(await screen.findByRole("checkbox", { name: "Pick payroll-bot" }));
    await fireEvent.click(teamRow(/finance/));
    await fireEvent.click(await screen.findByRole("checkbox", { name: "Pick ledger" }));
    expect(screen.getByText("2 agents picked")).not.toBeNull();
    expect((request as HTMLButtonElement).disabled).toBe(false);

    await fireEvent.click(request);

    await waitFor(() => {
      expect(postsTo("/api/agents/warden/invites")).toEqual([
        { to_agent: "payroll-bot" },
        { to_agent: "ledger" },
      ]);
    });
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "Request another team's agent" }).getAttribute("aria-expanded"),
      ).toBe("false");
    });
  });

  it("an agent already asked shows its state in place of a checkbox", async () => {
    stubFetch({
      "/api/agents/warden/invites": EMPTY_INVITES,
      "/api/agents/warden/requestable": {
        requestable: [],
        already: [
          { name: "iris", group: "payments-eng", status: "pending" },
          { name: "clio", group: "payments-eng", status: "accepted" },
        ],
        not_ready: [{ name: "echo", group: "finance", status: "provisioning" }],
        paired_group: "growth",
      },
    });
    renderWithClient(AgentToAgentPanel, { agentName: "warden" });

    await openPicker();
    await fireEvent.click(await screen.findByRole("button", { name: /payments-eng/ }));
    expect(await screen.findByText("iris")).not.toBeNull();
    expect(screen.getByText("Waiting for its owner")).not.toBeNull();
    expect(screen.getByText("clio")).not.toBeNull();
    expect(screen.getByText("Paired")).not.toBeNull();
    expect(screen.queryByRole("checkbox")).toBeNull();

    await fireEvent.click(screen.getByRole("button", { name: /finance/ }));
    expect(await screen.findByText("echo")).not.toBeNull();
    expect(screen.getByText("Not ready")).not.toBeNull();
    expect(screen.queryByRole("checkbox")).toBeNull();
  });

  it("with the permission service on, Request access posts ask permission requests, and the waiting and approved rows carry team and date", async () => {
    // Issue #310: asking another team's agent is a permission approved in
    // Review, not an invite the other owner accepts.
    stubFetch({
      "/api/agents/warden/invites": EMPTY_INVITES,
      "/api/agents/warden/pairs": { pairs: [{ name: "auditor", group: "security", via: "grant" }] },
      "/api/agents/warden/access": {
        agent: "warden",
        team: "growth",
        grants: [],
        pending: [
          {
            id: 4, agent: "warden", integration: "ledger", permissions: ["a2a_ask"], tiers: { read: 1 }, kind: "standing",
            status: "pending", requested_by: "owner@example.com", note: null, created_at: "2026-10-29T12:00:00Z",
            decided_at: null, decided_by: null, reason: null, outcome: {},
          },
        ],
        vendors: [],
        can_request: true,
        can_decide: false,
      },
      "/api/agents/warden/requestable": {
        requestable: [{ name: "payroll-bot", group: "payments-eng", team: null, capability: null, purpose_tags: [] }],
        already: [{ name: "ledger", group: "finance", status: "pending" }],
        not_ready: [],
        paired_group: "growth",
      },
    });
    const { container } = renderWithClient(AgentToAgentPanel, { agentName: "warden" });

    const waiting = (await screen.findByText("Waiting for review")).closest("tr") as HTMLElement;
    expect(within(waiting).getByText("ledger")).not.toBeNull();
    expect(within(waiting).getByText("finance")).not.toBeNull();
    expect(within(waiting).getByText(/Oct \d+/)).not.toBeNull();
    const approved = (await screen.findByText("Approved in Review")).closest("tr") as HTMLElement;
    expect(within(approved).getByText("auditor")).not.toBeNull();
    expect(within(approved).getByText("security")).not.toBeNull();

    await openPicker();
    await fireEvent.click(await screen.findByRole("button", { name: /payments-eng/ }));
    await fireEvent.click(await screen.findByRole("checkbox", { name: "Pick payroll-bot" }));
    await fireEvent.click(screen.getByRole("button", { name: "Request access" }));

    await waitFor(() => {
      expect(postsTo("/api/agents/warden/access/requests")).toEqual([
        { integration: "payroll-bot", permissions: ["a2a_ask"], note: null },
      ]);
    });
    expect(postsTo("/api/agents/warden/invites")).toEqual([]);
    expect(container.querySelector(".a2a__picker")).not.toBeNull();
  });

  it("shows an incoming pending invite with its team, Accept and Decline, wired to the right endpoints", async () => {
    stubFetch({
      "/api/agents/warden/requestable": {
        requestable: [{ name: "iris", group: "payments-eng", team: null, capability: null, purpose_tags: [] }],
        already: [],
        not_ready: [],
        paired_group: "growth",
      },
      "/api/agents/warden/invites": {
        incoming: [
          {
            id: 7,
            from_agent: "iris",
            to_agent: { name: "warden", group_name: "growth" },
            status: "pending",
            requested_by: "iris-owner@example.com",
            decided_by: null,
            created_at: "2026-08-20T00:00:00Z",
            decided_at: null,
          },
        ],
        outgoing: [],
        team_pairs: [],
      },
    });
    renderWithClient(AgentToAgentPanel, { agentName: "warden" });

    const row = (await screen.findByText("iris")).closest("tr") as HTMLElement;
    expect(within(row).getByText("Wants to talk to yours")).not.toBeNull();
    expect(await within(row).findByText("payments-eng")).not.toBeNull();
    expect(within(row).getByText(/Aug \d+/)).not.toBeNull();
    await fireEvent.click(screen.getByRole("button", { name: "Accept iris" }));

    expect(
      vi
        .mocked(fetch)
        .mock.calls.some(
          ([url, init]) =>
            String(url).includes("/api/invites/7/accept") &&
            (init as RequestInit | undefined)?.method === "POST",
        ),
    ).toBe(true);
  });

  it("lists team_pairs first as non-revocable rows carrying their group, ahead of accepted pairs", async () => {
    stubFetch({
      "/api/agents/warden/requestable": EMPTY_REQUESTABLE,
      "/api/agents/warden/invites": {
        incoming: [],
        outgoing: [
          {
            id: 21, from_agent: "warden", to_agent: { name: "payroll-bot", group_name: "payments-eng" }, status: "accepted",
            requested_by: "owner@example.com", decided_by: "payroll-owner@example.com",
            created_at: "2026-08-20T00:00:00Z", decided_at: "2026-08-21T00:00:00Z",
          },
        ],
        team_pairs: [
          { name: "network", group: "growth" },
          { name: "relay", group: "growth" },
          { name: "gemini-one", group: "growth" },
        ],
      },
    });
    const { container } = renderWithClient(AgentToAgentPanel, { agentName: "warden" });

    await screen.findByText("network");
    expect(screen.getAllByText("Same team")).toHaveLength(3);
    expect(screen.getAllByText("growth")).toHaveLength(3);
    // Every paired row, automatic or accepted, says what the pair buys.
    expect(screen.getAllByText("Can join shared threads")).toHaveLength(4);
    // Non-revocable: the only Revoke button anywhere belongs to the trailing
    // accepted pair, never one of the three auto rows.
    expect(screen.getAllByRole("button", { name: /revoke/i })).toHaveLength(1);

    const rowNames = Array.from(container.querySelectorAll(".a2a__row .a2a__name")).map((el) =>
      el.textContent?.trim(),
    );
    expect(rowNames).toEqual(["network", "relay", "gemini-one", "payroll-bot"]);
  });

  it("shows an accepted pair with Revoke, wired to the delete endpoint, and the lag hint in the same row", async () => {
    // Cabal's D-017: a revoked pass rides a few more minutes on any token
    // already minted for it; the hint sits beside the button, not as a
    // standing caption that reads as "connection pending".
    stubFetch({
      "/api/agents/warden/requestable": EMPTY_REQUESTABLE,
      "/api/agents/warden/invites": {
        incoming: [],
        outgoing: [
          {
            id: 11, from_agent: "warden", to_agent: { name: "payroll-bot", group_name: "payments-eng" }, status: "accepted",
            requested_by: "owner@example.com", decided_by: "payroll-owner@example.com",
            created_at: "2026-08-20T00:00:00Z", decided_at: "2026-08-21T00:00:00Z",
          },
        ],
        team_pairs: [],
      },
    });
    renderWithClient(AgentToAgentPanel, { agentName: "warden" });

    await screen.findByText("payroll-bot");
    const revokeButton = screen.getByRole("button", { name: /revoke/i });
    const hint = screen.getByText("Revoking takes effect within a few minutes.");
    expect(revokeButton.closest(".a2a__row")?.contains(hint)).toBe(true);
    expect(within(revokeButton.closest("tr") as HTMLElement).getByText("payments-eng")).not.toBeNull();

    await fireEvent.click(revokeButton);
    expect(
      vi
        .mocked(fetch)
        .mock.calls.some(
          ([url, init]) =>
            String(url).includes("/api/invites/11") &&
            (init as RequestInit | undefined)?.method === "DELETE",
        ),
    ).toBe(true);
  });
});
