// @vitest-environment jsdom
import { fireEvent, screen } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";

import LearningsPanel from "./LearningsPanel.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

// One row carries the new `said_by` field; the other is a legacy row from
// before that field existed (`said_by: null`) -- the byline falls back to
// just its source in that case (LearningsPanel.svelte's own `byline`
// helper: `[l.said_by, l.source].filter(Boolean).join(" · ")`).
const LEARNINGS = {
  learnings: [
    {
      id: 1,
      day: "2026-09-01",
      content: "the canary gate is manual",
      kind: "fact",
      source: "Slack thread, Sep 1",
      evidence: "gate is manual now",
      said_by: "kgreiner",
      status: "pending",
      decided_by: null,
      created_at: "2026-09-01T05:00:00",
    },
    {
      id: 2,
      day: "2026-09-01",
      content: "the deploy window is nine to five",
      kind: "fact",
      source: "Slack thread, Sep 1",
      evidence: "window is nine to five now",
      said_by: null,
      status: "pending",
      decided_by: null,
      created_at: "2026-09-01T05:05:00",
    },
    {
      id: 3,
      day: "2026-08-31",
      content: "old denied thing",
      kind: "fact",
      source: "Slack",
      evidence: "",
      said_by: "kyle",
      status: "denied",
      decided_by: "kyle@example.test",
      created_at: "2026-08-31T05:00:00",
    },
  ],
};

/** Open the "Teach something" disclosure. The teach box lives behind it
 * now, shut on arrival. */
async function openTeach(): Promise<void> {
  await fireEvent.click(
    await screen.findByRole("button", { name: "Teach something" }),
  );
}

