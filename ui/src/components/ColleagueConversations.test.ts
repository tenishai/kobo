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

// --- Terminal legibility wave: date rules, hover timestamps, xN collapse,
// filter box, counterpart chips (the operator's "hard to search / hard to
// follow with multiple agents / old conversations look like today" report) --

const DAY_ONE_ASK = { ...ASK_ENTRY, ts: "2026-08-19T10:00:00Z", conversation_id: "d1" };
const DAY_TWO_ASK = {
  ...ASK_ENTRY,
  ts: "2026-08-21T09:30:00Z",
  conversation_id: "d2",
  text: "Still the same config?",
};

describe("ColleagueConversations (legibility wave)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    FakeEventSource.instances = [];
    vi.stubGlobal("EventSource", FakeEventSource);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows each line's own date inline next to the time, across a day boundary", async () => {
    stubFetch({
      "/api/colleagues/feed": { entries: [DAY_ONE_ASK, DAY_TWO_ASK], truncated: false },
    });

    const { container } = renderWithClient(ColleagueConversations, { agentName: "ward" });
    await screen.findByText("What's the config?");

    // Every line carries its own date span (self-contained, no day divider).
    const days = Array.from(container.querySelectorAll(".colleague-terminal__day")).map(
      (el) => el.textContent,
    );
    expect(days).toEqual(["2026-08-19", "2026-08-21"]);
  });

  it("every line on the same day still shows the date, so a line never reads as today alone", async () => {
    // ASK_ENTRY + REPLY_ENTRY are both 2026-08-19: the date appears on BOTH
    // lines (not just once at a boundary), so any single line is unambiguous.
    stubFetch({
      "/api/colleagues/feed": { entries: [ASK_ENTRY, REPLY_ENTRY], truncated: false },
    });

    const { container } = renderWithClient(ColleagueConversations, { agentName: "ward" });
    await screen.findByText("What's the config?");

    const days = Array.from(container.querySelectorAll(".colleague-terminal__day")).map(
      (el) => el.textContent,
    );
    expect(days).toEqual(["2026-08-19", "2026-08-19"]);
  });

  it("hovering the timestamp shows the full ISO string (title on the date+time stamp)", async () => {
    stubFetch({
      "/api/colleagues/feed": { entries: [DAY_ONE_ASK], truncated: false },
    });

    const { container } = renderWithClient(ColleagueConversations, { agentName: "ward" });
    await screen.findByText("What's the config?");

    const stamp = container.querySelector(".colleague-terminal__stamp");
    expect(stamp?.getAttribute("title")).toBe("2026-08-19T10:00:00Z");
  });

  it("consecutive identical asks collapse into one line with an xN badge", async () => {
    const repeat = (n: number) => ({
      ...ASK_ENTRY,
      ts: `2026-08-19T10:0${n}:00Z`,
      conversation_id: `rep-${n}`,
    });
    stubFetch({
      "/api/colleagues/feed": {
        entries: [repeat(1), repeat(2), repeat(3), REPLY_ENTRY],
        truncated: false,
      },
    });

    const { container } = renderWithClient(ColleagueConversations, { agentName: "ward" });
    await screen.findByText("What's the config?");

    expect(screen.getAllByText("What's the config?")).toHaveLength(1);
    expect(screen.getByText("x3")).not.toBeNull();
    // The collapsed line shows when the repetition STARTED.
    expect(screen.getByText("10:01:00")).not.toBeNull();
    // The reply stayed its own line, uncollapsed and unbadged.
    expect(screen.getByText("Here's the config.")).not.toBeNull();
    expect(container.querySelectorAll(".colleague-terminal__count")).toHaveLength(1);
  });

  it("identical text that is NOT consecutive does not collapse", async () => {
    const a1 = { ...ASK_ENTRY, ts: "2026-08-19T10:00:00Z", conversation_id: "n1" };
    const other = {
      ...ASK_ENTRY,
      ts: "2026-08-19T10:01:00Z",
      conversation_id: "n2",
      text: "Something else entirely.",
    };
    const a2 = { ...ASK_ENTRY, ts: "2026-08-19T10:02:00Z", conversation_id: "n3" };
    stubFetch({
      "/api/colleagues/feed": { entries: [a1, other, a2], truncated: false },
    });

    renderWithClient(ColleagueConversations, { agentName: "ward" });
    await screen.findByText("Something else entirely.");

    expect(screen.getAllByText("What's the config?")).toHaveLength(2);
    expect(screen.queryByText("x2")).toBeNull();
  });

  it("the filter box narrows lines to matches and clears back to everything", async () => {
    stubFetch({
      "/api/colleagues/feed": { entries: [ASK_ENTRY, REPLY_ENTRY], truncated: false },
    });

    renderWithClient(ColleagueConversations, { agentName: "ward" });
    await screen.findByText("What's the config?");

    const box = screen.getByLabelText("Filter conversations") as HTMLInputElement;
    await fireEvent.input(box, { target: { value: "here's" } });

    expect(screen.queryByText("What's the config?")).toBeNull();
    expect(screen.getByText("Here's the config.")).not.toBeNull();

    await fireEvent.input(box, { target: { value: "" } });
    expect(screen.getByText("What's the config?")).not.toBeNull();
  });

  it("a filter with no matches says so instead of showing an empty void", async () => {
    stubFetch({
      "/api/colleagues/feed": { entries: [ASK_ENTRY], truncated: false },
    });

    renderWithClient(ColleagueConversations, { agentName: "ward" });
    await screen.findByText("What's the config?");

    const box = screen.getByLabelText("Filter conversations") as HTMLInputElement;
    await fireEvent.input(box, { target: { value: "zzz-nothing" } });

    expect(await screen.findByText("No lines match the filter.")).not.toBeNull();
  });

  it("counterpart chips scope the feed to one colleague and toggle back off", async () => {
    const toRelay = {
      ...ASK_ENTRY,
      ts: "2026-08-19T11:00:00Z",
      conversation_id: "r-1",
      to: "relay",
      text: "Relay, are you there?",
    };
    stubFetch({
      "/api/colleagues/feed": {
        entries: [ASK_ENTRY, REPLY_ENTRY, toRelay],
        truncated: false,
      },
    });

    renderWithClient(ColleagueConversations, { agentName: "ward" });
    await screen.findByText("Relay, are you there?");

    // One chip per counterpart, derived from the feed (never the viewed agent).
    const networkChip = screen.getByRole("button", { name: "network" });
    expect(screen.getByRole("button", { name: "relay" })).not.toBeNull();
    expect(screen.queryByRole("button", { name: "ward" })).toBeNull();

    await fireEvent.click(networkChip);
    expect(screen.getByText("What's the config?")).not.toBeNull();
    expect(screen.getByText("Here's the config.")).not.toBeNull();
    expect(screen.queryByText("Relay, are you there?")).toBeNull();

    // Clicking the active chip again clears the scope.
    await fireEvent.click(networkChip);
    expect(await screen.findByText("Relay, are you there?")).not.toBeNull();
  });

  it("chip scoping and the filter box compose (both must match)", async () => {
    const toRelay = {
      ...ASK_ENTRY,
      ts: "2026-08-19T11:00:00Z",
      conversation_id: "r-1",
      to: "relay",
      text: "What's the config, relay?",
    };
    stubFetch({
      "/api/colleagues/feed": {
        entries: [ASK_ENTRY, toRelay],
        truncated: false,
      },
    });

    renderWithClient(ColleagueConversations, { agentName: "ward" });
    await screen.findByText("What's the config, relay?");

    await fireEvent.click(screen.getByRole("button", { name: "network" }));
    const box = screen.getByLabelText("Filter conversations") as HTMLInputElement;
    await fireEvent.input(box, { target: { value: "config" } });

    expect(screen.getByText("What's the config?")).not.toBeNull();
    expect(screen.queryByText("What's the config, relay?")).toBeNull();
  });
});

