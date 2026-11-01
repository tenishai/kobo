// @vitest-environment jsdom
import { fireEvent, screen, waitFor, within } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import MetadataDetails from "./MetadataDetails.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

const BASE_PROPS = {
  agentName: "clio",
  team: "shared",
  purposeTags: ["billing", "support"],
  capability: "Answers billing questions.",
};

describe("MetadataDetails: the two changes there is no way back from (issue #368)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    stubFetch({
      "/api/teams": { teams: [], default: "shared" },
      "/api/": {},
    });
  });

  it("offers neither to a caller who may make neither", async () => {
    renderWithClient(MetadataDetails, BASE_PROPS);
    await fireEvent.click(screen.getByRole("button", { name: "Edit details" }));
    await screen.findByLabelText("Persona source");

    expect(screen.queryByText("No way back")).toBeNull();
    expect(screen.queryByText("Convert to connected")).toBeNull();
    expect(screen.queryByText("Destroy this agent")).toBeNull();
  });

  it("puts Convert and Destroy at the foot of the dialog, each behind its own confirm", async () => {
    renderWithClient(MetadataDetails, { ...BASE_PROPS, canConvert: true, canDestroy: true, status: "ready" });
    await fireEvent.click(screen.getByRole("button", { name: "Edit details" }));
    await screen.findByLabelText("Persona source");

    const dialog = screen.getByRole("dialog", { name: "Edit details" });
    expect(within(dialog).getByText("No way back")).not.toBeNull();
    expect(within(dialog).getByText("Convert to connected")).not.toBeNull();
    expect(within(dialog).getByText("Destroy this agent")).not.toBeNull();
    // Closed: no confirm paragraph, nothing to press by accident.
    expect(screen.queryByRole("button", { name: "Convert this agent" })).toBeNull();
    expect(screen.queryByLabelText(/to confirm/)).toBeNull();

    await fireEvent.click(within(dialog).getByRole("button", { name: "Destroy" }));
    expect(await screen.findByLabelText("Type clio to confirm")).not.toBeNull();
    expect(
      (screen.getByRole("button", { name: "Destroy this agent" }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it("converts through the same route, with the same run mode, on confirm", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/convert-to-connected")) {
          return new Response(JSON.stringify({ name: "clio", kind: "connected", run_mode: "http" }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
        return new Response(JSON.stringify({ teams: [], default: "shared" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }),
    );
    renderWithClient(MetadataDetails, { ...BASE_PROPS, canConvert: true });
    await fireEvent.click(screen.getByRole("button", { name: "Edit details" }));
    await screen.findByLabelText("Persona source");

    await fireEvent.click(screen.getByRole("button", { name: "Convert" }));
    expect(
      screen.getByText(/The room stops editing this agent: no chat, memory, learnings, skills, tooling or Slack from here/),
    ).not.toBeNull();
    // Still no run mode question: a composed agent already serves web
    // traffic, and the route refuses anything else.
    expect(screen.queryByRole("radio")).toBeNull();
    await fireEvent.click(screen.getByRole("button", { name: "Convert this agent" }));

    await waitFor(() => {
      const call = vi
        .mocked(fetch)
        .mock.calls.find(([url]) => String(url).includes("/convert-to-connected"));
      expect(call).toBeDefined();
      const init = (call as [RequestInfo | URL, RequestInit])[1];
      expect(init.method).toBe("POST");
      expect(JSON.parse(String(init.body))).toEqual({ run_mode: "http" });
    });
  });
});

describe("MetadataDetails — Edit details modal (persona-metadata plan)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    stubFetch({
      "/api/teams": { teams: ["growth", "platform"], default: "shared" },
      "/api/": {},
    });
  });

  it("opens seeded from the current team/purpose tags/capability, with team options from the stubbed teams query", async () => {
    renderWithClient(MetadataDetails, BASE_PROPS);
    await fireEvent.click(screen.getByRole("button", { name: "Edit details" }));

    const teamSelect = (await screen.findByLabelText("Persona source")) as HTMLSelectElement;
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
    const teamSelect = (await screen.findByLabelText("Persona source")) as HTMLSelectElement;

    await fireEvent.change(teamSelect, { target: { value: "__other__" } });

    const teamOther = await screen.findByLabelText("Team name");
    expect(teamOther).not.toBeNull();
    expect(screen.getByText("lowercase-with-dashes")).not.toBeNull();
  });

  it("chips: add lowercases/dedupes, remove works, caps at 8 with a hint", async () => {
    renderWithClient(MetadataDetails, { ...BASE_PROPS, purposeTags: [] });
    await fireEvent.click(screen.getByRole("button", { name: "Edit details" }));
    await screen.findByLabelText("Persona source");

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
    await screen.findByLabelText("Persona source");

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
    await screen.findByLabelText("Persona source");

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

