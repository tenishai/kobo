// @vitest-environment jsdom
import { fireEvent, screen, within } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";

import HarnessPanel from "./HarnessPanel.svelte";
import { renderWithClient, stubFetch } from "../test-utils/render";

const ROOM_RUNTIME = "us-east4-docker.pkg.dev/room/apps/agent-runtime:v10";
const OLD_RUNTIME = "us-east4-docker.pkg.dev/room/apps/agent-runtime:v9";

/** Every route the panels mounted inside Harness reach on their own. */
const BASE_ROUTES = {
  "/api/agents/warden/knowledge": { files: [] },
  "/api/agents/warden/intake": { files: [] },
  "/api/agents/warden/skills": { skills: [] },
  "/api/agents/warden/learnings": { learnings: [] },
  "/api/agents/warden/tooling": {
    files: [],
    enforcement: { attested: false, locked: false, locked_file: null },
  },
  "/api/": {},
};

const BASE_PROPS = {
  agentName: "warden",
  kind: "composed",
  frozen: false,
  model: "claude-x",
  modelLabel: "Claude X",
  runtimeImage: ROOM_RUNTIME,
  builtRuntimeImage: ROOM_RUNTIME,
  runtimePinned: false,
  roomRuntimeImage: ROOM_RUNTIME,
  canUpdateRuntime: false,
  pendingLearnings: 0,
  canDecideLearnings: false,
  capability: null,
  purposeTags: [],
};

function nav(): HTMLElement {
  return screen.getByRole("navigation", { name: "Harness sections" });
}

/** The section list's own items, by their visible label. */
function item(label: string): HTMLElement {
  return within(nav()).getByRole("button", { name: new RegExp(`^${label}`) });
}

function body(section: string): HTMLElement {
  return document.getElementById(`harness-${section}`) as HTMLElement;
}

describe("HarnessPanel sections", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    stubFetch(BASE_ROUTES);
  });

  it("opens on Model and runtime, the one section every agent has", async () => {
    renderWithClient(HarnessPanel, { ...BASE_PROPS });

    expect(item("Model and runtime").getAttribute("aria-current")).toBe("true");
    expect(body("runtime").hasAttribute("hidden")).toBe(false);
    expect(body("skills").hasAttribute("hidden")).toBe(true);
  });

  it("switches sections without unmounting the one it left", async () => {
    renderWithClient(HarnessPanel, { ...BASE_PROPS });

    await fireEvent.click(item("Skills"));

    expect(item("Skills").getAttribute("aria-current")).toBe("true");
    expect(item("Model and runtime").getAttribute("aria-current")).toBe("false");
    expect(body("skills").hasAttribute("hidden")).toBe(false);
    // Still in the DOM, merely hidden: the same rule the page's own tabs
    // follow, so a half-typed form in one section survives a look at
    // another.
    expect(body("runtime")).not.toBeNull();
    expect(body("runtime").hasAttribute("hidden")).toBe(true);
  });

  it("opens on the section it was given, so a bookmarked hash lands where it says", async () => {
    renderWithClient(HarnessPanel, { ...BASE_PROPS, section: "tooling" });

    expect(item("Guardrails").getAttribute("aria-current")).toBe("true");
    expect(body("tooling").hasAttribute("hidden")).toBe(false);
  });

  it("puts the pending-learnings count on the Learnings item", async () => {
    renderWithClient(HarnessPanel, { ...BASE_PROPS, pendingLearnings: 3 });

    expect(item("Learnings").textContent).toContain("3");
    // Nowhere else in the list: the count belongs to the one section that
    // can act on it.
    expect(item("Memory").textContent).not.toContain("3");
  });
});

