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

  it("switches tabs among primary and expanded advanced without unmounting panels", async () => {
    stubFetch({ ...BASE_ROUTES, "/api/agents/warden": { ...READY_AGENT, can_decide_learnings: true } });
    renderWithClient(AgentDetail);
    const chatTab = await screen.findByRole("tab", { name: "Chat" });
    expect(chatTab.getAttribute("aria-selected")).toBe("true");
    expect(screen.queryByRole("tab", { name: "Slack" })).not.toBeNull();
    // The group starts collapsed for everyone (2026-09-02 ruling), so open
    // it first -- the toggle mechanic itself has dedicated tests below.
    await fireEvent.click(screen.getByRole("button", { name: /advanced/i }));

    // Ready is the one status where every tab trigger is reachable — none
    // of them dead-end into a 409, so none should be disabled.
    for (const label of ["Chat", "Slack", "Learnings", "Memory", "Skills", "Tooling"]) {
      expect((screen.getByRole("tab", { name: label }) as HTMLButtonElement).disabled).toBe(
        false,
      );
    }

    const memoryTab = screen.getByRole("tab", { name: "Memory" });
    await fireEvent.click(memoryTab);
    expect(memoryTab.getAttribute("aria-selected")).toBe("true");
    expect(document.getElementById("panel-chat")?.hasAttribute("hidden")).toBe(true);
    expect(document.getElementById("panel-chat")).not.toBeNull();
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
    await fireEvent.click(screen.getByRole("button", { name: /advanced/i }));

    const memoryTab = screen.getByRole("tab", { name: "Memory" }) as HTMLButtonElement;
    const skillsTab = screen.getByRole("tab", { name: "Skills" }) as HTMLButtonElement;
    const toolingTab = screen.getByRole("tab", { name: "Tooling" }) as HTMLButtonElement;
    const slackTab = screen.getByRole("tab", { name: "Slack" }) as HTMLButtonElement;
    expect((chatTab as HTMLButtonElement).disabled).toBe(true);
    expect(memoryTab.disabled).toBe(true);
    expect(skillsTab.disabled).toBe(true);
    expect(toolingTab.disabled).toBe(true);
    expect(slackTab.disabled).toBe(true);

    // A disabled trigger can't be selected at all — no default tab was
    // chosen either (defaultTab returns null for a non-ready status), so
    // clicking just leaves every trigger unselected.
    await fireEvent.click(memoryTab);
    expect(memoryTab.getAttribute("aria-selected")).toBe("false");
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
    const editButton = await screen.findByRole("button", { name: "Edit details" });
    expect((editButton as HTMLButtonElement).disabled).toBe(true);
  });

  it("collapses the Advanced group by default and expands it on toggle", async () => {
    // READY_AGENT already has can_decide_learnings: false.
    renderWithClient(AgentDetail);
    await screen.findByRole("tab", { name: "Chat" });
    expect(screen.queryByRole("tab", { name: "Memory" })).toBeNull();
    await fireEvent.click(screen.getByRole("button", { name: /advanced/i }));
    expect(screen.getByRole("tab", { name: "Memory" })).not.toBeNull();
  });

  it("starts collapsed for deciders too (2026-09-02 ruling)", async () => {
    stubFetch({
      ...BASE_ROUTES,
      "/api/agents/warden": { ...READY_AGENT, can_decide_learnings: true },
    });
    renderWithClient(AgentDetail);
    await screen.findByRole("tab", { name: "Chat" });
    expect(screen.queryByRole("tab", { name: "Learnings" })).toBeNull();
    await fireEvent.click(screen.getByRole("button", { name: /advanced/i }));
    expect(screen.getByRole("tab", { name: "Learnings" })).not.toBeNull();
  });

  it("shows the pending badge on the Advanced toggle while collapsed", async () => {
    stubFetch({
      ...BASE_ROUTES,
      "/api/agents/warden": { ...READY_AGENT, can_decide_learnings: false, pending_learnings: 3 },
    });
    renderWithClient(AgentDetail);
    await screen.findByRole("tab", { name: "Chat" });
    const toggle = screen.getByRole("button", { name: /advanced/i });
    expect(toggle.textContent).toContain("3");
  });

  it("collapsing Advanced while an advanced tab is selected returns to a coherent state", async () => {
    stubFetch({
      ...BASE_ROUTES,
      "/api/agents/warden": { ...READY_AGENT, can_decide_learnings: true },
    });
    renderWithClient(AgentDetail);
    await screen.findByRole("tab", { name: "Chat" });
    // The group starts collapsed for everyone: open it, then select Memory.
    await fireEvent.click(screen.getByRole("button", { name: /advanced/i }));
    const memoryTab = screen.getByRole("tab", { name: "Memory" });
    await fireEvent.click(memoryTab);
    expect(memoryTab.getAttribute("aria-selected")).toBe("true");

    await fireEvent.click(screen.getByRole("button", { name: /advanced/i }));

    // Memory's own trigger is gone now that the group collapsed, and its
    // panel is hidden -- nothing is left selected-but-invisible, and Chat
    // (the coherent fallback) is the one tab left selected.
    expect(screen.queryByRole("tab", { name: "Memory" })).toBeNull();
    expect(document.getElementById("panel-memory")?.hasAttribute("hidden")).toBe(true);
    const chatTab = screen.getByRole("tab", { name: "Chat" });
    expect(chatTab.getAttribute("aria-selected")).toBe("true");
    const selectedTabs = screen
      .getAllByRole("tab")
      .filter((t) => t.getAttribute("aria-selected") === "true");
    expect(selectedTabs).toEqual([chatTab]);
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
    // The Advanced group starts collapsed for everyone (2026-09-02 ruling);
    // this test reaches the Memory tab, so open the group first.
    await fireEvent.click(screen.getByRole("button", { name: /advanced/i }));
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
    expect(screen.getByRole("tab", { name: "Memory" })).not.toBeNull();
    const chatTab = screen.getByRole("tab", { name: "Chat" });
    expect(chatTab.getAttribute("aria-selected")).toBe("false");
    const avatarButton = screen.getByRole("button", { name: /give warden a face/i });
    expect((avatarButton as HTMLButtonElement).disabled).toBe(true);

    // Frozen (destroying/destroy_failed) is the one non-ready status where
    // tabs stay fully clickable — the destruction ruling makes read-only
    // content reachable even though it's not "ready"; only per-panel
    // CONTROLS (like the avatar button above) freeze, never the tabs
    // themselves.
    for (const label of ["Chat", "Memory", "Skills", "Tooling"]) {
      expect((screen.getByRole("tab", { name: label }) as HTMLButtonElement).disabled).toBe(
        false,
      );
    }
  });

  it("renders the danger zone below the tabs while destroying", async () => {
    stubFetch({
      ...BASE_ROUTES,
      "/api/agents/warden": { ...READY_AGENT, status: "destroying", destroy_visible: true },
    });
    renderWithClient(AgentDetail);
    await screen.findByRole("tab", { name: "Chat" });
    expect(screen.getByText("Danger zone")).not.toBeNull();
  });

  it('shows a "Download the portrait" link, with the per-agent filename, when has_avatar is true', async () => {
    stubFetch({
      ...BASE_ROUTES,
      "/api/agents/warden": { ...READY_AGENT, has_avatar: true },
    });
    renderWithClient(AgentDetail);
    // The avatar action block lives in the always-visible header now, not
    // behind any tab — waiting for Chat just confirms the page finished
    // mounting.
    await screen.findByRole("tab", { name: "Chat" });

    const link = (await screen.findByRole("link", {
      name: "Download the portrait",
    })) as HTMLAnchorElement;
    expect(link.getAttribute("download")).toBe("warden-portrait.png");
    expect(link.getAttribute("href")).toContain("/api/agents/warden/avatar");
  });

  it('hides "Download the portrait" when has_avatar is false (nothing to download yet)', async () => {
    renderWithClient(AgentDetail);
    await screen.findByRole("tab", { name: "Chat" });

    expect(screen.queryByRole("link", { name: "Download the portrait" })).toBeNull();
  });

  it('a file over the 2 MiB cap shows the plain client-side copy and never PUTs', async () => {
    renderWithClient(AgentDetail);
    await screen.findByRole("tab", { name: "Chat" });

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

  it("renders `{name} -- {who_they_are}` once the persona catalog search answers with a matching id", async () => {
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
    expect(
      await within(header).findByText("Keeper -- a calm, steady presence who never panics"),
    ).not.toBeNull();
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

  it("renders no chat, slack or advanced tabs, and no Advanced toggle", async () => {
    renderWithClient(AgentDetail);

    // The header renders for every kind, so it is the wait point here --
    // there is no tab to wait for on this page.
    await screen.findByRole("heading", { name: "warden" });
    expect(screen.queryByRole("tab", { name: "Chat" })).toBeNull();
    expect(screen.queryByRole("tab", { name: "Slack" })).toBeNull();
    expect(screen.queryByRole("tab", { name: "Skills" })).toBeNull();
    expect(screen.queryByRole("button", { name: /Advanced/ })).toBeNull();
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

  it("offers the action to an operator looking at a composed, ready agent", async () => {
    renderWithClient(AgentDetail);

    expect(
      await screen.findByRole("button", { name: "Convert to connected" }),
    ).not.toBeNull();
  });

  it("says what stops working before it will convert", async () => {
    renderWithClient(AgentDetail);
    await fireEvent.click(
      await screen.findByRole("button", { name: "Convert to connected" }),
    );

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
    await fireEvent.click(
      await screen.findByRole("button", { name: "Convert to connected" }),
    );

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

  it("hides the action from a caller who is not an operator", async () => {
    stubFetch({ ...BASE_ROUTES, "/api/agents/warden": READY_AGENT });
    renderWithClient(AgentDetail);

    await screen.findByRole("heading", { name: "warden" });
    expect(screen.queryByRole("button", { name: "Convert to connected" })).toBeNull();
  });

  it("hides the action on an agent that is already connected", async () => {
    stubFetch({
      ...BASE_ROUTES,
      "/api/agents/warden": { ...OPERATOR_VIEW, kind: "connected" },
    });
    renderWithClient(AgentDetail);

    await screen.findByRole("heading", { name: "warden" });
    expect(screen.queryByRole("button", { name: "Convert to connected" })).toBeNull();
  });

  it("hides the action while the agent is still being provisioned", async () => {
    stubFetch({
      ...BASE_ROUTES,
      "/api/agents/warden": { ...OPERATOR_VIEW, status: "provisioning" },
    });
    renderWithClient(AgentDetail);

    await screen.findByRole("heading", { name: "warden" });
    expect(screen.queryByRole("button", { name: "Convert to connected" })).toBeNull();
  });
});
