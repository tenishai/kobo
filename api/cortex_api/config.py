"""Cortex API configuration.

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
    runtime_image: str = "registry.example/agents/daedalus-runtime:latest"
    gojira_url: str = "http://127.0.0.1:8080"
    gojira_token: str | None = None
    gojira_cluster: str = "example-cluster"
    gcp_project: str = "example-project"
    vertex_project: str = "example-model-project"
    vertex_location: str = "us-east5"
    model_group: str = "models@example.com"
    models: list[ModelChoice] = Field(
        default=[ModelChoice(id="vertex_ai/claude-sonnet-4-5", label="Claude Sonnet 4.5")],
        min_length=1,
    )
    skills_repo_path: Path | None = None
    personas_dir: Path | None = None  # None -> package templates/personas
    workdir: Path = Path.home() / ".cortex"
    db_url: str = "sqlite:///cortex.db"
    # Path to the gitignored Slack config-token pair (P2c); None means Slack
    # connect is unconfigured — Clients.real() gates on this to leave slack=None.
    slack_tokens_path: Path | None = None

    @field_validator(
        "workdir", "skills_repo_path", "personas_dir", "slack_tokens_path", mode="before"
    )
    @classmethod
    def _expand_user(cls, v):
        """Expand a leading `~` in any of the path-shaped fields.

        One validator for all of them (rather than a near-identical one per
        field) since the transform is identical: `None` passes through
        untouched (the optional fields default to it), anything else
        goes through `Path(...).expanduser()`.
        """
        if v is not None:
            return Path(v).expanduser()
        return v

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        """Load config from YAML, falling back to pure defaults.

        Resolution order for `path` when not given explicitly:
        1. `CORTEX_CONFIG` environment variable (must exist)
        2. `config.local.yaml` beside the current working directory (silent if absent)
        3. Pure defaults (no file touched) if neither exists.
        """
        if path is None:
            env_path = os.environ.get("CORTEX_CONFIG")
            if env_path:
                path = Path(env_path)
                if not path.exists():
                    raise FileNotFoundError(f"CORTEX_CONFIG points to missing file: {path}")
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
