// @vitest-environment jsdom
import { fireEvent, screen } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import AccessPanel from "./AccessPanel.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

const EMPTY_INVITES = { incoming: [], outgoing: [], team_pairs: [] };
const EMPTY_GRANTS = { grants: [] };
const EMPTY_REQUESTABLE = { requestable: [], already: [] };

/** Open the "Add person" disclosure. It is shut on arrival, so every test
 * that touches the add-a-person form opens it first. */
async function openGrant(): Promise<void> {
  await fireEvent.click(await screen.findByRole("button", { name: "Add person" }));
}

/** Open the "Request another team's agent" disclosure. */
async function openPicker(): Promise<void> {
  await fireEvent.click(
    await screen.findByRole("button", { name: "Request another team's agent" }),
  );
}

describe("AccessPanel", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  // --- The panel shape -------------------------------------------------

  it("keeps both toolbar forms shut until they are asked for", async () => {
    stubFetch({
      "/api/agents/warden/invites": EMPTY_INVITES,
      "/api/agents/warden/grants": EMPTY_GRANTS,
      "/api/agents/warden/requestable": EMPTY_REQUESTABLE,
    });
    renderWithClient(AccessPanel, { agentName: "warden", groupName: "growth" });

    const grant = await screen.findByRole("button", { name: "Add person" });
    const picker = screen.getByRole("button", {
      name: "Request another team's agent",
    });
    expect(grant.getAttribute("aria-expanded")).toBe("false");
    expect(picker.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByLabelText("Add someone by email")).toBeNull();
    expect(screen.queryByText(/Nobody to ask/)).toBeNull();
  });

  it("opens each toolbar form on click", async () => {
    stubFetch({
      "/api/agents/warden/invites": EMPTY_INVITES,
      "/api/agents/warden/grants": EMPTY_GRANTS,
      "/api/agents/warden/requestable": EMPTY_REQUESTABLE,
    });
    renderWithClient(AccessPanel, { agentName: "warden", groupName: "growth" });

    await openGrant();
    expect(
      screen.getByRole("button", { name: "Add person" }).getAttribute("aria-expanded"),
    ).toBe("true");
    expect(screen.getByLabelText("Add someone by email")).not.toBeNull();

    await openPicker();
    expect(
      screen
        .getByRole("button", { name: "Request another team's agent" })
        .getAttribute("aria-expanded"),
    ).toBe("true");
    expect(
      await screen.findByText(/Nobody to ask/),
    ).not.toBeNull();
  });

  it("renders the People and Other agents sections, and the role select posts the chosen role", async () => {
    // Issue #309: two sections with their own heading, and a grant carries a
    // role the server's ownership gate reads.
    stubFetch({
      "/api/agents/warden/invites": EMPTY_INVITES,
      "/api/agents/warden/grants": EMPTY_GRANTS,
      "/api/agents/warden/requestable": EMPTY_REQUESTABLE,
    });
    renderWithClient(AccessPanel, { agentName: "warden", groupName: "growth" });

    expect(await screen.findByRole("heading", { name: "People" })).not.toBeNull();
    expect(screen.getByRole("heading", { name: "Other agents" })).not.toBeNull();
    expect(screen.getByText("Everyone in growth can see this agent.")).not.toBeNull();

    await openGrant();
    await fireEvent.input(screen.getByLabelText("Add someone by email"), {
      target: { value: "lead@example.com" },
    });
    await fireEvent.change(screen.getByLabelText("Role"), { target: { value: "manage" } });
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
    expect(body).toEqual({ email: "lead@example.com", role: "manage" });
  });

  it("is two halves: who can reach this agent, then what it can reach (issue #365)", async () => {
    // The permissions moved here from the Harness: the Held row names the
    // identity the agent runs as, and the picker lists the room's vendors.
    stubFetch({
      "/api/agents/warden/invites": EMPTY_INVITES,
      "/api/agents/warden/grants": EMPTY_GRANTS,
      "/api/agents/warden/requestable": EMPTY_REQUESTABLE,
      "/api/agents/warden/access": {
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
      },
    });
    renderWithClient(AccessPanel, {
      agentName: "warden",
      groupName: "growth",
      identity: { service_account: "agent-warden", gsa_email: "agent-warden@example-project.iam.gserviceaccount.com", project: "example-project" },
    });

    await screen.findByRole("heading", { name: "Held" });
    const headings = screen.getAllByRole("heading").map((h) => h.textContent?.trim());
    expect(headings).toEqual([
      "Access",
      "Who can reach this agent",
      "People",
      "Other agents",
      "What this agent can reach",
      "Held",
      "Ask for more",
    ]);
    expect(screen.getByText("agent-warden@example-project.iam.gserviceaccount.com")).not.toBeNull();
    expect(await screen.findByRole("button", { name: /Datadog/ })).not.toBeNull();
  });

  // --- People section --------------------------------------------------

  it("renders the grants list and lets an owner remove a grant", async () => {
    stubFetch({
      "/api/agents/warden/invites": EMPTY_INVITES,
      "/api/agents/warden/grants": {
        grants: [{ email: "guest@example.com", role: "view", added_by: "owner@example.com" }],
      },
      "/api/agents/warden/requestable": EMPTY_REQUESTABLE,
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
      "/api/agents/warden/requestable": EMPTY_REQUESTABLE,
    });
    renderWithClient(AccessPanel, { agentName: "warden", groupName: null });

    await openGrant();
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
    expect(body).toEqual({ email: "new-person@example.com", role: "view" });
  });

  // --- People: directory-backed autocomplete (access-tab refinement wave) --

  it("debounces the directory search as the person types, shows a dropdown of Name <email> rows, and a pick fills the field", async () => {
    stubFetch({
      "/api/agents/warden/invites": EMPTY_INVITES,
      "/api/agents/warden/grants": EMPTY_GRANTS,
      "/api/agents/warden/requestable": EMPTY_REQUESTABLE,
      "/api/people/search": {
        people: [{ name: "Alice Smith", email: "alice@example.com" }],
      },
    });
    renderWithClient(AccessPanel, { agentName: "warden", groupName: null });

    await openGrant();
    const input = screen.getByLabelText("Add someone by email") as HTMLInputElement;
    await fireEvent.input(input, { target: { value: "al" } });

    const suggestion = await screen.findByText(
      "Alice Smith <alice@example.com>",
      {},
      { timeout: 2000 },
    );
    expect(suggestion).not.toBeNull();

    // The search only fires once the debounce settles, and only with the
    // >= 2 character query typed.
    expect(
      vi
        .mocked(fetch)
        .mock.calls.some(([url]) => String(url).includes("/api/people/search?q=al")),
    ).toBe(true);

    await fireEvent.click(suggestion);

    expect(input.value).toBe("alice@example.com");
    expect(screen.queryByText("Alice Smith <alice@example.com>")).toBeNull();
  });

  it("never searches below two characters", async () => {
    stubFetch({
      "/api/agents/warden/invites": EMPTY_INVITES,
      "/api/agents/warden/grants": EMPTY_GRANTS,
      "/api/agents/warden/requestable": EMPTY_REQUESTABLE,
    });
    renderWithClient(AccessPanel, { agentName: "warden", groupName: null });

    await openGrant();
    await fireEvent.input(screen.getByLabelText("Add someone by email"), {
      target: { value: "a" },
    });

    // Give the 250ms debounce a chance to have fired if it were going to.
    await new Promise((resolve) => setTimeout(resolve, 300));

    expect(
      vi.mocked(fetch).mock.calls.some(([url]) => String(url).includes("/api/people/search")),
    ).toBe(false);
  });

  it("keeps the plain typed-email entry working with a quiet note when the directory is unavailable", async () => {
    stubFetch({
      "/api/agents/warden/invites": EMPTY_INVITES,
      "/api/agents/warden/grants": EMPTY_GRANTS,
      "/api/agents/warden/requestable": EMPTY_REQUESTABLE,
      "/api/people/search": { people: [], unavailable: true },
    });
    renderWithClient(AccessPanel, { agentName: "warden", groupName: null });

    await openGrant();
    await fireEvent.input(screen.getByLabelText("Add someone by email"), {
      target: { value: "someone@example.com" },
    });

    expect(
      await screen.findByText(/directory search is unavailable/i, {}, { timeout: 2000 }),
    ).not.toBeNull();

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
    expect(body).toEqual({ email: "someone@example.com", role: "view" });
  });

  // --- Agents section --------------------------------------------------

  it("shows an incoming pending invite with Accept and Decline, wired to the right endpoints", async () => {
    stubFetch({
      "/api/agents/warden/grants": EMPTY_GRANTS,
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

  // --- Agents section: the outgoing picker (access-tab refinement wave) -

  it("the outgoing picker lists requestable candidates with group + purpose (capability, then tags, then a fallback), and fires a request", async () => {
    stubFetch({
      "/api/agents/warden/grants": EMPTY_GRANTS,
      "/api/agents/warden/invites": EMPTY_INVITES,
      "/api/agents/warden/requestable": {
        requestable: [
          {
            name: "payroll-bot",
            group: "payments-eng",
            team: "payments",
            capability: "Handles refund inquiries.",
            purpose_tags: ["billing", "refunds"],
          },
          {
            name: "tagged-only-bot",
            group: "growth",
            team: null,
            capability: null,
            purpose_tags: ["scheduling", "reminders"],
          },
          {
            name: "no-description-bot",
            group: "growth",
            team: null,
            capability: null,
            purpose_tags: [],
          },
        ],
        already: [],
      },
    });
    renderWithClient(AccessPanel, { agentName: "warden", groupName: "growth" });

    await openPicker();
    expect(await screen.findByText("payroll-bot")).not.toBeNull();
    expect(screen.getByText("payments-eng")).not.toBeNull();
    expect(screen.getByText("Handles refund inquiries.")).not.toBeNull();
    expect(screen.getByText("scheduling, reminders")).not.toBeNull();
    expect(screen.getByText("No description yet.")).not.toBeNull();

    await fireEvent.click(
      screen.getByRole("button", { name: "Request access to payroll-bot" }),
    );

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
      "/api/agents/warden/grants": EMPTY_GRANTS,
      "/api/agents/warden/invites": EMPTY_INVITES,
      "/api/agents/warden/pairs": { pairs: [{ name: "auditor", group: "security", via: "grant" }] },
      "/api/agents/warden/access": {
        agent: "warden",
        team: "growth",
        grants: [],
        pending: [
          {
            id: 4,
            agent: "warden",
            integration: "ledger",
            permissions: ["a2a_ask"],
            tiers: { read: 1 },
            kind: "standing",
            status: "pending",
            requested_by: "owner@example.com",
            note: null,
            created_at: "2026-10-29T12:00:00Z",
            decided_at: null,
            decided_by: null,
            reason: null,
            outcome: {},
          },
        ],
        vendors: [],
        can_request: true,
        can_decide: false,
      },
      "/api/agents/warden/requestable": {
        requestable: [
          { name: "payroll-bot", group: "payments-eng", team: null, capability: null, purpose_tags: [] },
        ],
        already: [],
      },
    });
    renderWithClient(AccessPanel, { agentName: "warden", groupName: "growth" });

    expect(await screen.findByText("Waiting for review")).not.toBeNull();
    expect(screen.getByText("ledger")).not.toBeNull();
    expect(await screen.findByText("auditor")).not.toBeNull();
    expect(screen.getByText("Approved in Review")).not.toBeNull();

    await openPicker();
    await fireEvent.click(
      await screen.findByRole("button", { name: "Request access to payroll-bot" }),
    );

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
      "/api/agents/warden/grants": EMPTY_GRANTS,
      "/api/agents/warden/invites": EMPTY_INVITES,
      "/api/agents/warden/requestable": {
        requestable: [],
        already: [
          { name: "iris", status: "pending" },
          { name: "clio", status: "accepted" },
        ],
      },
    });
    renderWithClient(AccessPanel, { agentName: "warden", groupName: "growth" });

    await openPicker();
    expect(await screen.findByText("iris")).not.toBeNull();
    expect(screen.getByText("pending")).not.toBeNull();
    expect(screen.getByText("clio")).not.toBeNull();
    expect(screen.getByText("accepted")).not.toBeNull();
    expect(screen.queryByRole("button", { name: /request access to iris/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /request access to clio/i })).toBeNull();
  });

  // --- Agents table: pairs (team_pairs + accepted invites) --------------

  it("lists team_pairs first as non-revocable 'auto' rows carrying their group, ahead of accepted pairs", async () => {
    stubFetch({
      "/api/agents/warden/grants": EMPTY_GRANTS,
      "/api/agents/warden/requestable": EMPTY_REQUESTABLE,
      "/api/agents/warden/invites": {
        incoming: [],
        outgoing: [
          {
            id: 21,
            from_agent: "warden",
            to_agent: { name: "payroll-bot", group_name: "payments-eng" },
            status: "accepted",
            requested_by: "owner@example.com",
            decided_by: "payroll-owner@example.com",
            created_at: "2026-08-20T00:00:00Z",
            decided_at: "2026-08-21T00:00:00Z",
          },
        ],
        team_pairs: [
          { name: "network", group: "growth" },
          { name: "relay", group: "growth" },
          { name: "gemini-one", group: "growth" },
        ],
      },
    });
    const { container } = renderWithClient(AccessPanel, {
      agentName: "warden",
      groupName: "growth",
    });

    await screen.findByText("network");
    expect(screen.getAllByText("Same team (growth)")).toHaveLength(3);
    // Every paired row, automatic or accepted, says what the pair buys.
    expect(screen.getAllByText("Can join shared threads")).toHaveLength(4);
    // Non-revocable: the only Revoke button anywhere belongs to the trailing
    // accepted pair, never one of the three auto rows.
    expect(screen.getAllByRole("button", { name: /revoke/i })).toHaveLength(1);

    const rowNames = Array.from(
      container.querySelectorAll(".access-panel__row-name"),
    ).map((el) => el.textContent?.trim());
    expect(rowNames).toEqual([
      "network",
      "relay",
      "gemini-one",
      "warden and payroll-bot",
    ]);
  });

  it("shows an accepted pair with Revoke, wired to the delete endpoint", async () => {
    stubFetch({
      "/api/agents/warden/grants": EMPTY_GRANTS,
      "/api/agents/warden/requestable": EMPTY_REQUESTABLE,
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
        team_pairs: [],
      },
    });
    renderWithClient(AccessPanel, { agentName: "warden", groupName: "growth" });

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

  it("shows the revocation-lag hint inside the same row as its Revoke button, not as a standing caption", async () => {
    // Cabal's D-017: a revoked pass rides ~5 more minutes on any token
    // already minted for it -- the room owes the caller that same honesty
    // right where they'd click Revoke. It must read as being about
    // REVOKING specifically -- an earlier wording sat as a standing
    // caption above the whole pairs list and got misread as
    // "connection pending" for a brand-new (already-live) pair.
    stubFetch({
      "/api/agents/warden/grants": EMPTY_GRANTS,
      "/api/agents/warden/requestable": EMPTY_REQUESTABLE,
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
        team_pairs: [],
      },
    });
    renderWithClient(AccessPanel, { agentName: "warden", groupName: "growth" });

    const revokeButton = await screen.findByRole("button", { name: /revoke/i });
    const hint = await screen.findByText("Revoking takes effect within a few minutes.");
    expect(hint).not.toBeNull();
    // Scoped to the Revoke row itself, not floating above the whole table.
    expect(revokeButton.closest(".access-panel__row")?.contains(hint)).toBe(true);
  });

});
