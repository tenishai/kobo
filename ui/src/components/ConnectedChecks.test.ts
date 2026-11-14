// @vitest-environment jsdom
import { screen, waitFor } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ConnectedChecks from "./ConnectedChecks.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

describe("ConnectedChecks", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("says each line as the server wrote it, with the pull request it waits on and no control", async () => {
    stubFetch({
      "/api/agents/clio/checks": {
        checks: [
          { id: "running", state: "ok", line: "Running.", at: null, pull_request: null },
          {
            id: "gateway",
            state: "waiting",
            line: "Waiting on your team to merge the room's setup pull request.",
            at: null,
            pull_request: "https://github.test/acme/clio/pull/7",
          },
          {
            id: "slack",
            state: "missing",
            line: "It refused the last message from Slack. Its code has to take the messages the room hands it.",
            at: new Date(Date.now() - 5 * 60_000).toISOString(),
            pull_request: null,
          },
        ],
      },
    });

    renderWithClient(ConnectedChecks, { agentName: "clio" });

    await waitFor(() => expect(screen.getByText("Running.")).not.toBeNull());
    expect(screen.getByText("Deployed")).not.toBeNull();
    expect(screen.getByText("Tools")).not.toBeNull();
    expect(screen.getByRole("link", { name: "Open the pull request" }).getAttribute("href")).toBe(
      "https://github.test/acme/clio/pull/7",
    );
    expect(screen.getByText(/It refused the last message from Slack/)).not.toBeNull();
    expect(screen.getByText("5m ago")).not.toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("draws nothing for an agent with nothing to say", async () => {
    stubFetch({ "/api/agents/clio/checks": { checks: [] } });

    renderWithClient(ConnectedChecks, { agentName: "clio" });

    await waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(screen.queryByRole("list")).toBeNull();
  });
});
