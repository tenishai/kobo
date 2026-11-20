// @vitest-environment jsdom
//
// An agent's persona, read and edited in place (2026-11-20). These hold what
// a person can count on: the section shows the persona the agent runs, Edit
// persona saves this agent's copy and says the agent restarts with it, rules
// are offered only while they live in the persona, Save to {persona} too
// appears only for who may edit the catalog persona, sends that persona's
// own rules back unchanged and stays off while the text names the agent, a
// persona not in the house shape is edited as one text, and a save from a
// copy that changed since is refused and the form takes the text as it
// stands.
import { fireEvent, screen, waitFor, within } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import AgentPersonaPanel from "./AgentPersonaPanel.svelte";
import { renderWithClient } from "../test-utils/render";

const WHAT = "Keeps the team's alerts straight. ".repeat(12).trim();

const HOUSE = {
  persona: "alert-scribe",
  sha: "1".repeat(64),
  house_shape: true,
  who_they_are: "a careful keeper of alerts",
  what_they_do: WHAT,
  rules: [],
  text: null,
  rules_in_persona: false,
  can_edit: true,
  can_edit_source: false,
};

const CATALOG = {
  id: "alert-scribe",
  name: "alert-scribe",
  team: "sre",
  builtin: false,
  tags: [],
  who_they_are: "a careful keeper of alerts",
  what_they_do: WHAT,
  rules: ["Never guess.", "Say what you checked.", "Name the alert."],
  created_by: "me@example.com",
  created_at: null,
  truncated: false,
};

type Call = { method: string; url: string; body: Record<string, unknown> | null };

/** Answers the persona routes and records every call. `persona` is what the
 * read answers, read again on every call so a test can change it; `save` is
 * the edit's answer. */
function stubRoom(
  persona: Record<string, unknown> | (() => Record<string, unknown>),
  save: { status: number; body: unknown } = {
    status: 200,
    body: { persona: "alert-scribe", sha: "a".repeat(40) },
  },
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
      if (url.includes("/delivery/")) {
        return json({ state: "building", detail: null });
      }
      if (url === "/api/agents/clio/persona/text" && method === "PUT") {
        return json(save.body, save.status);
      }
      if (url === "/api/agents/clio/persona") {
        return json(typeof persona === "function" ? persona() : persona);
      }
      if (url.startsWith("/api/personas/alert-scribe") && method === "GET") {
        return json(CATALOG);
      }
      if (url === "/api/personas/alert-scribe" && method === "PUT") {
        return json({ ...CATALOG, description: "a calmer keeper of alerts" });
      }
      return json({});
    }),
  );
  return calls;
}

function render() {
  return renderWithClient(AgentPersonaPanel, { agentName: "clio", personaName: "alert-scribe" });
}

async function openEditor(): Promise<HTMLElement> {
  await fireEvent.click(await screen.findByRole("button", { name: "Edit persona" }));
  return screen.getByRole("dialog", { name: "Edit persona" });
}

