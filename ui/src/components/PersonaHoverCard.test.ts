// @vitest-environment jsdom
import { screen, waitFor } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import PersonaHoverCard from "./PersonaHoverCard.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

const ARIA = {
  id: "aria",
  name: "Aria the Assistant",
  team: "cs-team",
  builtin: false,
  tags: ["billing", "support"],
  who_they_are: "a calm troubleshooter for billing questions",
  what_they_do:
    "Help customers fix billing problems quickly.\n\nEscalate anything that smells like fraud.",
  rules: ["Never share account numbers.", "Say when you do not know."],
  created_by: "room",
  created_at: new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString(),
  truncated: false,
};

function renderCard(personaId: string | null = "aria") {
  return renderWithClient(PersonaHoverCard, { personaId, row: null, boundary: null });
}

describe("PersonaHoverCard", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("is a tooltip, not a dialog", async () => {
    stubFetch({ "/api/personas/aria": ARIA });
    renderCard();

    const card = await screen.findByRole("tooltip");
    expect(card.getAttribute("role")).toBe("tooltip");
  });

  it("shows a skeleton while the persona is on its way", async () => {
    // A fetch that never settles: the card is in its loading beat for the
    // whole test.
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise(() => {})),
    );
    const { container } = renderCard();

    await waitFor(() => {
      expect(container.querySelector(".skeleton")).not.toBeNull();
    });
    expect(container.querySelectorAll(".skeleton__row")).toHaveLength(2);
  });

  it("shows the name, the tags, both prose sections and the footer", async () => {
    stubFetch({ "/api/personas/aria": ARIA });
    const { container } = renderCard();

    await screen.findByText("Aria the Assistant");

    const tags = Array.from(container.querySelectorAll(".hover-card__tag")).map(
      (el) => el.textContent?.trim(),
    );
    expect(tags).toEqual(["cs-team", "billing", "support"]);

    expect(screen.getByText("Who they are")).not.toBeNull();
    expect(
      screen.getByText("a calm troubleshooter for billing questions"),
    ).not.toBeNull();

    expect(screen.getByText("What they do")).not.toBeNull();
    const paragraphs = Array.from(
      container.querySelectorAll(".hover-card__para"),
    ).map((el) => el.textContent?.trim());
    expect(paragraphs).toEqual([
      "Help customers fix billing problems quickly.",
      "Escalate anything that smells like fraud.",
    ]);

    expect(screen.getByText("Made by room · 2h ago")).not.toBeNull();
  });

  it("says Built-in instead of a team for a packaged persona", async () => {
    stubFetch({
      "/api/personas/helper": {
        ...ARIA,
        id: "helper",
        name: "helper",
        team: "shared",
        builtin: true,
        tags: [],
      },
    });
    const { container } = renderCard("helper");

    await screen.findByText("helper");
    const tags = Array.from(container.querySelectorAll(".hover-card__tag")).map(
      (el) => el.textContent?.trim(),
    );
    expect(tags).toEqual(["Built-in"]);
  });

  it("shows five rules and counts the rest", async () => {
    stubFetch({
      "/api/personas/aria": {
        ...ARIA,
        rules: ["one", "two", "three", "four", "five", "six", "seven"],
      },
    });
    const { container } = renderCard();

    await screen.findByText("Rules");
    const rules = Array.from(container.querySelectorAll(".hover-card__rule")).map(
      (el) => el.textContent?.trim(),
    );
    expect(rules).toEqual(["one", "two", "three", "four", "five"]);
    expect(screen.getByText("and 2 more")).not.toBeNull();
  });

  it("leaves Rules out when there are none", async () => {
    stubFetch({ "/api/personas/aria": { ...ARIA, rules: [] } });
    renderCard();

    await screen.findByText("Who they are");
    expect(screen.queryByText("Rules")).toBeNull();
  });

  it("leaves the footer out when nobody is recorded", async () => {
    stubFetch({
      "/api/personas/aria": { ...ARIA, created_by: null, created_at: null },
    });
    const { container } = renderCard();

    await screen.findByText("Who they are");
    expect(container.querySelector(".hover-card__footer")).toBeNull();
  });

  it("links to the personas page for the whole thing", async () => {
    stubFetch({ "/api/personas/aria": ARIA });
    renderCard();

    const link = (await screen.findByText(
      "Open the full persona",
    )) as HTMLAnchorElement;
    expect(link.getAttribute("href")).toBe("#/personas");
  });

  it("says so plainly when it cannot load", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("nope", { status: 500 })),
    );
    renderCard();

    expect(await screen.findByText("Could not load this persona.")).not.toBeNull();
  });

  it("asks for nothing at all without a persona id", async () => {
    const fetchMock = vi.fn(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    renderCard(null);

    await Promise.resolve();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
