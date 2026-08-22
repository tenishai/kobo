// @vitest-environment jsdom
import { render, screen } from "@testing-library/svelte";
import { describe, expect, it } from "vitest";
import GettingStarted from "./GettingStarted.svelte";

// Static page, no queries/mutations — plain `render` is enough, no
// QueryClientProvider harness needed (contrast Dashboard.test.ts).

describe("GettingStarted", () => {
  it("renders the five journey headings, in order", () => {
    render(GettingStarted);
    const headings = screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent);
    expect(headings).toEqual([
      "Make an agent",
      "Talk to it, teach it",
      "Give it colleagues",
      "Put it in Slack",
      "Who can do what",
    ]);
  });

  it("walks the actual create flow: Create agent, the five name cards, the skills catalog, and the group picker's escape hatch", () => {
    render(GettingStarted);
    expect(screen.getByText(/Create agent/)).not.toBeNull();
    expect(screen.getByText(/five cards/)).not.toBeNull();
    expect(screen.getByText(/skills/i, { selector: "p" })).not.toBeNull();
    expect(screen.getByText(/request it right from the menu/)).not.toBeNull();
  });

  it("names the real chat/memory gestures: Teach this, and that a correction retires the old fact", () => {
    render(GettingStarted);
    expect(screen.getByText(/Teach this/)).not.toBeNull();
    expect(screen.getByText(/the old one retires on its own/)).not.toBeNull();
  });

  it("describes cross-team colleagues through Access, not an old vaguer claim", () => {
    render(GettingStarted);
    expect(screen.getByText(/request a colleague from another team's list/)).not.toBeNull();
    expect(screen.getByText(/terminal view under Chat/)).not.toBeNull();
  });

  it("walks the current Slack self-service setup (manifest, two tokens, channel id)", () => {
    render(GettingStarted);
    expect(screen.getByText(/ready-made manifest/)).not.toBeNull();
    expect(screen.getByText(/pasting two tokens/)).not.toBeNull();
    expect(screen.getByText(/saving the channel id/)).not.toBeNull();
  });

  it("states who can do what: sign-in identity, owner/admin edit rights, team-wide visibility, and grants", () => {
    render(GettingStarted);
    expect(screen.getByText(/email shows bottom left/)).not.toBeNull();
    expect(screen.getByText(/only you and the room's admins can change your agent/)).not.toBeNull();
    expect(screen.getByText(/Grants on the Access tab/)).not.toBeNull();
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
