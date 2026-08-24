// @vitest-environment jsdom
import { fireEvent, screen, within } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Personas from "./Personas.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";
import { toasts } from "../lib/toast.svelte";

const HELPER = {
  id: "helper",
  name: "Helper",
  description: "a calm, steady presence who never panics",
  team: "shared",
  tags: ["general"],
  builtin: true,
};

const GROWTH_SCOUT = {
  id: "growth-scout",
  name: "growth-scout",
  description: "a relentless, upbeat hunter for the next signup",
  team: "growth",
  tags: ["acquisition"],
  builtin: false,
};

describe("Personas page", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    toasts.splice(0, toasts.length);
  });

  it("renders active persona entries from the search: name primary, description as the grey secondary line, team + tag chips, builtin badge only where true", async () => {
    stubFetch({
      "/api/personas/search": { results: [HELPER, GROWTH_SCOUT] },
      "/api/": {},
    });
    const { container } = renderWithClient(Personas);

    expect(await screen.findByText("Helper")).not.toBeNull();
    // The display hierarchy the naming law is about: the card's own
    // .persona-card__name is the primary line, .persona-card__description
    // the secondary one directly beneath -- pinned by class rather than
    // visual style, since component styles never make it into jsdom's DOM
    // under this project's vite/vitest setup (Sidebar.test.ts's own doc
    // comment notes the same limitation).
    const helperCard = screen.getByText("Helper").closest(".persona-card") as HTMLElement;
    expect(helperCard.querySelector(".persona-card__name")?.textContent).toBe("Helper");
    expect(helperCard.querySelector(".persona-card__description")?.textContent).toBe(
      "a calm, steady presence who never panics",
    );
    expect(within(helperCard).getByText("shared")).not.toBeNull();
    expect(within(helperCard).getByText("general")).not.toBeNull();
    expect(within(helperCard).getByText("builtin")).not.toBeNull();

    const scoutCard = screen.getByText("growth-scout").closest(".persona-card") as HTMLElement;
    expect(within(scoutCard).getByText("acquisition")).not.toBeNull();
    // A non-builtin persona carries no builtin badge at all.
    expect(container.querySelectorAll(".persona-card__badge--builtin")).toHaveLength(1);
  });

  it("shows the empty state when the active search returns nothing", async () => {
    stubFetch({
      "/api/personas/search": { results: [] },
      "/api/": {},
    });
    renderWithClient(Personas);

    expect(await screen.findByText("No personas yet.")).not.toBeNull();
  });

  it("Archive calls archivePersona: POSTs /api/personas/{id}/archive", async () => {
    stubFetch({
      "/api/personas/search": { results: [HELPER] },
      "/api/personas/helper/archive": { ...HELPER, archived: true },
      "/api/": {},
    });
    renderWithClient(Personas);

    const archiveButton = await screen.findByRole("button", { name: "Archive" });
    await fireEvent.click(archiveButton);

    await vi.waitFor(() => {
      expect(
        vi
          .mocked(fetch)
          .mock.calls.some(
            ([url, init]) =>
              String(url).includes("/api/personas/helper/archive") &&
              (init as RequestInit | undefined)?.method === "POST",
          ),
      ).toBe(true);
    });
  });

  it("surfaces a 403 refusal's exact server error copy as a toast, naming the gate requirement", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/personas/helper/archive") && init?.method === "POST") {
          return new Response(
            JSON.stringify({
              error: "only a room operator can archive a persona filed under the commons",
            }),
            { status: 403, headers: { "Content-Type": "application/json" } },
          );
        }
        if (url.includes("/api/personas/search")) {
          return new Response(JSON.stringify({ results: [HELPER] }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
        return new Response(JSON.stringify({}), { status: 200 });
      }),
    );
    renderWithClient(Personas);

    const archiveButton = await screen.findByRole("button", { name: "Archive" });
    await fireEvent.click(archiveButton);

    await vi.waitFor(() => {
      expect(
        toasts.some(
          (t) =>
            t.kind === "error" &&
            t.body === "only a room operator can archive a persona filed under the commons",
        ),
      ).toBe(true);
    });
  });

  it('the "Show archived" toggle is fetch-gated and reveals the archived section with Unarchive controls', async () => {
    stubFetch({
      "/api/personas/search": { results: [] },
      "/api/personas/archived": {
        personas: [{ ...HELPER, archived: true }],
      },
      "/api/": {},
    });
    renderWithClient(Personas);

    await screen.findByText("No personas yet.");
    // Not fetched, and not rendered, before the toggle is switched on.
    expect(screen.queryByText("Helper")).toBeNull();
    expect(
      vi.mocked(fetch).mock.calls.some(([url]) => String(url).includes("/api/personas/archived")),
    ).toBe(false);

    const toggle = screen.getByRole("button", { name: "Show archived" });
    await fireEvent.click(toggle);

    expect(await screen.findByText("Helper")).not.toBeNull();
    expect(
      vi.mocked(fetch).mock.calls.some(([url]) => String(url).includes("/api/personas/archived")),
    ).toBe(true);
    expect(screen.getByRole("button", { name: "Unarchive" })).not.toBeNull();
    expect(screen.queryByRole("button", { name: "Archive" })).toBeNull();
  });

  it("Unarchive calls unarchivePersona: POSTs /api/personas/{id}/unarchive", async () => {
    stubFetch({
      "/api/personas/search": { results: [] },
      "/api/personas/archived": { personas: [{ ...HELPER, archived: true }] },
      "/api/personas/helper/unarchive": { ...HELPER },
      "/api/": {},
    });
    renderWithClient(Personas);

    await fireEvent.click(await screen.findByRole("button", { name: "Show archived" }));
    const unarchiveButton = await screen.findByRole("button", { name: "Unarchive" });
    await fireEvent.click(unarchiveButton);

    await vi.waitFor(() => {
      expect(
        vi
          .mocked(fetch)
          .mock.calls.some(
            ([url, init]) =>
              String(url).includes("/api/personas/helper/unarchive") &&
              (init as RequestInit | undefined)?.method === "POST",
          ),
      ).toBe(true);
    });
  });
});
