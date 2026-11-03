// @vitest-environment jsdom
// Slack as a vendor on the Access tab (issue #371): three states a person
// reads (pick Wakes on then Create Slack app; Install to Slack; installed
// with the workspace, Wakes on and Remove, and no channel list: inviting
// the app in Slack is the only control), and nothing at all when the room
// does not make Slack apps itself. The row's one line is honest: Live only
// when the agent is ready and no change is pending, Applying while a Wakes
// on change restarts it. The row is one of the Access tab's compact rows
// (issue #382): the panel says whether it is open; the row asks to open
// while something is in flight and to shut once it reads Live. The install's
// way back (`?vendor=slack`) is the panel's to read (PermissionsPanel.test).
import { fireEvent, screen, waitFor } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";

import SlackVendor from "./SlackVendor.svelte";
import { WAKE_POLL_MS, WAKE_SETTLE_MS } from "../lib/slackWake";
import { renderWithClient, stubFetch } from "../test-utils/render";

const AGENT = {
  name: "clio",
  kind: "composed",
  status: "ready",
  slack: { app_id: null, channels: [], state: "idle", wake: "thread", available: true, relay: true, legacy: false },
};

const NONE = {
  enabled: true, available: true, state: "none", app_id: null, install_url: null,
  workspace: null, app_name: null, channels: [], wake: "thread", legacy: false,
};
const CREATED = {
  ...NONE, state: "created", app_id: "A0MADE",
  install_url: "https://slack.com/oauth/v2/authorize?client_id=fake-client-id&state=relay.nonce",
};
const INSTALLED = {
  ...NONE, state: "installed", app_id: "A0MADE", workspace: "Acme", app_name: "clio", channels: ["C0123456789"],
};

/** The row's one read is the view; the agent detail comes from the page's
 * own query cache, seeded here the way AgentDetail leaves it. Open unless
 * the test says otherwise: the panel owns that. */
function room(view: unknown, agent: unknown = AGENT, props: Record<string, unknown> = {}) {
  stubFetch({ "/api/agents/clio/slack": view });
  const onOpenChange = vi.fn();
  const rendered = renderWithClient(SlackVendor, { agentName: "clio", open: true, onOpenChange, ...props });
  rendered.client.setQueryData(["agents", "clio"], agent);
  return { ...rendered, onOpenChange };
}

beforeEach(() => {
  vi.unstubAllGlobals();
  window.location.hash = "";
});

