// @vitest-environment jsdom
import { fireEvent, screen, within } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import AgentDetail from "./AgentDetail.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

const READY_AGENT = {
  name: "warden",
  persona: "keeper",
  team: "shared",
  purpose_tags: [],
  capability: null,
  model: "m",
  model_label: "M",
  skills: [],
  status: "ready",
  repo_full_name: null,
  error: null,
  created_at: "2026-08-17T00:00:00Z",
  steps: [],
  first_reply: null,
  slack: { app_id: null, channels: [], state: "idle" },
  destroy_visible: false,
  has_avatar: false,
  created_by: null,
  group_name: null,
  pending_learnings: 0,
  can_decide_learnings: false,
};

/** Every route AgentDetail's panels touch besides the agent itself and the
 * persona search -- shared across every fixture below since every panel
 * stays mounted regardless of which tab (or none) is active. Individual
 * tests spread this and override `/api/agents/warden` (and, where needed,
 * `/api/personas/search`) for their own fixture shape. */
const BASE_ROUTES = {
  "/api/colleagues/feed": { entries: [], truncated: false },
  "/api/agents/warden/knowledge": { files: [] },
  "/api/agents/warden/intake": { files: [] },
  "/api/agents/warden/skills": { skills: [] },
  "/api/agents/warden/tooling": {
    files: [],
    enforcement: { attested: false, locked: false, locked_file: null },
  },
  "/api/agents/warden/invites": { incoming: [], outgoing: [] },
  "/api/agents/warden/grants": { grants: [] },
  "/api/agents/warden/secrets": {
    keys: [],
    updated_at: null,
    platform: true,
    secret_name: "warden-env",
    unreadable: false,
  },
  "/api/agents/warden/env": { env: {} },
  "/api/agents/warden": READY_AGENT,
  "/api/personas/search": { results: [] },
  "/api/": {},
};

// A minimal EventSource stand-in - jsdom has none, and ColleagueConversations
// (mounted unconditionally on the Chat tab, same "every panel stays mounted"
// invariant as every other panel here) opens one as soon as its list query
// resolves. This file's own tests never exercise live SSE updates (that's
// ColleagueConversations.test.ts's job, with its own richer fake); this one
// only needs to exist so the effect doesn't throw `ReferenceError:
// EventSource is not defined` partway through an otherwise-unrelated test.
class NoopEventSource {
  constructor(_url: string) {}
  addEventListener(): void {}
  removeEventListener(): void {}
  close(): void {}
}

/** Opens the header's one overflow menu (page-shell wave, 2026-09-23).
 * Every portrait action and Edit details is an item inside it now, so a
 * test that reaches for one of those opens the menu first -- the items are
 * not in the DOM at all until then. */
async function openHeaderMenu(): Promise<void> {
  await fireEvent.click(screen.getByRole("button", { name: "More actions" }));
}

/** One item in the Harness tab's own section list. Matched inside its nav
 * so a section never collides with the panel heading of the same name. */
function harnessItem(label: string): HTMLElement {
  return within(screen.getByRole("navigation", { name: "Harness sections" })).getByRole(
    "button",
    { name: new RegExp(`^${label}`) },
  );
}

