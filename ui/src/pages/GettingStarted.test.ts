// @vitest-environment jsdom
import { fireEvent, screen, waitFor } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import GettingStarted from "./GettingStarted.svelte";
import { navigate } from "../lib/router.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

// Guide wave (2026-09-24): the page reads the room's guide from
// `GET /api/guide` and renders one section at a time, so these tests stub
// that one route and check the reading experience, not the words -- the
// words are the API package's, pinned by api/tests/test_guide.py.

const GUIDE = {
  sections: [
    {
      id: "before-you-start",
      title: "Before you start",
      summary: "What the room is.",
      body: "## What you need\n\n- A sign-in.\n- A group.\n\nPress **New agent** to begin.\n",
    },
    {
      id: "slack",
      title: "Slack",
      summary: "One-time Slack app setup.",
      body: "## 1. Create the Slack app\n\nCopy the manifest, then paste `xoxb-` and `xapp-` back.\n",
    },
    {
      id: "glossary",
      title: "Glossary",
      summary: "The words the room uses.",
      body: "- **Agent.** A running assistant.\n",
    },
  ],
};

describe("GettingStarted", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    navigate({ page: "getting-started" });
    stubFetch({ "/api/guide": GUIDE });
  });

  it("lists every section down the side, numbered, and opens the first", async () => {
    renderWithClient(GettingStarted);
    const nav = await screen.findByRole("navigation", { name: "Guide sections" });
    const links = Array.from(nav.querySelectorAll("a"));
    expect(links.map((a) => a.textContent?.replace(/\s+/g, " ").trim())).toEqual([
      "1 Before you start",
      "2 Slack",
      "3 Glossary",
    ]);
    expect(links[0].getAttribute("aria-current")).toBe("page");
    expect(links[1].getAttribute("href")).toBe("#/getting-started/slack");

    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Before you start");
    expect(screen.getByText("What the room is.")).not.toBeNull();
    // The body is rendered markdown: a heading, a list, bold.
    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("What you need");
    expect(screen.getByText("A sign-in.")).not.toBeNull();
    expect(screen.getByText("New agent").tagName).toBe("STRONG");
  });

  it("switches sections from the side list and writes the section into the hash", async () => {
    renderWithClient(GettingStarted);
    const slack = await screen.findByRole("link", { name: /Slack/ });
    await fireEvent.click(slack);

    await waitFor(() =>
      expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Slack"),
    );
    expect(location.hash).toBe("#/getting-started/slack");
    expect(screen.getByText("xoxb-").tagName).toBe("CODE");
    expect(slack.getAttribute("aria-current")).toBe("page");
  });

  it("opens the section the hash names, and the first for one it does not have", async () => {
    navigate({ page: "getting-started", section: "glossary" });
    const first = renderWithClient(GettingStarted);
    await waitFor(() =>
      expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Glossary"),
    );
    first.unmount();

    navigate({ page: "getting-started", section: "never-written" });
    renderWithClient(GettingStarted);
    await waitFor(() =>
      expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Before you start"),
    );
  });

  it("points at the connector so an assistant can read the same guide", async () => {
    renderWithClient(GettingStarted);
    await screen.findByRole("heading", { level: 1 });
    expect(screen.getByText(/connect it to the room as an MCP server/)).not.toBeNull();
  });
});
