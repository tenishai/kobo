"""Connecting an integration from the admin panel (design 2026-10-18, Arc 2
step 3): one base credential per vendor, held once by the room, served to
agents through the tool gateway.

A connect does three things, in order, each visible on the card:

1. the row and its audit event (`admin_store.put_integration`), state
   `pending`;
2. the credential to the secrets add-on under the room's own path,
   `apps/kobo/integrations/<id>`, synced into the gateway's namespace as the
   Secret `integration-<id>`; the row keeps only the path (`credential_ref`);
3. a pull request on the gateway's config repository adding the vendor's
   `[[target]]` to `targets.toml`, naming the env variable the Secret must
   feed. The gateway serves the target after that pull request merges and
   the gateway rolls, so the row stays `pending` with the link until then.

Disconnect reverses the three. Rows are the truth; the vault and the repo
are projections, and `state_detail` says how far they have caught up.

Datadog through the permission service (kobo#322, `datadog_service_account`)
takes another road: the service account's keys go into the Kubernetes Secret
`bab-datadog` in the permission service's namespace (`bab_namespace`), which
that service reads from env, the service proves it can mint by creating and
deleting a probe key, and the connection is held there with every Datadog
read permission as the ceiling. Nothing reaches the secrets add-on; a lease
is a scoped application key the service mints per agent. The gateway learns
one thing (kobo#327): a `[[target]]` named `datadog-mcp` for Datadog's hosted
MCP server, `auth_method = "service-headers"` (liminal 0.8.0), whose calls
carry each agent's leased key as the `DD_API_KEY` / `DD_APPLICATION_KEY`
headers and whose catalog fetch uses the gateway's own Datadog env.
With the permission service off, Datadog connects as it always has, a key
pair served through the gateway (`api_keys`).

The gateway learns a target two ways (liminal#222, kobo#327). When the room
holds the gateway's admin plane (`clients.liminal_admin`), a Datadog or
token connect PUTs the target there and it serves at once: the row is
Connected with `spec.gateway = "live"` and no pull request is opened; the
disconnect DELETEs it. A gateway whose admin plane predates targets (before
liminal 0.9.0) answers a plain 404, and the connect falls back to the pull
request above; a gateway that refuses the spec fails the row, since a pull
request would carry the same spec. Rows that went by pull request are
removed by pull request.

Slack and GitHub are the two vendors whose credential is also the room's own
(kobo#377; `kobo_api.room_credentials`). Slack's connection is the
app-configuration token pair the room creates agents' Slack apps with
(`slack_config_token`): no permission service row, no gateway target, held
in the room's own Secret and probed by the same dry-run write the boot probe
does, since a pair the room cannot rotate fails twelve hours later. GitHub's
connection is the App the room's own pull requests and agent repositories
use: `_connect_github` holds it in the room's Secret first, and with the
permission service on goes on to the service's Secret, the probe and the two
MCP targets as before (`spec.room_only` marks a row made without it).
Neither has a "service" row any more; both are added, replaced and removed
like every other connection.

Google Cloud (kobo#390, organization first since kobo#405) is the second
cloud through the permission service and the simplest: no role to create, no
trust to federate. The service's one identity (`GET /identity/gcp`, whatever
account the service reports) is granted the role packs once, on the
organization (`gcp_parent`, the AWS hub's shape: the projects under it the
identity can read connect under it, `register_accounts`), or on one project
(`gcp_identity`). The service probes before anything is written (`POST
/probe/gcp`); the row is held there with the read ceiling, and nothing goes
to the gateway. Either scope may also name the room's Vertex AI project: with
the box ticked the row's spec carries `models = {provider: "vertex_ai",
project, location}` (for an organization row the named project, not the
row's), and the probe lists models in that project at the location, refusing
the connect when it cannot; the room's model gateway reads that spec.
`service_account` (act as a narrower account) is no longer on the sheet but
is still accepted for rows that carry it.

Self-hosted models (kobo#390, `kobo_api.self_hosted`) is the provider that is
only models: an OpenAI-compatible endpoint the person runs, named by them,
one row per endpoint. The room probes it itself (`GET /v1/models`, then one
tiny chat completion per named model with the row's options) and connects
as long as one model answers, marking the ones that did not on the row;
the key, when there is one, goes into the room's own Secret so the gateway
filler can read it back. Nothing reaches the permission service or the
tool gateway: nothing is leased. Check models runs the probe again.

Bedrock (kobo#390) rides an AWS account row: a direct role (`aws_role`) or a
member account's spoke (`aws_spoke`) connected with the Bedrock box ticked
carries `models = {provider: "bedrock", region, role_arn}`, and the model
gateway, running as the room's identity, assumes that role itself for every
call. The connect probe is the one the role already gets (the permission
service assumes it once); the permission service has no Bedrock read yet,
so the models are registered on the gateway and the first call an agent
makes is the check with AWS. An organization's hub row never carries it:
Bedrock is per account.
"""

from __future__ import annotations

import base64
import logging
import re
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.orm import Session

from . import (
    admin_store,
    cloud_access,
    gateway_lane,
    model_providers,
    room_credentials,
    room_services,
    self_hosted,
)
from . import bab as bab_mod
from .agent_env import require_our_branch
from .clients import ClientError, Clients, GitHubClient
from .config import Config
from .liminal_admin import TargetNotFound, targets_unsupported
from .models import AdminIntegration

log = logging.getLogger(__name__)

ROOM_APP = "kobo"
TARGETS_FILE = "targets.toml"
# An AWS account is operator config for the gateway's `aws` handler, not a
# target: `[tool_extras."aws.roles".<id>]` in the trust registry.
REGISTRY_FILE = "registry.toml"
AWS_ROLES_TABLE = "aws.roles"
BRANCH_PREFIX = "kobo/integration-"
# An add and a removal of the same target are different pull requests on different
# branches; opening one closes the other if it is still open.
REMOVE_SUFFIX = "-remove"
SECRET_KEY = "TOKEN"
# An account's email and API token sent together as Basic auth (kobo, 2026-11-02): Atlassian's
# MCP server takes a user's API token only this way (a plain Bearer is for its service-account
# keys). The email is a plain identity field; the stored secret is the joined `email:token`
# the gateway's basic scheme sends; the room checks the pair with the vendor's server, the way
# the gateway will, before anything is stored.
BASIC_AUTH = "basic_token"
BASIC_USER_KEY = "email"
TOKEN_AUTHS = ("token", BASIC_AUTH)
BASIC_PROBE_TIMEOUT_S = 15.0
# The gateway's client appends this to a target's address itself; a pasted address that ends in
# it (Atlassian's docs give https://mcp.atlassian.com/v1/mcp) loses it, or the call would 404.
MCP_SUFFIX = "/mcp"
# What a token vendor's target needs beyond the credential, by vendor. Atlassian's server keeps an
# Mcp-Session-Id per client, so each agent gets its own upstream session over the shared credential;
# its v2 endpoint (https://mcp.atlassian.com/v2, the one that serves the Jira and Confluence tools
# to an API token; v1 serves only the Teamwork Graph four) also lists tools that create, edit,
# transition and delete. Those sit behind scopes of their own: the Jira writing four behind the one
# the `atlassian_jira_issues_write` permission opens (gateway_lane.PERMISSION_LANES, 2026-11-03),
# the Confluence writing two behind a scope no lane grants yet, and the generic executors and the
# graph writers behind one no lane will ever grant. The read tools stay under the lane's
# `required_scope`, and the shared credential is the outer wall for all of them.
ATLASSIAN_JIRA_WRITE_SCOPE = "atlassian:jira-write"
ATLASSIAN_CONFLUENCE_WRITE_SCOPE = "atlassian:confluence-write"
ATLASSIAN_NEVER_SCOPE = "atlassian:never"
ATLASSIAN_JIRA_WRITE_TOOLS: tuple[str, ...] = ("createJiraIssue", "editJiraIssue", "addOrEditJiraIssueComment", "transitionJiraIssue")
ATLASSIAN_CONFLUENCE_WRITE_TOOLS: tuple[str, ...] = ("createConfluenceContent", "updateConfluenceContent")
ATLASSIAN_NEVER_TOOLS: tuple[str, ...] = ("executeWrite", "executeDestructive", "addGraphContext", "addTeamworkGraphContext")
ATLASSIAN_WRITE_TOOLS: tuple[str, ...] = ATLASSIAN_JIRA_WRITE_TOOLS + ATLASSIAN_CONFLUENCE_WRITE_TOOLS + ATLASSIAN_NEVER_TOOLS
TARGET_EXTRAS: dict[str, dict[str, Any]] = {
    "atlassian": {"session": "per-persona", "tool_scopes": {
        **dict.fromkeys(ATLASSIAN_JIRA_WRITE_TOOLS, ATLASSIAN_JIRA_WRITE_SCOPE),
        **dict.fromkeys(ATLASSIAN_CONFLUENCE_WRITE_TOOLS, ATLASSIAN_CONFLUENCE_WRITE_SCOPE),
        **dict.fromkeys(ATLASSIAN_NEVER_TOOLS, ATLASSIAN_NEVER_SCOPE),
    }},
}

