// @vitest-environment jsdom
//
// The persona form (rebuilt 2026-11-17): one screen with the four parts of
// a persona and the group it is filed under, for making one and for
// editing one. These hold what a person can count on: a draft fills the
// form and never takes what they typed, Create stays off with one sentence
// saying why until the persona is enough to work from, a new persona is
// filed under its maker's group, a taken name says where the persona is,
// and an edit saves the text and nothing else. `mergeDraftFields`,
// `personaMissing` and `personaGroups` have their own cases in
// ../lib/validate.test.ts.
import { fireEvent, screen, waitFor } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import PersonaBuilder from "./PersonaBuilder.svelte";
import { renderWithClient } from "../test-utils/render";

const BODY = "Keeps the team's billing answers straight. ".repeat(8).trim();
const RULES = ["Never guess a figure.", "Always say what you checked.", "Ask before you change a plan."];

const DRAFT = {
  name: "billing-buddy",
  who_they_are: "a calm troubleshooter for billing questions",
  what_they_do: BODY,
  rules: RULES,
  purpose_tags: ["billing", "support"],
};

const CREATED = {
  id: "ledger",
  name: "ledger",
  description: DRAFT.who_they_are,
  team: "sre",
  tags: [],
  builtin: false,
};

const CATALOG_GROUPS = {
  groups: [
    { name: "shared", kind: "shared" },
    { name: "sre", kind: "engineering" },
    { name: "qa", kind: "engineering" },
  ],
};

type Call = { method: string; url: string; body: Record<string, unknown> | null };

/** Answers whoami, the group catalog, the draft, the create and the edit
 * routes, and records every call. `draft: null` is a route that is down. */
function stubRoom(
  whoami: { groups: string[]; operator?: boolean },
  answers: {
    draft?: unknown | null;
    create?: { status: number; body: unknown };
    stored?: unknown;
  } = {},
): Call[] {
  const calls: Call[] = [];
  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      calls.push({ method, url, body: init?.body ? JSON.parse(String(init.body)) : null });
      if (url.includes("/api/whoami")) {
        return json({
          email: "me@example.com",
          operator: whoami.operator ?? false,
          groups: whoami.groups,
          groups_unavailable: false,
        });
      }
      if (url.includes("/api/catalog/groups")) {
        return json(CATALOG_GROUPS);
      }
      if (url.includes("/api/personas/draft")) {
        const draft = answers.draft === undefined ? DRAFT : answers.draft;
        return draft === null ? json({ error: "down" }, 502) : json(draft);
      }
      if (url.includes("/api/personas/") && method === "GET") {
        return json(answers.stored);
      }
      if (url.includes("/api/personas/") && method === "PUT") {
        return json(CREATED);
      }
      if (url.includes("/api/personas") && method === "POST") {
        const create = answers.create ?? { status: 201, body: CREATED };
        return json(create.body, create.status);
      }
      return json({});
    }),
  );
  return calls;
}

function field(label: string | RegExp): HTMLInputElement | HTMLTextAreaElement {
  return screen.getByLabelText(label) as HTMLInputElement | HTMLTextAreaElement;
}

async function type(label: string | RegExp, value: string): Promise<void> {
  await fireEvent.input(field(label), { target: { value } });
}

async function fillByHand(): Promise<void> {
  await type("Name", "ledger");
  await type("Who they are", DRAFT.who_they_are);
  await type("What they do", BODY);
  await type("Rules", RULES.join("\n"));
}

function createButton(): HTMLButtonElement {
  return screen.getByRole("button", { name: "Create persona" }) as HTMLButtonElement;
}

async function groupSelect(): Promise<HTMLSelectElement> {
  const select = screen.getByLabelText("Group") as HTMLSelectElement;
  await waitFor(() => expect(select.options.length).toBeGreaterThan(2));
  return select;
}

