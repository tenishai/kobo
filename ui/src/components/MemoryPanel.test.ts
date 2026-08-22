// @vitest-environment jsdom
import { fireEvent, screen } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import MemoryPanel from "./MemoryPanel.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

// Fixture files for the memory-shelf condensation tests below. `list_intake`
// (kobo_api.intake) returns files sorted by name ascending -- for the
// date-prefixed taught-note convention (`chat-<8-digit date>-<slug>.md`,
// `kobo_api.teach.note_filename`) that reads oldest-first, so these are
// declared in that same ascending order to match exactly what the component
// actually receives over the wire.
const OTHER_OLD = { name: "warden-aaa-notes.md", content_bytes: 200, status: "ingested" as const };
const OTHER_NEW = { name: "warden-zzz-notes.md", content_bytes: 300, status: "pending" as const };
const CHAT_OLD = {
  name: "chat-20260810-early-note.md",
  content_bytes: 400,
  status: "ingested" as const,
};
const CHAT_MID = {
  name: "chat-20260815-middle-note.md",
  content_bytes: 500,
  status: "pending" as const,
};
const CHAT_NEW = {
  name: "chat-20260820-recent-note-about-something-long.md",
  content_bytes: 600,
  status: "ingested" as const,
};

describe("MemoryPanel", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    // Both of this panel's own queries (knowledge, intake) need a real,
    // correctly-shaped payload or the panel throws reading `.length` before
    // it ever renders anything — same reasoning as FrozenPanels.test.ts's
    // stub set for this component.
    stubFetch({
      "/api/agents/warden/knowledge": { files: [] },
      "/api/agents/warden/intake": { files: [] },
      "/api/": {},
    });
  });

  it("renders the how-learning-works explainer near the top of the panel", () => {
    // Retention-confusion fix: a short, honest explainer so "is Teach this
    // the only way? does the agent not learn on its own?" has an answer
    // right there in the panel, in its own plain voice.
    renderWithClient(MemoryPanel, { agentName: "warden", frozen: false });

    expect(
      screen.getByText(
        /Your agent remembers what you teach it.*It also keeps what colleague agents tell it.*It does not yet learn on its own from ordinary conversation\./s,
      ),
    ).not.toBeNull();
  });

  describe("memory-shelf condensation", () => {
    it("renders each intake file as a single-line row with the full filename in the title attribute", async () => {
      stubFetch({
        "/api/agents/warden/knowledge": { files: [] },
        "/api/agents/warden/intake": { files: [OTHER_OLD] },
        "/api/": {},
      });

      const { container } = renderWithClient(MemoryPanel, { agentName: "warden", frozen: false });

      const nameEl = await screen.findByTitle(OTHER_OLD.name);
      // The row is one `<li>` carrying the name, size, status chip, and
      // Remove affordance side by side -- not a stacked multi-line card.
      const row = nameEl.closest("li");
      expect(row).not.toBeNull();
      expect(row?.textContent).toContain("200 B");
      expect(row?.textContent).toContain("Ingested");
      expect(
        container.querySelector(`[aria-label="Remove ${OTHER_OLD.name}"]`),
      ).not.toBeNull();
    });

    it("collapses chat-<8 digits>-*.md files under a collapsed 'Taught notes (N)' header by default", async () => {
      stubFetch({
        "/api/agents/warden/knowledge": { files: [] },
        "/api/agents/warden/intake": { files: [CHAT_OLD, CHAT_MID, CHAT_NEW, OTHER_OLD, OTHER_NEW] },
        "/api/": {},
      });

      renderWithClient(MemoryPanel, { agentName: "warden", frozen: false });

      // Non-chat uploads always show, ungrouped.
      expect(await screen.findByTitle(OTHER_OLD.name)).not.toBeNull();
      expect(screen.getByTitle(OTHER_NEW.name)).not.toBeNull();

      // The three taught notes count into the collapsed group instead of
      // rendering as their own rows.
      const toggle = screen.getByRole("button", { name: /Taught notes \(3\)/ });
      expect(toggle.getAttribute("aria-expanded")).toBe("false");
      expect(screen.queryByTitle(CHAT_OLD.name)).toBeNull();
      expect(screen.queryByTitle(CHAT_MID.name)).toBeNull();
      expect(screen.queryByTitle(CHAT_NEW.name)).toBeNull();
    });

    it("expanding the taught-notes header reveals its rows", async () => {
      stubFetch({
        "/api/agents/warden/knowledge": { files: [] },
        "/api/agents/warden/intake": { files: [CHAT_OLD, CHAT_MID, CHAT_NEW, OTHER_OLD] },
        "/api/": {},
      });

      renderWithClient(MemoryPanel, { agentName: "warden", frozen: false });

      const toggle = await screen.findByRole("button", { name: /Taught notes \(3\)/ });
      await fireEvent.click(toggle);

      expect(toggle.getAttribute("aria-expanded")).toBe("true");
      expect(screen.getByTitle(CHAT_OLD.name)).not.toBeNull();
      expect(screen.getByTitle(CHAT_MID.name)).not.toBeNull();
      expect(screen.getByTitle(CHAT_NEW.name)).not.toBeNull();
    });

    it("orders rows newest-first in both the ungrouped list and the expanded taught-notes group", async () => {
      stubFetch({
        "/api/agents/warden/knowledge": { files: [] },
        "/api/agents/warden/intake": { files: [CHAT_OLD, CHAT_MID, CHAT_NEW, OTHER_OLD, OTHER_NEW] },
        "/api/": {},
      });

      const { container } = renderWithClient(MemoryPanel, { agentName: "warden", frozen: false });

      const toggle = await screen.findByRole("button", { name: /Taught notes \(3\)/ });
      await fireEvent.click(toggle);

      const names = Array.from(container.querySelectorAll("[title]"))
        .map((el) => el.getAttribute("title"))
        .filter((title): title is string => title !== null && title.endsWith(".md"));

      expect(names).toEqual([
        OTHER_NEW.name,
        OTHER_OLD.name,
        CHAT_NEW.name,
        CHAT_MID.name,
        CHAT_OLD.name,
      ]);
    });

    it("still fires Remove per row, for both an ungrouped file and a taught note inside the expanded group", async () => {
      stubFetch({
        "/api/agents/warden/knowledge": { files: [] },
        "/api/agents/warden/intake": { files: [CHAT_NEW, OTHER_OLD] },
        "/api/": {},
      });

      renderWithClient(MemoryPanel, { agentName: "warden", frozen: false });

      const otherRemove = await screen.findByRole("button", {
        name: `Remove ${OTHER_OLD.name}`,
      });
      await fireEvent.click(otherRemove);
      expect(
        screen.getByRole("heading", { name: `Remove ${OTHER_OLD.name}` }),
      ).not.toBeNull();
      await fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

      const toggle = screen.getByRole("button", { name: /Taught notes \(1\)/ });
      await fireEvent.click(toggle);

      const chatRemove = await screen.findByRole("button", {
        name: `Remove ${CHAT_NEW.name}`,
      });
      await fireEvent.click(chatRemove);
      expect(
        screen.getByRole("heading", { name: `Remove ${CHAT_NEW.name}` }),
      ).not.toBeNull();
    });
  });
});
