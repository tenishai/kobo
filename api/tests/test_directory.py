"""Tests for kobo_api.directory: `sync_directory` (fetch the card from the
agent's repo, graduate) and `retire_directory` (absorb) — the directory seam
shared by DirectoryStep, the destroy stage, the metadata tails, and the sync
route (colleagues switchboard plan, Task 2). Against the fakes
(tests/fakes.py), no real git/switchboard involved.
"""

from __future__ import annotations

import pytest

from kobo_api.clients import CabalError, ClientError
from kobo_api.config import Config
from kobo_api.directory import retire_directory, sync_directory
from kobo_api.models import Agent
from tests.fakes import FakeCabal, FakeGitHub

AGENT_NAME = "clio"
PERSONA = "helpful-generalist"
MODEL_ID = "vertex_ai/claude-sonnet-4-5"
REPO = "acme/agent-clio"
ENDPOINT_TEMPLATE = "http://agent-{name}.default.svc.cluster.local"


def _agent(**overrides) -> Agent:
    fields = {
        "name": AGENT_NAME,
        "persona": PERSONA,
        "model_id": MODEL_ID,
        "repo_full_name": REPO,
    }
    fields.update(overrides)
    return Agent(**fields)


def _config(**overrides) -> Config:
    fields = {"agent_endpoint_template": ENDPOINT_TEMPLATE}
    fields.update(overrides)
    return Config(**fields)


# --- sync_directory ---------------------------------------------------------------


def test_sync_directory_clones_the_agents_own_repo():
    github = FakeGitHub()
    cabal = FakeCabal()

    sync_directory(github, cabal, _config(), _agent())

    assert len(github.cloned) == 1
    assert github.cloned[0][0] == REPO


def test_sync_directory_card_is_fetched_verbatim_not_recomposed():
    """The exact dict `clone` writes to `bundle/agent-card.json` must reach
    `graduate` byte-identically — never rebuilt via BundleSpec/
    kobo_api.composer, which could drift from whatever the runtime actually
    serves at its own well-known endpoint."""
    card = {
        "name": AGENT_NAME,
        "description": "hand-edited on purpose, not compose's default",
        "version": "9.9.9",
        "capabilities": ["something-unusual"],
        "skills": [],
        "metadata": {"team": "acme-team", "purpose_tags": ["billing"], "capability": "Helps."},
    }
    github = FakeGitHub(agent_card=card)
    cabal = FakeCabal()

    sync_directory(github, cabal, _config(), _agent())

    assert cabal.graduate_calls[0]["card"] == card


def test_sync_directory_registers_with_name_endpoint_and_audience():
    github = FakeGitHub()
    cabal = FakeCabal()

    sync_directory(github, cabal, _config(), _agent())

    assert cabal.graduate_calls == [
        {
            "name": AGENT_NAME,
            "endpoint": f"http://agent-{AGENT_NAME}.default.svc.cluster.local",
            "audience": AGENT_NAME,
            "card": cabal.graduate_calls[0]["card"],
        }
    ]


def test_sync_directory_endpoint_uses_the_configured_template_and_agent_name():
    github = FakeGitHub()
    cabal = FakeCabal()

    sync_directory(
        github,
        cabal,
        _config(agent_endpoint_template="http://agent-{name}.svc.cluster.local:8080"),
        _agent(),
    )

    assert (
        cabal.graduate_calls[0]["endpoint"]
        == f"http://agent-{AGENT_NAME}.svc.cluster.local:8080"
    )


def test_sync_directory_clone_failure_propagates_and_never_calls_graduate():
    github = FakeGitHub(clone_error="gh: rate limited")
    cabal = FakeCabal()

    with pytest.raises(ClientError, match="rate limited"):
        sync_directory(github, cabal, _config(), _agent())

    assert cabal.graduate_calls == []


def test_sync_directory_graduate_failure_propagates():
    github = FakeGitHub()
    cabal = FakeCabal(graduate_error="switchboard unreachable")

    with pytest.raises(CabalError):
        sync_directory(github, cabal, _config(), _agent())


# --- retire_directory --------------------------------------------------------------


def test_retire_directory_absorbs_the_agent_by_name():
    cabal = FakeCabal()

    retire_directory(cabal, _agent())

    assert cabal.absorb_calls == [AGENT_NAME]


def test_retire_directory_propagates_a_non_tolerated_failure():
    cabal = FakeCabal(absorb_error="switchboard unreachable")

    with pytest.raises(CabalError):
        retire_directory(cabal, _agent())