// --- Review fix round: day-boundary collapse, chip dead-end escape, null
// field guards, re-pin on filter transitions, empty-transition reset -------

describe("ColleagueConversations (review fix round)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    FakeEventSource.instances = [];
    vi.stubGlobal("EventSource", FakeEventSource);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("an identical run spanning midnight breaks at the day boundary", async () => {
    const rep = (ts: string, id: string) => ({ ...ASK_ENTRY, ts, conversation_id: id });
    stubFetch({
      "/api/colleagues/feed": {
        entries: [
          rep("2026-08-19T23:58:00Z", "m1"),
          rep("2026-08-20T00:01:00Z", "m2"),
          rep("2026-08-20T00:03:00Z", "m3"),
        ],
        truncated: false,
      },
    });

    renderWithClient(ColleagueConversations, { agentName: "ward" });
    await screen.findAllByText("What's the config?");

    // Two lines: one on the 19th (x1, unbadged), one on the 20th (x2) --
    // today's repeats are filed under TODAY, never under yesterday's rule.
    expect(screen.getAllByText("What's the config?")).toHaveLength(2);
    expect(screen.getByText("2026-08-19")).not.toBeNull();
    expect(screen.getByText("2026-08-20")).not.toBeNull();
    expect(screen.getByText("x2")).not.toBeNull();
    expect(screen.queryByText("x3")).toBeNull();
  });

  it("the no-match state offers Clear filter, which escapes a dead chip", async () => {
    const toRelay = {
      ...ASK_ENTRY,
      ts: "2026-08-19T11:00:00Z",
      conversation_id: "r-1",
      to: "relay",
      text: "Relay, are you there?",
    };
    stubFeedSequence(
      { entries: [ASK_ENTRY, toRelay], truncated: false },
      // The refetch loses every "network" conversation (room-window eviction):
      { entries: [toRelay], truncated: false },
    );

    renderWithClient(ColleagueConversations, { agentName: "ward" });
    await screen.findByText("Relay, are you there?");

    await fireEvent.click(screen.getByRole("button", { name: "network" }));
    expect(screen.getByText("What's the config?")).not.toBeNull();

    // Eviction: the active counterpart vanishes from the feed entirely.
    FakeEventSource.instances[0].emit("dispatch_success");
    expect(await screen.findByText("No lines match the filter.")).not.toBeNull();
    // The chip row is gone (one counterpart left) -- the escape hatch is not.
    expect(screen.queryByRole("button", { name: "network" })).toBeNull();

    await fireEvent.click(screen.getByRole("button", { name: "Clear filter" }));
    expect(await screen.findByText("Relay, are you there?")).not.toBeNull();
  });

  it("a null counterpart field never crashes the filter", async () => {
    const nullTo = { ...ASK_ENTRY, to: null as unknown as string };
    stubFetch({
      "/api/colleagues/feed": { entries: [nullTo], truncated: false },
    });

    renderWithClient(ColleagueConversations, { agentName: "ward" });
    await screen.findByText("What's the config?");

    const box = screen.getByLabelText("Filter conversations") as HTMLInputElement;
    await fireEvent.input(box, { target: { value: "zzz" } });
    expect(await screen.findByText("No lines match the filter.")).not.toBeNull();

    await fireEvent.input(box, { target: { value: "config" } });
    expect(await screen.findByText("What's the config?")).not.toBeNull();
  });

  it("clearing a chip while pinned re-pins to the true bottom", async () => {
    const toRelay = {
      ...ASK_ENTRY,
      ts: "2026-08-19T11:00:00Z",
      conversation_id: "r-1",
      to: "relay",
      text: "Relay, are you there?",
    };
    stubFetch({
      "/api/colleagues/feed": { entries: [ASK_ENTRY, REPLY_ENTRY, toRelay], truncated: false },
    });

    renderWithClient(ColleagueConversations, { agentName: "ward" });
    await screen.findByText("Relay, are you there?");

    const logEl = screen.getByRole("log") as HTMLDivElement;
    Object.defineProperty(logEl, "scrollHeight", { value: 900, configurable: true });
    Object.defineProperty(logEl, "clientHeight", { value: 500, configurable: true });
    Object.defineProperty(logEl, "scrollTop", { value: 0, configurable: true, writable: true });

    const chip = screen.getByRole("button", { name: "network" });
    await fireEvent.click(chip); // narrow
    await fireEvent.click(chip); // clear again: rows regrow while pinned

    await waitFor(() => {
      expect(logEl.scrollTop).toBe(900);
    });
  });

  it("a feed that empties out resets the filter state for its return", async () => {
    // Three acts: entries -> empty (state must reset) -> entries again
    // (the remounted box must be blank, every line visible).
    const acts = [
      { entries: [ASK_ENTRY], truncated: false },
      { entries: [], truncated: false },
      { entries: [ASK_ENTRY, REPLY_ENTRY], truncated: false },
    ];
    let call = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/colleagues/feed")) {
          const act = acts[Math.min(call, acts.length - 1)];
          call += 1;
          return new Response(JSON.stringify(act), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
        return new Response(JSON.stringify({}), { status: 200 });
      }),
    );

    renderWithClient(ColleagueConversations, { agentName: "ward" });
    await screen.findByText("What's the config?");

    const box = screen.getByLabelText("Filter conversations") as HTMLInputElement;
    await fireEvent.input(box, { target: { value: "zzz-no-match" } });
    expect(await screen.findByText("No lines match the filter.")).not.toBeNull();

    FakeEventSource.instances[0].emit("dispatch_success");
    expect(await screen.findByText("No agent conversations yet.")).not.toBeNull();

    FakeEventSource.instances[0].emit("dispatch_success");
    await screen.findByText("Here's the config.");
    // The stale query never lay in wait: box blank, every line visible.
    const returned = screen.getByLabelText("Filter conversations") as HTMLInputElement;
    expect(returned.value).toBe("");
    expect(screen.getByText("What's the config?")).not.toBeNull();
  });
});
