// @vitest-environment jsdom
//
// Access-control Task A3: the dashboard grid card shows who made the agent.
import { screen } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import AgentCard from "./AgentCard.svelte";
import type { AgentSummary } from "../lib/queries";
import { renderWithClient, stubFetch } from "../test-utils/render";

// AgentCard mounts `observabilityQuery` (Task 6, observability-catalog
// addendum) unconditionally now, so every render here needs a QueryClient in
// context plus a stubbed `/api/catalog/observability` — same harness
// AgentDetail.test.ts already uses. The catch-all `/api/` fixture resolves
// to `{}`, i.e. `{url: undefined, project: undefined}`, so the
// Observability link never renders in these tests (neither is a string) —
// exactly the "config not both set" case, and none of the assertions below
// are about that link.
beforeEach(() => {
  vi.unstubAllGlobals();
  stubFetch({ "/api/": {} });
});

function render(component: typeof AgentCard, props: { agent: AgentSummary }) {
  return renderWithClient(component, props);
}

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
