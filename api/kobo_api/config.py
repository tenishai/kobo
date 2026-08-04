"""Kobo API configuration.

Loads settings from a YAML file (or pure defaults) into a pydantic model.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator


class ModelChoice(BaseModel):
    id: str  # LiteLLM route, e.g. vertex_ai/claude-sonnet-4-5
    label: str


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

    @field_validator("workdir", "slack_tokens_path", "ui_dist", mode="before")
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
