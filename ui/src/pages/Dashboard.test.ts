// @vitest-environment jsdom
import { fireEvent, screen } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Dashboard from "./Dashboard.svelte";
import type { AgentSummary } from "../lib/queries";
import { renderWithClient, stubFetch } from "../test-utils/render";

const AGENT: AgentSummary = {
  name: "warden",
  status: "ready",
  model: "gpt-5",
  model_label: "GPT-5",
  created_at: "2026-08-17T00:00:00Z",
  has_avatar: false,
  created_by: null,
  group_name: null,
  archetype: null,
  purpose_tags: [],
  kind: "composed",
};

function agent(fields: Partial<AgentSummary>): AgentSummary {
  return { ...AGENT, ...fields };
}

/** `n` agents named a1..aN, enough to cross the controls threshold. */
function many(n: number): AgentSummary[] {
  return Array.from({ length: n }, (_, i) => agent({ name: `a${i + 1}` }));
}

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

/** The three routes every render of this page reaches for. */
function stub(agents: AgentSummary[], groups_unavailable = false, whoami = WHOAMI_NON_ADMIN) {
  stubFetch({
    "/api/agents": { agents, groups_unavailable },
    "/api/whoami": whoami,
    "/api/": {},
  });
}

describe("Dashboard — server-filtered list + groups_unavailable banner (access-control Task B2)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders exactly what GET /api/agents returns, with no banner when visibility is normal", async () => {
    stub([AGENT]);
    renderWithClient(Dashboard);

    await screen.findByText("warden");
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("shows the Global Constraints banner verbatim when groups_unavailable is true", async () => {
    stub([AGENT], true);
    renderWithClient(Dashboard);

    await screen.findByText("warden");
    expect(
      screen.getByText(
        "Group lookups are unavailable right now. You are seeing only what you own.",
      ),
    ).not.toBeNull();
  });

  it("shows the empty state, not the banner, when the filtered list is empty and visibility is normal", async () => {
    stub([]);
    renderWithClient(Dashboard);

    await screen.findByText("No agents yet. Create your first.");
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("shows both the banner and the empty state when a stranger's visibility degrades to nothing", async () => {
    stub([], true);
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
    stub([AGENT]);
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
      "/api/": {},
    });
    renderWithClient(Dashboard);

    const toggle = await screen.findByText("Group requests: 2 pending");
    await screen.findByText("warden");

    await fireEvent.click(toggle);
    await screen.findByText("design-team");
  });
});

// Card wave 2026-09-23: a small room needs no controls at all. They appear
// with the seventh card, and they remember nothing between visits.
describe("Dashboard — finding an agent in a big room", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("offers no search and no group picker at six agents", async () => {
    stub(many(6));
    renderWithClient(Dashboard);

    await screen.findByText("a1");
    expect(screen.queryByLabelText("Search agents")).toBeNull();
    expect(screen.queryByLabelText("Group")).toBeNull();
  });

  it("offers both at seven", async () => {
    stub(many(7));
    renderWithClient(Dashboard);

    await screen.findByText("a1");
    expect(screen.getByLabelText("Search agents")).not.toBeNull();
    expect(screen.getByLabelText("Group")).not.toBeNull();
  });

  it("filters the grid by what was typed, and says so when nothing matches", async () => {
    stub([
      ...many(6),
      agent({ name: "warden", group_name: "sre", purpose_tags: ["fleet"] }),
    ]);
    renderWithClient(Dashboard);

    const search = await screen.findByLabelText("Search agents");
    await fireEvent.input(search, { target: { value: "warden" } });

    expect(screen.getByText("warden")).not.toBeNull();
    expect(screen.queryByText("a1")).toBeNull();

    await fireEvent.input(search, { target: { value: "nobody" } });
    expect(screen.getByText("No agents match that.")).not.toBeNull();
  });

  it("finds an agent by its group and by the line on its card", async () => {
    stub([
      ...many(6),
      agent({ name: "warden", group_name: "sre", purpose_tags: ["fleet"] }),
    ]);
    renderWithClient(Dashboard);

    const search = await screen.findByLabelText("Search agents");

    await fireEvent.input(search, { target: { value: "sre" } });
    expect(screen.getByText("warden")).not.toBeNull();
    expect(screen.queryByText("a1")).toBeNull();

    await fireEvent.input(search, { target: { value: "fleet" } });
    expect(screen.getByText("warden")).not.toBeNull();
  });

  it("narrows to one group from the picker, which offers only the groups present", async () => {
    stub([...many(6), agent({ name: "warden", group_name: "sre" })]);
    renderWithClient(Dashboard);

    const picker = (await screen.findByLabelText("Group")) as HTMLSelectElement;
    expect([...picker.options].map((o) => o.text)).toEqual(["All groups", "sre"]);

    await fireEvent.change(picker, { target: { value: "sre" } });
    expect(screen.getByText("warden")).not.toBeNull();
    expect(screen.queryByText("a1")).toBeNull();
  });
});
