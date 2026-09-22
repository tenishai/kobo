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

describe("DangerZone copy by kind (connected-agents design 2026-09-21)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    stubFetch({});
  });

  it("tells a composed agent's owner about the archived repo and the Slack presence", () => {
    renderWithClient(DangerZone, { agentName: "warden", status: "ready", error: null });

    expect(
      screen.getByText(
        "This destroys the running agent, its platform identity, and its Slack presence. The repo is archived, not deleted. Memories die with the namespace.",
      ),
    ).not.toBeNull();
  });

  it("says the same for an agent whose kind is named composed", () => {
    renderWithClient(DangerZone, {
      agentName: "warden",
      status: "ready",
      error: null,
      kind: "composed",
    });

    expect(screen.getByText(/The repo is archived, not deleted\./)).not.toBeNull();
  });

  it("promises a connected agent's team its repository, and claims no chat app", () => {
    renderWithClient(DangerZone, {
      agentName: "warden",
      status: "ready",
      error: null,
      kind: "connected",
    });

    expect(
      screen.getByText(
        "This destroys the running agent and its platform identity. The repository belongs to your team and is left as it is. Memories die with the namespace.",
      ),
    ).not.toBeNull();
    expect(screen.queryByText(/archived/)).toBeNull();
    expect(screen.queryByText(/Slack/)).toBeNull();
    // Still the same confirmation: the copy changes, the gesture does not.
    expect(
      screen.getByRole("button", { name: /destroy this agent/i }),
    ).toHaveProperty("disabled", true);
  });
});
