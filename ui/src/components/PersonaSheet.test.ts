// @vitest-environment jsdom
import { fireEvent, screen } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import PersonaSheet from "./PersonaSheet.svelte";
import { renderWithClient } from "../test-utils/render";

const ARIA = {
  id: "aria",
  name: "aria",
  team: "sre",
  builtin: false,
  tags: ["ops"],
  who_they_are: "A calm operator.",
  what_they_do: "Checks first.\n\nActs second.",
  rules: ["one", "two", "three", "four", "five", "six", "seven"],
  created_by: "kyle",
  created_at: "2026-09-01T00:00:00Z",
  truncated: false,
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** whoami, the movable list and the persona; anything else is an error. */
function stubRoom(movable: string[], operator = false): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/personas/movable")) {
        return json({ ids: movable });
      }
      if (url.includes("/api/personas/aria")) {
        return json(ARIA);
      }
      if (url.includes("/api/whoami")) {
        return json({ email: "k@example.org", operator, groups: ["sre"], display_name: "K" });
      }
      if (url.includes("/api/groups")) {
        return json({ groups: [] });
      }
      throw new Error(`unstubbed fetch: ${url}`);
    }),
  );
}

describe("PersonaSheet (personas-in-place wave)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows every rule and every paragraph, and Back returns to the list", async () => {
    stubRoom([]);
    const onback = vi.fn();
    renderWithClient(PersonaSheet, { personaId: "aria", onback });

    await screen.findByText("A calm operator.");
    expect(screen.getByText("Checks first.")).not.toBeNull();
    expect(screen.getByText("Acts second.")).not.toBeNull();
    expect(screen.getAllByRole("listitem")).toHaveLength(7);
    expect(screen.queryByText(/and \d+ more/)).toBeNull();
    expect(screen.getByText(/Made by kyle/)).not.toBeNull();

    await fireEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(onback).toHaveBeenCalled();
  });

  it("keeps the manage row from someone who may neither move nor archive", async () => {
    stubRoom([]);
    renderWithClient(PersonaSheet, { personaId: "aria", onback: () => {} });

    await screen.findByText("A calm operator.");
    expect(screen.queryByRole("button", { name: "Archive" })).toBeNull();
    expect(screen.queryByLabelText("Move to")).toBeNull();
  });

  it("offers Move to and Archive when the server says the persona is theirs to re-file", async () => {
    stubRoom(["aria"]);
    renderWithClient(PersonaSheet, { personaId: "aria", onback: () => {} });

    await screen.findByText("A calm operator.");
    expect(await screen.findByRole("button", { name: "Archive" })).not.toBeNull();
    const move = (await screen.findByLabelText("Move to")) as HTMLSelectElement;
    // The persona's own team is not a destination; everyone is.
    const options = Array.from(move.options).map((o) => o.value);
    expect(options).toEqual(["", "shared"]);
  });
});
