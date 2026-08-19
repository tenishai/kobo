// @vitest-environment jsdom
import { screen } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ChatPanel from "./ChatPanel.svelte";
import MemoryPanel from "./MemoryPanel.svelte";
import MetadataDetails from "./MetadataDetails.svelte";
import SkillsPanel from "./SkillsPanel.svelte";
import SlackPanel from "./SlackPanel.svelte";
import ToolingPanel from "./ToolingPanel.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

const CASES: Array<[string, unknown, Record<string, unknown>]> = [
  ["ChatPanel", ChatPanel, { agentName: "warden", frozen: true }],
  ["MemoryPanel", MemoryPanel, { agentName: "warden", frozen: true }],
  ["SkillsPanel", SkillsPanel, { agentName: "warden", frozen: true }],
  ["ToolingPanel", ToolingPanel, { agentName: "warden", frozen: true }],
  [
    "SlackPanel",
    SlackPanel,
    {
      agentName: "warden",
      frozen: true,
      slack: { app_id: null, channels: [], state: "idle" },
    },
  ],
  [
    "MetadataDetails",
    MetadataDetails,
    {
      agentName: "warden",
      frozen: true,
      team: "shared",
      purposeTags: [],
      capability: null,
    },
  ],
];

describe("frozen panels disable every control", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    // Each panel mounted here queries its own real endpoint on mount
    // (MemoryPanel: knowledge + intake; SkillsPanel: skills; ToolingPanel:
    // tooling) — same reasoning as AgentDetail.test.ts's stub set (queries.ts
    // / api.ts): against the bare `{}` catch-all these come back shaped
    // wrong and the panel throws before it ever renders a control. ChatPanel
    // has no query of its own; SlackPanel's two queries (delivery, and the
    // Slack self-service manifest reveal) are both disabled until a sha
    // exists / the manifest control is clicked, so neither needs a dedicated
    // stub beyond the catch-all.
    stubFetch({
      "/api/agents/warden/knowledge": { files: [] },
      "/api/agents/warden/intake": { files: [] },
      "/api/agents/warden/skills": { skills: [] },
      "/api/agents/warden/tooling": {
        files: [],
        enforcement: { attested: false, locked: false, locked_file: null },
      },
      "/api/": {},
    });
  });

  for (const [label, component, props] of CASES) {
    it(`${label} disables all interactive controls when frozen`, async () => {
      const { container } = renderWithClient(component, props);
      await new Promise((resolve) => setTimeout(resolve, 0));
      const controls = container.querySelectorAll("button, input, textarea, select");
      expect(controls.length).toBeGreaterThan(0);
      for (const control of controls) {
        expect((control as HTMLButtonElement).disabled, `${label}: ${control.outerHTML.slice(0, 80)}`).toBe(true);
      }
    });
  }
});