# How the gateway reaches a vendor (Kyle, 2026-10-25: AWS is a role, not a
# key; GCP is workload identity or a scoped service account; the API-token
# vendors are tokens). `identity` fields are plain values the gateway reads
# from its config; `secrets` are stored in the secrets service and reach the
# gateway as env vars named `secret_env(id, key)`. A kind with no secret
# stores nothing: the gateway's own cloud identity does the work.
AUTH_KINDS = {
    "token": "one bearer token",
    BASIC_AUTH: "an account's email and API token, sent together as Basic auth",
    "api_keys": "one or more named keys",
    "aws_role": "an IAM role the room assumes directly",
    # Hub and spoke (permission service on): one hub role per AWS organization, in its
    # management account, that the room assumes; one spoke role per member account that
    # the hub assumes with an external id. Never an assume across organizations.
    "aws_hub": "an AWS organization: the hub role in its management account",
    "aws_spoke": "an AWS account under a connected organization: the role its hub assumes",
    # Google Cloud (kobo#390): the permission service's own identity, granted the role pack
    # directly on a project (optionally acting as a narrower service account there), or on
    # a folder or organization whose readable projects then connect under it.
    "gcp_identity": "a Google Cloud project the permission service's identity is granted on",
    "gcp_parent": "a Google Cloud organization (or folder): the projects under it the identity can read connect under it, and it may name the Vertex AI project",
    "azure_federated": "an app registration federated to the gateway's identity",
    # Datadog through the permission service (kobo#322): the room holds the service
    # account's API key and a management application key (scoped to
    # service_account_write) for the service, which mints a scoped key per agent.
    "datadog_service_account": "a Datadog service account the permission service mints scoped application keys from",
    # GitHub (kobo#338, kobo#377): the room's GitHub App, held by the room for its own
    # pull requests and agent repositories; with the permission service on it also
    # mints an installation token per agent, narrowed to the granted level and the
    # agent's own repository set (kobo#365: a child integration under this connection,
    # written from the agent's Access tab).
    "github_app": "the room's GitHub App; with the permission service on, agents borrow narrowed installation tokens from it",
    # Self-hosted models (kobo#390): an OpenAI-compatible endpoint the person runs, with an
    # optional key held in the room's own Secret. Only models; nothing is leased.
    self_hosted.AUTH: "an OpenAI-compatible endpoint you run yourself, serving the models you name",
    # Slack (kobo#377): the app-configuration token pair the room creates agents' Slack
    # apps with. The room's own; never leased, never a gateway target.
    "slack_config_token": "the app-configuration token pair the room makes agents' Slack apps with",
}
DATADOG_AUTH = "datadog_service_account"
GITHUB_AUTH = room_credentials.GITHUB_AUTH
SLACK_AUTH = room_credentials.SLACK_AUTH
GITHUB_SECRET = room_credentials.BAB_GITHUB_SECRET
# GitHub's hosted MCP server: the gateway's client appends /mcp. Read lanes send
# X-MCP-Readonly on every request (the server only honours it per request, not
# per session). The server refuses an unauthenticated initialize and wants the
# credential on Authorization, which liminal's catalog_headers_env may not set,
# so the catalog fetch names a catalog token provider the gateway registers
# (liminal 0.9.2): the gateway mints its own App installation token before
# every fetch, and nobody stores or rotates a long-lived token for it.
GITHUB_MCP_ADDRESS = "https://api.githubcopilot.com"
GITHUB_MCP_SCOPE = "github:read"
GITHUB_MCP_CATALOG_PROVIDER = "github-app"
# The second target per GitHub connection (kobo#365): the same server without the
# read-only header, open to an agent granted Review pull requests (`github:review`);
# the tools that create branches, commits, pull requests and issues want the scope
# Open pull requests carries (`github:write`), and merge and delete want one no lane
# ever grants, so the gateway refuses them whatever the token could do.
MCP_WRITE_TARGET_SUFFIX = "-mcp-write"
GITHUB_MCP_WRITE_SCOPE = "github:review"
GITHUB_WRITE_SCOPE = "github:write"
GITHUB_NEVER_SCOPE = "github:never"
GITHUB_WRITE_TOOL_SCOPES: dict[str, str] = {
    **dict.fromkeys((
        "create_branch", "create_or_update_file", "push_files", "create_pull_request", "update_pull_request",
        "update_pull_request_branch", "create_repository", "fork_repository", "create_issue", "update_issue",
        "add_issue_comment",
    ), GITHUB_WRITE_SCOPE),
    **dict.fromkeys(("merge_pull_request", "delete_file"), GITHUB_NEVER_SCOPE),
}
GITHUB_PRIVATE_KEY_FIELD = "PRIVATE_KEY"
_DIGITS = re.compile(r"^\d+$")
# How long a connect waits for the permission service to see the Secret it just wrote
# (a mounted Secret takes the kubelet up to about a minute to appear in the pod).
DATADOG_PROBE_TRIES = 12
DATADOG_PROBE_PAUSE_S = 5.0
_sleep = time.sleep
DATADOG_SECRET = "bab-datadog"
DATADOG_SECRET_KEYS = ("API_KEY", "MGMT_APP_KEY")
DATADOG_DEFAULT_SITE = "datadoghq.com"
DATADOG_SITES = frozenset({
    "datadoghq.com", "us3.datadoghq.com", "us5.datadoghq.com", "datadoghq.eu",
    "ap1.datadoghq.com", "ap2.datadoghq.com", "ddog-gov.com",
})
# A vendor's MCP server is its own target on the gateway, named after the
# integration with this suffix: the gateway's resolver leases the integration
# `datadog` for a call on `datadog-mcp` (liminal 0.8.0, the `-mcp` rule).
MCP_TARGET_SUFFIX = "-mcp"
DATADOG_MCP_SCOPE = "datadog:read"
# Header on the call -> key in the leased credentials dict the permission
# service hands the gateway ({DD_API_KEY, DD_APP_KEY, DD_SITE}); the catalog
# fetch reads the same two headers from the gateway pod's env.
DATADOG_MCP_HEADERS = (("DD_API_KEY", "DD_API_KEY"), ("DD_APPLICATION_KEY", "DD_APP_KEY"))
# `spec.gateway` on a row whose target the admin plane holds live (no pull request).
GATEWAY_LIVE = "live"
_AWS_ACCOUNT_ID = re.compile(r"^\d{12}$")
_AWS_ROLE_ARN = re.compile(r"^arn:aws:iam::\d{12}:role/[\w+=,.@/-]+$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_GUID = re.compile(r"^[0-9a-fA-F-]{32,36}$")
GCP_AUTH = "gcp_identity"
GCP_PARENT_AUTH = "gcp_parent"
GCP_AUTHS = frozenset({GCP_AUTH, GCP_PARENT_AUTH})
# A project id as Google issues them: 6 to 30 characters, lowercase letter first, letters,
# digits and hyphens, no trailing hyphen. A parent is `folders/<number>` or `organizations/<number>`.
_GCP_PROJECT = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_GCP_PARENT = re.compile(r"^(folders|organizations)/\d+$")
# A Vertex AI location: `global` or a region (`us-east5`, `europe-west4`).
_GCP_LOCATION = re.compile(r"^(global|[a-z]+-[a-z]+\d+)$")
# The one model provider a Google Cloud row offers the room (kobo#390), and where its
# models are served from unless the person says otherwise: `global` (owner ruling, kobo#405).
MODELS_PROVIDER_VERTEX = "vertex_ai"
VERTEX_GLOBAL = "global"
VERTEX_DEFAULT_LOCATION = VERTEX_GLOBAL
SELF_HOSTED_AUTH = self_hosted.AUTH
# Bedrock through an AWS account (kobo#390): the kinds that may carry it (per
# account, never the organization's hub) and what a region looks like.
MODELS_PROVIDER_BEDROCK = model_providers.PROVIDER_BEDROCK
BEDROCK_DEFAULT_REGION = cloud_access.BEDROCK_DEFAULT_REGION
BEDROCK_AUTHS = frozenset({"aws_role", "aws_spoke"})
_AWS_REGION = re.compile(r"^[a-z]{2}(-gov)?-[a-z]+-\d$")


def check_auth(auth: str, identity: dict[str, str], secrets: dict[str, str]) -> None:
    """Refuse a connect whose fields do not fit its kind, in one sentence."""
    if auth not in AUTH_KINDS:
        raise ConnectRefused(f"unknown auth kind {auth!r}; one of {', '.join(AUTH_KINDS)}")
    if auth == "token" and not secrets.get(SECRET_KEY, "").strip():
        raise ConnectRefused("an integration needs the vendor's token or key")
    if auth == BASIC_AUTH and not (identity.get(BASIC_USER_KEY, "").strip() and secrets.get(SECRET_KEY, "").strip()):
        raise ConnectRefused("this vendor needs the account's email and its API token")
    if auth == "api_keys" and not any(v.strip() for v in secrets.values()):
        raise ConnectRefused("this vendor needs at least one key")
    if auth == "aws_role" and not _AWS_ROLE_ARN.match(identity.get("role_arn", "").strip()):
        raise ConnectRefused("AWS needs the ARN of a role the room may assume (arn:aws:iam::<account>:role/<name>)")
    if auth == "aws_hub" and not _AWS_ROLE_ARN.match(identity.get("hub_role_arn", "").strip()):
        raise ConnectRefused("an AWS organization needs the ARN of its hub role, in the management account")
    if auth == "aws_spoke":
        if not identity.get("parent", "").strip():
            raise ConnectRefused("an AWS account connects under one of the room's connected organizations")
        if not _AWS_ROLE_ARN.match(identity.get("role_arn", "").strip()):
            raise ConnectRefused("an AWS account needs the ARN of the role its organization's hub assumes")
        if not _AWS_ACCOUNT_ID.match(identity.get("account_id", "").strip()):
            raise ConnectRefused("an AWS account needs its 12-digit account id")
    if auth.startswith("aws_"):
        provider = identity.get("models_provider", "").strip()
        if provider and provider != MODELS_PROVIDER_BEDROCK:
            raise ConnectRefused(f"an AWS account serves models through Bedrock; {provider!r} is not a provider here")
        if provider and auth not in BEDROCK_AUTHS:
            raise ConnectRefused("Bedrock is per account: tick it on an account's role, not on the organization")
        region = identity.get("models_region", "").strip()
        if provider and region and not _AWS_REGION.match(region):
            raise ConnectRefused(f"{region!r} is not an AWS region; one such as {BEDROCK_DEFAULT_REGION}")
    if auth == GCP_AUTH:
        if not _GCP_PROJECT.match(identity.get("project", "").strip()):
            raise ConnectRefused("Google Cloud needs the project's id: 6 to 30 lowercase letters, digits and hyphens, "
                                 "starting with a letter (my-project-1234, not its name or number)")
        sa = identity.get("service_account", "").strip()
        if sa and not _EMAIL.match(sa):
            raise ConnectRefused("the service account to act as must be its email address")
    if auth == GCP_PARENT_AUTH and not _GCP_PARENT.match(identity.get("parent_resource", "").strip()):
        raise ConnectRefused("a Google Cloud organization is named organizations/<number> (a folder, folders/<number>)")
    if auth in GCP_AUTHS:
        provider = identity.get("models_provider", "").strip()
        if provider and provider != MODELS_PROVIDER_VERTEX:
            raise ConnectRefused(f"Google Cloud serves models through Vertex AI; {provider!r} is not a provider here")
        models_project = identity.get("models_project", "").strip()
        if provider and auth == GCP_PARENT_AUTH and not models_project:
            raise ConnectRefused("name the Vertex AI project whose models the room should offer (my-vertex-project)")
        if provider and models_project and not _GCP_PROJECT.match(models_project):
            raise ConnectRefused("the Vertex AI project needs its id: 6 to 30 lowercase letters, digits and hyphens, "
                                 "starting with a letter (my-vertex-project, not its name or number)")
        location = identity.get("models_location", "").strip()
        if provider and location and not _GCP_LOCATION.match(location):
            raise ConnectRefused(f"{location!r} is not a Vertex AI location; global, or a region such as us-east5")
    if auth == "azure_federated" and not (
        _GUID.match(identity.get("client_id", "").strip()) and _GUID.match(identity.get("tenant_id", "").strip())
    ):
        raise ConnectRefused("Azure needs the app registration's client ID and tenant ID")
    if auth == DATADOG_AUTH:
        if not identity.get("service_account_id", "").strip():
            raise ConnectRefused("Datadog needs the id of the service account the room mints keys from")
        if not all(secrets.get(k, "").strip() for k in DATADOG_SECRET_KEYS):
            raise ConnectRefused("Datadog needs the API key and the management application key (scoped to service_account_write)")
        site = identity.get("site", "").strip()
        if site and site not in DATADOG_SITES:
            raise ConnectRefused(f"{site!r} is not a Datadog site; one of {', '.join(sorted(DATADOG_SITES))}")
    if auth == GITHUB_AUTH:
        given = [identity.get("app_id", "").strip(), identity.get("installation_id", "").strip(),
                 secrets.get(GITHUB_PRIVATE_KEY_FIELD, "").strip()]
        if any(given) and not all(given):
            raise ConnectRefused("GitHub needs the app id, the installation id and the private key together, "
                                 "or all three blank to keep the app the room already holds")
        if given[0] and not (_DIGITS.match(given[0]) and _DIGITS.match(given[1])):
            raise ConnectRefused("the GitHub App id and installation id are numbers")
        if given[2] and not given[2].startswith("-----BEGIN"):
            raise ConnectRefused("the GitHub App private key is the PEM text, starting with -----BEGIN")
    if auth == SLACK_AUTH:
        try:
            room_credentials.values_from_form(room_credentials.SLACK_VENDOR, identity, secrets)
        except room_credentials.RoomServiceRefused as exc:
            raise ConnectRefused(str(exc)) from None
    if auth == SELF_HOSTED_AUTH:
        try:
            self_hosted.normalize_base_url(identity.get("base_url", ""))
            self_hosted.parse_models(identity.get("models", ""))
            self_hosted.parse_options(identity.get("options", ""))
        except self_hosted.EndpointRefused as exc:
            raise ConnectRefused(str(exc)) from None


class ConnectRefused(ValueError):
    """A connect the room cannot do yet, with the sentence that says why."""


def credential_path(integration_id: str) -> str:
    return f"apps/{ROOM_APP}/integrations/{integration_id}"


def secret_name(integration_id: str) -> str:
    return f"integration-{integration_id}"


def secret_env(integration_id: str, key: str = SECRET_KEY) -> str:
    """The env var one stored secret reaches the gateway as."""
    return "INTEGRATION_" + re.sub(r"[^A-Za-z0-9]", "_", integration_id).upper() + "_" + re.sub(r"[^A-Za-z0-9]", "_", key).upper()


def token_env(integration_id: str) -> str:
    return secret_env(integration_id, SECRET_KEY)


def mcp_target_name(integration_id: str) -> str:
    """The gateway target that is this integration's MCP server."""
    return f"{integration_id}{MCP_TARGET_SUFFIX}"


def mcp_write_target_name(integration_id: str) -> str:
    """The gateway target that is a GitHub connection's writing MCP server (kobo#365)."""
    return f"{integration_id}{MCP_WRITE_TARGET_SUFFIX}"


def datadog_mcp_address(site: str | None = None) -> str:
    """Datadog's hosted MCP server for a site, without the trailing `/mcp`
    the gateway adds itself."""
    return f"https://mcp.{site or DATADOG_DEFAULT_SITE}/v1"


def _mcp_address(pasted: str, default: str) -> str:
    """A pasted MCP address as the target wants it: whitespace, a trailing
    `/mcp` and a trailing slash gone; the default when nothing was pasted."""
    address = pasted.strip().rstrip("/")
    if address.endswith("/mcp"):
        address = address[: -len("/mcp")].rstrip("/")
    return address or default


def target_spec(row: AdminIntegration) -> dict[str, Any]:
    """The target for a token vendor, as the admin plane takes it (the
    `[[target]]` keys without `name`): address, trust tier, bearer token from
    the env the synced Secret feeds. Scopes are the gateway operator's call
    and stay out of here. Only the `token` kind has a spec the gateway knows
    whole; the other kinds go by pull request (`target_stanza`)."""
    spec = row.spec or {}
    auth = spec.get("auth", "token")
    if auth not in TOKEN_AUTHS:
        raise ValueError(f"no live target spec for the {auth!r} kind")
    out: dict[str, Any] = {
        "transport": "http",
        "address": spec.get("address", ""),
        "trust": spec.get("trust", "untrusted"),
        "auth_method": "bearer",
        "bearer_token_env": token_env(row.id),
    }
    if auth == BASIC_AUTH:
        out["auth_scheme"] = "basic"  # the env holds `email:token`; the gateway encodes it at the wire
    out.update({k: (dict(v) if isinstance(v, dict) else v) for k, v in TARGET_EXTRAS.get(row.vendor, {}).items()})
    # A vendor with a gateway lane is open only to an agent whose grant carries the lane's scope.
    lane = gateway_lane.GATEWAY_LANES.get(row.vendor)
    if lane is not None:
        out["required_scope"] = lane.scopes[0]
    return out


def gateway_address(address: str) -> str:
    """The MCP base the gateway takes for a token vendor: trimmed, no trailing
    slash, and without the `/mcp` the gateway's client appends itself."""
    address = address.strip().rstrip("/")
    if address.endswith(MCP_SUFFIX):
        address = address[: -len(MCP_SUFFIX)].rstrip("/")
    return address


def _probe_mcp(name: str, address: str, authorization: str, what: str) -> None:
    """Ask the vendor's MCP server to initialize with the credential as the
    gateway will send it. 401 or 403 is a refusal of the credential; any other
    answer means the server took it (a protocol complaint still passed the
    door). Raises `ConnectRefused` with the vendor's sentence. The credential
    is never logged."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "kobo", "version": "0"}}}
    try:
        resp = httpx.post(f"{address}{MCP_SUFFIX}", json=body, timeout=BASIC_PROBE_TIMEOUT_S,
                          headers={"Authorization": authorization, "Accept": "application/json, text/event-stream"})
    except httpx.HTTPError as exc:
        raise ConnectRefused(f"{name}'s MCP server at {address} could not be reached from the room "
                             f"({exc.__class__.__name__}); nothing was connected") from exc
    if resp.status_code in (401, 403):
        raise ConnectRefused(f"{name} did not accept {what} (HTTP {resp.status_code}); check it, and "
                             "that API token access to its MCP server is turned on for the organization; nothing was connected")


def probe_basic_mcp(name: str, address: str, user: str, token: str) -> None:
    """A user's API token rides as Basic `email:token` (Atlassian's shape for personal tokens)."""
    pair = base64.b64encode(f"{user}:{token}".encode()).decode()
    _probe_mcp(name, address, f"Basic {pair}", "the email and API token")


def probe_bearer_mcp(name: str, address: str, token: str) -> None:
    """A service account's API key rides as a Bearer (Atlassian's shape for service accounts,
    2026-11-03: a key an admin makes under Directory, Service accounts, with scopes fixed at
    creation; the one credential shape that belongs to no person)."""
    _probe_mcp(name, address, f"Bearer {token}", "the service account API key")


_basic_probe = probe_basic_mcp  # tests swap these for recorders
_bearer_probe = probe_bearer_mcp
# Token vendors whose MCP server is asked before a Bearer key is kept (the others have no
# server to ask, or their own connect path).
PROBED_TOKEN_VENDORS: frozenset[str] = frozenset(TARGET_EXTRAS)


def datadog_target_spec(row: AdminIntegration) -> dict[str, Any]:
    """The target for Datadog's hosted MCP server (kobo#327), as the admin
    plane takes it: untrusted, flagged, one session per persona, and
    `service-headers` auth, so each agent's call carries the application key
    the permission service leased it while the catalog fetch uses the
    gateway's shared key from its env."""
    spec = row.spec or {}
    return {
        "transport": "http",
        "address": spec.get("address", ""),
        "trust": "untrusted",
        "on_untrusted": "flag",
        "identity": "none",
        "session": "per-persona",
        "auth_method": "service-headers",
        "required_scope": DATADOG_MCP_SCOPE,
        "client_name": "liminal-gateway",
        "credential_headers": dict(DATADOG_MCP_HEADERS),
        "catalog_headers_env": dict(DATADOG_MCP_HEADERS),
    }


