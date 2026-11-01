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

});

describe("CreateModal — the footer says what is still missing (plain-form wave)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("names the persona first, then the name, then the group — one sentence at a time", async () => {
    stubCreateModalCatalogs({
      "/api/catalog/groups": { groups: [{ name: "payments-eng", kind: "engineering" }] },
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
      target: { value: "payments-eng" },
    });
    await waitFor(() => {
      expect(screen.queryByText("Choose a group.")).toBeNull();
    });
    const submit = screen.getByRole("button", { name: "Create agent" }) as HTMLButtonElement;
    expect(submit.disabled).toBe(false);
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
    expect(sentBody).not.toHaveProperty("purpose_tags");
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

  it("sections the select: My group is not listed first, then Engineering groups, and no Shared", async () => {
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
    expect(optgroups.map((g) => g.label)).toEqual(["Engineering groups"]);
    // The not-listed option leads straight into the first group section.
    expect(children[1].nextElementSibling).toBe(optgroups[0]);

    const engineeringOptions = Array.from(optgroups[0].querySelectorAll("option"));
    expect(engineeringOptions.map((o) => o.value)).toEqual(["payments-eng", "growth"]);

    // Shared is the very last thing in the menu: teams should reach for
    // their own group, and the commons is the exception.
    expect(groupSelect.querySelector('option[value="shared"]')).toBeNull();
  });

  it("picking 'My group is not listed...' reveals an inline mini-form, not a new modal", async () => {
    stubCreateModalCatalogs({
      "/api/catalog/groups": { groups: [{ name: "payments-eng", kind: "engineering" }] },
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
      "/api/catalog/groups": { groups: [{ name: "payments-eng", kind: "engineering" }] },
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
      "Requested. A room admin will add it, and you can create the agent once it is listed.",
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
          "/api/catalog/groups": { groups: [{ name: "payments-eng", kind: "engineering" }] },
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
        "Requested. A room admin will add it, and you can create the agent once it is listed.",
      ),
    ).toBeNull();
  });

  it("submit stays blocked while 'My group is not listed...' is picked — it is never sent as a real group_name", async () => {
    stubCreateModalCatalogs({
      "/api/catalog/groups": { groups: [{ name: "payments-eng", kind: "engineering" }] },
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

  it("Escape dismisses the card", async () => {
    const { row } = await openWithHelper();
    await rest(row);
    await screen.findByRole("tooltip");

    await fireEvent.keyDown(row, { key: "Escape" });

    await waitFor(() => {
      expect(screen.queryByRole("tooltip")).toBeNull();
    });
  });

});


describe("CreateModal — five built-ins lead, group personas sit behind pills (2026-09-29)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  const SCRIBE = { id: "scribe", name: "Scribe", description: "Writes things down.", team: "sre", tags: [], builtin: false };
  const QA_CRITIC = { id: "qa-critic", name: "QA Critic", description: "Finds the bug first.", team: "qa", tags: [], builtin: false };

  /** The whole catalog, team rows first on purpose: the list's grouping,
   * not the payload order, puts built-ins on top and team rows behind pills. */
  function stubWholeCatalog(): void {
    stubCreateModalCatalogs();
    const catalogs = vi.mocked(fetch).getMockImplementation()!;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/personas/search")) {
          expect(new URL(url, "http://room").searchParams.get("scope")).toBe("all");
          return new Response(JSON.stringify({ results: [QA_CRITIC, SCRIBE, HELPER] }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
        return catalogs(input, init);
      }),
    );
  }

  it("shows the built-ins, then a pill per group, and no group persona until a pill is pressed", async () => {
    stubWholeCatalog();
    renderWithClient(CreateModal, { open: true, onclose: () => {} });

    await screen.findByRole("radio", { name: /Helper/i });
    expect(screen.getByText("Built-in")).not.toBeNull();
    const pills = screen.getByRole("group", { name: "Group personas" });
    const labels = Array.from(pills.querySelectorAll("button")).map((b) => b.textContent?.trim());
    expect(labels).toEqual(["qa", "sre"]);
    expect(screen.queryByRole("radio", { name: /QA Critic/i })).toBeNull();
    expect(screen.queryByRole("radio", { name: /Scribe/i })).toBeNull();
  });

  it("a pressed pill shows that group's personas, and pressing it again hides them", async () => {
    stubWholeCatalog();
    renderWithClient(CreateModal, { open: true, onclose: () => {} });
    await screen.findByRole("radio", { name: /Helper/i });

    const qa = screen.getByRole("button", { name: "qa" });
    await fireEvent.click(qa);
    expect(qa.getAttribute("aria-pressed")).toBe("true");
    expect(await screen.findByRole("radio", { name: /QA Critic/i })).not.toBeNull();
    expect(screen.queryByRole("radio", { name: /Scribe/i })).toBeNull();

    await fireEvent.click(screen.getByRole("button", { name: "sre" }));
    expect(await screen.findByRole("radio", { name: /Scribe/i })).not.toBeNull();
    expect(screen.queryByRole("radio", { name: /QA Critic/i })).toBeNull();

    await fireEvent.click(screen.getByRole("button", { name: "sre" }));
    await waitFor(() => {
      expect(screen.queryByRole("radio", { name: /Scribe/i })).toBeNull();
    });
  });
});

describe("CreateModal — models from the connected clouds (kobo#390)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  const GATEWAY_MODELS = {
    gateway: true,
    default: "vertex_ai/claude-sonnet-4-5",
    models: [
      {
        id: "vertex_ai/claude-sonnet-4-5", label: "Claude Affordable Sonnet", family: "Claude", tier: "balanced",
        provider: "vertex_ai", provider_label: "Google Cloud", gateway_model: "claude-affordable-sonnet",
        available: true, reason: null,
      },
      {
        id: "bedrock/claude-3-7", label: "Claude Fast Haiku", family: "Claude", tier: "fast",
        provider: "bedrock", provider_label: "AWS", gateway_model: "claude-fast-haiku",
        available: false, reason: "Ask a kobo admin to connect AWS",
      },
    ],
  };

  it("groups the list by cloud and greys a model whose cloud is not connected, with the reason", async () => {
    stubCreateModalCatalogs({ "/api/catalog/models": GATEWAY_MODELS });
    renderWithClient(CreateModal, { open: true, onclose: () => {} });
    await findFormReady();

    expect(await screen.findByText("Google Cloud")).not.toBeNull();
    expect(screen.getByText("AWS")).not.toBeNull();
    const greyed = screen.getByRole("radio", { name: /Claude, Fast/ }) as HTMLInputElement;
    expect(greyed.disabled).toBe(true);
    expect(screen.getByText("Ask a kobo admin to connect AWS")).not.toBeNull();
    const offered = screen.getByRole("radio", { name: /Claude, Balanced/ }) as HTMLInputElement;
    expect(offered.disabled).toBe(false);
    expect(offered.checked).toBe(true);
  });

  it("disables Create agent with one sentence when no connected cloud serves a model", async () => {
    stubCreateModalCatalogs({
      "/api/catalog/models": {
        ...GATEWAY_MODELS,
        default: null,
        models: GATEWAY_MODELS.models.map((m) => ({ ...m, available: false, reason: "Ask a kobo admin to connect Google Cloud" })),
      },
      "/api/personas/search": { results: [HELPER] },
      "/api/availability": { available: true, reason: null },
    });
    renderWithClient(CreateModal, { open: true, onclose: () => {} });
    await findFormReady();
    await pickHelperAndName();

    expect(
      screen.getByText(/Connect a cloud with models under Admin, Integrations, before creating an agent\./),
    ).not.toBeNull();
    const submit = screen.getByRole("button", { name: "Create agent" }) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
  });
});
