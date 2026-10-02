// @vitest-environment jsdom
import { fireEvent, screen } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ToolingPanel from "./ToolingPanel.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

const FILE = {
  name: "no_secrets.py",
  content_bytes: 512,
  delivered: true,
  status: "stored" as const,
};

/** Open the "+ Tool" disclosure. The upload form lives behind it now. */
async function openUpload(): Promise<void> {
  await fireEvent.click(await screen.findByRole("button", { name: "+ Tool" }));
}

describe("ToolingPanel", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    // The tooling query needs a real, correctly-shaped payload.
    stubFetch({
      "/api/agents/warden/tooling": { files: [], enforcement: null },
      "/api/": {},
    });
  });

  it("keeps the upload form shut until it is asked for, and opens it on click", async () => {
    renderWithClient(ToolingPanel, { agentName: "warden", frozen: false });

    const toggle = await screen.findByRole("button", { name: "+ Tool" });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByLabelText("Choose file")).toBeNull();
    expect(screen.queryByRole("button", { name: "Upload" })).toBeNull();

    await fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByLabelText("Choose file")).not.toBeNull();
    expect(screen.getByLabelText("File name")).not.toBeNull();
    expect(screen.getByLabelText("Content")).not.toBeNull();
    expect(screen.getByRole("button", { name: "Upload" })).not.toBeNull();
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

  it("uploads the typed file name and content", async () => {
    renderWithClient(ToolingPanel, { agentName: "warden", frozen: false });

    await openUpload();
    await fireEvent.input(screen.getByLabelText("File name"), {
      target: { value: "no_secrets.py" },
    });
    await fireEvent.input(screen.getByLabelText("Content"), {
      target: { value: "def hook():\n    return None\n" },
    });
    await fireEvent.click(screen.getByRole("button", { name: "Upload" }));

    const call = vi
      .mocked(fetch)
      .mock.calls.find(
        ([url, init]) =>
          String(url).includes("/api/agents/warden/tooling") &&
          (init as RequestInit | undefined)?.method === "POST",
      );
    expect(call).toBeDefined();
    const body = JSON.parse((call as [RequestInfo | URL, RequestInit])[1].body as string);
    expect(body).toEqual({
      filename: "no_secrets.py",
      content: "def hook():\n    return None\n",
    });
  });
});