def github_target_spec(row: AdminIntegration) -> dict[str, Any]:
    """The target for GitHub's hosted MCP server (kobo#338), as the admin
    plane takes it: untrusted, flagged, one session per persona, and
    `service-token` auth, so each agent's call carries the installation token
    the permission service leased it. Read-only on every request; the catalog
    fetch carries an App installation token the gateway mints on demand
    (`catalog_token_provider`, liminal 0.9.2)."""
    spec = row.spec or {}
    return {
        "transport": "http",
        "address": spec.get("address") or GITHUB_MCP_ADDRESS,
        "trust": "untrusted",
        "on_untrusted": "flag",
        "identity": "none",
        "session": "per-persona",
        "auth_method": "service-token",
        "credential_key": "GITHUB_TOKEN",
        "required_scope": GITHUB_MCP_SCOPE,
        "client_name": "liminal-gateway",
        "headers": {"X-MCP-Readonly": "true"},
        "catalog_token_provider": GITHUB_MCP_CATALOG_PROVIDER,
    }


def github_write_target_spec(row: AdminIntegration) -> dict[str, Any]:
    """The second target of a GitHub connection (kobo#365): `github_target_spec`
    without the read-only header, wanting `github:review` (the Review pull
    requests level), with `tool_scopes` sending the creating tools to
    `github:write` (Open pull requests) and merge and delete to a scope no lane
    grants. The leased installation token is what the vendor enforces; the
    scopes are what the gateway enforces before the call leaves."""
    spec = {k: v for k, v in github_target_spec(row).items() if k != "headers"}
    spec["required_scope"] = GITHUB_MCP_WRITE_SCOPE
    spec["tool_scopes"] = dict(GITHUB_WRITE_TOOL_SCOPES)
    return spec


def extra_live_targets(row: AdminIntegration) -> dict[str, dict[str, Any]]:
    """Further targets a row puts on the gateway beside `live_target_spec`'s,
    by name: a GitHub connection's `<id>-mcp-write` (kobo#365); nothing for
    any other kind. Attached and removed with the row."""
    if (row.spec or {}).get("auth") == GITHUB_AUTH:
        return {mcp_write_target_name(row.id): github_write_target_spec(row)}
    return {}


def live_target_spec(row: AdminIntegration) -> dict[str, Any] | None:
    """The spec the admin plane takes for this row, or None for a kind that
    only goes by pull request (AWS is a registry entry, the other key kinds
    write a `[target.auth]` table the gateway's routes do not know)."""
    auth = (row.spec or {}).get("auth", "token")
    if auth == DATADOG_AUTH:
        return datadog_target_spec(row)
    if auth == GITHUB_AUTH:
        return github_target_spec(row)
    if auth in TOKEN_AUTHS:
        return target_spec(row)
    return None


def _render_stanza(row: AdminIntegration, name: str, spec: dict[str, Any]) -> str:
    """One `[[target]]` in TOML from the spec the admin plane takes: the
    room's comment, `name`, the scalar keys in order, then one
    `[target.<key>]` table per object value. The same bytes whichever lane
    carries the target."""
    scalars = [f'{key} = "{value}"\n' for key, value in spec.items() if not isinstance(value, dict)]
    tables = []
    for key, value in spec.items():
        if isinstance(value, dict):
            tables.append(f"[target.{key}]\n")
            tables.extend(f'{k} = "{v}"\n' for k, v in value.items())
    return "".join([
        f"\n# {row.name}: connected from the room\'s admin panel by {row.created_by}.\n",
        "[[target]]\n",
        f'name = "{name}"\n',
        *scalars,
        *tables,
    ])


def target_stanza(row: AdminIntegration) -> str:
    """The `[[target]]` the gateway needs for this vendor: `target_spec`
    rendered for a token; every other kind names its method, its plain
    fields, and an env name per stored secret, under the target's own auth
    table."""
    spec = row.spec or {}
    auth = spec.get("auth", "token")
    if auth in TOKEN_AUTHS:
        return _render_stanza(row, row.id, target_spec(row))
    lines = [
        _render_stanza(row, row.id, {
            "transport": "http", "address": spec.get("address", ""), "trust": spec.get("trust", "untrusted"), "auth_method": auth,
        }),
        "[target.auth]\n",
    ]
    for key, value in sorted((spec.get("identity") or {}).items()):
        lines.append(f'{key} = "{value}"\n')
    for key in sorted(spec.get("secret_keys") or []):
        lines.append(f'{key.lower()}_env = "{secret_env(row.id, key)}"\n')
    return "".join(lines)


def datadog_target_stanza(row: AdminIntegration) -> str:
    """`datadog_target_spec` as the `[[target]]` the pull request writes."""
    return _render_stanza(row, mcp_target_name(row.id), datadog_target_spec(row))


def github_target_stanza(row: AdminIntegration) -> str:
    """`github_target_spec` and `github_write_target_spec` as the two
    `[[target]]` stanzas the pull request writes (kobo#365)."""
    return (_render_stanza(row, mcp_target_name(row.id), github_target_spec(row))
            + _render_stanza(row, mcp_write_target_name(row.id), github_write_target_spec(row)))


MCP_AUTHS = frozenset({DATADOG_AUTH, GITHUB_AUTH})


def aws_roles_stanza(row: AdminIntegration) -> str:
    """The trust-registry table the gateway's `aws` handler reads for this
    account: the role to assume and the audience its trust policy expects."""
    identity = (row.spec or {}).get("identity") or {}
    lines = [
        f"\n# {row.name}: connected from the room\'s admin panel by {row.created_by}.\n",
        f'[tool_extras."{AWS_ROLES_TABLE}".{row.id}]\n',
        f'role_arn = "{identity.get("role_arn", "")}"\n',
    ]
    if identity.get("audience"):
        lines.append(f'audience = "{identity["audience"]}"\n')
    return "".join(lines)


def _without_aws_stanza(text: str, integration_id: str) -> str:
    pattern = re.compile(
        r"\n?#[^\n]*connected from the room[^\n]*\n\[tool_extras\.\"" + re.escape(AWS_ROLES_TABLE) + r"\"\."
        + re.escape(integration_id)
        + r"\]\n(?:[a-z_]+ = [^\n]*\n)*",
    )
    return pattern.sub("", text)


def _without_stanza(text: str, target_name: str) -> str:
    """`targets.toml` with every stanza of this target name gone: the comment
    lines right above it, its keys and its `[target.<table>]` sub-tables.
    Whoever wrote it: a stanza an operator added by hand under the same name
    would otherwise sit next to the room's, and the gateway refuses a
    duplicate target name at startup."""
    pattern = re.compile(
        r"\n?(?:#[^\n]*\n)*\[\[target\]\]\nname = \""
        + re.escape(target_name)
        + r"\"\n(?:(?:[A-Za-z0-9_-]+ = [^\n]*|\[target\.[a-z_]+\])\n)*",
    )
    return pattern.sub("", text)


def gateway_target_name(row: AdminIntegration) -> str:
    """What the gateway calls this row's target: the MCP name for a Datadog
    connection through the permission service, the row id otherwise."""
    return mcp_target_name(row.id) if (row.spec or {}).get("auth") in MCP_AUTHS else row.id


def _admin(clients: Clients | None):
    return getattr(clients, "liminal_admin", None) if clients is not None else None


def apply_target_live(clients: Clients | None, row: AdminIntegration, *, remove: bool) -> str | None:
    """Put this row's target on the gateway through its admin plane, or take
    it off. None when no admin plane is wired, or the kind has no live spec;
    otherwise the sentence for the row. The gateway's refusal (`ClientError`,
    including the admin plane lacking target routes) is the caller's to
    read; a target the gateway no longer serves counts as removed."""
    admin = _admin(clients)
    spec = live_target_spec(row) if admin is not None else None
    if spec is None:
        return None
    name = gateway_target_name(row)
    extras = extra_live_targets(row)
    if remove:
        for target in (name, *extras):
            try:
                admin.delete_target(target)
            except TargetNotFound:
                pass
        return "removed from the gateway"
    states = []
    for target, target_spec_ in ((name, spec), *extras.items()):
        out = admin.put_target(target, target_spec_) or {}
        status = str(out.get("status") or "ready")
        states.append(f"the {target} target is ready" if status == "ready" else f"the {target} target is degraded: {status}")
    return "live on the gateway: " + "; ".join(states)


PR_SENTENCE = "; pull request open on the gateway:"
PR_FALLBACK_SENTENCE = "; the gateway admin plane does not take targets yet, so a pull request is open on the gateway instead:"


def _target_live_or_fallback(
    session: Session, row: AdminIntegration, clients: Clients | None, detail: str,
) -> tuple[str | None, str | None]:
    """Try the live lane for a connect. `(sentence, None)` when the gateway
    took the target; `(None, lead-in)` when the pull request is the way (no
    admin plane, or one without target routes: the lead-in says which);
    `(None, None)` when the gateway refused the spec, the row already marked
    Failed, since a pull request would carry the same spec."""
    try:
        live = apply_target_live(clients, row, remove=False)
    except ClientError as exc:
        if targets_unsupported(exc):
            return None, PR_FALLBACK_SENTENCE
        admin_store.set_integration_state(
            session, row.id, state=admin_store.STATE_FAILED, detail=f"{detail}; the gateway refused the target: {exc}",
        )
        return None, None
    return (live, None) if live else (None, PR_SENTENCE)


def _gateway_branch_pr(
    github: GitHubClient, config: Config, *, branch: str, title: str, body: str, edit,
    files: frozenset[str] = frozenset({TARGETS_FILE, REGISTRY_FILE}), supersedes: str | None = None,
) -> str | None:
    """Clone the gateway repo, let `edit(workdir) -> bool` change it, and
    open (or find) the pull request on `branch`. None when no repository is
    configured or the edit changed nothing (`gateway_repo_set` tells the two
    apart for the sentence). `supersedes` names the branch of the opposite
    intent (an add's removal, a removal's add): a pull request still open
    there is closed first, so the row never links to one that would undo it.
    `files` are the only paths the room's edit writes, for the ownership
    check before a stale branch is deleted."""
    repo = config.gateway_config_repo
    if not repo:
        return None
    if supersedes:
        stale = github.find_pr(repo, branch=supersedes)
        if stale is not None:
            github.close_pr(repo, stale, f"Superseded by the room's later change on `{branch}`.")
    existing = github.find_pr(repo, branch=branch)
    if existing is not None:
        return existing
    with tempfile.TemporaryDirectory(prefix="kobo-gateway-") as tmp:
        workdir = Path(tmp) / "gateway"
        github.clone(repo, workdir)
        if not edit(workdir):
            return None
        if github.branch_exists(repo, branch):
            require_our_branch(github, repo, branch, files)
            github.delete_branch(repo, branch)
        # open_pr alone: it checks the branch out, commits the edit and pushes THAT
        # branch. `commit_push` would commit and push the checked-out default branch
        # first, which is how a Datadog stanza once landed on the gateway's main
        # twice and took the gateway down (kobo 0.3.17).
        return github.open_pr(workdir, repo=repo, branch=branch, title=title, body=body)


