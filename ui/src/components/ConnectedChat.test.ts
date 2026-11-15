// @vitest-environment jsdom
import { screen, waitFor } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ConnectedChat from "./ConnectedChat.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

const line = (state: string, text: string) => ({
  checks: [
    { id: "running", state: "ok", line: "Running.", at: null, pull_request: null },
    { id: "chat", state, line: text, at: null, pull_request: null },
  ],
});

describe("ConnectedChat", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("says what is missing, in the server's words, and offers no box that could only fail", async () => {
    stubFetch({
      "/api/agents/clio/checks": line(
        "idle",
        "Its code does not answer chat yet. The guide says what it has to serve, under Running your own code.",
      ),
    });

    renderWithClient(ConnectedChat, { agentName: "clio" });

    await waitFor(() => expect(screen.getByText(/Its code does not answer chat yet/)).not.toBeNull());
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("is the same chat box once the agent answers, without Teach this", async () => {
    stubFetch({ "/api/agents/clio/checks": line("ok", "Answering chat.") });

    renderWithClient(ConnectedChat, { agentName: "clio" });

    expect(await screen.findByRole("textbox", { name: "Your message" })).not.toBeNull();
    expect(screen.queryByText("Answering chat.")).toBeNull();
    expect(screen.queryByRole("button", { name: "Teach this" })).toBeNull();
  });

  it("claims nothing when the room could not read where the agent stands", async () => {
    stubFetch({ "/api/agents/clio/checks": { checks: [] } });

    renderWithClient(ConnectedChat, { agentName: "clio" });

    await waitFor(() =>
      expect(screen.getByText("The room could not check whether chat reaches this agent just now.")).not.toBeNull(),
    );
    expect(screen.queryByRole("textbox")).toBeNull();
  });
});
