"""Cortex API configuration.

Loads settings from a YAML file (or pure defaults) into a pydantic model.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel


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
    models: list[ModelChoice] = [
        ModelChoice(id="vertex_ai/claude-sonnet-4-5", label="Claude Sonnet 4.5")
    ]
    skills_repo_path: Path | None = None
    personas_dir: Path | None = None  # None -> package templates/personas
    workdir: Path = Path.home() / ".cortex"
    db_url: str = "sqlite:///cortex.db"

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        """Load config from YAML, falling back to pure defaults.

        Resolution order for `path` when not given explicitly:
        1. `CORTEX_CONFIG` environment variable
        2. `config.local.yaml` beside the current working directory
        3. Pure defaults (no file touched) if neither exists.
        """
        if path is None:
            env_path = os.environ.get("CORTEX_CONFIG")
            if env_path:
                path = Path(env_path)
            else:
                candidate = Path.cwd() / "config.local.yaml"
                if candidate.exists():
                    path = candidate

        if path is None or not Path(path).exists():
            return cls()

        with open(path) as f:
            data = yaml.safe_load(f) or {}

        return cls(**data)
