// @vitest-environment jsdom
import { fireEvent, screen, waitFor } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import CreateModal from "./CreateModal.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

// The create dialog is one plain form now (plain-form wave): persona,
// name, group, model, and a purpose-tags disclosure, all of it on screen
// at once with no stepper and no generated names. Every catalog query it
// owns still fires on mount regardless of `open` (the same "warm modal"
// queries App.svelte prefetches), so every case here needs the full
// catalog stub set, in subpath-before-catch-all order (stubFetch matches by
// `url.includes`, so `/api/personas/search` and `/api/personas/draft` must
// precede a bare `/api/personas` needle, and every specific route must
// precede the `/api/` catch-all).
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
    ...extra,
    "/api/": {},
  });
}

const HELPER = {
  id: "helper",
  name: "Helper",
  description: "Helps with things.",
  team: "shared",
  tags: [],
  builtin: true,
};

/** The form's stable, always-rendered anchor: the name field is a plain
 * text input that is on screen from the first beat, whether or not a
 * persona has been picked. */
async function findFormReady(): Promise<HTMLInputElement> {
  return (await screen.findByLabelText("Name")) as HTMLInputElement;
}

/** Opens the purpose-tags disclosure, which starts shut. */
async function openPurposeTags(): Promise<void> {
  await fireEvent.click(
    screen.getByRole("button", { name: "Add purpose tags (optional)" }),
  );
}

/** Picks the seeded Helper persona and types a name the availability stub
 * will call free, leaving the form one group-pick away from submittable. */
async function pickHelperAndName(value = "helper-bot"): Promise<void> {
  await fireEvent.click(await screen.findByRole("radio", { name: /Helper/i }));
  await fireEvent.input(await findFormReady(), { target: { value } });
  await screen.findByText("Available", {}, { timeout: 2000 });
}

function createPostBody(): Record<string, unknown> {
  const call = vi
    .mocked(fetch)
    .mock.calls.find(
      ([url, init]) =>
        String(url) === "/api/agents" &&
        (init as RequestInit | undefined)?.method === "POST",
    );
  return JSON.parse((call?.[1] as RequestInit).body as string);
}

describe("CreateModal — one plain form (plain-form wave)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows every field at once, in order, with no stepper", async () => {
    stubCreateModalCatalogs({
      "/api/catalog/groups": { groups: [{ name: "shared", kind: "shared" }] },
      "/api/personas/search": { results: [HELPER] },
    });
    const { container } = renderWithClient(CreateModal, {
      open: true,
      onclose: () => {},
    });

    await findFormReady();
    await screen.findByLabelText("Group");

    expect(container.querySelector(".modal__steps")).toBeNull();
    expect(container.querySelectorAll(".modal__step")).toHaveLength(0);

    const labels = Array.from(container.querySelectorAll(".field__label")).map(
      (el) => el.textContent?.trim(),
    );
    expect(labels).toEqual(["Persona", "Name", "Group", "Model"]);

    // Group and Model are one-line answers and share a line, which is what
    // keeps all five fields inside a 70vh body on a laptop.
    const pair = container.querySelector(".field-pair");
    const paired = Array.from(pair?.querySelectorAll(".field__label") ?? []).map(
      (el) => el.textContent?.trim(),
    );
    expect(paired).toEqual(["Group", "Model"]);

    // Purpose tags are the only thing not on screen, and they say so.
    expect(
      screen.getByRole("button", { name: "Add purpose tags (optional)" }),
    ).not.toBeNull();
  });

  it("sits at the dialog's default width, not the picker width", async () => {
    stubCreateModalCatalogs();
    const { container } = renderWithClient(CreateModal, {
      open: true,
      onclose: () => {},
    });

    await findFormReady();
    expect(container.querySelector(".modal--wide")).toBeNull();
  });

  it("has one filled button, in a footer under the body", async () => {
    stubCreateModalCatalogs();
    const { container } = renderWithClient(CreateModal, {
      open: true,
      onclose: () => {},
    });

    await findFormReady();

    const footer = container.querySelector(".modal__footer");
    expect(footer).not.toBeNull();
    expect(container.querySelector(".modal__body")?.contains(footer as Node)).toBe(
      false,
    );
    expect(footer?.querySelector(".btn--primary")?.textContent?.trim()).toBe(
      "Create agent",
    );
    expect(footer?.textContent).toContain("Cancel");
    expect(container.querySelectorAll(".btn--primary")).toHaveLength(1);
  });
});

