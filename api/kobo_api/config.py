"""Kobo API configuration.

Loads settings from a YAML file (or pure defaults) into a pydantic model.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class ModelChoice(BaseModel):
    id: str  # LiteLLM route, e.g. vertex_ai/claude-sonnet-4-5
    label: str
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


class Config(BaseModel):
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
    vertex_location: str = "us-east5"
    model_group: str = "models@example.com"
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
    # The room's OWN in-cluster base URL, as deployed AGENTS reach it -- the
    # PR broker's client half (daedalus's github sidecar) calls
    # {broker_url}/broker/... with the agent's colleague JWT. Org-settable
    # (an infra coordinate like cabal_url); empty = the composer emits no
    # github block at all.
    broker_url: str = ""
    # The observability catalog's Argus link (Task 4): the dashboard base
    # URL and this room's project slug within it, surfaced at
    # GET /api/catalog/observability for the UI to build a deep link from.
    # Org-settable like broker_url/liminal_url above. Both-or-nothing, same
    # convention as cabal_url/agent_endpoint_template: a half-configured
    # pair (only one of the two set) is not actionable, so the route
    # treats it as fully unset rather than emitting a broken link.
    argus_url: str | None = None
    argus_project: str | None = None
    # The MCP gateway's in-cluster base URL, as deployed AGENTS reach its
    # data plane (external-MCP arc). Org-settable like broker_url; empty =
    # the composer emits no liminal block. The token exchange lives at
    # cabal_url (the /auth/liminal-token route), so no second coordinate.
    # SAFE TO SET FLEET-WIDE: a wired agent with no policy grant sees zero
    # tools -- the gateway denies by default; grants are the control point.
    liminal_url: str = ""
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
