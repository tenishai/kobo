// @vitest-environment jsdom
import { screen } from "@testing-library/svelte";
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

describe("Dashboard — server-filtered list + groups_unavailable banner (access-control Task B2)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders exactly what GET /api/agents returns, with no banner when visibility is normal", async () => {
    stubFetch({ "/api/agents": { agents: [AGENT], groups_unavailable: false } });
    renderWithClient(Dashboard);

    await screen.findByText("warden");
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("shows the Global Constraints banner verbatim when groups_unavailable is true", async () => {
    stubFetch({ "/api/agents": { agents: [AGENT], groups_unavailable: true } });
    renderWithClient(Dashboard);

    await screen.findByText("warden");
    expect(
      screen.getByText(
        "Group lookups are unavailable right now. You are seeing only what you own.",
      ),
    ).not.toBeNull();
  });

  it("shows the empty state, not the banner, when the filtered list is empty and visibility is normal", async () => {
    stubFetch({ "/api/agents": { agents: [], groups_unavailable: false } });
    renderWithClient(Dashboard);

    await screen.findByText("No agents yet. Create your first.");
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("shows both the banner and the empty state when a stranger's visibility degrades to nothing", async () => {
    stubFetch({ "/api/agents": { agents: [], groups_unavailable: true } });
    renderWithClient(Dashboard);

    await screen.findByText("No agents yet. Create your first.");
    expect(
      screen.getByText(
        "Group lookups are unavailable right now. You are seeing only what you own.",
      ),
    ).not.toBeNull();
  });
});
