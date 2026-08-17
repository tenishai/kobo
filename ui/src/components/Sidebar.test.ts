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
});
