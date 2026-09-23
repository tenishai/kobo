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

describe("DangerZone copy by kind (connected-agents design 2026-09-21)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    stubFetch({});
  });

  it("tells a composed agent's owner about the archived repo and the Slack presence", async () => {
    renderWithClient(DangerZone, { agentName: "warden", status: "ready", error: null });
    await openConfirm();

    expect(
      screen.getByText(
        "This destroys the running agent, its platform identity, and its Slack presence. The repo is archived, not deleted. Memories die with the namespace.",
      ),
    ).not.toBeNull();
  });

  it("says the same for an agent whose kind is named composed", async () => {
    renderWithClient(DangerZone, {
      agentName: "warden",
      status: "ready",
      error: null,
      kind: "composed",
    });
    await openConfirm();

    expect(screen.getByText(/The repo is archived, not deleted\./)).not.toBeNull();
  });

  it("promises a connected agent's team its repository, and claims no chat app", async () => {
    renderWithClient(DangerZone, {
      agentName: "warden",
      status: "ready",
      error: null,
      kind: "connected",
    });
    await openConfirm();

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

describe("DangerZone — the destroy confirm (modal wave)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    stubFetch({ "/api/": {} });
  });

  it("is a narrow dialog whose action is danger, not the room's filled button", async () => {
    const { container } = renderWithClient(DangerZone, {
      agentName: "warden",
      status: "ready",
      error: null,
    });
    await openConfirm();

    expect(container.querySelector(".modal--narrow")).not.toBeNull();
    const footer = container.querySelector(".modal__footer");
    expect(footer).not.toBeNull();
    // Typing the name stays in the body; the two buttons are in the footer.
    expect(container.querySelector(".modal__body")?.contains(screen.getByRole("textbox"))).toBe(
      true,
    );
    const destroy = screen.getByRole("button", { name: /destroy this agent/i });
    expect(footer?.contains(destroy)).toBe(true);
    expect(destroy.className).toContain("btn--danger");
    // Nothing in a destroy confirm is an invitation.
    expect(container.querySelectorAll(".btn--primary")).toHaveLength(0);
  });
});
