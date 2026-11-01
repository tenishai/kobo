// @vitest-environment jsdom
// Slack as a vendor on the Access tab (issue #371): three states a person
// reads (pick Wakes on then Create Slack app; Install to Slack; installed
// with the workspace, channels, Wakes on and Remove), and nothing at all
// when the room does not make Slack apps itself.
import { fireEvent, screen } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";

import SlackVendor from "./SlackVendor.svelte";
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

/** The section's one read is the view; the agent detail comes from the
 * page's own query cache, seeded here the way AgentDetail leaves it. */
function room(view: unknown, agent: unknown = AGENT) {
  stubFetch({ "/api/agents/clio/slack": view });
  const rendered = renderWithClient(SlackVendor, { agentName: "clio" });
  rendered.client.setQueryData(["agents", "clio"], agent);
  return rendered;
}

beforeEach(() => {
  vi.unstubAllGlobals();
});

describe("SlackVendor", () => {
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

  it("shows the workspace, the channels, Wakes on and Remove once installed", async () => {
    room(INSTALLED);

    expect((await screen.findByText("Acme")).textContent).toBe("Acme");
    expect((screen.getByRole("textbox", { name: "Channel 1" }) as HTMLInputElement).value).toBe("C0123456789");
    expect((screen.getByRole("radio", { name: /Mentions, then the thread/ }) as HTMLInputElement).checked).toBe(true);
    expect(screen.queryByRole("link", { name: "Install to Slack" })).toBeNull();

    await fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    expect(screen.getByRole("button", { name: "Remove the Slack app" })).toBeDefined();
  });

  it("renders nothing when the room does not make Slack apps", async () => {
    room(NONE, { ...AGENT, slack: { ...AGENT.slack, relay: false } });

    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByRole("region", { name: "Slack" })).toBeNull();
    expect(vi.mocked(fetch).mock.calls).toHaveLength(0);
  });
});
