// @vitest-environment jsdom
import { render, screen } from "@testing-library/svelte";
import { describe, expect, it } from "vitest";
import GettingStarted from "./GettingStarted.svelte";

// Static page, no queries/mutations — plain `render` is enough, no
// QueryClientProvider harness needed (contrast Dashboard.test.ts).
//
// Rewritten for the UI-simplification wave: the five steps a new user
// actually lives now (create, say hello, put it in Slack, bring in
// teammates, grow it), replacing the old make-an-agent/talk-to-it/
// give-it-colleagues/put-it-in-slack/who-can-do-what ordering that
// described a room predating the checklist-on-the-agent-page, the
// Chat/Slack/Access/Harness/Settings row, and the skills-free create flow.

describe("GettingStarted", () => {
  it("renders the five journey headings, in order", () => {
    render(GettingStarted);
    const headings = screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent);
    expect(headings).toEqual([
      "1. Create your agent",
      "2. Say hello",
      "3. Put it in Slack",
      "4. Bring in teammates",
      "5. Grow it (Harness)",
    ]);
  });

  it("links the dashboard, the one live link in the page, and names the actual create flow", () => {
    render(GettingStarted);
    const dashboardLink = screen.getByRole("link", { name: "dashboard" });
    expect(dashboardLink.getAttribute("href")).toBe("#/");
    expect(screen.getByText(/Create agent/)).not.toBeNull();
    expect(screen.getByText(/choose the group/)).not.toBeNull();
  });

  it("says the build checklist lives on the agent's page and clears once it's ready", () => {
    render(GettingStarted);
    expect(screen.getByText(/checklist appears at the top of the agent's page/)).not.toBeNull();
    expect(screen.getByText(/every step turns green/)).not.toBeNull();
  });

  it("names the real chat gesture: the Chat tab, and Teach this", () => {
    render(GettingStarted);
    expect(screen.getByText(/Chat tab/)).not.toBeNull();
    expect(screen.getByText(/Teach this/)).not.toBeNull();
  });

  it("states the actual Slack answering rule: @-mention in channels, always in a DM", () => {
    render(GettingStarted);
    expect(screen.getByText(/@-mention/)).not.toBeNull();
    expect(screen.getByText(/direct message always reaches it/)).not.toBeNull();
  });

  it("describes same-group visibility and the Access tab's two mechanisms", () => {
    render(GettingStarted);
    expect(screen.getByText(/same group can see and chat with your agent/)).not.toBeNull();
    expect(screen.getByText(/Access tab lets you grant an individual person/)).not.toBeNull();
    expect(screen.getByText(/colleague requests connect your agent to agents in other groups/)).not.toBeNull();
  });

  it("covers every grow-it section, and names the tab they are in", () => {
    render(GettingStarted);
    expect(screen.getByText(/the Harness tab has the rest/)).not.toBeNull();
    expect(screen.getByText(/Model and runtime says what it thinks with/)).not.toBeNull();
    expect(screen.getByText(/Persona is the soul it was built from/)).not.toBeNull();
    expect(screen.getByText(/nothing is kept until you approve it/)).not.toBeNull();
    expect(screen.getByText(/Memory holds the notes you teach it/)).not.toBeNull();
    expect(screen.getByText(/Skills lets you add abilities from the catalog/)).not.toBeNull();
    expect(screen.getByText(/recommended for this agent/)).not.toBeNull();
    expect(screen.getByText(/Tooling shows the lanes every agent starts with/)).not.toBeNull();
  });

  it("no longer sends anyone to a More menu that is gone", () => {
    const { container } = render(GettingStarted);
    expect(container.textContent).not.toMatch(/More menu/);
    expect(container.textContent).not.toMatch(/Advanced/);
  });

  it("never falls back to retired infra vocabulary (repo, image, pod, CI, pipeline, deploy)", () => {
    const { container } = render(GettingStarted);
    const text = (container.textContent ?? "").toLowerCase();
    for (const word of ["repo", "image", "pod", "pipeline", "deploy"]) {
      expect(text).not.toMatch(new RegExp(`\\b${word}\\b`));
    }
    expect(text).not.toMatch(/\bci\b/);
  });
});
