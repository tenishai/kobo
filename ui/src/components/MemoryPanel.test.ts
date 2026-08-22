// @vitest-environment jsdom
import { screen } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import MemoryPanel from "./MemoryPanel.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

describe("MemoryPanel", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    // Both of this panel's own queries (knowledge, intake) need a real,
    // correctly-shaped payload or the panel throws reading `.length` before
    // it ever renders anything — same reasoning as FrozenPanels.test.ts's
    // stub set for this component.
    stubFetch({
      "/api/agents/warden/knowledge": { files: [] },
      "/api/agents/warden/intake": { files: [] },
      "/api/": {},
    });
  });

  it("renders the how-learning-works explainer near the top of the panel", () => {
    // Retention-confusion fix: a short, honest explainer so "is Teach this
    // the only way? does the agent not learn on its own?" has an answer
    // right there in the panel, in its own plain voice.
    renderWithClient(MemoryPanel, { agentName: "warden", frozen: false });

    expect(
      screen.getByText(
        /Your agent remembers what you teach it.*It also keeps what colleague agents tell it.*It does not yet learn on its own from ordinary conversation\./s,
      ),
    ).not.toBeNull();
  });
});
