// @vitest-environment jsdom
import { fireEvent, screen } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Dashboard from "./Dashboard.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

const AGENT = {
  name: "warden",
  status: "ready",
  model: "gpt-5",
  model_label: "GPT-5",
  created_at: "2026-08-17T00:00:00Z",
  has_avatar: false,
  created_by: null,
  group_name: null,
};

// `GroupRequestsCard` (mounted unconditionally by Dashboard, shared-group +
// group-requests wave) queries whoami on its own — every case here needs
// this stubbed even when the card itself isn't under test, or `stubFetch`'s
// "unstubbed fetch" guard throws.
const WHOAMI_NON_ADMIN = {
  email: "someone@example.com",
  operator: false,
  groups: [],
  groups_unavailable: false,
};

describe("Dashboard — server-filtered list + groups_unavailable banner (access-control Task B2)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders exactly what GET /api/agents returns, with no banner when visibility is normal", async () => {
    stubFetch({
      "/api/agents": { agents: [AGENT], groups_unavailable: false },
      "/api/whoami": WHOAMI_NON_ADMIN,
    });
    renderWithClient(Dashboard);

    await screen.findByText("warden");
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("shows the Global Constraints banner verbatim when groups_unavailable is true", async () => {
    stubFetch({
      "/api/agents": { agents: [AGENT], groups_unavailable: true },
      "/api/whoami": WHOAMI_NON_ADMIN,
    });
    renderWithClient(Dashboard);

    await screen.findByText("warden");
    expect(
      screen.getByText(
        "Group lookups are unavailable right now. You are seeing only what you own.",
      ),
    ).not.toBeNull();
  });

  it("shows the empty state, not the banner, when the filtered list is empty and visibility is normal", async () => {
    stubFetch({
      "/api/agents": { agents: [], groups_unavailable: false },
      "/api/whoami": WHOAMI_NON_ADMIN,
    });
    renderWithClient(Dashboard);

    await screen.findByText("No agents yet. Create your first.");
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("shows both the banner and the empty state when a stranger's visibility degrades to nothing", async () => {
    stubFetch({
      "/api/agents": { agents: [], groups_unavailable: true },
      "/api/whoami": WHOAMI_NON_ADMIN,
    });
    renderWithClient(Dashboard);

    await screen.findByText("No agents yet. Create your first.");
    expect(
      screen.getByText(
        "Group lookups are unavailable right now. You are seeing only what you own.",
      ),
    ).not.toBeNull();
  });
});

describe("Dashboard — group requests admin card (shared-group + group-requests wave)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("does not render the group requests card for a non-admin caller", async () => {
    stubFetch({
      "/api/agents": { agents: [AGENT], groups_unavailable: false },
      "/api/whoami": WHOAMI_NON_ADMIN,
    });
    renderWithClient(Dashboard);

    await screen.findByText("warden");
    expect(screen.queryByText(/Group requests:/)).toBeNull();
  });

  it("renders the group requests card above the grid when whoami reports a positive pending count", async () => {
    stubFetch({
      "/api/agents": { agents: [AGENT], groups_unavailable: false },
      "/api/whoami": {
        ...WHOAMI_NON_ADMIN,
        operator: true,
        pending_group_requests: 2,
      },
      "/api/group-requests": {
        requests: [
          {
            id: 1,
            group_name: "design-team",
            requested_by: "asha@example.com",
            note: null,
            status: "pending",
            decided_by: null,
            created_at: "2026-08-21T12:00:00Z",
            decided_at: null,
          },
        ],
      },
    });
    renderWithClient(Dashboard);

    const toggle = await screen.findByText("Group requests: 2 pending");
    await screen.findByText("warden");

    await fireEvent.click(toggle);
    await screen.findByText("design-team");
  });
});