describe("AgentDetail tabs", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    vi.stubGlobal("EventSource", NoopEventSource);
    location.hash = "#/agents/warden";
    // `parseHash`/`route` (router.svelte.ts) only resync on a real
    // `hashchange` event (or `navigate`, which needs the module already
    // wired up) — the router module was already imported (and `route`
    // initialized from whatever the hash was at that point) well before
    // this test's `beforeEach` runs, so setting `location.hash` alone
    // doesn't retroactively update it. A manual `hashchange` dispatch
    // drives the same `sync()` listener a real navigation would.
    window.dispatchEvent(new Event("hashchange"));
    stubFetch(BASE_ROUTES);
  });

  it("defaults to Chat when ready and shows no checklist", async () => {
    // A real steps entry (same shape the provisioning fixture below uses)
    // so this actually proves the ready state HIDES an existing checklist,
    // rather than trivially finding nothing because READY_AGENT.steps is
    // normally [].
    stubFetch({
      ...BASE_ROUTES,
      "/api/agents/warden": {
        ...READY_AGENT,
        steps: [{ name: "clone", label: "Clone the repo", status: "pending", error: null }],
      },
    });
    renderWithClient(AgentDetail);
    const chatTab = await screen.findByRole("tab", { name: "Chat" });
    expect(chatTab.getAttribute("aria-selected")).toBe("true");
    expect(screen.queryByText("Clone the repo")).toBeNull();
  });

  it("switches tabs without unmounting the panel it left", async () => {
    stubFetch({ ...BASE_ROUTES, "/api/agents/warden": { ...READY_AGENT, can_decide_learnings: true } });
    renderWithClient(AgentDetail);
    const chatTab = await screen.findByRole("tab", { name: "Chat" });
    expect(chatTab.getAttribute("aria-selected")).toBe("true");

    // Ready is the one status where every tab trigger is reachable — none
    // of them dead-end into a 409, so none should be disabled.
    for (const label of ["Chat", "Slack", "Access", "Harness", "Settings"]) {
      expect((screen.getByRole("tab", { name: label }) as HTMLButtonElement).disabled).toBe(
        false,
      );
    }

    await fireEvent.click(screen.getByRole("tab", { name: "Harness" }));
    expect(screen.getByRole("tab", { name: "Harness" }).getAttribute("aria-selected")).toBe(
      "true",
    );
    expect(document.getElementById("panel-chat")?.hasAttribute("hidden")).toBe(true);
    expect(document.getElementById("panel-chat")).not.toBeNull();
  });

  it("puts the five grow-it sections inside Harness, not in the row", async () => {
    stubFetch({ ...BASE_ROUTES, "/api/agents/warden": { ...READY_AGENT, can_decide_learnings: true } });
    renderWithClient(AgentDetail);
    await screen.findByRole("tab", { name: "Chat" });

    // They are not tabs any more, and there is no menu left to open.
    for (const label of ["Learnings", "Memory", "Skills", "Tooling", "Secrets"]) {
      expect(screen.queryByRole("tab", { name: label })).toBeNull();
    }
    expect(screen.queryByRole("button", { name: /^More ▾/ })).toBeNull();

    await fireEvent.click(screen.getByRole("tab", { name: "Harness" }));
    await fireEvent.click(harnessItem("Memory"));
    expect(harnessItem("Memory").getAttribute("aria-current")).toBe("true");
    expect(document.getElementById("harness-memory")?.hasAttribute("hidden")).toBe(false);
  });

  it("selects no tab while provisioning and shows the checklist above the tabs", async () => {
    stubFetch({
      ...BASE_ROUTES,
      "/api/agents/warden": {
        ...READY_AGENT,
        status: "provisioning",
        steps: [{ name: "clone", label: "Clone the repo", status: "pending", error: null }],
      },
    });
    renderWithClient(AgentDetail);
    const chatTab = await screen.findByRole("tab", { name: "Chat" });
    expect(chatTab.getAttribute("aria-selected")).toBe("false");
    expect(chatTab.hasAttribute("disabled")).toBe(true);
    expect(screen.getByText("Clone the repo")).not.toBeNull();
    expect(screen.getByRole("button", { name: /build log/i })).not.toBeNull();
  });

  it("disables every tab trigger while provisioning, so a click can't dead-end into a 409", async () => {
    stubFetch({
      ...BASE_ROUTES,
      "/api/agents/warden": { ...READY_AGENT, status: "provisioning", can_decide_learnings: true },
    });
    renderWithClient(AgentDetail);
    const chatTab = await screen.findByRole("tab", { name: "Chat" });
    expect(chatTab.getAttribute("aria-selected")).toBe("false");

    const harnessTab = screen.getByRole("tab", { name: "Harness" }) as HTMLButtonElement;
    const settingsTab = screen.getByRole("tab", { name: "Settings" }) as HTMLButtonElement;
    const slackTab = screen.getByRole("tab", { name: "Slack" }) as HTMLButtonElement;
    expect((chatTab as HTMLButtonElement).disabled).toBe(true);
    expect(harnessTab.disabled).toBe(true);
    expect(settingsTab.disabled).toBe(true);
    expect(slackTab.disabled).toBe(true);

    // A disabled trigger can't be selected at all — no default tab was
    // chosen either (defaultTab returns null for a non-ready status), so
    // clicking just leaves every trigger unselected.
    await fireEvent.click(harnessTab);
    expect(harnessTab.getAttribute("aria-selected")).toBe("false");
  });

  it("does not auto-select a tab while destroying, but tabs stay clickable (frozen makes read-only content reachable)", async () => {
    stubFetch({
      ...BASE_ROUTES,
      "/api/agents/warden": { ...READY_AGENT, status: "destroying", destroy_visible: true },
    });
    renderWithClient(AgentDetail);
    const chatTab = await screen.findByRole("tab", { name: "Chat" });
    expect(chatTab.getAttribute("aria-selected")).toBe("false");
    expect((chatTab as HTMLButtonElement).disabled).toBe(false);

    await fireEvent.click(chatTab);
    expect(chatTab.getAttribute("aria-selected")).toBe("true");
  });

  it("renders first words inside the Chat tabpanel", async () => {
    stubFetch({
      ...BASE_ROUTES,
      "/api/agents/warden": { ...READY_AGENT, first_reply: "Hello there." },
    });
    renderWithClient(AgentDetail);
    await screen.findByRole("tab", { name: "Chat" });
    const chatPanel = document.getElementById("panel-chat");
    expect(chatPanel?.textContent).toContain("Hello there.");
  });

  it("keeps identity fields in the always-visible header", async () => {
    const agentWithIdentity = {
      ...READY_AGENT,
      capability: "Answers billing questions and escalates refunds.",
      created_by: "owner@example.com",
      skills: [{ name: "cost-report", team: "sre", sha: "abc123" }],
    };
    stubFetch({ ...BASE_ROUTES, "/api/agents/warden": agentWithIdentity });
    renderWithClient(AgentDetail);
    await screen.findByRole("tab", { name: "Chat" });
    const header = document.querySelector(".agent-detail__header");
    expect(header?.textContent).toContain(agentWithIdentity.capability);
    expect(header?.textContent).toContain(`Made by ${agentWithIdentity.created_by}`);
    // Skill chips live in the header (2026-09-02 ruling): a visitor sees
    // what the agent can do without expanding the Advanced group.
    expect(header?.textContent).toContain("cost-report");
  });

  it("keeps the team chip, purpose tags, and the null-capability copy in the header", async () => {
    stubFetch({
      ...BASE_ROUTES,
      "/api/agents/warden": {
        ...READY_AGENT,
        team: "growth",
        purpose_tags: ["billing", "support"],
        capability: null,
      },
    });
    renderWithClient(AgentDetail);
    await screen.findByRole("tab", { name: "Chat" });
    const header = document.querySelector(".agent-detail__header") as HTMLElement;
    expect(within(header).getByText("growth")).not.toBeNull();
    expect(within(header).getByText("billing")).not.toBeNull();
    expect(within(header).getByText("support")).not.toBeNull();
    expect(
      within(header).getByText("Not written yet. It arrives after first contact."),
    ).not.toBeNull();
  });

  it("shows the earned capability line in the header, replacing the not-written-yet copy", async () => {
    stubFetch({
      ...BASE_ROUTES,
      "/api/agents/warden": {
        ...READY_AGENT,
        capability: "Answers billing questions and escalates refunds.",
      },
    });
    renderWithClient(AgentDetail);
    await screen.findByRole("tab", { name: "Chat" });
    const header = document.querySelector(".agent-detail__header") as HTMLElement;
    expect(
      within(header).getByText("Answers billing questions and escalates refunds."),
    ).not.toBeNull();
    expect(within(header).queryByText(/Not written yet/)).toBeNull();
  });

  it("renders no owner line in the header when created_by is null", async () => {
    renderWithClient(AgentDetail);
    await screen.findByRole("tab", { name: "Chat" });
    const header = document.querySelector(".agent-detail__header") as HTMLElement;
    expect(within(header).queryByText(/Made by/)).toBeNull();
  });

  it("the Edit details affordance disables while frozen (destroying)", async () => {
    stubFetch({
      ...BASE_ROUTES,
      "/api/agents/warden": { ...READY_AGENT, status: "destroying", destroy_visible: true },
    });
    renderWithClient(AgentDetail);
    await screen.findByRole("heading", { name: "warden" });
    await openHeaderMenu();
    const editButton = screen.getByRole("button", { name: "Edit details" });
    expect((editButton as HTMLButtonElement).disabled).toBe(true);
  });

  it("keeps every header action behind one menu, and no filled button beside the name", async () => {
    stubFetch({ ...BASE_ROUTES, "/api/agents/warden": { ...READY_AGENT, has_avatar: true } });
    renderWithClient(AgentDetail);
    await screen.findByRole("heading", { name: "warden" });

    // Closed: the header is two lines of fact and one button.
    expect(screen.queryByRole("button", { name: "New face" })).toBeNull();
    expect(screen.queryByRole("link", { name: "Download the portrait" })).toBeNull();
    const trigger = screen.getByRole("button", { name: "More actions" });
    expect(trigger.getAttribute("aria-haspopup")).toBe("menu");
    expect(trigger.getAttribute("aria-expanded")).toBe("false");

    await openHeaderMenu();
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByRole("button", { name: "New face" })).not.toBeNull();
    expect(screen.getByLabelText("Upload a portrait")).not.toBeNull();
    expect(screen.getByRole("link", { name: "Download the portrait" })).not.toBeNull();
    expect(screen.getByRole("button", { name: "Edit details" })).not.toBeNull();
  });

  it("names the group, the maker and the persona in the one meta run", async () => {
    stubFetch({
      ...BASE_ROUTES,
      "/api/agents/warden": {
        ...READY_AGENT,
        created_by: "owner@example.com",
        group_name: "platform",
      },
    });
    renderWithClient(AgentDetail);
    await screen.findByRole("tab", { name: "Chat" });

    const meta = document.querySelector(".agent-detail__meta") as HTMLElement;
    expect(within(meta).getByText("M")).not.toBeNull();
    expect(within(meta).getByText("platform")).not.toBeNull();
    expect(within(meta).getByText("Made by owner@example.com")).not.toBeNull();
    expect(within(meta).getByText("keeper")).not.toBeNull();
  });

  it("shows three chips and a count, never the whole set", async () => {
    stubFetch({
      ...BASE_ROUTES,
      "/api/agents/warden": {
        ...READY_AGENT,
        purpose_tags: ["billing", "support", "refunds"],
        skills: [
          { name: "cost-report", team: "sre", sha: "a" },
          { name: "pager", team: "sre", sha: "b" },
        ],
      },
    });
    renderWithClient(AgentDetail);
    await screen.findByRole("tab", { name: "Chat" });

    const header = document.querySelector(".agent-detail__header") as HTMLElement;
    expect(within(header).getByText("billing")).not.toBeNull();
    expect(within(header).getByText("support")).not.toBeNull();
    expect(within(header).getByText("refunds")).not.toBeNull();
    // The two that did not fit are a count, and the full set is in Edit
    // details -- which is where they are changed.
    expect(within(header).queryByText("cost-report")).toBeNull();
    expect(within(header).getByText("+2")).not.toBeNull();
  });

  it("shows the pending badge on the Harness tab, where Learnings lives now", async () => {
    stubFetch({
      ...BASE_ROUTES,
      "/api/agents/warden": { ...READY_AGENT, can_decide_learnings: false, pending_learnings: 3 },
    });
    renderWithClient(AgentDetail);
    const harnessTab = await screen.findByRole("tab", { name: /^Harness/ });
    expect(harnessTab.textContent).toContain("3");
    // And on the section itself, so the count survives opening the tab.
    await fireEvent.click(harnessTab);
    expect(harnessItem("Learnings").textContent).toContain("3");
  });

  it("keeps the tab row to one line of five tabs, with no menu after them", async () => {
    stubFetch({
      ...BASE_ROUTES,
      "/api/agents/warden": { ...READY_AGENT, can_decide_learnings: true },
    });
    renderWithClient(AgentDetail);
    await screen.findByRole("tab", { name: "Chat" });

    const row = document.querySelector(".detail-tabs__list") as HTMLElement;
    expect(within(row).getAllByRole("tab").map((t) => t.textContent?.trim())).toEqual([
      "Chat",
      "Slack",
      "Access",
      "Harness",
      "Settings",
    ]);
  });

  it("remembers the tab and the harness section in the hash", async () => {
    renderWithClient(AgentDetail);
    await fireEvent.click(await screen.findByRole("tab", { name: "Harness" }));
    await fireEvent.click(harnessItem("Skills"));

    await vi.waitFor(() => {
      expect(location.hash).toBe("#/agents/warden/harness/skills");
    });
  });

  it("opens on the tab and section the hash names", async () => {
    location.hash = "#/agents/warden/harness/tooling";
    window.dispatchEvent(new Event("hashchange"));
    renderWithClient(AgentDetail);

    const harnessTab = await screen.findByRole("tab", { name: "Harness" });
    expect(harnessTab.getAttribute("aria-selected")).toBe("true");
    expect(harnessItem("Tooling").getAttribute("aria-current")).toBe("true");
    expect(screen.getByRole("tab", { name: "Chat" }).getAttribute("aria-selected")).toBe(
      "false",
    );
  });

  it("ignores a hash naming a tab this room does not have", async () => {
    // A stale bookmark from before the harness wave. Secrets is a section
    // of Settings now, not a tab, so the page falls back to its default
    // rather than selecting nothing.
    location.hash = "#/agents/warden/secrets";
    window.dispatchEvent(new Event("hashchange"));
    renderWithClient(AgentDetail);

    const chatTab = await screen.findByRole("tab", { name: "Chat" });
    expect(chatTab.getAttribute("aria-selected")).toBe("true");
  });

  it("puts Secrets and environment in Settings, above the build log", async () => {
    renderWithClient(AgentDetail);
    await fireEvent.click(await screen.findByRole("tab", { name: "Settings" }));

    const settings = document.getElementById("panel-settings") as HTMLElement;
    expect(settings.hasAttribute("hidden")).toBe(false);
    const text = settings.textContent ?? "";
    expect(text).toContain("Secrets and environment");
    expect(text).toContain("Build log");
    expect(text.indexOf("Secrets and environment")).toBeLessThan(text.indexOf("Build log"));
    // And nowhere else: it used to be its own tab behind the More menu.
    expect(screen.queryByRole("tab", { name: "Secrets" })).toBeNull();
  });

  it("preserves the selected tab across a destroy transition", async () => {
    // `client` (exposed by `renderWithClient`) re-resolves the same agent
    // query with a destroying status, the same way a real poll would
    // (status.ts keeps polling while an agent is non-terminal) -- this
    // forces that re-resolution deterministically rather than faking
    // timers around the real interval.
    const { client } = renderWithClient(AgentDetail);
    const slackTab = await screen.findByRole("tab", { name: "Slack" });
    await fireEvent.click(slackTab);
    expect(slackTab.getAttribute("aria-selected")).toBe("true");

    stubFetch({
      ...BASE_ROUTES,
      "/api/agents/warden": { ...READY_AGENT, status: "destroying", destroy_visible: true },
    });
    await client.invalidateQueries({ queryKey: ["agents", "warden"] });

    await screen.findByText("This agent is being destroyed.");
    const slackTabAfter = screen.getByRole("tab", { name: "Slack" });
    expect(slackTabAfter.getAttribute("aria-selected")).toBe("true");
  });

  it("shows the Slack tab, and renders the setup section unconditionally inside it", async () => {
    // Slack self-service (2026-08-17): the setup section (manifest reveal,
    // instructions, three paste fields) renders for every signed-in user
    // now, no gate. The agent payload's old visibility flag for the setup
    // lane is gone entirely, and AgentDetail no longer threads any such
    // prop into SlackPanel.
    renderWithClient(AgentDetail);
    const slackTab = await screen.findByRole("tab", { name: "Slack" });
    await fireEvent.click(slackTab);
    expect(screen.getByRole("button", { name: "Show the manifest" })).not.toBeNull();
    expect(screen.getByLabelText("App ID")).not.toBeNull();
  });

  it("renders frozen panels and one notice while destroying", async () => {
    // Same ordering discipline as the beforeEach above: specific subpaths
    // before the bare agent route, since stubFetch matches by
    // `url.includes` — "/api/agents/warden" alone would also answer
    // "/api/agents/warden/knowledge" etc. with this agent payload, which
    // isn't shaped like those endpoints' real responses and leaves those
    // panels' queries permanently pending instead of settled.
    stubFetch({
      ...BASE_ROUTES,
      "/api/agents/warden": {
        ...READY_AGENT,
        status: "destroying",
        destroy_visible: true,
        can_decide_learnings: true,
      },
    });
    renderWithClient(AgentDetail);
    expect(await screen.findByText("This agent is being destroyed.")).not.toBeNull();
    // Let MemoryPanel's own knowledge/intake queries (mounted alongside the
    // notice, since every panel stays mounted per the tab invariant) settle
    // past their initial Skeleton — otherwise those transient loading
    // indicators (also role="status") would inflate the count below.
    await new Promise((resolve) => setTimeout(resolve, 50));
    // One destroy announcer, not two: this used to be DangerZone's own
    // "Destroying this agent…" role="status" line duplicating this notice
    // (fixed by dropping DangerZone's live-region attributes — the visible
    // text stays, see DangerZone.svelte). StepChecklist's own step-count
    // announcer ("N of N steps done") is a separate, pre-existing
    // role="status", but it no longer renders at all while frozen (the
    // checklist is gated on `!isFrozen` now), so there is nothing left to
    // exclude here — kept anyway as a defensive filter.
    const destroyAnnouncers = screen
      .getAllByRole("status")
      .filter((el) => !el.classList.contains("step-checklist__sr-only"));
    expect(destroyAnnouncers).toHaveLength(1);
    const chatTab = screen.getByRole("tab", { name: "Chat" });
    expect(chatTab.getAttribute("aria-selected")).toBe("false");
    // Destruction leaves read-only content reachable, Harness included.
    await fireEvent.click(screen.getByRole("tab", { name: "Harness" }));
    expect(harnessItem("Memory")).not.toBeNull();
    await fireEvent.click(chatTab);
    await openHeaderMenu();
    const avatarButton = screen.getByRole("button", { name: /give warden a face/i });
    expect((avatarButton as HTMLButtonElement).disabled).toBe(true);

    // Frozen (destroying/destroy_failed) is the one non-ready status where
    // tabs stay fully clickable — the destruction ruling makes read-only
    // content reachable even though it's not "ready"; only per-panel
    // CONTROLS (like the avatar button above) freeze, never the tabs
    // themselves.
    for (const label of ["Chat", "Slack", "Harness", "Settings"]) {
      expect((screen.getByRole("tab", { name: label }) as HTMLButtonElement).disabled).toBe(
        false,
      );
    }
  });

  it("keeps the destroy out of every other tab, and puts it in Settings", async () => {
    stubFetch({
      ...BASE_ROUTES,
      "/api/agents/warden": { ...READY_AGENT, status: "destroying", destroy_visible: true },
    });
    renderWithClient(AgentDetail);
    await screen.findByRole("tab", { name: "Chat" });

    // It used to be a sibling of the tab strip, so it rendered under
    // whichever tab somebody had open -- including Chat, which is where
    // most visits start.
    const chatPanel = document.getElementById("panel-chat") as HTMLElement;
    expect(chatPanel.textContent).not.toContain("Destroy this agent");

    await fireEvent.click(screen.getByRole("tab", { name: "Settings" }));
    const settingsPanel = document.getElementById("panel-settings") as HTMLElement;
    expect(settingsPanel.hasAttribute("hidden")).toBe(false);
    expect(within(settingsPanel).getByText("Destroy this agent")).not.toBeNull();
    expect(within(settingsPanel).getByText("Destroying this agent…")).not.toBeNull();
  });

  it('shows a "Download the portrait" link, with the per-agent filename, when has_avatar is true', async () => {
    stubFetch({
      ...BASE_ROUTES,
      "/api/agents/warden": { ...READY_AGENT, has_avatar: true },
    });
    renderWithClient(AgentDetail);
    // The portrait actions are items in the header's overflow menu now —
    // waiting for Chat confirms the page finished mounting, then the menu
    // opens.
    await screen.findByRole("tab", { name: "Chat" });
    await openHeaderMenu();

    const link = (await screen.findByRole("link", {
      name: "Download the portrait",
    })) as HTMLAnchorElement;
    expect(link.getAttribute("download")).toBe("warden-portrait.png");
    expect(link.getAttribute("href")).toContain("/api/agents/warden/avatar");
  });

  it('hides "Download the portrait" when has_avatar is false (nothing to download yet)', async () => {
    renderWithClient(AgentDetail);
    await screen.findByRole("tab", { name: "Chat" });
    await openHeaderMenu();

    expect(screen.queryByRole("link", { name: "Download the portrait" })).toBeNull();
  });

  it('a file over the 2 MiB cap shows the plain client-side copy and never PUTs', async () => {
    renderWithClient(AgentDetail);
    await screen.findByRole("tab", { name: "Chat" });
    await openHeaderMenu();

    const oversized = new Uint8Array(2 * 1024 * 1024 + 1);
    const file = new File([oversized], "too-big.png", { type: "image/png" });

    const input = screen.getByLabelText("Upload a portrait") as HTMLInputElement;
    await fireEvent.change(input, { target: { files: [file] } });

    expect(
      await screen.findByText("That image is too large. Two megabytes at most."),
    ).not.toBeNull();
    expect(
      vi
        .mocked(fetch)
        .mock.calls.some(
          ([url, init]) =>
            String(url).includes("/avatar") && (init as RequestInit | undefined)?.method === "PUT",
        ),
    ).toBe(false);
  });

  it('uploading a valid file shows "Uploading…" while pending, PUTs the exact raw bytes, and lands the saved note on success', async () => {
    let resolvePut!: (value: Response) => void;
    const putResponse = new Promise<Response>((resolve) => {
      resolvePut = resolve;
    });

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/agents/warden/avatar") && init?.method === "PUT") {
          return putResponse;
        }
        const routes: Record<string, unknown> = {
          "/api/colleagues/feed": { entries: [], truncated: false },
          "/api/agents/warden/knowledge": { files: [] },
          "/api/agents/warden/intake": { files: [] },
          "/api/agents/warden/skills": { skills: [] },
          "/api/agents/warden/tooling": {
            files: [],
            enforcement: { attested: false, locked: false, locked_file: null },
          },
          "/api/agents/warden/invites": { incoming: [], outgoing: [] },
          "/api/agents/warden/grants": { grants: [] },
          "/api/agents/warden": READY_AGENT,
          "/api/personas/search": { results: [] },
        };
        for (const [needle, payload] of Object.entries(routes)) {
          if (url.includes(needle)) {
            return new Response(JSON.stringify(payload), {
              status: 200,
              headers: { "Content-Type": "application/json" },
            });
          }
        }
        return new Response(JSON.stringify({}), { status: 200 });
      }),
    );

    renderWithClient(AgentDetail);
    await screen.findByRole("tab", { name: "Chat" });
    await openHeaderMenu();

    const pngBytes = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1, 2, 3]);
    const file = new File([pngBytes], "face.png", { type: "image/png" });

    const input = screen.getByLabelText("Upload a portrait") as HTMLInputElement;
    await fireEvent.change(input, { target: { files: [file] } });

    expect(await screen.findByText("Uploading…")).not.toBeNull();

    resolvePut(
      new Response(JSON.stringify({ sha: "cafefeed" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    expect(
      await screen.findByText("Saved. It'll ride along with the next update."),
    ).not.toBeNull();

    const putCall = vi
      .mocked(fetch)
      .mock.calls.find(
        ([url, init]) =>
          String(url).includes("/avatar") && (init as RequestInit | undefined)?.method === "PUT",
      );
    expect(putCall).toBeDefined();
    const sentBody = (putCall as [RequestInfo | URL, RequestInit])[1].body as ArrayBuffer;
    expect(new Uint8Array(sentBody)).toEqual(pngBytes);
  });
});

