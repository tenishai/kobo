// @vitest-environment jsdom
// The picker is one list across the clouds (kobo#405): every offered model is
// a row, a family's tiers sit together in order, and the tag after the name
// says where the model comes from; a model nobody serves cannot be picked.
import { render, screen } from "@testing-library/svelte";
import { describe, expect, it, vi } from "vitest";

import ModelPicker from "./ModelPicker.svelte";

const MODELS = [
  { id: "self-hosted/lab/qwen3-32b", label: "Qwen3 32B", family: null, tier: null, provider: "self-hosted", provider_tag: "Self-hosted", available: true, reason: null },
  { id: "bedrock/anthropic.claude-3-5-haiku", label: "Claude Cheap on AWS", family: "Claude", tier: "fast", provider: "bedrock", provider_tag: "AWS", available: false, reason: "Ask a kobo admin to connect AWS with Bedrock" },
  { id: "vertex_ai/gemini-2.5-pro", label: "Gemini Best Pro", family: "Gemini", tier: "best", provider: "vertex_ai", provider_tag: "GCP", available: true, reason: null },
  { id: "vertex_ai/claude-sonnet-4-5", label: "Claude Affordable Sonnet", family: "Claude", tier: "balanced", provider: "vertex_ai", provider_tag: "GCP", available: true, reason: null },
];

describe("ModelPicker", () => {
  it("shows one tagged list, tiers in order within a family, with no cloud headings", () => {
    render(ModelPicker, { props: { models: MODELS, value: "vertex_ai/claude-sonnet-4-5", onchange: vi.fn(), name: "model" } });

    const rows = screen.getAllByRole("radio");
    expect(rows.map((r) => (r as HTMLInputElement).value)).toEqual([
      "vertex_ai/claude-sonnet-4-5",
      "bedrock/anthropic.claude-3-5-haiku",
      "vertex_ai/gemini-2.5-pro",
      "self-hosted/lab/qwen3-32b",
    ]);
    const tags = Array.from(document.querySelectorAll(".model-row__tag")).map((t) => t.textContent);
    expect(tags).toEqual(["GCP", "AWS", "GCP", "Self-hosted"]);
    expect(document.querySelector(".models__group")).toBeNull();
    expect(screen.getByText("Claude, Balanced")).not.toBeNull();
    expect(screen.getByText("Qwen3 32B")).not.toBeNull();
    expect((rows[1] as HTMLInputElement).disabled).toBe(true);
    expect(screen.getByText("Ask a kobo admin to connect AWS with Bedrock")).not.toBeNull();
  });
});
