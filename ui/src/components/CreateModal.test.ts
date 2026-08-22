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
      "/api/catalog/groups": {
        groups: [
          { name: "payments-eng", kind: "engineering" },
          { name: "growth", kind: "engineering" },
        ],
      },
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
      "/api/catalog/groups": { groups: [{ name: "payments-eng", kind: "engineering" }] },
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

describe("CreateModal — the group menu has a commons, a directory, and a door to ask (shared commons wave)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  /** Finds the option whose visible text is "My group is not listed...",
   * without hardcoding this component's own internal sentinel value. */
  function notListedValue(select: HTMLSelectElement): string {
    const option = Array.from(select.options).find(
      (o) => o.textContent === "My group is not listed...",
    );
    if (!option) {
      throw new Error("no 'My group is not listed...' option found");
    }
    return option.value;
  }

  it("sections the select: My group is not listed first, then Shared, then Engineering groups (group-menu-leads-with-the-door ruling)", async () => {
    stubCreateModalCatalogs({
      "/api/catalog/groups": {
        groups: [
          { name: "shared", kind: "shared" },
          { name: "payments-eng", kind: "engineering" },
          { name: "growth", kind: "engineering" },
        ],
      },
    });
    renderWithClient(CreateModal, { open: true, onclose: () => {} });

    const groupSelect = (await screen.findByLabelText("Group")) as HTMLSelectElement;
    const children = Array.from(groupSelect.children);

    // The disabled "Choose a group" placeholder is still the very first
    // child (untouched by this ruling); "My group is not listed..." is the
    // first SELECTABLE option right after it, outside any optgroup.
    expect((children[0] as HTMLOptionElement).disabled).toBe(true);
    expect(children[1].tagName).toBe("OPTION");
    expect(children[1].textContent).toBe("My group is not listed...");

    const optgroups = Array.from(groupSelect.querySelectorAll("optgroup"));
    expect(optgroups.map((g) => g.label)).toEqual(["Shared", "Engineering groups"]);
    // The not-listed option leads straight into the Shared optgroup.
    expect(children[1].nextElementSibling).toBe(optgroups[0]);

    const sharedOptions = Array.from(optgroups[0].querySelectorAll("option"));
    expect(sharedOptions.map((o) => o.value)).toEqual(["shared"]);
    expect(sharedOptions[0].textContent).toBe("shared (everyone in the company)");

    const engineeringOptions = Array.from(optgroups[1].querySelectorAll("option"));
    expect(engineeringOptions.map((o) => o.value)).toEqual(["payments-eng", "growth"]);
  });

  it("sections the select: Non-engineering groups sits between Shared and Engineering groups (create-modal non-engineering-wing ruling)", async () => {
    stubCreateModalCatalogs({
      "/api/catalog/groups": {
        groups: [
          { name: "shared", kind: "shared" },
          { name: "payments-eng", kind: "engineering" },
          { name: "legal", kind: "non-engineering" },
          { name: "finance", kind: "non-engineering" },
        ],
      },
    });
    renderWithClient(CreateModal, { open: true, onclose: () => {} });

    const groupSelect = (await screen.findByLabelText("Group")) as HTMLSelectElement;
    const children = Array.from(groupSelect.children);

    // "My group is not listed..." still leads, right after the placeholder.
    expect(children[1].textContent).toBe("My group is not listed...");

    const optgroups = Array.from(groupSelect.querySelectorAll("optgroup"));
    expect(optgroups.map((g) => g.label)).toEqual([
      "Shared",
      "Non-engineering groups",
      "Engineering groups",
    ]);
    expect(children[1].nextElementSibling).toBe(optgroups[0]);

    const nonEngineeringOptions = Array.from(optgroups[1].querySelectorAll("option"));
    expect(nonEngineeringOptions.map((o) => o.value)).toEqual(["legal", "finance"]);
  });

  it("omits the Non-engineering groups optgroup entirely when the room has none configured", async () => {
    stubCreateModalCatalogs({
      "/api/catalog/groups": {
        groups: [
          { name: "shared", kind: "shared" },
          { name: "payments-eng", kind: "engineering" },
        ],
      },
    });
    renderWithClient(CreateModal, { open: true, onclose: () => {} });

    const groupSelect = (await screen.findByLabelText("Group")) as HTMLSelectElement;
    const optgroups = Array.from(groupSelect.querySelectorAll("optgroup"));
    expect(optgroups.map((g) => g.label)).toEqual(["Shared", "Engineering groups"]);
  });

  it("shows the shared helper sentence only once shared is the picked value", async () => {
    stubCreateModalCatalogs({
      "/api/catalog/groups": {
        groups: [
          { name: "shared", kind: "shared" },
          { name: "payments-eng", kind: "engineering" },
        ],
      },
    });
    renderWithClient(CreateModal, { open: true, onclose: () => {} });

    const groupSelect = (await screen.findByLabelText("Group")) as HTMLSelectElement;
    const sentence = "Shared agents are visible to everyone in the company.";
    expect(screen.queryByText(sentence)).toBeNull();

    await fireEvent.change(groupSelect, { target: { value: "payments-eng" } });
    expect(screen.queryByText(sentence)).toBeNull();

    await fireEvent.change(groupSelect, { target: { value: "shared" } });
    expect(screen.getByText(sentence)).not.toBeNull();
  });

  it("picking 'My group is not listed...' reveals an inline mini-form, not a new modal", async () => {
    stubCreateModalCatalogs({
      "/api/catalog/groups": { groups: [{ name: "shared", kind: "shared" }] },
    });
    renderWithClient(CreateModal, { open: true, onclose: () => {} });

    const groupSelect = (await screen.findByLabelText("Group")) as HTMLSelectElement;
    expect(screen.queryByLabelText("Your group's name")).toBeNull();

    await fireEvent.change(groupSelect, {
      target: { value: notListedValue(groupSelect) },
    });

    // Same document, same form — no second dialog opened.
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
    const nameField = screen.getByLabelText("Your group's name") as HTMLInputElement;
    screen.getByLabelText("Note (optional)");
    const requestButton = screen.getByRole("button", { name: "Request it" }) as HTMLButtonElement;
    expect(requestButton.disabled).toBe(true);

    await fireEvent.input(nameField, { target: { value: "design-team" } });
    expect(requestButton.disabled).toBe(false);
  });

  it("Request it posts to /api/group-requests, then collapses to the confirmation and returns the select to Choose a group", async () => {
    stubCreateModalCatalogs({
      "/api/catalog/groups": { groups: [{ name: "shared", kind: "shared" }] },
      "/api/group-requests": {
        id: 1,
        group_name: "design-team",
        requested_by: "kai@example.com",
        note: "for the new bot",
        status: "pending",
        decided_by: null,
        created_at: "2026-08-22T00:00:00Z",
        decided_at: null,
      },
    });
    renderWithClient(CreateModal, { open: true, onclose: () => {} });

    const groupSelect = (await screen.findByLabelText("Group")) as HTMLSelectElement;
    await fireEvent.change(groupSelect, {
      target: { value: notListedValue(groupSelect) },
    });
    await fireEvent.input(screen.getByLabelText("Your group's name"), {
      target: { value: "design-team" },
    });
    await fireEvent.input(screen.getByLabelText("Note (optional)"), {
      target: { value: "for the new bot" },
    });
    await fireEvent.click(screen.getByRole("button", { name: "Request it" }));

    await screen.findByText(
      "Requested. A room admin will add it. Pick shared to create this agent now, or wait for your group.",
    );
    expect(screen.queryByLabelText("Your group's name")).toBeNull();
    expect(groupSelect.value).toBe("");

    const call = vi
      .mocked(fetch)
      .mock.calls.find(
        ([url, init]) =>
          String(url) === "/api/group-requests" &&
          (init as RequestInit | undefined)?.method === "POST",
      );
    expect(call).toBeDefined();
    const body = JSON.parse((call?.[1] as RequestInit).body as string);
    expect(body).toEqual({ group_name: "design-team", note: "for the new bot" });
  });

  it("a 409 duplicate shows 'Already requested. A room admin will get to it.' and keeps the mini-form open to retry", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/group-requests") && init?.method === "POST") {
          return new Response(
            JSON.stringify({ error: "a request for 'design-team' is already pending" }),
            { status: 409, headers: { "Content-Type": "application/json" } },
          );
        }
        const routes: Record<string, unknown> = {
          "/api/catalog/models": {
            models: [{ id: "gpt-5", label: "GPT-5" }],
            default: "gpt-5",
          },
          "/api/catalog/groups": { groups: [{ name: "shared", kind: "shared" }] },
          "/api/catalog/teams": { teams: ["shared"] },
          "/api/teams": { teams: ["growth", "platform"], default: "shared" },
          "/api/org/status": { org: "acme", wired: true, checks: [] },
          "/api/personas/search": { results: [] },
          "/api/skills/search": { results: [] },
          "/api/": {},
        };
        for (const [needle, payload] of Object.entries(routes)) {
          if (url.includes(needle)) {
            return new Response(JSON.stringify(payload), {
              status: 200,
              headers: { "Content-Type": "application/json" },
            });
          }
        }
        throw new Error(`unstubbed fetch: ${url}`);
      }),
    );
    renderWithClient(CreateModal, { open: true, onclose: () => {} });

    const groupSelect = (await screen.findByLabelText("Group")) as HTMLSelectElement;
    await fireEvent.change(groupSelect, {
      target: { value: notListedValue(groupSelect) },
    });
    await fireEvent.input(screen.getByLabelText("Your group's name"), {
      target: { value: "design-team" },
    });
    await fireEvent.click(screen.getByRole("button", { name: "Request it" }));

    await screen.findByText("Already requested. A room admin will get to it.");
    // The mini-form stays open (not the success confirmation) so the human
    // can edit and retry.
    expect(screen.getByLabelText("Your group's name")).not.toBeNull();
    expect(
      screen.queryByText(
        "Requested. A room admin will add it. Pick shared to create this agent now, or wait for your group.",
      ),
    ).toBeNull();
  });

  it("submit stays blocked while 'My group is not listed...' is picked — it is never sent as a real group_name", async () => {
    stubCreateModalCatalogs({
      "/api/catalog/groups": { groups: [{ name: "shared", kind: "shared" }] },
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
      "/api/availability": { available: true, reason: null },
    });
    renderWithClient(CreateModal, { open: true, onclose: () => {} });

    const groupSelect = (await screen.findByLabelText("Group")) as HTMLSelectElement;
    await fireEvent.change(groupSelect, {
      target: { value: notListedValue(groupSelect) },
    });

    const personaRadio = await screen.findByRole("radio", { name: /Helper/i });
    await fireEvent.click(personaRadio);
    await fireEvent.click(
      await screen.findByRole("button", { name: "name it yourself" }),
    );
    await fireEvent.input(await screen.findByLabelText("Name"), {
      target: { value: "helper-bot" },
    });
    await screen.findByText("✓ Available", {}, { timeout: 2000 });

    const submit = screen.getByRole("button", { name: "Create agent" }) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
  });
});