describe("HarnessPanel model and runtime", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    stubFetch(BASE_ROUTES);
  });

  it("names the tag, not the digest, for a tag@digest room pin", async () => {
    const digest = "sha256:" + "a".repeat(64);
    renderWithClient(HarnessPanel, {
      ...BASE_PROPS,
      runtimeImage: OLD_RUNTIME,
      builtRuntimeImage: OLD_RUNTIME,
      roomRuntimeImage: `${ROOM_RUNTIME}@${digest}`,
    });

    const behind = within(body("runtime")).getByText(/Runs on/);
    expect(behind.textContent).toContain("v10");
    expect(behind.textContent).not.toContain("sha256");
  });

  it("reads behind from what the agent is built on, not from the resolved runtime (2026-11-01)", async () => {
    // The bug: an agent that follows the room has no runtime of its own, so
    // the resolved value IS the room's pin and the old comparison always
    // said current. The built fact says what it really runs on.
    renderWithClient(HarnessPanel, {
      ...BASE_PROPS,
      runtimeImage: OLD_RUNTIME,
      builtRuntimeImage: OLD_RUNTIME,
      runtimePinned: false,
      canUpdateRuntime: true,
    });

    const runtime = body("runtime");
    const line = within(runtime).getByText(/Runs on/);
    expect(line.textContent).toContain("v9");
    expect(line.textContent).toContain("v10");
    expect(within(runtime).queryByText("Runtime is up to date")).toBeNull();
    expect(within(runtime).getByRole("button", { name: "Update to current" })).not.toBeNull();
  });

  it("says Pinned, with the move button, for an agent held on a runtime of its own", async () => {
    renderWithClient(HarnessPanel, {
      ...BASE_PROPS,
      runtimeImage: OLD_RUNTIME,
      builtRuntimeImage: OLD_RUNTIME,
      runtimePinned: true,
      canUpdateRuntime: true,
    });

    const runtime = body("runtime");
    expect(within(runtime).getByText(/Pinned to v9/)).not.toBeNull();
    expect(within(runtime).queryByRole("button", { name: "Update to current" })).toBeNull();
    expect(within(runtime).getByRole("button", { name: "Move to the room's runtime" })).not.toBeNull();
  });

  it("says it is checking, and offers nothing, until the room has read what the agent runs on", async () => {
    renderWithClient(HarnessPanel, {
      ...BASE_PROPS,
      builtRuntimeImage: null,
      canUpdateRuntime: true,
    });

    const runtime = body("runtime");
    expect(within(runtime).getByText("Checking what it runs on.")).not.toBeNull();
    expect(within(runtime).queryByText(/up to date/)).toBeNull();
    expect(within(runtime).queryByRole("button", { name: "Update to current" })).toBeNull();
  });

  it("offers the update only to an operator", async () => {
    renderWithClient(HarnessPanel, {
      ...BASE_PROPS,
      runtimeImage: OLD_RUNTIME,
      builtRuntimeImage: OLD_RUNTIME,
      canUpdateRuntime: false,
    });

    const runtime = body("runtime");
    expect(within(runtime).getByText(/Runs on/)).not.toBeNull();
    expect(
      within(runtime).queryByRole("button", { name: "Update to current" }),
    ).toBeNull();
  });

  it("sends the room's own image, and nothing the caller typed", async () => {
    stubFetch({
      ...BASE_ROUTES,
      "/api/agents/warden/runtime": {
        runtime_image: ROOM_RUNTIME,
        previous_runtime_image: OLD_RUNTIME,
        sha: "abc123",
      },
    });
    renderWithClient(HarnessPanel, {
      ...BASE_PROPS,
      runtimeImage: OLD_RUNTIME,
      builtRuntimeImage: OLD_RUNTIME,
      canUpdateRuntime: true,
    });

    await fireEvent.click(screen.getByRole("button", { name: "Update to current" }));

    await vi.waitFor(() => {
      const call = vi
        .mocked(fetch)
        .mock.calls.find(([url]) => String(url).includes("/api/agents/warden/runtime"));
      expect(call).toBeDefined();
      const init = (call as [RequestInfo | URL, RequestInit])[1];
      expect(init.method).toBe("PATCH");
      expect(JSON.parse(String(init.body))).toEqual({ runtime_image: ROOM_RUNTIME });
    });
  });

});

