"""What an agent is to Google Cloud: one service account, used one of two ways.

Every agent has the account `agent-<name>@<project>`. Until 2026-11-24 the
agent's pod ran as it (workload identity). That is still how every agent born
before then runs, and how every agent runs in a room that has not named the
permission service's Google identity.

The other way (bab issue 93): the pod runs as its Kubernetes service account
and nothing else. What it needs in Google Cloud, which is its own memory
prefix, is granted to that Kubernetes identity directly. The Google account
is then a name and nothing runs as it, which is what lets the permission
service ask for a lease *through* it: Google's own log then records the
agent's account on every call the lease makes. An account a pod runs as could
not be that name, because it would hold the right to ask for the token itself.

The choice is made once, when the agent is created, and kept on the row
(`agents.cloud_identity`), so a retry or a re-wire of an older agent never
moves it.
"""
from __future__ import annotations

from kobo_api.clients import gsa_email
from kobo_api.config import Config
from kobo_api.model_providers import gateway_on

# `agents.cloud_identity`. NULL: the pod runs as the agent's Google account.
DIRECT = "direct"

SERVICEACCOUNT_YAML_TEMPLATE = """\
# App-authored (preserved by the generator): the agent's identity.
# Workload Identity binds this KSA to the agent's GSA, whose group
# membership grants model access.
apiVersion: v1
kind: ServiceAccount
metadata:
  name: agent-{name}
  namespace: {name}
  annotations:
    iam.gke.io/gcp-service-account: {gsa_email}
"""

SERVICEACCOUNT_DIRECT_YAML_TEMPLATE = """\
# App-authored (preserved by the generator): the agent's identity.
# The agent runs as this account and no Google service account: what it
# needs in Google Cloud is granted to this account directly, and the rest
# it borrows through the room's permission service.
apiVersion: v1
kind: ServiceAccount
metadata:
  name: agent-{name}
  namespace: {name}
"""


def for_a_new_agent(config: Config) -> str | None:
    """How an agent created now is known to Google Cloud. Direct only where
    all three hold: the room names the permission service's Google identity
    (the opt-in), that service is on, and the model gateway is on, since an
    agent that reaches Vertex itself joins a Google group to do it and only
    a service account can be a group's member."""
    if config.bab_service_account and config.bab_url and gateway_on(config):
        return DIRECT
    return None


def runs_direct(agent) -> bool:
    return getattr(agent, "cloud_identity", None) == DIRECT


def service_account_manifest(agent, config: Config) -> str:
    """The agent's Kubernetes service account file, as the room writes it."""
    if runs_direct(agent):
        return SERVICEACCOUNT_DIRECT_YAML_TEMPLATE.format(name=agent.name)
    return SERVICEACCOUNT_YAML_TEMPLATE.format(
        name=agent.name, gsa_email=gsa_email(f"agent-{agent.name}", config.gcp_project)
    )