describe("CreateModal — the persona list (plain-form wave)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  const LONG = {
    id: "long",
    name: "long-winded-helper",
    description:
      "Helps with things, and keeps helping long past the width of any one row in this list or any dialog that could hold it.",
    team: "growth",
    tags: [],
    builtin: false,
  };

  it("scrolls in a fixed box and cuts each description off at the row's edge", async () => {
    stubCreateModalCatalogs({ "/api/personas/search": { results: [LONG] } });
    const { container } = renderWithClient(CreateModal, {
      open: true,
      onclose: () => {},
    });

    await screen.findByRole("radio", { name: /long-winded-helper/i });

    const list = container.querySelector(".list");
    // The fixed-height, width-bounded box: about five rows deep, and no
    // row inside it may widen the dialog body.
    expect(list?.className).toContain("list--scroll");

    const row = list?.querySelector(".list__row");
    expect(row?.querySelector(".list__name")?.textContent).toBe("long-winded-helper");
    expect(row?.querySelector(".list__tag")?.textContent?.trim()).toBe("growth");
    const desc = row?.querySelector(".list__desc");
    expect(desc?.textContent).toBe(LONG.description);
    // Truncation hides the rest, and the hover card (not a title tooltip)
    // is what shows it now.
    expect(desc?.getAttribute("title")).toBeNull();
  });

  it("washes the picked row and leaves the others plain", async () => {
    stubCreateModalCatalogs({
      "/api/personas/search": {
        results: [
          HELPER,
          {
            id: "scribe",
            name: "Scribe",
            description: "Writes things down.",
            team: "growth",
            tags: [],
            builtin: false,
          },
        ],
      },
    });
    const { container } = renderWithClient(CreateModal, {
      open: true,
      onclose: () => {},
    });

    await fireEvent.click(await screen.findByRole("radio", { name: /Helper/i }));

    const selected = container.querySelectorAll(".list__row--selected");
    expect(selected).toHaveLength(1);
    expect(selected[0].querySelector(".list__name")?.textContent).toBe("Helper");
  });

  it("says so plainly when a search matches nothing", async () => {
    stubCreateModalCatalogs();
    renderWithClient(CreateModal, { open: true, onclose: () => {} });

    await findFormReady();
    await fireEvent.input(screen.getByLabelText("Search personas"), {
      target: { value: "nothing-like-this" },
    });

    expect(await screen.findByText("No personas match.")).not.toBeNull();
  });

  it("offers no Start from scratch row at all", async () => {
    stubCreateModalCatalogs({ "/api/personas/search": { results: [HELPER] } });
    const { container } = renderWithClient(CreateModal, {
      open: true,
      onclose: () => {},
    });

    await findFormReady();
    expect(screen.queryByText("Start from scratch")).toBeNull();
    expect(container.querySelector(".list__row--action")).toBeNull();
  });

  it("Build your own persona sits on the label row and opens the builder in place", async () => {
    stubCreateModalCatalogs({ "/api/personas/search": { results: [HELPER] } });
    const { container } = renderWithClient(CreateModal, {
      open: true,
      onclose: () => {},
    });

    await findFormReady();

    const link = screen.getByRole("button", { name: "Build your own persona" });
    expect(link.closest(".field__label-row")?.querySelector(".field__label")?.textContent)
      .toBe("Persona");

    await fireEvent.click(link);

    // The builder takes the whole form over, same as before.
    expect(
      await screen.findByLabelText("What should this agent do for you?"),
    ).not.toBeNull();
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
    expect(screen.queryByLabelText("Name")).toBeNull();
    // The shell's pinned footer steps aside entirely: the builder draws its
    // own sticky one inside the body, so the two are never both on screen.
    expect(
      container.querySelector(".modal__body")?.nextElementSibling,
    ).toBeNull();
    expect(container.querySelectorAll(".btn--primary")).toHaveLength(1);
  });
});