describe("HarnessPanel colleague mode (issue #131)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    stubFetch({
      ...BASE_ROUTES,
      "/api/agents/warden/colleague-mode": {
        colleague_mode: "roster-only",
        previous_colleague_mode: "default",
        sha: "abc123",
      },
    });
  });

  it("sends the picked mode to the agent's colleague-mode route", async () => {
    renderWithClient(HarnessPanel, {
      ...BASE_PROPS,
      canUpdateRuntime: true,
      colleagueMode: "default",
    });

    await fireEvent.change(screen.getByRole("combobox", { name: "Colleague mode" }), {
      target: { value: "roster-only" },
    });
    await fireEvent.click(screen.getByRole("button", { name: "Change" }));

    await vi.waitFor(() => {
      const call = vi
        .mocked(fetch)
        .mock.calls.find(([url]) => String(url).includes("/api/agents/warden/colleague-mode"));
      expect(call).toBeDefined();
      const init = (call as [RequestInfo | URL, RequestInit])[1];
      expect(init.method).toBe("PUT");
      expect(JSON.parse(String(init.body))).toEqual({ mode: "roster-only" });
    });
  });

  it("says under the row that roster-only also drops general knowledge", async () => {
    renderWithClient(HarnessPanel, {
      ...BASE_PROPS,
      canUpdateRuntime: true,
      colleagueMode: "default",
    });

    await fireEvent.change(screen.getByRole("combobox", { name: "Colleague mode" }), {
      target: { value: "roster-only" },
    });

    expect(screen.getByText(/not general knowledge/)).toBeTruthy();
  });
});

describe("HarnessPanel for a connected agent", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    stubFetch(BASE_ROUTES);
  });

  it("shows Model and runtime plus Identity, and says why the rest is missing", async () => {
    renderWithClient(HarnessPanel, {
      ...BASE_PROPS,
      kind: "connected",
      runtimeImage: ROOM_RUNTIME,
    });

    const labels = within(nav())
      .getAllByRole("button")
      .map((b) => b.textContent?.trim());
    // Permissions is not here: what an agent may reach lives on its Access
    // tab (issue #365), for both kinds of agent. Agent to agent is not
    // either: this caller may not administer the agent.
    expect(labels).toEqual(["Model and runtime", "Identity and access", "Activity"]);
    expect(
      screen.getByText(
        "This agent brings its own harness. The room shows what it runs on and nothing more.",
      ),
    ).not.toBeNull();
    // Its own runtime is never behind the room's: no state line, no button.
    expect(within(body("runtime")).queryByText(/up to date|Runs on|Checking/)).toBeNull();
  });

  it("mounts none of the bundle-backed panels, so none of them asks for a bundle", async () => {
    renderWithClient(HarnessPanel, { ...BASE_PROPS, kind: "connected" });

    expect(body("skills")).toBeNull();
    expect(body("memory")).toBeNull();
    expect(body("learnings")).toBeNull();
    expect(
      vi.mocked(fetch).mock.calls.some(([url]) => String(url).includes("/skills")),
    ).toBe(false);
  });
});


