// @vitest-environment jsdom
// One entry point (new-agent entry wave): the rail used to stack three
// buttons that each opened a different dialog, and operators read them as
// three competing primaries. This chooser is the one door: a single
// question and three rows, each of which hands the choice back to the
// caller and gets out of the way.
import { fireEvent, screen } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import NewAgentModal from "./NewAgentModal.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

const ROWS: { title: string; desc: string; choice: string }[] = [
  {
    title: "Build an agent",
    desc: "Pick a persona and the room builds and deploys it on its runtime.",
    choice: "build",
  },
  {
    title: "Connect a repository you already have",
    desc: "Your code, your runtime. The room deploys and watches it.",
    choice: "connect",
  },
];

describe("NewAgentModal", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    stubFetch({ "/api/": {} });
  });

  it("renders nothing while closed", () => {
    renderWithClient(NewAgentModal, {
      open: false,
      onclose: () => {},
      onchoose: () => {},
    });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("hands back the choice a row stands for", async () => {
    for (const row of ROWS) {
      const onchoose = vi.fn();
      const { unmount } = renderWithClient(NewAgentModal, {
        open: true,
        onclose: () => {},
        onchoose,
      });
      await fireEvent.click(screen.getByRole("button", { name: new RegExp(row.title) }));
      expect(onchoose).toHaveBeenCalledWith(row.choice);
      unmount();
    }
  });

  it("closes on Cancel without choosing anything", async () => {
    const onclose = vi.fn();
    const onchoose = vi.fn();
    renderWithClient(NewAgentModal, { open: true, onclose, onchoose });
    await fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onclose).toHaveBeenCalled();
    expect(onchoose).not.toHaveBeenCalled();
  });

  it("keeps every row reachable from the keyboard", () => {
    const { container } = renderWithClient(NewAgentModal, {
      open: true,
      onclose: () => {},
      onchoose: () => {},
    });
    // Real buttons, not clickable divs: no row carries a tabindex that
    // would take it out of the tab order, and none is disabled.
    for (const row of container.querySelectorAll<HTMLButtonElement>(".list__row")) {
      expect(row.getAttribute("tabindex")).toBeNull();
      expect(row.disabled).toBe(false);
      expect(row.getAttribute("type")).toBe("button");
    }
  });
});