describe("CreateModal — the name is typed, never generated (plain-form wave)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("offers one text input and no generated suggestions", async () => {
    stubCreateModalCatalogs({ "/api/personas/search": { results: [HELPER] } });
    renderWithClient(CreateModal, { open: true, onclose: () => {} });

    const nameInput = await findFormReady();
    expect(nameInput.placeholder).toBe("e.g. release-notes");
    expect(
      screen.getByText("Lowercase letters, numbers, and hyphens. 2 to 24 characters."),
    ).not.toBeNull();

    await fireEvent.click(await screen.findByRole("radio", { name: /Helper/i }));

    expect(screen.queryByRole("button", { name: "Offer more" })).toBeNull();
    expect(screen.queryByRole("button", { name: "name it yourself" })).toBeNull();
    expect(screen.queryByRole("radiogroup", { name: "Suggested names" })).toBeNull();
  });

  it("never asks the name-suggestions route for anything", async () => {
    stubCreateModalCatalogs({
      "/api/personas/search": { results: [HELPER] },
      "/api/availability": { available: true, reason: null },
    });
    renderWithClient(CreateModal, { open: true, onclose: () => {} });

    await findFormReady();
    await pickHelperAndName();

    expect(
      vi
        .mocked(fetch)
        .mock.calls.filter(([url]) =>
          String(url).includes("/api/agents/name-suggestions"),
        ),
    ).toHaveLength(0);
  });

  it("validates on every keystroke and only then checks availability", async () => {
    stubCreateModalCatalogs({
      "/api/personas/search": { results: [HELPER] },
      "/api/availability": { available: true, reason: null },
    });
    renderWithClient(CreateModal, { open: true, onclose: () => {} });

    const nameInput = await findFormReady();

    await fireEvent.input(nameInput, { target: { value: "Helper Bot" } });
    expect(
      await screen.findByText("Use lowercase letters, numbers, and hyphens only."),
    ).not.toBeNull();
    // An obviously-bad name never costs a round trip.
    expect(
      vi.mocked(fetch).mock.calls.filter(([url]) =>
        String(url).includes("/api/availability"),
      ),
    ).toHaveLength(0);

    await fireEvent.input(nameInput, { target: { value: "helper-bot" } });
    expect(await screen.findByText("Available", {}, { timeout: 2000 })).not.toBeNull();
  });

  it("says Taken in the fail tone when the room already has that name", async () => {
    stubCreateModalCatalogs({
      "/api/personas/search": { results: [HELPER] },
      "/api/availability": { available: false, reason: null },
    });
    const { container } = renderWithClient(CreateModal, {
      open: true,
      onclose: () => {},
    });

    await fireEvent.input(await findFormReady(), { target: { value: "helper-bot" } });

    const taken = await screen.findByText("Taken", {}, { timeout: 2000 });
    expect(taken.className).toContain("field__status--error");
    expect(container.querySelector(".field__status--ok")).toBeNull();
  });
});

