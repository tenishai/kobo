// @vitest-environment jsdom
import { fireEvent, screen } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import SkillsPanel from "./SkillsPanel.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

const EMPTY_SKILLS = { skills: [] };
const EMPTY_TEAMS = { teams: ["sre", "growth"] };
const EMPTY_SEARCH = { results: [] };
const NO_RECOMMENDED = { results: [] };

const COST_REPORT_ENTRY = {
  id: "cost-report",
  name: "cost-report",
  description: "Summarizes recent spend.",
  team: "sre",
  tags: [],
};

const BATCH_OK = { sha: "abc123def456", directory: "off", added: ["cost-report"], refused: [] };

/** Tick one row and press the sticky "Add 1 skill": one press saves, no
 * second confirm (issue #287). */
async function pickAndAdd(name: string): Promise<void> {
  await fireEvent.click(screen.getByLabelText(`Select ${name}`));
  await fireEvent.click(screen.getByRole("button", { name: "Add 1 skill" }));
}

/** The body of the one batch POST the picker sends. */
async function batchBody(): Promise<unknown> {
  await new Promise((resolve) => setTimeout(resolve, 0));
  const call = vi
    .mocked(fetch)
    .mock.calls.find(
      ([url, init]) =>
        String(url).includes("/api/agents/warden/skills/catalog/batch") &&
        (init as RequestInit | undefined)?.method === "POST",
    );
  expect(call).toBeDefined();
  return JSON.parse((call as [RequestInfo | URL, RequestInit])[1].body as string);
}

/** Open the "Suggestions" disclosure. */
async function openSuggestions(): Promise<void> {
  await fireEvent.click(await screen.findByRole("button", { name: "Suggestions" }));
}

/** Open the "Browse the catalog" disclosure. */
async function openCatalog(): Promise<void> {
  await fireEvent.click(
    await screen.findByRole("button", { name: "Browse the catalog" }),
  );
}

