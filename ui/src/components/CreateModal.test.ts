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
    // Empty by default (access-control Task B2): most existing cases here
    // predate groups entirely and must see no picker at all — a test that
    // wants the picker overrides this key in `extra`.
    "/api/catalog/groups": { groups: [] },
    "/api/catalog/teams": { teams: ["shared"] },
    "/api/teams": { teams: ["growth", "platform"], default: "shared" },
    "/api/org/status": { org: "acme", wired: true, checks: [] },
    "/api/personas/search": { results: [] },
    "/api/skills/search": { results: [] },
    ...extra,
    "/api/": {},
  });
}

// The Create form's own stable, always-rendered anchor: station 4's
// "unlocking" is purely visual (opacity, see the doc comment at the top of
// CreateModal.svelte), so the Purpose tags field is present in the DOM the
// moment the modal mounts, regardless of persona/naming progress — same
// role the removed Team dropdown used to play as a wait-for-render point in
// these tests before the "team vs group" ruling retired it.
async function findCreateStationReady(): Promise<void> {
  await screen.findByLabelText("Purpose tags");
}

describe("CreateModal — team vs group ruling: team dropdown retired", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders no Team dropdown or its Other... escape — team is a derived fact, edited later via Edit details", async () => {
    stubCreateModalCatalogs();
    renderWithClient(CreateModal, { open: true, onclose: () => {} });

    await findCreateStationReady();

    expect(screen.queryByLabelText("Team")).toBeNull();
    expect(screen.queryByLabelText("Team name")).toBeNull();
    expect(screen.queryByText("lowercase-with-dashes")).toBeNull();
    // The skills-search team filter is a DIFFERENT control (a distinct
    // accessible name) and must survive this ruling untouched.
    expect(screen.getByLabelText("Filter skills by team")).not.toBeNull();
  });

  it("submits with no team key in the POST body at all", async () => {
    stubCreateModalCatalogs({
      "/api/personas/search": {
        results: [
          {
            id: "helper",
            name: "Helper",
            description: "Helps with things.",
            team: "shared",
            tags: [],
            builtin: true,
          },
        ],
      },
      "/api/agents/name-suggestions": { suggestions: [] },
      "/api/agents": { name: "helper-bot", status: "provisioning" },
      "/api/availability": { available: true, reason: null },
    });
    renderWithClient(CreateModal, { open: true, onclose: () => {} });

    await findCreateStationReady();

    // Station 1: pick the one seeded persona.
    const personaRadio = await screen.findByRole("radio", { name: /Helper/i });
    await fireEvent.click(personaRadio);

    // Station 2: no suggestions came back, so go straight to manual naming.
    await fireEvent.click(
      await screen.findByRole("button", { name: "name it yourself" }),
    );
    const nameInput = await screen.findByLabelText("Name");
    await fireEvent.input(nameInput, { target: { value: "helper-bot" } });
    await screen.findByText("✓ Available", {}, { timeout: 2000 });

    // Station 4: model defaults, no groups configured — ready to submit.
    const submit = screen.getByRole("button", { name: "Create agent" }) as HTMLButtonElement;
    expect(submit.disabled).toBe(false);
    await fireEvent.click(submit);

    await waitFor(() => {
      expect(
        vi.mocked(fetch).mock.calls.some(
          ([url, init]) =>
            String(url) === "/api/agents" &&
            (init as RequestInit | undefined)?.method === "POST",
        ),
      ).toBe(true);
    });

    const call = vi
      .mocked(fetch)
      .mock.calls.find(
        ([url, init]) =>
          String(url) === "/api/agents" &&
          (init as RequestInit | undefined)?.method === "POST",
      );
    const sentBody = JSON.parse((call?.[1] as RequestInit).body as string);
    expect(sentBody).not.toHaveProperty("team");
    expect(sentBody.name).toBe("helper-bot");
    expect(sentBody.persona).toBe("helper");
  });
});