def _gateway_pr(
    github: GitHubClient, config: Config, row: AdminIntegration, *, remove: bool, actor: str
) -> str | None:
    """Open (or find) the pull request that adds or removes this vendor's
    target. None when no repository is configured or nothing changed."""
    verb = "remove" if remove else "add"

    aws = (row.spec or {}).get("auth") == "aws_role"
    datadog = (row.spec or {}).get("auth") == DATADOG_AUTH
    github_app = (row.spec or {}).get("auth") == GITHUB_AUTH
    target_name = gateway_target_name(row)

    def edit(workdir: Path) -> bool:
        path = workdir / (REGISTRY_FILE if aws else TARGETS_FILE)
        current = path.read_text() if path.exists() else ""
        if aws:
            stripped = _without_aws_stanza(current, row.id)
            updated = stripped if remove else stripped.rstrip("\n") + "\n" + aws_roles_stanza(row)
        else:
            stripped = _without_stanza(current, target_name)
            if github_app:
                stripped = _without_stanza(stripped, mcp_write_target_name(row.id))
            stanza = datadog_target_stanza(row) if datadog else github_target_stanza(row) if github_app else target_stanza(row)
            updated = stripped if remove else stripped.rstrip("\n") + "\n" + stanza
        if updated == current:
            return False
        path.write_text(updated)
        return True

    if aws:
        what = "account"
        added = (
            f"Adds the `{row.id}` account to `[tool_extras.\"{AWS_ROLES_TABLE}\"]` in the trust registry. "
            f"The gateway\'s `aws` handler assumes the role with its own identity, one session per agent, "
            f"once a lane grants `{row.id}:read`. No secret is involved. Serves after this merges and the gateway rolls."
        )
    elif datadog:
        what = "target"
        added = (
            f"Adds the `{target_name}` target: Datadog\'s hosted MCP server at {(row.spec or {}).get('address', '')}. "
            f"Each agent\'s calls carry its own application key leased from the permission service "
            f"(`[target.credential_headers]`); the catalog fetch uses the gateway\'s shared Datadog key from its env "
            f"(`[target.catalog_headers_env]`, DD_API_KEY / DD_APP_KEY). Needs the gateway on liminal 0.8.0 or later. "
            f"Serves after this merges and the gateway rolls."
        )
    elif github_app:
        what = "targets"
        added = (
            f"Adds the `{target_name}` and `{mcp_write_target_name(row.id)}` targets: GitHub\'s hosted MCP server at "
            f"{(row.spec or {}).get('address') or GITHUB_MCP_ADDRESS}. Each agent\'s calls carry its own installation "
            f"token leased from the permission service (`service-token`, `GITHUB_TOKEN`), narrowed to the agent\'s "
            f"repository set. `{target_name}` is read-only on every request (`github:read`); "
            f"`{mcp_write_target_name(row.id)}` wants `{GITHUB_MCP_WRITE_SCOPE}`, sends the branch-, commit-, pull-request- "
            f"and issue-creating tools to `{GITHUB_WRITE_SCOPE}` and merge and delete to `{GITHUB_NEVER_SCOPE}`, which no "
            f"lane grants. The catalog fetch carries an installation token the gateway mints from its own App "
            f"(`catalog_token_provider = \"{GITHUB_MCP_CATALOG_PROVIDER}\"`). Needs the gateway on liminal 0.9.2 or later. "
            f"Serves after this merges and the gateway rolls."
        )
    else:
        what = "target"
        added = (
            f"Adds the `{row.id}` target. Its credential is the Secret `{secret_name(row.id)}` "
            f"in the `{config.gateway_namespace or '<gateway namespace>'}` namespace, synced by the "
            f"secrets add-on; the gateway\'s deployment must carry it as the env variable "
            f"`{token_env(row.id)}` (key `{SECRET_KEY}`). The target serves after this merges and "
            f"the gateway rolls."
        )
    removed = (f"Removes the `{target_name}` and `{mcp_write_target_name(row.id)}` targets." if github_app
               else f"Removes the `{row.id if aws else target_name}` {what}.")
    body = f"Opened by the room\'s admin panel for {actor}.\n\n" + (removed if remove else added)
    add_branch, remove_branch = f"{BRANCH_PREFIX}{row.id}", f"{BRANCH_PREFIX}{row.id}{REMOVE_SUFFIX}"
    return _gateway_branch_pr(
        github, config, branch=remove_branch if remove else add_branch, supersedes=add_branch if remove else remove_branch,
        title=f"kobo: {verb} the {row.name} {what}", body=body, edit=edit,
    )

def probe_aws_role(clients: Clients | None, role_arn: str, audience: str) -> dict[str, str] | None:
    """Ask the gateway to assume the role once. None when no admin plane is
    wired (the room cannot check); `ConnectRefused` with AWS\'s sentence when
    it is and AWS said no, so nothing is written for a role that does not work."""
    admin = getattr(clients, "liminal_admin", None) if clients is not None else None
    if admin is None:
        return None
    try:
        return admin.aws_probe(role_arn, audience)
    except ClientError as exc:
        raise ConnectRefused(f"{exc}; fix the role\'s trust policy and connect again") from exc


def _bab(clients: Clients | None):
    return getattr(clients, "bab", None) if clients is not None else None


def datadog_secret_literals(identity: dict[str, str], secrets: dict[str, str]) -> dict[str, str]:
    """The four env variables the permission service's Datadog minter reads, as the
    Secret `bab-datadog` carries them."""
    return {
        "DD_SITE": identity.get("site") or DATADOG_DEFAULT_SITE,
        "DD_API_KEY": secrets["API_KEY"],
        "DD_MGMT_APP_KEY": secrets["MGMT_APP_KEY"],
        "DD_SERVICE_ACCOUNT_ID": identity["service_account_id"],
    }


def github_secret_literals(identity: dict[str, str], secrets: dict[str, str]) -> dict[str, str]:
    """The env variables the permission service's GitHub minter reads, as the
    Secret `bab-github` carries them."""
    out = {
        "GITHUB_APP_ID": identity["app_id"],
        "GITHUB_APP_INSTALLATION_ID": identity["installation_id"],
        "GITHUB_APP_PRIVATE_KEY": secrets[GITHUB_PRIVATE_KEY_FIELD],
    }
    if identity.get("api_url"):
        out["GITHUB_API_URL"] = identity["api_url"]
    return out


def _attach_mcp_target(
    session: Session, clients: Clients | None, config: Config, row: AdminIntegration, *,
    detail: str, waiting: bool, actor: str, label: str,
) -> AdminIntegration:
    """The gateway's half of a connection through the permission service
    (kobo#327): the vendor's `<id>-mcp` target. Live through the admin plane
    when the room holds one; a pull request on the gateway's config repository
    otherwise. Independent of the Secret restart, so a waiting row gets it too."""
    live, fallback = _target_live_or_fallback(session, row, clients, detail)
    if live is None and fallback is None:
        return row  # the gateway refused the spec; the row says so
    if live:
        row.spec = {**dict(row.spec or {}), "gateway": GATEWAY_LIVE}
        admin_store.set_integration_state(
            session, row.id, state=admin_store.STATE_PENDING if waiting else admin_store.STATE_APPLIED, detail=f"{detail}; {live}",
        )
        return row
    github = getattr(clients, "github", None) if clients is not None else None
    pr: str | None = None
    if github is not None and config.gateway_config_repo:
        try:
            pr = _gateway_pr(github, config, row, remove=False, actor=actor)
        except (ClientError, OSError) as exc:
            admin_store.set_integration_state(
                session, row.id, state=admin_store.STATE_FAILED, detail=f"{detail}; the gateway pull request failed: {exc}",
            )
            return row
    if pr:
        detail += f"{fallback} {pr}; agents reach {label} after it merges and the gateway rolls"
        row.spec = {**dict(row.spec or {}), "pull_request": pr}
        state = admin_store.STATE_PENDING
    elif config.gateway_config_repo:
        detail += f"; the gateway already carries the {mcp_target_name(row.id)} target"
        state = admin_store.STATE_PENDING if waiting else admin_store.STATE_APPLIED
    else:
        detail += f"; no gateway config repository is set, so add the {mcp_target_name(row.id)} target by hand"
        state = admin_store.STATE_PENDING if waiting else admin_store.STATE_APPLIED
    admin_store.set_integration_state(session, row.id, state=state, detail=detail)
    return row


def _probe_through_secret(bab, kubectl, config: Config, *, secret: str, literals: dict[str, str], probe, undo_hint: str) -> bool:
    """Write the permission service's Secret and probe until the service has read
    it. True when the row must wait (the service has not read the Secret yet);
    any other refusal takes the Secret back and raises with the vendor's sentence."""
    where = f"the Secret {secret} in the {config.bab_namespace} namespace, read by the permission service"
    try:
        kubectl.apply_secret(config.bab_namespace, secret, literals)
    except ClientError as exc:
        raise ConnectRefused(f"{where} could not be written: {exc}; nothing was connected") from exc
    for attempt in range(DATADOG_PROBE_TRIES):
        try:
            probe()
            return False
        except ClientError as exc:
            # The permission service reads the Secret from a mount, and the kubelet takes
            # up to a minute to project a Secret just written; until then the probe answers
            # "not configured" (or, on an older service, "cannot mint for ... yet").
            text = str(exc).lower()
            if "not configured" in text or "cannot mint for" in text:
                if attempt < DATADOG_PROBE_TRIES - 1:
                    _sleep(DATADOG_PROBE_PAUSE_S)
                continue
            try:
                kubectl.delete_secret(config.bab_namespace, secret)
            except ClientError as undo:
                log.warning("connect refused and %s was left behind: %s", secret, undo)
            raise ConnectRefused(f"{exc}; {undo_hint}") from exc
    return True


def _connect_github(
    session: Session, clients: Clients | None, config: Config, bab, *, vendor: str, name: str, base_access: str,
    address: str, trust: str, actor: str, identity: dict[str, str], secrets: dict[str, str],
) -> AdminIntegration:
    """GitHub (kobo#338, kobo#377): the one row for the room's GitHub App. The
    pasted app (or, with the fields blank, the app the room already holds: the
    row being replaced, or the deployment config's) is held in the room's own
    Secret as this connection's credential, where the room's pull requests and
    agent repositories read it (`kobo_api.room_credentials`). With the
    permission service on, it is then written to the service's Secret and
    probed, and the `github-mcp` and `github-mcp-write` targets go on the
    gateway (kobo#365); a refusal there takes the room's copy back too, so a
    bad app connects nothing. Without the service the row is `room_only`:
    the room's own, nothing for agents to ask for. The connection carries no
    repository list of its own: each agent's set is a child integration under
    it, written from the agent's Access tab (`kobo_api.github_access`)."""
    room = room_credentials.services(clients)
    if room is None:
        raise ConnectRefused(room_credentials.NOWHERE_TO_STORE)
    kubectl = getattr(clients, "kubectl", None) if clients is not None else None
    if bab is not None and kubectl is None:
        raise ConnectRefused("this room cannot write the permission service's Secret; nothing was connected")
    if not identity.get("app_id"):
        app = room_credentials.github_app(clients, config)
        if app is None:
            raise ConnectRefused("paste the app id, the installation id and the private key: "
                                 "this room holds no GitHub App yet")
        try:
            pem = app.pem()
        except ClientError as exc:
            raise ConnectRefused(f"the room's GitHub App key could not be read: {exc}") from exc
        identity["app_id"], identity["installation_id"] = str(app.app_id), str(app.installation_id)
        secrets[GITHUB_PRIVATE_KEY_FIELD] = pem
    identity.pop("repositories", None)
    try:
        values = room_credentials.values_from_form(room_credentials.GITHUB_VENDOR, identity, secrets)
    except room_credentials.RoomServiceRefused as exc:
        raise ConnectRefused(str(exc)) from None
    inst = identity["installation_id"]
    held_before = room.read(room_credentials.store.GITHUB, fresh=True)
    try:
        room_credentials.store_values(clients, room_credentials.GITHUB_VENDOR, values, actor=actor)
    except ClientError as exc:
        raise ConnectRefused(f"the room could not hold the app in its own secret store: {exc}; nothing was connected") from exc

    def take_back() -> None:
        """A refusal downstream: the room keeps what it held before, or nothing."""
        try:
            if held_before is not None:
                room.put(room_credentials.store.GITHUB, held_before, actor=held_before.get("updated_by") or actor)
            else:
                room.delete(room_credentials.store.GITHUB)
        except ClientError as undo:
            log.warning("github connect refused and the room's copy was left behind: %s", undo)

    ref = room_credentials.credential_ref(room, room_credentials.GITHUB_VENDOR)
    base_spec: dict[str, Any] = {
        "address": _mcp_address(address, GITHUB_MCP_ADDRESS),
        "trust": trust if trust in ("trusted", "untrusted") else "untrusted",
        "auth": GITHUB_AUTH,
        "secret_keys": [],
    }
    if bab is None:
        spec = {**base_spec, "identity": {"app_id": identity["app_id"], "installation_id": inst, "source": "room"},
                "room_only": True}
        row = admin_store.put_integration(
            session, vendor=vendor, name=name, base_access=base_access, spec=spec, credential_ref=ref, actor=actor,
        )
        admin_store.set_integration_state(
            session, row.id, state=admin_store.STATE_APPLIED,
            detail="held by the room for its own pull requests and agent repositories; with the permission "
                   "service off, agents cannot be granted GitHub",
        )
        return row
    where = f"the Secret {GITHUB_SECRET} in the {config.bab_namespace} namespace, read by the permission service"
    probed: dict[str, Any] = {}

    def probe() -> None:
        probed.update(bab.probe_github(inst, actor) or {})

    try:
        waiting = _probe_through_secret(
            bab, kubectl, config, secret=GITHUB_SECRET, literals=github_secret_literals(identity, secrets), probe=probe,
            undo_hint="check the app id, the installation id and the private key and connect again",
        )
    except ConnectRefused:
        take_back()
        raise
    account = str(probed.get("account") or "")
    spec = {
        **base_spec,
        "identity": {"app_id": identity["app_id"], "installation_id": inst, "source": "room",
                     **({"account": account} if account else {})},
    }
    row = admin_store.put_integration(
        session, vendor=vendor, name=name, base_access=base_access, spec=spec, credential_ref=ref, actor=actor,
    )
    if waiting:
        detail = (f"the permission service has not read {where} yet; it does within a minute or two, "
                  "so remove this row and add GitHub again to check the app")
    else:
        detail = (f"GitHub agreed: the permission service read installation {inst}"
                  + (f" on {account}" if account else "") + f" and minted a probe token that sees "
                  f"{probed.get('repositories', 0)} repositories; the app is in {where}")
    spec_for_bab = {"installation_id": inst, "name": name, **({"account": account} if account else {})}
    try:
        # Every github permission, not the read tier alone: the levels (Read, Review
        # pull requests, Open pull requests) are each agent's to ask on its own
        # repository set, and Review's to decide (kobo#365).
        bab.put_integration(row.id, vendor, spec_for_bab, bab_mod.all_permissions(bab, vendor, actor), actor)
    except ClientError as exc:
        admin_store.set_integration_state(session, row.id, state=admin_store.STATE_FAILED, detail=f"{detail}; the permission service refused: {exc}")
        return row
    detail += ("; connected in the permission service: each agent picks its repositories on its Access tab and asks "
               "for a level (Read, Review pull requests, Open pull requests); the room's own pull requests use the same app")
    return _attach_mcp_target(session, clients, config, row, detail=detail, waiting=waiting, actor=actor,
                              label="GitHub\'s MCP server (the read and the write targets)")


