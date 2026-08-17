// @vitest-environment jsdom
import { fireEvent, screen } from "@testing-library/svelte";
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
  slack: null,
  slack_visible: false,
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

  it("defaults to Memory and switches tabs without unmounting panels", async () => {
    renderWithClient(AgentDetail);
    const memoryTab = await screen.findByRole("tab", { name: "Memory" });
    expect(memoryTab.getAttribute("aria-selected")).toBe("true");
    expect(screen.queryByRole("tab", { name: "Slack" })).toBeNull();

    const skillsTab = screen.getByRole("tab", { name: "Skills" });
    await fireEvent.click(skillsTab);
    expect(skillsTab.getAttribute("aria-selected")).toBe("true");
    expect(document.getElementById("panel-memory")?.hasAttribute("hidden")).toBe(true);
    expect(document.getElementById("panel-memory")).not.toBeNull();
  });

  it("shows the Slack tab when slack_visible", async () => {
    stubFetch({
      "/api/agents/warden/knowledge": { files: [] },
      "/api/agents/warden/intake": { files: [] },
      "/api/agents/warden/skills": { skills: [] },
      "/api/agents/warden/tooling": {
        files: [],
        enforcement: { attested: false, locked: false, locked_file: null },
      },
      "/api/agents/warden": { ...READY_AGENT, slack_visible: true },
      "/api/": {},
    });
    renderWithClient(AgentDetail);
    expect(await screen.findByRole("tab", { name: "Slack" })).not.toBeNull();
  });
});