describe("AgentDetail persona soul-line (persona-naming law, hardening wave)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    vi.stubGlobal("EventSource", NoopEventSource);
    location.hash = "#/agents/warden";
    window.dispatchEvent(new Event("hashchange"));
  });

  it("renders the persona's NAME once the catalog search answers with a matching id, with who-they-are on the hover", async () => {
    stubFetch({
      ...BASE_ROUTES,
      "/api/personas/search": {
        results: [
          {
            id: "keeper",
            name: "Keeper",
            description: "a calm, steady presence who never panics",
            team: "shared",
            tags: [],
            builtin: true,
          },
        ],
      },
    });
    renderWithClient(AgentDetail);
    await screen.findByRole("tab", { name: "Chat" });

    const header = document.querySelector(".agent-detail__header") as HTMLElement;
    // The name, never the bare id -- that is the naming law. The
    // who-they-are half no longer takes a grey line of its own (page-shell
    // wave): it rides along as the hover on the same element.
    const persona = await within(header).findByText("Keeper");
    expect(persona.getAttribute("title")).toBe(
      "Keeper -- a calm, steady presence who never panics",
    );
  });

  it("falls back to the bare persona id when the search has no entry matching it (an archived or deleted-from-catalog persona) -- picking the right entry by id, not just the first result", async () => {
    stubFetch({
      ...BASE_ROUTES,
      "/api/personas/search": {
        // A result set that is non-empty but contains no entry for "keeper"
        // -- pins that the lookup filters by id rather than just grabbing
        // whatever the search happens to return first.
        results: [
          {
            id: "someone-else",
            name: "Someone Else",
            description: "an unrelated persona",
            team: "shared",
            tags: [],
            builtin: true,
          },
        ],
      },
    });
    renderWithClient(AgentDetail);
    await screen.findByRole("tab", { name: "Chat" });

    // Wait for the persona search fetch to actually resolve, and for its
    // result to settle all the way through TanStack Query's cache and
    // Svelte's own reactivity, before asserting -- the fallback text (the
    // bare id, "keeper") is ALSO what's on screen during the brief window
    // before that fetch settles, so a plain `findByText("keeper")` (which
    // resolves on the FIRST match) would pass on that transient initial
    // render even under a broken id-matching implementation that naively
    // grabbed the first search result instead of filtering by id -- never
    // actually exercising the fallback decision this test means to pin. A
    // handful of drained macrotask ticks (empirically enough here, and far
    // cheaper than a fixed real-time sleep) stands in for "wait until the
    // query has settled" since the component exposes no query-state hook a
    // test could await directly.
    await vi.waitFor(() => {
      expect(
        vi.mocked(fetch).mock.calls.some(([url]) => String(url).includes("/api/personas/search")),
      ).toBe(true);
    });
    for (let i = 0; i < 10; i++) {
      await new Promise((resolve) => setTimeout(resolve, 0));
    }

    const header = document.querySelector(".agent-detail__header") as HTMLElement;
    expect(within(header).getByText("keeper")).not.toBeNull();
    expect(within(header).queryByText(/Someone Else/)).toBeNull();
    expect(within(header).queryByText(/--/)).toBeNull();
  });
});

