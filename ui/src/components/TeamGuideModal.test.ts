// @vitest-environment jsdom
//
// "Quick-start team agent" (team-guide spec 2026-09-14): one group, one
// name, an optional voice, and an archetype the server turns into a
// persona. These cover the dialog's own shape (modal idiom wave) and the
// request it sends, which is the part nobody may change by accident.
import { fireEvent, screen, waitFor } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import TeamGuideModal from "./TeamGuideModal.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

function stubGuideRoutes(extra: Record<string, unknown> = {}): void {
  stubFetch({
    "/api/catalog/groups": {
      groups: [
        { name: "shared", kind: "shared" },
        { name: "payments-eng", kind: "engineering" },
      ],
    },
    "/api/agents": { agents: [] },
    ...extra,
    "/api/": {},
  });
}

describe("TeamGuideModal", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("has one filled button, in a footer under the body", async () => {
    stubGuideRoutes();
    const { container } = renderWithClient(TeamGuideModal, {
      open: true,
      onclose: () => {},
    });

    await screen.findByLabelText("Group");

    const footer = container.querySelector(".modal__footer");
    expect(footer).not.toBeNull();
    expect(container.querySelector(".modal__body")?.contains(footer as Node)).toBe(
      false,
    );
    expect(container.querySelectorAll(".btn--primary")).toHaveLength(1);
    const primary = footer?.querySelector(".btn--primary") as HTMLButtonElement;
    expect(primary.textContent?.trim()).toBe("Quick-start agent");
    // Nothing is picked yet, so there is nothing to start.
    expect(primary.disabled).toBe(true);
    expect(screen.getByText("Pick a group first.")).not.toBeNull();
  });

  it("asks for a name and a voice once a group is picked, and sends the archetype", async () => {
    stubGuideRoutes();
    renderWithClient(TeamGuideModal, { open: true, onclose: () => {} });

    const groupSelect = (await screen.findByLabelText("Group")) as HTMLSelectElement;
    await fireEvent.change(groupSelect, { target: { value: "payments-eng" } });

    await fireEvent.input(screen.getByLabelText("Name"), {
      target: { value: "payments-guide" },
    });
    await fireEvent.input(screen.getByLabelText("Voice (optional)"), {
      target: { value: "a patient librarian" },
    });

    const start = screen.getByRole("button", {
      name: "Quick-start agent",
    }) as HTMLButtonElement;
    expect(start.disabled).toBe(false);
    await fireEvent.click(start);

    await waitFor(() => {
      expect(
        vi
          .mocked(fetch)
          .mock.calls.some(
            ([url, init]) =>
              String(url) === "/api/agents" &&
              (init as RequestInit | undefined)?.method === "POST",
          ),
      ).toBe(true);
    });

    const call = vi
      .mocked(fetch)
      .mock.calls.find(
        ([url, init]) =>
          String(url) === "/api/agents" &&
          (init as RequestInit | undefined)?.method === "POST",
      );
    const body = JSON.parse((call?.[1] as RequestInit).body as string);
    expect(body.name).toBe("payments-guide");
    expect(body.group_name).toBe("payments-eng");
    expect(body.archetype).toBe("team_guide");
    expect(body.voice).toBe("a patient librarian");
    expect(body.skills).toEqual([]);
  });

  it("offers the group's existing guide instead of a second one", async () => {
    stubGuideRoutes({
      "/api/agents": {
        agents: [
          {
            name: "payments-guide",
            archetype: "team_guide",
            purpose_tags: ["team-guide", "payments-eng"],
          },
        ],
      },
    });
    renderWithClient(TeamGuideModal, { open: true, onclose: () => {} });

    const groupSelect = (await screen.findByLabelText("Group")) as HTMLSelectElement;
    await fireEvent.change(groupSelect, { target: { value: "payments-eng" } });

    expect(
      await screen.findByText("payments-eng already has a team guide."),
    ).not.toBeNull();
    expect(screen.getByRole("link", { name: "Open payments-guide" })).not.toBeNull();
    expect(
      (screen.getByRole("button", { name: "Quick-start agent" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });
});