describe("PersonaBuilder", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("a draft fills the parts that are empty, keeps what was typed, and Create files it under the maker's group", async () => {
    const calls = stubRoom({ groups: ["sre"] });
    const oncreated = vi.fn();
    renderWithClient(PersonaBuilder, { oncancel: () => {}, oncreated });

    await type("Name", "ledger");
    await type(/Start from a description/, "Help customers with billing questions.");
    await fireEvent.click(screen.getByRole("button", { name: "Draft persona" }));

    await waitFor(() => expect(field("What they do").value).toBe(BODY));
    expect(field("Name").value).toBe("ledger");
    expect(field("Who they are").value).toBe(DRAFT.who_they_are);
    expect(field("Rules").value).toBe(RULES.join("\n"));
    // Nothing was typed into Rules, so none are sent as the person's own.
    expect(calls.find((c) => c.url.includes("/draft"))?.body).toEqual({
      job: "Help customers with billing questions.",
      rules: [],
    });

    await waitFor(() => expect(createButton().disabled).toBe(false));
    await fireEvent.click(createButton());

    await waitFor(() => expect(oncreated).toHaveBeenCalledWith(CREATED, DRAFT.purpose_tags));
    expect(calls.find((c) => c.method === "POST" && c.url === "/api/personas")?.body).toEqual({
      name: "ledger",
      who_they_are: DRAFT.who_they_are,
      what_they_do: BODY,
      rules: RULES,
      purpose_tags: DRAFT.purpose_tags,
      team: "sre",
    });
  });

  it("a draft that fails says so and leaves the form as it was", async () => {
    stubRoom({ groups: ["sre"] }, { draft: null });
    renderWithClient(PersonaBuilder, { oncancel: () => {}, oncreated: () => {} });

    await type("What they do", "typed by hand");
    await type(/Start from a description/, "Help customers with billing questions.");
    await fireEvent.click(screen.getByRole("button", { name: "Draft persona" }));

    expect(
      (await screen.findByRole("alert")).textContent?.trim(),
    ).toBe("Couldn't draft a persona right now. Fill in the parts yourself below.");
    expect(field("What they do").value).toBe("typed by hand");
    expect(field("Name").value).toBe("");
  });

  it("a stand-in from a room whose model is away is not shown as a draft", async () => {
    stubRoom({ groups: ["sre"] }, { draft: { ...DRAFT, generated: false } });
    renderWithClient(PersonaBuilder, { oncancel: () => {}, oncreated: () => {} });

    await type(/Start from a description/, "Help customers with billing questions.");
    await fireEvent.click(screen.getByRole("button", { name: "Draft persona" }));

    expect(
      (await screen.findByRole("alert")).textContent?.trim(),
    ).toBe("Couldn't draft a persona right now. Fill in the parts yourself below.");
    expect(field("Name").value).toBe("");
    expect(field("What they do").value).toBe("");
  });

  it("an uploaded file lands in the description box, and nothing is drafted until asked", async () => {
    const calls = stubRoom({ groups: ["sre"] });
    const { container } = renderWithClient(PersonaBuilder, {
      oncancel: () => {},
      oncreated: () => {},
    });

    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    await fireEvent.change(input, {
      target: { files: [new File(["# Runbook\nPage the owner."], "runbook.md", { type: "text/markdown" })] },
    });

    await waitFor(() =>
      expect(field(/Start from a description/).value).toBe("# Runbook\nPage the owner."),
    );
    expect(calls.some((c) => c.url.includes("/draft"))).toBe(false);
    expect(field("What they do").value).toBe("");
  });

  it("keeps Create off, with one sentence saying why, until the persona is enough to work from", async () => {
    stubRoom({ groups: ["sre"] });
    renderWithClient(PersonaBuilder, { oncancel: () => {}, oncreated: () => {} });

    expect(screen.getByText("Give it a name.")).not.toBeNull();
    expect(createButton().disabled).toBe(true);

    await fillByHand();
    await type("What they do", "Helps.");
    expect(screen.getByText("Say more about what they do: 6 of at least 300 characters.")).not.toBeNull();
    expect(createButton().disabled).toBe(true);

    await type("What they do", BODY);
    await type("Rules", "Never guess.");
    expect(screen.getByText("Add at least 3 rules, one per line.")).not.toBeNull();

    await type("Rules", RULES.join("\n"));
    await waitFor(() => expect(createButton().disabled).toBe(false));
  });

  it("someone in several groups chooses one, and Everyone is the last choice, never the default", async () => {
    const calls = stubRoom({ groups: ["sre", "qa"] });
    renderWithClient(PersonaBuilder, { oncancel: () => {}, oncreated: () => {} });
    await fillByHand();

    const select = await groupSelect();
    expect([...select.options].map((o) => o.textContent?.trim())).toEqual([
      "Choose a group",
      "sre",
      "qa",
      "Everyone",
    ]);
    expect(select.value).toBe("");
    expect(screen.getByText("Choose a group.")).not.toBeNull();
    expect(createButton().disabled).toBe(true);

    await fireEvent.change(select, { target: { value: "shared" } });
    await waitFor(() => expect(createButton().disabled).toBe(false));
    await fireEvent.click(createButton());
    await waitFor(() =>
      expect(calls.find((c) => c.method === "POST" && c.url === "/api/personas")?.body?.team).toBe("shared"),
    );
  });

  it("offers the group the dialog was on first, and ignores one the maker is not in", async () => {
    stubRoom({ groups: ["sre", "qa"] });
    const first = renderWithClient(PersonaBuilder, {
      oncancel: () => {},
      oncreated: () => {},
      defaultTeam: "qa",
    });
    await waitFor(async () => expect((await groupSelect()).value).toBe("qa"));
    first.unmount();

    renderWithClient(PersonaBuilder, {
      oncancel: () => {},
      oncreated: () => {},
      defaultTeam: "design",
    });
    const select = await groupSelect();
    expect(select.value).toBe("");
    expect([...select.options].map((o) => o.value)).not.toContain("design");
  });

  it("an operator can file under any group the room has named", async () => {
    stubRoom({ groups: [], operator: true });
    renderWithClient(PersonaBuilder, { oncancel: () => {}, oncreated: () => {} });

    const select = await groupSelect();
    expect([...select.options].map((o) => o.value)).toEqual(["", "sre", "qa", "shared"]);
  });

  it("a taken name says which group the persona is under", async () => {
    stubRoom(
      { groups: ["sre"] },
      {
        create: {
          status: 409,
          body: {
            error: "a persona with that name already exists",
            id: "ledger",
            team: "shared",
            archived: false,
          },
        },
      },
    );
    renderWithClient(PersonaBuilder, { oncancel: () => {}, oncreated: () => {} });
    await fillByHand();
    await waitFor(() => expect(createButton().disabled).toBe(false));
    await fireEvent.click(createButton());

    expect((await screen.findByRole("alert")).textContent).toBe(
      "A persona with that name already exists. It is under Everyone in the persona list.",
    );
  });

  it("editing starts from the whole stored text and saves the text alone", async () => {
    const calls = stubRoom(
      { groups: ["sre"] },
      {
        stored: {
          id: "ledger",
          name: "ledger",
          team: "sre",
          builtin: false,
          tags: [],
          who_they_are: DRAFT.who_they_are,
          what_they_do: BODY,
          rules: RULES,
          created_by: "me@example.com",
          created_at: null,
          truncated: false,
        },
      },
    );
    const onsaved = vi.fn();
    renderWithClient(PersonaBuilder, { oncancel: () => {}, onsaved, editId: "ledger" });

    await waitFor(() => expect(field("What they do").value).toBe(BODY));
    expect(calls.some((c) => c.url === "/api/personas/ledger?full=true")).toBe(true);
    expect((field("Name") as HTMLInputElement).disabled).toBe(true);
    expect(screen.queryByLabelText("Group")).toBeNull();
    expect(screen.queryByRole("button", { name: "Draft persona" })).toBeNull();

    await type("Who they are", "a blunt auditor of invoices");
    await fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(onsaved).toHaveBeenCalled());
    expect(calls.find((c) => c.method === "PUT")).toEqual({
      method: "PUT",
      url: "/api/personas/ledger",
      body: { who_they_are: "a blunt auditor of invoices", what_they_do: BODY, rules: RULES },
    });
  });
});