describe("AgentDetail Access tab (access-control Task C2)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    vi.stubGlobal("EventSource", NoopEventSource);
    location.hash = "#/agents/warden";
    window.dispatchEvent(new Event("hashchange"));
  });

  /** Every route AgentDetail's panels touch, with `/api/agents/warden/invites`
   * answering `invitesStatus`/`invitesBody` -- the one thing that decides
   * whether the Access tab shows at all (per `tabs.ts`'s own doc comment,
   * never `whoami.operator`). `/api/whoami` and the bare `/api/agents` list
   * are included unconditionally since RequestAccessCard (mounted whenever
   * invites 403s) reads both for its own-agents picker. */
  function stubAccessProbe(invitesStatus: number, invitesBody: unknown): void {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/agents/warden/invites")) {
          return new Response(JSON.stringify(invitesBody), {
            status: invitesStatus,
            headers: { "Content-Type": "application/json" },
          });
        }
        const routes: Record<string, unknown> = {
          "/api/colleagues/feed": { entries: [], truncated: false },
          "/api/agents/warden/knowledge": { files: [] },
          "/api/agents/warden/intake": { files: [] },
          "/api/agents/warden/skills": { skills: [] },
          "/api/agents/warden/tooling": {
            files: [],
            enforcement: { attested: false, locked: false, locked_file: null },
          },
          "/api/agents/warden/grants": { grants: [] },
          "/api/agents/warden": READY_AGENT,
          "/api/whoami": {
            email: "me@example.com",
            operator: false,
            groups: [],
            groups_unavailable: false,
          },
          "/api/agents": { agents: [], groups_unavailable: false },
          "/api/personas/search": { results: [] },
        };
        for (const [needle, payload] of Object.entries(routes)) {
          if (url.includes(needle)) {
            return new Response(JSON.stringify(payload), {
              status: 200,
              headers: { "Content-Type": "application/json" },
            });
          }
        }
        return new Response(JSON.stringify({}), { status: 200 });
      }),
    );
  }

  it("owner/operator (invites 200): the Access tab shows, and its People/Agents sections render", async () => {
    stubAccessProbe(200, { incoming: [], outgoing: [] });
    renderWithClient(AgentDetail);

    const accessTab = await screen.findByRole("tab", { name: "Access" });
    expect((accessTab as HTMLButtonElement).disabled).toBe(false);
    await fireEvent.click(accessTab);

    const panel = document.getElementById("panel-access") as HTMLElement;
    expect(within(panel).getByText("People")).not.toBeNull();
    expect(within(panel).getByText("Agents")).not.toBeNull();
    // No group on this fixture (group_name: null) -- the no-group fact
    // line, not the "Everyone in <group>" one.
    expect(within(panel).getByText(/This agent is not in a group\./)).not.toBeNull();

    // A caller who already administers this agent isn't shown the
    // request-access affordance meant for someone else's agent.
    expect(screen.queryByText("Request colleague access")).toBeNull();
  });

  it("stranger (invites 403): no Access tab, but the Request colleague access card renders instead", async () => {
    stubAccessProbe(403, { error: "Only this agent's owner or a room operator can change it." });
    renderWithClient(AgentDetail);

    await screen.findByRole("tab", { name: "Chat" });
    expect(await screen.findByText("Request colleague access")).not.toBeNull();
    expect(screen.queryByRole("tab", { name: "Access" })).toBeNull();
  });

  it("a caller who owns nothing sees the explanatory copy in the request-access card, not a picker", async () => {
    stubAccessProbe(403, { error: "Only this agent's owner or a room operator can change it." });
    renderWithClient(AgentDetail);

    expect(
      await screen.findByText(/you need an agent of your own/i),
    ).not.toBeNull();
    expect(screen.queryByRole("combobox")).toBeNull();
  });
});