describe("CreateModal — purpose tags hide behind a disclosure (plain-form wave)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("starts shut, with nothing of the chips editor on screen", async () => {
    stubCreateModalCatalogs();
    renderWithClient(CreateModal, { open: true, onclose: () => {} });

    await findFormReady();

    const disclosure = screen.getByRole("button", {
      name: "Add purpose tags (optional)",
    });
    expect(disclosure.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByLabelText("Purpose tags")).toBeNull();
    expect(screen.queryByRole("button", { name: "Add" })).toBeNull();

    await fireEvent.click(disclosure);
    expect(disclosure.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByLabelText("Purpose tags")).not.toBeNull();
  });

  it("has no capability field at create — it arrives after first contact", async () => {
    stubCreateModalCatalogs();
    renderWithClient(CreateModal, { open: true, onclose: () => {} });

    await findFormReady();
    await openPurposeTags();
    expect(screen.queryByLabelText(/capability/i)).toBeNull();
    expect(screen.queryByText(/first contact/i)).toBeNull();
  });

  it("shows helper copy explaining what the tags are for", async () => {
    stubCreateModalCatalogs();
    renderWithClient(CreateModal, { open: true, onclose: () => {} });

    await findFormReady();
    await openPurposeTags();
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

    await findFormReady();
    await openPurposeTags();

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

    await findFormReady();
    await openPurposeTags();

    const tagInput = screen.getByLabelText("Purpose tags") as HTMLInputElement;
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

  it("pre-fills purpose chips from the persona builder's drafted purpose_tags, and opens the disclosure to show them", async () => {
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
    });
    renderWithClient(CreateModal, { open: true, onclose: () => {} });

    await findFormReady();
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
    expect(
      screen
        .getByRole("button", { name: "Add purpose tags (optional)" })
        .getAttribute("aria-expanded"),
    ).toBe("true");
  });
});

describe("CreateModal — the footer says what is still missing (plain-form wave)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("names the persona first, then the name, then the group — one sentence at a time", async () => {
    stubCreateModalCatalogs({
      "/api/catalog/groups": { groups: [{ name: "shared", kind: "shared" }] },
      "/api/personas/search": { results: [HELPER] },
      "/api/availability": { available: true, reason: null },
    });
    renderWithClient(CreateModal, { open: true, onclose: () => {} });

    await findFormReady();
    expect(await screen.findByText("Pick a persona first.")).not.toBeNull();
    expect(screen.queryByText("Give it a name.")).toBeNull();
    expect(screen.queryByText("Choose a group.")).toBeNull();

    await fireEvent.click(await screen.findByRole("radio", { name: /Helper/i }));
    expect(await screen.findByText("Give it a name.")).not.toBeNull();
    expect(screen.queryByText("Pick a persona first.")).toBeNull();

    await fireEvent.input(screen.getByLabelText("Name"), {
      target: { value: "helper-bot" },
    });
    await screen.findByText("Available", {}, { timeout: 2000 });
    expect(await screen.findByText("Choose a group.")).not.toBeNull();

    await fireEvent.change(screen.getByLabelText("Group"), {
      target: { value: "shared" },
    });
    await waitFor(() => {
      expect(screen.queryByText("Choose a group.")).toBeNull();
    });
    const submit = screen.getByRole("button", { name: "Create agent" }) as HTMLButtonElement;
    expect(submit.disabled).toBe(false);
  });

  it("says nothing at all once the form is complete", async () => {
    stubCreateModalCatalogs({
      "/api/personas/search": { results: [HELPER] },
      "/api/availability": { available: true, reason: null },
    });
    renderWithClient(CreateModal, { open: true, onclose: () => {} });

    await findFormReady();
    await pickHelperAndName();

    await waitFor(() => {
      expect(screen.queryByText("Pick a persona first.")).toBeNull();
      expect(screen.queryByText("Give it a name.")).toBeNull();
      expect(screen.queryByText("Choose a group.")).toBeNull();
    });
  });
});

