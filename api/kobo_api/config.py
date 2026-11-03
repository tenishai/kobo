"""Kobo API configuration.

Loads settings from a YAML file (or pure defaults) into a pydantic model.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ModelChoice(BaseModel):
    id: str  # LiteLLM route, e.g. vertex_ai/claude-sonnet-4-5
    label: str
    # How the picker names it (2026-09-29): a `family` ("Claude", "Gemini")
    # and a `tier` ("best", "balanced", "fast") instead of a model number.
    # When any model carries a tier the picker shows only tiered models,
    # grouped by family; the label above still names the exact model on
    # the agent's card. Both optional so an org config without them keeps
    # the flat list of labels.
    family: str | None = None
    tier: str | None = None
    # Per-model Vertex location override (Gemini-3.1-Pro-Preview arc):
    # Gemini previews serve ONLY on Vertex's global endpoint (a regional
    # endpoint 404s), while the Claude partner models stay on the org's
    # regional default. None (the default) means "no override — use
    # Config.vertex_location", same as every other model in the list today.
    # See vertex_location_for below for the resolution.
    location: str | None = None


# The reserved commons group name (shared-group + group-requests wave): the
# one group_name value every signed-in caller may create an agent under, or
# see an agent under, with no directory membership check at all -- see
# `kobo_api.pairs.allowed_pairs`'s own docstring for why a commons agent
# still produces no implicit dispatch pairs from group membership. Lives
# HERE rather than in `kobo_api.pairs` or `kobo_api.main` (the other two
# modules that read it): this file already owns what counts as a valid
# group name (`GroupChoice`, `groups`, `_admin_groups_must_be_configured_
# groups` below) -- SHARED_GROUP is a valid group name that's deliberately
# NOT one of `groups`' own entries, so the constant belongs beside the
# concept it's the one exception to, not beside either of its two
# consumers. `_groups_may_not_reuse_the_shared_name` below keeps a real
# `GroupChoice` from ever colliding with it.
SHARED_GROUP = "shared"


class GroupChoice(BaseModel):
    name: str  # what the room shows for this group
    # The directory splits one HUMAN team's membership across several
    # REGIONAL groups rather than one group per team (e.g. a single
    # "infra-engineering" team shows up in Okta as an org-wide group PLUS
    # per-region groups for usa/india/france, plus a separate leads group) --
    # so a room group's directory mapping is a LIST, not a single name, and
    # membership in ANY one of them counts. `kobo_api.groups.GroupResolver`
    # unions across the list; see that module's inverted-universe mapping
    # for how membership actually resolves.
    #
    # okta_group is 1:1 sugar for the (still common) single-group case;
    # okta_groups is the general list form. Setting both is fine -- they're
    # unioned (deduped, order preserving) by the validator below. At least
    # one of the two must resolve to a non-empty list: a group with no
    # directory group behind it could never have a member, which is a config
    # mistake, not a valid empty state, so construction fails loudly instead
    # of silently defining a group nobody can ever be granted.
    okta_group: str = ""
    okta_groups: list[str] = []
    # Which section of the create modal's group dropdown this group renders
    # under (create-modal non-engineering-wing ruling) -- "engineering" (the
    # default, so every group configured before this field existed keeps
    # rendering exactly where it always has) or "non-engineering". "shared"
    # is deliberately NOT a legal value here: that section is the reserved
    # commons `catalog_groups` (kobo_api.main) always prepends itself, never
    # a real `GroupChoice` at all -- see SHARED_GROUP's own docstring above
    # and `_groups_may_not_reuse_the_shared_name` below, which already keeps
    # a real group from claiming that NAME; `_kind_must_be_a_known_dropdown_
    # section` below is the same posture applied to this field instead.
    kind: str = "engineering"

    @field_validator("kind")
    @classmethod
    def _kind_must_be_a_known_dropdown_section(cls, v: str) -> str:
        allowed = {"engineering", "non-engineering"}
        if v not in allowed:
            raise ValueError(
                f"GroupChoice kind {v!r} is not one of {sorted(allowed)} "
                "(the create modal's dropdown only sections into these two "
                "-- 'shared' is the reserved commons section and is never "
                "configured on a group)"
            )
        return v

    @model_validator(mode="after")
    def _fold_okta_group_sugar(self) -> GroupChoice:
        merged: list[str] = []
        for candidate in (*self.okta_groups, self.okta_group):
            if candidate and candidate not in merged:
                merged.append(candidate)
        if not merged:
            raise ValueError(
                "GroupChoice requires okta_group or okta_groups to be set "
                "(a group with no directory group(s) could never have a member)"
            )
        self.okta_groups = merged
        return self


class MCPConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    """Google OAuth + JWT signing coordinates for kobo's MCP server (MCP
    server arc, Task 1) -- this block is the config surface later tasks'
    OAuth authorization server and tool set build on. All plain strings, no
    validators here: the one coupling rule (an enabled server needs a
    domain check) needs to see this whole block alongside the rest of
    `Config`, so it lives on `Config` itself
    (`_mcp_requires_a_workspace_domain_when_enabled` below) -- same "the
    validator lives where it can see the whole picture" posture as
    `admin_groups`' own validator living on `Config`, not `GroupChoice`.
    """

    # The Google OAuth 2.0 client ID MCP sign-ins authenticate through.
    # Empty (the default) means the MCP server is off -- see
    # `Config.mcp_enabled` below, the one place that truthiness is read.
    oauth_client_id: str = ""
    # Env var name (never the secret value itself) holding the OAuth client
    # secret. NEVER put a real secret in this or any tracked file.
    oauth_client_secret_env: str = "KOBO_MCP_GOOGLE_CLIENT_SECRET"
    # The Google Workspace domain an MCP sign-in must belong to -- the org
    # gate on the MCP path. Empty is fine while the server itself is off;
    # required the moment `oauth_client_id` is set (see the `Config`
    # validator below for why leaving it empty on an enabled server fails
    # loudly at construction instead of booting open).
    workspace_domain: str = ""
    # Env var name (never the key value itself) holding the key the MCP
    # server signs its own JWTs with. NEVER put a real key in a tracked file.
    jwt_key_env: str = "KOBO_MCP_JWT_KEY"
    # This room's own externally-reachable base URL as MCP clients and
    # Google see it (Task 4's OAuth AS: RFC 8414/9728 well-known documents,
    # and the redirect_uri kobo registers with Google for its own callback)
    # -- e.g. "https://kobo.example.org". Deliberately its OWN field rather
    # than reusing `Config.api_base_url`: that field describes the LOCAL
    # loopback address the bootstrap CLI/org-status consumers hit (defaults
    # to 127.0.0.1, and is explicitly NOT org.yaml-settable -- "it
    # describes *this* machine, not anything the org can hand out," per its
    # own docstring), whereas this one is exactly the opposite: the
    # externally-advertised identity an org configures once it knows its
    # own public hostname, the same "org-supplied later" posture as
    # `workspace_domain` above. Empty (the default) means "not known yet":
    # the well-known documents fall back to relative endpoint paths
    # (`/oauth/authorize` rather than an absolute URL) and the Google
    # callback redirect_uri falls back to the incoming request's own base
    # URL -- both fine for local dev/tests, neither sufficient for a real
    # deployment, which must set this before Google's own redirect_uri
    # allowlist can ever be satisfied. Required the moment `oauth_client_id`
    # is set (see the `Config` validator below,
    # `_mcp_requires_an_issuer_base_url_when_enabled`, for why leaving it
    # empty on an enabled server fails loudly at construction instead of
    # booting half-configured).
    issuer_base_url: str = ""
    # An MCP server on a room with no IAP in front of it hands every Workspace
    # member who completes OAuth the rights of an owner over every agent,
    # because the room's gates treat "no perimeter" as "trusted" (issue
    # #198). Construction refuses that pair unless this is set on purpose
    # (a local dev room, say); see `_mcp_requires_iap_unless_allowed`.
    allow_without_iap: bool = False


class SkillsFrontmatterConfig(BaseModel):
    """Optional vocabularies for the marketplace frontmatter gate
    (hardening wave). Each configured list adds a subset check in
    kobo_api.marketplace._validate_skill_frontmatter; an empty list adds
    nothing, so the gate is fully functional with this block absent and
    vocabulary drift can never brick publishing."""

    allowed_profiles: list[str] = []
    allowed_tags: list[str] = []


def vertex_location_for(config: Config, model_id: str) -> str:
    """The Vertex location `model_id` should run against: its own
    `ModelChoice.location` when the catalog sets one, else
    `config.vertex_location` (the org-wide default). Mirrors main.py's
    `_model_label` iteration idiom, including its "still render, not 500"
    stance — an unknown model_id (an agent created under an older `models`
    list) also falls back to `config.vertex_location` rather than raising.
    """
    for model in config.models:
        if model.id == model_id:
            return model.location or config.vertex_location
    return config.vertex_location


# Config fields that changed name and still mean the same thing. sero was
# named ladon and noto was named argus until 2026-10-12; a deployment config
# written before that keeps working, with one WARNING per old key. The new
# name wins when both are present.
RENAMED_CONFIG_KEYS = {
    "ladon_url": "sero_url",
    "ladon_audience": "sero_audience",
    "ladon_namespace": "sero_namespace",
    "argus_url": "noto_url",
    "argus_project": "noto_project",
}

_log = logging.getLogger(__name__)

# Keys a config file may still carry from an earlier release that no longer
# mean anything. Dropped with a warning rather than refused, so a room is not
# locked out of booting by a line it was told to leave alone at the time. The
# org-config side keeps its own list (`kobo_api.org.RETIRED_ORG_KEYS`).
RETIRED_CONFIG_KEYS = frozenset({"runtimes"})


class CloudAccount(BaseModel):
    """One unit a cloud vendor is connected by: an AWS account, a Google Cloud
    project, an Azure subscription. `integration` is the id the permission
    service knows it by (default: the name)."""

    model_config = ConfigDict(extra="forbid")

    vendor: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    integration: str = ""

    @property
    def integration_id(self) -> str:
        return self.integration or self.name


class Config(BaseModel):
    # Unknown keys are refused (issue #198). A typo used to be dropped on the
    # floor, and several security switches are off when their key is absent:
    # `iap_audiance: x` ran the room with no authentication and nothing said so.
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _accept_old_key_names(cls, data):
        if not isinstance(data, dict):
            return data
        out = dict(data)
        for retired in sorted(RETIRED_CONFIG_KEYS & set(out)):
            out.pop(retired)
            _log.warning("config key %r is retired and ignored; remove it from the file", retired)
        for old, new in sorted(RENAMED_CONFIG_KEYS.items()):
            if old not in out:
                continue
            value = out.pop(old)
            if new in out:
                _log.warning("config sets both %r and %r; %r wins", old, new, new)
                continue
            _log.warning("config key %r was renamed %r; honoured, rename it in the file", old, new)
            out[new] = value
        return out

    github_org: str = "example-org"
    # Full name (owner/repo) of the client's org config repo — cloned at
    # boot and merged over this file's own values (kobo_api.org.load_org_
    # config). None means no org config repo is configured; kobo runs on
    # this file's values alone.
    org_repo: str | None = None
    runtime_image: str = "registry.example/agents/daedalus-runtime:latest"
    gojira_url: str = "http://127.0.0.1:8080"
    gojira_token: str | None = None
    gojira_cluster: str = "example-cluster"
    gcp_project: str = "example-project"
    vertex_project: str = "example-model-project"
    # The Vertex AI default location is `global` everywhere in the room
    # (owner ruling 2026-11-02); a region such as us-east5 is a per-install
    # or per-model choice, never the default.
    vertex_location: str = "global"
    # A Google Group that confers model access; every agent's identity joins
    # it. Empty (the default): the Vertex role is granted on vertex_project
    # directly, which works on any project, Workspace or not.
    model_group: str = ""
    # Model garden image-generation model + region for agent avatar PNGs
    # (P2d) — a separate location from vertex_location since the avatar
    # model may not be available (or may be cheaper/faster) in a different
    # region than the chat models; project is shared with vertex_project
    # (both hit the same Vertex AI publisher-model surface).
    avatar_model: str = "gemini-2.5-flash-image"
    avatar_location: str = "us-central1"
    # Model garden text-generation model + region for agent name suggestions
    # (create flow v2 addendum) — same auth/transport plumbing family as the
    # avatar client above (VertexNameClient mirrors VertexImageClient's
    # gcloud-token + generateContent idiom for a TEXT call instead of an
    # image one); project is shared with vertex_project, location mirrors
    # avatar_location's own "may differ from vertex_location" reasoning.
    name_model: str = "gemini-2.5-flash"
    name_location: str = "us-central1"
    models: list[ModelChoice] = Field(
        default=[ModelChoice(id="vertex_ai/claude-sonnet-4-5", label="Claude Sonnet 4.5")],
        min_length=1,
    )
    # skills_repo is the bare repo name (e.g., "skills", not "owner/skills")
    # of the org's skills repo (its own publish pipeline, if any, is what
    # actually populates the buckets below — kobo itself never clones it);
    # kobo prefixes it with github_org for every GitHub operation.
    # skills_bucket/personas_bucket are the GCS buckets backing the catalogs
    # kobo-api actually reads from (Task 5). Empty string means unconfigured
    # for all three. These replace the old skills_repo_path/personas_dir
    # local-clone fields (Task 5 deleted the consumers that read a local git
    # checkout/directory in favor of bucket catalogs).
    skills_repo: str | None = None
    skills_bucket: str = ""
    personas_bucket: str = ""
    # Where the catalogs live (issue #208). `gcs` (the default) reads the
    # buckets above through the GCS JSON API with a gcloud token; `directory`
    # maps each bucket name to `<storage_root>/<bucket>/` on local disk
    # (`kobo_api.clients.DirectoryStorageClient`), for a room with no object
    # store: ergon's laptop and team tiers, a dev box, a demo. Deployed rooms
    # change nothing.
    storage_backend: str = "gcs"
    storage_root: str = ""
    # How a created agent is delivered (issue #207). `deployed` (the default)
    # is the repository-and-cluster pipeline. `directory` composes the bundle
    # into `<directory_root>/<agent>/` for a running set of local daedalus
    # processes (ergon) to load, registers it with the switchboard when one
    # is wired, and then tells the running set: a POST to
    # `directory_reload_url` with `{"name", "path"}` when set, otherwise a
    # `.ready` marker file in the agent's folder for a watcher. Destroy for
    # that kind removes the folder and the directory listing, never a
    # namespace or a repository.
    delivery_backend: str = "deployed"
    directory_root: str = ""
    directory_reload_url: str = ""
    # Agent memory replication (resilience arc, Task 14): the object-storage
    # bucket every composed agent's techne database replicates to
    # continuously from birth, under a per-agent prefix (the agent's own
    # name) kobo_api.steps.AccessStep grants write access to and nothing
    # wider -- same "absent = feature off" convention as skills_bucket/
    # personas_bucket above. Empty (the default) means the feature is off
    # room-wide: kobo_api.composer.compose emits no litestream.yml, no
    # entrypoint.sh, and no Dockerfile ENTRYPOINT override for any agent,
    # and AccessStep grants nothing extra -- a composed bundle is
    # byte-identical to one composed before this feature existed.
    memory_replica_bucket: str = ""
    # One runtime, bundles as artifacts (tenishai/kobo#434). The registry path the
    # room publishes each agent's bundle to (`<registry>/<agent>-bundle:current`,
    # beside the runtime image, e.g. `us-east4-docker.pkg.dev/<project>/<repo>`);
    # the pod pulls it at start and a change to the bundle is a restart, not a
    # build. Empty keeps every change a commit the agent's CI builds, as before.
    # Set it only once `runtime_image` pins a runtime that pulls bundles, and
    # the room's identity may write to the registry.
    bundle_registry: str = ""
    # WIF provider + service account identity the org's own skills-publish
    # pipeline (if any) runs as. skills_publish_external True means that
    # pipeline is external to kobo — kobo must never install or modify
    # anything in the skills repo itself.
    skills_publish_wif_provider: str = ""
    skills_publish_service_account: str = ""
    skills_publish_external: bool = False
    # Base URL of the switchboard (cabal) service the room registers agents
    # with and (once wired) relays chat through. None means the switchboard
    # integration is off - kobo runs the legacy direct-to-agent paths
    # unchanged, same "absent = feature off" convention as skills_bucket/
    # personas_bucket above. See kobo_api.clients.cabal_enabled for the full
    # "is this wired" check (both keys here plus the CABAL_OPERATOR_TOKEN
    # env var - the operator token itself is never org-config-settable).
    cabal_url: str | None = None
    # Template for an agent's in-cluster a2a endpoint, with a literal
    # "{name}" placeholder the directory sync (Task 2) fills in via
    # `.format(name=agent.name)` - e.g.
    # "http://agent-{name}.default.svc.cluster.local". None means
    # unconfigured, same as cabal_url.
    agent_endpoint_template: str | None = None
    # This kobo instance's own reachable base URL (bootstrap CLI / org
    # status consumers, Task 3+) — not org.yaml-settable (ORG_KEYS excludes
    # it), same as workdir/db_url below: it describes *this* machine, not
    # anything the org can hand out.
    api_base_url: str = "http://127.0.0.1:8100"
    workdir: Path = Path.home() / ".kobo"
    db_url: str = "sqlite:///kobo.db"
    # Path to the gitignored Slack config-token pair (P2c); None means Slack
    # connect is unconfigured — Clients.real() gates on this to leave slack=None.
    slack_tokens_path: Path | None = None
    # The Kubernetes Secret holding that same pair, as "<namespace>/<name>"
    # (keys `config_token` and `refresh_token`). Set this instead of
    # slack_tokens_path to run more than one replica: a file on a
    # ReadWriteOnce volume is what pins the room to one pod, and rotation
    # rewrites it. Both set = the Secret wins, with a WARNING
    # (kobo_api.clients.slack_token_store). The namespace is explicit, never
    # detected from inside the pod, so the Secret a room reads and writes is
    # always the one its own config names. Deployment-local like
    # slack_tokens_path above and deliberately absent from org.py's ORG_KEYS:
    # it names an object in THIS cluster, not anything the org hands out.
    slack_tokens_secret: str = ""
    # The Kubernetes namespace this room runs in: where it keeps the values
    # an admin pastes under Admin > Integrations, the Slack and GitHub rows
    # (issues #284, #304): the Slack config-token pair and the GitHub app for pull
    # requests, one Secret per service (`kobo-room-slack`,
    # `kobo-room-github-app`) read and written through the same kubectl
    # calls slack_tokens_secret uses. Empty (the default) means the room
    # reads its own namespace from the service-account mount a pod carries;
    # a room with neither has nowhere to hold those values, and the section
    # says so. Deployment-local like slack_tokens_secret above and
    # deliberately absent from org.py's ORG_KEYS. See kobo_api.room_services.
    room_namespace: str = ""
    # The Kubernetes namespace the permission service (bab) runs in: where the
    # room writes the Secret a vendor's room-held credential lives in when the
    # service, not the gateway, mints from it (`bab-datadog` for Datadog,
    # kobo#322), which the service's Deployment mounts as env. Deployment-local
    # like room_namespace above and deliberately absent from org.py's ORG_KEYS:
    # it names an object in THIS cluster.
    bab_namespace: str = "bab"
    # The secrets platform's machine lane (2026-09-22): the service's base
    # URL, the OIDC audience this room's own workload identity mints an ID
    # token for (the lane's only accepted credential), and the team
    # namespace this room's secrets live in over there. All three set means
    # the room stops writing an agent's per-agent Secrets itself and hands
    # the values to that platform instead, which stores them once and syncs
    # them into the agent's namespace under the SAME Secret name the deploy
    # contract already names -- so nothing about a pod changes. Any one of
    # the three empty (the default) means the room keeps writing those
    # Secrets directly, exactly as before: "absent = feature off", the same
    # convention slack_tokens_secret above and skills_bucket hold. See
    # `sero_enabled` below for the one place that check lives, and
    # kobo_api.sero for the lane itself.
    #
    # Deployment-local, deliberately absent from org.py's ORG_KEYS: these
    # name a service and a namespace in THIS cluster, not anything the org
    # hands out.
    sero_url: str = ""
    sero_audience: str = ""
    sero_namespace: str = ""
    # The deploy platform's workload class for a connected agent that has
    # no HTTP port (connected-agents design 2026-09-21): a single-replica
    # workload with no network probes. Empty (the default) means this
    # deploy platform does not offer one yet, and the room refuses that
    # run mode with a plain sentence rather than onboarding something the
    # platform would immediately probe to death -- "absent = feature off",
    # the same convention skills_bucket holds. Deployment-local like
    # slack_tokens_secret above and deliberately absent from org.py's
    # ORG_KEYS: it names a class THIS cluster's deploy platform offers,
    # not anything the org hands out.
    connected_headless_class: str = ""
    # Emails allowed to use the Slack connect routes once IAP is enabled
    # (Task 5) — main.py's `_slack_operator_block` 403s any other caller on
    # both routes, before their existing checks. Empty (the default) leaves
    # Slack connect ungated, matching today's behavior. IAP disabled (no
    # iap_audience) treats every caller as an operator too — the room only
    # trusts an email claim when it has a perimeter that verified it.
    slack_operators: list[str] = []
    # Emails allowed to destroy an agent (`DELETE /api/agents/{name}`) once
    # IAP is enabled — main.py's `_operator_block` 403s any other caller on
    # that route, before its existence/status checks, same semantics family
    # as slack_operators above. Empty (the default) leaves destruction
    # ungated, matching today's behavior. IAP disabled (no iap_audience)
    # treats every caller as an operator too — the room only trusts an email
    # claim when it has a perimeter that verified it.
    operators: list[str] = []
    # Group-based operator status (operator-status-by-group ruling): names
    # from the groups universe below (`Config.groups`, matched against
    # `GroupChoice.name`, never an Okta group name directly) whose members
    # hold operator status room-wide, resolved via the directory
    # (`clients.groups.groups_for`, `kobo_api.main._is_admin`). This is
    # additive, not a replacement: the plain `operators` email list above
    # stays live and keeps granting operator status even with the directory
    # unconfigured or down, same "email operators unaffected" fail-closed
    # contract `_is_admin` documents. Empty (the default) means no admin
    # group is configured, matching today's behavior exactly -- group
    # membership never grants operator status until this is set. Every entry
    # here must name a `groups` entry's `name` (`_admin_groups_must_be_
    # configured_groups` below enforces this at construction) -- an admin
    # group the resolver could never answer for would make every gate it
    # should open a permanent, silent 403 instead. Removing someone from the
    # backing directory group can take up to the resolver's cache TTL (900s
    # default -- `OktaGroupResolver.ttl_s`) to take effect here. Keep at
    # least one `operators` email set as break-glass: a directory outage
    # takes group-based admin status down with it, and an email operator is
    # the only way in that survives that outage.
    admin_groups: list[str] = []
    # The admin panel (`#/admin`: add-ons, integrations, connections, roles,
    # people, audit). Off by default while it is built; `whoami` reports it
    # so the account menu shows the Admin entry only when it is on and the
    # caller is an admin. The server gates the panel's routes on its own.
    admin_panel: bool = False
    # Slack as a vendor on the Access tab (issue #371, phase 2 of #304): the
    # room creates an agent's Slack app itself, Slack posts events to the
    # room's public relay (`kobo_api.slack_relay`) and the room forwards
    # them to the agent, so no app-level token is ever made. Off by default
    # until proven on one room; off means the Slack tab and its paste flow
    # are exactly what they were, and the relay routes answer 404.
    slack_relay: bool = False
    # Where the account menu's "Log out" goes: the identity proxy's sign-out
    # path (for Google's proxy, `/_gcp_iap/clear_login_cookie`). None hides
    # the item, since the room itself holds no session to end.
    logout_url: str | None = None
    # Where an integration lands when connected from the admin panel: the
    # tool gateway's config repository (its `targets.toml` gets the vendor's
    # `[[target]]` as a pull request) and the Kubernetes namespace the
    # gateway runs in (the credential is synced there as a Secret through the
    # secrets add-on). Both empty means a connect records the row and says
    # what is left to do by hand.
    gateway_config_repo: str = ""
    gateway_namespace: str = ""
    # The tool gateway's admin plane (its own in-cluster Service, never the
    # data port). With it set, a lane granted from the admin panel applies
    # at once through `POST /admin/policy/grant`, with the room's own token:
    # the switchboard mints it for persona `kobo-room`, which the gateway's
    # trust registry lists as an admin persona. Empty means grants land only
    # as the pull request on the grants file.
    liminal_admin_url: str = ""
    # Path to a built UI `dist/` directory (e.g. `ui/dist` from `npm run
    # build`) to serve alongside the API, same origin, no CORS (Task 1).
    # None (the default) means dev: no UI is mounted, matching today's setup
    # of a separate dev server proxying /api — the API alone.
    ui_dist: Path | None = None
    # The audience string your identity proxy signs its assertions for
    # (Task 2) — when non-empty, every request (API and UI alike) must carry
    # a verified `x-goog-iap-jwt-assertion` header naming this audience, or
    # the app answers 401 before the request reaches any route. Empty (the
    # default) disables verification entirely, matching today's unauthenticated
    # local dev setup.
    iap_audience: str = ""
    # Empty `iap_audience` runs the room with no authentication at all: every
    # gate's "trusts its perimeter only when it has one" arm allows, and
    # `created_by` is never recorded. That is the local dev shape and most of
    # the test suite, so it stays possible; this switch says it is on purpose.
    # A room that boots open WITHOUT it logs an ERROR at startup (issue #198,
    # `auth_regime` below), so the unauthenticated state is never silent.
    allow_unauthenticated: bool = False
    # GitHub App identity kobo authenticates as when deployed (Task 3),
    # instead of borrowing the operator's ambient `gh auth` session
    # (dev's only mode today) — Clients.real() (kobo_api.clients) wires an
    # InstallationTokenProvider (kobo_api.github_app) into GhCLI iff all
    # three of these are set; any one left empty/unset means dev mode,
    # unchanged.
    github_app_id: str = ""
    github_app_installation_id: str = ""
    # Path to the App's PEM-encoded private key. Read lazily on first token
    # mint, never logged. NEVER put a real key in a tracked file.
    github_app_private_key_path: Path | None = None
    # The agent PR broker's OWN GitHub App (the org's PR app, 2026-08-27
    # spec) -- a second, deliberately separate app identity from the room's
    # repo-management app above: this one's tokens reach OTHER TEAMS' repos
    # (scoped per agent by the broker), so its blast radius is never mixed
    # with the app that owns the agent-* repos. All three set = broker
    # routes live; any one empty/unset = 503, the same absent-equals-off
    # convention as cabal_url.
    pr_app_id: str = ""
    pr_app_installation_id: str = ""
    pr_app_private_key_path: Path | None = None
    # The room's OWN in-cluster base URL, as deployed AGENTS reach it: the
    # /broker/agents/{name}/jira/* lane is called at {broker_url}/broker/...
    # with the agent's colleague JWT. Org-settable (an infra coordinate like
    # cabal_url). The composer no longer reads it: the github broker sidecar
    # retired with the pull request broker (kobo#365, 2026-11-01).
    broker_url: str = ""
    # The room's model gateway (kobo#390): the in-cluster base URL of the
    # LiteLLM proxy the install deploys, as the room reaches its admin plane
    # and as agents reach `/v1/chat/completions`. Org-settable like
    # broker_url above. Empty (the default) = off: the catalogue answers from
    # `models` as it always has and agents reach Vertex AI with their own
    # identity. Set, the catalogue answers from the connected providers
    # (`kobo_api.model_providers`), a new agent's model block points at the
    # gateway with a key the room mints for it, and the per-agent Vertex
    # grant is skipped. Agents born before the key was set are untouched
    # until an owner presses Change model.
    models_gateway_url: str = ""
    # `<namespace>/<name>` of the Kubernetes Secret holding the gateway's
    # master key under `LITELLM_MASTER_KEY`, read through the same kubectl
    # call slack_tokens_secret uses. Deployment-local like slack_tokens_secret
    # and deliberately absent from org.py's ORG_KEYS: it names an object in
    # THIS cluster, and a key's address is never the org's to hand out. The
    # value is read when first needed and never logged.
    models_gateway_master_key_secret: str = ""
    # The observability catalog's Noto link (Task 4): the dashboard base
    # URL and this room's project slug within it, surfaced at
    # GET /api/catalog/observability for the UI to build a deep link from.
    # Org-settable like broker_url/liminal_url above. Both-or-nothing, same
    # convention as cabal_url/agent_endpoint_template: a half-configured
    # pair (only one of the two set) is not actionable, so the route
    # treats it as fully unset rather than emitting a broken link.
    noto_url: str | None = None
    noto_project: str | None = None
    # The MCP gateway's in-cluster base URL, as deployed AGENTS reach its
    # data plane (external-MCP arc). Org-settable like broker_url; empty =
    # the composer emits no liminal block. The token exchange lives at
    # cabal_url (the /auth/liminal-token route), so no second coordinate.
    # SAFE TO SET FLEET-WIDE: a wired agent with no policy grant sees zero
    # tools -- the gateway denies by default; grants are the control point.
    liminal_url: str = ""
    # The room's permission service (bab), as the room reaches it in the
    # cluster. Org-settable. Empty = Access is off: no Permissions section,
    # no Review pane, and the room runs as it does today. The console token
    # it calls with comes from the environment (KOBO_BAB_TOKEN), never from
    # here; both must be present for the client to be wired (kobo_api.bab).
    bab_url: str = ""
    # The accounts, projects and subscriptions the room's permission service
    # can reach, one per vendor it names, so the picker can offer them before
    # a person has connected each one (Access through bab, 2026-10-29). An
    # entry whose `integration` (default: its name) is connected in bab shows
    # as connected; the rest show as not connected yet. Org-settable; later
    # this list is read from the gateway's own roles rather than written here.
    cloud_accounts: list[CloudAccount] = []
    # The Jira read broker (AREQ lane, 2026-09-04) -- the PR broker's
    # custody model applied to tracker reads: the room holds ONE read-only
    # service-account API token (mounted as a file, the PR App key's own
    # convention) and `kobo_api.jira_broker` pins the reachable projects
    # server-side. Deployment-config-settable like pr_app_* above, NOT
    # org.yaml-settable. domain + email + pat_path + at least one project
    # all set = the /broker/.../jira routes live; anything missing = 503,
    # the same absent-equals-off convention as the PR broker.
    jira_domain: str = ""  # e.g. "yourorg.atlassian.net"
    jira_email: str = ""  # the read-only service account's email
    jira_pat_path: Path | None = None  # mounted API-token file, never logged
    jira_projects: list[str] = []  # project keys the broker will read, e.g. ["AREQ"]
    # Old project keys that MIGRATED into a pinned project (a Jira project
    # move keeps every old key as an alias that resolves to the new one).
    # A key with one of these prefixes may be REQUESTED -- historical Slack
    # threads cite them -- but the broker returns the issue only when the
    # RESOLVED key lands inside jira_projects; anything else is refused
    # after the fetch, so the alias lane can never widen the read surface
    # beyond the pinned projects. e.g. ["IE"] after the IE -> AREQ move.
    jira_alias_projects: list[str] = []
    # Agent names allowed to call the jira read routes. There is no
    # per-agent grant table for this lane yet (the CloudAccess/ServiceAccess
    # arc is the real fix); an explicit deployment-config allowlist is the
    # honest interim gate. Absent from this list reads as 404, the
    # anti-enumeration answer the github-token route already gives.
    jira_read_agents: list[str] = []
    # Drop a nightly learnings candidate that names nobody (`said_by` empty)
    # instead of queueing it flagged (issue #143). Leave off until every
    # agent runs a daedalus that marks turns from the room's chat as "user"
    # (daedalus issue #165); before that, whatever anyone taught through the
    # room's chat arrives unattributed too and would be dropped.
    learnings_require_attribution: bool = False
    # How many runs in a row a system task (the nightly learnings pass) may
    # fail for an agent before its card says so (issue #142).
    system_task_flag_after: int = 3
    # How long the room's audit log keeps a row (issue #316). The nightly
    # system task deletes older rows and records one `audit.prune` row
    # saying how many went. Deployment config, not org.yaml: retention is
    # the operator's promise, not a team's preference. Zero keeps everything.
    audit_retention_days: int = 30
    # Global READERS (reader-role wave, 2026-08-28): RAW Okta group names
    # whose members see EVERY agent in the room -- visibility only, no
    # mutation power of any kind. Raw directory names, deliberately NOT
    # config-group names like admin_groups: a directory group may only
    # back one config group, so a lead group inside a team's regional
    # split could never be named in config terms.
    reader_groups: list[str] = Field(default_factory=list)
    # The ACL axis (access-control arc, Task B1) — distinct from `team` (the
    # skills-catalog-derived capability axis; kobo_api.groups' module
    # docstring covers the fail-closed contract this feeds). Each entry's
    # `name` is what the room shows; `okta_groups` (see GroupChoice above)
    # is the LIST of directory group names it maps to -- the directory
    # splits one human team across regional groups, so a room group is the
    # UNION of whichever of those the member is in. kobo_api.groups.
    # GroupResolver filters an Okta membership list down to these and
    # answers with the CONFIG `name`s, never Okta's own names. Empty (the
    # default) means no groups are configured; Clients.real gates
    # OktaGroupResolver construction on this being non-empty, same
    # "absent = feature off" convention as cabal_url/skills_bucket above.
    groups: list[GroupChoice] = []
    # This room's Okta org domain (e.g. "acme.okta.com") group lookups hit:
    # GET https://{okta_domain}/api/v1/users/{email}/groups
    # (kobo_api.groups.OktaGroupResolver). Empty string (the default) means
    # unconfigured, same as skills_bucket above; Clients.real gates
    # OktaGroupResolver construction on this too.
    okta_domain: str = ""
    # Google OAuth + JWT signing coordinates for kobo's MCP server (MCP
    # server arc, Task 1) -- empty `mcp.oauth_client_id` (the default) means
    # the MCP server is off, same "absent = feature off" convention as
    # `cabal_url`/`skills_bucket` above. See `mcp_enabled` and
    # `_mcp_requires_a_workspace_domain_when_enabled` below.
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    # Optional vocabularies for the marketplace frontmatter gate (hardening
    # wave, Task 1) -- kobo_api.marketplace._validate_skill_frontmatter reads
    # allowed_profiles and allowed_tags. Empty lists (the default) add no
    # validation, so the gate is fully functional with this block absent and
    # vocabulary drift can never brick publishing.
    skills_frontmatter: SkillsFrontmatterConfig = Field(default_factory=SkillsFrontmatterConfig)

    @property
    def mcp_enabled(self) -> bool:
        """Whether the MCP server is wired -- a real Google OAuth app is the
        one field only an actually-configured server has, so its presence
        is the switch, same "presence is the switch" posture as
        `cabal_url`/`skills_bucket` above."""
        return bool(self.mcp.oauth_client_id)

    @property
    def sero_enabled(self) -> bool:
        """Whether the secrets platform's machine lane is wired -- ALL
        three coordinates, never a subset: a URL with no audience could
        never authenticate, and a URL with no namespace has nowhere to
        write. A half-configured lane is a config mistake whose only honest
        reading is "off", so it reads as off here rather than failing every
        secret delivery at the first agent that needs one. Same "presence is
        the switch" posture as `mcp_enabled` just above."""
        return bool(self.sero_url and self.sero_audience and self.sero_namespace)

    @field_validator(
        "workdir", "slack_tokens_path", "ui_dist", "github_app_private_key_path",
        "pr_app_private_key_path", "jira_pat_path",
        mode="before",
    )
    @classmethod
    def _expand_user(cls, v):
        """Expand a leading `~` in any of the path-shaped fields.

        One validator for both (rather than a near-identical one per field)
        since the transform is identical: `None` passes through untouched
        (the optional field defaults to it), anything else goes through
        `Path(...).expanduser()`.
        """
        if v is not None:
            return Path(v).expanduser()
        return v

    @model_validator(mode="after")
    def _slack_tokens_secret_names_a_namespace_and_a_name(self) -> Config:
        """A non-empty `slack_tokens_secret` must read `<namespace>/<name>`,
        both halves non-empty and neither carrying whitespace or a second
        slash — the shape `kobo_api.clients.slack_token_store` splits on.

        Same "fail loudly at construction" posture as the validators below:
        a half-written value here (just a name, say) would build a store
        pointed at an empty namespace, and the room would only find out the
        first time Slack's config token expired — the one moment losing the
        pair is unrecoverable, since the refresh token is single-use.
        """
        value = self.slack_tokens_secret
        if not value:
            return self
        namespace, slash, name = value.partition("/")
        if not slash or not namespace or not name or "/" in name:
            raise ValueError(
                f"slack_tokens_secret {value!r} must be '<namespace>/<name>'"
            )
        if any(ch.isspace() for ch in value):
            raise ValueError(
                f"slack_tokens_secret {value!r} must be '<namespace>/<name>' "
                "with no spaces"
            )
        return self

    @model_validator(mode="after")
    def _models_gateway_secret_names_a_namespace_and_a_name(self) -> Config:
        """Same shape rule as `slack_tokens_secret` above, for the same
        reason: a half-written address builds a reader pointed at an empty
        namespace, found out at the first agent's key mint."""
        value = self.models_gateway_master_key_secret
        if not value:
            return self
        namespace, slash, name = value.partition("/")
        if not slash or not namespace or not name or "/" in name or any(ch.isspace() for ch in value):
            raise ValueError(
                f"models_gateway_master_key_secret {value!r} must be '<namespace>/<name>'"
            )
        return self

    @model_validator(mode="after")
    def _admin_groups_must_be_configured_groups(self) -> Config:
        """Every `admin_groups` entry must name a `groups` entry's `name`
        (mirrors `GroupChoice._fold_okta_group_sugar`'s own "fail loudly at
        construction" voice above). `admin_groups` is matched against
        `GroupChoice.name` everywhere it's consulted (`kobo_api.main.
        _is_admin`'s `set(member_groups) & set(cfg.admin_groups)`), and
        `GroupResolver.groups_for` can only ever answer with configured
        `groups` names (`kobo_api.groups._invert_universe`'s okta-name ->
        config-name mapping) -- an `admin_groups` entry with no matching
        `groups` entry names a group the resolver could never return, so the
        gate it should open (destroy, sync-pairs, every owner-or-operator
        check) becomes an unconditional, permanent 403 with nothing in the
        config to explain why. A config mistake, not a valid empty state, so
        construction fails loudly instead of silently defining an admin
        group nobody can ever satisfy.
        """
        known = {g.name for g in self.groups}
        for name in self.admin_groups:
            if name not in known:
                raise ValueError(
                    f"admin_groups entry {name!r} is not a name in `groups` "
                    "(an admin group the resolver could never answer for "
                    "would make every gate it should open a permanent, "
                    "silent 403 instead)"
                )
        return self

    @model_validator(mode="after")
    def _groups_may_not_reuse_the_shared_name(self) -> Config:
        """No `groups` entry may be named `SHARED_GROUP` ("shared") -- that
        name is reserved room-wide for the commons every signed-in caller
        already gets for free, with no directory membership check at all
        (shared-group + group-requests wave). A configured group that
        happened to share the name would collide with the reserved commons
        the moment either `create_agent`'s "shared is always valid" branch
        or `_visible_agents`'s "shared is visible to everyone" branch ran --
        a config mistake, not a valid state, so construction fails loudly
        here instead, the same posture `_admin_groups_must_be_configured_
        groups` above already takes for its own collision class.
        """
        for g in self.groups:
            if g.name == SHARED_GROUP:
                raise ValueError(
                    f"groups entry {g.name!r} collides with the reserved "
                    "commons group name (SHARED_GROUP) -- rename it"
                )
        return self

    @model_validator(mode="after")
    def _delivery_backend_is_known_and_a_directory_has_a_root(self) -> Config:
        if self.delivery_backend not in ("deployed", "directory"):
            raise ValueError(
                f"delivery_backend must be 'deployed' or 'directory', not {self.delivery_backend!r}"
            )
        if self.delivery_backend == "directory" and not self.directory_root:
            raise ValueError("delivery_backend: directory needs directory_root (where agent folders go)")
        return self

    @model_validator(mode="after")
    def _storage_backend_is_known_and_a_directory_has_a_root(self) -> Config:
        if self.storage_backend not in ("gcs", "directory"):
            raise ValueError(
                f"storage_backend must be 'gcs' or 'directory', not {self.storage_backend!r}"
            )
        if self.storage_backend == "directory" and not self.storage_root:
            raise ValueError("storage_backend: directory needs storage_root (the directory the catalogs live under)")
        return self

    @model_validator(mode="after")
    def _mcp_requires_a_workspace_domain_when_enabled(self) -> Config:
        """An enabled MCP server (`mcp.oauth_client_id` set) must also set
        `mcp.workspace_domain` -- the domain check is the org gate on the
        MCP path. An AS with no domain check must refuse to boot, fail-
        closed, same "fail loudly at construction" posture as
        `_admin_groups_must_be_configured_groups` above: a server left able
        to accept a sign-in from any Google account, not just the org's
        own, is a config mistake, not a valid state.
        """
        if self.mcp.oauth_client_id and not self.mcp.workspace_domain:
            raise ValueError(
                "mcp.oauth_client_id is set but mcp.workspace_domain is "
                "empty; the domain check is the org gate on the MCP path"
            )
        return self

    @model_validator(mode="after")
    def _mcp_requires_iap_unless_allowed(self) -> Config:
        """An enabled MCP server on a room with no IAP in front of it (issue
        #198): the MCP lane verifies a Workspace sign-in, but every gate the
        tools then call treats "no perimeter" as trusted, so each Workspace
        member becomes an owner of every agent. Refused at construction
        unless `mcp.allow_without_iap` says a person chose it (local dev)."""
        # Only once the MCP block is otherwise complete, so a missing domain or
        # issuer is reported by its own validator first.
        complete = self.mcp.oauth_client_id and self.mcp.workspace_domain and self.mcp.issuer_base_url
        if complete and not self.iap_audience and not self.mcp.allow_without_iap:
            raise ValueError(
                "mcp.oauth_client_id is set but iap_audience is empty: every Workspace "
                "member who signs in would own every agent. Put the room behind IAP, or "
                "set mcp.allow_without_iap: true to run it open on purpose"
            )
        return self

    def auth_regime(self, *, resolver_configured: bool) -> tuple[str, list[str]]:
        """One line saying who this room lets in, and the list of ERRORs a
        boot should log about it (issue #198). The line is for every boot;
        the list is empty for a room configured on purpose.

        `resolver_configured` is whether a directory group resolver was
        built (`Clients.groups is not None`): it needs `okta_domain` and the
        `OKTA_API_TOKEN` env var, neither of which this object can see alone.
        """
        problems: list[str] = []
        if not self.iap_audience:
            line = "auth: NO perimeter (iap_audience empty): every caller is trusted"
            if not self.allow_unauthenticated:
                problems.append(
                    "the room runs with no authentication (iap_audience is empty) and "
                    "allow_unauthenticated is not set; if this is a dev room say so with "
                    "allow_unauthenticated: true, otherwise set iap_audience"
                )
        else:
            admins = len(self.operators) + len(self.admin_groups)
            line = (
                f"auth: IAP audience set; {len(self.operators)} operator email(s), "
                f"{len(self.admin_groups)} admin group(s), {len(self.groups)} group(s), "
                f"group resolver {'on' if resolver_configured else 'OFF'}"
            )
            if admins == 0:
                problems.append(
                    "iap_audience is set but operators and admin_groups are both empty: "
                    "every signed-in user may destroy any agent"
                )
        if self.groups and not resolver_configured:
            problems.append(
                f"{len(self.groups)} group(s) are configured but no directory resolver is "
                "(okta_domain and the OKTA_API_TOKEN env var are both needed): every "
                "signed-in user sees every agent"
            )
        return line, problems

    @model_validator(mode="after")
    def _mcp_requires_an_issuer_base_url_when_enabled(self) -> Config:
        """An enabled MCP server (`mcp.oauth_client_id` set) must also set
        `mcp.issuer_base_url` -- same fail-closed posture as
        `_mcp_requires_a_workspace_domain_when_enabled` just above: a server
        left with this field empty boots silently serving `issuer: ""` in
        the well-known documents (non-conformant for MCP clients) and falls
        back to trusting the incoming request's own Host header to build
        the Google callback `redirect_uri`
        (`kobo_api.mcp.oauth_as._google_redirect_uri`) -- fine for local
        dev/tests, not a valid state for a real deployment, so construction
        fails loudly instead of booting half-configured.

        CYBER-1132 ride-along: a NON-empty `issuer_base_url` must also be
        `https://`. `kobo_api.mcp.oauth_as._issuer`/`_endpoint` echo this
        value verbatim into the well-known documents' `issuer`,
        `authorization_endpoint`, `token_endpoint`, etc -- so `http://...`
        here is the one in-repo way left to advertise a plain-http OAuth
        surface, config-side, independent of `HttpsOnlyMiddleware`
        (`kobo_api.iap`) refusing the actual traffic. Checked unconditionally
        whenever the field is set (not gated on `oauth_client_id`, unlike the
        empty check above), since a stray http value would be just as wrong
        the moment MCP later gets enabled.

        Exception, deliberate: a `localhost`/`127.0.0.1` host stays allowed
        over http -- dev rooms run the MCP server and its OAuth AS locally
        over plain http with no LB in front to upgrade the scheme, and
        there's no real client relying on that redirect_uri being spoofable,
        so gating dev on a self-signed cert here would just move the pain
        without buying anything.
        """
        if self.mcp.oauth_client_id and not self.mcp.issuer_base_url:
            raise ValueError(
                "mcp.oauth_client_id is set but mcp.issuer_base_url is "
                "empty; issuer_base_url is required for the well-known "
                "documents and the google redirect_uri once mcp is enabled"
            )

        issuer = self.mcp.issuer_base_url
        if issuer and not issuer.startswith("https://"):
            host = urlparse(issuer).hostname
            if host not in ("localhost", "127.0.0.1"):
                raise ValueError(
                    "mcp.issuer_base_url must be https:// -- the OAuth "
                    "discovery documents echo it verbatim"
                )
        return self

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        """Load config from YAML, falling back to pure defaults.

        Resolution order for `path` when not given explicitly:
        1. `KOBO_CONFIG` environment variable (must exist)
        2. `config.local.yaml` beside the current working directory (silent if absent)
        3. Pure defaults (no file touched) if neither exists.
        """
        if path is None:
            env_path = os.environ.get("KOBO_CONFIG")
            if env_path:
                path = Path(env_path)
                if not path.exists():
                    raise FileNotFoundError(f"KOBO_CONFIG points to missing file: {path}")
            else:
                candidate = Path.cwd() / "config.local.yaml"
                if candidate.exists():
                    path = candidate

        if path is None or not Path(path).exists():
            return cls()

        with open(path) as f:
            data = yaml.safe_load(f)

        if data is None:
            data = {}
        elif not isinstance(data, dict):
            raise ValueError(
                f"config file {path} must contain a YAML mapping at the top "
                f"level, got {type(data).__name__}"
            )

        return cls(**data)
