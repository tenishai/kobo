// @vitest-environment jsdom
import { fireEvent, screen, waitFor } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import SlackPanel from "./SlackPanel.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

const IDLE_SLACK = { app_id: null, channels: [], state: "idle" as const };

const MANIFEST_FIXTURE = {
  display_information: { name: "warden", description: "A keeper agent, reachable in Slack." },
  features: { bot_user: { display_name: "warden" } },
};

describe("SlackPanel", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    // No dedicated query is exercised by most cases here except the
    // delivery chip and the manifest reveal, both disabled until something
    // triggers them (a sha existing, the manifest control being clicked) —
    // the bare catch-all is enough for those, same reasoning as
    // FrozenPanels.test.ts's SlackPanel case.
    stubFetch({ "/api/": {} });
  });

  afterEach(() => {
    // jsdom has no `clipboard` own property by default (confirmed against
    // the installed jsdom directly) — deleting it after every test restores
    // that pristine state regardless of which test defined it, so the
    // clipboard-success test below can't leak into its neighbors.
    Reflect.deleteProperty(navigator, "clipboard");
  });

  it("idle state: channels lane renders with one empty row and Add another channel", () => {
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: IDLE_SLACK,
      frozen: false,
    });

    const rows = screen.getAllByLabelText(/^Channel \d+$/);
    expect(rows).toHaveLength(1);
    expect((rows[0] as HTMLInputElement).value).toBe("");
    expect(screen.getByRole("button", { name: "Add another channel" })).not.toBeNull();
  });

  it('waiting state shows "Channels saved. Waiting on an operator to finish the connection."', () => {
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: { app_id: "A123", channels: ["C123456"], state: "waiting" },
      frozen: false,
    });

    expect(
      screen.getByText("Channels saved. Waiting on an operator to finish the connection."),
    ).not.toBeNull();
  });

  it("setup section renders unconditionally (no gate): App ID, bot token, app-level token fields and Finish setup", () => {
    // Slack self-service (2026-08-17): this used to require adminVisible.
    // The prop is gone entirely now — this is a plain, non-gated mount.
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: IDLE_SLACK,
      frozen: false,
    });

    expect(screen.getByLabelText("App ID")).not.toBeNull();
    expect(screen.getByLabelText("Bot token")).not.toBeNull();
    expect(screen.getByLabelText("App-level token")).not.toBeNull();
    expect(screen.getByRole("button", { name: "Finish setup" })).not.toBeNull();
    expect(screen.queryByRole("button", { name: /connect to slack/i })).toBeNull();
  });

  it("App ID field is first, before Bot token and App-level token", () => {
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: IDLE_SLACK,
      frozen: false,
    });

    const appId = screen.getByLabelText("App ID");
    const botToken = screen.getByLabelText("Bot token");
    const appToken = screen.getByLabelText("App-level token");

    expect(
      appId.compareDocumentPosition(botToken) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      botToken.compareDocumentPosition(appToken) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("shows a client-side App ID format hint matching the server's own copy (it starts with A)", () => {
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: IDLE_SLACK,
      frozen: false,
    });

    expect(screen.getByText("Starts with A, like A0123456789.")).not.toBeNull();
  });

  it("renders the numbered setup instructions, asserting two lines verbatim", () => {
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: IDLE_SLACK,
      frozen: false,
    });

    expect(
      screen.getByText('Choose "From a manifest", then select your workspace.'),
    ).not.toBeNull();
    expect(
      screen.getByText(
        'Back on "Install App", copy the Bot User OAuth Token. It starts with xoxb-.',
      ),
    ).not.toBeNull();
  });

  it('adds a final numbered step telling the human to /invite the agent\'s own bot, by name, into each channel', () => {
    // The runtime's slack transport never auto-joins channels: a human has
    // to run /invite themselves. This is the last step in the ceremony,
    // after the app is installed and both tokens are in hand, and it uses
    // this agent's actual name (same personalization discipline as the
    // manifest itself, which is rendered per-agent).
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: IDLE_SLACK,
      frozen: false,
    });

    expect(
      screen.getByText(
        "In each Slack channel you want the agent in, type /invite @warden, then paste that channel's ID above.",
      ),
    ).not.toBeNull();
  });

  it('"Show the manifest" fetches lazily via slackManifestQuery, only once opened (not on mount)', async () => {
    // Order matters: stubFetch matches by url.includes, so the specific
    // manifest path must come before the "/api/" catch-all.
    stubFetch({
      "/api/agents/warden/slack/manifest": { manifest: MANIFEST_FIXTURE },
      "/api/": {},
    });

    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: IDLE_SLACK,
      frozen: false,
    });

    // Not fetched on mount.
    expect(
      vi.mocked(fetch).mock.calls.some(([url]) => String(url).includes("/slack/manifest")),
    ).toBe(false);
    expect(screen.queryByText(/keeper agent/)).toBeNull();

    await fireEvent.click(screen.getByRole("button", { name: "Show the manifest" }));

    expect(await screen.findByText(/keeper agent/)).not.toBeNull();
    expect(
      vi.mocked(fetch).mock.calls.some(([url]) => String(url).includes("/slack/manifest")),
    ).toBe(true);
  });

  it('"Copy the manifest" is disabled until the manifest has loaded, then writes the exact rendered text to the clipboard and shows "Copied."', async () => {
    stubFetch({
      "/api/agents/warden/slack/manifest": { manifest: MANIFEST_FIXTURE },
      "/api/": {},
    });
    const writeText = vi.fn().mockResolvedValue(undefined);
    // jsdom (confirmed directly against the installed package) has no
    // `navigator.clipboard` at all, own or inherited, so `defineProperty`
    // straight onto the real `navigator` is the accurate stub here — it
    // leaves the rest of `navigator` (userAgent, etc.) intact, unlike
    // `vi.stubGlobal("navigator", {...})`, which would replace the whole
    // object wholesale.
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });

    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: IDLE_SLACK,
      frozen: false,
    });

    const copyButton = screen.getByRole("button", {
      name: "Copy the manifest",
    }) as HTMLButtonElement;
    expect(copyButton.disabled).toBe(true); // manifest not loaded yet

    await fireEvent.click(screen.getByRole("button", { name: "Show the manifest" }));
    await screen.findByText(/keeper agent/);
    expect(copyButton.disabled).toBe(false);

    await fireEvent.click(copyButton);

    expect(writeText).toHaveBeenCalledWith(JSON.stringify(MANIFEST_FIXTURE, null, 2));
    expect(await screen.findByText("Copied.")).not.toBeNull();
  });

  it('"Copy the manifest" shows the fallback note when the clipboard write is unavailable', async () => {
    stubFetch({
      "/api/agents/warden/slack/manifest": { manifest: MANIFEST_FIXTURE },
      "/api/": {},
    });
    // No stub here on purpose: jsdom's own `navigator.clipboard` is already
    // undefined, which is the real-world failure mode this covers (a
    // non-secure context).

    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: IDLE_SLACK,
      frozen: false,
    });

    await fireEvent.click(screen.getByRole("button", { name: "Show the manifest" }));
    await screen.findByText(/keeper agent/);

    await fireEvent.click(screen.getByRole("button", { name: "Copy the manifest" }));

    expect(await screen.findByText("Copy failed. Select the text instead.")).not.toBeNull();
  });

  it("Finish setup stays disabled until App ID, bot token, and app token are all filled", async () => {
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: { app_id: null, channels: ["C123456"], state: "idle" },
      frozen: false,
    });

    const finish = screen.getByRole("button", { name: "Finish setup" }) as HTMLButtonElement;
    expect(finish.disabled).toBe(true);

    await fireEvent.input(screen.getByLabelText("Bot token"), {
      target: { value: "xoxb-secret" },
    });
    await fireEvent.input(screen.getByLabelText("App-level token"), {
      target: { value: "xapp-secret" },
    });
    expect(finish.disabled).toBe(true); // still missing the App ID

    await fireEvent.input(screen.getByLabelText("App ID"), {
      target: { value: "A1234567" },
    });
    expect(finish.disabled).toBe(false);
  });

  it("Finish setup with zero channels keeps the pasted App ID and both tokens, and shows the plain-language warning", async () => {
    // Real flow this pins: App ID, bot token, and app-level token filled in,
    // but no channel ever added — the channels lane still has just its one
    // empty row. The server's own client-side-shaped guard (POST
    // .../slack/connect, 409 "Add at least one channel first.") is what
    // actually catches this, since `canFinish` itself doesn't look at
    // channels. Regression coverage for the bug where all three pasted
    // secrets used to vanish on this exact failure.
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/slack/connect")) {
          return new Response(
            JSON.stringify({ error: "Add at least one channel first." }),
            { status: 409, headers: { "Content-Type": "application/json" } },
          );
        }
        return new Response(JSON.stringify({}), { status: 200 });
      }),
    );

    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: IDLE_SLACK, // channels: []
      frozen: false,
    });

    await fireEvent.input(screen.getByLabelText("App ID"), {
      target: { value: "A1234567" },
    });
    await fireEvent.input(screen.getByLabelText("Bot token"), {
      target: { value: "xoxb-secret" },
    });
    await fireEvent.input(screen.getByLabelText("App-level token"), {
      target: { value: "xapp-secret" },
    });

    await fireEvent.click(screen.getByRole("button", { name: "Finish setup" }));

    expect(
      await screen.findByText(
        "Add at least one channel above, then press Finish setup again. Your values are kept.",
      ),
    ).not.toBeNull();

    expect((screen.getByLabelText("App ID") as HTMLInputElement).value).toBe("A1234567");
    expect((screen.getByLabelText("Bot token") as HTMLInputElement).value).toBe("xoxb-secret");
    expect((screen.getByLabelText("App-level token") as HTMLInputElement).value).toBe(
      "xapp-secret",
    );
  });

  it("Finish setup clears the App ID and both tokens only after a successful connect", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/slack/connect")) {
          return new Response(JSON.stringify({ sha: "abc1234" }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
        return new Response(JSON.stringify({}), { status: 200 });
      }),
    );

    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: { app_id: null, channels: ["C123456"], state: "idle" },
      frozen: false,
    });

    await fireEvent.input(screen.getByLabelText("App ID"), {
      target: { value: "A1234567" },
    });
    await fireEvent.input(screen.getByLabelText("Bot token"), {
      target: { value: "xoxb-secret" },
    });
    await fireEvent.input(screen.getByLabelText("App-level token"), {
      target: { value: "xapp-secret" },
    });

    await fireEvent.click(screen.getByRole("button", { name: "Finish setup" }));

    await waitFor(() => {
      expect((screen.getByLabelText("App ID") as HTMLInputElement).value).toBe("");
    });
    expect((screen.getByLabelText("Bot token") as HTMLInputElement).value).toBe("");
    expect((screen.getByLabelText("App-level token") as HTMLInputElement).value).toBe("");
  });

  it("add-another adds a row; remove removes it; Save disabled while every row is empty", async () => {
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: IDLE_SLACK,
      frozen: false,
    });

    const saveButton = screen.getByRole("button", { name: "Save" }) as HTMLButtonElement;
    expect(saveButton.disabled).toBe(true);

    await fireEvent.click(screen.getByRole("button", { name: "Add another channel" }));
    expect(screen.getAllByLabelText(/^Channel \d+$/)).toHaveLength(2);
    expect(saveButton.disabled).toBe(true); // both rows still empty

    await fireEvent.input(screen.getAllByLabelText(/^Channel \d+$/)[1], {
      target: { value: "C123456" },
    });
    expect(saveButton.disabled).toBe(false);

    const removeButtons = screen.getAllByRole("button", { name: /remove channel/i });
    await fireEvent.click(removeButtons[1]);
    expect(screen.getAllByLabelText(/^Channel \d+$/)).toHaveLength(1);
    expect(saveButton.disabled).toBe(true); // back to the one empty row
  });

  it("frozen disables every control: rows, add, save, the manifest toggle, and every setup field", () => {
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: { app_id: "A123", channels: ["C123456"], state: "waiting" },
      frozen: true,
    });

    for (const row of screen.getAllByLabelText(/^Channel \d+$/)) {
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
    expect(
      (screen.getByRole("button", { name: "Show the manifest" }) as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(
      (screen.getByRole("button", { name: "Copy the manifest" }) as HTMLButtonElement).disabled,
    ).toBe(true);
    expect((screen.getByLabelText("App ID") as HTMLInputElement).disabled).toBe(true);
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
      frozen: false,
    });

    expect(screen.getByText("Connected.")).not.toBeNull();
    const rows = screen.getAllByLabelText(/^Channel \d+$/) as HTMLInputElement[];
    expect(rows.map((row) => row.value)).toEqual(["C123456", "C654321"]);
  });
});
