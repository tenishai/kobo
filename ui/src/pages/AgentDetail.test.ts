// @vitest-environment jsdom
import { fireEvent, screen, within } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import AgentDetail from "./AgentDetail.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

const READY_AGENT = {
  name: "warden",
  persona: "keeper",
  team: "shared",
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
};

describe("AgentDetail tabs", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
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
    // Against the bare `{}` catch-all, each of those becomes `undefined`
    // (or, for tooling, a `{}` with no `.files`) and the panel throws
    // reading `.length`/`.some` before it ever gets to render an empty
    // state — so each gets its own minimal, healthy payload here instead.
    stubFetch({
      "/api/agents/warden/knowledge": { files: [] },
      "/api/agents/warden/intake": { files: [] },
      "/api/agents/warden/skills": { skills: [] },
      "/api/agents/warden/tooling": {
        files: [],
        enforcement: { attested: false, locked: false, locked_file: null },
      },
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
      "/api/agents/warden/knowledge": { files: [] },
      "/api/agents/warden/intake": { files: [] },
      "/api/agents/warden/skills": { skills: [] },
      "/api/agents/warden/tooling": {
        files: [],
        enforcement: { attested: false, locked: false, locked_file: null },
      },
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
      "/api/agents/warden/knowledge": { files: [] },
      "/api/agents/warden/intake": { files: [] },
      "/api/agents/warden/skills": { skills: [] },
      "/api/agents/warden/tooling": {
        files: [],
        enforcement: { attested: false, locked: false, locked_file: null },
      },
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
      "/api/agents/warden/knowledge": { files: [] },
      "/api/agents/warden/intake": { files: [] },
      "/api/agents/warden/skills": { skills: [] },
      "/api/agents/warden/tooling": {
        files: [],
        enforcement: { attested: false, locked: false, locked_file: null },
      },
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

  it("shows the Slack tab, and renders the setup section unconditionally inside it", async () => {
    // Slack self-service (2026-08-17): the setup section (manifest reveal,
    // instructions, three paste fields) renders for every signed-in user
    // now, no gate. The agent payload's old visibility flag for the setup
    // lane is gone entirely, and AgentDetail no longer threads any such
    // prop into SlackPanel.
    stubFetch({
      "/api/agents/warden/knowledge": { files: [] },
      "/api/agents/warden/intake": { files: [] },
      "/api/agents/warden/skills": { skills: [] },
      "/api/agents/warden/tooling": {
        files: [],
        enforcement: { attested: false, locked: false, locked_file: null },
      },
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
      "/api/agents/warden/knowledge": { files: [] },
      "/api/agents/warden/intake": { files: [] },
      "/api/agents/warden/skills": { skills: [] },
      "/api/agents/warden/tooling": {
        files: [],
        enforcement: { attested: false, locked: false, locked_file: null },
      },
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
});
