// @vitest-environment jsdom
//
// "Teach this" gesture (Task 2): a quiet per-bubble action that opens
// TeachModal pre-filled with the exchange pair, edits post as `{text}` to
// Task 1's teach route, and the whole thing respects the frozen state
// ChatPanel already honors for everything else.
import { fireEvent, screen, waitFor } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ChatPanel from "./ChatPanel.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

const SAVED_COPY = "Saved to memory. It arrives with the next delivery.";
const INSTANT_COPY = "Learned. Filed to the repo in the background.";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

async function sendMessage(text: string): Promise<void> {
  const input = screen.getByLabelText("Your message");
  await fireEvent.input(input, { target: { value: text } });
  await fireEvent.click(screen.getByRole("button", { name: "Send" }));
}

describe("ChatPanel — Teach this gesture (Task 2)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders no Teach affordance before any message exists", () => {
    stubFetch({ "/api/": {} });
    renderWithClient(ChatPanel, { agentName: "ward" });
    expect(screen.queryAllByRole("button", { name: "Teach this" })).toHaveLength(0);
  });

  it("renders a quiet Teach this action on every bubble, both roles", async () => {
    stubFetch({
      "/api/agents/ward/chat": { reply: "Sure thing." },
      "/api/": {},
    });
    renderWithClient(ChatPanel, { agentName: "ward" });

    await sendMessage("hello there");
    await waitFor(() => {
      expect(screen.getByText("Sure thing.")).not.toBeNull();
    });

    expect(screen.getAllByRole("button", { name: "Teach this" })).toHaveLength(2);
  });

  it("hides the affordance entirely while frozen, even with bubbles already present", async () => {
    stubFetch({
      "/api/agents/ward/chat": { reply: "Sure thing." },
      "/api/": {},
    });
    const { rerender } = renderWithClient(ChatPanel, { agentName: "ward", frozen: false });

    await sendMessage("hello there");
    await waitFor(() => {
      expect(screen.getByText("Sure thing.")).not.toBeNull();
    });
    expect(screen.getAllByRole("button", { name: "Teach this" })).toHaveLength(2);

    await rerender({ componentProps: { agentName: "ward", frozen: true } });

    expect(screen.queryAllByRole("button", { name: "Teach this" })).toHaveLength(0);
  });

  it("user-then-agent pairing: Teach on the user bubble prefills both sides, agent name included", async () => {
    stubFetch({
      "/api/agents/ward/chat": { reply: "Sure thing." },
      "/api/": {},
    });
    renderWithClient(ChatPanel, { agentName: "ward" });

    await sendMessage("hello there");
    await waitFor(() => {
      expect(screen.getByText("Sure thing.")).not.toBeNull();
    });

    const [userTeach] = screen.getAllByRole("button", { name: "Teach this" });
    await fireEvent.click(userTeach);

    // The bubble's own words only: the reply is not folded into the note.
    const textarea = (await screen.findByLabelText("Teach ward")) as HTMLTextAreaElement;
    expect(textarea.value).toBe("hello there");
  });

  it("lone message: Teach on a message with no adjacent counterpart yet prefills just itself", async () => {
    let resolveChat: (value: Response) => void = () => {};
    const chatPromise = new Promise<Response>((resolve) => {
      resolveChat = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/agents/ward/chat")) {
          return chatPromise;
        }
        return jsonResponse({});
      }),
    );

    renderWithClient(ChatPanel, { agentName: "ward" });
    await sendMessage("hello there");

    // While the reply is still pending, the only real transcript entry is
    // the lone user message (the "thinking…" bubble is not a real entry and
    // carries no Teach affordance of its own).
    const teachButtons = await screen.findAllByRole("button", { name: "Teach this" });
    expect(teachButtons).toHaveLength(1);

    await fireEvent.click(teachButtons[0]);
    const textarea = (await screen.findByLabelText("Teach ward")) as HTMLTextAreaElement;
    expect(textarea.value).toBe("hello there");

    resolveChat(jsonResponse({ reply: "ok" }));
  });

  it("Save posts the EDITED textarea content, not the original prefill", async () => {
    const teachCalls: unknown[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/agents/ward/chat")) {
          return jsonResponse({ reply: "Sure thing." });
        }
        if (url.includes("/api/agents/ward/teach")) {
          teachCalls.push(init?.body ? JSON.parse(init.body as string) : null);
          return jsonResponse({ filename: "chat-x.md", sha: "abc1234" });
        }
        return jsonResponse({});
      }),
    );

    renderWithClient(ChatPanel, { agentName: "ward" });
    await sendMessage("hello there");
    await waitFor(() => {
      expect(screen.getByText("Sure thing.")).not.toBeNull();
    });

    await fireEvent.click(screen.getAllByRole("button", { name: "Teach this" })[0]);
    const textarea = (await screen.findByLabelText("Teach ward")) as HTMLTextAreaElement;
    await fireEvent.input(textarea, { target: { value: "always answer in one paragraph" } });

    await fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(teachCalls).toHaveLength(1);
    });
    expect(teachCalls[0]).toEqual({ text: "always answer in one paragraph" });
  });

  it("success shows the exact confirmation copy plus a Done affordance that closes it", async () => {
    stubFetch({
      "/api/agents/ward/chat": { reply: "Sure thing." },
      "/api/agents/ward/teach": { filename: "chat-x.md", sha: "abc1234" },
      "/api/": {},
    });
    renderWithClient(ChatPanel, { agentName: "ward" });
    await sendMessage("hello there");
    await waitFor(() => {
      expect(screen.getByText("Sure thing.")).not.toBeNull();
    });

    await fireEvent.click(screen.getAllByRole("button", { name: "Teach this" })[0]);
    await screen.findByLabelText("Teach ward");
    await fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(screen.getByText(SAVED_COPY)).not.toBeNull();
      expect(screen.getByText("Saved as intake/chat-x.md")).not.toBeNull();
    });
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();

    await fireEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(screen.queryByText(SAVED_COPY)).toBeNull();
  });

  it("resets fully between opens: an unsaved edit on one bubble never leaks into another", async () => {
    stubFetch({
      "/api/agents/ward/chat": { reply: "Sure thing." },
      "/api/": {},
    });
    renderWithClient(ChatPanel, { agentName: "ward" });
    await sendMessage("hello there");
    await waitFor(() => {
      expect(screen.getByText("Sure thing.")).not.toBeNull();
    });

    const [userTeach, agentTeach] = screen.getAllByRole("button", { name: "Teach this" });

    await fireEvent.click(userTeach);
    let textarea = (await screen.findByLabelText("Teach ward")) as HTMLTextAreaElement;
    await fireEvent.input(textarea, { target: { value: "some half-written draft" } });
    await fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByLabelText("Teach ward")).toBeNull();

    await fireEvent.click(agentTeach);
    textarea = (await screen.findByLabelText("Teach ward")) as HTMLTextAreaElement;
    expect(textarea.value).toBe("Sure thing.");
  });

});