def _connect_slack(
    session: Session, clients: Clients | None, *, vendor: str, name: str, base_access: str, trust: str, actor: str,
    identity: dict[str, str], secrets: dict[str, str],
) -> AdminIntegration:
    """Slack (kobo#377): the app-configuration token pair, held in the room's own
    Secret as this connection's credential, where the room reads it to make and
    remove agents' Slack apps and writes each rotation back. The probe is the
    boot probe's: a server dry-run of the write, so a room that could not keep
    a rotated pair refuses now rather than twelve hours on. No permission
    service row and no gateway target: agents never lease Slack."""
    room = room_credentials.services(clients)
    if room is None:
        raise ConnectRefused(room_credentials.NOWHERE_TO_STORE)
    try:
        values = room_credentials.values_from_form(room_credentials.SLACK_VENDOR, identity, secrets)
    except room_credentials.RoomServiceRefused as exc:
        raise ConnectRefused(str(exc)) from None
    held_before = room.read(room_credentials.store.SLACK, fresh=True)
    try:
        room_credentials.store_values(clients, room_credentials.SLACK_VENDOR, values, actor=actor)
    except ClientError as exc:
        raise ConnectRefused(f"the room could not hold the pair in its own secret store: {exc}; nothing was connected") from exc
    try:
        room_credentials.check_slack_writable(clients)
    except ClientError as exc:
        try:
            if held_before is not None:
                room.put(room_credentials.store.SLACK, held_before, actor=held_before.get("updated_by") or actor)
            else:
                room.delete(room_credentials.store.SLACK)
        except ClientError as undo:
            log.warning("slack connect refused and the room's copy was left behind: %s", undo)
        raise ConnectRefused(f"the pair was stored but the room could not write it back the way a rotation will: {exc}; "
                             "nothing was connected") from exc
    spec: dict[str, Any] = {
        "address": "", "trust": trust if trust in ("trusted", "untrusted") else "untrusted", "auth": SLACK_AUTH,
        "identity": {}, "secret_keys": sorted(secrets), "room_only": True,
    }
    row = admin_store.put_integration(
        session, vendor=vendor, name=name, base_access=base_access, spec=spec,
        credential_ref=room_credentials.credential_ref(room, room_credentials.SLACK_VENDOR), actor=actor,
        integration_id=room_credentials.SLACK_VENDOR,
    )
    admin_store.set_integration_state(
        session, row.id, state=admin_store.STATE_APPLIED,
        detail="held by the room; agents' Slack apps are made with it, and the pair rotates on its own",
    )
    return row


def _connect_datadog(
    session: Session, clients: Clients | None, config: Config, bab, *, vendor: str, name: str, base_access: str,
    address: str, trust: str, actor: str, identity: dict[str, str], secrets: dict[str, str],
) -> AdminIntegration:
    """Datadog through the permission service (kobo#322). The Secret is written
    first because the service reads it from env: a probe that answers "not
    configured" means the service has not restarted onto it yet, so the row waits
    and says so; any other refusal takes the Secret back and connects nothing."""
    if bab is None:
        raise ConnectRefused("Datadog through a service account needs the permission service; turn Access on first, "
                             "or connect Datadog with its key pair for the gateway")
    kubectl = getattr(clients, "kubectl", None) if clients is not None else None
    if kubectl is None:
        raise ConnectRefused("this room cannot write the permission service's Secret; nothing was connected")
    identity["site"] = identity.get("site") or DATADOG_DEFAULT_SITE
    sa = identity["service_account_id"]
    where = f"the Secret {DATADOG_SECRET} in the {config.bab_namespace} namespace, read by the permission service"
    try:
        kubectl.apply_secret(config.bab_namespace, DATADOG_SECRET, datadog_secret_literals(identity, secrets))
    except ClientError as exc:
        raise ConnectRefused(f"{where} could not be written: {exc}; nothing was connected") from exc
    waiting = False
    for attempt in range(DATADOG_PROBE_TRIES):
        try:
            bab.probe_datadog(sa, actor)
            waiting = False
            break
        except ClientError as exc:
            # The permission service reads the Secret from a mount, and the kubelet takes
            # up to a minute to project a Secret just written; until then the probe answers
            # "not configured" (or, on an older service, "cannot mint for datadog yet").
            # That is a wait, not a refused key: try again for a while, then let the row
            # say so. Any other refusal takes the Secret back and connects nothing.
            text = str(exc).lower()
            if "not configured" in text or "cannot mint for" in text:
                waiting = True
                if attempt < DATADOG_PROBE_TRIES - 1:
                    _sleep(DATADOG_PROBE_PAUSE_S)
                continue
            try:
                kubectl.delete_secret(config.bab_namespace, DATADOG_SECRET)
            except ClientError as undo:
                log.warning("datadog connect refused and %s was left behind: %s", DATADOG_SECRET, undo)
            raise ConnectRefused(f"{exc}; check the service account id and both keys and connect again") from exc
    spec: dict[str, Any] = {
        "address": _mcp_address(address, datadog_mcp_address(identity["site"])),
        "trust": trust if trust in ("trusted", "untrusted") else "untrusted",
        "auth": DATADOG_AUTH,
        "identity": {"service_account_id": sa, "site": identity["site"]},
        "secret_keys": sorted(secrets),
    }
    row = admin_store.put_integration(
        session, vendor=vendor, name=name, base_access=base_access, spec=spec, credential_ref=None, actor=actor,
    )
    if waiting:
        detail = (f"the permission service has not read {where} yet; it does within a minute or two, "
                  "so remove this row and add Datadog again to check the keys")
    else:
        detail = f"Datadog agreed: the permission service minted and removed a probe key for service account {sa}; the keys are in {where}"
    spec_for_bab = {"site": identity["site"], "service_account_id": sa, "name": name}
    try:
        bab.put_integration(row.id, vendor, spec_for_bab, bab_mod.read_permissions(bab, vendor, actor), actor)
    except ClientError as exc:
        admin_store.set_integration_state(session, row.id, state=admin_store.STATE_FAILED, detail=f"{detail}; the permission service refused: {exc}")
        return row
    detail += "; connected in the permission service, agents may ask for its read permissions"
    return _attach_mcp_target(session, clients, config, row, detail=detail, waiting=waiting, actor=actor, label="Datadog\'s MCP server")


_PARENT_LABEL = {"aws": "AWS organizations", "gcp": "Google Cloud folders or organizations"}


def _org_or_refused(bab, parent: str, actor: str, vendor: str | None = "aws") -> dict:
    """The connected organization (AWS) or folder or organization (Google Cloud) an
    account or project hangs under. `vendor=None` takes whichever vendor's it is."""
    for i in bab.integrations(actor):
        if i["integration"] == parent and i.get("kind") == "org" and (vendor is None or i.get("vendor") == vendor):
            return i
    raise ConnectRefused(f"{parent!r} is not one of the room's connected {_PARENT_LABEL.get(vendor or '', 'organizations')}")


def _org_hub(bab, parent: str, actor: str) -> str:
    """The hub role ARN of a connected organization: bab keeps it in the row's spec, which
    its listing does not return, so the console asks the probe-free way: the org row's
    accounts call fails without it, and the setup page needs it for the spoke's trust."""
    _org_or_refused(bab, parent, actor)
    return str(bab.org_hub_role_arn(parent, actor))


def aws_setup(clients: Clients | None, *, integration_id: str, packs: list[str] | None, actor: str = "operator@room",
              mode: str = "role", parent: str | None = None, account_id: str | None = None,
              bedrock: bool = False) -> dict[str, Any]:
    """Everything the Connect AWS page shows: who the role must trust, which
    packs exist, and the three ways to create the role, each pre-filled. With
    the permission service on, the principal is bab (it mints the leases); the
    gateway otherwise.

    Three modes. `role`: one flat role the room assumes (the gateway path, or
    bab without an organization). `org`: the hub role in an organization's
    management account, trusting the room's identity. `account`: a member
    account's spoke role under a connected organization, trusting the hub
    with this account's external id.

    With `bedrock` (kobo#390, `role` and `account` only) the snippets carry the
    Bedrock permissions block too: appended to the Terraform, and as a policy
    of its own for CloudFormation and by hand. The trust policy is unchanged.

    `models`: a role that serves Bedrock models and nothing else (owner ruling
    2026-11-02, the account that holds the room's Bedrock role carried the
    full read ceiling and was offered to agents). Its Terraform is a plain
    role with the trust policy and the Bedrock permissions, there is no
    CloudFormation template, and Bedrock is always on."""
    bab = _bab(clients)
    if mode == "models":
        bedrock = True
    bedrock_block = {
        "bedrock_default_region": BEDROCK_DEFAULT_REGION,
        "bedrock_policy": cloud_access.bedrock_permissions_policy() if bedrock and mode != "org" else None,
    }
    bedrock_tf = "\n" + cloud_access.bedrock_terraform_snippet() if bedrock and mode != "org" else ""
    if mode == "account":
        if bab is None:
            raise ConnectRefused("accounts under an organization need the permission service")
        if not parent or not account_id or not _AWS_ACCOUNT_ID.match(account_id):
            raise ConnectRefused("an account set-up needs the organization and the 12-digit account id")
        hub = _org_hub(bab, parent, actor)
        external_id = bab.external_id_for_org(parent)
        chosen = cloud_access.chosen_packs(packs)
        return {
            "version": cloud_access.version(), "mode": "account", "principal": "access",
            "role_name": cloud_access.ROLE_NAME, "audience": cloud_access.AUDIENCE,
            "parent": parent, "account_id": account_id, "hub_role_arn": hub, "external_id": external_id,
            "identity": None, "identity_note": None,
            "packs": cloud_access.aws_packs(), "chosen": chosen,
            "terraform": cloud_access.spoke_terraform_snippet(hub, external_id, integration_id, chosen) + bedrock_tf,
            "cloudformation": {"template": cloud_access.cloudformation_template(),
                               "parameters": {**cloud_access.cloudformation_parameters("", integration_id, chosen),
                                              "HubRoleArn": hub, "ExternalId": external_id}},
            "trust_policy": cloud_access.spoke_trust_policy(hub, external_id),
            "checked": True,
            **bedrock_block,
        }
    admin = getattr(clients, "liminal_admin", None) if clients is not None else None
    identity: dict[str, str] | None = None
    identity_note: str | None = None
    principal = "access" if bab is not None else "gateway"
    if bab is not None:
        try:
            identity = bab.aws_identity(actor)
        except ClientError as exc:
            identity_note = str(exc)
    elif admin is None:
        identity_note = "the gateway\'s admin plane is not wired, so the room cannot read its identity; fill the subject in by hand"
    else:
        try:
            identity = admin.aws_identity(cloud_access.AUDIENCE)
        except ClientError as exc:
            identity_note = str(exc)
    subject = (identity or {}).get("subject", "")
    chosen = cloud_access.chosen_packs(packs)
    if mode == "org":
        if bab is None:
            raise ConnectRefused("organizations need the permission service")
        # The member accounts' half (#275): one external id for the whole organization and
        # one spoke template, so the role is deployed once (a StackSet) and the hub then
        # finds every account that has it. The hub ARN is known once the organization is
        # connected; before that the snippets carry a placeholder the owner fills in.
        external_id = bab.external_id_for_org(integration_id)
        try:
            hub = _org_hub(bab, integration_id, actor)
        except (ConnectRefused, ClientError):
            hub = ""
        hub_shown = hub or f"arn:aws:iam::<management account>:role/{cloud_access.HUB_ROLE_NAME}"
        return {
            "version": cloud_access.version(), "mode": "org", "principal": "access",
            "role_name": cloud_access.HUB_ROLE_NAME, "audience": cloud_access.AUDIENCE,
            "identity": identity, "identity_note": identity_note,
            "packs": cloud_access.aws_packs(), "chosen": chosen,
            "terraform": cloud_access.hub_terraform_snippet(subject, integration_id),
            "cloudformation": {"template": cloud_access.cloudformation_template(),
                               "parameters": {**cloud_access.cloudformation_parameters("", integration_id, chosen),
                                              "HubRoleArn": hub_shown, "ExternalId": external_id}},
            "trust_policy": cloud_access.hub_trust_policy(subject),
            "permissions_policy": cloud_access.hub_permissions_policy(),
            "external_id": external_id,
            "hub_role_arn": hub or None,
            "spoke_role_name": cloud_access.ROLE_NAME,
            "spoke_terraform": cloud_access.spoke_terraform_snippet(hub_shown, external_id, integration_id, chosen),
            "spoke_trust_policy": cloud_access.spoke_trust_policy(hub_shown, external_id),
            "stackset": cloud_access.stackset_snippet(hub_shown, external_id, integration_id, chosen),
            "checked": True,
            **bedrock_block,
        }
    if mode == "models":
        return {
            "version": cloud_access.version(), "mode": "models", "principal": principal,
            "role_name": cloud_access.MODELS_ROLE_NAME, "audience": cloud_access.AUDIENCE,
            "identity": identity, "identity_note": identity_note,
            "packs": [], "chosen": [],
            "terraform": cloud_access.models_role_terraform_snippet(subject),
            "cloudformation": {"template": "", "parameters": {}},
            "trust_policy": cloud_access.trust_policy(subject),
            "checked": bab is not None or admin is not None,
            **bedrock_block,
        }
    return {
        "version": cloud_access.version(),
        "mode": "role",
        "role_name": cloud_access.ROLE_NAME,
        "audience": cloud_access.AUDIENCE,
        "principal": principal,
        "identity": identity,
        "identity_note": identity_note,
        "packs": cloud_access.aws_packs(),
        "chosen": chosen,
        "terraform": cloud_access.terraform_snippet(subject, integration_id, chosen) + bedrock_tf,
        "cloudformation": {
            "template": cloud_access.cloudformation_template(),
            "parameters": cloud_access.cloudformation_parameters(subject, integration_id, chosen),
        },
        "trust_policy": cloud_access.trust_policy(subject),
        "checked": bab is not None or admin is not None,
        **bedrock_block,
    }


