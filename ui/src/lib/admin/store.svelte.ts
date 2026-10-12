// The admin panel's data, as one in-memory store (admin-panel demo,
// 2026-10-12). Every pane reads from `admin` and changes it only through the
// functions below, each of which also writes the audit row that the change
// would produce. The shapes are the contract the API will serve later; the
// values are the demo's example room, and nothing here names a real
// organisation.
//
// The model, in Kyle's words: a plugin is one base credential from a vendor
// (a service account or an API token with read/write access), held once by
// the room. A connection is an agent's lane to one of those vendors through
// the gateway, scoped down to what that agent needs. A role is a named set
// of permissions a group holds, including which vendors its agents may be
// granted and at what ceiling.

export type PluginKind = "platform" | "vendor";
export type PluginState = "connected" | "not_configured" | "error";
export type Access = "read" | "write";

export type PluginField = {
  key: string;
  label: string;
  value: string;
  /** Rendered masked; the value is a placeholder, never a real credential. */
  secret?: boolean;
};

export type Plugin = {
  id: string;
  name: string;
  kind: PluginKind;
  /** One line under the name: what the room uses it for. */
  summary: string;
  fields: PluginField[];
  state: PluginState;
  /** One line of live state: last check, counts, or what is missing. */
  detail: string;
  /** The room cannot create agents without it; the card has no Disconnect. */
  required?: boolean;
  /** Ships wired on a fresh room (Slack today). */
  baseline?: boolean;
  /** What the base credential may do at the vendor. Agents are scoped below it. */
  credential?: Access | "read-write";
  /** The gateway lanes this vendor offers to agents. Platform plugins have none. */
  lanes: string[];
};

export type CatalogEntry = {
  id: string;
  name: string;
  summary: string;
  fields: PluginField[];
  lanes: string[];
};

export type LaneState = "none" | "requested" | "pr_open" | "granted";

export type Lane = {
  plugin: string;
  access: Access;
  state: LaneState;
  /** The pull request carrying the grant, while one is open. */
  pr?: string;
};

export type Connection = {
  agent: string;
  group: string;
  owner: string;
  lanes: Lane[];
};

export type Ceiling = "none" | "read" | "write" | "approve";

export type Role = {
  id: string;
  name: string;
  builtin: boolean;
  description: string;
  groups: string[];
  capabilities: string[];
  admin: string[];
  connectors: { plugin: string; ceiling: Ceiling }[];
  models: string[];
};

export type GroupRequest = { id: number; group: string; requester: string; when: string };

export type AuditEntry = {
  id: number;
  when: string;
  who: string;
  change: string;
  landed: string;
  state: "merged" | "open" | "applied";
};

export const CAPABILITIES: { id: string; label: string; desc: string }[] = [
  { id: "create_agents", label: "Create agents", desc: "Build an agent from the catalogs." },
  { id: "connect_repos", label: "Connect repositories", desc: "Bring a repository the team already owns." },
  { id: "chat_teach", label: "Chat and teach", desc: "Talk to visible agents and put facts in their memory." },
  { id: "manage_secrets", label: "Manage secrets", desc: "Set an agent's secrets and environment." },
  { id: "wire_slack", label: "Wire Slack", desc: "Create and attach an agent's Slack app." },
  { id: "invite_colleagues", label: "Invite colleagues", desc: "Accept cross-group colleague invites." },
  { id: "destroy_agents", label: "Destroy agents", desc: "Tear an agent down." },
];

export const ADMIN_PERMISSIONS: { id: string; label: string; desc: string }[] = [
  { id: "manage_plugins", label: "Manage plugins", desc: "Connect and disconnect vendors and platforms." },
  { id: "approve_connections", label: "Approve connections", desc: "Grant an agent a lane to a vendor." },
  { id: "manage_roles", label: "Manage roles", desc: "Create roles and assign groups." },
  { id: "manage_people", label: "Manage people", desc: "Admins, operators, readers, group requests." },
  { id: "view_audit", label: "View audit", desc: "Read who changed what." },
];

export const MODELS: { id: string; label: string }[] = [
  { id: "sonnet", label: "Sonnet 5.5" },
  { id: "opus", label: "Opus 5.5" },
  { id: "haiku", label: "Haiku 4.5" },
  { id: "gemini-flash", label: "Gemini Flash" },
  { id: "gemini-pro", label: "Gemini Pro" },
];

