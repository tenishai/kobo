// @vitest-environment jsdom
// Admin > Models (kobo#405): the pane opens on what the room holds and asks each
// cloud it is owed a list from on its own; the offered models are first, one row
// per model with a tick under each cloud that has it; a tick is one PUT with that
// change and asks no cloud again.
import { fireEvent, screen, within } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";

import ModelsPane from "./ModelsPane.svelte";
import { renderWithClient } from "../../test-utils/render";

const GCP = {
  provider: "vertex_ai", tag: "GCP", source: "gcp-example", name: "Google Cloud example", region_or_location: "us-east5",
  discovered: [
    { id: "vertex_ai/claude-sonnet-4-5", model: "claude-sonnet-4-5", label: "Claude Affordable Sonnet", name: "Claude Sonnet 4.5", publisher: "Anthropic", via: null, stage: "GA", family: "claude", tier: "balanced", source: "gcp-example", enabled: true, discovered: true },
    { id: "vertex_ai/gemini-2.5-pro", model: "gemini-2.5-pro", label: "Gemini 2.5 Pro", name: "Gemini 2.5 Pro", publisher: "Google", via: null, stage: "GA", family: "gemini", tier: null, source: "gcp-example", enabled: false, discovered: true },
  ],
  offered: ["vertex_ai/claude-sonnet-4-5"],
  error: null,
  fetched_at: "2026-11-02T12:00:00Z",
  pending: false,
};
const AWS = {
  provider: "bedrock", tag: "AWS", source: "aws-example", name: "AWS example", region_or_location: "us-east-1",
  discovered: [], offered: [], error: null, fetched_at: null, pending: true,
};
const AWS_LISTED = {
  ...AWS,
  discovered: [
    { id: "bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0", model: "us.anthropic.claude-sonnet-4-5-20250929-v1:0", label: "Claude Sonnet 4.5", name: "Claude Sonnet 4.5", publisher: "Anthropic", via: "inference-profile", stage: "GA", family: "claude", tier: null, source: "aws-example", enabled: false, discovered: true },
  ],
  fetched_at: "2026-11-02T12:00:05Z",
  pending: false,
};
const REST = {
  gateway: true,
  not_connected: [{ provider: "azure", tag: "Azure" }, { provider: "self-hosted", tag: "Self-hosted" }],
  org_list: [{ id: "vertex_ai/claude-sonnet-4-5", label: "Claude Affordable Sonnet", tier: "balanced", family: "Claude" }],
  empty_state: "Connect a cloud or a self-hosted endpoint under Integrations; its models appear here.",
};

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
}

function stub(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/admin/models" && init?.method === "PUT") {
        return json({ ...REST, providers: [GCP, AWS_LISTED], changed: ["vertex_ai/gemini-2.5-pro"], gateway_result: "1 added, 0 removed, 1 kept" });
      }
      if (url === "/api/admin/models?listing=held") return json({ ...REST, providers: [GCP, AWS] });
      if (url === "/api/admin/models?source=aws-example") return json({ ...REST, providers: [AWS_LISTED] });
      throw new Error(`unstubbed fetch: ${init?.method ?? "GET"} ${url}`);
    }),
  );
}

function asked(): string[] {
  return vi.mocked(fetch).mock.calls.map(([input, init]) => `${(init as RequestInit | undefined)?.method ?? "GET"} ${String(input)}`);
}

afterEach(() => vi.unstubAllGlobals());

describe("ModelsPane", () => {
  it("opens on what the room holds, lists an owed cloud on its own, and a tick is one PUT that asks no cloud again", async () => {
    stub();
    renderWithClient(ModelsPane);

    // The offered model is first, one row for both clouds once the second one's list is in.
    const offered = await screen.findByRole("region", { name: /Offered in New agent/ });
    const fromAws = await within(offered).findByRole("checkbox", { name: "Offer Claude Sonnet 4.5 from AWS" });
    const row = fromAws.closest("tr")!;
    expect((within(row).getByRole("checkbox", { name: "Offer Claude Sonnet 4.5 from GCP" }) as HTMLInputElement).checked).toBe(true);
    expect((fromAws as HTMLInputElement).checked).toBe(false);
    expect(within(row).getByText("as Claude Affordable Sonnet")).not.toBeNull();
    expect(within(offered).queryByText("Gemini 2.5 Pro")).toBeNull();
    expect(asked()).toEqual(["GET /api/admin/models?listing=held", "GET /api/admin/models?source=aws-example"]);

    // Everything the clouds list is under it, by publisher.
    const all = screen.getByRole("region", { name: /All models/ });
    expect(within(all).getByRole("button", { name: /Anthropic 1 model, 1 offered/ })).not.toBeNull();
    expect(within(all).getByRole("button", { name: /Google 1 model, 0 offered/ })).not.toBeNull();
    expect(screen.getByText(/Not connected: Azure, Self-hosted/)).not.toBeNull();

    await fireEvent.click(within(all).getByRole("checkbox", { name: "Offer Gemini 2.5 Pro from GCP" }));

    await within(offered).findByText("Gemini 2.5 Pro");
    const put = vi.mocked(fetch).mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "PUT");
    expect(JSON.parse(String((put![1] as RequestInit).body))).toEqual({ changes: [{ id: "vertex_ai/gemini-2.5-pro", enabled: true }] });
    expect(asked().filter((a) => a.startsWith("GET /api/admin/models"))).toHaveLength(2);
  });
});
