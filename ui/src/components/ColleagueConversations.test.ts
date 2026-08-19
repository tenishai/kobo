// @vitest-environment jsdom
import { fireEvent, screen, waitFor } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ColleagueConversations from "./ColleagueConversations.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

const AGENT_CONVERSATION = {
  task_id: "t-1",
  target_agent: "clio",
  target_generation: 1,
  caller_agent: "agent:iris",
  state: "completed",
  cabal_escalated_from: null,
  created_at: "2020-01-01T00:00:00Z",
  updated_at: "2020-01-01T00:05:00Z",
  result_snippet: "Sure, here's how you configure that.",
};

const OPERATOR_CONVERSATION = {
  task_id: "t-2",
  target_agent: "clio",
  target_generation: 1,
  caller_agent: "operator:kgreiner@example.com",
  state: "completed",
  cabal_escalated_from: null,
  created_at: "2020-01-01T01:00:00Z",
  updated_at: "2020-01-01T01:00:02Z",
  result_snippet: "Hello there.",
};

const CONVERSATION_DETAIL = {
  conversation: { ...AGENT_CONVERSATION, result: "Sure, here's the full untruncated answer." },
  events: [
    { id: 1, event_type: "dispatch_start", payload: {}, created_at: "2020-01-01T00:00:00Z" },
    { id: 2, event_type: "dispatch_success", payload: {}, created_at: "2020-01-01T00:00:05Z" },
  ],
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

describe("ColleagueConversations", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    FakeEventSource.instances = [];
    vi.stubGlobal("EventSource", FakeEventSource);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders rows from the fixture: caller without its prefix, target, state, snippet", async () => {
    // The room API itself owns the agent-only/everything split (it's a
    // server-side filter, Task 4/B4's own design - see main.py); this stub
    // returns exactly what a default (agent-only) request would come back
    // with, and the next test proves the toggle actually changes the
    // request/response pair.
    stubFetch({
      "/api/colleagues/conversations": { conversations: [AGENT_CONVERSATION] },
    });

    renderWithClient(ColleagueConversations);

    expect(await screen.findByText("iris")).not.toBeNull();
    expect(screen.getByText("clio")).not.toBeNull();
    expect(screen.getByText("completed")).not.toBeNull();
    expect(
      screen.getByText("Sure, here's how you configure that."),
    ).not.toBeNull();
  });

  it('"Show everything" toggles the all=true request and reveals every caller', async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/colleagues/conversations")) {
          const conversations = url.includes("all=true")
            ? [AGENT_CONVERSATION, OPERATOR_CONVERSATION]
            : [AGENT_CONVERSATION];
          return new Response(JSON.stringify({ conversations }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
        return new Response(JSON.stringify({}), { status: 200 });
      }),
    );

    renderWithClient(ColleagueConversations);
    await screen.findByText("iris");
    expect(screen.queryByText("kgreiner@example.com")).toBeNull();

    await fireEvent.click(screen.getByLabelText("Show everything"));

    expect(await screen.findByText("kgreiner@example.com")).not.toBeNull();
    expect(
      vi.mocked(fetch).mock.calls.some(([url]) => String(url).includes("all=true")),
    ).toBe(true);
  });

  it("expanding a row fetches and renders the detail route's exchange", async () => {
    stubFetch({
      "/api/colleagues/conversations/t-1": CONVERSATION_DETAIL,
      "/api/colleagues/conversations": { conversations: [AGENT_CONVERSATION] },
    });

    renderWithClient(ColleagueConversations);
    await screen.findByText("iris");

    expect(
      vi.mocked(fetch).mock.calls.some(([url]) =>
        String(url).includes("/api/colleagues/conversations/t-1"),
      ),
    ).toBe(false);

    await fireEvent.click(screen.getByRole("button", { name: /iris/ }));

    expect(
      await screen.findByText("Sure, here's the full untruncated answer."),
    ).not.toBeNull();
    expect(screen.getByText("dispatch_start")).not.toBeNull();
    expect(screen.getByText("dispatch_success")).not.toBeNull();
  });

  it("any SSE frame refetches the list (no client-side event merging)", async () => {
    stubFetch({
      "/api/colleagues/conversations": { conversations: [AGENT_CONVERSATION] },
    });

    renderWithClient(ColleagueConversations);
    await screen.findByText("iris");

    const callsBefore = vi
      .mocked(fetch)
      .mock.calls.filter(([url]) => String(url).includes("/api/colleagues/conversations")).length;

    expect(FakeEventSource.instances).toHaveLength(1);
    FakeEventSource.instances[0].emit("dispatch_success");

    await waitFor(() => {
      const callsAfter = vi
        .mocked(fetch)
        .mock.calls.filter(([url]) => String(url).includes("/api/colleagues/conversations"))
        .length;
      expect(callsAfter).toBeGreaterThan(callsBefore);
    });
  });

  it("a refetch that brings materially new data does not reopen the EventSource", async () => {
    // Review finding: the effect that opens the stream must not be keyed on
    // the query's result object. If it is, invalidateQueries's own refetch
    // (triggered by the SSE handler itself, right below) tears the old
    // EventSource down and opens a new one - and since the switchboard never
    // replays, any frame arriving in that gap is lost for good.
    let conversationsCallCount = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/colleagues/conversations")) {
          conversationsCallCount += 1;
          // First load: one row. Every refetch after: a second, genuinely
          // new row - not the same object, not the same JSON text.
          const conversations =
            conversationsCallCount === 1
              ? [AGENT_CONVERSATION]
              : [AGENT_CONVERSATION, OPERATOR_CONVERSATION];
          return new Response(JSON.stringify({ conversations }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
        return new Response(JSON.stringify({}), { status: 200 });
      }),
    );

    renderWithClient(ColleagueConversations);
    await screen.findByText("iris");
    expect(FakeEventSource.instances).toHaveLength(1);
    const firstSource = FakeEventSource.instances[0];

    firstSource.emit("dispatch_success");

    await waitFor(() => {
      expect(screen.queryByText("kgreiner@example.com")).not.toBeNull();
    });

    expect(FakeEventSource.instances).toHaveLength(1);
    expect(firstSource.closed).toBe(false);
  });

  it('shows the empty state "No colleague conversations yet. They start talking once an agent asks for help."', async () => {
    stubFetch({ "/api/colleagues/conversations": { conversations: [] } });

    renderWithClient(ColleagueConversations);

    expect(
      await screen.findByText(
        "No colleague conversations yet. They start talking once an agent asks for help.",
      ),
    ).not.toBeNull();
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

    const { container } = renderWithClient(ColleagueConversations);

    // A false `{#if}` still leaves Svelte's own anchor comments in the DOM
    // (compiled-output detail) - textContent (no visible text/elements) is
    // the accurate "rendered nothing a human would see" check, not raw
    // innerHTML.
    await waitFor(() => {
      expect(container.textContent?.trim()).toBe("");
    });
    expect(screen.queryByLabelText("Show everything")).toBeNull();
  });
});
