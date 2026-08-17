// @vitest-environment jsdom
import { fireEvent, screen } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import DangerZone from "./DangerZone.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

describe("DangerZone arming", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    stubFetch({});
  });

  it("keeps Destroy disabled until the exact name is typed", async () => {
    renderWithClient(DangerZone, { agentName: "warden", status: "ready", error: null });
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
});
