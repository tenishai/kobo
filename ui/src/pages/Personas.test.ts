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

  describe("Move to... (persona team scope)", () => {
    /** whoami, the movable list, the search and the PATCH, with the PATCH
     * answer up to the test. */
    function stubMoveRoom(
      movable: string[],
      patch: { status: number; body: unknown } = {
        status: 200,
        body: { ...GROWTH_SCOUT, team: "qa" },
      },
    ): void {
      vi.stubGlobal(
        "fetch",
        vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = String(input);
          const json = (body: unknown, status = 200) =>
            new Response(JSON.stringify(body), {
              status,
              headers: { "Content-Type": "application/json" },
            });
          if (init?.method === "PATCH") {
            return json(patch.body, patch.status);
          }
          if (url.includes("/api/personas/movable")) {
            return json({ ids: movable });
          }
          if (url.includes("/api/personas/search")) {
            return json({ results: [HELPER, GROWTH_SCOUT] });
          }
          if (url.includes("/api/whoami")) {
            return json({
              email: "me@example.com",
              operator: false,
              groups: ["growth", "qa"],
              groups_unavailable: false,
            });
          }
          return json({});
        }),
      );
    }

    function patchCalls(): Array<[string, RequestInit]> {
      return vi
        .mocked(fetch)
        .mock.calls.filter(([, init]) => (init as RequestInit | undefined)?.method === "PATCH")
        .map(([url, init]) => [String(url), init as RequestInit]);
    }

    it("lists every group, not just the caller's", async () => {
      stubMoveRoom([]);
      renderWithClient(Personas);
      await screen.findByText("Helper");

      const searches = vi
        .mocked(fetch)
        .mock.calls.map(([url]) => String(url))
        .filter((url) => url.includes("/api/personas/search"));
      expect(searches.every((url) => url.includes("scope=all"))).toBe(true);
    });

    it("appears only on personas the caller may move", async () => {
      stubMoveRoom(["growth-scout"]);
      renderWithClient(Personas);

      const move = await screen.findByLabelText("Move growth-scout to");
      expect(move.closest(".persona-card")?.textContent).toContain("growth-scout");
      expect(screen.queryByLabelText("Move Helper to")).toBeNull();
    });

    it("never appears on a built-in, even if the server listed it", async () => {
      stubMoveRoom(["helper", "growth-scout"]);
      renderWithClient(Personas);

      await screen.findByLabelText("Move growth-scout to");
      expect(screen.queryByLabelText("Move Helper to")).toBeNull();
    });

    it("offers the caller's other groups and everyone, never where it already is", async () => {
      stubMoveRoom(["growth-scout"]);
      renderWithClient(Personas);

      const move = (await screen.findByLabelText("Move growth-scout to")) as HTMLSelectElement;
      await vi.waitFor(() => expect(move.options.length).toBeGreaterThan(2));

      expect([...move.options].map((o) => o.textContent?.trim())).toEqual([
        "Move to...",
        "qa",
        "Everyone (shared)",
      ]);
    });

    it("moving sends just the new team and says where it went", async () => {
      stubMoveRoom(["growth-scout"]);
      renderWithClient(Personas);

      const move = (await screen.findByLabelText("Move growth-scout to")) as HTMLSelectElement;
      await vi.waitFor(() => expect(move.options.length).toBeGreaterThan(2));
      await fireEvent.change(move, { target: { value: "qa" } });

      await vi.waitFor(() => expect(patchCalls()).toHaveLength(1));
      const [url, init] = patchCalls()[0];
      expect(url).toBe("/api/personas/growth-scout");
      expect(JSON.parse(String(init.body))).toEqual({ team: "qa" });
      await vi.waitFor(() => {
        expect(
          toasts.some((t) => t.kind === "success" && t.title === "Moved growth-scout to qa"),
        ).toBe(true);
      });
    });

    it("a refusal shows the server's own reason as a toast", async () => {
      stubMoveRoom(["growth-scout"], {
        status: 403,
        body: { error: "you must be a member of team qa to file a persona under it" },
      });
      renderWithClient(Personas);

      const move = (await screen.findByLabelText("Move growth-scout to")) as HTMLSelectElement;
      await vi.waitFor(() => expect(move.options.length).toBeGreaterThan(2));
      await fireEvent.change(move, { target: { value: "qa" } });

      await vi.waitFor(() => {
        expect(
          toasts.some(
            (t) =>
              t.kind === "error" &&
              t.title === "Couldn't move that persona" &&
              t.body === "you must be a member of team qa to file a persona under it",
          ),
        ).toBe(true);
      });
      expect(move.value).toBe("");
    });
  });

  describe("built-ins first (built-ins are the main personas)", () => {
    const QA_CRITIC = {
      id: "qa-critic",
      name: "QA Critic",
      description: "finds the bug first",
      team: "qa",
      tags: [],
      builtin: false,
    };
    const BEE = {
      id: "bee",
      name: "Bee",
      description: "busy",
      team: "shared",
      tags: [],
      builtin: true,
    };

    /** Headings and card names in page order, headings as `heading:<text>`. */
    function pageOrder(container: HTMLElement): string[] {
      return Array.from(
        container.querySelectorAll(".personas-page__group, .persona-card__name"),
      ).map((el) =>
        el.classList.contains("personas-page__group")
          ? `heading:${el.textContent?.trim()}`
          : (el.textContent ?? ""),
      );
    }

    it("groups the cards under Built-in, then From all groups", async () => {
      stubFetch({
        "/api/personas/search": { results: [QA_CRITIC, HELPER, GROWTH_SCOUT, BEE] },
        "/api/": {},
      });
      const { container } = renderWithClient(Personas);
      await screen.findByText("Helper");

      expect(pageOrder(container)).toEqual([
        "heading:Built-in",
        "Helper",
        "Bee",
        "heading:From all groups",
        "QA Critic",
        "growth-scout",
      ]);
    });

    it("drops a group's heading when it has no personas", async () => {
      stubFetch({
        "/api/personas/search": { results: [GROWTH_SCOUT] },
        "/api/": {},
      });
      const { container } = renderWithClient(Personas);
      await screen.findByText("growth-scout");

      expect(pageOrder(container)).toEqual(["heading:From all groups", "growth-scout"]);
    });
  });
});
