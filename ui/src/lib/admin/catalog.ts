// The vendors the room knows how to describe when an admin adds an
// integration, each with the credential shape it really uses (Kyle,
// 2026-10-25): AWS is a role the gateway assumes, Google Cloud is the room's
// identity granted once on an organization, or on one project (kobo#390, kobo#405),
// either naming the Vertex AI project, Azure is a federated app registration,
// Wiz is a service-account key pair, the rest are API tokens. Datadog, with the
// permission service on, is a service account the service mints a scoped
// application key per agent from (kobo#322); with it off, a key pair served
// through the gateway (`DATADOG_GATEWAY`).
// Slack and GitHub (kobo#377) connect like every vendor, with one difference: their
// credential is also the room's own. Slack's is the app-configuration token pair the
// room makes agents' Slack apps with; GitHub's is the App behind the room's pull
// requests and agent repositories, and, with the permission service on, the one
// agents borrow narrowed installation tokens from.
// Self-hosted models (kobo#390) is the one kind that is only models: an OpenAI-compatible
// endpoint the person runs, named by them, one row per endpoint; the room probes it and
// serves its models through the model gateway. Nothing is leased.
// A vendor not on this list is still connectable as "Other" with a token.
export type AuthKind = "token" | "api_keys" | "aws_role" | "aws_hub" | "aws_spoke" | "gcp_identity" | "gcp_parent" | "azure_federated" | "datadog_service_account" | "github_app" | "slack_config_token" | "self_hosted_endpoint";

export type CatalogField = {
  key: string;
  label: string;
  /** Stored in the secrets service and never shown again. */
  secret: boolean;
  optional?: boolean;
  placeholder?: string;
  /** A text area rather than one line (a PEM key). */
  multiline?: boolean;
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

/** The one kind that is only models (kobo#390); a room may hold many rows of it. */
export const SELF_HOSTED = "self-hosted";
export const SELF_HOSTED_AUTH = "self_hosted_endpoint";
export const SELF_HOSTED_SUMMARY = "Models you run yourself, on your own machines or clusters, at an address you give the room.";

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
    id: "github", name: "GitHub", summary: "The room's own pull requests and agent repositories; code, issues and pull requests for agents.", auth: "github_app",
    fields: [
      { key: "app_id", label: "App ID", secret: false, placeholder: "123456" },
      { key: "installation_id", label: "Installation ID", secret: false, placeholder: "12345678" },
      { key: "PRIVATE_KEY", label: "Private key (PEM)", secret: true, multiline: true, placeholder: "-----BEGIN RSA PRIVATE KEY-----" },
    ],
    note: "The room's GitHub App. App ID and the private key come from the app's settings page on GitHub (Developer settings, GitHub Apps); the installation ID is the number at the end of the installation's URL under Install App. The room opens its own pull requests with it (skills, tooling) and, with the permission service on, mints each agent a short-lived installation token narrowed to the repositories its owner picks on its Access tab and its level; agents also reach GitHub's MCP server through the gateway with that token. For that the app must hold every permission agents may be granted: metadata, contents, issues, pull requests, actions and checks, with pull requests and contents writable for the Review and Open levels.",
  },
  {
    id: "slack", name: "Slack", summary: "Lets the room create agents' Slack apps.", auth: "slack_config_token",
    fields: [
      { key: "CONFIG_TOKEN", label: "App configuration token", secret: true, placeholder: "xoxe.xoxp-..." },
      { key: "REFRESH_TOKEN", label: "Refresh token", secret: true, placeholder: "xoxe-..." },
    ],
    note: "Both values come from api.slack.com/apps, under Your App Configuration Tokens: generate a token for your workspace and paste the pair. Every Slack app the room creates belongs to the account that generated the token, so use a dedicated kobo account rather than your own: create a Slack user for kobo in your workspace, sign in as it at api.slack.com/apps, generate the token there, and keep its sign-in with the room's operators. The token pair rotates on its own once pasted.",
  },
  {
    id: "gcp", name: "Google Cloud", summary: "An organization, granted once, or one project: resources, settings and the Vertex AI models for the room.", auth: "gcp_parent",
    fields: [
      { key: "parent_resource", label: "Organization", secret: false, placeholder: "organizations/123456789012" },
    ],
    note: "No key. Grant the room's identity the roles the page shows, once on the organization, whose projects then connect by themselves, or on one project. Tick Vertex AI and name the project whose models the room offers.",
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
  {
    id: SELF_HOSTED, name: "Self-hosted models", summary: SELF_HOSTED_SUMMARY, auth: "self_hosted_endpoint",
    fields: [
      { key: "base_url", label: "Base address", secret: false, placeholder: "http://10.0.0.5 or https://models.example.internal/v1" },
      { key: "api_key", label: "API key", secret: true, optional: true },
      { key: "models", label: "Models it serves, one per line", secret: false, multiline: true, placeholder: "qwen3-32b = Qwen3 32B\nllama-3-70b" },
      { key: "options", label: "Request options for every model", secret: false, optional: true, multiline: true, placeholder: "chat_template_kwargs: {enable_thinking: false}" },
    ],
    note: "The address is the root the server answers /v1/models at; the room adds /v1 when it is missing. On connect the room lists the models there and asks each one you named for a short answer, so an entry nothing stands behind is marked rather than offered. The options block, written as in the example, is sent with every request to every model of this row.",
  },
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
 * service is on. Slack and GitHub are offered either way: the room's own needs ride
 * their credential (kobo#377), whatever agents may be granted. */
export function catalogFor(accessOn: boolean): CatalogEntry[] {
  return accessOn ? CATALOG : CATALOG.map((c) => (c.id === "datadog" ? DATADOG_GATEWAY : c));
}

/** The vendors whose credential is also the room's own (kobo#377). */
export const ROOM_VENDORS = new Set(["slack", "github"]);

export function catalogEntry(id: string): CatalogEntry | undefined {
  return CATALOG.find((c) => c.id === id);
}
