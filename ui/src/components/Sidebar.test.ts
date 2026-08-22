// @vitest-environment jsdom
import { fireEvent, screen, waitFor } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Sidebar from "./Sidebar.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

describe("Sidebar", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    // Sidebar fires whoamiQuery on mount (access-control Task A1); every
    // test needs at least this catch-all so that fetch never goes unstubbed,
    // same idiom ChatPanel.test.ts's `stubFetch({ "/api/": {} })` uses.
    stubFetch({ "/api/": {} });
  });

  it("renders nav links and marks the active page", () => {
    renderWithClient(Sidebar, { active: "getting-started", oncreate: () => {}, onprefetch: () => {} });
    const agents = screen.getByRole("link", { name: "Agents" });
    const getting = screen.getByRole("link", { name: "Getting started" });
    expect(agents.getAttribute("href")).toBe("#/");
    expect(getting.getAttribute("href")).toBe("#/getting-started");
    expect(getting.getAttribute("aria-current")).toBe("page");
    expect(agents.getAttribute("aria-current")).toBeNull();
  });

  it("fires oncreate and onprefetch from the create button", async () => {
    const oncreate = vi.fn();
    const onprefetch = vi.fn();
    renderWithClient(Sidebar, { active: "dashboard", oncreate, onprefetch });
    const button = screen.getByRole("button", { name: "Create agent" });
    await fireEvent.mouseEnter(button);
    expect(onprefetch).toHaveBeenCalled();
    await fireEvent.click(button);
    expect(oncreate).toHaveBeenCalled();
  });

  it("gives both nav links an unconditional aria-label so the icon rail keeps its accessible name", () => {
    renderWithClient(Sidebar, { active: "dashboard", oncreate: () => {}, onprefetch: () => {} });
    const agents = screen.getByRole("link", { name: "Agents" });
    const getting = screen.getByRole("link", { name: "Getting started" });
    expect(agents.getAttribute("aria-label")).toBe("Agents");
    expect(getting.getAttribute("aria-label")).toBe("Getting started");
    expect(agents.getAttribute("title")).toBe("Agents");
    expect(getting.getAttribute("title")).toBe("Getting started");
  });

  it("keeps the create button's accessible name 'Create agent' via an unconditional aria-label", () => {
    renderWithClient(Sidebar, { active: "dashboard", oncreate: () => {}, onprefetch: () => {} });
    const button = screen.getByRole("button", { name: "Create agent" });
    expect(button.getAttribute("aria-label")).toBe("Create agent");
    expect(button.getAttribute("title")).toBe("Create agent");
  });

  it("shows the signed-in email in the footer when whoami returns one", async () => {
    stubFetch({
      "/api/whoami": { email: "kgreiner@example.com", operator: true, groups: [] },
      "/api/": {},
    });
    renderWithClient(Sidebar, { active: "dashboard", oncreate: () => {}, onprefetch: () => {} });

    expect(await screen.findByText("kgreiner@example.com")).not.toBeNull();
  });

  it("renders nothing in the footer when whoami's email is null", async () => {
    stubFetch({
      "/api/whoami": { email: null, operator: false, groups: [] },
      "/api/": {},
    });
    const { container } = renderWithClient(Sidebar, {
      active: "dashboard",
      oncreate: () => {},
      onprefetch: () => {},
    });

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith("/api/whoami");
    });
    expect(container.querySelector(".sidebar__whoami")).toBeNull();
  });
});
