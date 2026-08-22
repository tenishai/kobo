// @vitest-environment jsdom
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fireEvent, screen, waitFor } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Sidebar from "./Sidebar.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

const SIDEBAR_SOURCE = readFileSync(join(__dirname, "Sidebar.svelte"), "utf8");
const INDEX_HTML = readFileSync(join(__dirname, "..", "..", "index.html"), "utf8");

/** Extracts a brace-delimited block (a `@media { ... }` or `@keyframes { ... }`
 * rule) starting at `startMarker` by brace-counting, since a plain regex
 * can't safely match nested braces. Returns the block including its own
 * outer braces. Shared by the keyframes and reduced-motion checks below. */
function blockAt(source: string, startMarker: string): string {
  const start = source.indexOf(startMarker);
  expect(start).toBeGreaterThanOrEqual(0);
  const openBrace = source.indexOf("{", start);
  let depth = 0;
  let end = -1;
  for (let i = openBrace; i < source.length; i++) {
    if (source[i] === "{") depth++;
    if (source[i] === "}") {
      depth--;
      if (depth === 0) {
        end = i;
        break;
      }
    }
  }
  expect(end).toBeGreaterThan(openBrace);
  return source.slice(start, end + 1);
}

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

  it("renders the operator's hammer.png mark to the left of the wordmark", () => {
    const { container } = renderWithClient(Sidebar, {
      active: "dashboard",
      oncreate: () => {},
      onprefetch: () => {},
    });
    const logo = container.querySelector(".sidebar__logo");
    const hammer = container.querySelector(".sidebar__hammer");
    expect(hammer).not.toBeNull();
    expect(hammer!.tagName.toLowerCase()).toBe("img");
    expect(hammer!.getAttribute("src")).toBe("/hammer.png");
    expect(hammer!.getAttribute("width")).toBe("26");
    expect(hammer!.getAttribute("height")).toBe("26");
    expect(hammer!.getAttribute("aria-hidden")).toBe("true");
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

    // Confirm the reduced-motion override turns the hammer's own pulse
    // animation off, but still names the hammer (not just "any animation").
    const reducedMotionBlock = blockAt(SIDEBAR_SOURCE, "@media (prefers-reduced-motion: reduce)");
    expect(reducedMotionBlock).toMatch(/sidebar__hammer/);
    expect(reducedMotionBlock).toMatch(/animation:\s*none/);
  });

  it("breathes a green glow (drop-shadow tied to --ok) in step with the pulse's opacity/scale", () => {
    // The glow radius animates alongside the existing opacity/scale beats
    // rather than being a separate, out-of-sync animation — same
    // `hammer-pulse` keyframes, an added `filter` line per stop.
    const keyframesBlock = blockAt(SIDEBAR_SOURCE, "@keyframes hammer-pulse");
    const dropShadows = [...keyframesBlock.matchAll(/filter:\s*drop-shadow\(([^)]*var\(--ok\)[^)]*)\)/g)].map(
      (m) => m[1],
    );
    // One drop-shadow per keyframe stop (0%/100% share a stop, 50% is the
    // other), and the radius actually changes between them — a breath, not
    // a static glow baked into a looping animation.
    expect(dropShadows.length).toBeGreaterThanOrEqual(2);
    expect(new Set(dropShadows).size).toBeGreaterThan(1);
  });

  it("keeps a static soft glow under reduced motion — a visible state signal without relying on motion", () => {
    const reducedMotionBlock = blockAt(SIDEBAR_SOURCE, "@media (prefers-reduced-motion: reduce)");
    expect(reducedMotionBlock).toMatch(/animation:\s*none/);
    expect(reducedMotionBlock).toMatch(/filter:\s*drop-shadow\([^)]*var\(--ok\)[^)]*\)/);
  });

  it("is a decorative image -- alt text is empty since aria-hidden already removes it from the a11y tree", () => {
    const { container } = renderWithClient(Sidebar, {
      active: "dashboard",
      oncreate: () => {},
      onprefetch: () => {},
    });
    const hammer = container.querySelector(".sidebar__hammer");
    expect(hammer?.getAttribute("alt")).toBe("");
  });
});

describe("Sidebar — favicon (the same operator-supplied hammer, on the tab)", () => {
  it("links a PNG favicon from index.html", () => {
    expect(INDEX_HTML).toMatch(
      /<link\s+rel="icon"\s+type="image\/png"\s+href="[^"]+"\s*\/?>/,
    );
  });

  it("points the favicon link at a png file that actually exists on disk", () => {
    const match = INDEX_HTML.match(/<link\s+rel="icon"\s+type="image\/png"\s+href="([^"]+)"/);
    expect(match).not.toBeNull();
    const href = match![1].replace(/^\//, "");
    const pngPath = join(__dirname, "..", "..", "public", href);
    const pngBytes = readFileSync(pngPath);
    expect(pngBytes.length).toBeGreaterThan(0);
  });

  it("has a valid PNG signature, since a mislabeled/corrupt asset would silently fail to render as a tab icon", () => {
    // Regression-shaped guard: the old favicon.svg test caught XML
    // well-formedness the same way -- here, the equivalent structural
    // check for a binary PNG asset is its 8-byte magic-number header
    // (the fixed signature every PNG file starts with).
    const match = INDEX_HTML.match(/<link\s+rel="icon"\s+type="image\/png"\s+href="([^"]+)"/);
    const href = match![1].replace(/^\//, "");
    const pngPath = join(__dirname, "..", "..", "public", href);
    const header = readFileSync(pngPath).subarray(0, 8);
    expect([...header]).toEqual([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  });
});
