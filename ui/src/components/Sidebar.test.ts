// @vitest-environment jsdom
import { fireEvent, screen } from "@testing-library/svelte";
import { describe, expect, it, vi } from "vitest";
import Sidebar from "./Sidebar.svelte";
import { renderWithClient } from "../test-utils/render";

describe("Sidebar", () => {
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
});
