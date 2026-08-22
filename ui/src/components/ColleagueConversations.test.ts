// @vitest-environment jsdom
import { fireEvent, screen, waitFor } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ColleagueConversations from "./ColleagueConversations.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

const ASK_ENTRY = {
  ts: "2026-08-19T10:00:00Z",
  conversation_id: "t-1",
  from: "ward",
  to: "network",
  role: "ask" as const,
  text: "What's the config?",
};

const REPLY_ENTRY = {
  ts: "2026-08-19T10:00:05Z",
  conversation_id: "t-1",
  from: "network",
  to: "ward",
  role: "reply" as const,
  text: "Here's the config.",
};

/** A minimal EventSource stand-in — jsdom has no real implementation, and
 * the component rig has no existing mock for it (grepped: only `fetch` is
 * stubbed elsewhere via `vi.stubGlobal`), so this defines the smallest
 * fake that satisfies what the component actually uses: `addEventListener`
 * (per named switchboard frame) and `close`. `instances` lets a test reach
 * the one the component constructed and fire a named frame at it. */
class FakeEventSource {
  static instances: FakeEventSource[] = [];
  url: string;
  closed = false;
  private listeners = new Map<string, Set<() => void>>();

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, handler: () => void): void {
    let set = this.listeners.get(type);
    if (!set) {
      set = new Set();
      this.listeners.set(type, set);
    }
    set.add(handler);
  }

  removeEventListener(type: string, handler: () => void): void {
    this.listeners.get(type)?.delete(handler);
  }

  close(): void {
    this.closed = true;
  }

  emit(type: string): void {
    for (const handler of this.listeners.get(type) ?? []) {
      handler();
    }
  }
}

/** Stubs fetch so the FIRST `/api/colleagues/feed` call answers with
 * `first`, and every call after that answers with `later` — the shape
 * every SSE-refetch test below needs (an append becoming visible only
 * after the stream's own refetch lands). */
function stubFeedSequence(first: unknown, later: unknown): void {
  let callCount = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/colleagues/feed")) {
        callCount += 1;
        return new Response(JSON.stringify(callCount === 1 ? first : later), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response(JSON.stringify({}), { status: 200 });
    }),
  );
}

