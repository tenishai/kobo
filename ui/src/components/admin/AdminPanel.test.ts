// @vitest-environment jsdom
import { fireEvent, screen } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import AdminPanel from "../../pages/AdminPanel.svelte";
import { navigate } from "../../lib/router.svelte";
import { renderWithClient, stubFetch } from "../../test-utils/render";

// The demo's contract: the panel mounts from the hash, a pane opens from a
// link, and the store's mutations leave an audit row behind.

describe("AdminPanel", () => {
  const ADDONS = {
    addons: [
      {
        id: "secrets",
        name: "Secrets",
        summary: "Holds each agent's secrets in the vault.",
        state: "on",
        detail: "Vault lane at http://sero.internal.",
        keys: ["sero_url"],
        owner: "this team",
      },
    ],
  };
  const ROLES = {
    roles: [
      {
        id: "admin", name: "Admin", description: "Runs the room.", builtin: true, groups: ["platform-admins"],
        capabilities: ["create_agents"], admin: ["view_audit"], connectors: [], models: ["sonnet"], updated_at: "2026-10-18T20:00:00",
      },
    ],
    catalogue: {
      capabilities: [{ id: "create_agents", label: "Create agents", desc: "Build an agent." }],
      admin: [{ id: "view_audit", label: "View audit", desc: "Read who changed what." }],
      models: [{ id: "sonnet", label: "Sonnet" }],
      groups: ["platform-admins", "data"],
      connectors: [{ id: "datadog", name: "Datadog", base_access: "read" }],
    },
  };
  const PEOPLE = {
    admin_groups: ["platform-admins"],
    operators: ["kyle@example.com"],
    reader_groups: ["sre-leads"],
    slack_operators: [],
  };
  const AUDIT = {
    events: [
      {
        id: 1,
        at: "2026-10-18T20:00:00",
        actor: "kyle@example.com",
        action: "setting.put",
        object_type: "setting",
        object_id: "panel.preferences",
        landed: "applied",
        landed_detail: "stored",
      },
    ],
  };
  const REQUESTS = [
    {
      id: 7,
      group_name: "growth-eng",
      requested_by: "sam@example.com",
      note: null,
      status: "pending",
      decided_by: null,
      created_at: "2026-10-18T19:00:00",
      decided_at: null,
    },
  ];
  const ADMIN_WHOAMI = {
    email: "kyle@example.com",
    operator: true,
    admin_panel: true,
    groups: [],
    groups_unavailable: false,
  };

  beforeEach(() => {
    vi.unstubAllGlobals();
    stubFetch({
      "/api/whoami": ADMIN_WHOAMI,
      "/api/admin/addons": ADDONS,
      "/api/admin/integrations": { integrations: [] },
      "/api/admin/roles": ROLES,
      "/api/admin/people": PEOPLE,
      "/api/admin/audit": AUDIT,
      "/api/group-requests": { requests: REQUESTS },
    });
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
    // Cards come from the server, not the store.
    expect(await screen.findByText("Vault lane at http://sero.internal.")).toBeTruthy();
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

  it("Integrations starts empty and offers the catalog", async () => {
    navigate({ page: "admin", pane: "integrations" });
    renderWithClient(AdminPanel);
    expect(await screen.findByRole("heading", { name: "Integrations" })).toBeTruthy();
    expect(await screen.findByText(/Nothing connected yet/)).toBeTruthy();
    await fireEvent.click(screen.getByRole("button", { name: "Add integration" }));
    expect(await screen.findByRole("heading", { name: "Add an integration" })).toBeTruthy();
  });

  it("People reads the real lists and the real queue", async () => {
    navigate({ page: "admin", pane: "people" });
    renderWithClient(AdminPanel);
    expect(await screen.findByText("platform-admins")).toBeTruthy();
    expect(await screen.findByText("growth-eng")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Added" })).toBeTruthy();
  });

  it("Audit reads the store's events with their landed state", async () => {
    navigate({ page: "admin", pane: "audit" });
    renderWithClient(AdminPanel);
    expect(await screen.findByText("panel.preferences")).toBeTruthy();
    expect(screen.getByText("Applied")).toBeTruthy();
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

