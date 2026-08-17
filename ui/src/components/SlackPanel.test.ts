// @vitest-environment jsdom
import { fireEvent, screen } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import SlackPanel from "./SlackPanel.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

const IDLE_SLACK = { app_id: null, channels: [], state: "idle" as const };

describe("SlackPanel", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    // No dedicated query of its own is ever exercised here except the
    // delivery chip (disabled until a sha exists) — the bare catch-all is
    // enough, same reasoning as FrozenPanels.test.ts's SlackPanel case.
    stubFetch({ "/api/": {} });
  });

  it("idle state, non-admin: channels lane renders with one empty row and Add another channel; no token fields anywhere", () => {
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: IDLE_SLACK,
      adminVisible: false,
      frozen: false,
    });

    const rows = screen.getAllByRole("textbox");
    expect(rows).toHaveLength(1);
    expect((rows[0] as HTMLInputElement).value).toBe("");
    expect(screen.getByRole("button", { name: "Add another channel" })).not.toBeNull();
    expect(screen.queryByLabelText(/bot token/i)).toBeNull();
    expect(screen.queryByLabelText(/app-level token/i)).toBeNull();
    expect(screen.queryByRole("button", { name: /finish setup/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /connect to slack/i })).toBeNull();
  });

  it('waiting state shows "Channels saved. Waiting on an operator to finish the connection."', () => {
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: { app_id: "A123", channels: ["C123456"], state: "waiting" },
      adminVisible: false,
      frozen: false,
    });

    expect(
      screen.getByText("Channels saved. Waiting on an operator to finish the connection."),
    ).not.toBeNull();
  });

  it("admin + waiting: setup lane renders bot/app token fields and Finish setup", () => {
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: { app_id: "A123", channels: ["C123456"], state: "waiting" },
      adminVisible: true,
      frozen: false,
    });

    expect(screen.getByLabelText("Bot token")).not.toBeNull();
    expect(screen.getByLabelText("App-level token")).not.toBeNull();
    expect(screen.getByRole("button", { name: "Finish setup" })).not.toBeNull();
  });

  it("add-another adds a row; remove removes it; Save disabled while every row is empty", async () => {
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: IDLE_SLACK,
      adminVisible: false,
      frozen: false,
    });

    const saveButton = screen.getByRole("button", { name: "Save" }) as HTMLButtonElement;
    expect(saveButton.disabled).toBe(true);

    await fireEvent.click(screen.getByRole("button", { name: "Add another channel" }));
    expect(screen.getAllByRole("textbox")).toHaveLength(2);
    expect(saveButton.disabled).toBe(true); // both rows still empty

    await fireEvent.input(screen.getAllByRole("textbox")[1], {
      target: { value: "C123456" },
    });
    expect(saveButton.disabled).toBe(false);

    const removeButtons = screen.getAllByRole("button", { name: /remove channel/i });
    await fireEvent.click(removeButtons[1]);
    expect(screen.getAllByRole("textbox")).toHaveLength(1);
    expect(saveButton.disabled).toBe(true); // back to the one empty row
  });

  it("frozen disables every control: rows, add, save, and tokens", () => {
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: { app_id: "A123", channels: ["C123456"], state: "waiting" },
      adminVisible: true,
      frozen: true,
    });

    for (const row of screen.getAllByRole("textbox")) {
      expect((row as HTMLInputElement).disabled).toBe(true);
    }
    expect(
      (screen.getByRole("button", { name: "Add another channel" }) as HTMLButtonElement).disabled,
    ).toBe(true);
    expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    for (const button of screen.getAllByRole("button", { name: /remove channel/i })) {
      expect((button as HTMLButtonElement).disabled).toBe(true);
    }
    expect((screen.getByLabelText("Bot token") as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByLabelText("App-level token") as HTMLInputElement).disabled).toBe(true);
    expect(
      (screen.getByRole("button", { name: "Finish setup" }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it("connected state shows \"Connected.\" and the saved channel list", () => {
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: { app_id: "A123", channels: ["C123456", "C654321"], state: "connected" },
      adminVisible: false,
      frozen: false,
    });

    expect(screen.getByText("Connected.")).not.toBeNull();
    const rows = screen.getAllByRole("textbox") as HTMLInputElement[];
    expect(rows.map((row) => row.value)).toEqual(["C123456", "C654321"]);
  });
});
