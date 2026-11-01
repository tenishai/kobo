// @vitest-environment jsdom
import { fireEvent, screen } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import AccessPanel from "./AccessPanel.svelte";
import AgentToAgentPanel from "./AgentToAgentPanel.svelte";
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
      // An agent that is not connected gets the setup as the whole panel:
      // the stepper, the three paste fields and the manifest controls are
      // on screen with nothing to open first. Asserted rather than assumed,
      // so that if the first run ever moves back behind a disclosure the
      // sweep below does not quietly stop covering those fields.
      expect(screen.getByLabelText("App ID")).not.toBeNull();
      expect(screen.getByRole("button", { name: "Copy the manifest" })).not.toBeNull();
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
    async () => {
      // The vendors come from the access route as closed rows (issue #382);
      // the row's own button is a disclosure that still opens when frozen,
      // and the one held grant inside puts a Revoke on screen. The picker
      // is not offered on a frozen agent.
      await fireEvent.click(await screen.findByRole("button", { name: /Datadog/ }));
      expect(await screen.findByRole("button", { name: /Revoke now/ })).not.toBeNull();
    },
  ],
  ["AgentToAgentPanel", AgentToAgentPanel, { agentName: "warden", frozen: true }],
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
      "/api/agents/warden/invites": {
        incoming: [
          {
            id: 7, from_agent: "iris", to_agent: { name: "warden", group_name: "growth" }, status: "pending",
            requested_by: "iris-owner@example.com", decided_by: null, created_at: "2026-08-20T00:00:00Z", decided_at: null,
          },
        ],
        outgoing: [],
        team_pairs: [],
      },
      "/api/agents/warden/requestable": {
        requestable: [{ name: "payroll-bot", group: "payments-eng", team: null, capability: null, purpose_tags: [] }],
        already: [],
      },
      "/api/agents/warden/access": {
        agent: "warden",
        team: "growth",
        grants: [
          { id: 1, agent: "warden", integration: "datadog", permission: "datadog_monitors_read", tier: "read", gated: false, expires_at: null, granted_by: "admin@example.com", created_at: "2026-10-30T10:00:00Z", request_id: 3, state: "active" },
        ],
        pending: [],
        vendors: [
          { id: "datadog", kind: "service", label: "Datadog", connected: true, account_noun: null, organizations: [],
            accounts: [{ id: "datadog", name: "datadog", connected: true, ceiling: ["datadog_monitors_read"], organization: null, account_id: null, repositories: [], has_repo_set: false }],
            groups: [{ name: "Monitors", permissions: [{ name: "datadog_monitors_read", tier: "read", description: "Monitors", label: null }] }], tiers: [] },
        ],
        can_request: true,
        can_decide: true,
      },
      "/api/agents/warden/grants": { grants: [] },
      // SlackPanel's first run asks for the manifest as soon as it mounts
      // (step 1 is on screen from the start now), so it gets a real answer
      // here: against the bare `{}` catch-all the query resolves to
      // undefined and parks itself in an error state.
      "/api/agents/warden/slack/manifest": { manifest: { display_information: {} } },
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