describe("AgentDetail for a connected agent (design 2026-09-21)", () => {
  // A connected row as the read route serves one: no persona anybody can
  // look up, no capability, no skills, no purpose tags, and a repository
  // that belongs to its team.
  const CONNECTED_AGENT = {
    ...READY_AGENT,
    kind: "connected",
    persona: "connected",
    capability: null,
    skills: [],
    purpose_tags: [],
    repo_full_name: "acme/release-bot",
  };

  beforeEach(() => {
    vi.unstubAllGlobals();
    vi.stubGlobal("EventSource", NoopEventSource);
    location.hash = "#/agents/warden";
    window.dispatchEvent(new Event("hashchange"));
    stubFetch({ ...BASE_ROUTES, "/api/agents/warden": CONNECTED_AGENT });
  });

  it("renders no chat or slack, and keeps Harness and Settings", async () => {
    renderWithClient(AgentDetail);

    // The header renders for every kind, so it is the wait point here --
    // there is no tab selected on this page.
    await screen.findByRole("heading", { name: "warden" });
    expect(screen.queryByRole("tab", { name: "Chat" })).toBeNull();
    expect(screen.queryByRole("tab", { name: "Slack" })).toBeNull();
    // Harness survives because a connected agent still runs on something,
    // and Settings because a build log, the secrets a team's own service
    // has exactly as much of, and a destroy belong to any workload the room
    // deploys.
    const row = document.querySelector(".detail-tabs__list") as HTMLElement;
    expect(within(row).getAllByRole("tab").map((t) => t.textContent?.trim())).toEqual([
      "Access",
      "Harness",
      "Settings",
    ]);
  });

  it("reduces its Harness to what it runs on, and says why", async () => {
    renderWithClient(AgentDetail);

    await fireEvent.click(await screen.findByRole("tab", { name: "Harness" }));
    const nav = screen.getByRole("navigation", { name: "Harness sections" });
    expect(within(nav).getAllByRole("button").map((b) => b.textContent?.trim())).toEqual([
      "Model and runtime",
    ]);
    expect(
      screen.getByText(
        "This agent brings its own harness. The room shows what it runs on and nothing more.",
      ),
    ).not.toBeNull();
  });

  it("shows the connected marker and the repository itself as the link", async () => {
    renderWithClient(AgentDetail);

    const header = (await screen.findByRole("heading", { name: "warden" }))
      .closest(".agent-detail__header") as HTMLElement;
    expect(within(header).getByText("connected")).not.toBeNull();
    const repoLink = within(header).getByRole("link", { name: "acme/release-bot" });
    expect(repoLink.getAttribute("href")).toBe("https://github.com/acme/release-bot");
    expect(within(header).queryByText("source")).toBeNull();
  });

  it("renders nothing the room composed: no persona line and no capability sentence", async () => {
    renderWithClient(AgentDetail);

    const header = (await screen.findByRole("heading", { name: "warden" }))
      .closest(".agent-detail__header") as HTMLElement;
    expect(header.querySelector(".agent-detail__persona")).toBeNull();
    expect(header.querySelector(".agent-detail__capability")).toBeNull();
    expect(
      screen.queryByText("Not written yet. It arrives after first contact."),
    ).toBeNull();
    // Not merely closed behind the menu: open it and there is no such item.
    await openHeaderMenu();
    expect(screen.queryByRole("button", { name: /Edit details/ })).toBeNull();
  });

  it("keeps purpose chips off the page, since nothing here can edit them", async () => {
    // A converted row (Task 6) still carries whatever tags it had.
    stubFetch({
      ...BASE_ROUTES,
      "/api/agents/warden": { ...CONNECTED_AGENT, purpose_tags: ["ops", "oncall"] },
    });
    renderWithClient(AgentDetail);

    await screen.findByRole("heading", { name: "warden" });
    expect(screen.queryByText("ops")).toBeNull();
    expect(screen.queryByText("oncall")).toBeNull();
  });

  it("shows the group beside the owner, the one ownership fact it still has", async () => {
    stubFetch({
      ...BASE_ROUTES,
      "/api/agents/warden": {
        ...CONNECTED_AGENT,
        created_by: "owner@example.com",
        group_name: "platform",
      },
    });
    renderWithClient(AgentDetail);

    const header = (await screen.findByRole("heading", { name: "warden" }))
      .closest(".agent-detail__header") as HTMLElement;
    expect(within(header).getByText("Made by owner@example.com")).not.toBeNull();
    expect(within(header).getByText("platform")).not.toBeNull();
  });

  it("hands the Danger zone the kind, so it does not promise to archive the team's repository", async () => {
    stubFetch({
      ...BASE_ROUTES,
      "/api/agents/warden": { ...CONNECTED_AGENT, destroy_visible: true },
    });
    renderWithClient(AgentDetail);

    await screen.findByRole("heading", { name: "warden" });
    await fireEvent.click(await screen.findByRole("tab", { name: "Settings" }));
    await fireEvent.click(screen.getByRole("button", { name: "Destroy..." }));

    expect(
      await screen.findByText(
        "This destroys the running agent and its platform identity. The repository belongs to your team and is left as it is. Memories die with the namespace.",
      ),
    ).not.toBeNull();
  });

  it("still keeps the Access tab when the caller may administer it", async () => {
    stubFetch({
      ...BASE_ROUTES,
      "/api/agents/warden/invites": { incoming: [], outgoing: [] },
      "/api/agents/warden": CONNECTED_AGENT,
    });
    renderWithClient(AgentDetail);

    expect(await screen.findByRole("tab", { name: "Access" })).not.toBeNull();
    expect(screen.queryByRole("tab", { name: "Chat" })).toBeNull();
  });
});

