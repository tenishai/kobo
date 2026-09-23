// @vitest-environment jsdom
// The shell's one job in the new-agent entry wave: the rail has a single
// button, that button opens the chooser, and the chooser hands off to the
// station the person picked. Asserted end to end against the real dialogs
// (by their own titles) rather than against a spy, because the bug being
// fixed was exactly "which button opens which dialog".
import { render, fireEvent, screen, waitFor } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App.svelte";
import { stubFetch } from "./test-utils/render";

function stubRoom(): void {
  stubFetch({
    "/api/whoami": { email: "someone@example.com", operator: true, groups: [] },
    "/api/agents": { agents: [], groups_unavailable: false },
    "/api/catalog/models": { models: [{ id: "m", label: "M" }], default: "m" },
    "/api/catalog/groups": { groups: [] },
    "/api/catalog/teams": { teams: [] },
    "/api/catalog/observability": { dashboard_base_url: null, project: null },
    "/api/org/status": { org: null, wired: false, checks: [] },
    "/api/personas/search": { results: [] },
    "/api/skills/search": { results: [] },
    "/api/connect/repos": { results: [] },
    "/api/": {},
  });
}

async function openChooser(): Promise<void> {
  await fireEvent.click(screen.getByRole("button", { name: "New agent" }));
  expect(screen.getByText("How do you want to start?")).not.toBeNull();
}

describe("App — one way in to a new agent", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    stubRoom();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("offers one new-agent button in the rail, not three", () => {
    render(App);
    expect(screen.getByRole("button", { name: "New agent" })).not.toBeNull();
    expect(screen.queryByRole("button", { name: "Create agent" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Quick-start team agent" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Connect your own agent" })).toBeNull();
  });

  it("shows no dialog until the button is pressed", () => {
    render(App);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("opens the build station from the first row", async () => {
    render(App);
    await openChooser();
    await fireEvent.click(screen.getByRole("button", { name: /Build an agent/ }));

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Create agent" })).not.toBeNull();
    });
    expect(screen.queryByText("How do you want to start?")).toBeNull();
  });

  it("opens the team-guide station from the second row", async () => {
    render(App);
    await openChooser();
    await fireEvent.click(screen.getByRole("button", { name: /Team guide in one step/ }));

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Quick-start team agent" })).not.toBeNull();
    });
    expect(screen.queryByText("How do you want to start?")).toBeNull();
  });

  it("opens the connect station from the third row", async () => {
    render(App);
    await openChooser();
    await fireEvent.click(
      screen.getByRole("button", { name: /Connect a repository you already have/ }),
    );

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Connect your own agent" })).not.toBeNull();
    });
    expect(screen.queryByText("How do you want to start?")).toBeNull();
  });

  it("returns to the dashboard when a station is closed, never back to the chooser", async () => {
    render(App);
    await openChooser();
    await fireEvent.click(screen.getByRole("button", { name: /Build an agent/ }));
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Create agent" })).not.toBeNull();
    });

    await fireEvent.click(screen.getByRole("button", { name: "Close" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(screen.queryByText("How do you want to start?")).toBeNull();
  });

  it("closes the chooser on Cancel without opening any station", async () => {
    render(App);
    await openChooser();
    await fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
  });
});
