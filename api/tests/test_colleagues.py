"""Tests for kobo_api.colleagues: `deliver_pass` — mints a colleague-scoped
switchboard token for an agent and delivers it to the agent's own namespace
as a Secret (Task 3/B3: every agent is born with a colleague pass). Against
the fakes (tests/fakes.py), no real switchboard/kubectl involved.
"""

from __future__ import annotations

import pytest

from kobo_api.clients import CabalError, ClientError
from kobo_api.colleagues import deliver_pass
from kobo_api.composer import CABAL_COLLEAGUE_TOKEN_ENV
from kobo_api.models import Agent
from tests.fakes import FakeCabal, FakeKubectl, FakeSero

AGENT_NAME = "clio"
PERSONA = "helpful-generalist"
MODEL_ID = "vertex_ai/claude-sonnet-4-5"
CREATOR = "someone@example.com"


def _agent(**overrides) -> Agent:
    fields = {"name": AGENT_NAME, "persona": PERSONA, "model_id": MODEL_ID}
    fields.update(overrides)
    return Agent(**fields)


def test_deliver_pass_mints_for_the_agents_own_name():
    cabal = FakeCabal()
    kubectl = FakeKubectl()

    deliver_pass(kubectl, cabal, _agent())

    assert cabal.mint_colleague_calls == [AGENT_NAME]


def test_deliver_pass_applies_secret_named_agent_dash_cabal_in_agents_namespace():
    cabal = FakeCabal()
    kubectl = FakeKubectl()

    deliver_pass(kubectl, cabal, _agent())

    assert len(kubectl.secrets_applied) == 1
    namespace, name, _literals = kubectl.secrets_applied[0]
    assert namespace == AGENT_NAME
    assert name == f"{AGENT_NAME}-cabal"


def test_deliver_pass_secret_carries_the_minted_token_under_the_token_env_key():
    cabal = FakeCabal(mint_colleague_result="minted-token-value")
    kubectl = FakeKubectl()

    deliver_pass(kubectl, cabal, _agent())

    _namespace, _name, literals = kubectl.secrets_applied[0]
    assert literals == {CABAL_COLLEAGUE_TOKEN_ENV: "minted-token-value"}
    # The literal env var name every composed agent's `tools.colleagues.
    # token_env` points at (kobo_api.composer._tools_block) — pinned here so
    # the two sides (compose-time config, delivery-time secret key) can
    # never silently drift apart.
    assert CABAL_COLLEAGUE_TOKEN_ENV == "CABAL_COLLEAGUE_TOKEN"


def test_deliver_pass_different_agent_names_get_their_own_secret():
    cabal = FakeCabal()
    kubectl = FakeKubectl()

    deliver_pass(kubectl, cabal, _agent(name="rae"))

    assert cabal.mint_colleague_calls == ["rae"]
    namespace, name, _literals = kubectl.secrets_applied[0]
    assert namespace == "rae"
    assert name == "rae-cabal"


def test_deliver_pass_is_idempotent_by_construction_a_rerun_reapplies():
    """apply_secret upserts; calling deliver_pass again (a retried WireStep)
    never fails on an already-existing secret name — it re-mints (accepted,
    per the plan) and overwrites."""
    cabal = FakeCabal()
    kubectl = FakeKubectl()

    deliver_pass(kubectl, cabal, _agent())
    deliver_pass(kubectl, cabal, _agent())  # must not raise

    assert cabal.mint_colleague_calls == [AGENT_NAME, AGENT_NAME]
    assert len(kubectl.secrets_applied) == 2
    assert kubectl.secrets_applied[0][1] == kubectl.secrets_applied[1][1] == f"{AGENT_NAME}-cabal"


def test_deliver_pass_propagates_mint_failure_as_cabal_error_before_delivering():
    cabal = FakeCabal(mint_colleague_error="switchboard unreachable")
    kubectl = FakeKubectl()

    with pytest.raises(CabalError):
        deliver_pass(kubectl, cabal, _agent())

    # Never got as far as attempting to deliver anything to kubectl.
    assert kubectl.secrets_applied == []


def test_deliver_pass_propagates_secret_delivery_failure_as_client_error():
    cabal = FakeCabal()
    kubectl = FakeKubectl(apply_secret_error="kubectl apply failed")

    with pytest.raises(ClientError):
        deliver_pass(kubectl, cabal, _agent())


def test_deliver_pass_never_leaks_the_minted_token_in_a_raised_message():
    cabal = FakeCabal(mint_colleague_result="ultra-secret-value")
    kubectl = FakeKubectl(apply_secret_error="kubectl apply failed")

    with pytest.raises(ClientError) as exc_info:
        deliver_pass(kubectl, cabal, _agent())

    assert "ultra-secret-value" not in str(exc_info.value)


