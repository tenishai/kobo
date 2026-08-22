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
    // The generic `"/api/"` catch-all below answers with `{}`, which is
    // enough for panels that only ever read their query data through an
    // optional chain (SlackPanel's `slack` prop; SkillsPanel's team
    // filter, guarded by `{#if $teams.data}`). But every panel mounted here
    // is mounted unconditionally per the tab invariant (all four panels
    // stay mounted; only the tabpanel's `hidden` toggles), and each
    // destructures its own query's real response shape on first render
    // (queries.ts / api.ts):
    //   - MemoryPanel:  knowledgeQuery -> listKnowledge -> reads `data.files`
    //   - MemoryPanel:  intakeQuery    -> listIntake    -> reads `data.files`
    //   - SkillsPanel:  agentSkillsQuery -> listSkills  -> reads `data.skills`
    //   - ToolingPanel: toolingQuery  -> listTooling    -> the body itself is
    //                   `{ files, enforcement }` (ToolingListResponse)
    //   - ColleagueConversations: colleagueConversationsQuery ->
    //                   listColleagueConversations -> reads `data.length`
    // Against the bare `{}` catch-all, each of those becomes `undefined`
    // (or, for tooling, a `{}` with no `.files`) and the panel throws
    // reading `.length`/`.some` before it ever gets to render an empty
    // state — so each gets its own minimal, healthy payload here instead.
    stubFetch({
      "/api/colleagues/conversations": { conversations: [] },
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
      "/api/": {},
    });
  });

  it("defaults to Chat when ready and switches tabs without unmounting panels", async () => {
    renderWithClient(AgentDetail);
    const chatTab = await screen.findByRole("tab", { name: "Chat" });
    expect(chatTab.getAttribute("aria-selected")).toBe("true");
    // Two-lane Slack (2026-08-17): the tab is unconditional now, not gated
    // on any visibility flag.
    expect(screen.queryByRole("tab", { name: "Slack" })).not.toBeNull();

    // Ready is the one status where every tab trigger is reachable — none
    // of them dead-end into a 409, so none should be disabled.
    for (const label of ["Overview", "Chat", "Memory", "Skills", "Tooling", "Slack"]) {
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

  it("disables the non-Overview tab triggers while provisioning, so a click can't dead-end into a 409", async () => {
    stubFetch({
      "/api/colleagues/conversations": { conversations: [] },
      "/api/agents/warden/knowledge": { files: [] },
      "/api/agents/warden/intake": { files: [] },
      "/api/agents/warden/skills": { skills: [] },
      "/api/agents/warden/tooling": {
        files: [],
        enforcement: { attested: false, locked: false, locked_file: null },
      },
      "/api/agents/warden/invites": { incoming: [], outgoing: [] },
      "/api/agents/warden/grants": { grants: [] },
      "/api/agents/warden": { ...READY_AGENT, status: "provisioning" },
      "/api/": {},
    });
    renderWithClient(AgentDetail);
    const overviewTab = await screen.findByRole("tab", { name: "Overview" });
    expect(overviewTab.getAttribute("aria-selected")).toBe("true");
    expect((overviewTab as HTMLButtonElement).disabled).toBe(false);

    const chatTab = screen.getByRole("tab", { name: "Chat" }) as HTMLButtonElement;
    const memoryTab = screen.getByRole("tab", { name: "Memory" }) as HTMLButtonElement;
    const skillsTab = screen.getByRole("tab", { name: "Skills" }) as HTMLButtonElement;
    const toolingTab = screen.getByRole("tab", { name: "Tooling" }) as HTMLButtonElement;
    expect(chatTab.disabled).toBe(true);
    expect(memoryTab.disabled).toBe(true);
    expect(skillsTab.disabled).toBe(true);
    expect(toolingTab.disabled).toBe(true);

    // The root of review-Important-2's gap: a disabled trigger can't be
    // selected at all, so Memory's 409-then-ErrorState path is never
    // reached by clicking in from here — selection just stays put.
    await fireEvent.click(memoryTab);
    expect(overviewTab.getAttribute("aria-selected")).toBe("true");
    expect(memoryTab.getAttribute("aria-selected")).toBe("false");
  });

  it("lands on Overview on mount for a non-ready status (destroying)", async () => {
    stubFetch({
      "/api/colleagues/conversations": { conversations: [] },
      "/api/agents/warden/knowledge": { files: [] },
      "/api/agents/warden/intake": { files: [] },
      "/api/agents/warden/skills": { skills: [] },
      "/api/agents/warden/tooling": {
        files: [],
        enforcement: { attested: false, locked: false, locked_file: null },
      },
      "/api/agents/warden/invites": { incoming: [], outgoing: [] },
      "/api/agents/warden/grants": { grants: [] },
      "/api/agents/warden": { ...READY_AGENT, status: "destroying", destroy_visible: true },
      "/api/": {},
    });
    renderWithClient(AgentDetail);
    const overviewTab = await screen.findByRole("tab", { name: "Overview" });
    expect(overviewTab.getAttribute("aria-selected")).toBe("true");
    expect(screen.getByRole("tab", { name: "Chat" }).getAttribute("aria-selected")).toBe("false");
  });

  it("renders the checklist and first words inside the Overview tabpanel", async () => {
    stubFetch({
      "/api/colleagues/conversations": { conversations: [] },
      "/api/agents/warden/knowledge": { files: [] },
      "/api/agents/warden/intake": { files: [] },
      "/api/agents/warden/skills": { skills: [] },
      "/api/agents/warden/tooling": {
        files: [],
        enforcement: { attested: false, locked: false, locked_file: null },
      },
      "/api/agents/warden/invites": { incoming: [], outgoing: [] },
      "/api/agents/warden/grants": { grants: [] },
      "/api/agents/warden": {
        ...READY_AGENT,
        steps: [{ name: "clone", label: "Clone the repo", status: "done", error: null }],
        first_reply: "Hello there.",
      },
      "/api/": {},
    });
    renderWithClient(AgentDetail);
    await screen.findByRole("tab", { name: "Chat" });
    const overviewPanel = document.getElementById("panel-overview") as HTMLElement;
    expect(overviewPanel).not.toBeNull();
    expect(within(overviewPanel).getByText("Clone the repo")).not.toBeNull();
    expect(within(overviewPanel).getByText("Hello there.")).not.toBeNull();
  });

  it("composition card shows the team chip, purpose chips, and the null-capability copy verbatim", async () => {
    stubFetch({
      "/api/colleagues/conversations": { conversations: [] },
      "/api/agents/warden/knowledge": { files: [] },
      "/api/agents/warden/intake": { files: [] },
      "/api/agents/warden/skills": { skills: [] },
      "/api/agents/warden/tooling": {
        files: [],
        enforcement: { attested: false, locked: false, locked_file: null },
      },
      "/api/agents/warden/invites": { incoming: [], outgoing: [] },
      "/api/agents/warden/grants": { grants: [] },
      "/api/agents/warden": {
        ...READY_AGENT,
        team: "growth",
        purpose_tags: ["billing", "support"],
        capability: null,
      },
      "/api/": {},
    });
    renderWithClient(AgentDetail);
    await screen.findByRole("tab", { name: "Chat" });
    const overviewPanel = document.getElementById("panel-overview") as HTMLElement;
    expect(within(overviewPanel).getByText("growth")).not.toBeNull();
    expect(within(overviewPanel).getByText("billing")).not.toBeNull();
    expect(within(overviewPanel).getByText("support")).not.toBeNull();
    expect(
      within(overviewPanel).getByText(
        "Not written yet. It arrives after first contact.",
      ),
    ).not.toBeNull();
  });

  it("composition card shows the earned capability line verbatim when present", async () => {
    stubFetch({
      "/api/colleagues/conversations": { conversations: [] },
      "/api/agents/warden/knowledge": { files: [] },
      "/api/agents/warden/intake": { files: [] },
      "/api/agents/warden/skills": { skills: [] },
      "/api/agents/warden/tooling": {
        files: [],
        enforcement: { attested: false, locked: false, locked_file: null },
      },
      "/api/agents/warden/invites": { incoming: [], outgoing: [] },
      "/api/agents/warden/grants": { grants: [] },
      "/api/agents/warden": {
        ...READY_AGENT,
        capability: "Answers billing questions and escalates refunds.",
      },
      "/api/": {},
    });
    renderWithClient(AgentDetail);
    await screen.findByRole("tab", { name: "Chat" });
    const overviewPanel = document.getElementById("panel-overview") as HTMLElement;
    expect(
      within(overviewPanel).getByText(
        "Answers billing questions and escalates refunds.",
      ),
    ).not.toBeNull();
    expect(
      within(overviewPanel).queryByText(/Not written yet/),
    ).toBeNull();
  });

  it("Overview shows 'Made by <email>' when created_by is present (Task A3)", async () => {
    stubFetch({
      "/api/colleagues/conversations": { conversations: [] },
      "/api/agents/warden/knowledge": { files: [] },
      "/api/agents/warden/intake": { files: [] },
      "/api/agents/warden/skills": { skills: [] },
      "/api/agents/warden/tooling": {
        files: [],
        enforcement: { attested: false, locked: false, locked_file: null },
      },
      "/api/agents/warden/invites": { incoming: [], outgoing: [] },
      "/api/agents/warden/grants": { grants: [] },
      "/api/agents/warden": { ...READY_AGENT, created_by: "owner@example.com" },
      "/api/": {},
    });
    renderWithClient(AgentDetail);
    await screen.findByRole("tab", { name: "Chat" });
    const overviewPanel = document.getElementById("panel-overview") as HTMLElement;
    expect(within(overviewPanel).getByText("Made by owner@example.com")).not.toBeNull();
  });

  it("Overview renders no owner line when created_by is null", async () => {
    stubFetch({
      "/api/colleagues/conversations": { conversations: [] },
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
      "/api/": {},
    });
    renderWithClient(AgentDetail);
    await screen.findByRole("tab", { name: "Chat" });
    const overviewPanel = document.getElementById("panel-overview") as HTMLElement;
    expect(within(overviewPanel).queryByText(/Made by/)).toBeNull();
  });

  it("the Edit details affordance disables while frozen (destroying)", async () => {
    stubFetch({
      "/api/colleagues/conversations": { conversations: [] },
      "/api/agents/warden/knowledge": { files: [] },
      "/api/agents/warden/intake": { files: [] },
      "/api/agents/warden/skills": { skills: [] },
      "/api/agents/warden/tooling": {
        files: [],
        enforcement: { attested: false, locked: false, locked_file: null },
      },
      "/api/agents/warden/invites": { incoming: [], outgoing: [] },
      "/api/agents/warden/grants": { grants: [] },
      "/api/agents/warden": {
        ...READY_AGENT,
        status: "destroying",
        destroy_visible: true,
      },
      "/api/": {},
    });
    renderWithClient(AgentDetail);
    const editButton = await screen.findByRole("button", { name: "Edit details" });
    expect((editButton as HTMLButtonElement).disabled).toBe(true);
  });

  it("shows the Slack tab, and renders the setup section unconditionally inside it", async () => {
    // Slack self-service (2026-08-17): the setup section (manifest reveal,
    // instructions, three paste fields) renders for every signed-in user
    // now, no gate. The agent payload's old visibility flag for the setup
    // lane is gone entirely, and AgentDetail no longer threads any such
    // prop into SlackPanel.
    stubFetch({
      "/api/colleagues/conversations": { conversations: [] },
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
      "/api/": {},
    });
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
      "/api/colleagues/conversations": { conversations: [] },
      "/api/agents/warden/knowledge": { files: [] },
      "/api/agents/warden/intake": { files: [] },
      "/api/agents/warden/skills": { skills: [] },
      "/api/agents/warden/tooling": {
        files: [],
        enforcement: { attested: false, locked: false, locked_file: null },
      },
      "/api/agents/warden/invites": { incoming: [], outgoing: [] },
      "/api/agents/warden/grants": { grants: [] },
      "/api/agents/warden": { ...READY_AGENT, status: "destroying", destroy_visible: true },
      "/api/": {},
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
    // role="status" unrelated to destroying — always present regardless of
    // agent status — so it's excluded here rather than folded into this
    // count.
    const destroyAnnouncers = screen
      .getAllByRole("status")
      .filter((el) => !el.classList.contains("step-checklist__sr-only"));
    expect(destroyAnnouncers).toHaveLength(1);
    expect(screen.getByRole("tab", { name: "Memory" })).not.toBeNull();
    const overviewTab = screen.getByRole("tab", { name: "Overview" });
    expect(overviewTab.getAttribute("aria-selected")).toBe("true");
    const avatarButton = screen.getByRole("button", { name: /give warden a face/i });
    expect((avatarButton as HTMLButtonElement).disabled).toBe(true);

    // Frozen (destroying/destroy_failed) is the one non-ready status where
    // tabs stay fully clickable — the destruction ruling makes read-only
    // content reachable even though it's not "ready"; only per-panel
    // CONTROLS (like the avatar button above) freeze, never the tabs
    // themselves.
    for (const label of ["Overview", "Chat", "Memory", "Skills", "Tooling"]) {
      expect((screen.getByRole("tab", { name: label }) as HTMLButtonElement).disabled).toBe(
        false,
      );
    }
  });

  it('shows a "Download the portrait" link, with the per-agent filename, when has_avatar is true', async () => {
    stubFetch({
      "/api/colleagues/conversations": { conversations: [] },
      "/api/agents/warden/knowledge": { files: [] },
      "/api/agents/warden/intake": { files: [] },
      "/api/agents/warden/skills": { skills: [] },
      "/api/agents/warden/tooling": {
        files: [],
        enforcement: { attested: false, locked: false, locked_file: null },
      },
      "/api/agents/warden/invites": { incoming: [], outgoing: [] },
      "/api/agents/warden/grants": { grants: [] },
      "/api/agents/warden": { ...READY_AGENT, has_avatar: true },
      "/api/": {},
    });
    renderWithClient(AgentDetail);
    // A ready agent defaults to the Chat tab (defaultTab, tabs.ts) — every
    // panel stays mounted but the Overview tabpanel is `hidden` until
    // switched to, and role queries (unlike getByText) respect that.
    await fireEvent.click(await screen.findByRole("tab", { name: "Overview" }));

    const link = (await screen.findByRole("link", {
      name: "Download the portrait",
    })) as HTMLAnchorElement;
    expect(link.getAttribute("download")).toBe("warden-portrait.png");
    expect(link.getAttribute("href")).toContain("/api/agents/warden/avatar");
  });

  it('hides "Download the portrait" when has_avatar is false (nothing to download yet)', async () => {
    renderWithClient(AgentDetail);
    await fireEvent.click(await screen.findByRole("tab", { name: "Overview" }));

    expect(screen.queryByRole("link", { name: "Download the portrait" })).toBeNull();
  });

  it('a file over the 2 MiB cap shows the plain client-side copy and never PUTs', async () => {
    renderWithClient(AgentDetail);
    await fireEvent.click(await screen.findByRole("tab", { name: "Overview" }));

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
          "/api/colleagues/conversations": { conversations: [] },
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
          "/api/colleagues/conversations": { conversations: [] },
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
