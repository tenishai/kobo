// @vitest-environment jsdom
import { screen } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ToolingPanel from "./ToolingPanel.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

describe("ToolingPanel", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    // The tooling query needs a real, correctly-shaped payload.
    stubFetch({
      "/api/agents/warden/tooling": { files: [], enforcement: null },
      "/api/": {},
    });
  });

  it("explains the built-in tool lanes near the top of the panel", () => {
    renderWithClient(ToolingPanel, { agentName: "warden", frozen: false });

    expect(
      screen.getByText(
        /Every agent ships with colleague tools.*list and consult the room.*share a fact with quorum review.*repo tools stay dormant until an operator approves a repository.*Tools added here are extras on top of that baseline/s,
      ),
    ).not.toBeNull();
  });
});
