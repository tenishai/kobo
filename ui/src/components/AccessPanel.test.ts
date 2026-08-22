// @vitest-environment jsdom
import { fireEvent, screen } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import AccessPanel from "./AccessPanel.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

const EMPTY_INVITES = { incoming: [], outgoing: [] };
const EMPTY_GRANTS = { grants: [] };

describe("AccessPanel", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  // --- People section --------------------------------------------------

  it("shows the group fact line verbatim when the agent has a group", async () => {
    stubFetch({
      "/api/agents/warden/invites": EMPTY_INVITES,
      "/api/agents/warden/grants": EMPTY_GRANTS,
    });
    renderWithClient(AccessPanel, { agentName: "warden", groupName: "growth" });

    expect(
      await screen.findByText("Everyone in growth can see this agent."),
    ).not.toBeNull();
  });

  it("explains there is no group when groupName is null", async () => {
    stubFetch({
      "/api/agents/warden/invites": EMPTY_INVITES,
      "/api/agents/warden/grants": EMPTY_GRANTS,
    });
    renderWithClient(AccessPanel, { agentName: "warden", groupName: null });

    expect(
      await screen.findByText(/This agent is not in a group\./),
    ).not.toBeNull();
  });

  it("renders the grants list and lets an owner remove a grant", async () => {
    stubFetch({
      "/api/agents/warden/invites": EMPTY_INVITES,
      "/api/agents/warden/grants": {
        grants: [{ email: "guest@example.com", added_by: "owner@example.com" }],
      },
    });
    renderWithClient(AccessPanel, { agentName: "warden", groupName: null });

    expect(await screen.findByText("guest@example.com")).not.toBeNull();

    await fireEvent.click(
      screen.getByRole("button", { name: "Remove guest@example.com" }),
    );

    expect(
      vi
        .mocked(fetch)
        .mock.calls.some(
          ([url, init]) =>
            String(url).includes("/api/agents/warden/grants/guest%40example.com") &&
            (init as RequestInit | undefined)?.method === "DELETE",
        ),
    ).toBe(true);
  });

  it("adding a grant posts the typed email to the grants endpoint", async () => {
    stubFetch({
      "/api/agents/warden/invites": EMPTY_INVITES,
      "/api/agents/warden/grants": EMPTY_GRANTS,
    });
    renderWithClient(AccessPanel, { agentName: "warden", groupName: null });

    await screen.findByLabelText("Add someone by email");
    await fireEvent.input(screen.getByLabelText("Add someone by email"), {
      target: { value: "new-person@example.com" },
    });
    await fireEvent.click(screen.getByRole("button", { name: "Add" }));

    const call = vi
      .mocked(fetch)
      .mock.calls.find(
        ([url, init]) =>
          String(url) === "/api/agents/warden/grants" &&
          (init as RequestInit | undefined)?.method === "POST",
      );
    expect(call).toBeDefined();
    const body = JSON.parse((call as [RequestInfo | URL, RequestInit])[1].body as string);
    expect(body).toEqual({ email: "new-person@example.com" });
  });

  // --- Agents section ----------------------------------------------------

  it("shows an incoming pending invite with Accept and Decline, wired to the right endpoints", async () => {
    stubFetch({
      "/api/agents/warden/grants": EMPTY_GRANTS,
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
      },
    });
    renderWithClient(AccessPanel, { agentName: "warden", groupName: "growth" });

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

  it("declining an incoming invite calls the decline endpoint", async () => {
    stubFetch({
      "/api/agents/warden/grants": EMPTY_GRANTS,
      "/api/agents/warden/invites": {
        incoming: [
          {
            id: 8,
            from_agent: "iris",
            to_agent: { name: "warden", group_name: "growth" },
            status: "pending",
            requested_by: null,
            decided_by: null,
            created_at: "2026-08-20T00:00:00Z",
            decided_at: null,
          },
        ],
        outgoing: [],
      },
    });
    renderWithClient(AccessPanel, { agentName: "warden", groupName: "growth" });

    await fireEvent.click(await screen.findByRole("button", { name: "Decline iris" }));

    expect(
      vi
        .mocked(fetch)
        .mock.calls.some(
          ([url, init]) =>
            String(url).includes("/api/invites/8/decline") &&
            (init as RequestInit | undefined)?.method === "POST",
        ),
    ).toBe(true);
  });

  it("shows an outgoing invite with a state chip and no action buttons", async () => {
    stubFetch({
      "/api/agents/warden/grants": EMPTY_GRANTS,
      "/api/agents/warden/invites": {
        incoming: [],
        outgoing: [
          {
            id: 9,
            from_agent: "warden",
            to_agent: { name: "payroll-bot", group_name: "payments-eng" },
            status: "pending",
            requested_by: "owner@example.com",
            decided_by: null,
            created_at: "2026-08-20T00:00:00Z",
            decided_at: null,
          },
        ],
      },
    });
    renderWithClient(AccessPanel, { agentName: "warden", groupName: "growth" });

    expect(await screen.findByText("payroll-bot")).not.toBeNull();
    expect(screen.getByText("pending")).not.toBeNull();
    expect(screen.queryByRole("button", { name: /accept/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /decline/i })).toBeNull();
  });

  it("shows an accepted pair with Revoke, wired to the delete endpoint", async () => {
    stubFetch({
      "/api/agents/warden/grants": EMPTY_GRANTS,
      "/api/agents/warden/invites": {
        incoming: [],
        outgoing: [
          {
            id: 11,
            from_agent: "warden",
            to_agent: { name: "payroll-bot", group_name: "payments-eng" },
            status: "accepted",
            requested_by: "owner@example.com",
            decided_by: "payroll-owner@example.com",
            created_at: "2026-08-20T00:00:00Z",
            decided_at: "2026-08-21T00:00:00Z",
          },
        ],
      },
    });
    renderWithClient(AccessPanel, { agentName: "warden", groupName: "growth" });

    // This same accepted invite also shows in "Outgoing requests" (that
    // section lists every outgoing invite regardless of status, chip only,
    // no actions) — so the query below is scoped to the row carrying the
    // Revoke button, not a bare substring match that would hit both rows.
    await screen.findByText("warden and payroll-bot");
    await fireEvent.click(screen.getByRole("button", { name: /revoke/i }));

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

  it("shows the revocation-lag note next to an active pair's Revoke button", async () => {
    // Cabal's D-017: a revoked pass rides ~5 more minutes on any token
    // already minted for it -- the room owes the caller that same honesty
    // right where they'd click Revoke.
    stubFetch({
      "/api/agents/warden/grants": EMPTY_GRANTS,
      "/api/agents/warden/invites": {
        incoming: [],
        outgoing: [
          {
            id: 12,
            from_agent: "warden",
            to_agent: { name: "payroll-bot", group_name: "payments-eng" },
            status: "accepted",
            requested_by: "owner@example.com",
            decided_by: "payroll-owner@example.com",
            created_at: "2026-08-20T00:00:00Z",
            decided_at: "2026-08-21T00:00:00Z",
          },
        ],
      },
    });
    renderWithClient(AccessPanel, { agentName: "warden", groupName: "growth" });

    expect(
      await screen.findByText("Takes effect within a few minutes."),
    ).not.toBeNull();
  });

  it("does not show the revocation-lag note when there are no active pairs", async () => {
    stubFetch({
      "/api/agents/warden/grants": EMPTY_GRANTS,
      "/api/agents/warden/invites": EMPTY_INVITES,
    });
    renderWithClient(AccessPanel, { agentName: "warden", groupName: "growth" });

    await screen.findByText(/no cross-group pairs/i);
    expect(screen.queryByText("Takes effect within a few minutes.")).toBeNull();
  });

  it("shows empty-state copy when there are no invites at all", async () => {
    stubFetch({
      "/api/agents/warden/grants": EMPTY_GRANTS,
      "/api/agents/warden/invites": EMPTY_INVITES,
    });
    renderWithClient(AccessPanel, { agentName: "warden", groupName: "growth" });

    expect(await screen.findByText(/no pending requests/i)).not.toBeNull();
    expect(screen.getByText(/no outgoing requests/i)).not.toBeNull();
    expect(screen.getByText(/no cross-group pairs/i)).not.toBeNull();
  });
});