export const CATALOG: CatalogEntry[] = [
  {
    id: "pagerduty",
    name: "PagerDuty",
    summary: "Incidents, on-call schedules, escalation policies.",
    fields: [{ key: "api_token", label: "API token", value: "", secret: true }],
    lanes: ["pagerduty"],
  },
  {
    id: "google-workspace",
    name: "Google Workspace",
    summary: "Drive, Docs and Calendar through one service account.",
    fields: [
      { key: "service_account", label: "Service account email", value: "" },
      { key: "key", label: "Key (JSON)", value: "", secret: true },
    ],
    lanes: ["drive", "calendar"],
  },
  {
    id: "snowflake",
    name: "Snowflake",
    summary: "Read-only warehouse queries.",
    fields: [
      { key: "account", label: "Account", value: "" },
      { key: "user", label: "User", value: "" },
      { key: "key", label: "Private key", value: "", secret: true },
    ],
    lanes: ["snowflake"],
  },
];

const PLUGINS: Plugin[] = [
  {
    id: "deploy",
    name: "Deploy platform",
    kind: "platform",
    summary: "Identity, CI, manifests and teardown for every agent.",
    fields: [
      { key: "url", label: "URL", value: "http://gojira.gojira.svc:8080" },
      { key: "cluster", label: "Cluster", value: "agents-dev" },
      { key: "token", label: "Token", value: "set in the deployment config", secret: true },
    ],
    state: "connected",
    detail: "Connected. Last sync 2 minutes ago. Webhook delivering.",
    required: true,
    lanes: [],
  },
  {
    id: "noto",
    name: "Observability (noto)",
    kind: "platform",
    summary: "Dashboards and alerts for every agent, linked from its card.",
    fields: [
      { key: "url", label: "URL", value: "" },
      { key: "project", label: "Project", value: "" },
    ],
    state: "not_configured",
    detail: "Not configured. Agent cards show no dashboard link.",
    lanes: [],
  },
  {
    id: "sero",
    name: "Secrets (sero)",
    kind: "platform",
    summary: "Holds each agent's secrets and syncs them into its namespace.",
    fields: [
      { key: "url", label: "URL", value: "" },
      { key: "audience", label: "Audience", value: "" },
      { key: "namespace", label: "Namespace", value: "" },
    ],
    state: "not_configured",
    detail: "Not configured. The room writes Secrets itself.",
    lanes: [],
  },
  {
    id: "cabal",
    name: "Switchboard (cabal)",
    kind: "platform",
    summary: "Chat, teach and agent-to-agent talk relay through it.",
    fields: [
      { key: "url", label: "URL", value: "http://cabal-core.cabal.svc" },
      { key: "endpoint_template", label: "Endpoint template", value: "http://{name}.{name}.svc:8080" },
      { key: "operator_token", label: "Operator token", value: "set in the deployment config", secret: true },
    ],
    state: "connected",
    detail: "Connected. 11 agents registered.",
    lanes: [],
  },
  {
    id: "gateway",
    name: "Gateway (liminal)",
    kind: "platform",
    summary: "The one door every agent's vendor lane goes through.",
    fields: [
      { key: "url", label: "URL", value: "http://liminal.liminal.svc" },
      { key: "grants_repo", label: "Grants repository", value: "example/gateway-config" },
    ],
    state: "connected",
    detail: "Connected. 3 agents granted, 1 request waiting.",
    lanes: [],
  },
  {
    id: "directory",
    name: "Directory (Okta)",
    kind: "platform",
    summary: "Groups decide who sees which agents and who may approve.",
    fields: [
      { key: "domain", label: "Domain", value: "example.okta.com" },
      { key: "groups", label: "Groups", value: "46 synced" },
    ],
    state: "connected",
    detail: "Connected. 46 groups, refreshed hourly.",
    lanes: [],
  },
  {
    id: "slack",
    name: "Slack",
    kind: "vendor",
    summary: "One config token pair; the room creates an app per agent.",
    fields: [{ key: "config_tokens", label: "Config token pair", value: "stored", secret: true }],
    state: "connected",
    detail: "Connected. 7 agent apps. Baseline plugin.",
    baseline: true,
    credential: "read-write",
    lanes: ["slack"],
  },
  {
    id: "github",
    name: "GitHub App",
    kind: "vendor",
    summary: "One App installation; agents read repositories and open pull requests through it.",
    fields: [
      { key: "app_id", label: "App ID", value: "5141957" },
      { key: "installation", label: "Installation", value: "example org" },
      { key: "private_key", label: "Private key", value: "stored", secret: true },
    ],
    state: "connected",
    detail: "Connected. 2 agents granted.",
    credential: "read-write",
    lanes: ["github"],
  },
  {
    id: "atlassian",
    name: "Atlassian",
    kind: "vendor",
    summary: "Jira and Confluence through one scoped API token.",
    fields: [
      { key: "site", label: "Site", value: "example.atlassian.net" },
      { key: "user", label: "Service user", value: "automation@example.com" },
      { key: "token", label: "API token (scoped)", value: "stored", secret: true },
    ],
    state: "connected",
    detail: "Connected. Read scope only. 1 agent granted.",
    credential: "read",
    lanes: ["atlassian"],
  },
  {
    id: "datadog",
    name: "Datadog",
    kind: "vendor",
    summary: "Monitors, logs and metrics through one API and app key pair.",
    fields: [
      { key: "site", label: "Site", value: "" },
      { key: "api_key", label: "API key", value: "", secret: true },
      { key: "app_key", label: "Application key", value: "", secret: true },
    ],
    state: "not_configured",
    detail: "Not configured. Agents answer with a curl recipe instead.",
    lanes: ["datadog"],
  },
];

