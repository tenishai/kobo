// @vitest-environment jsdom
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fireEvent, screen, waitFor } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Sidebar from "./Sidebar.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

const SIDEBAR_SOURCE = readFileSync(join(__dirname, "Sidebar.svelte"), "utf8");

const BASE_AGENT = {
  name: "warden",
  status: "ready",
  model: "m",
  model_label: "M",
  created_at: "2026-08-22T00:00:00Z",
  has_avatar: false,
  created_by: null,
  group_name: null,
};

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

describe("Sidebar — the Kobo identity (wordmark + hammer mark ruling)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    stubFetch({ "/api/": {} });
  });

  it("shows the wordmark as 'Kobo' -- the room's display name, capital K", () => {
    renderWithClient(Sidebar, { active: "dashboard", oncreate: () => {}, onprefetch: () => {} });
    expect(screen.getByText("Kobo")).not.toBeNull();
    expect(screen.queryByText("kobo")).toBeNull();
  });

  it("renders an inline hammer-mark svg to the left of the wordmark", () => {
    const { container } = renderWithClient(Sidebar, {
      active: "dashboard",
      oncreate: () => {},
      onprefetch: () => {},
    });
    const logo = container.querySelector(".sidebar__logo");
    const hammer = container.querySelector(".sidebar__hammer");
    expect(hammer).not.toBeNull();
    expect(hammer!.tagName.toLowerCase()).toBe("svg");
    // Left of the wordmark: the hammer is an earlier sibling within the
    // same logo link, not appended after the wordmark text.
    const wordmark = container.querySelector(".sidebar__wordmark");
    expect(wordmark).not.toBeNull();
    expect(logo!.contains(hammer)).toBe(true);
    expect(
      hammer!.compareDocumentPosition(wordmark!) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("pulses the hammer mark green when an agent in the list is pending or provisioning, with a title explaining why", async () => {
    stubFetch({
      "/api/agents": {
        agents: [{ ...BASE_AGENT, name: "new-agent", status: "provisioning" }],
        groups_unavailable: false,
      },
      "/api/": {},
    });
    const { container } = renderWithClient(Sidebar, {
      active: "dashboard",
      oncreate: () => {},
      onprefetch: () => {},
    });

    await waitFor(() => {
      const hammer = container.querySelector(".sidebar__hammer");
      expect(hammer?.classList.contains("sidebar__hammer--building")).toBe(true);
    });
    const hammer = container.querySelector(".sidebar__hammer");
    expect(hammer?.getAttribute("title")).toBe("Kobo is building an agent.");
  });

  it("does not pulse the hammer mark when every agent is in a terminal state", async () => {
    stubFetch({
      "/api/agents": {
        agents: [
          { ...BASE_AGENT, name: "done-agent", status: "ready" },
          { ...BASE_AGENT, name: "failed-agent", status: "failed" },
        ],
        groups_unavailable: false,
      },
      "/api/": {},
    });
    const { container } = renderWithClient(Sidebar, {
      active: "dashboard",
      oncreate: () => {},
      onprefetch: () => {},
    });

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith("/api/agents");
    });
    const hammer = container.querySelector(".sidebar__hammer");
    expect(hammer?.classList.contains("sidebar__hammer--building")).toBe(false);
    expect(hammer?.getAttribute("title")).toBeNull();
  });

  it("does not pulse the hammer mark before the agents list has loaded", () => {
    const { container } = renderWithClient(Sidebar, {
      active: "dashboard",
      oncreate: () => {},
      onprefetch: () => {},
    });
    const hammer = container.querySelector(".sidebar__hammer");
    expect(hammer?.classList.contains("sidebar__hammer--building")).toBe(false);
  });

  it("defines the pulse keyframes with a prefers-reduced-motion override that turns the animation off", () => {
    // Component styles never make it into jsdom's `document` under this
    // project's vite/vitest setup (no <style> tag gets injected on mount —
    // confirmed empirically), so the CSS text itself is read straight from
    // source, the same static-scan approach lib/copy.test.ts already uses
    // for its em-dash guard.
    expect(SIDEBAR_SOURCE).toMatch(/sidebar__hammer--building/);
    expect(SIDEBAR_SOURCE).toMatch(/animation:/);

    // Extract the `@media (prefers-reduced-motion: reduce) { ... }` block by
    // brace-counting (a plain regex can't safely match nested braces) and
    // confirm it turns the hammer's own pulse animation off.
    const start = SIDEBAR_SOURCE.indexOf("@media (prefers-reduced-motion: reduce)");
    expect(start).toBeGreaterThanOrEqual(0);
    const openBrace = SIDEBAR_SOURCE.indexOf("{", start);
    let depth = 0;
    let end = -1;
    for (let i = openBrace; i < SIDEBAR_SOURCE.length; i++) {
      if (SIDEBAR_SOURCE[i] === "{") depth++;
      if (SIDEBAR_SOURCE[i] === "}") {
        depth--;
        if (depth === 0) {
          end = i;
          break;
        }
      }
    }
    expect(end).toBeGreaterThan(openBrace);
    const reducedMotionBlock = SIDEBAR_SOURCE.slice(start, end + 1);
    expect(reducedMotionBlock).toMatch(/sidebar__hammer/);
    expect(reducedMotionBlock).toMatch(/animation:\s*none/);
  });
});