describe("ColleagueConversations (terminal)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    FakeEventSource.instances = [];
    vi.stubGlobal("EventSource", FakeEventSource);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders entries in chronological order with direction-colored prefixes and full text", async () => {
    stubFetch({
      "/api/colleagues/feed": { entries: [ASK_ENTRY, REPLY_ENTRY], truncated: false },
    });

    const { container } = renderWithClient(ColleagueConversations, { agentName: "ward" });

    expect(await screen.findByText("What's the config?")).not.toBeNull();
    expect(screen.getByText("Here's the config.")).not.toBeNull();
    expect(screen.getByText("10:00:00")).not.toBeNull();
    expect(screen.getByText("10:00:05")).not.toBeNull();

    const directions = Array.from(
      container.querySelectorAll(".colleague-terminal__direction"),
    ).map((el) => el.textContent);
    expect(directions).toEqual(["ward -> network", "network -> ward"]);
  });

  it("renders a long message's full text, unclipped", async () => {
    const longText = `${"the-switchboard-said-quite-a-lot-".repeat(30)}end-of-message`;
    stubFetch({
      "/api/colleagues/feed": {
        entries: [{ ...ASK_ENTRY, text: longText }],
        truncated: false,
      },
    });

    renderWithClient(ColleagueConversations, { agentName: "ward" });

    expect(await screen.findByText(longText)).not.toBeNull();
  });

  it('shows the quiet terminal-style empty line "No agent conversations yet."', async () => {
    stubFetch({ "/api/colleagues/feed": { entries: [], truncated: false } });

    renderWithClient(ColleagueConversations, { agentName: "ward" });

    expect(await screen.findByText("No agent conversations yet.")).not.toBeNull();
  });

  it("shows a note when the feed was capped at the scrollback limit", async () => {
    stubFetch({
      "/api/colleagues/feed": { entries: [ASK_ENTRY], truncated: true },
    });

    renderWithClient(ColleagueConversations, { agentName: "ward" });

    expect(await screen.findByText("What's the config?")).not.toBeNull();
    expect(screen.getByText("Only the most recent 200 lines are shown.")).not.toBeNull();
  });

  it("the feed fetch carries ?agent=<the viewed agent>", async () => {
    stubFetch({ "/api/colleagues/feed": { entries: [], truncated: false } });

    renderWithClient(ColleagueConversations, { agentName: "ward" });
    await screen.findByText("No agent conversations yet.");

    expect(
      vi
        .mocked(fetch)
        .mock.calls.some(
          ([url]) =>
            String(url).includes("/api/colleagues/feed") && String(url).includes("agent=ward"),
        ),
    ).toBe(true);
  });

  it("the SSE stream URL also carries ?agent=<the viewed agent>", async () => {
    stubFetch({ "/api/colleagues/feed": { entries: [], truncated: false } });

    renderWithClient(ColleagueConversations, { agentName: "ward" });
    await screen.findByText("No agent conversations yet.");

    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.instances[0].url).toBe("/api/colleagues/stream?agent=ward");
  });

  it("any SSE frame refetches the feed and the new entry appears (no client-side merging)", async () => {
    stubFeedSequence(
      { entries: [ASK_ENTRY], truncated: false },
      { entries: [ASK_ENTRY, REPLY_ENTRY], truncated: false },
    );

    renderWithClient(ColleagueConversations, { agentName: "ward" });
    await screen.findByText("What's the config?");
    expect(screen.queryByText("Here's the config.")).toBeNull();

    expect(FakeEventSource.instances).toHaveLength(1);
    FakeEventSource.instances[0].emit("dispatch_success");

    expect(await screen.findByText("Here's the config.")).not.toBeNull();
  });

  it("renders nothing (silent absence) when the room has no switchboard wired (409)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(
          JSON.stringify({ error: "the switchboard is not configured" }),
          { status: 409, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );

    const { container } = renderWithClient(ColleagueConversations, { agentName: "warden" });

    // A false `{#if}` still leaves Svelte's own anchor comments in the DOM
    // (compiled-output detail) - textContent (no visible text/elements) is
    // the accurate "rendered nothing a human would see" check, not raw
    // innerHTML.
    await waitFor(() => {
      expect(container.textContent?.trim()).toBe("");
    });
  });

  // --- Auto-scroll pin/unpin (jsdom-level: jsdom computes no real layout,
  // so scrollHeight/clientHeight/scrollTop are overridden by hand on the
  // scroll container, the standard way to exercise scroll logic under it) --

  it("scrolling away from the bottom unpins and shows Jump to latest; clicking it re-pins and jumps to the bottom", async () => {
    stubFetch({
      "/api/colleagues/feed": { entries: [ASK_ENTRY, REPLY_ENTRY], truncated: false },
    });

    renderWithClient(ColleagueConversations, { agentName: "ward" });
    await screen.findByText("What's the config?");

    const logEl = screen.getByRole("log") as HTMLDivElement;
    expect(screen.queryByText("Jump to latest")).toBeNull();

    Object.defineProperty(logEl, "scrollHeight", { value: 1000, configurable: true });
    Object.defineProperty(logEl, "clientHeight", { value: 500, configurable: true });
    Object.defineProperty(logEl, "scrollTop", {
      value: 100,
      configurable: true,
      writable: true,
    });

    await fireEvent.scroll(logEl);

    expect(await screen.findByText("Jump to latest")).not.toBeNull();

    await fireEvent.click(screen.getByText("Jump to latest"));

    expect(logEl.scrollTop).toBe(1000);
    expect(screen.queryByText("Jump to latest")).toBeNull();
  });

  it("staying within a hair of the bottom counts as pinned (no Jump to latest)", async () => {
    stubFetch({
      "/api/colleagues/feed": { entries: [ASK_ENTRY, REPLY_ENTRY], truncated: false },
    });

    renderWithClient(ColleagueConversations, { agentName: "ward" });
    await screen.findByText("What's the config?");

    const logEl = screen.getByRole("log") as HTMLDivElement;
    Object.defineProperty(logEl, "scrollHeight", { value: 1000, configurable: true });
    Object.defineProperty(logEl, "clientHeight", { value: 500, configurable: true });
    Object.defineProperty(logEl, "scrollTop", {
      value: 495, // 1000 - 495 - 500 = 5px off the bottom, inside the slack
      configurable: true,
      writable: true,
    });

    await fireEvent.scroll(logEl);

    expect(screen.queryByText("Jump to latest")).toBeNull();
  });

  it("auto-scrolls to the newest entry while pinned when a new entry arrives", async () => {
    stubFeedSequence(
      { entries: [ASK_ENTRY], truncated: false },
      { entries: [ASK_ENTRY, REPLY_ENTRY], truncated: false },
    );

    renderWithClient(ColleagueConversations, { agentName: "ward" });
    await screen.findByText("What's the config?");

    const logEl = screen.getByRole("log") as HTMLDivElement;
    Object.defineProperty(logEl, "scrollHeight", { value: 800, configurable: true });
    Object.defineProperty(logEl, "clientHeight", { value: 500, configurable: true });
    Object.defineProperty(logEl, "scrollTop", {
      value: 0,
      configurable: true,
      writable: true,
    });

    expect(FakeEventSource.instances).toHaveLength(1);
    FakeEventSource.instances[0].emit("dispatch_success");

    await screen.findByText("Here's the config.");
    await waitFor(() => {
      expect(logEl.scrollTop).toBe(800);
    });
  });
});
