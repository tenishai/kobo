// @vitest-environment jsdom
import { fireEvent, screen } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import DangerZone from "./DangerZone.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

/** Opens the confirm. Since the page-shell wave the row carries a quiet
 * "Destroy..." and everything that matters -- the copy, the type-the-name
 * field, the armed button -- is inside the dialog behind it. */
async function openConfirm(): Promise<void> {
  await fireEvent.click(screen.getByRole("button", { name: "Destroy..." }));
}

describe("DangerZone arming", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    stubFetch({});
  });

  it("keeps the row quiet until somebody asks: no field, no armed button", () => {
    renderWithClient(DangerZone, { agentName: "warden", status: "ready", error: null });

    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByRole("button", { name: /destroy this agent/i })).toBeNull();
    expect(screen.getByRole("button", { name: "Destroy..." })).not.toBeNull();
  });

  it("keeps Destroy disabled until the exact name is typed", async () => {
    renderWithClient(DangerZone, { agentName: "warden", status: "ready", error: null });
    await openConfirm();
    const button = screen.getByRole("button", { name: /destroy this agent/i });
    expect(button).toHaveProperty("disabled", true);

    const input = screen.getByRole("textbox");
    await fireEvent.input(input, { target: { value: "warde" } });
    expect(button).toHaveProperty("disabled", true);

    await fireEvent.input(input, { target: { value: "Warden" } });
    expect(button).toHaveProperty("disabled", true);

    await fireEvent.input(input, { target: { value: "warden " } });
    expect(button).toHaveProperty("disabled", true);

    await fireEvent.input(input, { target: { value: "warden" } });
    expect(button).toHaveProperty("disabled", false);
  });

  it("forgets a half-typed name when the confirm is closed and opened again", async () => {
    renderWithClient(DangerZone, { agentName: "warden", status: "ready", error: null });
    await openConfirm();
    await fireEvent.input(screen.getByRole("textbox"), { target: { value: "warden" } });
    await fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    await openConfirm();
    expect(screen.getByRole("textbox")).toHaveProperty("value", "");
    expect(screen.getByRole("button", { name: /destroy this agent/i })).toHaveProperty(
      "disabled",
      true,
    );
  });

  it("offers no confirm at all while the destruction is running", () => {
    renderWithClient(DangerZone, { agentName: "warden", status: "destroying", error: null });

    expect(screen.queryByRole("button", { name: "Destroy..." })).toBeNull();
    expect(screen.getByText("Destroying this agent…")).not.toBeNull();
  });

  it("resumes a failed destruction without asking for the name again", () => {
    renderWithClient(DangerZone, {
      agentName: "warden",
      status: "destroy_failed",
      error: "The namespace would not go.",
    });

    expect(screen.getByRole("button", { name: "Destroy again" })).not.toBeNull();
    expect(screen.getByText("The namespace would not go.")).not.toBeNull();
    expect(screen.queryByRole("textbox")).toBeNull();
  });
});

describe("DangerZone after the room accepts", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    stubFetch({ "/api/agents/warden": { status: "destroying" } });
  });

  it("closes the confirm once the destruction is accepted", async () => {
    renderWithClient(DangerZone, { agentName: "warden", status: "ready", error: null });
    await openConfirm();
    await fireEvent.input(screen.getByRole("textbox"), { target: { value: "warden" } });
    await fireEvent.click(screen.getByRole("button", { name: /destroy this agent/i }));

    await vi.waitFor(() => {
      expect(screen.queryByRole("textbox")).toBeNull();
      expect(screen.queryByRole("button", { name: /destroy this agent/i })).toBeNull();
    });
  });
});
