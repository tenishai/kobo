// @vitest-environment jsdom
import { fireEvent, screen } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import AccessPanel from "./AccessPanel.svelte";
import ChatPanel from "./ChatPanel.svelte";
import MemoryPanel from "./MemoryPanel.svelte";
import MetadataDetails from "./MetadataDetails.svelte";
import SkillsPanel from "./SkillsPanel.svelte";
import SlackPanel from "./SlackPanel.svelte";
import ToolingPanel from "./ToolingPanel.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

/** A step that puts a panel's hidden controls on screen before the sweep
 * below counts them. A panel that keeps a whole section behind a
 * disclosure would otherwise pass this suite by rendering nothing. */
type Reveal = () => Promise<void>;

const CASES: Array<[string, unknown, Record<string, unknown>, Reveal?]> = [
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
    async () => {
      // The Slack setup (manifest, steps, three fields) sits behind a
      // disclosure now. It is open by default for an agent that is not
      // connected, which this asserts rather than assumes: a frozen
      // disclosure cannot be clicked open, so if it ever started shut
      // here the sweep below would quietly stop covering those fields.
      const toggle = screen.getByRole("button", { name: "Set up the Slack app" });
      if (toggle.getAttribute("aria-expanded") !== "true") {
        await fireEvent.click(toggle);
      }
      expect(screen.getByLabelText("App ID")).not.toBeNull();
      expect(screen.getByRole("button", { name: "Show the manifest" })).not.toBeNull();
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
  [
    "AccessPanel",
    AccessPanel,
    { agentName: "warden", groupName: "growth", frozen: true },
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
      "/api/agents/warden/invites": { incoming: [], outgoing: [] },
      "/api/agents/warden/grants": { grants: [] },
      "/api/": {},
    });
  });

  /** Click every shut disclosure so the sweep below sees the forms they
   * hide. Without this the check is vacuous for a panel whose only
   * writable controls live behind a toolbar button: nothing is rendered,
   * so nothing can be found enabled. Run twice, in case opening one
   * reveals another. */
  async function openEveryDisclosure(container: HTMLElement): Promise<void> {
    for (let pass = 0; pass < 2; pass += 1) {
      const shut = container.querySelectorAll('[aria-expanded="false"]');
      for (const toggle of shut) {
        await fireEvent.click(toggle);
      }
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
  }

  for (const [label, component, props, reveal] of CASES) {
    it(`${label} disables all interactive controls when frozen`, async () => {
      const { container } = renderWithClient(component, props);
      await new Promise((resolve) => setTimeout(resolve, 0));
      // A panel whose disclosure is itself frozen shut (Slack) proves it is
      // open by hand; every other disclosure is clicked open below.
      if (reveal) {
        await reveal();
      }
      await openEveryDisclosure(container);

      const controls = Array.from(
        container.querySelectorAll("button, input, textarea, select"),
        // A disclosure toggle only shows and hides a form; it changes
        // nothing about the agent, and a frozen panel still has to let
        // somebody read what is in there. The freeze bites on the
        // controls inside, which this sweep is exactly what checks.
      ).filter((control) => !control.hasAttribute("aria-expanded"));
      expect(controls.length).toBeGreaterThan(0);
      for (const control of controls) {
        expect((control as HTMLButtonElement).disabled, `${label}: ${control.outerHTML.slice(0, 80)}`).toBe(true);
      }
    });
  }
});
