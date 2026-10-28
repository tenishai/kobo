// @vitest-environment jsdom
//
// The thread surface (threads arc, Task 10): starting a shared thread from
// an agent's own chat context, adding participants (a plain picker of
// visible agents, filtered client-side to drop whoever's already seated or
// not ready), and driving turns with replies attributed by name.
import { fireEvent, screen, waitFor } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ThreadPanel from "./ThreadPanel.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";
import { toasts } from "../lib/toast.svelte";

const AGENTS_LIST = {
  agents: [
    {
      name: "ward",
      status: "ready",
      model: "m",
      model_label: "M",
      created_at: "2026-08-01T00:00:00Z",
      has_avatar: false,
      created_by: null,
      group_name: null,
    },
    {
      name: "clio",
      status: "ready",
      model: "m",
      model_label: "M",
      created_at: "2026-08-01T00:00:00Z",
      has_avatar: false,
      created_by: null,
      group_name: null,
    },
    {
      name: "not-ready-bot",
      status: "provisioning",
      model: "m",
      model_label: "M",
      created_at: "2026-08-01T00:00:00Z",
      has_avatar: false,
      created_by: null,
      group_name: null,
    },
  ],
  groups_unavailable: false,
};

const FRESH_THREAD = {
  id: 7,
  initiator_email: "me@example.com",
  created_at: "2026-08-27T00:00:00+00:00",
  participants: ["ward"],
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

async function startThread(): Promise<void> {
  await fireEvent.click(await screen.findByRole("button", { name: "Start a shared thread" }));
  await screen.findByLabelText("Message the thread");
}

describe("ThreadPanel", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    toasts.splice(0, toasts.length);
  });

  it("shows the Start affordance and no composer before any thread exists", async () => {
    stubFetch({ "/api/agents": AGENTS_LIST, "/api/": {} });
    renderWithClient(ThreadPanel, { agentName: "ward" });

    expect(await screen.findByRole("button", { name: "Start a shared thread" })).not.toBeNull();
    expect(screen.queryByLabelText("Message the thread")).toBeNull();
  });

  it("starting a thread POSTs {agent} to /api/threads and seats the viewed agent", async () => {
    stubFetch({
      "/api/threads": FRESH_THREAD,
      "/api/agents": AGENTS_LIST,
      "/api/": {},
    });
    renderWithClient(ThreadPanel, { agentName: "ward" });

    await startThread();

    expect(screen.getByText("ward")).not.toBeNull();
    const call = vi
      .mocked(fetch)
      .mock.calls.find(
        ([url, init]) =>
          String(url) === "/api/threads" && (init as RequestInit | undefined)?.method === "POST",
      );
    expect(call).toBeDefined();
    expect(JSON.parse((call as [RequestInfo | URL, RequestInit])[1].body as string)).toEqual({
      agent: "ward",
    });
  });

  it("the Add-participant picker excludes the already-seated agent and any not-ready agent", async () => {
    stubFetch({
      "/api/threads": FRESH_THREAD,
      "/api/agents": AGENTS_LIST,
      "/api/": {},
    });
    renderWithClient(ThreadPanel, { agentName: "ward" });
    await startThread();

    const picker = (await screen.findByRole("combobox")) as HTMLSelectElement;
    const optionValues = Array.from(picker.options).map((option) => option.value);
    expect(optionValues).toEqual(["clio"]);
  });

  it("adding a participant POSTs {agent} to /api/threads/{id}/participants and updates the seated list", async () => {
    // The more specific route is listed first: stubFetch matches by plain
    // substring, and "/api/threads" is itself a substring of "/api/threads/
    // 7/participants" -- listing the longer route first is what makes the
    // add-participant call resolve to its own stub instead of falling
    // through to the create-thread one.
    stubFetch({
      "/api/threads/7/participants": {
        ...FRESH_THREAD,
        participants: ["ward", "clio"],
      },
      "/api/threads": FRESH_THREAD,
      "/api/agents": AGENTS_LIST,
      "/api/": {},
    });
    renderWithClient(ThreadPanel, { agentName: "ward" });
    await startThread();

    await screen.findByRole("combobox");
    await fireEvent.click(screen.getByRole("button", { name: "Add" }));

    expect(await screen.findByText("clio")).not.toBeNull();
    const call = vi
      .mocked(fetch)
      .mock.calls.find(([url, init]) => {
        const u = String(url);
        return (
          u === "/api/threads/7/participants" &&
          (init as RequestInit | undefined)?.method === "POST"
        );
      });
    expect(call).toBeDefined();
    expect(JSON.parse((call as [RequestInfo | URL, RequestInit])[1].body as string)).toEqual({
      agent: "clio",
    });
  });

  it("a refused add-participant surfaces as a toast, never an inline error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url === "/api/agents") {
          return jsonResponse(AGENTS_LIST);
        }
        if (url === "/api/threads" && init?.method === "POST") {
          return jsonResponse(FRESH_THREAD);
        }
        if (url === "/api/threads/7/participants") {
          return jsonResponse(
            {
              error:
                "clio and ward are not connected. Accept a colleague invite between them first.",
            },
            422,
          );
        }
        return jsonResponse({});
      }),
    );

    renderWithClient(ThreadPanel, { agentName: "ward" });
    await startThread();
    await screen.findByRole("combobox");
    await fireEvent.click(screen.getByRole("button", { name: "Add" }));

    await waitFor(() => {
      expect(toasts.some((t) => t.kind === "error")).toBe(true);
    });
    expect(
      toasts.some((t) =>
        (t.body ?? "").includes("not connected"),
      ),
    ).toBe(true);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("sending a turn POSTs {messages} to /api/threads/{id}/turns and renders the reply attributed by agent name", async () => {
    stubFetch({
      "/api/threads/7/turns": {
        replies: [{ agent: "ward", text: "Here's my take.", error: null, consults: [] }],
        to: ["ward"],
      },
      "/api/threads": FRESH_THREAD,
      "/api/agents": AGENTS_LIST,
      "/api/": {},
    });
    renderWithClient(ThreadPanel, { agentName: "ward" });
    await startThread();

    const textarea = screen.getByLabelText("Message the thread");
    await fireEvent.input(textarea, { target: { value: "What do you all think?" } });
    await fireEvent.click(screen.getByRole("button", { name: "Send to ward" }));

    expect(await screen.findByText("Here's my take.")).not.toBeNull();
    expect(screen.getByText("ward", { selector: ".thread-bubble__attribution" })).not.toBeNull();

    const call = vi
      .mocked(fetch)
      .mock.calls.find(([url]) => String(url) === "/api/threads/7/turns");
    expect(call).toBeDefined();
    expect(JSON.parse((call as [RequestInfo | URL, RequestInit])[1].body as string)).toEqual({
      messages: [{ role: "user", text: "What do you all think?" }],
      to: "ward",
      broadcast: false,
    });
  });

  it("a second turn resends the full transcript, including the prior reply", async () => {
    const turnCalls: unknown[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url === "/api/agents") {
          return jsonResponse(AGENTS_LIST);
        }
        if (url === "/api/threads" && init?.method === "POST") {
          return jsonResponse(FRESH_THREAD);
        }
        if (url === "/api/threads/7/turns") {
          turnCalls.push(init?.body ? JSON.parse(init.body as string) : null);
          const turnNumber = turnCalls.length;
          return jsonResponse({
            replies: [{ agent: "ward", text: `reply ${turnNumber}`, error: null, consults: [] }],
            to: ["ward"],
          });
        }
        return jsonResponse({});
      }),
    );

    renderWithClient(ThreadPanel, { agentName: "ward" });
    await startThread();

    const textarea = screen.getByLabelText("Message the thread");
    await fireEvent.input(textarea, { target: { value: "first ask" } });
    await fireEvent.click(screen.getByRole("button", { name: "Send to ward" }));
    await screen.findByText("reply 1");

    await fireEvent.input(textarea, { target: { value: "second ask" } });
    await fireEvent.click(screen.getByRole("button", { name: "Send to ward" }));
    await screen.findByText("reply 2");

    expect(turnCalls).toHaveLength(2);
    expect(turnCalls[1]).toEqual({
      messages: [
        { role: "user", text: "first ask" },
        { role: "agent", text: "reply 1" },
        { role: "user", text: "second ask" },
      ],
      to: "ward",
      broadcast: false,
    });
  });

  it("with two seated, Send goes to the primary, Ask everyone broadcasts, and @name addresses one turn", async () => {
    const turnCalls: Array<{ to: string | null; broadcast: boolean }> = [];
    const seated = { ...FRESH_THREAD, participants: ["ward", "clio"] };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url === "/api/agents") {
          return jsonResponse(AGENTS_LIST);
        }
        if (url === "/api/threads" && init?.method === "POST") {
          return jsonResponse(seated);
        }
        if (url === "/api/threads/7/turns") {
          const body = JSON.parse(init?.body as string);
          turnCalls.push({ to: body.to, broadcast: body.broadcast });
          return jsonResponse({
            replies: [
              { agent: body.to ?? "ward", text: `ok ${turnCalls.length}`, error: null, consults: [] },
            ],
            to: [body.to ?? "ward"],
          });
        }
        return jsonResponse({});
      }),
    );
    renderWithClient(ThreadPanel, { agentName: "ward" });
    await startThread();
    const textarea = screen.getByLabelText("Message the thread");

    await fireEvent.input(textarea, { target: { value: "plain" } });
    await fireEvent.click(screen.getByRole("button", { name: "Send to ward" }));
    await screen.findByText("ok 1");

    await fireEvent.input(textarea, { target: { value: "everyone?" } });
    await fireEvent.click(screen.getByRole("button", { name: "Ask everyone" }));
    await screen.findByText("ok 2");

    await fireEvent.input(textarea, { target: { value: "@clio just you" } });
    await fireEvent.click(screen.getByRole("button", { name: "Send to ward" }));
    await screen.findByText("ok 3");

    expect(turnCalls).toEqual([
      { to: "ward", broadcast: false },
      { to: null, broadcast: true },
      { to: "clio", broadcast: false },
    ]);
    expect(screen.getByText("just you")).not.toBeNull();
  });

  it("an agent that did not answer is a muted entry, and consults render under the reply", async () => {
    stubFetch({
      "/api/threads/7/turns": {
        replies: [
          {
            agent: "ward",
            text: "All green.",
            error: null,
            consults: [
              {
                ts: "1",
                conversation_id: "c1",
                from: "ward",
                to: "clio",
                role: "ask",
                text: "clio, cluster ok?",
              },
              { ts: "2", conversation_id: "c1", from: "clio", to: "ward", role: "reply", text: "yes" },
            ],
          },
          { agent: "clio", text: null, error: "didn't answer", consults: [] },
        ],
        to: ["ward", "clio"],
      },
      "/api/threads": FRESH_THREAD,
      "/api/agents": AGENTS_LIST,
      "/api/": {},
    });
    renderWithClient(ThreadPanel, { agentName: "ward" });
    await startThread();
    await fireEvent.input(screen.getByLabelText("Message the thread"), { target: { value: "hi" } });
    await fireEvent.click(screen.getByRole("button", { name: "Send to ward" }));

    expect(await screen.findByText("All green.")).not.toBeNull();
    expect(screen.getByText("didn't answer")).not.toBeNull();
    expect(screen.getByText("clio, cluster ok?", { exact: false })).not.toBeNull();
    expect(screen.getByText("asked clio")).not.toBeNull();
    expect(screen.getByText("clio replied")).not.toBeNull();
  });

  it("a 422 turn failure pulls the poisoned message back into the input, same recovery as chat", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url === "/api/agents") {
          return jsonResponse(AGENTS_LIST);
        }
        if (url === "/api/threads" && init?.method === "POST") {
          return jsonResponse(FRESH_THREAD);
        }
        if (url === "/api/threads/7/turns") {
          return jsonResponse({ error: "message text must not be empty" }, 422);
        }
        return jsonResponse({});
      }),
    );

    renderWithClient(ThreadPanel, { agentName: "ward" });
    await startThread();

    const textarea = screen.getByLabelText("Message the thread") as HTMLTextAreaElement;
    await fireEvent.input(textarea, { target: { value: "a poisoned ask" } });
    await fireEvent.click(screen.getByRole("button", { name: "Send to ward" }));

    await waitFor(() => {
      expect(screen.getByText("message text must not be empty")).not.toBeNull();
    });
    expect(textarea.value).toBe("a poisoned ask");
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
  });

  it("disables Start and the composer while frozen", async () => {
    stubFetch({
      "/api/threads": FRESH_THREAD,
      "/api/agents": AGENTS_LIST,
      "/api/": {},
    });
    const { rerender } = renderWithClient(ThreadPanel, { agentName: "ward", frozen: false });

    await startThread();
    await rerender({ componentProps: { agentName: "ward", frozen: true } });

    expect(
      (screen.getByRole("button", { name: "Send to ward" }) as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(
      (screen.getByLabelText("Message the thread") as HTMLTextAreaElement).disabled,
    ).toBe(true);
  });
});