def vertex_models_url(project: str, location: str) -> str:
    """The one Vertex AI read the probe makes: list one publisher model in the project at
    the location. Regional locations have their own host; `global` uses the plain one."""
    host = "aiplatform.googleapis.com" if location == VERTEX_GLOBAL else f"{location}-aiplatform.googleapis.com"
    return f"https://{host}/v1/projects/{project}/locations/{location}/publishers/google/models?pageSize=1"


def gcp_setup(clients: Clients | None, *, integration_id: str, packs: list[str] | None, actor: str = "operator@room",
              mode: str = cloud_access.GCP_PROJECT_MODE, target: str = "") -> dict[str, Any]:
    """Everything the Connect Google Cloud page shows (kobo#390): the identity to grant
    (the permission service's service account, as it reports itself), the packs, and
    the roles bound three ways: `gcloud` commands, Terraform, and the plain list. `mode`
    is `parent` (an organization, or a folder, whose readable projects connect under it;
    the default scope since kobo#405) or `project` (one project); `target` is the id
    typed so far, so the snippets read whole. The models pack rides either scope: on an
    organization the one grant covers the Vertex AI project too."""
    bab = _bab(clients)
    mode = cloud_access.GCP_PARENT_MODE if mode == cloud_access.GCP_PARENT_MODE else cloud_access.GCP_PROJECT_MODE
    identity: dict[str, str] | None = None
    identity_note: str | None = None
    if bab is None:
        identity_note = "Google Cloud connects through the permission service; turn Access on first"
    else:
        try:
            got = bab.identity("gcp", actor)
            identity = {"email": str(got.get("email") or ""), "subject": str(got.get("subject") or "")}
        except ClientError as exc:
            identity_note = str(exc)
    chosen = cloud_access.gcp_chosen_packs(packs)
    roles = cloud_access.gcp_roles(chosen)
    email = (identity or {}).get("email", "")
    return {
        "version": cloud_access.version(),
        "mode": mode,
        "principal": "access",
        "identity": identity,
        "identity_note": identity_note,
        "packs": cloud_access.gcp_packs(),
        "chosen": chosen,
        "roles": roles,
        "target": target,
        "gcloud": cloud_access.gcloud_snippet(email, target, roles, mode),
        "terraform": cloud_access.gcp_terraform_snippet(email, target, roles, mode),
        "vertex_default_location": VERTEX_DEFAULT_LOCATION,
        "checked": bab is not None,
    }


def vertex_models_spec(identity: dict[str, str], *, row_project: str | None = None) -> dict[str, str] | None:
    """The `models` a Google Cloud row carries when the Vertex AI box is ticked
    (kobo#405): the provider, the Vertex AI project (the one named, else the row's
    own project) and the location. None when the box is not ticked."""
    if identity.get("models_provider") != MODELS_PROVIDER_VERTEX:
        return None
    project = identity.get("models_project") or row_project or ""
    return {"provider": MODELS_PROVIDER_VERTEX, "project": project,
            "location": identity.get("models_location") or VERTEX_DEFAULT_LOCATION}


def _vertex_probe_fields(models: dict[str, str]) -> dict[str, str]:
    """What the probe body carries so the permission service lists models in the
    Vertex AI project at the location with its own token: the service does the
    read, since the token is its to mint, never the room's."""
    return {"vertex_location": models["location"], "vertex_url": vertex_models_url(models["project"], models["location"])}


def _probe_vertex_project(bab, actor: str, models: dict[str, str]) -> dict[str, Any]:
    """Read the Vertex AI project and list its models there, as the same probe a
    project row makes; a refusal is the connect's refusal."""
    try:
        return bab.probe("gcp", actor, what=f"the Vertex AI project {models['project']}",
                         project=models["project"], service_account=None, **_vertex_probe_fields(models))
    except ClientError as exc:
        raise ConnectRefused(f"{exc}; grant the room's identity the roles on this page, including the models pack, "
                             f"and connect again") from exc


def _vertex_detail(models: dict[str, str], probe: dict[str, Any]) -> str:
    return (f"; Vertex AI answers at {models['location']} in {models['project']}, so its models serve the room" if probe.get("vertex")
            else f"; Vertex AI at {models['location']} in {models['project']} was not checked by the permission service, "
                 f"the models are taken at their word")


def _connect_gcp(
    session: Session, clients: Clients | None, config: Config, bab, *, vendor: str, name: str, base_access: str,
    trust: str, actor: str, auth: str, identity: dict[str, str],
) -> AdminIntegration:
    """Google Cloud through the permission service (kobo#390), the AWS shape without
    the role: the service's own identity is granted on the organization or the project,
    so the probe is the service reading it (and, with models ticked, listing Vertex AI
    models in the named project), and nothing is written until it does. An organization
    (or folder) is the hub shape: no ceiling of its own, its readable projects connect
    under it, and it may carry the Vertex AI project (kobo#405). Leases come from the
    service; the gateway has nothing to learn."""
    if bab is None:
        raise ConnectRefused("Google Cloud connects through the permission service; turn Access on first")
    row_id = re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")[:64]
    base_spec: dict[str, Any] = {
        "address": "", "trust": trust if trust in ("trusted", "untrusted") else "untrusted", "auth": auth, "secret_keys": [],
    }
    if auth == GCP_PARENT_AUTH:
        parent_resource = identity["parent_resource"]
        models = vertex_models_spec(identity)
        detail = f"{parent_resource} is held as a {parent_resource.split('/', 1)[0][:-1]} in the permission service"
        if models:
            # The Vertex AI project is listed before anything is written; a refusal saves nothing.
            detail += _vertex_detail(models, _probe_vertex_project(bab, actor, models))
        spec = {**base_spec, "kind": "org", "parent_resource": parent_resource, "identity": {"parent_resource": parent_resource}}
        if models:
            spec["models"] = models
        row = admin_store.put_integration(
            session, vendor=vendor, name=name, base_access=base_access, spec=spec, credential_ref=None, actor=actor,
            integration_id=row_id,
        )
        try:
            # Nothing is granted on a folder or organization; its projects carry the ceilings.
            bab.put_integration(row.id, vendor, {"parent_resource": parent_resource, "name": name}, [], actor)
        except ClientError as exc:
            admin_store.set_integration_state(session, row.id, state=admin_store.STATE_FAILED,
                                              detail=f"{detail}; the permission service refused: {exc}")
            return row
        try:
            found = register_accounts(session, clients, config, org_id=row.id, actor=actor)
            detail += (f"; {len(found['connected'])} project(s) the identity can read are connected, "
                       f"{len(found['without_role'])} it cannot read yet")
        except ClientError as exc:
            detail += f"; its projects could not be checked yet: {exc}"
        admin_store.set_integration_state(session, row.id, state=admin_store.STATE_APPLIED, detail=detail)
        return row

    project = identity["project"]
    service_account = identity.get("service_account") or None
    parent_id = identity.get("parent") or None
    if parent_id:
        _org_or_refused(bab, parent_id, actor, vendor="gcp")
    models = vertex_models_spec(identity, row_project=project)
    body: dict[str, Any] = {"project": project, "service_account": service_account}
    if models and models["project"] == project:
        # The probe reaches Vertex AI at the location too, in the same read.
        body.update(_vertex_probe_fields(models))
    try:
        probe = bab.probe("gcp", actor, what=f"the project {project}", **body)
    except ClientError as exc:
        raise ConnectRefused(f"{exc}; grant the room's identity the roles on this page on the project and connect again") from exc
    if models and models["project"] != project:
        # A Vertex AI project other than the row's own is read on its own.
        probe["vertex"] = _probe_vertex_project(bab, actor, models).get("vertex")
    spec = {
        **base_spec,
        "kind": "account", "project": project,
        "identity": {k: v for k, v in identity.items() if k in ("project", "service_account", "parent")},
    }
    if probe.get("project_number"):
        spec["project_number"] = str(probe["project_number"])
    if parent_id:
        spec["parent"] = parent_id
    if models:
        spec["models"] = models
    row = admin_store.put_integration(
        session, vendor=vendor, name=name, base_access=base_access, spec=spec, credential_ref=None, actor=actor,
        integration_id=row_id,
    )
    detail = f"Google Cloud agreed: the permission service reached project {project}"
    if probe.get("project_number"):
        detail += f" (number {probe['project_number']})"
    detail += f" as {probe.get('subject') or 'its identity'}" + (f", acting as {service_account}" if service_account else "")
    if models:
        detail += _vertex_detail(models, probe)
    try:
        bab.put_integration(row.id, vendor, {"project": project, "service_account": service_account, "name": name},
                            bab_mod.read_permissions(bab, vendor, actor), actor, parent=parent_id)
    except ClientError as exc:
        admin_store.set_integration_state(session, row.id, state=admin_store.STATE_FAILED,
                                          detail=f"{detail}; the permission service refused: {exc}")
        return row
    detail += "; connected in the permission service, agents may ask for its read permissions"
    admin_store.set_integration_state(session, row.id, state=admin_store.STATE_APPLIED, detail=detail)
    return row


def _self_hosted_probe(base_url: str, api_key: str | None, models: list[dict[str, str]], options: dict[str, Any]) -> self_hosted.ProbeResult:
    """The room's own probe of a self-hosted endpoint, as one sentence when it
    refuses. Not through the permission service: nothing is leased here."""
    try:
        return self_hosted.probe(base_url, api_key, models, options, client=self_hosted.http_client())
    except self_hosted.EndpointRefused as exc:
        raise ConnectRefused(str(exc)) from None


def _connect_self_hosted(
    session: Session, clients: Clients | None, config: Config, *, vendor: str, name: str, base_access: str,
    trust: str, actor: str, identity: dict[str, str], secrets: dict[str, str],
) -> AdminIntegration:
    """Self-hosted models (kobo#390): probe the endpoint and each named model
    first, write nothing until at least one answers; then the key, when
    there is one, into the room's own Secret, and the row with the probe's
    verdict per model. The gateway filler runs after (`connect`)."""
    base_url = self_hosted.normalize_base_url(identity.get("base_url", ""))
    models = self_hosted.parse_models(identity.get("models", ""))
    options = self_hosted.parse_options(identity.get("options", ""))
    api_key = secrets.get(self_hosted.KEY_FIELD) or None
    kubectl = getattr(clients, "kubectl", None) if clients is not None else None
    namespace = room_services.room_namespace(config) if api_key else ""
    if api_key and (not namespace or kubectl is None):
        raise ConnectRefused(f"the endpoint's key has nowhere to live: {room_services.NOWHERE_TO_STORE}")
    probe = _self_hosted_probe(base_url, api_key, models, options)
    if not probe.answering:
        raise ConnectRefused(f"none of the models answered at {self_hosted.host_of(base_url)}: {probe.sentence()}")
    row_id = re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")[:64]
    if not row_id:
        raise ConnectRefused("name this endpoint with letters or digits")
    spec: dict[str, Any] = {
        "address": "", "trust": trust if trust in ("trusted", "untrusted") else "untrusted", "auth": SELF_HOSTED_AUTH,
        "secret_keys": [self_hosted.KEY_FIELD] if api_key else [], "identity": {},
        "base_url": base_url, "options": options, "models": probe.models, "listed": probe.listed,
    }
    credential_ref = self_hosted.credential_ref(namespace, row_id) if api_key else None
    row = admin_store.put_integration(
        session, vendor=vendor, name=name, base_access=base_access, spec=spec, credential_ref=credential_ref, actor=actor,
        integration_id=row_id,
    )
    if api_key:
        try:
            kubectl.apply_secret(namespace, self_hosted.secret_name(row_id), {self_hosted.KEY_FIELD: api_key})
        except ClientError as exc:
            admin_store.set_integration_state(session, row.id, state=admin_store.STATE_FAILED,
                                              detail=f"the room could not keep the key: {exc}")
            return row
    detail = f"{self_hosted.host_of(base_url)} answers; {probe.sentence()}"
    admin_store.set_integration_state(session, row.id, state=admin_store.STATE_APPLIED, detail=detail)
    return row


