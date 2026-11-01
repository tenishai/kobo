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
  // GitHub as an older install's deployment config carries it (kobo#377): a row like the
  // rest, marked from the deployment config, with Replace and no Remove.
  const GITHUB_FROM_DEPLOYMENT = {
    id: "github", vendor: "github", name: "GitHub", base_access: "read", state: "applied",
    state_detail: "set in the deployment config; Replace to hold it in the room", credential_ref: null,
    spec: { auth: "github_app", source: "deployment", identity: {}, secret_keys: [] }, created_by: "", created_at: "", updated_at: "",
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

  // The Connect Google Cloud page (kobo#390): the identity to grant and the roles three ways.
  const GCP_SETUP = {
    version: "v0.2.0 0503b5b", mode: "project", principal: "access",
    identity: { email: "access@example.iam.gserviceaccount.com", subject: "102000000000000000000" },
    identity_note: null,
    packs: [
      { id: "inventory", purpose: "See what exists.", required: true, roles: ["roles/compute.viewer"] },
      { id: "models", purpose: "Use the project's Vertex AI models.", required: false, roles: ["roles/aiplatform.user"] },
    ],
    chosen: ["inventory"], roles: ["roles/compute.viewer"], target: "",
    gcloud: "gcloud projects add-iam-policy-binding <project id> --member=serviceAccount:access@example.iam.gserviceaccount.com --role=roles/compute.viewer",
    terraform: 'resource "google_project_iam_member" "kobo_access" {}',
    vertex_default_location: "us-east5",
    checked: true,
  };

  beforeEach(() => {
    vi.unstubAllGlobals();
    stubFetch({
      "/api/whoami": ADMIN_WHOAMI,
      "/api/admin/addons": ADDONS,
      "/api/admin/room": { org_repo: "example/kobo-config", runtime_image: "registry/daedalus-runtime@sha256:789b43aa", cluster: "agents-dev", delivery_backend: "deployed" },
      // Listed before the integrations list: the stub matches by substring, first wins.
      "/api/admin/integrations/aws/setup": AWS_SETUP,
      "/api/admin/integrations/gcp/setup": GCP_SETUP,
      "/api/admin/integrations": { integrations: [GITHUB_FROM_DEPLOYMENT] },
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

  it("Overview carries an Agent connections card, with how many agents hold access, when the permission service is on", async () => {
    // The card's figure is the room's cheap count (`/api/admin/access/summary`),
    // never the pane's full read; it opens the Agent connections page.
    vi.unstubAllGlobals();
    stubFetch({
      "/api/whoami": ADMIN_WHOAMI,
      "/api/admin/addons": { addons: [{ id: "access", name: "Access", summary: "", state: "on", detail: "", keys: [], owner: "" }] },
      "/api/admin/room": { org_repo: "example/kobo-config", runtime_image: "r", cluster: "c", delivery_backend: "deployed" },
      "/api/admin/integrations": { integrations: [] },
      "/api/admin/roles": ROLES,
      "/api/admin/connections": CONNECTIONS,
      "/api/admin/access/summary": { agents_with_access: 1, agents: 2, fetched_at: "2026-11-01T10:00:00Z" },
      "/api/admin/access/requests": [],
      "/api/admin/access/leases": { leases: [] },
      "/api/group-requests": { requests: REQUESTS },
    });
    navigate({ page: "admin", pane: "overview" });
    renderWithClient(AdminPanel);
    const figure = await screen.findByText("1 agent holds access");
    const card = figure.closest("a") as HTMLAnchorElement;
    expect(card.getAttribute("href")).toContain("admin/connections");
    expect(card.textContent).toContain("Agent connections");
    expect(card.textContent).toContain("Which agents reach which vendors, and what they hold right now.");
    // The lanes card is the other face of the same page, and stays away.
    expect(screen.queryByText(/without a lane|all with a lane/)).toBeNull();
    const summaryCalls = vi.mocked(fetch).mock.calls.filter(([url]) => String(url).includes("/api/admin/access/summary"));
    expect(summaryCalls.length).toBeGreaterThanOrEqual(1);
    expect(vi.mocked(fetch).mock.calls.some(([url]) => String(url).includes("/api/admin/access/agents"))).toBe(false);
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

  it("with the permission service on, the page is Agent connections and reads what every agent holds", async () => {
    // Issue #371: the same id, the newer page; the old lanes page stays for a room with Access off.
    vi.unstubAllGlobals();
    stubFetch({
      "/api/whoami": ADMIN_WHOAMI,
      "/api/admin/addons": { addons: [{ id: "access", name: "Access", summary: "", state: "on", detail: "", keys: [], owner: "" }] },
      "/api/admin/room": { org_repo: "example/kobo-config", runtime_image: "r", cluster: "c", delivery_backend: "deployed" },
      "/api/admin/access/agents": { agents: [{ name: "janus", team: null, status: "ready", vendors: [] }], fetched_at: "2026-11-01T10:00:00Z" },
    });
    navigate({ page: "admin", pane: "connections" });
    renderWithClient(AdminPanel);
    expect(await screen.findByRole("heading", { name: "Agent connections" })).toBeTruthy();
    expect(await screen.findByRole("link", { name: "Agent connections" })).toBeTruthy();
    expect(screen.queryByRole("link", { name: "Connections" })).toBeNull();
    expect(await screen.findByText("janus")).toBeTruthy();
    expect(screen.getByText("No agent holds anything yet.")).toBeTruthy();
  });

  it("Integrations is one kind of row, with Slack and GitHub connected through the catalog", async () => {
    navigate({ page: "admin", pane: "integrations" });
    renderWithClient(AdminPanel);
    expect(await screen.findByRole("heading", { name: "Integrations" })).toBeTruthy();
    // No service kind and no rows that are always there (kobo#377): the one row is the
    // GitHub app the deployment config carries, a connection like any other.
    expect(await screen.findByText("GitHub")).toBeTruthy();
    expect(screen.queryByText("service")).toBeNull();
    expect(screen.queryByText("Not set up")).toBeNull();
    expect(screen.getAllByText("connection")).toHaveLength(1);
    // Each row carries its vendor's mark beside the name (issue #368).
    expect(document.querySelectorAll("table .vendor-mark").length).toBe(1);
    // A value the deployment config carries reads Connected like any other row, with the
    // provenance as a note and Replace to move it into the room (#337); nothing to Remove.
    expect((await screen.findAllByText("Connected")).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("set in the deployment config").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByRole("button", { name: "Replace" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Remove" })).toBeNull();
    await fireEvent.click(screen.getByRole("button", { name: "Add integration" }));
    expect(await screen.findByRole("heading", { name: "Add an integration" })).toBeTruthy();
    // The catalog is this room's vendors, one line each; Slack is one of them and GitHub,
    // already connected, is not offered twice.
    for (const vendor of ["Atlassian", "Datadog", "Google Cloud", "AWS", "Azure", "Wiz", "Fastly", "Okta", "Slack"]) {
      expect(screen.getByText(vendor)).toBeTruthy();
    }
    expect(screen.queryByRole("button", { name: "Add GitHub" })).toBeNull();
    expect(screen.queryByText("PagerDuty")).toBeNull();
    // AWS is its own sheet: what agents may read, the role three ways, paste the ARN. No key anywhere.
    await fireEvent.click(screen.getByRole("button", { name: "Add AWS" }));
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

  it("Google Cloud is its own sheet: the identity to grant, the roles three ways, then a project and its Vertex AI models", async () => {
    // kobo#390. No key anywhere: the form submits the project under `gcp_identity`, with
    // the models fields when the Vertex AI box is ticked.
    navigate({ page: "admin", pane: "integrations" });
    renderWithClient(AdminPanel);
    await fireEvent.click(await screen.findByRole("button", { name: "Add integration" }));
    await fireEvent.click(await screen.findByRole("button", { name: "Add Google Cloud" }));
    expect(await screen.findByRole("heading", { name: "Connect Google Cloud" })).toBeTruthy();
    expect(await screen.findByText("access@example.iam.gserviceaccount.com")).toBeTruthy();
    expect(screen.getByRole("radio", { name: /A project/ }).getAttribute("aria-checked")).toBe("true");
    expect(screen.getByRole("radio", { name: /A folder or organization/ })).toBeTruthy();
    for (const method of [/gcloud/, /Terraform/, /By hand/]) expect(screen.getByRole("radio", { name: method })).toBeTruthy();
    expect(screen.getByText(/gcloud projects add-iam-policy-binding/)).toBeTruthy();
    expect(document.querySelector("#integration-form input[type=password]")).toBeNull();
    await fireEvent.input(screen.getByLabelText("Name for this connection"), { target: { value: "Example prod" } });
    await fireEvent.input(screen.getByLabelText("Project id"), { target: { value: "example-prod-1234" } });
    await fireEvent.click(screen.getByLabelText(/Vertex AI models in the room/));
    const location = (await screen.findByLabelText("Location")) as HTMLInputElement;
    expect(location.value).toBe("us-east5");
    await fireEvent.click(screen.getByRole("button", { name: "Connect" }));
    const post = vi.mocked(fetch).mock.calls.find(([url, init]) => String(url) === "/api/admin/integrations" && init?.method === "POST");
    expect(post).toBeTruthy();
    const body = JSON.parse(String(post?.[1]?.body));
    expect(body.vendor).toBe("gcp");
    expect(body.auth).toBe("gcp_identity");
    expect(body.identity).toEqual({ project: "example-prod-1234", models_provider: "vertex_ai", models_location: "us-east5" });
    expect(body.secrets).toEqual({});
  });

  it("Self-hosted models is a form with the address, an optional key, the models and the options, and a row with its host, count, failures and Check models", async () => {
    // kobo#390. The form submits under `self_hosted_endpoint` with the key as the one secret;
    // the row shows the host alone (never the key), how many models answer and which failed,
    // and Check models runs the probe again.
    const LAB = {
      id: "lab-gpu-pool", vendor: "self-hosted", name: "Lab GPU pool", base_access: "read", state: "applied",
      state_detail: "10.0.0.5 answers; 2 of 3 models answer; `old-model` failed: HTTP 404: model 'old-model' does not exist",
      credential_ref: "secret:kobo/self-hosted-lab-gpu-pool",
      spec: {
        auth: "self_hosted_endpoint", base_url: "http://10.0.0.5/v1", options: { chat_template_kwargs: { enable_thinking: false } },
        models: [
          { name: "qwen3-32b", label: "Qwen3 32B", ok: true, reason: null },
          { name: "llama-3-70b", label: "llama-3-70b", ok: true, reason: null },
          { name: "old-model", label: "old-model", ok: false, reason: "HTTP 404: model 'old-model' does not exist" },
        ],
      },
      created_by: "k", created_at: "2026-11-01T10:00:00Z", updated_at: "2026-11-01T10:00:00Z",
    };
    vi.unstubAllGlobals();
    stubFetch({
      "/api/whoami": ADMIN_WHOAMI,
      "/api/admin/addons": ADDONS,
      "/api/admin/room": { org_repo: "example/kobo-config", runtime_image: "r", cluster: "c", delivery_backend: "deployed" },
      "/api/admin/integrations/lab-gpu-pool/models/check": { id: "lab-gpu-pool", models: LAB.spec.models, answering: 2, total: 3, state: "applied", detail: LAB.state_detail },
      "/api/admin/integrations": { integrations: [LAB] },
      "/api/admin/roles": ROLES,
      "/api/admin/connections": CONNECTIONS,
      "/api/admin/people": PEOPLE,
      "/api/admin/audit": AUDIT,
      "/api/group-requests": { requests: REQUESTS },
    });
    navigate({ page: "admin", pane: "integrations" });
    renderWithClient(AdminPanel);
    expect(await screen.findByText("Lab GPU pool")).toBeTruthy();
    expect(screen.getByText("10.0.0.5")).toBeTruthy();
    expect(screen.getByText("2 of 3 models answer")).toBeTruthy();
    expect(screen.getByText(/old-model failed: HTTP 404/)).toBeTruthy();
    expect(screen.getByText("models")).toBeTruthy();
    expect(document.body.textContent).not.toContain("self-hosted-lab-gpu-pool");
    await fireEvent.click(screen.getByRole("button", { name: "Check models" }));
    const check = vi.mocked(fetch).mock.calls.find(([url, init]) => String(url).endsWith("/api/admin/integrations/lab-gpu-pool/models/check") && init?.method === "POST");
    expect(check).toBeTruthy();
    // A second endpoint is a second row: the kind stays addable.
    await fireEvent.click(screen.getByRole("button", { name: "Add integration" }));
    // The description is on the row and on the catalog card.
    expect(await screen.findAllByText("Models you run yourself, on your own machines or clusters, at an address you give the room.")).toHaveLength(2);
    await fireEvent.click(screen.getByRole("button", { name: "Add Self-hosted models" }));
    expect(await screen.findByRole("heading", { name: "Connect self-hosted models" })).toBeTruthy();
    const key = screen.getByLabelText(/API key/) as HTMLInputElement;
    expect(key.type).toBe("password");
    expect(key.required).toBe(false);
    expect(screen.getByLabelText("Base address")).toBeTruthy();
    expect(screen.getByLabelText("Models it serves, one per line").tagName).toBe("TEXTAREA");
    const options = screen.getByLabelText(/Request options for every model/) as HTMLTextAreaElement;
    expect(options.tagName).toBe("TEXTAREA");
    expect(options.placeholder).toContain("enable_thinking");
    // Only models: no MCP address, no read/write choice.
    expect(screen.queryByLabelText(/MCP address/)).toBeNull();
    expect(screen.queryByText("Agents may")).toBeNull();
    await fireEvent.input(screen.getByLabelText("Name for this endpoint"), { target: { value: "Second pool" } });
    await fireEvent.input(screen.getByLabelText("Base address"), { target: { value: "https://models.example.internal/v1" } });
    await fireEvent.input(key, { target: { value: "sk-second" } });
    await fireEvent.input(screen.getByLabelText("Models it serves, one per line"), { target: { value: "qwen3-32b = Qwen3 32B\nllama-3-70b" } });
    await fireEvent.input(options, { target: { value: "chat_template_kwargs: {enable_thinking: false}" } });
    await fireEvent.click(screen.getByRole("button", { name: "Connect" }));
    const post = vi.mocked(fetch).mock.calls.find(([url, init]) => String(url) === "/api/admin/integrations" && init?.method === "POST");
    expect(post).toBeTruthy();
    const body = JSON.parse(String(post?.[1]?.body));
    expect(body.vendor).toBe("self-hosted");
    expect(body.name).toBe("Second pool");
    expect(body.auth).toBe("self_hosted_endpoint");
    expect(body.identity).toEqual({
      base_url: "https://models.example.internal/v1",
      models: "qwen3-32b = Qwen3 32B\nllama-3-70b",
      options: "chat_template_kwargs: {enable_thinking: false}",
    });
    expect(body.secrets).toEqual({ api_key: "sk-second" });
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

