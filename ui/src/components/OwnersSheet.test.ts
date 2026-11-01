// @vitest-environment jsdom
// Owners (issue #368): the People block of the Access tab as a sheet off the
// page header. Same routes, same roles, same add-person autocomplete.
import { fireEvent, screen } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import OwnersSheet from "./OwnersSheet.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

const EMPTY_GRANTS = { grants: [] };
const OPEN = { agentName: "warden", open: true, onclose: () => {} };

describe("OwnersSheet", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("is a dialog named Owners with the add-person form on screen and the role select posting the chosen role", async () => {
    stubFetch({ "/api/agents/warden/grants": EMPTY_GRANTS });
    renderWithClient(OwnersSheet, OPEN);

    expect(await screen.findByRole("dialog", { name: "Owners" })).not.toBeNull();
    expect(await screen.findByText("Nobody has been added yet.")).not.toBeNull();

    await fireEvent.input(screen.getByLabelText("Add someone by email"), {
      target: { value: "lead@example.com" },
    });
    await fireEvent.change(screen.getByLabelText("Role"), { target: { value: "manage" } });
    await fireEvent.click(screen.getByRole("button", { name: "Add" }));

    const call = vi
      .mocked(fetch)
      .mock.calls.find(
        ([url, init]) =>
          String(url) === "/api/agents/warden/grants" &&
          (init as RequestInit | undefined)?.method === "POST",
      );
    expect(call).toBeDefined();
    const body = JSON.parse((call as [RequestInfo | URL, RequestInit])[1].body as string);
    expect(body).toEqual({ email: "lead@example.com", role: "manage" });
  });

  it("lists the grants with their role and lets an owner remove one", async () => {
    stubFetch({
      "/api/agents/warden/grants": {
        grants: [{ email: "guest@example.com", role: "view", added_by: "owner@example.com" }],
      },
    });
    renderWithClient(OwnersSheet, OPEN);

    expect(await screen.findByText("guest@example.com")).not.toBeNull();
    // The role chip on the row, besides the note explaining the role.
    expect(document.querySelector(".owners__chip")?.textContent).toBe("Can see");

    await fireEvent.click(screen.getByRole("button", { name: "Remove guest@example.com" }));

    expect(
      vi
        .mocked(fetch)
        .mock.calls.some(
          ([url, init]) =>
            String(url).includes("/api/agents/warden/grants/guest%40example.com") &&
            (init as RequestInit | undefined)?.method === "DELETE",
        ),
    ).toBe(true);
  });

  it("debounces the directory search as the person types, shows Name <email> rows, and a pick fills the field", async () => {
    stubFetch({
      "/api/agents/warden/grants": EMPTY_GRANTS,
      "/api/people/search": {
        people: [{ name: "Alice Smith", email: "alice@example.com" }],
      },
    });
    renderWithClient(OwnersSheet, OPEN);

    const input = (await screen.findByLabelText("Add someone by email")) as HTMLInputElement;
    await fireEvent.input(input, { target: { value: "al" } });

    const suggestion = await screen.findByText(
      "Alice Smith <alice@example.com>",
      {},
      { timeout: 2000 },
    );
    expect(
      vi.mocked(fetch).mock.calls.some(([url]) => String(url).includes("/api/people/search?q=al")),
    ).toBe(true);

    await fireEvent.click(suggestion);

    expect(input.value).toBe("alice@example.com");
    expect(screen.queryByText("Alice Smith <alice@example.com>")).toBeNull();
  });

  it("never searches below two characters", async () => {
    stubFetch({ "/api/agents/warden/grants": EMPTY_GRANTS });
    renderWithClient(OwnersSheet, OPEN);

    await fireEvent.input(await screen.findByLabelText("Add someone by email"), {
      target: { value: "a" },
    });
    // Give the 250ms debounce a chance to have fired if it were going to.
    await new Promise((resolve) => setTimeout(resolve, 300));

    expect(
      vi.mocked(fetch).mock.calls.some(([url]) => String(url).includes("/api/people/search")),
    ).toBe(false);
  });

  it("keeps the typed-email entry working with a quiet note when the directory is unavailable", async () => {
    stubFetch({
      "/api/agents/warden/grants": EMPTY_GRANTS,
      "/api/people/search": { people: [], unavailable: true },
    });
    renderWithClient(OwnersSheet, OPEN);

    await fireEvent.input(await screen.findByLabelText("Add someone by email"), {
      target: { value: "someone@example.com" },
    });
    expect(
      await screen.findByText(/directory search is unavailable/i, {}, { timeout: 2000 }),
    ).not.toBeNull();

    await fireEvent.click(screen.getByRole("button", { name: "Add" }));

    const call = vi
      .mocked(fetch)
      .mock.calls.find(
        ([url, init]) =>
          String(url) === "/api/agents/warden/grants" &&
          (init as RequestInit | undefined)?.method === "POST",
      );
    expect(call).toBeDefined();
    const body = JSON.parse((call as [RequestInfo | URL, RequestInit])[1].body as string);
    expect(body).toEqual({ email: "someone@example.com", role: "view" });
  });

  it("rests every control that would change the agent while frozen", async () => {
    stubFetch({
      "/api/agents/warden/grants": {
        grants: [{ email: "guest@example.com", role: "view", added_by: "owner@example.com" }],
      },
    });
    renderWithClient(OwnersSheet, { ...OPEN, frozen: true });

    await screen.findByText("guest@example.com");
    expect((screen.getByLabelText("Add someone by email") as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByLabelText("Role") as HTMLSelectElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "Add" }) as HTMLButtonElement).disabled).toBe(true);
    expect(
      (screen.getByRole("button", { name: "Remove guest@example.com" }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });
});
