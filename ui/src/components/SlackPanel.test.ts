// @vitest-environment jsdom
import { fireEvent, screen, waitFor } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import SlackPanel from "./SlackPanel.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

const IDLE_SLACK = { app_id: null, channels: [], state: "idle" as const };
const ONE_CHANNEL = { app_id: null, channels: ["C123456"], state: "idle" as const };

const MANIFEST_FIXTURE = {
  display_information: { name: "warden", description: "A keeper agent, reachable in Slack." },
  features: { bot_user: { display_name: "warden" } },
};

/** The three setup fields, filled the way a human would fill them. */
async function pasteSetup(): Promise<void> {
  await fireEvent.input(screen.getByLabelText("App ID"), { target: { value: "A1234567" } });
  await fireEvent.input(screen.getByLabelText("Bot token"), {
    target: { value: "xoxb-secret" },
  });
  await fireEvent.input(screen.getByLabelText("App-level token"), {
    target: { value: "xapp-secret" },
  });
}

/** The body of the first call to a path, parsed. */
function bodyOf(path: string): unknown {
  const call = vi.mocked(fetch).mock.calls.find(([url]) => String(url).includes(path));
  if (!call) {
    throw new Error(`no request to ${path}`);
  }
  return JSON.parse(String((call[1] as RequestInit).body));
}

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

  // --- The header -----------------------------------------------------------

  it("shows the state as a chip beside the heading, not as a sentence", () => {
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: { app_id: "A123", channels: ["C123456"], state: "waiting" },
      frozen: false,
    });

    expect(screen.getByText("Waiting to connect")).not.toBeNull();
    expect(
      screen.queryByText("Channels saved. Finish the Slack setup below to connect."),
    ).toBeNull();
  });

  it("tells the human to invite the agent's own bot, by name, into each channel", () => {
    // The runtime's slack transport never auto-joins a channel: a human has
    // to run /invite themselves. It sits in the lead now rather than in the
    // numbered app-creation steps, because it is about the channels above,
    // not about the app.
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: IDLE_SLACK,
      frozen: false,
    });

    expect(screen.getByText(/type \/invite @warden in each one/)).not.toBeNull();
  });

  // --- The channels table ---------------------------------------------------

  it("idle state with no channels shows the empty row and offers + Channel", () => {
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: IDLE_SLACK,
      frozen: false,
    });

    expect(screen.getByText("No channels yet.")).not.toBeNull();
    expect(screen.queryAllByLabelText(/^Channel \d+$/)).toHaveLength(0);
    expect(screen.getByRole("button", { name: "+ Channel" })).not.toBeNull();
  });

  it("+ Channel adds an inline row and puts the cursor in it", async () => {
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: IDLE_SLACK,
      frozen: false,
    });

    await fireEvent.click(screen.getByRole("button", { name: "+ Channel" }));

    const rows = screen.getAllByLabelText(/^Channel \d+$/);
    expect(rows).toHaveLength(1);
    expect((rows[0] as HTMLInputElement).value).toBe("");
    expect(document.activeElement).toBe(rows[0]);
    expect(screen.queryByText("No channels yet.")).toBeNull();
  });

  it("the remove button names the channel it removes, and removing marks the draft pending", async () => {
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: ONE_CHANNEL,
      frozen: false,
    });

    expect(screen.queryByRole("button", { name: "Discard" })).toBeNull();

    await fireEvent.click(screen.getByRole("button", { name: "Remove C123456" }));

    expect(screen.queryAllByLabelText(/^Channel \d+$/)).toHaveLength(0);
    expect(screen.getByRole("button", { name: "Discard" })).not.toBeNull();
    expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(
      false,
    );
  });

  it("Discard puts the saved channels back and empties the three setup fields", async () => {
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: ONE_CHANNEL,
      frozen: false,
    });

    await fireEvent.click(screen.getByRole("button", { name: "Remove C123456" }));
    await pasteSetup();

    await fireEvent.click(screen.getByRole("button", { name: "Discard" }));

    const rows = screen.getAllByLabelText(/^Channel \d+$/) as HTMLInputElement[];
    expect(rows.map((row) => row.value)).toEqual(["C123456"]);
    expect((screen.getByLabelText("App ID") as HTMLInputElement).value).toBe("");
    expect((screen.getByLabelText("Bot token") as HTMLInputElement).value).toBe("");
    expect((screen.getByLabelText("App-level token") as HTMLInputElement).value).toBe("");
    expect(screen.queryByRole("button", { name: "Discard" })).toBeNull();
  });

  it("Save is disabled while nothing has changed", async () => {
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: ONE_CHANNEL,
      frozen: false,
    });

    const save = screen.getByRole("button", { name: "Save" }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);

    // An empty new row is not a change either: it saves nothing.
    await fireEvent.click(screen.getByRole("button", { name: "+ Channel" }));
    expect(save.disabled).toBe(true);

    await fireEvent.input(screen.getAllByLabelText(/^Channel \d+$/)[1], {
      target: { value: "C654321" },
    });
    expect(save.disabled).toBe(false);
  });

  it("one primary control in the whole panel", () => {
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: IDLE_SLACK,
      frozen: false,
    });

    expect(document.querySelectorAll(".slack-panel__button--primary")).toHaveLength(1);
    expect(
      (document.querySelector(".slack-panel__button--primary") as HTMLElement).textContent?.trim(),
    ).toBe("Save");
  });

  // --- The setup disclosure -------------------------------------------------

  it("the setup block is open when the agent is not connected", () => {
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: IDLE_SLACK,
      frozen: false,
    });

    const toggle = screen.getByRole("button", { name: "Set up the Slack app" });
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByLabelText("App ID")).not.toBeNull();
    expect(screen.getByLabelText("Bot token")).not.toBeNull();
    expect(screen.getByLabelText("App-level token")).not.toBeNull();
    expect(screen.getByRole("button", { name: "Show the manifest" })).not.toBeNull();
  });

  it("the setup block is collapsed when the agent is connected, and opens on the disclosure", async () => {
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: { app_id: "A123", channels: ["C123456"], state: "connected" },
      frozen: false,
    });

    const toggle = screen.getByRole("button", { name: "Set up the Slack app" });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByLabelText("App ID")).toBeNull();
    expect(screen.queryByRole("button", { name: "Show the manifest" })).toBeNull();

    await fireEvent.click(toggle);

    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByLabelText("App ID")).not.toBeNull();
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

  it("the shape of each value is a placeholder in its own field, not a line of copy under it", () => {
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: IDLE_SLACK,
      frozen: false,
    });

    expect((screen.getByLabelText("App ID") as HTMLInputElement).placeholder).toBe("A0123456789");
    expect((screen.getByLabelText("Bot token") as HTMLInputElement).placeholder).toBe("xoxb-...");
    expect((screen.getByLabelText("App-level token") as HTMLInputElement).placeholder).toBe(
      "xapp-...",
    );
    expect(screen.queryByText("Starts with A, like A0123456789.")).toBeNull();
  });

  it("keeps the never-kept promise in one line inside the disclosure", () => {
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: IDLE_SLACK,
      frozen: false,
    });

    expect(
      screen.getByText(
        "For safety, the room never keeps these values. They go to the agent when you press Save.",
      ),
    ).not.toBeNull();
  });

  it("five steps, with the copy-then-generate and install-then-copy pairs merged", () => {
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: IDLE_SLACK,
      frozen: false,
    });

    expect(document.querySelectorAll(".slack-panel__step")).toHaveLength(5);
    expect(
      screen.getByText('Choose "From a manifest", then select your workspace.'),
    ).not.toBeNull();
    expect(
      screen.getByText(
        'On "Basic Information", copy the App ID, then generate an app-level token with the connections:write scope.',
      ),
    ).not.toBeNull();
    expect(
      screen.getByText("Install the app to your workspace, then copy the bot token."),
    ).not.toBeNull();
  });

  it("drops the portrait step and the note that contradicted it", () => {
    // The old step 9 told the human to upload the portrait by hand, and the
    // note under the form told them they could add a picture later. One of
    // the two had to go, and neither is needed to connect an agent.
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: IDLE_SLACK,
      frozen: false,
    });

    expect(screen.queryByText(/Slack cannot receive the portrait automatically/)).toBeNull();
    expect(screen.queryByText(/You can add a picture for it later/)).toBeNull();
  });

  // --- The manifest ---------------------------------------------------------

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

  // --- The token fields -----------------------------------------------------

  it("the bot token toggle reveals the pasted value as plain text, then re-hides it", async () => {
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: IDLE_SLACK,
      frozen: false,
    });

    const botToken = screen.getByLabelText("Bot token") as HTMLInputElement;
    await fireEvent.input(botToken, { target: { value: "xoxb-secret" } });
    expect(botToken.type).toBe("password");

    await fireEvent.click(screen.getByRole("button", { name: "Show bot token" }));
    expect(botToken.type).toBe("text");
    expect(botToken.value).toBe("xoxb-secret");

    await fireEvent.click(screen.getByRole("button", { name: "Hide bot token" }));
    expect(botToken.type).toBe("password");
    expect(botToken.value).toBe("xoxb-secret");
  });

  it("the app-level token toggle reveals the pasted value as plain text, then re-hides it", async () => {
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: IDLE_SLACK,
      frozen: false,
    });

    const appToken = screen.getByLabelText("App-level token") as HTMLInputElement;
    await fireEvent.input(appToken, { target: { value: "xapp-secret" } });
    expect(appToken.type).toBe("password");

    await fireEvent.click(screen.getByRole("button", { name: "Show app-level token" }));
    expect(appToken.type).toBe("text");
    expect(appToken.value).toBe("xapp-secret");

    await fireEvent.click(screen.getByRole("button", { name: "Hide app-level token" }));
    expect(appToken.type).toBe("password");
  });

  it("the two token toggles are independent: revealing bot token leaves app-level token masked", async () => {
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: IDLE_SLACK,
      frozen: false,
    });

    const botToken = screen.getByLabelText("Bot token") as HTMLInputElement;
    const appToken = screen.getByLabelText("App-level token") as HTMLInputElement;
    await fireEvent.input(botToken, { target: { value: "xoxb-secret" } });
    await fireEvent.input(appToken, { target: { value: "xapp-secret" } });

    await fireEvent.click(screen.getByRole("button", { name: "Show bot token" }));

    expect(botToken.type).toBe("text");
    expect(appToken.type).toBe("password");
  });

  // --- What Save sends ------------------------------------------------------

  it("Save sends the edited channel list through the channels call, and nothing else", async () => {
    stubFetch({
      "/slack/channels": { channels: ["C123456", "C654321"], state: "waiting" },
      "/api/": {},
    });

    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: ONE_CHANNEL,
      frozen: false,
    });

    await fireEvent.click(screen.getByRole("button", { name: "+ Channel" }));
    await fireEvent.input(screen.getAllByLabelText(/^Channel \d+$/)[1], {
      target: { value: " C654321 " },
    });

    await fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(bodyOf("/slack/channels")).toEqual({ channels: ["C123456", "C654321"] });
    });
    expect(
      vi.mocked(fetch).mock.calls.some(([url]) => String(url).includes("/slack/connect")),
    ).toBe(false);
  });

  it("Save sends the three setup values through the connect call, and nothing else", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input).includes("/slack/connect")) {
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
      slack: ONE_CHANNEL,
      frozen: false,
    });

    await pasteSetup();
    await fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(bodyOf("/slack/connect")).toEqual({
        app_id: "A1234567",
        bot_token: "xoxb-secret",
        app_token: "xapp-secret",
      });
    });
    expect(
      vi.mocked(fetch).mock.calls.some(([url]) => String(url).includes("/slack/channels")),
    ).toBe(false);
  });

  it("one Save that carries both saves the channels first, so connect has one to find", async () => {
    const seen: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/slack/channels")) {
          seen.push("channels");
          return new Response(JSON.stringify({ channels: ["C777777"], state: "waiting" }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
        if (url.includes("/slack/connect")) {
          seen.push("connect");
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
      slack: IDLE_SLACK,
      frozen: false,
    });

    await fireEvent.click(screen.getByRole("button", { name: "+ Channel" }));
    await fireEvent.input(screen.getAllByLabelText(/^Channel \d+$/)[0], {
      target: { value: "C777777" },
    });
    await pasteSetup();

    await fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(seen).toEqual(["channels", "connect"]);
    });
    expect(bodyOf("/slack/channels")).toEqual({ channels: ["C777777"] });
  });

  it("Save with zero channels keeps the pasted App ID and both tokens, and says what to do", async () => {
    // Real flow this pins: all three setup values filled in, but no channel
    // ever added. The server's own guard (POST .../slack/connect, 409 "Add
    // at least one channel first.") is what catches it. Regression coverage
    // for the bug where all three pasted secrets used to vanish on this
    // exact failure.
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

    await pasteSetup();
    await fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(
      await screen.findByText(
        "Add at least one channel, then press Save again. Your values are kept.",
      ),
    ).not.toBeNull();

    expect((screen.getByLabelText("App ID") as HTMLInputElement).value).toBe("A1234567");
    expect((screen.getByLabelText("Bot token") as HTMLInputElement).value).toBe("xoxb-secret");
    expect((screen.getByLabelText("App-level token") as HTMLInputElement).value).toBe(
      "xapp-secret",
    );
  });

  it("Save clears the App ID and both tokens only after a successful connect", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input).includes("/slack/connect")) {
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
      slack: ONE_CHANNEL,
      frozen: false,
    });

    await pasteSetup();
    await fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect((screen.getByLabelText("App ID") as HTMLInputElement).value).toBe("");
    });
    expect((screen.getByLabelText("Bot token") as HTMLInputElement).value).toBe("");
    expect((screen.getByLabelText("App-level token") as HTMLInputElement).value).toBe("");
  });

  // --- Frozen and connected -------------------------------------------------

  it("frozen disables every control: rows, add, save, the disclosure, and every setup field", () => {
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: { app_id: "A123", channels: ["C123456"], state: "waiting" },
      frozen: true,
    });

    for (const row of screen.getAllByLabelText(/^Channel \d+$/)) {
      expect((row as HTMLInputElement).disabled).toBe(true);
    }
    expect(
      (screen.getByRole("button", { name: "+ Channel" }) as HTMLButtonElement).disabled,
    ).toBe(true);
    expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    for (const button of screen.getAllByRole("button", { name: /^Remove / })) {
      expect((button as HTMLButtonElement).disabled).toBe(true);
    }
    expect(
      (screen.getByRole("button", { name: "Set up the Slack app" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    expect(
      (screen.getByRole("button", { name: "Show the manifest" }) as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(
      (screen.getByRole("button", { name: "Copy the manifest" }) as HTMLButtonElement).disabled,
    ).toBe(true);
    expect((screen.getByLabelText("App ID") as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByLabelText("Bot token") as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByLabelText("App-level token") as HTMLInputElement).disabled).toBe(true);
  });

  it("connected state shows the Connected chip and the saved channel list", () => {
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: { app_id: "A123", channels: ["C123456", "C654321"], state: "connected" },
      frozen: false,
    });

    expect(screen.getByText("Connected")).not.toBeNull();
    const rows = screen.getAllByLabelText(/^Channel \d+$/) as HTMLInputElement[];
    expect(rows.map((row) => row.value)).toEqual(["C123456", "C654321"]);
  });
});