describe("CreateModal — the create request (payload unchanged)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("sends the typed name, the picked persona, an empty skills list, and no team key", async () => {
    stubCreateModalCatalogs({
      "/api/personas/search": { results: [HELPER] },
      "/api/agents": { name: "helper-bot", status: "provisioning" },
      "/api/availability": { available: true, reason: null },
    });
    renderWithClient(CreateModal, { open: true, onclose: () => {} });

    await findFormReady();
    await pickHelperAndName();

    // Skills are added post-create, in the Skills tab -- not here.
    expect(screen.queryByText("Skills")).toBeNull();

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

    const sentBody = createPostBody();
    expect(sentBody).not.toHaveProperty("team");
    expect(sentBody.name).toBe("helper-bot");
    expect(sentBody.persona).toBe("helper");
    expect(sentBody.skills).toEqual([]);
    expect(sentBody.model).toBe("gpt-5");
    expect(sentBody.purpose_tags).toEqual([]);
  });

  it("carries the purpose tags the disclosure collected", async () => {
    stubCreateModalCatalogs({
      "/api/personas/search": { results: [HELPER] },
      "/api/agents": { name: "helper-bot", status: "provisioning" },
      "/api/availability": { available: true, reason: null },
    });
    renderWithClient(CreateModal, { open: true, onclose: () => {} });

    await findFormReady();
    await openPurposeTags();
    await fireEvent.click(screen.getByRole("button", { name: "gcp" }));
    await pickHelperAndName();

    await fireEvent.click(screen.getByRole("button", { name: "Create agent" }));
    await waitFor(() => {
      expect(
        vi.mocked(fetch).mock.calls.some(
          ([url, init]) =>
            String(url) === "/api/agents" &&
            (init as RequestInit | undefined)?.method === "POST",
        ),
      ).toBe(true);
    });
    expect(createPostBody().purpose_tags).toEqual(["gcp"]);
  });

  it("sends the chosen group when the room has groups", async () => {
    stubCreateModalCatalogs({
      "/api/catalog/groups": { groups: [{ name: "payments-eng", kind: "engineering" }] },
      "/api/personas/search": { results: [HELPER] },
      "/api/agents": { name: "helper-bot", status: "provisioning" },
      "/api/availability": { available: true, reason: null },
    });
    renderWithClient(CreateModal, { open: true, onclose: () => {} });

    await findFormReady();
    await pickHelperAndName();
    await fireEvent.change(screen.getByLabelText("Group"), {
      target: { value: "payments-eng" },
    });

    await fireEvent.click(screen.getByRole("button", { name: "Create agent" }));
    await waitFor(() => {
      expect(
        vi.mocked(fetch).mock.calls.some(
          ([url, init]) =>
            String(url) === "/api/agents" &&
            (init as RequestInit | undefined)?.method === "POST",
        ),
      ).toBe(true);
    });
    expect(createPostBody().group_name).toBe("payments-eng");
  });
});

describe("CreateModal — group picker (access-control Task B2)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders no group picker when the room hasn't named any groups", async () => {
    stubCreateModalCatalogs();
    renderWithClient(CreateModal, { open: true, onclose: () => {} });

    await findFormReady();
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
      "/api/personas/search": { results: [HELPER] },
      "/api/availability": { available: true, reason: null },
    });
    renderWithClient(CreateModal, { open: true, onclose: () => {} });

    const groupSelect = (await screen.findByLabelText("Group")) as HTMLSelectElement;
    await fireEvent.change(groupSelect, {
      target: { value: notListedValue(groupSelect) },
    });

    await pickHelperAndName();

    const submit = screen.getByRole("button", { name: "Create agent" }) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    expect(await screen.findByText("Choose a group.")).not.toBeNull();
  });
});

// --- the persona hover card ------------------------------------------------

const HELPER_DETAIL = {
  id: "helper",
  name: "Helper",
  team: "shared",
  builtin: true,
  tags: [],
  who_they_are: "Helps with things.",
  what_they_do: "Answers questions and keeps the answer short.",
  rules: [],
  created_by: "kobo",
  created_at: null,
  truncated: false,
};

/** Every fetch this suite made for one persona's detail. */
function personaDetailCalls(id: string): unknown[] {
  return vi
    .mocked(fetch)
    .mock.calls.filter(([url]) => String(url) === `/api/personas/${id}`);
}

/** Rest on a row for longer than the hover delay, on a fake clock, and hand
 * the real one back before anything is asserted — testing-library's own
 * waiting helpers are not driven by vitest's fake timers. */
async function rest(row: Element, ms = 300): Promise<void> {
  vi.useFakeTimers();
  try {
    await fireEvent.mouseEnter(row);
    await vi.advanceTimersByTimeAsync(ms);
  } finally {
    vi.useRealTimers();
  }
}

