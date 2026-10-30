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
  const CONNECTIONS = {
    agents: [
      { agent: "janus", group: "platform", lanes: [] },
      { agent: "sage", group: "data", lanes: [{ id: 1, agent: "sage", integration: "datadog", access: "read", state: "applied", state_detail: "live at the gateway", spec: {}, requested_by: "k", decided_by: "k", created_at: "2026-10-24T10:00:00", updated_at: "2026-10-24T10:00:00" }] },
    ],
    integrations: [{ id: "datadog", vendor: "datadog", name: "Datadog", base_access: "read", state: "applied", state_detail: null, credential_ref: null, spec: {}, created_by: "k", created_at: "", updated_at: "" }],
    live: true,
    gateway_repo: true,
  };
  const ROOM_SERVICES = {
    can_store: true,
    note: null,
    services: {
      slack: { configured: false, source: "none", updated_at: null, updated_by: null },
      github: { configured: true, source: "deployment", updated_at: null, updated_by: null },
    },
  };
  const PEOPLE = {
    admin_groups: ["platform-admins"],
    operators: ["kyle@example.com"],
    reader_groups: ["sre-leads"],
    slack_operators: [],
  };
  const AUDIT = {
    rows: [
      {
        id: 1,
        at: "2026-10-18T20:00:00Z",
        actor: "kyle@example.com",
        actor_kind: "person",
        action: "agent.create",
        target_kind: "agent",
        target_id: "ward",
        agent_name: "ward",
        summary: "kyle created agent ward",
        outcome: "ok",
        source: "panel",
        request_ip: "203.0.113.7",
        user_agent: null,
        detail: null,
        landed: "applied",
        landed_detail: "stored",
      },
    ],
    next_cursor: null,
    total_in_window: 1,
    actors: ["kyle@example.com"],
    agents: ["ward"],
    families: ["agent"],
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

  const AWS_SETUP = {
    version: "v0.1.0 f8951a8", mode: "org", principal: "access", role_name: "KoboAccess-Hub", audience: "sts.amazonaws.com",
    identity: { issuer: "https://accounts.google.com", subject: "1234567890", email: "gw@p.iam.gserviceaccount.com", audience: "sts.amazonaws.com" },
    identity_note: null,
    packs: [
      { id: "inventory", purpose: "What exists.", required: true, grants: ["ViewOnlyAccess"] },
      { id: "logs", purpose: "Read logs and metrics.", required: false, grants: ["12 Logs actions"] },
    ],
    chosen: ["inventory", "logs"],
    terraform: 'module "kobo_access_hub" {\n  source = "git::https://github.com/tenishai/cloud-access.git//aws/hub?ref=v0.1.0"\n}',
    cloudformation: { template: "", parameters: {} },
    trust_policy: '{"Statement": []}',
    permissions_policy: '{"Statement": [{"Sid": "AssumeSpokes"}]}',
    checked: true,
  };

  beforeEach(() => {
    vi.unstubAllGlobals();
    stubFetch({
      "/api/whoami": ADMIN_WHOAMI,
      "/api/admin/addons": ADDONS,
      // Listed before "/api/admin/room": the stub matches by substring, first wins.
      "/api/admin/room-services": ROOM_SERVICES,
      "/api/admin/room": { org_repo: "example/kobo-config", runtime_image: "registry/daedalus-runtime@sha256:789b43aa", cluster: "agents-dev", delivery_backend: "deployed" },
      // Listed before the integrations list: the stub matches by substring, first wins.
      "/api/admin/integrations/aws/setup": AWS_SETUP,
      "/api/admin/integrations": { integrations: [] },
      "/api/admin/roles": ROLES,
      "/api/admin/connections": CONNECTIONS,
      "/api/admin/people": PEOPLE,
      "/api/admin/audit": AUDIT,
      "/api/group-requests": { requests: REQUESTS },
    });
  });

  it("is admins only, and only while the room has the panel on", async () => {
    vi.unstubAllGlobals();
    stubFetch({ "/api/whoami": { ...ADMIN_WHOAMI, admin_panel: false } });
    navigate({ page: "admin", pane: "services" });
    renderWithClient(AdminPanel);
    expect(await screen.findByRole("heading", { name: "Admins only" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Services" })).toBeNull();
  });

  it("opens the page the hash names, and the old add-ons name still opens Services", async () => {
    navigate({ page: "admin", pane: "addons" });
    renderWithClient(AdminPanel);
    expect(await screen.findByRole("heading", { name: "Services" })).toBeTruthy();
    // Rows come from the server, as a status word plus the detail.
    expect(await screen.findByText("Vault lane at http://sero.internal.")).toBeTruthy();
    expect(screen.getByText("On")).toBeTruthy();
    // A vendor is an integration, not a service the room runs on.
    expect(screen.queryByText("Datadog")).toBeNull();
    // The rail shows the room's own facts, read from the server.
    expect(await screen.findByText("example/kobo-config")).toBeTruthy();
  });

  it("Overview is one row per page, each a link with a number", async () => {
    navigate({ page: "admin", pane: "overview" });
    renderWithClient(AdminPanel);
    expect(await screen.findByRole("heading", { name: "Overview" })).toBeTruthy();
    expect(await screen.findByText("1 of 1 on")).toBeTruthy();
    expect(await screen.findByText("2 agents, 1 without a lane")).toBeTruthy();
    expect(await screen.findByText("1 group request waiting")).toBeTruthy();
    const rows = screen.getAllByRole("link").filter((a) => a.getAttribute("href")?.includes("admin/"));
    expect(rows.length).toBeGreaterThanOrEqual(6);
  });

  it("Reload org config asks first", async () => {
    navigate({ page: "admin", pane: "services" });
    renderWithClient(AdminPanel);
    await fireEvent.click(await screen.findByRole("button", { name: "Reload org config" }));
    expect(await screen.findByRole("heading", { name: "Reload the org config?" })).toBeTruthy();
  });

  it("lists every agent under Connections, including those with no lane", async () => {
    navigate({ page: "admin", pane: "connections" });
    renderWithClient(AdminPanel);
    expect(await screen.findByRole("heading", { name: "Connections" })).toBeTruthy();
    expect((await screen.findAllByText("no lane")).length).toBe(1);
    expect(screen.getByText("janus")).toBeTruthy();
    // sage's one lane, live at the gateway (the chip carries the state detail as its title).
    expect(screen.getByTitle("live at the gateway").textContent).toMatch(/datadog/);
  });

  it("Integrations starts empty and offers the catalog", async () => {
    navigate({ page: "admin", pane: "integrations" });
    renderWithClient(AdminPanel);
    expect(await screen.findByRole("heading", { name: "Integrations" })).toBeTruthy();
    // Slack and GitHub are rows of the one table, never "added" (#304): the
    // unset one offers Set up, the one the deployment config carries reads Connected with Replace.
    expect(screen.queryByRole("heading", { name: "Room services" })).toBeNull();
    expect(await screen.findByText("Not set up")).toBeTruthy();
    expect(screen.getAllByText("service")).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Set up" })).toBeTruthy();
    // A value the deployment config carries reads Connected like any other row, with the
    // provenance as a note and Replace to move it into the room (#337).
    expect((await screen.findAllByText("Connected")).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("set in the deployment config").length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByRole("button", { name: "Remove" })).toBeNull();
    expect(await screen.findByText(/Nothing connected yet/)).toBeTruthy();
    await fireEvent.click(screen.getByRole("button", { name: "Add integration" }));
    expect(await screen.findByRole("heading", { name: "Add an integration" })).toBeTruthy();
    // The catalog is this room's vendors, one line each.
    for (const vendor of ["Atlassian", "Datadog", "Google Cloud", "AWS", "Azure", "Wiz", "Fastly", "Okta"]) {
      expect(screen.getByText(vendor)).toBeTruthy();
    }
    expect(screen.queryByText("PagerDuty")).toBeNull();
    // AWS is its own sheet: what agents may read, the role three ways, paste the ARN. No key anywhere.
    await fireEvent.click(screen.getAllByRole("button", { name: "Add" })[3]);
    expect(await screen.findByRole("heading", { name: "Connect an AWS organization" })).toBeTruthy();
    expect(await screen.findByText("1234567890")).toBeTruthy();
    // The method comes first and the sheet shows only that method's content (#276).
    expect(screen.getByRole("radio", { name: /By hand/ })).toBeTruthy();
    expect(screen.getByRole("radio", { name: /CloudFormation/ })).toBeTruthy();
    expect(screen.getByText(/cloud-access.git\/\/aws\/hub\?ref=v0.1.0/)).toBeTruthy();
    expect(screen.getByLabelText("Name for this organization")).toBeTruthy();
    expect(screen.getByLabelText("Hub role ARN")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Connect" })).toBeTruthy();
    expect(screen.queryByLabelText(/token/i)).toBeNull();
    expect(document.querySelector("#integration-form input[type=password]")).toBeNull();
  });

  it("People reads the real lists and the real queue", async () => {
    navigate({ page: "admin", pane: "people" });
    renderWithClient(AdminPanel);
    expect(await screen.findByText("platform-admins")).toBeTruthy();
    expect(await screen.findByText("growth-eng")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Added" })).toBeTruthy();
  });

  it("Audit reads the log as sentences with the action and the landed state", async () => {
    navigate({ page: "admin", pane: "audit" });
    renderWithClient(AdminPanel);
    expect(await screen.findByText("kyle created agent ward")).toBeTruthy();
    expect(screen.getByText("agent.create")).toBeTruthy();
    expect(screen.getByText("· Applied")).toBeTruthy();
    expect(screen.getByRole("link", { name: "CSV" }).getAttribute("href")).toBe("/api/admin/audit/export?format=csv");
  });

  it("opens the role editor from the Create link", async () => {
    navigate({ page: "admin", pane: "roles" });
    renderWithClient(AdminPanel);
    const create = await screen.findByRole("link", { name: "Add role" });
    await fireEvent.click(create);
    expect(await screen.findByRole("heading", { name: "Create a role" })).toBeTruthy();
    expect(screen.getByRole("tab", { name: "Connectors" })).toBeTruthy();
  });
});

