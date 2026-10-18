"""The admin panel's Add-ons: what this room runs on, one card each, read
from the org keys that already turn each one on (design `docs/superpowers/
specs/2026-10-18-one-runtime-and-admin-panel-design.md`, Arc 2, step 2;
issue #173).

An add-on is something the room runs on; an integration is something an
agent reaches out to. Absent means off, as every integration in the room
already works, so a card's state is derived, never stored: `on` when its
keys are set (and, where a cheap probe exists, reachable), `off` when they
are not, `error` when set but unreachable. Nothing here writes anything.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from .clients import ClientError, Clients, cabal_enabled
from .config import Config


@dataclass(frozen=True)
class AddonCard:
    id: str
    name: str
    summary: str
    """One sentence, what it does for the room."""
    state: str
    """`on` | `off` | `error`."""
    detail: str
    """One sentence, why it is in that state."""
    keys: list[str] = field(default_factory=list)
    """The org keys (or env names) that turn it on; what a person edits."""
    owner: str = "this team"
    """Who ships it: `this team`, or a sibling team's name, so the pane says
    whose door to knock on."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "summary": self.summary,
            "state": self.state,
            "detail": self.detail,
            "keys": list(self.keys),
            "owner": self.owner,
        }


def _deploy_platform(config: Config, clients: Clients | None) -> AddonCard:
    base = {
        "id": "deploy-platform",
        "name": "Deploy platform",
        "summary": "Turns each agent's repository into a running deployment and keeps it there.",
        "keys": ["gojira_cluster", "gojira_url", "gojira_token"],
        "owner": "platform team",
    }
    if not config.gojira_cluster:
        return AddonCard(state="off", detail="No cluster configured; Create cannot run without it.", **base)
    ok = False
    if clients is not None:
        try:
            ok = clients.gojira.ping()
        except ClientError:
            ok = False
    if not ok:
        return AddonCard(state="error", detail="Configured, but the platform did not answer just now.", **base)
    return AddonCard(state="on", detail=f"Connected to {config.gojira_cluster}.", **base)


def _secrets(config: Config) -> AddonCard:
    base = {
        "id": "secrets",
        "name": "Secrets",
        "summary": "Holds each agent's secrets in the vault and syncs them into its namespace.",
        "keys": ["sero_url", "sero_audience", "sero_namespace"],
    }
    if not config.sero_url:
        return AddonCard(state="off", detail="Not configured; agents keep secrets as plain Kubernetes Secrets.", **base)
    return AddonCard(state="on", detail=f"Vault lane at {config.sero_url}.", **base)


def _observability(config: Config) -> AddonCard:
    base = {
        "id": "observability",
        "name": "Observability",
        "summary": "Dashboards, logs and alerts for every agent, linked from its page.",
        "keys": ["noto_url", "noto_project"],
    }
    if not (config.noto_url and config.noto_project):
        missing = [k for k, v in (("noto_url", config.noto_url), ("noto_project", config.noto_project)) if not v]
        return AddonCard(state="off", detail=f"Not configured ({', '.join(missing)} unset); agent pages show no dashboard link.", **base)
    return AddonCard(state="on", detail=f"Dashboards at {config.noto_url}.", **base)


def _switchboard(config: Config) -> AddonCard:
    base = {
        "id": "switchboard",
        "name": "Switchboard",
        "summary": "Lets agents find and ask each other, and registers each one in the directory.",
        "keys": ["cabal_url", "agent_endpoint_template", "CABAL_OPERATOR_TOKEN"],
        "owner": "orchestration team",
    }
    if cabal_enabled(config):
        return AddonCard(state="on", detail=f"Directory at {config.cabal_url}.", **base)
    if config.cabal_url and not os.environ.get("CABAL_OPERATOR_TOKEN"):
        return AddonCard(state="error", detail="URL set but the operator token is missing from the environment.", **base)
    return AddonCard(state="off", detail="Not configured; agents work alone.", **base)


def _tool_gateway(config: Config) -> AddonCard:
    base = {
        "id": "tool-gateway",
        "name": "Tool gateway",
        "summary": "Gives agents their tools behind one door, scoped per agent by grants.",
        "keys": ["liminal_url"],
        "owner": "gateway team",
    }
    if not config.liminal_url:
        return AddonCard(state="off", detail="Not configured; agents are composed with no gateway block.", **base)
    return AddonCard(state="on", detail=f"Gateway at {config.liminal_url}.", **base)


def _memory(config: Config) -> AddonCard:
    return AddonCard(
        id="memory",
        name="Memory",
        summary="Each agent's own store of what it was taught and learned, kept on its volume.",
        state="on",
        detail="Built into the runtime; every composed agent has it.",
        keys=["runtime_image", "memory_replica_bucket"],
        owner="memory team",
    )


def _model_gateway(config: Config) -> AddonCard:
    base = {
        "id": "model-gateway",
        "name": "Model gateway",
        "summary": "One place agents reach their models through, with the room's keys.",
        "keys": ["broker_url"],
    }
    if not config.broker_url:
        return AddonCard(state="off", detail="Not configured; agents reach models with their own credentials.", **base)
    return AddonCard(state="on", detail=f"Models through {config.broker_url}.", **base)


def addon_cards(config: Config, clients: Clients | None) -> list[dict[str, Any]]:
    """Every add-on card, in the order the pane shows them. Never raises: a
    probe that fails is an `error` card, not a failed page."""
    return [
        card.as_dict()
        for card in (
            _deploy_platform(config, clients),
            _secrets(config),
            _observability(config),
            _switchboard(config),
            _tool_gateway(config),
            _memory(config),
            _model_gateway(config),
        )
    ]