describe("AgentDetail convert to connected (design 2026-09-21)", () => {
  // The operator action for an agent the room composed whose repository has
  // stopped running the room's runtime. Offered off `destroy_visible`, the
  // operator flag the detail already carries: the server's convert gate is
  // the same operator gate that flag is computed from.
  const OPERATOR_VIEW = { ...READY_AGENT, destroy_visible: true };

  beforeEach(() => {
    vi.unstubAllGlobals();
    vi.stubGlobal("EventSource", NoopEventSource);
    location.hash = "#/agents/warden";
    window.dispatchEvent(new Event("hashchange"));
    stubFetch({ ...BASE_ROUTES, "/api/agents/warden": OPERATOR_VIEW });
  });

  /** Opens the Settings tab, where both one-way changes live since the
   * page-shell wave, and returns its panel. */
  async function openSettings(): Promise<HTMLElement> {
    await screen.findByRole("tab", { name: "Settings" });
    await fireEvent.click(screen.getByRole("tab", { name: "Settings" }));
    return document.getElementById("panel-settings") as HTMLElement;
  }

  it("offers the action to an operator looking at a composed, ready agent", async () => {
    renderWithClient(AgentDetail);
    const settings = await openSettings();

    expect(within(settings).getByText("Convert to connected")).not.toBeNull();
    expect(within(settings).getByRole("button", { name: "Convert..." })).not.toBeNull();
  });

  it("keeps the action out of every other tab, where it used to render", async () => {
    renderWithClient(AgentDetail);
    await screen.findByRole("tab", { name: "Chat" });

    const chatPanel = document.getElementById("panel-chat") as HTMLElement;
    expect(chatPanel.textContent).not.toContain("Convert to connected");
  });

  it("says what stops working before it will convert", async () => {
    renderWithClient(AgentDetail);
    await openSettings();
    await fireEvent.click(screen.getByRole("button", { name: "Convert..." }));

    expect(
      screen.getByText(
        "The room stops editing this agent: no chat, memory, learnings, skills, tooling or Slack from here, and other agents can no longer reach it. Its repository, deployment and cloud identity stay as they are. This cannot be undone from the room.",
      ),
    ).not.toBeNull();
    const confirm = screen.getByRole("button", {
      name: "Convert this agent",
    }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(false);
  });

  it("asks no run mode question, since a composed agent already serves web traffic", async () => {
    renderWithClient(AgentDetail);
    await openSettings();
    await fireEvent.click(screen.getByRole("button", { name: "Convert..." }));

    expect(screen.queryByRole("radio")).toBeNull();
    expect(
      screen.queryByText("It does not listen on a port (a chat bot or a worker)"),
    ).toBeNull();
  });

  it("drops the selected tab when the convert takes its tab away", async () => {
    // The poll right after a convert brings back a kind with no Chat tab.
    // Before this was handled, "chat" stayed selected against a tab bar
    // that no longer offered it, and the panel area under it was empty.
    const { client } = renderWithClient(AgentDetail);
    const chatTab = await screen.findByRole("tab", { name: "Chat" });
    expect(chatTab.getAttribute("aria-selected")).toBe("true");

    client.setQueryData(["agents", "warden"], {
      ...OPERATOR_VIEW,
      kind: "connected",
    });
    await screen.findByRole("tab", { name: "Access" });

    expect(screen.queryByRole("tab", { name: "Chat" })).toBeNull();
    for (const tab of screen.getAllByRole("tab")) {
      expect(tab.getAttribute("aria-selected")).toBe("false");
    }
  });

  // The three refusals below read the Settings panel's own text rather
  // than asking for a visible control: every panel on this page stays
  // mounted while hidden, so "the button is not reachable" would pass even
  // for a row that is there and merely behind another tab. The row has to
  // be absent, not hidden.

  it("hides the action from a caller who is not an operator", async () => {
    stubFetch({ ...BASE_ROUTES, "/api/agents/warden": READY_AGENT });
    renderWithClient(AgentDetail);

    await screen.findByRole("heading", { name: "warden" });
    const settings = document.getElementById("panel-settings") as HTMLElement;
    expect(settings.textContent).toContain("Build log");
    expect(settings.textContent).not.toContain("Convert to connected");
  });

  it("hides the action on an agent that is already connected", async () => {
    stubFetch({
      ...BASE_ROUTES,
      "/api/agents/warden": { ...OPERATOR_VIEW, kind: "connected" },
    });
    renderWithClient(AgentDetail);

    await screen.findByRole("heading", { name: "warden" });
    const settings = document.getElementById("panel-settings") as HTMLElement;
    expect(settings.textContent).not.toContain("Convert to connected");
  });

  it("hides the action while the agent is still being provisioned", async () => {
    stubFetch({
      ...BASE_ROUTES,
      "/api/agents/warden": { ...OPERATOR_VIEW, status: "provisioning" },
    });
    renderWithClient(AgentDetail);

    await screen.findByRole("heading", { name: "warden" });
    const settings = document.getElementById("panel-settings") as HTMLElement;
    expect(settings.textContent).not.toContain("Convert to connected");
  });
});
