// @vitest-environment jsdom
// Agent to agent (issue #368): the Other agents block of the Access tab as a
// Harness section. Same routes (invites, pairs, requestable, access).
import { fireEvent, screen } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import AgentToAgentPanel from "./AgentToAgentPanel.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

const EMPTY_INVITES = { incoming: [], outgoing: [], team_pairs: [] };
const EMPTY_REQUESTABLE = { requestable: [], already: [] };

/** Open the "Request another team's agent" disclosure. */
async function openPicker(): Promise<void> {
  await fireEvent.click(
    await screen.findByRole("button", { name: "Request another team's agent" }),
  );
}

describe("AgentToAgentPanel", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("opens with the one sentence and the picker shut", async () => {
    stubFetch({
      "/api/agents/warden/invites": EMPTY_INVITES,
      "/api/agents/warden/requestable": EMPTY_REQUESTABLE,
    });
    renderWithClient(AgentToAgentPanel, { agentName: "warden" });

    expect(await screen.findByRole("heading", { name: "Agent to agent" })).not.toBeNull();
    expect(
      screen.getByText(/Which agents from other teams this one may ask for help, and who has asked to reach it\. Agents in the same group need nothing here\./),
    ).not.toBeNull();
    const picker = screen.getByRole("button", { name: "Request another team's agent" });
    expect(picker.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText(/Nobody to ask/)).toBeNull();

    await openPicker();
    expect(picker.getAttribute("aria-expanded")).toBe("true");
    expect(await screen.findByText(/Nobody to ask/)).not.toBeNull();
  });

  it("shows an incoming pending invite with Accept and Decline, wired to the right endpoints", async () => {
    stubFetch({
      "/api/agents/warden/requestable": EMPTY_REQUESTABLE,
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

    expect(await screen.findByText("iris")).not.toBeNull();
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

  it("the outgoing picker lists requestable candidates with group + purpose (capability, then tags, then a fallback), and fires a request", async () => {
    stubFetch({
      "/api/agents/warden/invites": EMPTY_INVITES,
      "/api/agents/warden/requestable": {
        requestable: [
          { name: "payroll-bot", group: "payments-eng", team: "payments", capability: "Handles refund inquiries.", purpose_tags: ["billing", "refunds"] },
          { name: "tagged-only-bot", group: "growth", team: null, capability: null, purpose_tags: ["scheduling", "reminders"] },
          { name: "no-description-bot", group: "growth", team: null, capability: null, purpose_tags: [] },
        ],
        already: [],
      },
    });
    renderWithClient(AgentToAgentPanel, { agentName: "warden" });

    await openPicker();
    expect(await screen.findByText("payroll-bot")).not.toBeNull();
    expect(screen.getByText("payments-eng")).not.toBeNull();
    expect(screen.getByText("Handles refund inquiries.")).not.toBeNull();
    expect(screen.getByText("scheduling, reminders")).not.toBeNull();
    expect(screen.getByText("No description yet.")).not.toBeNull();

    await fireEvent.click(screen.getByRole("button", { name: "Request access to payroll-bot" }));

    const call = vi
      .mocked(fetch)
      .mock.calls.find(
        ([url, init]) =>
          String(url) === "/api/agents/warden/invites" &&
          (init as RequestInit | undefined)?.method === "POST",
      );
    expect(call).toBeDefined();
    const body = JSON.parse((call as [RequestInfo | URL, RequestInit])[1].body as string);
    expect(body).toEqual({ to_agent: "payroll-bot" });
  });

  it("with the permission service on, Request access posts an ask permission request, and the waiting and approved rows render", async () => {
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
        already: [],
      },
    });
    renderWithClient(AgentToAgentPanel, { agentName: "warden" });

    expect(await screen.findByText("Waiting for review")).not.toBeNull();
    expect(screen.getByText("ledger")).not.toBeNull();
    expect(await screen.findByText("auditor")).not.toBeNull();
    expect(screen.getByText("Approved in Review")).not.toBeNull();

    await openPicker();
    await fireEvent.click(await screen.findByRole("button", { name: "Request access to payroll-bot" }));

    const call = vi
      .mocked(fetch)
      .mock.calls.find(
        ([url, init]) =>
          String(url) === "/api/agents/warden/access/requests" &&
          (init as RequestInit | undefined)?.method === "POST",
      );
    expect(call).toBeDefined();
    const body = JSON.parse((call as [RequestInfo | URL, RequestInit])[1].body as string);
    expect(body).toEqual({ integration: "payroll-bot", permissions: ["a2a_ask"], note: null });
    expect(
      vi.mocked(fetch).mock.calls.some(
        ([url, init]) =>
          String(url) === "/api/agents/warden/invites" &&
          (init as RequestInit | undefined)?.method === "POST",
      ),
    ).toBe(false);
  });

  it("rows already asked (pending or accepted) render a status chip instead of a Request access button", async () => {
    stubFetch({
      "/api/agents/warden/invites": EMPTY_INVITES,
      "/api/agents/warden/requestable": {
        requestable: [],
        already: [
          { name: "iris", status: "pending" },
          { name: "clio", status: "accepted" },
        ],
      },
    });
    renderWithClient(AgentToAgentPanel, { agentName: "warden" });

    await openPicker();
    expect(await screen.findByText("iris")).not.toBeNull();
    expect(screen.getByText("pending")).not.toBeNull();
    expect(screen.getByText("clio")).not.toBeNull();
    expect(screen.getByText("accepted")).not.toBeNull();
    expect(screen.queryByRole("button", { name: /request access to iris/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /request access to clio/i })).toBeNull();
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
    expect(screen.getAllByText("Same team (growth)")).toHaveLength(3);
    // Every paired row, automatic or accepted, says what the pair buys.
    expect(screen.getAllByText("Can join shared threads")).toHaveLength(4);
    // Non-revocable: the only Revoke button anywhere belongs to the trailing
    // accepted pair, never one of the three auto rows.
    expect(screen.getAllByRole("button", { name: /revoke/i })).toHaveLength(1);

    const rowNames = Array.from(container.querySelectorAll(".a2a__row-name")).map((el) =>
      el.textContent?.trim(),
    );
    expect(rowNames).toEqual(["network", "relay", "gemini-one", "warden and payroll-bot"]);
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

    await screen.findByText("warden and payroll-bot");
    const revokeButton = screen.getByRole("button", { name: /revoke/i });
    const hint = screen.getByText("Revoking takes effect within a few minutes.");
    expect(revokeButton.closest(".a2a__row")?.contains(hint)).toBe(true);

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