def check_models(session: Session, clients: Clients | None, config: Config, *, integration_id: str, actor: str) -> dict[str, Any]:
    """Check models on a Self-hosted row (kobo#390): the connect probe again,
    with the row's key read back from the room's Secret; the verdict per
    model replaces the row's, the gateway filler follows, and a row whose
    models all stopped answering reads Failed until one answers again."""
    row = session.get(AdminIntegration, integration_id)
    if row is None or (row.spec or {}).get("auth") != SELF_HOSTED_AUTH:
        raise ConnectRefused(f"no self-hosted endpoint {integration_id!r}")
    spec = dict(row.spec or {})
    kubectl = getattr(clients, "kubectl", None) if clients is not None else None
    try:
        api_key = self_hosted.read_key(kubectl, row.credential_ref)
    except ClientError as exc:
        raise ConnectRefused(f"the endpoint's key could not be read back: {exc}") from None
    named = [{"name": str(m.get("name")), "label": str(m.get("label") or m.get("name"))}
             for m in spec.get("models") or [] if isinstance(m, dict) and m.get("name")]
    probe = _self_hosted_probe(str(spec.get("base_url") or ""), api_key, named, dict(spec.get("options") or {}))
    before = admin_store.integration_dict(row)
    row.spec = {**spec, "models": probe.models, "listed": probe.listed}
    session.flush()
    admin_store.record(session, actor=actor, action="integration.check_models", object_type="integration",
                       object_id=row.id, before=before, after=admin_store.integration_dict(row))
    detail = f"{self_hosted.host_of(str(spec.get('base_url') or ''))} answers; {probe.sentence()}"
    state = admin_store.STATE_APPLIED if probe.answering else admin_store.STATE_FAILED
    admin_store.set_integration_state(session, row.id, state=state, detail=detail)
    _fill_models_gateway(session, clients, config, row)
    return {"id": row.id, "models": probe.models, "answering": len(probe.answering), "total": len(probe.models),
            "state": state, "detail": row.state_detail}


def _fill_models_gateway(session: Session, clients: Clients | None, config: Config, row: AdminIntegration | None) -> None:
    """After a connect or a disconnect that touches a provider (kobo#390):
    run the gateway filler. A gateway that did not take the models leaves
    the row connected, with the refusal on its line; the models it did take
    serve, the rest show as not available until the next sync."""
    result = model_providers.sync_from_session(session, clients, config)
    if result is None or result.ok or row is None or row.state != admin_store.STATE_APPLIED:
        return
    admin_store.set_integration_state(
        session, row.id, state=admin_store.STATE_APPLIED, detail=f"{row.state_detail or 'connected'}; {result.failure}"
    )


def connect(
    session: Session,
    clients: Clients | None,
    config: Config,
    *,
    vendor: str,
    name: str,
    base_access: str,
    credential: str,
    address: str,
    trust: str,
    actor: str,
    auth: str = "token",
    identity: dict[str, str] | None = None,
    secrets: dict[str, str] | None = None,
) -> AdminIntegration:
    """The three steps above. The row is committed by the caller; a
    projection that fails leaves the row `pending` with the failure in
    `state_detail`, never an exception past the audit event."""
    identity = {k: v.strip() for k, v in (identity or {}).items() if v and v.strip()}
    secrets = {k.upper(): v for k, v in (secrets or {}).items() if v and v.strip()}
    if credential.strip() and SECRET_KEY not in secrets:
        secrets[SECRET_KEY] = credential.strip()
    check_auth(auth, identity, secrets)
    bab = _bab(clients)
    if auth == SLACK_AUTH:
        return _connect_slack(session, clients, vendor=vendor, name=name, base_access=base_access, trust=trust,
                              actor=actor, identity=identity, secrets=secrets)
    if auth == DATADOG_AUTH:
        return _connect_datadog(session, clients, config, bab, vendor=vendor, name=name, base_access=base_access,
                                address=address, trust=trust, actor=actor, identity=identity, secrets=secrets)
    if auth == GITHUB_AUTH:
        return _connect_github(session, clients, config, bab, vendor=vendor, name=name, base_access=base_access,
                               address=address, trust=trust, actor=actor, identity=identity, secrets=secrets)
    if auth in GCP_AUTHS:
        row = _connect_gcp(session, clients, config, bab, vendor=vendor, name=name, base_access=base_access,
                           trust=trust, actor=actor, auth=auth, identity=identity)
        _fill_models_gateway(session, clients, config, row)
        return row
    if auth == SELF_HOSTED_AUTH:
        row = _connect_self_hosted(session, clients, config, vendor=vendor, name=name, base_access=base_access,
                                   trust=trust, actor=actor, identity=identity, secrets=secrets)
        _fill_models_gateway(session, clients, config, row)
        return row
    row = _connect_cloud_or_token(session, clients, config, bab, vendor=vendor, name=name, base_access=base_access,
                                  address=address, trust=trust, actor=actor, auth=auth, identity=identity, secrets=secrets)
    if (row.spec or {}).get("models"):
        # An AWS account with Bedrock ticked (kobo#390): the gateway learns its models now.
        _fill_models_gateway(session, clients, config, row)
    return row


def bedrock_models_spec(identity: dict[str, str]) -> dict[str, str] | None:
    """The `models` an AWS account row carries when the Bedrock box is ticked:
    the provider, the region the gateway calls, and the role it assumes (the
    row's own). None when the box is not ticked."""
    if identity.get("models_provider") != MODELS_PROVIDER_BEDROCK:
        return None
    return {"provider": MODELS_PROVIDER_BEDROCK, "region": identity.get("models_region") or BEDROCK_DEFAULT_REGION,
            "role_arn": identity["role_arn"]}


def _connect_cloud_or_token(
    session: Session, clients: Clients | None, config: Config, bab, *, vendor: str, name: str, base_access: str,
    address: str, trust: str, actor: str, auth: str, identity: dict[str, str], secrets: dict[str, str],
) -> AdminIntegration:
    """The AWS kinds and the token kinds: probe, row, credential, then the
    permission service or the gateway. Split from `connect` so the Bedrock
    filler runs once over whatever row this leaves."""
    sero = clients.sero if clients is not None else None
    probe: dict[str, str] | None = None
    hub_role_arn: str | None = None
    if auth in ("aws_hub", "aws_spoke") and bab is None:
        raise ConnectRefused("AWS organizations and their accounts need the permission service; turn Access on first")
    if auth == "aws_role":
        identity.setdefault("audience", cloud_access.AUDIENCE)
        if bab is not None:
            try:
                probe = bab.probe_aws(identity["role_arn"], identity["audience"], actor)
            except ClientError as exc:
                raise ConnectRefused(f"{exc}; fix the role\'s trust policy and connect again") from exc
        else:
            probe = probe_aws_role(clients, identity["role_arn"], identity["audience"])
    elif auth == "aws_hub":
        identity.setdefault("audience", cloud_access.AUDIENCE)
        try:
            probe = bab.probe_aws(identity["hub_role_arn"], identity["audience"], actor)
        except ClientError as exc:
            raise ConnectRefused(f"{exc}; fix the hub role\'s trust policy and connect again") from exc
    elif auth == "aws_spoke":
        hub_role_arn = _org_hub(bab, identity["parent"], actor)
        identity["external_id"] = bab.external_id_for_org(identity["parent"])
        identity.setdefault("audience", cloud_access.AUDIENCE)
        try:
            probe = bab.probe_aws(identity["role_arn"], identity["audience"], actor,
                                  hub_role_arn=hub_role_arn, external_id=identity["external_id"])
        except ClientError as exc:
            raise ConnectRefused(f"{exc}; the account\'s role must trust the organization\'s hub with this external id; "
                                 "fix it and connect again") from exc
    if secrets and (sero is None or not config.sero_url):
        raise ConnectRefused("the Secrets add-on is off, so the room has nowhere to hold the credential; turn it on first")
    probe_note = ""
    if auth in TOKEN_AUTHS:
        address = gateway_address(address)
    if auth == BASIC_AUTH:
        # The pair is checked with the vendor's server first, then stored joined as the one value
        # the gateway's basic scheme sends; the email stays readable on the row.
        if address:
            _basic_probe(name, address, identity[BASIC_USER_KEY], secrets[SECRET_KEY])
            probe_note = f"{name} accepted the email and API token; "
        secrets = {SECRET_KEY: f"{identity[BASIC_USER_KEY]}:{secrets[SECRET_KEY]}"}
    elif auth == "token" and address and vendor in PROBED_TOKEN_VENDORS:
        # A service account's key is checked the same way, as the Bearer the gateway sends.
        _bearer_probe(name, address, secrets[SECRET_KEY])
        probe_note = f"{name} accepted the service account API key; "
    spec: dict[str, Any] = {
        "address": address,
        "trust": trust if trust in ("trusted", "untrusted") else "untrusted",
        "auth": auth,
        "identity": {k: v for k, v in identity.items() if k != "external_id"},
        "secret_keys": sorted(secrets),
    }
    if auth == "aws_hub":
        spec["kind"] = "org"
    elif auth == "aws_spoke":
        spec["kind"], spec["parent"], spec["account_id"] = "account", identity["parent"], identity["account_id"]
    # Models only (the `models` shape of the AWS sheet): the role holds the Bedrock
    # permissions and nothing else, so the row gets no ceiling and no agent is offered
    # the account; the person's tick is implied.
    models_only = auth == "aws_role" and str(identity.get("models_only", "")).strip().lower() in ("1", "true", "yes")
    if models_only:
        identity.setdefault("models_provider", MODELS_PROVIDER_BEDROCK)
    models = bedrock_models_spec(identity) if auth in BEDROCK_AUTHS else None
    if models:
        spec["models"] = models
        spec["identity"] = {k: v for k, v in spec["identity"].items()
                            if k not in ("models_provider", "models_region", "models_only")}
    if models_only:
        spec["models_only"] = True
    # Organizations, their accounts and directly assumed roles are named by the person
    # (acme-aws, acme-prod, acme-models), so the row id is the name, not the vendor: a
    # vendor may hold many of them (kobo#390: one account for Bedrock, another for audit).
    wanted_id = re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")[:64] if auth.startswith("aws_") else None
    row = admin_store.put_integration(
        session, vendor=vendor, name=name, base_access=base_access, spec=spec,
        credential_ref=None, actor=actor, integration_id=wanted_id,
    )
    path = credential_path(row.id)
    try:
        if secrets:
            sero.put_secret(actor, path, dict(secrets))
            if config.gateway_namespace:
                sero.ensure_synced(actor, ROOM_APP, path, config.gateway_namespace, secret_name(row.id))
            row.credential_ref = path
    except ClientError as exc:
        admin_store.set_integration_state(session, row.id, state=admin_store.STATE_FAILED, detail=f"the secrets add-on refused: {exc}")
        return row
    detail = f"{probe_note}credential stored" if secrets else "no secret to store; the connection uses the room\'s own identity"
    minter = "the permission service" if bab is not None else "the gateway"
    if probe is not None:
        detail = f"AWS agreed: {minter} assumed the role in account {probe['account']}"
    elif auth == "aws_role":
        detail = "not checked with AWS: the gateway\'s admin plane is not wired; the role is taken at its word"
    if secrets and not config.gateway_namespace:
        detail += "; no gateway namespace is set, so it is not synced to the gateway yet"
    if models:
        # No Bedrock read at connect: the permission service's AWS probe assumes the role
        # and stops there. The models are registered on the gateway as this role; the
        # first call an agent makes is the check with AWS.
        detail += (f"; Bedrock models in {models['region']} serve the room through this role, "
                   "registered on the model gateway and not yet checked with AWS")
    # The permission service holds the connection: what agents may be granted there (the
    # ceiling is every read permission the catalogue offers for the vendor; the role's or
    # token's own policy is the outer wall) and, for a cloud, what to assume.
    if bab is not None:
        parent_id: str | None = None
        if auth == "aws_role":
            spec_for_bab: dict[str, Any] = {"role_arn": identity["role_arn"], "audience": identity["audience"], "name": name}
            ceiling = [] if models_only else bab_mod.read_permissions(bab, vendor, actor)
        elif auth == "aws_hub":
            spec_for_bab = {"hub_role_arn": identity["hub_role_arn"], "audience": identity["audience"], "name": name,
                            "external_id": bab.external_id_for_org(wanted_id or name)}
            ceiling = []  # nothing is granted on an organization; its accounts carry the ceilings
        elif auth == "aws_spoke":
            parent_id = identity["parent"]
            spec_for_bab = {"spoke_role_arn": identity["role_arn"], "external_id": identity["external_id"],
                            "account_id": identity["account_id"], "audience": identity["audience"], "name": name}
            ceiling = bab_mod.read_permissions(bab, vendor, actor)
        else:
            # "Agents may read and write" on the sheet lifts the ceiling to every permission the
            # catalogue offers (its mutate tier too, with bab's shorter validity); "read" keeps
            # the read tier. The credential's own rights are the outer wall either way.
            spec_for_bab = {}
            ceiling = (bab_mod.all_permissions(bab, vendor, actor) if base_access == "write"
                       else bab_mod.read_permissions(bab, vendor, actor))
        try:
            bab.put_integration(row.id, vendor, spec_for_bab, ceiling, actor, parent=parent_id)
            if auth == "aws_hub":
                detail += "; connected in the permission service as an organization"
            elif models_only:
                detail += "; connected in the permission service with nothing to grant: it serves Bedrock models only"
            else:
                detail += "; connected in the permission service, agents may ask for its read permissions"
        except ClientError as exc:
            admin_store.set_integration_state(session, row.id, state=admin_store.STATE_FAILED, detail=f"{detail}; the permission service refused: {exc}")
            return row
        if auth == "aws_hub":
            # The accounts register themselves (#275): every one the hub can reach with the
            # organization's external id is connected now; the rest wait for the role.
            try:
                found = register_accounts(session, clients, config, org_id=row.id, actor=actor)
                detail += (f"; {len(found['connected'])} account(s) carry the role and are connected, "
                           f"{len(found['without_role'])} do not yet")
            except ClientError as exc:
                detail += f"; its accounts could not be checked yet: {exc}"
    if bab is not None and auth in ("aws_role", "aws_hub", "aws_spoke"):
        # Leases come from the permission service; the gateway has nothing to learn.
        admin_store.set_integration_state(session, row.id, state=admin_store.STATE_APPLIED, detail=detail)
        return row
    # A token's target goes live through the admin plane when the room holds one
    # (liminal#222); the other kinds, and a room without one, go by pull request.
    live, fallback = _target_live_or_fallback(session, row, clients, detail)
    if live is None and fallback is None:
        return row
    if live:
        row.spec = {**dict(row.spec or {}), "gateway": GATEWAY_LIVE}
        admin_store.set_integration_state(session, row.id, state=admin_store.STATE_APPLIED, detail=f"{detail}; {live}")
        return row
    try:
        pr = _gateway_pr(clients.github, config, row, remove=False, actor=actor)
    except (ClientError, OSError) as exc:
        admin_store.set_integration_state(session, row.id, state=admin_store.STATE_FAILED, detail=f"{detail}; the gateway pull request failed: {exc}")
        return row
    if pr:
        detail += f"{fallback} {pr}; serves after it merges and the gateway rolls"
        row.spec = {**dict(row.spec or {}), "pull_request": pr}
        admin_store.set_integration_state(session, row.id, state=admin_store.STATE_PENDING, detail=detail)
        return row
    if config.gateway_config_repo:
        detail += "; the gateway already carries this target"
        admin_store.set_integration_state(session, row.id, state=admin_store.STATE_APPLIED, detail=detail)
        return row
    detail += "; no gateway config repository is set, so add the target by hand"
    admin_store.set_integration_state(session, row.id, state=admin_store.STATE_PENDING, detail=detail)
    return row