describe("SkillsPanel", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("keeps both disclosures shut until they are asked for", async () => {
    stubFetch({
      "/api/agents/warden/skills": EMPTY_SKILLS,
      "/api/catalog/teams": EMPTY_TEAMS,
      "/api/skills/search": EMPTY_SEARCH,
      "/api/": {},
    });
    renderWithClient(SkillsPanel, { agentName: "warden", frozen: false });

    const catalog = await screen.findByRole("button", { name: "Browse the catalog" });
    const suggestions = screen.getByRole("button", { name: "Suggestions" });
    expect(catalog.getAttribute("aria-expanded")).toBe("false");
    expect(suggestions.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByLabelText("Search skills catalog")).toBeNull();
  });

  it("opens each disclosure on click", async () => {
    stubFetch({
      "/api/agents/warden/skills": EMPTY_SKILLS,
      "/api/catalog/teams": EMPTY_TEAMS,
      "/api/skills/search": EMPTY_SEARCH,
      "/api/": {},
    });
    renderWithClient(SkillsPanel, { agentName: "warden", frozen: false });

    await openCatalog();
    expect(
      screen
        .getByRole("button", { name: "Browse the catalog" })
        .getAttribute("aria-expanded"),
    ).toBe("true");
    expect(screen.getByLabelText("Search skills catalog")).not.toBeNull();
    expect(screen.getByLabelText("Filter by team")).not.toBeNull();

    await openSuggestions();
    expect(
      screen.getByRole("button", { name: "Suggestions" }).getAttribute("aria-expanded"),
    ).toBe("true");
  });

  it("lists the agent's own skills first, with Edit and Remove per row", async () => {
    stubFetch({
      "/api/agents/warden/skills": {
        skills: [{ name: "cost-report", editable: true, team: "sre", source: "catalog", sha: null }],
      },
      "/api/catalog/teams": EMPTY_TEAMS,
      "/api/skills/search": EMPTY_SEARCH,
      "/api/": {},
    });
    renderWithClient(SkillsPanel, { agentName: "warden", frozen: false });

    expect(await screen.findByText("cost-report")).not.toBeNull();
    expect(screen.getByRole("button", { name: "Edit cost-report" })).not.toBeNull();
    expect(screen.getByRole("button", { name: "Remove cost-report" })).not.toBeNull();
  });

  it("shows recommended skills for the agent and adds one on click", async () => {
    stubFetch({
      "/api/agents/warden/skills/catalog/batch": BATCH_OK,
      "/api/agents/warden/skills": EMPTY_SKILLS,
      "/api/catalog/teams": EMPTY_TEAMS,
      "/api/skills/recommended": { results: [COST_REPORT_ENTRY] },
      "/api/skills/search": EMPTY_SEARCH,
      "/api/": {},
    });

    renderWithClient(SkillsPanel, {
      agentName: "warden",
      frozen: false,
      recommendWho: "watches billing",
      recommendWhat: "cost billing",
    });

    await openSuggestions();

    expect(await screen.findByText(/Recommended for this agent/)).not.toBeNull();
    const row = screen.getByText("cost-report").closest("li");
    expect(row).not.toBeNull();

    await pickAndAdd("cost-report");

    expect(await batchBody()).toEqual({ skills: [{ name: "cost-report", team: "sre" }] });
    // No confirm step: the picked row is in the table with its state already.
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByText("cost-report", { selector: "code" })).not.toBeNull();
  });

  it("searches the catalog from the combobox and adds a result", async () => {
    // A manual mock (rather than `stubFetch`'s substring-match routing)
    // because this test needs to tell an empty-query search apart from a
    // "cost"-query one -- `stubFetch` would return the same body for both,
    // masking whether the debounced search ever actually re-fired with the
    // typed value.
    function jsonResponse(body: unknown): Response {
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/agents/warden/skills/catalog/batch") && init?.method === "POST") {
          return jsonResponse(BATCH_OK);
        }
        if (url.includes("/api/agents/warden/skills")) {
          return jsonResponse(EMPTY_SKILLS);
        }
        if (url.includes("/api/catalog/teams")) {
          return jsonResponse(EMPTY_TEAMS);
        }
        if (url.includes("/api/skills/recommended")) {
          return jsonResponse(NO_RECOMMENDED);
        }
        if (url.includes("/api/skills/search")) {
          return jsonResponse({ results: url.includes("q=cost") ? [COST_REPORT_ENTRY] : [] });
        }
        return jsonResponse({});
      }),
    );

    renderWithClient(SkillsPanel, { agentName: "warden", frozen: false });

    await openCatalog();

    const input = await screen.findByLabelText("Search skills catalog");
    // The dropdown opens on `focusin` (a bubbling event the combobox's
    // wrapper div listens for) -- jsdom's plain `focus` event does not bubble
    // the way a real browser's paired focusin does, so it has to be fired
    // explicitly here to open the dropdown the way a real click/tab would.
    await fireEvent.focusIn(input);
    await fireEvent.input(input, { target: { value: "cost" } });

    const found = await screen.findByText("cost-report", {}, { timeout: 2000 });
    expect(found).not.toBeNull();

    expect(
      vi
        .mocked(fetch)
        .mock.calls.some(([url]) => String(url).includes("/api/skills/search?q=cost")),
    ).toBe(true);

    await pickAndAdd("cost-report");

    expect(await batchBody()).toEqual({ skills: [{ name: "cost-report", team: "sre" }] });
  });

  it("omits the recommended strip when the agent has no capability or tags", async () => {
    stubFetch({
      "/api/agents/warden/skills": EMPTY_SKILLS,
      "/api/catalog/teams": EMPTY_TEAMS,
      "/api/skills/search": EMPTY_SEARCH,
      "/api/": {},
    });

    renderWithClient(SkillsPanel, {
      agentName: "warden",
      frozen: false,
      recommendWho: "",
      recommendWhat: "",
    });

    await openSuggestions();
    // Let any in-flight microtasks settle before asserting an absence.
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(screen.queryByText(/Recommended for this agent/)).toBeNull();
    expect(
      vi.mocked(fetch).mock.calls.some(([url]) => String(url).includes("/api/skills/recommended")),
    ).toBe(false);
  });
});
