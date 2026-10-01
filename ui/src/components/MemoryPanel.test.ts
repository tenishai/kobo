// @vitest-environment jsdom
import { fireEvent, screen } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import MemoryPanel from "./MemoryPanel.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

/** Open the "Teach from a file" disclosure. The upload form and the list
 * of uploaded files live behind it now, shut on arrival. */
async function openTeach(): Promise<void> {
  await fireEvent.click(
    await screen.findByRole("button", { name: "Teach from a file" }),
  );
}

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

  describe("the panel shape", () => {
    it("keeps the upload form shut until it is asked for", async () => {
      renderWithClient(MemoryPanel, { agentName: "warden", frozen: false });

      const toggle = await screen.findByRole("button", {
        name: "Teach from a file",
      });
      expect(toggle.getAttribute("aria-expanded")).toBe("false");
      expect(screen.queryByLabelText("Choose file")).toBeNull();
      expect(screen.queryByLabelText("Content")).toBeNull();
      expect(screen.queryByRole("button", { name: "Upload" })).toBeNull();
    });

    it("opens the upload form on click", async () => {
      renderWithClient(MemoryPanel, { agentName: "warden", frozen: false });

      await openTeach();
      expect(
        screen
          .getByRole("button", { name: "Teach from a file" })
          .getAttribute("aria-expanded"),
      ).toBe("true");
      expect(screen.getByLabelText("Choose file")).not.toBeNull();
      expect(screen.getByRole("button", { name: "Upload" })).not.toBeNull();
    });

  });

  describe("memory-shelf condensation", () => {
    it("collapses chat-<8 digits>-*.md files under a collapsed 'Taught notes (N)' header by default", async () => {
      stubFetch({
        "/api/agents/warden/knowledge": { files: [] },
        "/api/agents/warden/intake": { files: [CHAT_OLD, CHAT_MID, CHAT_NEW, OTHER_OLD, OTHER_NEW] },
        "/api/": {},
      });

      renderWithClient(MemoryPanel, { agentName: "warden", frozen: false });
      await openTeach();

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
      await openTeach();

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
      await openTeach();

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
      await openTeach();

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

  // Fast-memory-lane fix: knowledge saves/deletes and intake uploads all
  // ride the fast lane now (backend commits fafee93/0fe90f4), returning
  // `{"mode": "instant"}` with NO `sha` on success. Before this fix,
  // `handleSave`/`handleDelete`/`handleUpload` called `beginDelivery(result.
  // sha)` unconditionally, which would have started polling
  // `/knowledge/delivery/undefined` for an instant response — these tests
  // prove that never happens, and that the panel still tells the user
  // something landed.
  describe("instant memory lane (fast-memory-lane fix)", () => {
    function jsonResponse(body: unknown, status = 200): Response {
      return new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      });
    }

    it("an instant-mode intake upload shows the in-place notice without ever polling delivery", async () => {
      const urls: string[] = [];
      vi.stubGlobal(
        "fetch",
        vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = String(input);
          urls.push(url);
          if (url.includes("/api/agents/warden/intake") && init?.method === "POST") {
            return jsonResponse({ filename: "instant-note.md", mode: "instant" });
          }
          if (url.includes("/api/agents/warden/knowledge")) {
            return jsonResponse({ files: [] });
          }
          if (url.includes("/api/agents/warden/intake")) {
            return jsonResponse({ files: [] });
          }
          return jsonResponse({});
        }),
      );

      renderWithClient(MemoryPanel, { agentName: "warden", frozen: false });
      await openTeach();

      await fireEvent.input(await screen.findByLabelText("File name"), {
        target: { value: "instant-note.md" },
      });
      await fireEvent.input(screen.getByLabelText("Content"), {
        target: { value: "hello there" },
      });
      await fireEvent.click(screen.getByRole("button", { name: "Upload" }));

      expect(await screen.findByText("Changes are in place")).not.toBeNull();
      expect(urls.some((u) => u.includes("/delivery/"))).toBe(false);
    });

    it("an instant-mode knowledge save shows the in-place notice without ever polling delivery", async () => {
      const urls: string[] = [];
      vi.stubGlobal(
        "fetch",
        vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = String(input);
          urls.push(url);
          if (url.includes("/api/agents/warden/knowledge/") && init?.method === "PUT") {
            return jsonResponse({ mode: "instant" });
          }
          if (url.includes("/api/agents/warden/knowledge")) {
            return jsonResponse({ files: [] });
          }
          if (url.includes("/api/agents/warden/intake")) {
            return jsonResponse({ files: [] });
          }
          return jsonResponse({});
        }),
      );

      renderWithClient(MemoryPanel, { agentName: "warden", frozen: false });

      await fireEvent.click(await screen.findByRole("button", { name: "+ Memory" }));
      await fireEvent.input(screen.getByLabelText("Name"), {
        target: { value: "instant-note" },
      });
      await fireEvent.input(screen.getByLabelText("What it knows"), {
        target: { value: "hello there" },
      });
      await fireEvent.click(screen.getByRole("button", { name: "Review changes" }));
      await fireEvent.click(screen.getByRole("button", { name: "Save" }));

      expect(await screen.findByText("Changes are in place")).not.toBeNull();
      expect(urls.some((u) => u.includes("/delivery/"))).toBe(false);
    });

    it("an instant-mode knowledge delete shows the in-place notice without ever polling delivery", async () => {
      const urls: string[] = [];
      vi.stubGlobal(
        "fetch",
        vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = String(input);
          urls.push(url);
          if (url.includes("/api/agents/warden/knowledge/") && init?.method === "DELETE") {
            return jsonResponse({ mode: "instant" });
          }
          if (url.includes("/api/agents/warden/knowledge")) {
            return jsonResponse({ files: [{ name: "foo.md", content: "bar" }] });
          }
          if (url.includes("/api/agents/warden/intake")) {
            return jsonResponse({ files: [] });
          }
          return jsonResponse({});
        }),
      );

      renderWithClient(MemoryPanel, { agentName: "warden", frozen: false });

      await fireEvent.click(await screen.findByRole("button", { name: "Remove foo" }));
      await fireEvent.click(screen.getByRole("button", { name: "Remove" }));

      expect(await screen.findByText("Changes are in place")).not.toBeNull();
      expect(urls.some((u) => u.includes("/delivery/"))).toBe(false);
    });
  });

  // Intake filenames are .md only now (the fast lane's own unconditional
  // gate, `kobo_api.main._INTAKE_FAST_LANE_FILENAME_RE`, refuses a `.txt`
  // upload before it ever reaches the repo) — the picker's copy needs to
  // stop advertising a `.txt` option it can no longer actually deliver.
  describe("intake filenames are .md only (fast lane rule)", () => {
    it("flags a .txt filename with the .md-only rule and never mentions .txt", async () => {
      renderWithClient(MemoryPanel, { agentName: "warden", frozen: false });
      await openTeach();
      const filenameInput = screen.getByLabelText("File name");
      await fireEvent.input(filenameInput, { target: { value: "notes.txt" } });

      expect(
        await screen.findByText("Use lowercase letters, numbers, and hyphens, ending in .md."),
      ).not.toBeNull();
      expect(screen.queryByText(/\.txt/)).toBeNull();
    });
  });
});
