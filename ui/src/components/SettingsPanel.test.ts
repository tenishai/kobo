// @vitest-environment jsdom
import { fireEvent, screen } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import SettingsPanel from "./SettingsPanel.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

// The page's back office: the build log every agent has, plus the two
// changes there is no way back from, both of which used to render under
// every tab on the agent's page as siblings of the tab strip.

describe("SettingsPanel rows", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    stubFetch({ "/api/": {} });
  });

  it("always has the build log, whichever gates are closed", () => {
    renderWithClient(SettingsPanel, { agentName: "warden", status: "ready" });

    expect(screen.getByText("Build log")).not.toBeNull();
    expect(screen.getByRole("button", { name: /build log/i })).not.toBeNull();
    // Nothing is fetched until somebody asks for it: BuildLogCard is lazy,
    // and mounting a tab is not asking.
    expect(vi.mocked(fetch)).not.toHaveBeenCalled();
  });

  it("offers neither one-way change to a caller who may make neither", () => {
    renderWithClient(SettingsPanel, { agentName: "warden", status: "ready" });

    expect(screen.queryByText("Convert to connected")).toBeNull();
    expect(screen.queryByText("Destroy this agent")).toBeNull();
    expect(screen.queryByRole("button", { name: "Convert" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Destroy" })).toBeNull();
  });

  it("shows the convert row, and asks before doing anything", async () => {
    renderWithClient(SettingsPanel, {
      agentName: "warden",
      status: "ready",
      canConvert: true,
    });

    expect(
      screen.getByText("For an agent whose repository no longer runs the room's runtime."),
    ).not.toBeNull();
    // Closed: no paragraph, no confirm, nothing to press by accident.
    expect(screen.queryByRole("button", { name: "Convert this agent" })).toBeNull();

    await fireEvent.click(screen.getByRole("button", { name: "Convert" }));

    expect(
      screen.getByText(
        "The room stops editing this agent: no chat, memory, learnings, skills, tooling or Slack from here, and other agents can no longer reach it. Its repository, deployment and cloud identity stay as they are. This cannot be undone from the room.",
      ),
    ).not.toBeNull();
    expect(
      (screen.getByRole("button", { name: "Convert this agent" }) as HTMLButtonElement).disabled,
    ).toBe(false);
    expect(screen.getByRole("button", { name: "Cancel" })).not.toBeNull();
    // Still no run mode question: a composed agent already serves web
    // traffic, and the route refuses anything else.
    expect(screen.queryByRole("radio")).toBeNull();
  });

  it("converts through the same route, with the same run mode, on confirm", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({ name: "warden", kind: "connected", run_mode: "http" }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
      ),
    );
    renderWithClient(SettingsPanel, {
      agentName: "warden",
      status: "ready",
      canConvert: true,
    });

    await fireEvent.click(screen.getByRole("button", { name: "Convert" }));
    await fireEvent.click(screen.getByRole("button", { name: "Convert this agent" }));

    const call = vi
      .mocked(fetch)
      .mock.calls.find(([url]) => String(url).includes("/convert-to-connected"));
    expect(call).toBeDefined();
    const init = (call as [RequestInfo | URL, RequestInit])[1];
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ run_mode: "http" });
  });

  it("says what the room said when a convert is refused, and stays open", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify({ error: "This agent is not yours to convert." }), {
            status: 403,
            headers: { "Content-Type": "application/json" },
          }),
      ),
    );
    renderWithClient(SettingsPanel, {
      agentName: "warden",
      status: "ready",
      canConvert: true,
    });

    await fireEvent.click(screen.getByRole("button", { name: "Convert" }));
    await fireEvent.click(screen.getByRole("button", { name: "Convert this agent" }));

    expect(await screen.findByText("This agent is not yours to convert.")).not.toBeNull();
    expect(screen.getByRole("button", { name: "Convert this agent" })).not.toBeNull();
  });

});