describe("HarnessPanel agent to agent (issue #368)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    // Listed before the catch-all: the stub matches by substring, first wins.
    stubFetch({
      "/api/agents/warden/invites": { incoming: [], outgoing: [], team_pairs: [{ name: "relay", group: "growth" }] },
      "/api/agents/warden/requestable": { requestable: [], already: [] },
      ...BASE_ROUTES,
    });
  });

  it("is a section after Guardrails for a caller who may administer the agent, for both kinds", async () => {
    renderWithClient(HarnessPanel, { ...BASE_PROPS, canAdminister: true, section: "a2a" });

    const labels = within(nav()).getAllByRole("button").map((b) => b.textContent?.trim());
    expect(labels.indexOf("Agent to agent")).toBe(labels.indexOf("Guardrails") + 1);
    expect(body("a2a").hasAttribute("hidden")).toBe(false);
    // The block that was on the Access tab, with its one sentence.
    expect(within(body("a2a")).getByText(/Agents in the same group need nothing here/)).not.toBeNull();
    expect(await within(body("a2a")).findByText("relay")).not.toBeNull();
    expect(within(body("a2a")).getByRole("button", { name: "Request another team's agent" })).not.toBeNull();
  });

  it("is there for a connected agent too, since pairs are about reaching agents, not a bundle", async () => {
    renderWithClient(HarnessPanel, { ...BASE_PROPS, kind: "connected", canAdminister: true });

    const labels = within(nav()).getAllByRole("button").map((b) => b.textContent?.trim());
    expect(labels).toEqual(["Model and runtime", "Identity and access", "Agent to agent", "Activity"]);
  });

  it("is absent, not disabled, for a caller who may not administer the agent, and asks nothing of the invites route", async () => {
    renderWithClient(HarnessPanel, { ...BASE_PROPS });

    expect(within(nav()).queryByRole("button", { name: /Agent to agent/ })).toBeNull();
    expect(body("a2a")).toBeNull();
    expect(vi.mocked(fetch).mock.calls.some(([url]) => String(url).includes("/invites"))).toBe(false);
  });
});

describe("HarnessPanel — identity and access (2026-09-29)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    stubFetch(BASE_ROUTES);
  });

  const identity = {
    service_account: "agent-warden",
    gsa_email: "agent-warden@example-project.iam.gserviceaccount.com",
    project: "example-project",
  };

  it("names the service accounts and writes an access request, refusing basic roles", async () => {
    renderWithClient(HarnessPanel, { ...BASE_PROPS, identity, section: "identity" });
    expect(screen.getByText("agent-warden")).not.toBeNull();
    expect(screen.getByText(identity.gsa_email)).not.toBeNull();

    await fireEvent.input(screen.getByLabelText("Project"), { target: { value: "example-data-prod" } });
    await fireEvent.input(screen.getByLabelText("Roles"), { target: { value: "roles/editor" } });
    expect(screen.getByRole("alert").textContent).toContain("Basic roles are not granted");
    expect((screen.getByRole("button", { name: "Copy the request" }) as HTMLButtonElement).disabled).toBe(true);

    await fireEvent.input(screen.getByLabelText("Roles"), {
      target: { value: "roles/bigquery.dataViewer\nroles/storage.objectViewer" },
    });
    expect(screen.queryByRole("alert")).toBeNull();
    const request = document.querySelector(".harness__request")?.textContent ?? "";
    expect(request).toContain("Service account: " + identity.gsa_email);
    expect(request).toContain("Needs access to GCP project: example-data-prod");
    expect(request).toContain("  - roles/bigquery.dataViewer");
  });
});

describe("HarnessPanel — Change model with the model gateway on (kobo#390)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    stubFetch({
      "/api/catalog/models": {
        gateway: true,
        default: "claude-x",
        models: [
          { id: "claude-x", label: "Claude X", provider: "vertex_ai", provider_label: "Google Cloud", available: true, reason: null },
          { id: "gemini-y", label: "Gemini Y", provider: "vertex_ai", provider_label: "Google Cloud", available: false, reason: "The model gateway is not reachable" },
        ],
      },
      ...BASE_ROUTES,
    });
  });

  it("keeps a greyed model in the list and never lets Change model send it", async () => {
    renderWithClient(HarnessPanel, { ...BASE_PROPS, canUpdateRuntime: true });

    const greyed = (await screen.findByRole("radio", { name: /Gemini Y/ })) as HTMLInputElement;
    expect(greyed.disabled).toBe(true);
    expect(screen.getByText("The model gateway is not reachable")).not.toBeNull();
    const change = screen.getByRole("button", { name: "Change model" }) as HTMLButtonElement;
    expect(change.disabled).toBe(true);
  });
});