# --- through the secrets platform's machine lane (kobo_api.sero) -------------


def test_deliver_pass_through_the_lane_stores_the_token_and_syncs_the_same_secret():
    cabal = FakeCabal(mint_colleague_result="minted-token-value")
    kubectl = FakeKubectl()
    sero = FakeSero()

    deliver_pass(kubectl, cabal, _agent(created_by=CREATOR), sero)

    # Stored under the agent's own path, recorded against the human who
    # created it.
    assert sero.puts == [
        (
            CREATOR,
            f"apps/{AGENT_NAME}/cabal",
            {CABAL_COLLEAGUE_TOKEN_ENV: "minted-token-value"},
        )
    ]
    # Synced back as the SAME Secret name and namespace the direct write
    # used, so gojira.yaml's secrets_from entry and the pod are untouched.
    assert sero.syncs == [
        (
            CREATOR,
            AGENT_NAME,
            f"apps/{AGENT_NAME}/cabal",
            AGENT_NAME,
            f"{AGENT_NAME}-cabal",
        )
    ]


def test_deliver_pass_through_the_lane_writes_no_secret_directly():
    # Never both paths: an agent whose Secret has two writers has two
    # sources of truth, and the next sync would undo a human's repair.
    cabal = FakeCabal()
    kubectl = FakeKubectl()

    deliver_pass(kubectl, cabal, _agent(created_by=CREATOR), FakeSero())

    assert kubectl.secrets_applied == []


def test_deliver_pass_still_mints_exactly_once_through_the_lane():
    cabal = FakeCabal()

    deliver_pass(FakeKubectl(), cabal, _agent(created_by=CREATOR), FakeSero())

    assert cabal.mint_colleague_calls == [AGENT_NAME]


def test_deliver_pass_lane_failure_fails_the_step_with_no_fallback_write():
    cabal = FakeCabal()
    kubectl = FakeKubectl()
    sero = FakeSero(put_error="the secrets platform is unavailable right now")

    with pytest.raises(ClientError) as exc_info:
        deliver_pass(kubectl, cabal, _agent(created_by=CREATOR), sero)

    assert "unavailable" in str(exc_info.value)
    assert kubectl.secrets_applied == []


def test_deliver_pass_lane_sync_failure_also_fails_with_no_fallback_write():
    cabal = FakeCabal()
    kubectl = FakeKubectl()
    sero = FakeSero(sync_error="the cluster refused the delivery")

    with pytest.raises(ClientError):
        deliver_pass(kubectl, cabal, _agent(created_by=CREATOR), sero)

    assert kubectl.secrets_applied == []


def test_deliver_pass_lane_failure_never_leaks_the_minted_token():
    cabal = FakeCabal(mint_colleague_result="ultra-secret-value")
    sero = FakeSero(put_error="denied")

    with pytest.raises(ClientError) as exc_info:
        deliver_pass(FakeKubectl(), cabal, _agent(created_by=CREATOR), sero)

    assert "ultra-secret-value" not in str(exc_info.value)


def test_deliver_pass_through_the_lane_refuses_an_agent_with_no_creator():
    # Nobody to record the write against is a refusal, not a default: the
    # lane exists so every stored secret has a person's name on it.
    cabal = FakeCabal()
    kubectl = FakeKubectl()
    sero = FakeSero()

    with pytest.raises(ClientError) as exc_info:
        deliver_pass(kubectl, cabal, _agent(created_by=None), sero)

    assert AGENT_NAME in str(exc_info.value)
    assert sero.puts == []
    assert kubectl.secrets_applied == []


# --- the earlier passes are taken back ----------------------------------------------


def test_deliver_pass_revokes_the_agents_earlier_passes_before_minting_the_new_one():
    """A re-wire used to add one more live 365-day pass per run and take
    none back (the destroy-orphans audit, 2026-09-21). The switchboard now
    refuses every token a subject was issued before a cutoff, and this
    asks for that cutoff BEFORE minting, so the new pass is the only one
    that works and never falls to its own revocation."""
    cabal = FakeCabal()
    deliver_pass(FakeKubectl(), cabal, _agent())
    assert cabal.revoke_colleague_calls == [AGENT_NAME]
    assert cabal.calls == [f"revoke:{AGENT_NAME}", f"mint:{AGENT_NAME}"]


def test_deliver_pass_stops_before_minting_when_the_revocation_is_refused():
    """A pass minted over a refused revocation is one more live credential
    than there should be; the step fails first and mints nothing."""
    cabal = FakeCabal(revoke_colleague_error="switchboard down")
    kubectl = FakeKubectl()
    with pytest.raises(CabalError):
        deliver_pass(kubectl, cabal, _agent())
    assert cabal.mint_colleague_calls == []
    assert kubectl.secrets_applied == []