describe("AgentPersonaPanel", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows the persona the agent runs and saves an edit of this agent's copy, which restarts it", async () => {
    const calls = stubRoom(HOUSE);
    render();

    expect(await screen.findByText("You are clio, a careful keeper of alerts.")).not.toBeNull();
    const dialog = await openEditor();
    expect(within(dialog).queryByLabelText("Rules")).toBeNull();
    await fireEvent.input(within(dialog).getByLabelText("Who they are"), {
      target: { value: "a calmer keeper of alerts" },
    });
    await fireEvent.click(within(dialog).getByRole("button", { name: "Save and restart" }));

    await waitFor(() =>
      expect(calls.find((c) => c.method === "PUT")).toEqual({
        method: "PUT",
        url: "/api/agents/clio/persona/text",
        body: { who_they_are: "a calmer keeper of alerts", what_they_do: WHAT, base_sha: HOUSE.sha },
      }),
    );
    expect(await screen.findByText("Restarting clio with the new persona…")).not.toBeNull();
    // This caller may not edit the catalog persona, so nothing offers to.
    expect(screen.queryByRole("button", { name: "Save to alert-scribe too" })).toBeNull();
  });

  it("offers the rules only while they live in the persona", async () => {
    const calls = stubRoom({ ...HOUSE, rules: ["Never guess."], rules_in_persona: true });
    render();

    const dialog = await openEditor();
    const rules = within(dialog).getByLabelText("Rules") as HTMLTextAreaElement;
    expect(rules.value).toBe("Never guess.");
    await fireEvent.input(rules, { target: { value: "Never guess.\nName the alert." } });
    await fireEvent.click(within(dialog).getByRole("button", { name: "Save and restart" }));

    await waitFor(() =>
      expect(calls.find((c) => c.method === "PUT")?.body?.rules).toEqual([
        "Never guess.",
        "Name the alert.",
      ]),
    );
  });

  it("offers Save to the persona too to who may edit it, sending its own rules back unchanged", async () => {
    const calls = stubRoom({ ...HOUSE, can_edit_source: true });
    render();

    const dialog = await openEditor();
    await fireEvent.input(within(dialog).getByLabelText("Who they are"), {
      target: { value: "a calmer keeper of alerts" },
    });
    await fireEvent.click(within(dialog).getByRole("button", { name: "Save and restart" }));

    expect(
      await screen.findByText("Agents made from alert-scribe later still start from the old text."),
    ).not.toBeNull();
    await fireEvent.click(screen.getByRole("button", { name: "Save to alert-scribe too" }));

    await waitFor(() =>
      expect(calls.find((c) => c.method === "PUT" && c.url === "/api/personas/alert-scribe")?.body).toEqual({
        who_they_are: "a calmer keeper of alerts",
        what_they_do: WHAT,
        rules: CATALOG.rules,
      }),
    );
    expect(await screen.findByText(/Saved to alert-scribe\./)).not.toBeNull();
  });

  it("edits a persona not in the house shape as one text", async () => {
    const prose = "You are clio, a helpful assistant.\n\nAnswer from what you know.";
    const calls = stubRoom({
      ...HOUSE,
      house_shape: false,
      who_they_are: null,
      what_they_do: null,
      text: prose,
    });
    render();

    const dialog = await openEditor();
    expect(within(dialog).queryByLabelText("Who they are")).toBeNull();
    await fireEvent.input(within(dialog).getByLabelText("Persona"), {
      target: { value: `${prose}\nBe brief.` },
    });
    await fireEvent.click(within(dialog).getByRole("button", { name: "Save and restart" }));

    await waitFor(() =>
      expect(calls.find((c) => c.method === "PUT")?.body).toEqual({
        text: `${prose}\nBe brief.`,
        base_sha: HOUSE.sha,
      }),
    );
  });

  it("keeps Save to the persona off while the text names the agent", async () => {
    stubRoom({ ...HOUSE, can_edit_source: true });
    render();

    const dialog = await openEditor();
    await fireEvent.input(within(dialog).getByLabelText("What they do"), {
      target: { value: `${WHAT} Ask Clio's owners before paging anyone.` },
    });
    await fireEvent.click(within(dialog).getByRole("button", { name: "Save and restart" }));

    expect(
      await screen.findByText(
        "The text names clio. Agents made from alert-scribe later would carry that name, so take it out first, or keep this change on clio only.",
      ),
    ).not.toBeNull();
    const button = screen.getByRole("button", { name: "Save to alert-scribe too" }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
  });

  it("refuses a save from a copy that changed since, and fills the form with the text as it stands", async () => {
    let current: Record<string, unknown> = HOUSE;
    stubRoom(() => current, {
      status: 409,
      body: { error: "the persona changed since you opened it. Here it is as it stands; make your change again." },
    });
    render();

    const dialog = await openEditor();
    await fireEvent.input(within(dialog).getByLabelText("Who they are"), {
      target: { value: "a calmer keeper of alerts" },
    });
    current = { ...HOUSE, sha: "2".repeat(64), who_they_are: "the keeper someone else wrote" };
    await fireEvent.click(within(dialog).getByRole("button", { name: "Save and restart" }));

    expect(
      await within(dialog).findByText(
        "the persona changed since you opened it. Here it is as it stands; make your change again.",
      ),
    ).not.toBeNull();
    await waitFor(() =>
      expect((within(dialog).getByLabelText("Who they are") as HTMLInputElement).value).toBe(
        "the keeper someone else wrote",
      ),
    );
  });

  it("keeps Save off, with one sentence saying why, until what they do is enough", async () => {
    stubRoom(HOUSE);
    render();

    const dialog = await openEditor();
    await fireEvent.input(within(dialog).getByLabelText("What they do"), {
      target: { value: "Helps." },
    });

    const save = within(dialog).getByRole("button", { name: "Save and restart" }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    expect(within(dialog).getByText("Say more about what they do: 6 of at least 300 characters.")).not.toBeNull();
  });
});