describe("CreateModal - naming station warm-up (perf fix: prefetch on persona pick)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  // The naming station's own `nameSuggestions` query is already reactive
  // (a persona commit sets `nameSuggestionParams`, which the query's own
  // `derived` options read), but that's still gated behind whatever tick
  // Svelte schedules the effect on, so an explicit prefetch at the exact
  // moment of picking makes the warm-up unambiguous and no longer dependent
  // on that scheduling detail. `who_they_are`/`what_they_do` are both
  // already known client-side from the search result (persona.description/
  // persona.name, no network round trip needed), so this is the earliest
  // possible moment to start the Vertex call.
  it("prefetches name suggestions exactly once, with the right key, the moment a persona is picked from search", async () => {
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
    });
    const { client } = renderWithClient(CreateModal, { open: true, onclose: () => {} });
    await findCreateStationReady();

    const personaRadio = await screen.findByRole("radio", { name: /Helper/i });
    const prefetchSpy = vi.spyOn(client, "prefetchQuery");

    await fireEvent.click(personaRadio);

    expect(prefetchSpy).toHaveBeenCalledTimes(1);
    expect(prefetchSpy.mock.calls[0][0]).toMatchObject({
      queryKey: [
        "names",
        "suggestions",
        { who_they_are: "Helps with things.", what_they_do: "Helper" },
      ],
    });
  });
});