const CONNECTIONS: Connection[] = [
  {
    agent: "ledger",
    group: "finance",
    owner: "maya@example.com",
    lanes: [{ plugin: "atlassian", access: "read", state: "granted" }],
  },
  {
    agent: "sherlock",
    group: "sre",
    owner: "dev@example.com",
    lanes: [{ plugin: "github", access: "read", state: "granted" }],
  },
  {
    agent: "janus",
    group: "platform",
    owner: "dylan@example.com",
    lanes: [{ plugin: "github", access: "read", state: "requested" }],
  },
  { agent: "wren", group: "sre", owner: "kyle@example.com", lanes: [] },
  { agent: "prudence", group: "sre", owner: "kyle@example.com", lanes: [] },
  {
    agent: "clio",
    group: "sre",
    owner: "kyle@example.com",
    lanes: [{ plugin: "github", access: "write", state: "granted" }],
  },
];

const ROLES: Role[] = [
  {
    id: "admin",
    name: "Admin",
    builtin: true,
    description: "Runs the room. Every capability and every admin permission.",
    groups: ["platform-admins"],
    capabilities: CAPABILITIES.map((c) => c.id),
    admin: ADMIN_PERMISSIONS.map((p) => p.id),
    connectors: [
      { plugin: "slack", ceiling: "approve" },
      { plugin: "github", ceiling: "approve" },
      { plugin: "atlassian", ceiling: "approve" },
      { plugin: "datadog", ceiling: "approve" },
    ],
    models: MODELS.map((m) => m.id),
  },
  {
    id: "operator",
    name: "Operator",
    builtin: true,
    description: "Builds and runs agents for a team; may approve its own team's lanes.",
    groups: ["sre", "platform"],
    capabilities: CAPABILITIES.map((c) => c.id),
    admin: ["approve_connections", "view_audit"],
    connectors: [
      { plugin: "slack", ceiling: "write" },
      { plugin: "github", ceiling: "write" },
      { plugin: "atlassian", ceiling: "read" },
      { plugin: "datadog", ceiling: "read" },
    ],
    models: MODELS.map((m) => m.id),
  },
  {
    id: "member",
    name: "Member",
    builtin: true,
    description: "Creates and teaches agents in their own group.",
    groups: ["finance", "growth"],
    capabilities: ["create_agents", "chat_teach", "manage_secrets", "wire_slack", "invite_colleagues"],
    admin: [],
    connectors: [
      { plugin: "slack", ceiling: "write" },
      { plugin: "github", ceiling: "read" },
      { plugin: "atlassian", ceiling: "read" },
      { plugin: "datadog", ceiling: "none" },
    ],
    models: ["sonnet", "haiku", "gemini-flash"],
  },
  {
    id: "reader",
    name: "Reader",
    builtin: true,
    description: "Sees every agent, changes nothing.",
    groups: ["sre-leads"],
    capabilities: [],
    admin: ["view_audit"],
    connectors: [],
    models: [],
  },
];

const AUDIT: AuditEntry[] = [
  { id: 4, when: "today 10:12", who: "kyle@example.com", change: "Approved lane for sherlock: GitHub App, read", landed: "PR #9 merged", state: "merged" },
  { id: 3, when: "yesterday 17:30", who: "sam@example.com", change: "Added group sre", landed: "PR #8 merged", state: "merged" },
  { id: 2, when: "yesterday 17:02", who: "sam@example.com", change: "Became first admin", landed: "PR #7 merged", state: "merged" },
  { id: 1, when: "Oct 7 16:10", who: "kyle@example.com", change: "Connected Atlassian (read scope)", landed: "PR #6 merged", state: "merged" },
];