describe("CreateModal — the persona hover card", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  async function openWithHelper() {
    stubCreateModalCatalogs({
      "/api/personas/search": { results: [HELPER] },
      "/api/personas/helper": HELPER_DETAIL,
    });
    const rendered = renderWithClient(CreateModal, { open: true, onclose: () => {} });
    const radio = await screen.findByRole("radio", { name: /Helper/i });
    const row = radio.closest("label") as HTMLElement;
    return { ...rendered, radio, row };
  }

  it("the description cell no longer carries a title — the card is the answer now", async () => {
    const { row } = await openWithHelper();
    expect(row.querySelector(".list__desc")?.getAttribute("title")).toBeNull();
  });

  it("asks for nothing until the row has actually been rested on", async () => {
    const { row } = await openWithHelper();

    vi.useFakeTimers();
    try {
      await fireEvent.mouseEnter(row);
      await vi.advanceTimersByTimeAsync(200);
      expect(personaDetailCalls("helper")).toHaveLength(0);
    } finally {
      vi.useRealTimers();
    }
    expect(screen.queryByRole("tooltip")).toBeNull();
  });

  it("shows the card once the delay is up, and points the row at it", async () => {
    const { radio, row } = await openWithHelper();

    await rest(row);

    const card = await screen.findByRole("tooltip");
    expect(radio.getAttribute("aria-describedby")).toBe(card.id);
    expect(await screen.findByText("What they do")).not.toBeNull();
  });

  it("hides again when the pointer leaves", async () => {
    const { radio, row } = await openWithHelper();
    await rest(row);
    await screen.findByRole("tooltip");

    await fireEvent.mouseLeave(row);

    await waitFor(() => {
      expect(screen.queryByRole("tooltip")).toBeNull();
    });
    expect(radio.getAttribute("aria-describedby")).toBeNull();
  });

  it("opens on keyboard focus too, and closes on blur", async () => {
    const { radio } = await openWithHelper();

    vi.useFakeTimers();
    try {
      await fireEvent.focus(radio);
      await vi.advanceTimersByTimeAsync(300);
    } finally {
      vi.useRealTimers();
    }
    await screen.findByRole("tooltip");

    await fireEvent.blur(radio);
    await waitFor(() => {
      expect(screen.queryByRole("tooltip")).toBeNull();
    });
  });

  it("holds open while the pointer is on the card itself", async () => {
    // Otherwise the card would dismiss itself on the way to its own
    // "Open the full persona" link.
    const { row } = await openWithHelper();
    await rest(row);
    const card = await screen.findByRole("tooltip");

    vi.useFakeTimers();
    try {
      await fireEvent.mouseLeave(row);
      await fireEvent.mouseEnter(card);
      await vi.advanceTimersByTimeAsync(400);
    } finally {
      vi.useRealTimers();
    }

    expect(screen.queryByRole("tooltip")).not.toBeNull();
  });

  it("Escape dismisses the card", async () => {
    const { row } = await openWithHelper();
    await rest(row);
    await screen.findByRole("tooltip");

    await fireEvent.keyDown(row, { key: "Escape" });

    await waitFor(() => {
      expect(screen.queryByRole("tooltip")).toBeNull();
    });
  });

  it("picking a persona dismisses the card", async () => {
    const { radio, row } = await openWithHelper();
    await rest(row);
    await screen.findByRole("tooltip");

    await fireEvent.click(radio);

    await waitFor(() => {
      expect(screen.queryByRole("tooltip")).toBeNull();
    });
  });

  it("costs one request per persona, however often it is hovered", async () => {
    const { row } = await openWithHelper();

    await rest(row);
    await screen.findByRole("tooltip");
    await fireEvent.mouseLeave(row);
    await waitFor(() => {
      expect(screen.queryByRole("tooltip")).toBeNull();
    });

    await rest(row);
    await screen.findByRole("tooltip");

    expect(personaDetailCalls("helper")).toHaveLength(1);
  });
});