describe("LearningsPanel", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    stubFetch({ "/learnings": LEARNINGS });
  });

  it("keeps the teach box shut until it is asked for, and opens it on click", async () => {
    renderWithClient(LearningsPanel, {
      agentName: "wren",
      canDecide: true,
      frozen: false,
    });

    const toggle = await screen.findByRole("button", { name: "Teach something" });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByPlaceholderText("Teach wren something...")).toBeNull();

    await fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByPlaceholderText("Teach wren something...")).not.toBeNull();
  });

  it("renders one list with the sentence and byline, no kind text, and a correct summary count", async () => {
    renderWithClient(LearningsPanel, {
      agentName: "wren",
      canDecide: true,
      frozen: false,
    });

    expect(await screen.findByText("the canary gate is manual")).not.toBeNull();
    expect(screen.getByText("kgreiner · Slack thread, Sep 1")).not.toBeNull();
    // Legacy row (said_by: null) falls back to just its source.
    expect(screen.getByText("the deploy window is nine to five")).not.toBeNull();
    expect(screen.getByText("Slack thread, Sep 1")).not.toBeNull();

    // Neither the raw kind value nor an uppercased form shows up anywhere.
    expect(screen.queryByText(/\bfact\b/i)).toBeNull();
    expect(screen.queryByText("FACT")).toBeNull();

    // Denied row lives in history, not the pending list.
    expect(screen.queryByText("old denied thing")).toBeNull();

    expect(screen.getByText("wren picked up 2 things")).not.toBeNull();
  });

  it("hides Remember/Skip, the pencil, the bulk buttons, and the teach box when canDecide is false", async () => {
    renderWithClient(LearningsPanel, {
      agentName: "wren",
      canDecide: false,
      frozen: false,
    });

    await screen.findByText("the canary gate is manual");
    expect(screen.queryByRole("button", { name: "Remember" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Skip" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Fix the wording" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Remember all" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Skip all" })).toBeNull();
    expect(
      screen.queryByPlaceholderText("Teach wren something..."),
    ).toBeNull();
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
  it("posts a remember verdict with no body when the wording isn't touched", async () => {
    renderWithClient(LearningsPanel, {
      agentName: "wren",
      canDecide: true,
      frozen: false,
    });

    await screen.findByText("the canary gate is manual");
    const [rememberButton] = screen.getAllByRole("button", { name: "Remember" });
    await fireEvent.click(rememberButton);

    await vi.waitFor(() => {
      const call = vi
        .mocked(fetch)
        .mock.calls.find(([url]) => String(url).includes("/learnings/1/approve"));
      expect(call).toBeDefined();
      const init = call?.[1] as RequestInit | undefined;
      expect(init?.method).toBe("POST");
      expect(init?.body).toBeUndefined();
    });
  });

  it("posts a JSON body with the edited content after the pencil changes the wording", async () => {
    renderWithClient(LearningsPanel, {
      agentName: "wren",
      canDecide: true,
      frozen: false,
    });

    await screen.findByText("the canary gate is manual");
    const [pencil] = screen.getAllByRole("button", { name: "Fix the wording" });
    await fireEvent.click(pencil);

    const edit = screen.getByRole("textbox", { name: "Fix the wording" }) as HTMLInputElement;
    await fireEvent.input(edit, { target: { value: "the canary gate is now manual" } });

    const [rememberButton] = screen.getAllByRole("button", { name: "Remember" });
    await fireEvent.click(rememberButton);

    await vi.waitFor(() => {
      const call = vi
        .mocked(fetch)
        .mock.calls.find(([url]) => String(url).includes("/learnings/1/approve"));
      expect(call).toBeDefined();
      const init = call?.[1] as RequestInit | undefined;
      expect(init?.method).toBe("POST");
      expect(init?.body).toBe(
        JSON.stringify({ content: "the canary gate is now manual" }),
      );
    });
  });

  it("reveals the quote when the sentence is tapped, and flips aria-expanded", async () => {
    renderWithClient(LearningsPanel, {
      agentName: "wren",
      canDecide: true,
      frozen: false,
    });

    const statement = await screen.findByText("the canary gate is manual");
    expect(statement.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("gate is manual now")).toBeNull();

    await fireEvent.click(statement);

    expect(statement.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("gate is manual now")).not.toBeNull();
  });

  it("requires two taps for 'Remember all': the first shows a confirm prompt and posts nothing, the second posts the bulk approve", async () => {
    renderWithClient(LearningsPanel, {
      agentName: "wren",
      canDecide: true,
      frozen: false,
    });

    await screen.findByText("the canary gate is manual");
    await fireEvent.click(screen.getByRole("button", { name: "Remember all" }));

    expect(screen.getByText("Remember all 2?")).not.toBeNull();
    expect(
      vi.mocked(fetch).mock.calls.some(([url]) => String(url).includes("/learnings/bulk")),
    ).toBe(false);

    await fireEvent.click(screen.getByRole("button", { name: "Yes, remember" }));

    await vi.waitFor(() => {
      const call = vi
        .mocked(fetch)
        .mock.calls.find(([url]) => String(url).includes("/learnings/bulk"));
      expect(call).toBeDefined();
      const init = call?.[1] as RequestInit | undefined;
      expect(init?.method).toBe("POST");
      expect(JSON.parse(String(init?.body))).toEqual({ ids: [1, 2], action: "approve" });
    });
  });

  it("restores the two bulk buttons and posts nothing when the confirm prompt is cancelled", async () => {
    renderWithClient(LearningsPanel, {
      agentName: "wren",
      canDecide: true,
      frozen: false,
    });

    await screen.findByText("the canary gate is manual");
    await fireEvent.click(screen.getByRole("button", { name: "Remember all" }));
    expect(screen.getByText("Remember all 2?")).not.toBeNull();

    await fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByText("Remember all 2?")).toBeNull();
    expect(screen.getByRole("button", { name: "Remember all" })).not.toBeNull();
    expect(screen.getByRole("button", { name: "Skip all" })).not.toBeNull();
    expect(
      vi.mocked(fetch).mock.calls.some(([url]) => String(url).includes("/learnings/bulk")),
    ).toBe(false);
  });

  it("posts the teach text, clears the box, and shows a confirmation note", async () => {
    stubFetch({
      "/learnings": LEARNINGS,
      "/teach": { filename: "teach-1.md" },
    });
    renderWithClient(LearningsPanel, {
      agentName: "wren",
      canDecide: true,
      frozen: false,
    });

    await screen.findByText("the canary gate is manual");
    await openTeach();
    const input = screen.getByPlaceholderText("Teach wren something...") as HTMLInputElement;
    await fireEvent.input(input, { target: { value: "the release train leaves at noon" } });
    await fireEvent.click(screen.getByRole("button", { name: "Add" }));

    await vi.waitFor(() => {
      const call = vi.mocked(fetch).mock.calls.find(([url]) => String(url).includes("/teach"));
      expect(call).toBeDefined();
      const init = call?.[1] as RequestInit | undefined;
      expect(init?.method).toBe("POST");
      expect(JSON.parse(String(init?.body))).toEqual({
        text: "the release train leaves at noon",
      });
    });

    expect(await screen.findByText("wren will remember that.")).not.toBeNull();
    expect(input.value).toBe("");
  });

  // The teach box's own lock is `frozen || teach.isPending` -- it must never
  // depend on `decide`/`bulkDecide` being in flight (that's the bug this
  // follow-up fixes). render.ts's `stubFetch` never leaves a request
  // pending (every route resolves immediately), so a never-resolving verdict
  // fetch is stubbed by hand here, the same "custom vi.stubGlobal" idiom the
  // retryable-error test below already uses for a non-ok response.
  it("keeps the teach box enabled while an unrelated verdict request is in flight", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/learnings/1/deny")) {
          return new Promise<Response>(() => {}); // never resolves
        }
        if (url.includes("/learnings")) {
          return new Response(JSON.stringify(LEARNINGS), {
            status: 200,
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

    await screen.findByText("the canary gate is manual");
    await openTeach();
    const [skipButton] = screen.getAllByRole("button", { name: "Skip" });
    await fireEvent.click(skipButton);

    // The verdict is now stuck pending; the row's own Skip button (its
    // `deciding` lock) is disabled, but the teach box does not share that
    // lock.
    await vi.waitFor(() => {
      expect((skipButton as HTMLButtonElement).disabled).toBe(true);
    });
    const input = screen.getByPlaceholderText("Teach wren something...") as HTMLInputElement;
    expect(input.disabled).toBe(false);
  });

  // render.ts's `stubFetch` only ever answers 200 with a static body, so a
  // failed fetch is simulated with a custom `vi.stubGlobal("fetch", ...)`
  // returning a non-ok Response -- the same pattern
  // GroupRequestsCard.test.ts's own "shows a retryable error state" test
  // uses. Pins the fix for the reviewer's finding: before this, a failed
  // learnings fetch fell into the `pending.length === 0` branch and
  // rendered the empty state -- a silent failure indistinguishable from a
  // genuinely empty queue.
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
    expect(screen.queryByText(/Nothing new/)).toBeNull();
  });

  it("caps the bulk confirm text and the bulk POST at 50 when more than 50 are pending", async () => {
    const many = {
      learnings: Array.from({ length: 60 }, (_, i) => ({
        id: i + 1,
        day: "2026-09-01",
        content: `sentence number ${i}`,
        kind: "fact",
        source: "Slack",
        evidence: "",
        said_by: null,
        status: "pending",
        decided_by: null,
        created_at: "2026-09-01T05:00:00",
      })),
    };
    stubFetch({ "/learnings": many });
    renderWithClient(LearningsPanel, {
      agentName: "wren",
      canDecide: true,
      frozen: false,
    });

    await screen.findByText("sentence number 0");
    await fireEvent.click(screen.getByRole("button", { name: "Remember all" }));

    expect(screen.getByText("Remember the first 50 of 60?")).not.toBeNull();

    await fireEvent.click(screen.getByRole("button", { name: "Yes, remember" }));

    await vi.waitFor(() => {
      const call = vi
        .mocked(fetch)
        .mock.calls.find(([url]) => String(url).includes("/learnings/bulk"));
      expect(call).toBeDefined();
      const init = call?.[1] as RequestInit | undefined;
      const body = JSON.parse(String(init?.body));
      expect(body.action).toBe("approve");
      expect(body.ids).toEqual(Array.from({ length: 50 }, (_, i) => i + 1));
    });
  });

  it("history toggle counts only denied rows as Skipped, and adds Expired when any exist", async () => {
    stubFetch({
      "/learnings": {
        learnings: [
          {
            id: 1, day: "d", content: "approved one", kind: "fact", source: "s",
            evidence: "", said_by: null, status: "approved", decided_by: "kyle",
            created_at: "2026-09-01T05:00:00",
          },
          {
            id: 2, day: "d", content: "denied one", kind: "fact", source: "s",
            evidence: "", said_by: null, status: "denied", decided_by: "kyle",
            created_at: "2026-09-01T05:00:00",
          },
          {
            id: 3, day: "d", content: "expired one", kind: "fact", source: "s",
            evidence: "", said_by: null, status: "expired", decided_by: null,
            created_at: "2026-09-01T05:00:00",
          },
        ],
      },
    });
    renderWithClient(LearningsPanel, { agentName: "wren", canDecide: true, frozen: false });

    expect(
      await screen.findByRole("button", { name: "Remembered 1 · Skipped 1 · Expired 1" }),
    ).not.toBeNull();
  });

});
