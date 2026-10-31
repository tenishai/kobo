// @vitest-environment jsdom
import { fireEvent, screen, waitFor, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import SlackPanel from "./SlackPanel.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

const IDLE_SLACK = { app_id: null, channels: [], state: "idle" as const, wake: "thread" as const };
const ONE_CHANNEL = {
  app_id: null,
  channels: ["C123456"],
  state: "idle" as const,
  wake: "thread" as const,
};
const CONNECTED = {
  app_id: "A123",
  channels: ["C123456"],
  state: "connected" as const,
  wake: "mention" as const,
};
const WAITING = {
  app_id: "A123",
  channels: ["C123456"],
  state: "waiting" as const,
  wake: "mention" as const,
};

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

/** Adds one channel row and types an id into it. */
async function addChannel(value: string): Promise<void> {
  await fireEvent.click(screen.getByRole("button", { name: "Add channel" }));
  const rows = screen.getAllByLabelText(/^Channel \d+$/);
  await fireEvent.input(rows[rows.length - 1], { target: { value } });
}

/** The label on every filled button inside one panel. There is exactly one
 * of these in every state, which is the rule these tests keep. */
function primaries(root: ParentNode): string[] {
  return Array.from(root.querySelectorAll(".btn--primary"), (button) =>
    (button.textContent ?? "").trim(),
  );
}

/** The manifest route, for the hand-rolled fetch mocks below. Step 1 asks
 * for it as soon as it is on screen, so every first-run render needs an
 * answer: a query function that resolves to `undefined` parks the query in
 * an error state and prints a warning nobody needs to read. */
function manifestRoute(url: string): Response | null {
  if (!url.includes("/slack/manifest")) {
    return null;
  }
  return new Response(JSON.stringify({ manifest: MANIFEST_FIXTURE }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
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
    // The manifest is fetched as soon as the first ceremony step is on
    // screen now, so every first-run render here asks for it — it gets a
    // real answer by default rather than the bare catch-all, which would
    // resolve to `undefined` data and park the query in an error state.
    // Order matters: stubFetch matches by url.includes, so the specific
    // manifest path comes before the "/api/" catch-all.
    stubFetch({
      "/api/agents/warden/slack/manifest": { manifest: MANIFEST_FIXTURE },
      "/api/": {},
    });
  });

  afterEach(() => {
    // jsdom has no `clipboard` own property by default (confirmed against
    // the installed jsdom directly) — deleting it after every test restores
    // that pristine state regardless of which test defined it, so the
    // clipboard-success test below can't leak into its neighbors.
    Reflect.deleteProperty(navigator, "clipboard");
  });

  // --- First run: the panel is the setup -------------------------------------

  it("Connect stays off until all three values and one channel are there", async () => {
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: IDLE_SLACK,
      frozen: false,
    });

    const connect = screen.getByRole("button", { name: "Connect" }) as HTMLButtonElement;
    expect(connect.disabled).toBe(true);

    await pasteSetup();
    // Three values, no channel: the server would refuse this one (409), so
    // the button refuses it first.
    expect(connect.disabled).toBe(true);

    await addChannel("C777777");
    expect(connect.disabled).toBe(false);
  });

  it("Connect saves the channels first, then sends the three values", async () => {
    const seen: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        const manifest = manifestRoute(url);
        if (manifest) {
          return manifest;
        }
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

    await addChannel(" C777777 ");
    await pasteSetup();
    await fireEvent.click(screen.getByRole("button", { name: "Connect" }));

    await waitFor(() => {
      expect(seen).toEqual(["channels", "connect"]);
    });
    expect(bodyOf("/slack/channels")).toEqual({ channels: ["C777777"] });
    expect(bodyOf("/slack/connect")).toEqual({
      app_id: "A1234567",
      bot_token: "xoxb-secret",
      app_token: "xapp-secret",
    });
  });

  it("Connect with no channel keeps the pasted values and says what to do", async () => {
    // The button guards this case, so the only way to reach the server's own
    // 409 is a channel list that emptied out underneath the press. The
    // message and the kept values are what matter: tokens are never stored
    // anywhere, and re-pasting all three is a real cost.
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        const manifest = manifestRoute(url);
        if (manifest) {
          return manifest;
        }
        if (url.includes("/slack/channels")) {
          return new Response(JSON.stringify({ channels: ["C777777"], state: "idle" }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
        if (url.includes("/slack/connect")) {
          return new Response(JSON.stringify({ error: "Add at least one channel first." }), {
            status: 409,
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

    await addChannel("C777777");
    await pasteSetup();
    await fireEvent.click(screen.getByRole("button", { name: "Connect" }));

    expect(
      await screen.findByText(
        "Add at least one channel, then press Connect again. Your values are kept.",
      ),
    ).not.toBeNull();
    expect((screen.getByLabelText("App ID") as HTMLInputElement).value).toBe("A1234567");
    expect((screen.getByLabelText("Bot token") as HTMLInputElement).value).toBe("xoxb-secret");
    expect((screen.getByLabelText("App-level token") as HTMLInputElement).value).toBe(
      "xapp-secret",
    );
  });

  it("Connect clears the App ID and both tokens only after the call lands", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const manifest = manifestRoute(String(input));
        if (manifest) {
          return manifest;
        }
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
    await fireEvent.click(screen.getByRole("button", { name: "Connect" }));

    await waitFor(() => {
      expect((screen.getByLabelText("App ID") as HTMLInputElement).value).toBe("");
    });
    expect((screen.getByLabelText("Bot token") as HTMLInputElement).value).toBe("");
    expect((screen.getByLabelText("App-level token") as HTMLInputElement).value).toBe("");
  });

  it("Cancel throws the whole draft away", async () => {
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: ONE_CHANNEL,
      frozen: false,
    });

    await addChannel("C654321");
    await pasteSetup();

    await fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    const rows = screen.getAllByLabelText(/^Channel \d+$/) as HTMLInputElement[];
    expect(rows.map((row) => row.value)).toEqual(["C123456"]);
    expect((screen.getByLabelText("App ID") as HTMLInputElement).value).toBe("");
    expect((screen.getByLabelText("Bot token") as HTMLInputElement).value).toBe("");
    expect((screen.getByLabelText("App-level token") as HTMLInputElement).value).toBe("");
  });

  // --- The manifest ----------------------------------------------------------

  it("Show the manifest reveals the block, and hides it again", async () => {
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: IDLE_SLACK,
      frozen: false,
    });

    const toggle = screen.getByRole("button", { name: "Show the manifest" });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");

    await fireEvent.click(toggle);

    expect(await screen.findByText(/keeper agent/)).not.toBeNull();
    await fireEvent.click(screen.getByRole("button", { name: "Hide the manifest" }));
    expect(screen.queryByText(/keeper agent/)).toBeNull();
  });

  it("Copy the manifest writes the rendered manifest to the clipboard and says Copied", async () => {
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

    const copy = screen.getByRole("button", { name: "Copy the manifest" }) as HTMLButtonElement;
    // Off only for as long as it takes the manifest to land; nobody has to
    // press anything to arm it.
    await waitFor(() => {
      expect(copy.disabled).toBe(false);
    });

    await fireEvent.click(copy);

    expect(writeText).toHaveBeenCalledWith(JSON.stringify(MANIFEST_FIXTURE, null, 2));
    expect(await screen.findByText("Copied")).not.toBeNull();
  });

  it("Copy the manifest says so when there is no clipboard to write to", async () => {
    // No stub here on purpose: jsdom's own `navigator.clipboard` is already
    // undefined, which is the real-world failure mode this covers (a
    // non-secure context).
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: IDLE_SLACK,
      frozen: false,
    });

    const copy = screen.getByRole("button", { name: "Copy the manifest" }) as HTMLButtonElement;
    await waitFor(() => {
      expect(copy.disabled).toBe(false);
    });

    await fireEvent.click(copy);

    expect(await screen.findByText("Copy failed. Select the text instead.")).not.toBeNull();
  });

  // --- The three values ------------------------------------------------------

  it("each token toggle reveals its own value and leaves the other masked", async () => {
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: IDLE_SLACK,
      frozen: false,
    });

    const botToken = screen.getByLabelText("Bot token") as HTMLInputElement;
    const appToken = screen.getByLabelText("App-level token") as HTMLInputElement;
    await fireEvent.input(botToken, { target: { value: "xoxb-secret" } });
    await fireEvent.input(appToken, { target: { value: "xapp-secret" } });
    expect(botToken.type).toBe("password");

    await fireEvent.click(screen.getByRole("button", { name: "Show bot token" }));
    expect(botToken.type).toBe("text");
    expect(botToken.value).toBe("xoxb-secret");
    expect(appToken.type).toBe("password");

    await fireEvent.click(screen.getByRole("button", { name: "Hide bot token" }));
    expect(botToken.type).toBe("password");

    await fireEvent.click(screen.getByRole("button", { name: "Show app-level token" }));
    expect(appToken.type).toBe("text");
  });

  // --- The channels ----------------------------------------------------------

  it("Add channel adds an inline row and puts the cursor in it", async () => {
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: IDLE_SLACK,
      frozen: false,
    });

    await fireEvent.click(screen.getByRole("button", { name: "Add channel" }));

    const rows = screen.getAllByLabelText(/^Channel \d+$/);
    expect(rows).toHaveLength(1);
    expect((rows[0] as HTMLInputElement).value).toBe("");
    expect(document.activeElement).toBe(rows[0]);
    expect(screen.queryByText("No channels yet.")).toBeNull();
  });

  it("the remove button names the channel it removes", async () => {
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: CONNECTED,
      frozen: false,
    });

    await fireEvent.click(screen.getByRole("button", { name: "Remove C123456" }));

    expect(screen.queryAllByLabelText(/^Channel \d+$/)).toHaveLength(0);
    expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(
      false,
    );
  });

  // --- Connected -------------------------------------------------------------

  it("picking how it wakes up saves at once and says when the manifest must be re-applied", async () => {
    stubFetch({
      "/slack/wake": { wake: "thread", previous_wake: "mention", manifest_changed: true, wiring: "env" },
      "/api/agents/warden/slack/manifest": { manifest: MANIFEST_FIXTURE },
      "/api/": {},
    });
    renderWithClient(SlackPanel, { agentName: "warden", slack: CONNECTED, frozen: false });

    const mention = screen.getByLabelText(/Mentions only/) as HTMLInputElement;
    expect(mention.checked).toBe(true);
    await fireEvent.click(screen.getByLabelText(/Mentions, then the thread/));

    await waitFor(() => {
      expect(bodyOf("/slack/wake")).toEqual({ wake: "thread" });
    });
    await waitFor(() => {
      expect(screen.getByRole("status").textContent).toContain("copy the manifest again");
    });
    // Not a draft: the panel's Save stays off, the choice already landed.
    expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("greys out with one sentence when the room has no Slack integration", () => {
    // The room's gate (#304): no fields, no buttons, only who to ask.
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: { ...IDLE_SLACK, available: false },
      frozen: false,
    });

    expect(screen.getByText("Ask a kobo admin to add the Slack integration.")).not.toBeNull();
    expect(document.querySelectorAll("input, textarea, button")).toHaveLength(0);
  });

  it("a connected agent gets the channels table, not the stepper", () => {
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: { ...CONNECTED, channels: ["C123456", "C654321"] },
      frozen: false,
    });

    expect(screen.getByText("Connected")).not.toBeNull();
    expect(screen.getByText("The Slack app is connected to this agent.")).not.toBeNull();
    const rows = screen.getAllByLabelText(/^Channel \d+$/) as HTMLInputElement[];
    expect(rows.map((row) => row.value)).toEqual(["C123456", "C654321"]);

    expect(document.querySelectorAll(".slack-panel__step")).toHaveLength(0);
    expect(screen.queryByLabelText("App ID")).toBeNull();
    expect(screen.queryByRole("button", { name: "Copy the manifest" })).toBeNull();
    expect(screen.getByRole("button", { name: "Save" })).not.toBeNull();
  });

  it("Reconnect the Slack app opens the two ceremony steps, shut until asked", async () => {
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: CONNECTED,
      frozen: false,
    });

    const toggle = screen.getByRole("button", { name: "Reconnect the Slack app" });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(
      vi.mocked(fetch).mock.calls.some(([url]) => String(url).includes("/slack/manifest")),
    ).toBe(false);

    await fireEvent.click(toggle);

    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(document.querySelectorAll(".slack-panel__step")).toHaveLength(2);
    expect(screen.getByText("Create the Slack app from this manifest")).not.toBeNull();
    expect(screen.getByText("Paste the three values back")).not.toBeNull();
    expect(screen.getByLabelText("App ID")).not.toBeNull();
    // Step 3 is not in there: where it answers is the panel itself now.
    expect(screen.queryByText("Where it answers")).toBeNull();
    await waitFor(() => {
      expect(
        vi.mocked(fetch).mock.calls.some(([url]) => String(url).includes("/slack/manifest")),
      ).toBe(true);
    });
  });

  it("Save is off while nothing has changed, and sends only the channels", async () => {
    stubFetch({
      "/slack/channels": { channels: ["C123456", "C654321"], state: "connected" },
      "/api/agents/warden/slack/manifest": { manifest: MANIFEST_FIXTURE },
      "/api/": {},
    });

    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: CONNECTED,
      frozen: false,
    });

    const save = screen.getByRole("button", { name: "Save" }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);

    // An empty new row is not a change either: it saves nothing.
    await fireEvent.click(screen.getByRole("button", { name: "Add channel" }));
    expect(save.disabled).toBe(true);

    await fireEvent.input(screen.getAllByLabelText(/^Channel \d+$/)[1], {
      target: { value: " C654321 " },
    });
    expect(save.disabled).toBe(false);

    await fireEvent.click(save);

    await waitFor(() => {
      expect(bodyOf("/slack/channels")).toEqual({ channels: ["C123456", "C654321"] });
    });
    expect(
      vi.mocked(fetch).mock.calls.some(([url]) => String(url).includes("/slack/connect")),
    ).toBe(false);
  });

  it("Discard puts the saved channels back", async () => {
    renderWithClient(SlackPanel, {
      agentName: "warden",
      slack: CONNECTED,
      frozen: false,
    });

    expect(screen.queryByRole("button", { name: "Discard" })).toBeNull();

    await fireEvent.click(screen.getByRole("button", { name: "Remove C123456" }));
    await fireEvent.click(screen.getByRole("button", { name: "Discard" }));

    const rows = screen.getAllByLabelText(/^Channel \d+$/) as HTMLInputElement[];
    expect(rows.map((row) => row.value)).toEqual(["C123456"]);
    expect(screen.queryByRole("button", { name: "Discard" })).toBeNull();
  });

  // --- One primary, always ---------------------------------------------------

  // --- Frozen ----------------------------------------------------------------

});
