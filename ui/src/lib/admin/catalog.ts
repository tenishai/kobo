// The vendors the room knows how to describe when an admin adds an
// integration. A vendor not on this list is still connectable as "Other";
// the server takes any vendor id. Kept to what this room's teams reach for
// today (2026-10-25); one line each, no product tour.
export type CatalogEntry = {
  id: string;
  name: string;
  summary: string;
  /** What the credential field asks for. */
  credential: string;
  /** Placeholder for the vendor's MCP address, when there is a usual one. */
  address?: string;
};

export const CATALOG: CatalogEntry[] = [
  { id: "atlassian", name: "Atlassian", summary: "Jira issues and Confluence pages.", credential: "API token", address: "https://mcp.atlassian.com/v1/mcp" },
  { id: "datadog", name: "Datadog", summary: "Monitors, dashboards and logs.", credential: "API key" },
  { id: "gcp", name: "Google Cloud", summary: "Projects, resources and IAM, read-only unless widened.", credential: "Service account key (JSON)" },
  { id: "aws", name: "AWS", summary: "Accounts and resources, read-only unless widened.", credential: "Access key" },
  { id: "azure", name: "Azure", summary: "Subscriptions and resources, read-only unless widened.", credential: "Client secret" },
  { id: "wiz", name: "Wiz", summary: "Security findings and issues.", credential: "Service account token" },
  { id: "fastly", name: "Fastly", summary: "Services, configs and purges.", credential: "API token" },
  { id: "okta", name: "Okta", summary: "Users, groups and apps.", credential: "API token" },
];

export function catalogEntry(id: string): CatalogEntry | undefined {
  return CATALOG.find((c) => c.id === id);
}