describe("CreateModal — the persona list keeps to your groups (persona team scope)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  const OTHER_GROUP = {
    id: "qa-critic",
    name: "QA Critic",
    description: "Finds the bug first.",
    team: "qa",
    tags: [],
    builtin: false,
  };

  /** The mine list holds Helper alone; the all list adds a persona filed
   * under another group. Everything else answers like the shared stub. */
  function stubScopedSearch(): void {
    stubCreateModalCatalogs();
    const catalogs = vi.mocked(fetch).getMockImplementation()!;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/personas/search")) {
          const scope = new URL(url, "http://room").searchParams.get("scope");
          const results = scope === "all" ? [HELPER, OTHER_GROUP] : [HELPER];
          return new Response(JSON.stringify({ results }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
        return catalogs(input, init);
      }),
    );
  }

  function searchScopes(): Array<string | null> {
    return vi
      .mocked(fetch)
      .mock.calls.map(([url]) => String(url))
      .filter((url) => url.includes("/api/personas/search"))
      .map((url) => new URL(url, "http://room").searchParams.get("scope"));
  }

  it("asks for your own groups by default", async () => {
    stubScopedSearch();
    renderWithClient(CreateModal, { open: true, onclose: () => {} });

    await screen.findByRole("radio", { name: /Helper/i });

    expect(searchScopes().length).toBeGreaterThan(0);
    expect(new Set(searchScopes())).toEqual(new Set(["mine"]));
  });

  it("the toggle flips the list to every group and back", async () => {
    stubScopedSearch();
    renderWithClient(CreateModal, { open: true, onclose: () => {} });
    await screen.findByRole("radio", { name: /Helper/i });

    await fireEvent.click(screen.getByRole("button", { name: "Show all groups" }));
    await waitFor(() => expect(searchScopes()).toContain("all"));

    const back = await screen.findByRole("button", { name: "Show mine" });
    await fireEvent.click(back);
    expect(await screen.findByRole("button", { name: "Show all groups" })).not.toBeNull();
  });

  it("personas from other groups appear only after the toggle, still tagged", async () => {
    stubScopedSearch();
    renderWithClient(CreateModal, { open: true, onclose: () => {} });
    await screen.findByRole("radio", { name: /Helper/i });

    expect(screen.queryByRole("radio", { name: /QA Critic/i })).toBeNull();

    await fireEvent.click(screen.getByRole("button", { name: "Show all groups" }));

    const row = (await screen.findByRole("radio", { name: /QA Critic/i })).closest(
      ".list__row",
    ) as HTMLElement;
    expect(row.querySelector(".list__tag")?.textContent?.trim()).toBe("qa");
  });

  it("hands the chosen group to the persona builder as its default", async () => {
    stubCreateModalCatalogs({
      "/api/catalog/groups": {
        groups: [
          { name: "shared", kind: "shared" },
          { name: "sre", kind: "engineering" },
          { name: "qa", kind: "engineering" },
        ],
      },
      "/api/whoami": {
        email: "me@example.com",
        operator: false,
        groups: ["sre", "qa"],
        groups_unavailable: false,
      },
      "/api/personas/draft": {
        name: "billing-buddy",
        who_they_are: "a calm troubleshooter",
        what_they_do: "Helps.",
        purpose_tags: [],
      },
      "/api/personas/search": { results: [HELPER] },
    });
    renderWithClient(CreateModal, { open: true, onclose: () => {} });

    const group = (await screen.findByLabelText("Group")) as HTMLSelectElement;
    await fireEvent.change(group, { target: { value: "qa" } });
    await fireEvent.click(screen.getByRole("button", { name: "Build your own persona" }));
    await fireEvent.input(screen.getByLabelText("What should this agent do for you?"), {
      target: { value: "Help with billing." },
    });
    await fireEvent.click(screen.getByRole("button", { name: "Draft persona" }));

    const forWhom = (await screen.findByLabelText(
      "Who is this persona for?",
    )) as HTMLSelectElement;
    await waitFor(() => expect(forWhom.value).toBe("qa"));
  });
});
