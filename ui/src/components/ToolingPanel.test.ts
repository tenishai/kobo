// @vitest-environment jsdom
import { fireEvent, screen } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ToolingPanel from "./ToolingPanel.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

const FILE = {
  name: "no_secrets.py",
  content_bytes: 512,
  delivered: true,
  description: "Holds back a reply that names a customer.",
  baseline: false,
  can_remove: true,
};

const BASELINE = {
  name: "baseline-leakguard.py",
  content_bytes: 1100,
  delivered: true,
  status: "enforced" as const,
  description: "Keys and tokens are cut out of its replies before they're sent.",
  baseline: true,
  can_remove: false,
};

describe("ToolingPanel", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    // The tooling query needs a real, correctly-shaped payload.
    stubFetch({
      "/api/agents/warden/tooling": { files: [], enforcement: null },
      "/api/": {},
    });
  });

  // Always on (2026-11-20): an owner reads what each check does and whether it
  // is on; adding one is an engineer's job from their own assistant, so the
  // page has no upload, and a check this caller may not remove has no Remove.
  it("says what each check does and whether it is on, with no upload", async () => {
    stubFetch({
      "/api/agents/warden/tooling": {
        files: [BASELINE, { ...FILE, delivered: false }],
        enforcement: { attested: true, locked: false, locked_file: null },
      },
      "/api/": {},
    });
    renderWithClient(ToolingPanel, { agentName: "warden", frozen: false });

    expect(await screen.findByText(BASELINE.description)).not.toBeNull();
    expect(screen.getByText("On")).not.toBeNull();
    expect(screen.getByText(FILE.description)).not.toBeNull();
    expect(screen.getByText("Starts at its next restart")).not.toBeNull();
    expect(screen.queryByRole("button", { name: `Remove ${BASELINE.name}` })).toBeNull();
    expect(screen.getByRole("button", { name: `Remove ${FILE.name}` })).not.toBeNull();
    expect(screen.queryByRole("button", { name: "+ Tool" })).toBeNull();
    expect(screen.queryByLabelText("Content")).toBeNull();
  });

  // A lock stops every check: the banner names the one that failed and every
  // row says why it isn't on, never a guess like "waits for a newer runtime".
  it("says a broken check stopped the rest", async () => {
    stubFetch({
      // Locked, the server marks no file enforced.
      "/api/agents/warden/tooling": {
        files: [{ ...BASELINE, status: undefined }, FILE],
        enforcement: { attested: true, locked: true, locked_file: FILE.name },
      },
      "/api/": {},
    });
    renderWithClient(ToolingPanel, { agentName: "warden", frozen: false });

    expect((await screen.findByRole("alert")).textContent).toContain(`${FILE.name} failed to load`);
    expect(screen.getByText("Failed to load")).not.toBeNull();
    expect(screen.getByText("Paused while a check is broken")).not.toBeNull();
    expect(screen.queryByText("On")).toBeNull();
  });

  it("asks before removing, in the room's own dialog, and never through the browser", async () => {
    const confirmSpy = vi.fn(() => true);
    vi.stubGlobal("confirm", confirmSpy);
    stubFetch({
      "/api/agents/warden/tooling": { files: [FILE], enforcement: null },
      "/api/": {},
    });
    renderWithClient(ToolingPanel, { agentName: "warden", frozen: false });

    await fireEvent.click(
      await screen.findByRole("button", { name: `Remove ${FILE.name}` }),
    );

    expect(confirmSpy).not.toHaveBeenCalled();
    expect(
      screen.getByRole("heading", { name: `Remove ${FILE.name}` }),
    ).not.toBeNull();
    expect(
      screen.getByText(`Remove ${FILE.name}? This can't be undone.`),
    ).not.toBeNull();
    // Nothing is deleted until the dialog is answered.
    expect(
      vi
        .mocked(fetch)
        .mock.calls.some(
          ([, init]) => (init as RequestInit | undefined)?.method === "DELETE",
        ),
    ).toBe(false);
  });

  it("cancelling the dialog leaves the file alone", async () => {
    stubFetch({
      "/api/agents/warden/tooling": { files: [FILE], enforcement: null },
      "/api/": {},
    });
    renderWithClient(ToolingPanel, { agentName: "warden", frozen: false });

    await fireEvent.click(
      await screen.findByRole("button", { name: `Remove ${FILE.name}` }),
    );
    await fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(
      screen.queryByRole("heading", { name: `Remove ${FILE.name}` }),
    ).toBeNull();
    expect(
      vi
        .mocked(fetch)
        .mock.calls.some(
          ([, init]) => (init as RequestInit | undefined)?.method === "DELETE",
        ),
    ).toBe(false);
  });

  it("confirming the dialog deletes that one file", async () => {
    stubFetch({
      "/api/agents/warden/tooling": { files: [FILE], enforcement: null },
      "/api/": {},
    });
    renderWithClient(ToolingPanel, { agentName: "warden", frozen: false });

    await fireEvent.click(
      await screen.findByRole("button", { name: `Remove ${FILE.name}` }),
    );
    await fireEvent.click(screen.getByRole("button", { name: "Remove" }));

    expect(
      vi
        .mocked(fetch)
        .mock.calls.some(
          ([url, init]) =>
            String(url).includes(`/api/agents/warden/tooling/${FILE.name}`) &&
            (init as RequestInit | undefined)?.method === "DELETE",
        ),
    ).toBe(true);
  });
});