def register_accounts(session: Session, clients: Clients | None, config: Config, *, org_id: str, actor: str) -> dict[str, Any]:
    """Ask the permission service which of an organization's accounts carry the spoke
    role (probed through the hub with the organization's one external id) and connect
    the ones that do: a row here and an account under the organization there, with the
    read ceiling. Accounts without the role are reported, not created. Safe to run again:
    connected accounts are left alone."""
    bab = _bab(clients)
    if bab is None:
        raise ConnectRefused("checking an organization\'s accounts needs the permission service")
    org = _org_or_refused(bab, org_id, actor, vendor=None)
    if org.get("vendor") == "gcp":
        return _register_projects(session, bab, org_id=org_id, actor=actor)
    # An organization connected before external ids were per organization has none in
    # the permission service; give it its id now (the same derivation the set-up page
    # shows), so discover can probe its accounts. Its spoke rows keep their own ids.
    if not (org.get("external_id") or (org.get("spec") or {}).get("external_id")):
        bab.put_integration(org_id, "aws", {"hub_role_arn": org.get("hub_role_arn") or bab.org_hub_role_arn(org_id, actor),
                                            "audience": cloud_access.AUDIENCE, "name": org.get("name") or org_id,
                                            "external_id": bab.external_id_for_org(org_id)}, [], actor)
    found = bab.discover_accounts(org_id, actor, cloud_access.ROLE_NAME)
    taken = {r.id for r in admin_store.list_integrations(session)} | {i["integration"] for i in bab.integrations(actor)}
    ceiling: list[str] | None = None
    out: dict[str, Any] = {"organization": org_id, "connected": [], "without_role": [], "already": []}
    for a in found.get("accounts", []):
        entry = {"id": a["id"], "name": a.get("name") or a["id"], "integration": a.get("integration"), "reason": a.get("reason")}
        if a.get("connected"):
            out["already"].append(entry)
            continue
        if not a.get("reachable"):
            out["without_role"].append(entry)
            continue
        base = re.sub(r"[^a-z0-9-]", "-", str(entry["name"]).lower()).strip("-")[:60] or a["id"]
        row_id = base if base not in taken else f"{base}-{a['id'][-4:]}"
        taken.add(row_id)
        if ceiling is None:
            ceiling = bab_mod.read_permissions(bab, "aws", actor)
        spec_for_bab = {"spoke_role_arn": a["role_arn"], "account_id": a["id"], "audience": cloud_access.AUDIENCE, "name": entry["name"]}
        bab.put_integration(row_id, "aws", spec_for_bab, ceiling, actor, parent=org_id)
        row = admin_store.put_integration(
            session, vendor="aws", name=str(entry["name"]), base_access="read",
            spec={"address": "", "trust": "untrusted", "auth": "aws_spoke", "kind": "account", "parent": org_id,
                  "account_id": a["id"], "identity": {"parent": org_id, "role_arn": a["role_arn"], "account_id": a["id"],
                                                      "audience": cloud_access.AUDIENCE}, "secret_keys": []},
            credential_ref=None, actor=actor, integration_id=row_id,
        )
        admin_store.set_integration_state(
            session, row.id, state=admin_store.STATE_APPLIED,
            detail="found through the organization\'s hub: the account carries the role; connected in the permission service, "
                   "agents may ask for its read permissions")
        entry["integration"] = row.id
        out["connected"].append(entry)
    return out


def _register_projects(session: Session, bab, *, org_id: str, actor: str) -> dict[str, Any]:
    """The Google Cloud half of `register_accounts` (kobo#390): the permission service
    lists the projects under the folder or organization and says which its identity can
    read; those connect (a row here, a project under the parent there, the read ceiling),
    the rest are reported under `without_role` so the page can say which still need the
    grant. Safe to run again: connected projects are left alone."""
    found = bab.discover_accounts(org_id, actor)
    taken = {r.id for r in admin_store.list_integrations(session)} | {i["integration"] for i in bab.integrations(actor)}
    ceiling: list[str] | None = None
    out: dict[str, Any] = {"organization": org_id, "connected": [], "without_role": [], "already": []}
    for a in found.get("accounts", []):
        project = str(a.get("project") or a["id"])
        entry = {"id": project, "name": a.get("name") or project, "integration": a.get("integration"), "reason": a.get("reason")}
        if a.get("connected"):
            out["already"].append(entry)
            continue
        if not a.get("reachable"):
            out["without_role"].append(entry)
            continue
        base = re.sub(r"[^a-z0-9-]", "-", str(entry["name"]).lower()).strip("-")[:60] or project
        row_id = base if base not in taken else f"{base}-{project[-4:]}"
        taken.add(row_id)
        if ceiling is None:
            ceiling = bab_mod.read_permissions(bab, "gcp", actor)
        bab.put_integration(row_id, "gcp", {"project": project, "service_account": None, "name": entry["name"]}, ceiling, actor,
                            parent=org_id)
        spec: dict[str, Any] = {"address": "", "trust": "untrusted", "auth": GCP_AUTH, "kind": "account", "parent": org_id,
                                "project": project, "identity": {"parent": org_id, "project": project}, "secret_keys": []}
        if a.get("project_number"):
            spec["project_number"] = str(a["project_number"])
        row = admin_store.put_integration(
            session, vendor="gcp", name=str(entry["name"]), base_access="read", spec=spec,
            credential_ref=None, actor=actor, integration_id=row_id,
        )
        admin_store.set_integration_state(
            session, row.id, state=admin_store.STATE_APPLIED,
            detail="found under its folder or organization: the identity can read the project; connected in the "
                   "permission service, agents may ask for its read permissions")
        entry["integration"] = row.id
        out["connected"].append(entry)
    return out


def _remove_from_gateway(clients: Clients | None, config: Config, row: AdminIntegration, *, actor: str) -> str:
    """The gateway half of a disconnect, as one sentence. A target the admin
    plane holds live (`spec.gateway = "live"`, or a row that never had a pull
    request while the room holds an admin plane) is deleted there; a target
    that went by pull request gets the removal pull request. An admin plane
    without target routes sends the row down the pull-request path too."""
    spec = row.spec or {}
    went_live = spec.get("gateway") == GATEWAY_LIVE
    admin = _admin(clients)
    if went_live or (admin is not None and not spec.get("pull_request") and live_target_spec(row) is not None):
        if admin is None:
            return "left: the gateway\'s admin plane is not wired, so its live target stays"
        try:
            removed = apply_target_live(clients, row, remove=True)
        except ClientError as exc:
            if went_live or not targets_unsupported(exc):
                return f"left: {exc}"
        else:
            if removed:
                return removed
    github = getattr(clients, "github", None) if clients is not None else None
    try:
        pr = _gateway_pr(github, config, row, remove=True, actor=actor) if github is not None else None
    except (ClientError, OSError) as exc:
        return f"left: {exc}"
    return f"pull request open: {pr}" if pr else "nothing to remove"


def disconnect(session: Session, clients: Clients | None, config: Config, *, integration_id: str, actor: str) -> dict[str, Any]:
    """Reverse the three steps. The row goes first (it is the truth); each
    projection\'s outcome is reported back as a sentence."""
    bab = _bab(clients)
    row = session.get(AdminIntegration, integration_id)
    if row is None:
        # Connected straight into the permission service (never through this panel): take it out of there.
        if bab is not None:
            try:
                out = bab.delete_integration(integration_id, actor)
            except ClientError as exc:
                if "no integration" in str(exc):
                    # Gone on both sides: a second Remove while the first one was still
                    # opening the gateway's pull request lands here. Nothing to refuse.
                    return {"id": integration_id, "secret": "none", "gateway": "none", "access": "already removed"}
                raise ConnectRefused(str(exc)) from exc
            return {"id": integration_id, "secret": "none", "gateway": "none",
                    "access": f"removed; {out.get('grants_revoked', 0)} grant(s) revoked"}
        raise ConnectRefused(f"no integration {integration_id!r}")
    outcome: dict[str, Any] = {"id": integration_id, "secret": "left", "gateway": "left"}
    auth = (row.spec or {}).get("auth")
    # Slack, and a GitHub row made without the permission service, are the room's own
    # (kobo#377): nothing of theirs is in the service or on the gateway.
    room_only = auth in (SLACK_AUTH, SELF_HOSTED_AUTH) or bool((row.spec or {}).get("room_only"))
    if bab is not None and not room_only:
        try:
            out = bab.delete_integration(integration_id, actor)
            outcome["access"] = f"removed; {out.get('grants_revoked', 0)} grant(s) revoked"
        except ClientError as exc:
            outcome["access"] = f"left: {exc}"
    if auth in MCP_AUTHS or auth == SLACK_AUTH:
        # The keys live in the permission service's Secret and, for the room's own two
        # vendors, in the room's; never in the add-on or the gateway.
        kubectl = getattr(clients, "kubectl", None) if clients is not None else None
        gone: list[str] = []
        try:
            if auth in MCP_AUTHS and not room_only:
                if kubectl is None:
                    raise ClientError("this room cannot reach the permission service's namespace")
                kubectl.delete_secret(config.bab_namespace, DATADOG_SECRET if auth == DATADOG_AUTH else GITHUB_SECRET)
                gone.append("the permission service's")
            if auth in (GITHUB_AUTH, SLACK_AUTH):
                room_credentials.remove_values(clients, row.vendor)
                gone.append("the room's")
            outcome["secret"] = "removed"
        except ClientError as exc:
            outcome["secret"] = f"left: {exc}" + (f" (removed {' and '.join(gone)})" if gone else "")
    elif auth == SELF_HOSTED_AUTH:
        # The key lives in the room's own Secret (kobo_api.self_hosted); a row without one has nothing to take back.
        kubectl = getattr(clients, "kubectl", None) if clients is not None else None
        if not row.credential_ref:
            outcome["secret"] = "none"
        elif kubectl is None:
            outcome["secret"] = "left: this room cannot reach its own Secrets"
        else:
            namespace, _, name = row.credential_ref[len(self_hosted.CREDENTIAL_REF_PREFIX):].partition("/")
            try:
                kubectl.delete_secret(namespace, name)
                outcome["secret"] = "removed"
            except ClientError as exc:
                outcome["secret"] = f"left: {exc}"
    else:
        sero = clients.sero if clients is not None else None
        if sero is not None and row.credential_ref:
            try:
                if config.gateway_namespace:
                    sero.unsync(actor, ROOM_APP, config.gateway_namespace, secret_name(integration_id))
                sero.delete_secret(actor, row.credential_ref)
                outcome["secret"] = "removed"
            except ClientError as exc:
                outcome["secret"] = f"left: {exc}"
    # The gateway's target goes the way it came: off the admin plane, or by pull request.
    outcome["gateway"] = "nothing to remove" if room_only else _remove_from_gateway(clients, config, row, actor=actor)
    went_wrong = any(str(v).startswith("left:") for v in outcome.values())
    served_models = bool((row.spec or {}).get("models"))
    admin_store.remove_integration(
        session, integration_id, actor=actor,
        landed=admin_store.LANDED_FAILED if went_wrong else admin_store.LANDED_APPLIED,
        detail="; ".join(f"{k}: {outcome[k]}" for k in ("access", "secret", "gateway") if k in outcome),
    )
    if served_models:
        # The provider is gone from the rows; the filler deletes exactly its models.
        result = model_providers.sync_from_session(session, clients, config)
        if result is not None:
            outcome["models"] = result.sentence()
    return outcome