export const ME = "kyle@example.com";

export const admin = $state({
  plugins: PLUGINS,
  connections: CONNECTIONS,
  roles: ROLES,
  people: {
    adminGroups: ["platform-admins"],
    operators: ["kyle@example.com", "sam@example.com"],
    readerGroups: ["sre-leads"],
    groupRequests: [
      { id: 1, group: "dogs", requester: "d.h@example.com", when: "Oct 7 19:40" },
      { id: 2, group: "growth-eng", requester: "sam@example.com", when: "Oct 8 09:05" },
    ] as GroupRequest[],
  },
  audit: AUDIT,
  nextPr: 10,
  nextAudit: 5,
});

function record(change: string, landed: "pr" | "applied"): string {
  let text: string;
  let state: AuditEntry["state"];
  if (landed === "pr") {
    text = `PR #${admin.nextPr} open`;
    admin.nextPr += 1;
    state = "open";
  } else {
    text = "applied";
    state = "applied";
  }
  admin.audit.unshift({ id: admin.nextAudit, when: "just now", who: ME, change, landed: text, state });
  admin.nextAudit += 1;
  return text;
}

export function plugin(id: string): Plugin | undefined {
  return admin.plugins.find((p) => p.id === id);
}

export function pluginName(id: string): string {
  return plugin(id)?.name ?? id;
}

/** Vendors agents can be granted a lane to: connected vendor plugins. */
export function vendorsWithLanes(): Plugin[] {
  return admin.plugins.filter((p) => p.kind === "vendor" && p.state === "connected");
}

export function connectPlugin(id: string, values: Record<string, string>): string {
  const p = plugin(id);
  if (!p) return "";
  for (const f of p.fields) {
    if (values[f.key] !== undefined && values[f.key] !== "") {
      f.value = f.secret ? "stored" : values[f.key];
    }
  }
  p.state = "connected";
  p.detail = "Connected. Takes effect at the room's next boot.";
  return record(`Connected ${p.name}`, "pr");
}

export function disconnectPlugin(id: string): string {
  const p = plugin(id);
  if (!p || p.required) return "";
  p.state = "not_configured";
  p.detail = "Not configured.";
  for (const f of p.fields) {
    if (f.secret) f.value = "";
  }
  return record(`Disconnected ${p.name}`, "pr");
}

export function addPlugin(catalogId: string): Plugin | undefined {
  const entry = CATALOG.find((c) => c.id === catalogId);
  if (!entry || plugin(entry.id)) return undefined;
  const p: Plugin = {
    id: entry.id,
    name: entry.name,
    kind: "vendor",
    summary: entry.summary,
    fields: entry.fields.map((f) => ({ ...f })),
    state: "not_configured",
    detail: "Not configured.",
    lanes: entry.lanes,
  };
  admin.plugins.push(p);
  return p;
}

export function connection(agent: string): Connection | undefined {
  return admin.connections.find((c) => c.agent === agent);
}

export function requestLane(agent: string, pluginId: string, access: Access): void {
  const c = connection(agent);
  if (!c) return;
  const existing = c.lanes.find((l) => l.plugin === pluginId);
  if (existing) {
    existing.access = access;
    existing.state = "requested";
    delete existing.pr;
  } else {
    c.lanes.push({ plugin: pluginId, access, state: "requested" });
  }
  record(`Requested lane for ${agent}: ${pluginName(pluginId)}, ${access}`, "applied");
}

/** Approve a lane. `pr` opens a pull request against the grants file (the
 * durable lane); `now` applies it through the gateway's admin plane. */
export function approveLane(agent: string, pluginId: string, mode: "pr" | "now"): void {
  const c = connection(agent);
  const lane = c?.lanes.find((l) => l.plugin === pluginId);
  if (!c || !lane) return;
  const what = `Approved lane for ${agent}: ${pluginName(pluginId)}, ${lane.access}`;
  if (mode === "pr") {
    lane.state = "pr_open";
    lane.pr = record(what, "pr");
  } else {
    lane.state = "granted";
    delete lane.pr;
    record(`${what} (applied now)`, "applied");
  }
}

