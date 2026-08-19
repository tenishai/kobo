// @vitest-environment jsdom
import { fireEvent, screen, waitFor } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import CreateModal from "./CreateModal.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

// The create station's fresh-open effect (`$effect` gated on `open`) resets
// every field, and every catalog query CreateModal owns fires immediately
// on mount regardless of `open` (same "warm modal" queries App.svelte
// prefetches) — so every case here needs the full catalog stub set, in
// subpath-before-catch-all order (stubFetch matches by `url.includes`, so
// `/api/personas/search` and `/api/personas/draft` must precede a bare
// `/api/personas` needle, and every specific route must precede the `/api/`
// catch-all).
function stubCreateModalCatalogs(extra: Record<string, unknown> = {}): void {
  stubFetch({
    "/api/catalog/models": {
      models: [{ id: "gpt-5", label: "GPT-5" }],
      default: "gpt-5",
    },
    "/api/catalog/teams": { teams: ["shared"] },
    "/api/teams": { teams: ["growth", "platform"], default: "shared" },
    "/api/org/status": { org: "acme", wired: true, checks: [] },
    "/api/personas/search": { results: [] },
    "/api/skills/search": { results: [] },
    ...extra,
    "/api/": {},
  });
}

describe("CreateModal — team picker + purpose chips (persona-metadata plan)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders team picker options from the stubbed teams query plus the default, with an Other... escape", async () => {
    stubCreateModalCatalogs();
    renderWithClient(CreateModal, { open: true, onclose: () => {} });

    const teamSelect = (await screen.findByLabelText("Team")) as HTMLSelectElement;
    await waitFor(() => {
      const optionValues = Array.from(teamSelect.options).map((o) => o.value);
      expect(optionValues).toEqual(
        expect.arrayContaining(["shared", "growth", "platform", "__other__"]),
      );
    });

    expect(screen.queryByLabelText("Team name")).toBeNull();

    await fireEvent.change(teamSelect, { target: { value: "__other__" } });

    const teamOther = await screen.findByLabelText("Team name");
    expect(teamOther).not.toBeNull();
    expect(screen.getByText("lowercase-with-dashes")).not.toBeNull();
  });

  it("has no capability field at create — it arrives after first contact", async () => {
    stubCreateModalCatalogs();
    renderWithClient(CreateModal, { open: true, onclose: () => {} });

    await screen.findByLabelText("Team");
    expect(screen.queryByLabelText(/capability/i)).toBeNull();
    expect(screen.queryByText(/first contact/i)).toBeNull();
  });

  it("purpose chips: adding lowercases/dedupes, removing works, and caps at 8 with a hint", async () => {
    stubCreateModalCatalogs();
    renderWithClient(CreateModal, { open: true, onclose: () => {} });

    const tagInput = (await screen.findByLabelText("Purpose tags")) as HTMLInputElement;
    const addButton = screen.getByRole("button", { name: "Add" });
    expect(screen.getByText("Up to 8 tags.")).not.toBeNull();

    await fireEvent.input(tagInput, { target: { value: "Support" } });
    await fireEvent.click(addButton);
    expect(screen.getByText("support")).not.toBeNull();
    expect(screen.queryByText("Support")).toBeNull();

    // Re-adding the same (normalized) tag doesn't duplicate it.
    await fireEvent.input(tagInput, { target: { value: "support" } });
    await fireEvent.click(addButton);
    expect(screen.getAllByText("support")).toHaveLength(1);

    for (const tag of ["a", "b", "c", "d", "e", "f", "g"]) {
      await fireEvent.input(tagInput, { target: { value: tag } });
      await fireEvent.click(addButton);
    }
    // 8 total now (support + a..g); a 9th is refused.
    expect(screen.getAllByRole("button", { name: /^Remove /i })).toHaveLength(8);
    await fireEvent.input(tagInput, { target: { value: "one-too-many" } });
    await fireEvent.click(addButton);
    expect(screen.queryByText("one-too-many")).toBeNull();
    expect(screen.getAllByRole("button", { name: /^Remove /i })).toHaveLength(8);

    await fireEvent.click(screen.getByRole("button", { name: "Remove support" }));
    expect(screen.queryByText("support")).toBeNull();
    expect(screen.getAllByRole("button", { name: /^Remove /i })).toHaveLength(7);
  });

  it("pre-fills purpose chips from the persona builder's drafted purpose_tags", async () => {
    stubCreateModalCatalogs({
      "/api/personas/draft": {
        name: "billing-helper",
        who_they_are: "a calm guide through confusing invoices",
        what_they_do: "Answers billing questions and escalates refunds.",
        purpose_tags: ["billing", "support"],
      },
      "/api/personas": {
        id: "billing-helper",
        name: "billing-helper",
        description: "a calm guide through confusing invoices",
        team: "shared",
        tags: ["billing"],
      },
      "/api/agents/name-suggestions": { suggestions: [] },
    });
    renderWithClient(CreateModal, { open: true, onclose: () => {} });

    await screen.findByLabelText("Team");
    await fireEvent.click(
      screen.getByRole("button", { name: "Build your own persona" }),
    );

    const jobField = screen.getByLabelText("What should this agent do for you?");
    await fireEvent.input(jobField, {
      target: { value: "Help customers with billing questions." },
    });
    await fireEvent.click(screen.getByRole("button", { name: "Draft persona" }));

    await screen.findByRole("button", { name: "Create persona" });
    await fireEvent.click(screen.getByRole("button", { name: "Create persona" }));

    await waitFor(() => {
      expect(screen.getByText("billing")).not.toBeNull();
      expect(screen.getByText("support")).not.toBeNull();
    });
  });
});
