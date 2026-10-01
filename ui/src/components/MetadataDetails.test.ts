// @vitest-environment jsdom
import { fireEvent, screen, waitFor } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import MetadataDetails from "./MetadataDetails.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

const BASE_PROPS = {
  agentName: "clio",
  team: "shared",
  purposeTags: ["billing", "support"],
  capability: "Answers billing questions.",
};

describe("MetadataDetails — Edit details modal (persona-metadata plan)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    stubFetch({
      "/api/teams": { teams: ["growth", "platform"], default: "shared" },
      "/api/": {},
    });
  });

  it("the affordance disables with the page (isFrozen)", () => {
    renderWithClient(MetadataDetails, { ...BASE_PROPS, frozen: true });
    const trigger = screen.getByRole("button", { name: "Edit details" });
    expect((trigger as HTMLButtonElement).disabled).toBe(true);
  });

  it("opens seeded from the current team/purpose tags/capability, with team options from the stubbed teams query", async () => {
    renderWithClient(MetadataDetails, BASE_PROPS);
    await fireEvent.click(screen.getByRole("button", { name: "Edit details" }));

    const teamSelect = (await screen.findByLabelText("Team")) as HTMLSelectElement;
    expect(teamSelect.value).toBe("shared");
    await waitFor(() => {
      const optionValues = Array.from(teamSelect.options).map((o) => o.value);
      expect(optionValues).toEqual(
        expect.arrayContaining(["shared", "growth", "platform", "__other__"]),
      );
    });

    expect(screen.getByText("billing")).not.toBeNull();
    expect(screen.getByText("support")).not.toBeNull();

    const capabilityField = screen.getByLabelText("Capability") as HTMLTextAreaElement;
    expect(capabilityField.value).toBe("Answers billing questions.");
    expect(screen.getByText(`${"Answers billing questions.".length}/160`)).not.toBeNull();
  });

  it("typed team escape: selecting Other reveals a slug-hinted text input", async () => {
    renderWithClient(MetadataDetails, BASE_PROPS);
    await fireEvent.click(screen.getByRole("button", { name: "Edit details" }));
    const teamSelect = (await screen.findByLabelText("Team")) as HTMLSelectElement;

    await fireEvent.change(teamSelect, { target: { value: "__other__" } });

    const teamOther = await screen.findByLabelText("Team name");
    expect(teamOther).not.toBeNull();
    expect(screen.getByText("lowercase-with-dashes")).not.toBeNull();
  });

  it("chips: add lowercases/dedupes, remove works, caps at 8 with a hint", async () => {
    renderWithClient(MetadataDetails, { ...BASE_PROPS, purposeTags: [] });
    await fireEvent.click(screen.getByRole("button", { name: "Edit details" }));
    await screen.findByLabelText("Team");

    const tagInput = screen.getByLabelText("Purpose tags") as HTMLInputElement;
    const addButton = screen.getByRole("button", { name: "Add" });
    expect(screen.getByText("Up to 8 tags.")).not.toBeNull();

    await fireEvent.input(tagInput, { target: { value: "Ops" } });
    await fireEvent.click(addButton);
    expect(screen.getByText("ops")).not.toBeNull();

    for (const tag of ["a", "b", "c", "d", "e", "f", "g"]) {
      await fireEvent.input(tagInput, { target: { value: tag } });
      await fireEvent.click(addButton);
    }
    expect(screen.getAllByRole("button", { name: /^Remove /i })).toHaveLength(8);

    await fireEvent.input(tagInput, { target: { value: "over-cap" } });
    await fireEvent.click(addButton);
    expect(screen.queryByText("over-cap")).toBeNull();

    await fireEvent.click(screen.getByRole("button", { name: "Remove ops" }));
    expect(screen.queryByText("ops")).toBeNull();
    expect(screen.getAllByRole("button", { name: /^Remove /i })).toHaveLength(7);
  });

  it("save flow: PATCHes the edited fields and shows the delivery chip on the returned sha", async () => {
    stubFetch({
      "/api/teams": { teams: ["growth", "platform"], default: "shared" },
      "/api/agents/clio/metadata": {
        team: "growth",
        purpose_tags: ["billing", "support", "escalations"],
        capability: "Answers billing questions.",
        sha: "abc1234",
      },
      "/api/agents/clio/knowledge/delivery/abc1234": {
        state: "delivered",
        detail: null,
      },
      "/api/": {},
    });
    renderWithClient(MetadataDetails, BASE_PROPS);
    await fireEvent.click(screen.getByRole("button", { name: "Edit details" }));
    await screen.findByLabelText("Team");

    const tagInput = screen.getByLabelText("Purpose tags") as HTMLInputElement;
    await fireEvent.input(tagInput, { target: { value: "escalations" } });
    await fireEvent.click(screen.getByRole("button", { name: "Add" }));

    await fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(screen.getByText("Changes are in place")).not.toBeNull();
    });
  });

  it("regenerate: pending state, then swaps in the returned capability value", async () => {
    let resolveRegenerate: (value: Response) => void = () => {};
    const regeneratePromise = new Promise<Response>((resolve) => {
      resolveRegenerate = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/agents/clio/metadata/regenerate")) {
          return regeneratePromise;
        }
        if (url.includes("/api/teams")) {
          return new Response(
            JSON.stringify({ teams: ["growth"], default: "shared" }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        return new Response(JSON.stringify({}), { status: 200 });
      }),
    );

    renderWithClient(MetadataDetails, BASE_PROPS);
    await fireEvent.click(screen.getByRole("button", { name: "Edit details" }));
    await screen.findByLabelText("Team");

    await fireEvent.click(screen.getByRole("button", { name: "Regenerate" }));
    expect(await screen.findByRole("button", { name: "Regenerating…" })).not.toBeNull();

    resolveRegenerate(
      new Response(
        JSON.stringify({
          team: "shared",
          purpose_tags: ["billing", "support"],
          capability: "A freshly regenerated capability line.",
          sha: "def5678",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    await waitFor(() => {
      const capabilityField = screen.getByLabelText("Capability") as HTMLTextAreaElement;
      expect(capabilityField.value).toBe("A freshly regenerated capability line.");
    });
  });

});