describe("CreateModal — purpose tags (persona-metadata plan)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("has no capability field at create — it arrives after first contact", async () => {
    stubCreateModalCatalogs();
    renderWithClient(CreateModal, { open: true, onclose: () => {} });

    await findCreateStationReady();
    expect(screen.queryByLabelText(/capability/i)).toBeNull();
    expect(screen.queryByText(/first contact/i)).toBeNull();
  });

  it("shows helper copy explaining what the tags are for", async () => {
    stubCreateModalCatalogs();
    renderWithClient(CreateModal, { open: true, onclose: () => {} });

    await findCreateStationReady();
    expect(
      screen.getByText(
        "Short labels that help people and agents discover what this agent is for.",
      ),
    ).not.toBeNull();
    expect(screen.getByText("Up to 8 tags.")).not.toBeNull();
  });

  it("example chips add themselves on click, and disable once added or once the cap is reached", async () => {
    stubCreateModalCatalogs();
    renderWithClient(CreateModal, { open: true, onclose: () => {} });

    await findCreateStationReady();

    const networking = screen.getByRole("button", { name: "networking" }) as HTMLButtonElement;
    const incident = screen.getByRole("button", {
      name: "incident-response",
    }) as HTMLButtonElement;
    const gcp = screen.getByRole("button", { name: "gcp" }) as HTMLButtonElement;
    const billing = screen.getByRole("button", { name: "billing" }) as HTMLButtonElement;
    expect(networking.disabled).toBe(false);
    expect(billing.disabled).toBe(false);

    await fireEvent.click(networking);
    // The added tag renders its own chip (with a "Remove networking"
    // button) alongside the now-disabled example chip — both show the text
    // "networking", so assert via the unique Remove button rather than
    // `getByText`, which would be ambiguous between the two.
    expect(screen.getByRole("button", { name: "Remove networking" })).not.toBeNull();
    expect(networking.disabled).toBe(true); // already added

    await fireEvent.click(incident);
    await fireEvent.click(gcp);
    // "billing" deliberately left unclicked so the cap case below can prove
    // a NEVER-added example chip also disables once the cap is hit, not
    // just chips that were already picked.
    expect(screen.getAllByRole("button", { name: /^Remove /i })).toHaveLength(3);

    const tagInput = screen.getByLabelText("Purpose tags") as HTMLInputElement;
    const addButton = screen.getByRole("button", { name: "Add" });
    for (const tag of ["a", "b", "c", "d", "e"]) {
      await fireEvent.input(tagInput, { target: { value: tag } });
      await fireEvent.click(addButton);
    }

    // 3 examples + 5 typed = 8, the cap.
    expect(screen.getAllByRole("button", { name: /^Remove /i })).toHaveLength(8);
    expect(billing.disabled).toBe(true); // never added, but the cap is full
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
});

describe("CreateModal — group picker (access-control Task B2)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders no group picker when the room hasn't named any groups", async () => {
    stubCreateModalCatalogs();
    renderWithClient(CreateModal, { open: true, onclose: () => {} });

    await findCreateStationReady();
    expect(screen.queryByLabelText("Group")).toBeNull();
  });

  it("renders a required group picker with the configured names once the room has some", async () => {
    stubCreateModalCatalogs({
      "/api/catalog/groups": { groups: [{ name: "payments-eng" }, { name: "growth" }] },
    });
    renderWithClient(CreateModal, { open: true, onclose: () => {} });

    const groupSelect = (await screen.findByLabelText("Group")) as HTMLSelectElement;
    expect(groupSelect.required).toBe(true);
    const optionValues = Array.from(groupSelect.options).map((o) => o.value);
    expect(optionValues).toEqual(expect.arrayContaining(["payments-eng", "growth"]));
    expect(screen.getByText("Who can see and use this agent.")).not.toBeNull();
  });

  it("submit stays disabled until a group is chosen when groups are configured", async () => {
    stubCreateModalCatalogs({
      "/api/catalog/groups": { groups: [{ name: "payments-eng" }] },
      "/api/availability": { available: true, reason: null },
    });
    renderWithClient(CreateModal, { open: true, onclose: () => {} });

    const groupSelect = (await screen.findByLabelText("Group")) as HTMLSelectElement;
    const submit = screen.getByRole("button", { name: "Create agent" }) as HTMLButtonElement;
    // No persona/name picked yet either, so this is already disabled for
    // other reasons -- this test only proves the group select's own empty
    // starting value (the disabled "Choose a group" placeholder) never
    // silently counts as a pick.
    expect(groupSelect.value).toBe("");
    expect(submit.disabled).toBe(true);

    await fireEvent.change(groupSelect, { target: { value: "payments-eng" } });
    expect(groupSelect.value).toBe("payments-eng");
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

    await findCreateStationReady();
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

    // "billing" is also one of the example-chip labels (below the field),
    // so the pre-filled tag is asserted via its unique "Remove billing"
    // button rather than `getByText`, which would otherwise match both the
    // example chip and the pre-filled tag chip.
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Remove billing" })).not.toBeNull();
      expect(screen.getByText("support")).not.toBeNull();
    });
  });
});
