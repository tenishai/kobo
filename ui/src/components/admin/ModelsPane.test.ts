// @vitest-environment jsdom
// Admin > Models (kobo#405): one section per connected provider with its tag and
// what the cloud lists, a tick on a model is one PUT with that change, and a
// model nobody ticked reads as hidden from New agent.
import { fireEvent, screen, within } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";

import ModelsPane from "./ModelsPane.svelte";
import { renderWithClient } from "../../test-utils/render";

const VIEW = {
  gateway: true,
  providers: [
    {
      provider: "vertex_ai", tag: "GCP", source: "gcp-example", name: "Google Cloud example", region_or_location: "us-east5",
      discovered: [
        { id: "vertex_ai/claude-sonnet-4-6", model: "claude-sonnet-4-6", label: "Claude Sonnet 4.6", publisher: "anthropic", via: null, stage: "GA", family: "claude", tier: null, source: "gcp-example", enabled: false, discovered: true },
        { id: "vertex_ai/claude-sonnet-4-5", model: "claude-sonnet-4-5", label: "Claude Affordable Sonnet", publisher: null, via: null, stage: null, family: "Claude", tier: "balanced", source: "gcp-example", enabled: true, discovered: false },
      ],
      offered: ["vertex_ai/claude-sonnet-4-5"],
      error: null,
      fetched_at: "2026-11-02T12:00:00Z",
    },
  ],
  not_connected: [{ provider: "bedrock", tag: "AWS" }, { provider: "azure", tag: "Azure" }, { provider: "self-hosted", tag: "Self-hosted" }],
  org_list: [{ id: "vertex_ai/claude-sonnet-4-5", label: "Claude Affordable Sonnet", tier: "balanced", family: "Claude" }],
  empty_state: "Connect a cloud or a self-hosted endpoint under Integrations; its models appear here.",
};

function stub(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/admin/models") && init?.method === "PUT") {
        return new Response(JSON.stringify({ ...VIEW, changed: ["vertex_ai/claude-sonnet-4-6"], gateway_result: "1 added, 0 removed, 1 kept" }), {
          status: 200, headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/api/admin/models")) {
        return new Response(JSON.stringify(VIEW), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      throw new Error(`unstubbed fetch: ${init?.method ?? "GET"} ${url}`);
    }),
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("ModelsPane", () => {
  it("lists the provider's models with their tick and offers one with a single PUT", async () => {
    stub();
    renderWithClient(ModelsPane);

    const section = await screen.findByRole("article", { name: "GCP Google Cloud example" });
    expect(within(section).getByText("2 discovered, 1 offered")).not.toBeNull();
    expect(within(section).getByText("Hidden from New agent")).not.toBeNull();
    expect(within(section).getByText("Offered")).not.toBeNull();
    expect(screen.getByText(/Not connected: AWS, Azure, Self-hosted/)).not.toBeNull();

    await fireEvent.click(within(section).getByRole("checkbox", { name: "Offer Claude Sonnet 4.6" }));

    await vi.waitFor(() => {
      const call = vi.mocked(fetch).mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "PUT");
      expect(call).toBeDefined();
      expect(String(call![0])).toBe("/api/admin/models");
      expect(JSON.parse(String((call![1] as RequestInit).body))).toEqual({ changes: [{ id: "vertex_ai/claude-sonnet-4-6", enabled: true }] });
    });
  });
});
