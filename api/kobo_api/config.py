"""Kobo API configuration.

Loads settings from a YAML file (or pure defaults) into a pydantic model.
"""

from __future__ import annotations

import os
from pathlib import Path

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

    @field_validator(
        "workdir", "slack_tokens_path", "ui_dist", "github_app_private_key_path",
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
