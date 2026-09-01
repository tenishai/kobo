// @vitest-environment jsdom
import { fireEvent, screen } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";

import LearningsPanel from "./LearningsPanel.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

const LEARNINGS = {
  learnings: [
    {
      id: 1,
      day: "2026-09-01",
      content: "the canary gate is manual",
      kind: "fact",
      source: "Slack thread, Sep 1",
      evidence: "gate is manual now",
      status: "pending",
      decided_by: null,
      created_at: "2026-09-01T05:00:00",
    },
    {
      id: 2,
      day: "2026-08-31",
      content: "old denied thing",
      kind: "fact",
      source: "Slack",
      evidence: "",
      status: "denied",
      decided_by: "kyle@example.test",
      created_at: "2026-08-31T05:00:00",
    },
  ],
};

describe("LearningsPanel", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    stubFetch({ "/learnings": LEARNINGS });
  });

  it("renders pending candidates grouped by day", async () => {
    renderWithClient(LearningsPanel, {
      agentName: "wren",
      canDecide: true,
      frozen: false,
    });

    expect(await screen.findByText("the canary gate is manual")).not.toBeNull();
    expect(screen.getByText(/2026-09-01/)).not.toBeNull();
    // Denied row lives in history, not in the pending list.
    expect(screen.queryByText("old denied thing")).toBeNull();
  });

  it("hides verdict buttons when the caller cannot decide", async () => {
    renderWithClient(LearningsPanel, {
      agentName: "wren",
      canDecide: false,
      frozen: false,
    });

    await screen.findByText("the canary gate is manual");
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Deny" })).toBeNull();
  });

  // render.ts's `stubFetch` only supports static JSON bodies keyed by URL
  // substring (no function-valued routes to record calls or vary the
  // response per invocation) -- so this asserts the deny POST the same way
  // MemoryPanel.test.ts's own mutation tests do: inspecting `fetch`'s mock
  // calls directly, rather than a route callback recording invocations.
  // The single static "/learnings" route above also matches the POST's own
  // URL (`/learnings/1/deny`), which is fine -- `decideLearning` (api.ts)
  // only parses the response as JSON and discards it (`Promise<void>`), so
  // its shape doesn't matter here.
  it("posts a deny verdict", async () => {
    renderWithClient(LearningsPanel, {
      agentName: "wren",
      canDecide: true,
      frozen: false,
    });

    await screen.findByText("the canary gate is manual");
    await fireEvent.click(screen.getByRole("button", { name: "Deny" }));

    await vi.waitFor(() => {
      expect(
        vi
          .mocked(fetch)
          .mock.calls.some(
            ([url, init]) =>
              String(url).includes("/learnings/1/deny") &&
              (init as RequestInit | undefined)?.method === "POST",
          ),
      ).toBe(true);
    });
  });

  // render.ts's `stubFetch` only ever answers 200 with a static body, so a
  // failed fetch is simulated with a custom `vi.stubGlobal("fetch", ...)`
  // returning a non-ok Response -- the same pattern
  // GroupRequestsCard.test.ts's own "shows a retryable error state" test
  // uses. Pins the fix for the reviewer's finding: before this, a failed
  // learnings fetch fell into the `pending.length === 0` branch and
  // rendered "Nothing waiting for review" -- a silent failure
  // indistinguishable from a genuinely empty queue.
  it("shows a retryable error state when the fetch fails, not the empty state", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/learnings")) {
          return new Response(JSON.stringify({ error: "boom" }), {
            status: 500,
            headers: { "Content-Type": "application/json" },
          });
        }
        throw new Error(`unstubbed fetch: ${url}`);
      }),
    );

    renderWithClient(LearningsPanel, {
      agentName: "wren",
      canDecide: true,
      frozen: false,
    });

    expect(await screen.findByText("boom")).not.toBeNull();
    expect(screen.getByRole("button", { name: "Try again" })).not.toBeNull();
    expect(screen.queryByText(/Nothing waiting for review/)).toBeNull();
  });

  it("shows an empty state when nothing is pending", async () => {
    stubFetch({ "/learnings": { learnings: [] } });
    renderWithClient(LearningsPanel, {
      agentName: "wren",
      canDecide: true,
      frozen: false,
    });

    expect(
      await screen.findByText(/Nothing waiting for review/),
    ).not.toBeNull();
  });
});
