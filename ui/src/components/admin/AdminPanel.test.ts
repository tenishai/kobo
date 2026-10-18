// @vitest-environment jsdom
import { fireEvent, screen } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import AdminPanel from "../../pages/AdminPanel.svelte";
import { admin, blankRole, grantLane, saveRole } from "../../lib/admin/store.svelte";
import { navigate } from "../../lib/router.svelte";
import { renderWithClient, stubFetch } from "../../test-utils/render";

// The demo's contract: the panel mounts from the hash, a pane opens from a
// link, and the store's mutations leave an audit row behind.

describe("AdminPanel", () => {
  const ADMIN_WHOAMI = {
    email: "kyle@example.com",
    operator: true,
    admin_panel: true,
    groups: [],
    groups_unavailable: false,
  };

  beforeEach(() => {
    vi.unstubAllGlobals();
    stubFetch({ "/api/whoami": ADMIN_WHOAMI });
  });

  it("is admins only, and only while the room has the panel on", async () => {
    vi.unstubAllGlobals();
    stubFetch({ "/api/whoami": { ...ADMIN_WHOAMI, admin_panel: false } });
    navigate({ page: "admin", pane: "addons" });
    renderWithClient(AdminPanel);
    expect(await screen.findByRole("heading", { name: "Admins only" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Add-ons" })).toBeNull();
  });

  it("opens the pane the hash names", async () => {
    navigate({ page: "admin", pane: "addons" });
    renderWithClient(AdminPanel);
    expect(await screen.findByRole("heading", { name: "Add-ons" })).toBeTruthy();
    expect(screen.getByText("Secrets (sero)")).toBeTruthy();
    // A vendor is an integration, not an add-on.
    expect(screen.queryByText("Datadog")).toBeNull();
  });

  it("lists every agent under Connections, including those with no lane", async () => {
    navigate({ page: "admin", pane: "connections" });
    renderWithClient(AdminPanel);
    expect(await screen.findByRole("heading", { name: "Connections" })).toBeTruthy();
    expect(screen.getAllByText("no lane").length).toBeGreaterThan(0);
    expect(screen.getByText("janus")).toBeTruthy();
  });

  it("opens the role editor from the Create link", async () => {
    navigate({ page: "admin", pane: "roles" });
    renderWithClient(AdminPanel);
    const create = await screen.findByRole("link", { name: "Create a role" });
    await fireEvent.click(create);
    expect(await screen.findByRole("heading", { name: "Create a role" })).toBeTruthy();
    expect(screen.getByRole("tab", { name: "Connectors" })).toBeTruthy();
  });
});

describe("admin store", () => {
  it("a grant by pull request waits on the PR and writes an audit row", () => {
    const before = admin.audit.length;
    grantLane("wren", "github", "read", "pr");
    const lane = admin.connections.find((c) => c.agent === "wren")?.lanes.find((l) => l.plugin === "github");
    expect(lane?.state).toBe("pr_open");
    expect(lane?.pr).toMatch(/^PR #\d+ open$/);
    expect(admin.audit.length).toBe(before + 1);
    expect(admin.audit[0].change).toContain("wren");
  });

  it("a custom role gets an id from its name and is not built in", () => {
    const draft = blankRole();
    draft.name = "Data engineering";
    draft.groups.push("data");
    const saved = saveRole(draft);
    expect(saved.id).toBe("data-engineering");
    expect(saved.builtin).toBe(false);
    expect(admin.roles.some((r) => r.id === "data-engineering")).toBe(true);
  });
});
