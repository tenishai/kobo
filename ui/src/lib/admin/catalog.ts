// The vendors the room knows how to describe when an admin adds an
// integration, each with the credential shape it really uses (Kyle,
// 2026-10-25): AWS is a role the gateway assumes, GCP is workload identity
// or a service account to act as, Azure is a federated app registration,
// Wiz is a service-account key pair, the rest are API tokens. Datadog, with the
// permission service on, is a service account the service mints a scoped
// application key per agent from (kobo#322); with it off, a key pair served
// through the gateway (`DATADOG_GATEWAY`).
// A vendor not on this list is still connectable as "Other" with a token.
export type AuthKind = "token" | "api_keys" | "aws_role" | "aws_hub" | "aws_spoke" | "gcp_identity" | "azure_federated" | "datadog_service_account" | "github_app";

export type CatalogField = {
  key: string;
  label: string;
  /** Stored in the secrets service and never shown again. */
  secret: boolean;
  optional?: boolean;
  placeholder?: string;
};

export type CatalogEntry = {
  id: string;
  name: string;
  summary: string;
  auth: AuthKind;
  fields: CatalogField[];
  /** One line on what the fields mean, when the kind is not a token. */
  note?: string;
  /** Placeholder for the vendor's MCP address, when there is a usual one. */
  address?: string;
};

const TOKEN: CatalogField[] = [{ key: "TOKEN", label: "API token", secret: true }];

export const CATALOG: CatalogEntry[] = [
  { id: "atlassian", name: "Atlassian", summary: "Jira issues and Confluence pages.", auth: "token", fields: TOKEN, address: "https://mcp.atlassian.com/v1/mcp" },
  {
    id: "datadog", name: "Datadog", summary: "Monitors, dashboards and logs.", auth: "datadog_service_account",
    fields: [
      { key: "service_account_id", label: "Service account id", secret: false, placeholder: "00000000-0000-0000-0000-000000000000" },
      { key: "API_KEY", label: "API key", secret: true },
      { key: "MGMT_APP_KEY", label: "Management application key, scoped to service_account_write", secret: true },
      { key: "site", label: "Site", secret: false, optional: true, placeholder: "datadoghq.com" },
    ],
    note: "Make a service account for the room with a role that holds service_account_write and every Datadog read permission; the room mints a short-lived, scoped application key per agent from it. Agents also reach Datadog's MCP server through the gateway with that key.",
  },
  {
    id: "github", name: "GitHub", summary: "Code, issues, pull requests and workflow runs.", auth: "github_app",
    fields: [
      { key: "app_id", label: "App id", secret: false, optional: true, placeholder: "leave blank to use the room's GitHub App" },
      { key: "installation_id", label: "Installation id", secret: false, optional: true },
      { key: "PRIVATE_KEY", label: "Private key (PEM)", secret: true, optional: true },
    ],
    note: "Uses the room's own GitHub App (the one under Integrations that opens pull requests) unless you paste another. The app must hold every permission agents may be granted: metadata, contents, issues, pull requests, actions and checks, with pull requests and contents writable for the Review and Open levels. Each agent picks its own repositories on its Access tab; the room mints it a short-lived installation token narrowed to that set and its level. Agents also reach GitHub's MCP server through the gateway with that token.",
  },
  {
    id: "gcp", name: "Google Cloud", summary: "Projects, resources and IAM.", auth: "gcp_identity",
    fields: [{ key: "service_account", label: "Service account to act as", secret: false, optional: true, placeholder: "name@project.iam.gserviceaccount.com" }],
    note: "No key. The gateway uses workload identity; name a service account only to act as a narrower one.",
  },
  {
    id: "aws", name: "AWS", summary: "An organization: its management account's hub role, then the accounts under it.", auth: "aws_hub",
    fields: [
      { key: "hub_role_arn", label: "Hub role ARN", secret: false, placeholder: "arn:aws:iam::123456789012:role/KoboAccess-Hub" },
    ],
    note: "No access key. The hub role lives in the organization's management account and trusts the room's identity; each member account then gets its own role that the hub assumes.",
  },
  {
    id: "azure", name: "Azure", summary: "Subscriptions and resources.", auth: "azure_federated",
    fields: [
      { key: "client_id", label: "App registration client ID", secret: false },
      { key: "tenant_id", label: "Tenant ID", secret: false },
    ],
    note: "No client secret. The app registration carries a federated credential for the gateway's identity.",
  },
  {
    id: "wiz", name: "Wiz", summary: "Security findings and issues.", auth: "api_keys",
    fields: [
      { key: "CLIENT_ID", label: "Service account client ID", secret: true },
      { key: "CLIENT_SECRET", label: "Service account client secret", secret: true },
    ],
  },
  { id: "fastly", name: "Fastly", summary: "Services, configs and purges.", auth: "token", fields: TOKEN },
  { id: "okta", name: "Okta", summary: "Users, groups and apps.", auth: "token", fields: TOKEN },
];

export const OTHER: CatalogEntry = { id: "", name: "", summary: "A vendor not on this list.", auth: "token", fields: TOKEN };

/** Datadog with the permission service off: one key pair served through the gateway. */
export const DATADOG_GATEWAY: CatalogEntry = {
  id: "datadog", name: "Datadog", summary: "Monitors, dashboards and logs.", auth: "api_keys",
  fields: [
    { key: "API_KEY", label: "API key", secret: true },
    { key: "APP_KEY", label: "Application key, from a Datadog service account", secret: true },
  ],
  note: "Make the application key under a service account, not a person, so it outlives them.",
};

/** The catalog as this room offers it: Datadog's shape follows whether the permission
 * service is on, and GitHub (which only works through it) is offered only then. */
export function catalogFor(accessOn: boolean): CatalogEntry[] {
  return accessOn ? CATALOG : CATALOG.filter((c) => c.id !== "github").map((c) => (c.id === "datadog" ? DATADOG_GATEWAY : c));
}

export function catalogEntry(id: string): CatalogEntry | undefined {
  return CATALOG.find((c) => c.id === id);
}
