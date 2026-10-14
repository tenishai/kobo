// @vitest-environment jsdom
//
// Access-control Task A3: the dashboard grid card shows who made the agent.
// Card wave 2026-09-23: the same facts, in four rows that line up card to
// card -- identity, one line about the agent, tags, and a muted footer.
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
// are about that link unless they say so.
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
  kind: "composed",
};

describe("AgentCard — connected marker (connected-agents design 2026-09-21)", () => {
  it("renders no marker for a composed agent", () => {
    render(AgentCard, { agent: BASE_AGENT });

    expect(screen.queryByText("connected")).toBeNull();
  });

  it("marks a connected agent, beside its name", () => {
    const { container } = render(AgentCard, {
      agent: { ...BASE_AGENT, kind: "connected" },
    });

    expect(screen.getByText("connected")).not.toBeNull();
    expect(container.querySelector(".agent-card__kind")).not.toBeNull();
  });
});

describe("AgentCard — the facts a card carries (card wave 2026-09-23)", () => {
  it("opens the agent from anywhere on the card", () => {
    const { container } = render(AgentCard, { agent: BASE_AGENT });

    const link = container.querySelector(".agent-card__stretched-link");
    expect(link?.getAttribute("href")).toBe("#/agents/warden");
    expect(link?.getAttribute("aria-label")).toBe("Open warden");
  });

});

describe("AgentCard — a failed agent (card wave 2026-09-23)", () => {
  it("replaces the footer facts with the failure, and says where to look", () => {
    const { container } = render(AgentCard, {
      agent: { ...BASE_AGENT, status: "failed", created_by: "owner@example.com" },
    });

    expect(container.querySelector(".agent-card__error")).not.toBeNull();
    expect(screen.getByText(/Open for details/)).not.toBeNull();
    // The meta run is gone -- one failure reads louder than three facts.
    expect(container.querySelector(".agent-card__meta")).toBeNull();
    expect(screen.queryByText("Made by owner@example.com")).toBeNull();
  });

  it("says what a waiting agent waits on, not just that it waits", () => {
    const { container } = render(AgentCard, {
      agent: { ...BASE_AGENT, status: "waiting", created_by: "owner@example.com" },
    });

    expect(container.querySelector(".agent-card__waiting")).not.toBeNull();
    expect(screen.getByText("Waiting on a pull request.")).not.toBeNull();
    expect(screen.getByText(/Open for details/)).not.toBeNull();
    expect(container.querySelector(".agent-card__error")).toBeNull();
    expect(container.querySelector(".agent-card__meta")).toBeNull();
  });

  it("flags a ready agent with no purpose tags (issue #147)", () => {
    render(AgentCard, { agent: { ...BASE_AGENT, purpose_tags: [] } });
    expect(screen.getByText("no purpose tags")).toBeTruthy();
  });

  it("does not flag a tagged, a provisioning or a connected agent", () => {
    render(AgentCard, { agent: { ...BASE_AGENT, purpose_tags: ["billing"] } });
    render(AgentCard, { agent: { ...BASE_AGENT, name: "p", status: "provisioning" } });
    render(AgentCard, { agent: { ...BASE_AGENT, name: "c", kind: "connected" } });
    expect(screen.queryByText("no purpose tags")).toBeNull();
  });

  it("tags an agent whose nightly learnings pass keeps failing", () => {
    render(AgentCard, { agent: { ...BASE_AGENT, system_tasks_failing: ["learnings"] } });
    expect(screen.getByText("learnings failing")).not.toBeNull();
  });

  it("keeps the footer facts for an agent that is fine", () => {
    const { container } = render(AgentCard, { agent: BASE_AGENT });

    expect(container.querySelector(".agent-card__error")).toBeNull();
    expect(container.querySelector(".agent-card__meta")).not.toBeNull();
  });
});

describe("AgentCard — the observability link (card wave 2026-09-23)", () => {
  it("is an icon with a hidden label, not a coloured text link", async () => {
    vi.unstubAllGlobals();
    stubFetch({
      "/api/catalog/observability": {
        url: "https://noto.example.com",
        project: "agents",
      },
      "/api/": {},
    });
    const { container } = render(AgentCard, { agent: BASE_AGENT });

    const link = await screen.findByTitle("Observability");
    expect(link.getAttribute("href")).toBe(
      "https://noto.example.com/projects/agents/dashboards/warden-agent-health",
    );
    expect(link.querySelector("svg")).not.toBeNull();
    // The label is visible text, not screen-reader-only: an icon alone did
    // not read as a link to the operator.
    const label = container.querySelector(
      ".agent-card__observability .agent-card__observability-label",
    );
    expect(label?.textContent).toBe("Observability");
    expect(container.querySelector(".agent-card__observability .agent-card__sr")).toBeNull();
  });
});
