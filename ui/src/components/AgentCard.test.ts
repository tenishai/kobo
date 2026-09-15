// @vitest-environment jsdom
//
// Access-control Task A3: the dashboard grid card shows who made the agent.
import { render, screen } from "@testing-library/svelte";
import { describe, expect, it } from "vitest";
import AgentCard from "./AgentCard.svelte";
import type { AgentSummary } from "../lib/queries";

const BASE_AGENT: AgentSummary = {
  name: "warden",
  status: "ready",
  model: "m",
  model_label: "M",
  created_at: "2026-08-17T00:00:00Z",
  has_avatar: false,
  created_by: null,
  group_name: null,
  archetype: null,
  purpose_tags: [],
};

describe("AgentCard — owner line (Task A3)", () => {
  it("renders no owner line when created_by is null", () => {
    render(AgentCard, { agent: BASE_AGENT });

    expect(screen.queryByText(/Made by/)).toBeNull();
  });

  it("shows 'Made by <email>' when created_by is present", () => {
    render(AgentCard, {
      agent: { ...BASE_AGENT, created_by: "owner@example.com" },
    });

    expect(screen.getByText("Made by owner@example.com")).not.toBeNull();
  });
});