describe("SlackVendor", () => {
  it("asks for a kobo admin when the room has no Slack connection (kobo#377)", async () => {
    room({ ...NONE, available: false });

    expect(await screen.findByText(/Ask a kobo admin to add the Slack connection/)).toBeTruthy();
    expect(screen.getByText("Not connected to the room")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Create Slack app" })).toBeNull();
  });

  it("offers Wakes on and Create Slack app when the agent has no app", async () => {
    room(NONE);

    const create = await screen.findByRole("button", { name: "Create Slack app" });
    expect((screen.getByRole("radio", { name: /Mentions, then the thread/ }) as HTMLInputElement).checked).toBe(true);
    expect(screen.queryByRole("link", { name: "Install to Slack" })).toBeNull();

    // The pick before the app exists is sent with the create, not saved on its own.
    await fireEvent.click(screen.getByRole("radio", { name: /Every message in the channel/ }));
    vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify({ app_id: "A0MADE", install_url: "https://slack.com/x" }), { status: 200 }));
    await fireEvent.click(create);
    const post = vi.mocked(fetch).mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "POST");
    expect(post).toBeDefined();
    expect(String(post![0])).toContain("/api/agents/clio/slack/app");
    expect(JSON.parse(String((post![1] as RequestInit).body))).toEqual({ wake: "channel" });
  });

  it("shows Install to Slack, in this tab, once the app exists", async () => {
    room(CREATED);

    const install = await screen.findByRole("link", { name: "Install to Slack" });
    expect(install.getAttribute("href")).toBe(CREATED.install_url);
    expect(install.getAttribute("target")).toBeNull();
    expect(screen.queryByRole("button", { name: "Create Slack app" })).toBeNull();
    expect(screen.getByRole("button", { name: "Remove" })).toBeDefined();
  });

  it("reads Rebuilding, not Live, while the build the Slack wiring started has not rolled", async () => {
    room({ ...INSTALLED, rebuilding: true });

    expect((await screen.findByRole("status")).textContent?.trim()).toMatch(/^Rebuilding with its Slack app/);
    expect(screen.queryByText("Live")).toBeNull();
  });

  it("shows the workspace, the invite sentence, Wakes on and Remove once installed, reading Live", async () => {
    room(INSTALLED);

    expect((await screen.findByText("Acme")).textContent).toBe("Acme");
    expect(screen.getByRole("status").textContent?.trim()).toBe("Live");
    expect(screen.getByText("Invite @clio to a channel in Slack and it answers there.")).toBeDefined();
    // No channel list on a room-made app: inviting it in Slack is the only control.
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByRole("button", { name: "Add channel" })).toBeNull();
    expect((screen.getByRole("radio", { name: /Mentions, then the thread/ }) as HTMLInputElement).checked).toBe(true);
    expect(screen.queryByRole("link", { name: "Install to Slack" })).toBeNull();

    await fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    expect(screen.getByRole("button", { name: "Remove the Slack app" })).toBeDefined();
  });

  it("is one closed row with its line and Add Slack until the panel opens it", async () => {
    const { onOpenChange } = room(NONE, AGENT, { open: false });

    const head = await screen.findByRole("button", { name: /Slack/ });
    expect(head.textContent).toContain("Not connected for this agent");
    expect(head.textContent).toContain("Add Slack");
    expect(head.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByRole("button", { name: "Create Slack app" })).toBeNull();

    await fireEvent.click(head);
    expect(onOpenChange).toHaveBeenCalledWith(true);
  });

  it("puts the failed-install sentence on the row itself", async () => {
    room(CREATED, AGENT, { installFailed: true });

    const section = await screen.findByRole("region", { name: "Slack" });
    expect(section.textContent).toContain("Installing to Slack did not finish.");
  });

  /** The wake route answers `wake`, and the agent detail is answered by
   * `detail` (a promise the test settles when the room's next check lands). */
  function roomAfterWakeSave(wake: Record<string, unknown>, detail: Promise<Response>) {
    const json = (body: unknown) =>
      new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/slack/wake")) return json(wake);
        if (url.includes("/slack")) return json(INSTALLED);
        return detail;
      }),
    );
    const rendered = renderWithClient(SlackVendor, { agentName: "clio", open: true, onOpenChange: vi.fn() });
    rendered.client.setQueryData(["agents", "clio"], checkedAt("2026-11-01T10:00:00Z"));
    return { json };
  }
  const checkedAt = (at: string) => ({ ...AGENT, health: { state: "up", reason: null, checked_at: at } });
  const radio = (name: RegExp) => screen.getByRole("radio", { name }) as HTMLInputElement;

  it("reads Applying with Wakes on locked, and Live only after a check past the save finds it back, plus a settle", async () => {
    // Contract (live means live, 2026-11-01): a saved choice restarts the
    // agent (`applied_now`), so two picks cannot race; the header goes back
    // to Live only on a health reading taken after the save that is up and
    // not restarting, and then only after the settle, never on a rolling
    // reading and never on the one already in hand. Everything after the
    // click runs on a fake clock, because the watch is timers all the way.
    const readings = [
      { ...AGENT, health: { state: "down", reason: "0 of 1 running", checked_at: "2026-11-01T10:01:00Z", rolling: true } },
      checkedAt("2026-11-01T10:01:05Z"),
    ];
    const json = (body: unknown) =>
      new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/slack/wake")) {
          return json({ wake: "channel", previous_wake: "thread", manifest_changed: false, applied_now: true,
                        applied_at: "2026-11-01T10:00:30Z", wiring: "env" });
        }
        if (url.includes("/slack")) return json(INSTALLED);
        return json(readings.length > 1 ? readings.shift() : readings[0]);
      }),
    );
    const onOpenChange = vi.fn();
    const rendered = renderWithClient(SlackVendor, { agentName: "clio", open: true, onOpenChange });
    rendered.client.setQueryData(["agents", "clio"], checkedAt("2026-11-01T10:00:00Z"));
    const channel = await screen.findByRole("radio", { name: /Every message in the channel/ });
    const status = () => screen.getByRole("status").textContent?.trim();

    vi.useFakeTimers();
    try {
      await fireEvent.click(channel);
      await vi.advanceTimersByTimeAsync(0);
      expect(status()).toBe("Applying your change. Give it a minute.");
      expect(radio(/Mentions only/).disabled).toBe(true);

      // The first reading after the save says the agent is still restarting; the
      // next poll finds it back, and even that is not Live until the settle passes.
      await vi.advanceTimersByTimeAsync(WAKE_POLL_MS + WAKE_SETTLE_MS - 1);
      expect(status()).toBe("Applying your change. Give it a minute.");
      await vi.advanceTimersByTimeAsync(1);
      expect(status()).toBe("Live");
      expect(radio(/Mentions only/).disabled).toBe(false);
      // The change landed: the row asks the panel to shut it (issue #382),
      // and not a moment before.
      expect(onOpenChange).toHaveBeenCalledTimes(1);
      expect(onOpenChange).toHaveBeenCalledWith(false);
    } finally {
      vi.useRealTimers();
    }
  });

  it("reads Restarting, not Down, while the cluster says the agent is mid-restart, and opens itself", async () => {
    // A restart from anywhere (a model change in another tab) is the agent on
    // its way back, which the row says in the same words as its own save;
    // something in flight is a reason for the row to open (issue #382).
    const { onOpenChange } = room(
      INSTALLED,
      { ...AGENT, health: { state: "down", reason: "0 of 1 running", checked_at: "2026-11-01T10:01:00Z", rolling: true } },
      { open: false },
    );
    await waitFor(() => expect(screen.getByRole("status").textContent?.trim()).toBe("Restarting. A mention sent now is answered once it is back."));
    expect(onOpenChange).toHaveBeenCalledWith(true);
  });

  it("says a saved choice waits for the agent's next restart when it could not be applied now", async () => {
    roomAfterWakeSave(
      { wake: "channel", previous_wake: "thread", manifest_changed: false, applied_now: false, wiring: "env" },
      new Promise<Response>(() => {}),
    );

    await fireEvent.click(await screen.findByRole("radio", { name: /Every message in the channel/ }));

    await waitFor(() => expect(screen.getByRole("status").textContent?.trim()).toBe("Saved. Takes effect when the agent next restarts."));
    expect(radio(/Mentions only/).disabled).toBe(false);
  });

  it("shows the old app with one Move button for a legacy agent, and the move calls the route (kobo#304)", async () => {
    // Contract (phase 3 of #304): an agent with an app from the Slack tab
    // reads "Connected the old way" with its app id and offers nothing but
    // the move; the press is one POST to the move route, and the room's
    // honest answer about the old app lands on the row.
    const LEGACY = { ...NONE, app_id: "A0OLD", legacy: true };
    room(LEGACY, { ...AGENT, slack: { ...AGENT.slack, app_id: "A0OLD", legacy: true } });

    const moveButton = await screen.findByRole("button", { name: "Move to the new Slack setup" });
    expect(screen.getByRole("status").textContent?.trim()).toBe("Connected the old way: A0OLD");
    expect(screen.getByText(/the old app is removed. Channels and Wakes on carry over/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Create Slack app" })).toBeNull();
    expect(screen.queryByRole("link", { name: "Install to Slack" })).toBeNull();

    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ app_id: "A0MADE", install_url: "https://slack.com/x", old_app_removed: false }), { status: 200 }),
    );
    await fireEvent.click(moveButton);
    const post = vi.mocked(fetch).mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "POST");
    expect(post).toBeDefined();
    expect(String(post![0])).toContain("/api/agents/clio/slack/move");
    expect(await screen.findByText(/could not confirm the old app was removed/)).toBeTruthy();
  });

  it("renders nothing when the room does not make Slack apps", async () => {
    room(NONE, { ...AGENT, slack: { ...AGENT.slack, relay: false } });

    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByRole("region", { name: "Slack" })).toBeNull();
    expect(vi.mocked(fetch).mock.calls).toHaveLength(0);
  });
});
