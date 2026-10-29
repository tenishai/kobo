// The vendors the room knows how to describe when an admin adds an
// integration, each with the credential shape it really uses (Kyle,
// 2026-10-25): AWS is a role the gateway assumes, GCP is workload identity
// or a service account to act as, Azure is a federated app registration,
// Datadog and Wiz are service-account key pairs, the rest are API tokens.
// A vendor not on this list is still connectable as "Other" with a token.
export type AuthKind = "token" | "api_keys" | "aws_role" | "aws_hub" | "aws_spoke" | "gcp_identity" | "azure_federated";

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
    id: "datadog", name: "Datadog", summary: "Monitors, dashboards and logs.", auth: "api_keys",
    fields: [
      { key: "API_KEY", label: "API key", secret: true },
      { key: "APP_KEY", label: "Application key, from a Datadog service account", secret: true },
    ],
    note: "Make the application key under a service account, not a person, so it outlives them.",
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

export function catalogEntry(id: string): CatalogEntry | undefined {
  return CATALOG.find((c) => c.id === id);
}