export function grantLane(agent: string, pluginId: string, access: Access, mode: "pr" | "now"): void {
  const c = connection(agent);
  if (!c) return;
  if (!c.lanes.find((l) => l.plugin === pluginId)) {
    c.lanes.push({ plugin: pluginId, access, state: "requested" });
  } else {
    const lane = c.lanes.find((l) => l.plugin === pluginId)!;
    lane.access = access;
  }
  approveLane(agent, pluginId, mode);
}

export function revokeLane(agent: string, pluginId: string): void {
  const c = connection(agent);
  if (!c) return;
  const idx = c.lanes.findIndex((l) => l.plugin === pluginId);
  if (idx < 0) return;
  c.lanes.splice(idx, 1);
  record(`Revoked lane for ${agent}: ${pluginName(pluginId)}`, "pr");
}

/** Simulate the pull request landing, which is what the gateway seeds from. */
export function markLanded(agent: string, pluginId: string): void {
  const lane = connection(agent)?.lanes.find((l) => l.plugin === pluginId);
  if (!lane || lane.state !== "pr_open") return;
  const pr = lane.pr;
  lane.state = "granted";
  delete lane.pr;
  const row = admin.audit.find((a) => a.landed === pr);
  if (row) {
    row.landed = pr!.replace("open", "merged");
    row.state = "merged";
  }
}

export function role(id: string): Role | undefined {
  return admin.roles.find((r) => r.id === id);
}

export function blankRole(): Role {
  return {
    id: "",
    name: "",
    builtin: false,
    description: "",
    groups: [],
    capabilities: ["chat_teach"],
    admin: [],
    connectors: admin.plugins
      .filter((p) => p.kind === "vendor")
      .map((p) => ({ plugin: p.id, ceiling: "none" as Ceiling })),
    models: ["sonnet"],
  };
}

export function saveRole(draft: Role): Role {
  const id = draft.id || draft.name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/(^-|-$)/g, "");
  const existing = role(id);
  const saved: Role = { ...draft, id, builtin: existing?.builtin ?? false };
  if (existing) {
    Object.assign(existing, saved);
    record(`Changed role ${saved.name}`, "pr");
    return existing;
  }
  admin.roles.push(saved);
  record(`Created role ${saved.name} for ${saved.groups.join(", ") || "no groups yet"}`, "pr");
  return saved;
}

export function deleteRole(id: string): void {
  const r = role(id);
  if (!r || r.builtin) return;
  admin.roles.splice(admin.roles.indexOf(r), 1);
  record(`Deleted role ${r.name}`, "pr");
}

export function decideGroupRequest(id: number, action: "add" | "decline"): void {
  const idx = admin.people.groupRequests.findIndex((g) => g.id === id);
  if (idx < 0) return;
  const [req] = admin.people.groupRequests.splice(idx, 1);
  if (action === "add") {
    record(`Added group ${req.group} (requested by ${req.requester})`, "pr");
  } else {
    record(`Declined group request ${req.group}`, "applied");
  }
}

export function setPeople(kind: "adminGroups" | "operators" | "readerGroups", values: string[]): void {
  admin.people[kind] = values;
  const label = kind === "adminGroups" ? "admin groups" : kind === "operators" ? "operators" : "reader groups";
  record(`Set ${label}: ${values.join(", ") || "none"}`, "pr");
}

/** What the Overview lists under "needs attention". Derived, never stored. */
export function attention(): { tone: "fail" | "warn"; text: string; pane: string; item?: string }[] {
  const out: { tone: "fail" | "warn"; text: string; pane: string; item?: string }[] = [];
  for (const c of admin.connections) {
    for (const l of c.lanes) {
      if (l.state === "requested") {
        out.push({ tone: "warn", text: `${c.agent} asked for ${pluginName(l.plugin)} (${l.access})`, pane: "connections", item: c.agent });
      }
      if (l.state === "pr_open") {
        out.push({ tone: "warn", text: `${c.agent}'s ${pluginName(l.plugin)} grant waits on ${l.pr}`, pane: "connections", item: c.agent });
      }
    }
  }
  if (admin.people.groupRequests.length > 0) {
    out.push({
      tone: "warn",
      text: `${admin.people.groupRequests.length} group request${admin.people.groupRequests.length === 1 ? "" : "s"} waiting`,
      pane: "people",
    });
  }
  for (const p of admin.plugins) {
    if (p.state === "error") {
      out.push({ tone: "fail", text: `${p.name}: ${p.detail}`, pane: "plugins", item: p.id });
    }
  }
  return out;
}
