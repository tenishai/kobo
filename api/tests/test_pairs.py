"""Tests for `kobo_api.pairs.allowed_pairs` (access-control arc, Task C1):
the pure derivation of the flat caller-may-reach-target pairs the
switchboard will eventually enforce. No HTTP, no `Clients` -- just a plain
session and a plain `Config`, exercising the properties the brief calls
for: same-group symmetry (both directions), invite directionality (never
implied backward), revocation removes the pair, and group-less agents never
produce an implicit pair with anyone (including each other).
"""

from __future__ import annotations

import logging

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.clients import CabalError, Clients
from kobo_api.config import SHARED_GROUP, Config
from kobo_api.main import _boot_sync_dispatch_pairs
from kobo_api.models import A2AInvite, Agent, AgentStatus, Base
from kobo_api.pairs import (
    allowed_pairs,
    implicitly_paired,
    sync_dispatch_pairs,
    sync_dispatch_pairs_best_effort,
)
from tests.fakes import FakeCabal

PERSONA = "helpful-generalist"
MODEL_ID = "vertex_ai/claude-sonnet-4-5"


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def session(session_factory):
    s = session_factory()
    yield s
    s.close()


@pytest.fixture
def config(tmp_path):
    return Config(workdir=tmp_path / "work")


def _agent(session, name, *, group_name=None, status=AgentStatus.READY):
    agent = Agent(
        name=name,
        persona=PERSONA,
        model_id=MODEL_ID,
        status=status,
        repo_full_name=f"acme/agent-{name}",
        group_name=group_name,
    )
    session.add(agent)
    session.commit()
    return agent


def _invite(session, *, from_agent, to_agent, status):
    invite = A2AInvite(from_agent=from_agent, to_agent=to_agent, status=status)
    session.add(invite)
    session.commit()
    return invite


# --- Same-group pairs: implicit, both directions ----------------------------


def test_same_group_pair_is_symmetric_both_directions(session, config):
    _agent(session, "a", group_name="growth")
    _agent(session, "b", group_name="growth")

    pairs = allowed_pairs(session, config)

    assert ("agent:a", "b") in pairs
    assert ("agent:b", "a") in pairs


def test_same_group_pairs_do_not_include_self_pairs(session, config):
    _agent(session, "a", group_name="growth")
    _agent(session, "b", group_name="growth")

    pairs = allowed_pairs(session, config)

    assert ("agent:a", "a") not in pairs
    assert ("agent:b", "b") not in pairs


def test_different_groups_produce_no_pair(session, config):
    _agent(session, "a", group_name="growth")
    _agent(session, "b", group_name="payments-eng")

    pairs = allowed_pairs(session, config)

    assert pairs == set()


def test_group_less_agents_produce_no_pair_with_anyone_including_each_other(
    session, config
):
    _agent(session, "a", group_name=None)
    _agent(session, "b", group_name=None)
    _agent(session, "c", group_name="growth")

    pairs = allowed_pairs(session, config)

    assert pairs == set()


def test_three_agents_same_group_all_pairs_present(session, config):
    _agent(session, "a", group_name="growth")
    _agent(session, "b", group_name="growth")
    _agent(session, "c", group_name="growth")

    pairs = allowed_pairs(session, config)

    expected = {
        ("agent:a", "b"), ("agent:b", "a"),
        ("agent:a", "c"), ("agent:c", "a"),
        ("agent:b", "c"), ("agent:c", "b"),
    }
    assert pairs == expected


# --- implicitly_paired: the one predicate every call site (allowed_pairs'
# own loop below, and kobo_api.main's create_invite/list_requestable_agents/
# _team_pairs) shares -- fix round, shared-group wave HIGH ------------------


def test_implicitly_paired_true_for_same_non_null_group():
    assert implicitly_paired("growth", "growth") is True


def test_implicitly_paired_false_for_different_groups():
    assert implicitly_paired("growth", "payments-eng") is False


def test_implicitly_paired_false_when_either_side_is_group_less():
    assert implicitly_paired(None, "growth") is False
    assert implicitly_paired("growth", None) is False
    assert implicitly_paired(None, None) is False


def test_implicitly_paired_false_for_shared_group_even_though_equal():
    # The one exception: SHARED_GROUP is a real, equal, non-null group on
    # both sides, but the commons stays invite-gated -- the whole point of
    # this predicate existing as a single shared definition.
    assert implicitly_paired(SHARED_GROUP, SHARED_GROUP) is False


# --- SHARED_GROUP: commons agents get no implicit pair from anyone,
# including each other (shared-group + group-requests wave) -----------------


def test_two_shared_agents_produce_no_implicit_pair(session, config):
    _agent(session, "a", group_name=SHARED_GROUP)
    _agent(session, "b", group_name=SHARED_GROUP)

    pairs = allowed_pairs(session, config)

    assert pairs == set()


def test_shared_agent_and_real_group_agent_produce_no_pair(session, config):
    _agent(session, "a", group_name=SHARED_GROUP)
    _agent(session, "b", group_name="growth")

    pairs = allowed_pairs(session, config)

    assert pairs == set()


def test_shared_agent_alongside_a_real_same_group_pair_only_pairs_the_latter(
    session, config
):
    # A commons agent sitting in the mix must not suppress OTHER agents'
    # legitimate same-group pairs -- only its own would-be pairs vanish.
    _agent(session, "a", group_name=SHARED_GROUP)
    _agent(session, "b", group_name="growth")
    _agent(session, "c", group_name="growth")

    pairs = allowed_pairs(session, config)

    assert pairs == {("agent:b", "c"), ("agent:c", "b")}


def test_accepted_invite_still_grants_a_pair_touching_a_shared_agent(session, config):
    # An invite is group-agnostic (pinned above for group-less agents) --
    # a commons agent is no exception: the wire stays consent-based, not
    # blanket-closed, for a commons endpoint that explicitly asked for one.
    _agent(session, "a", group_name=SHARED_GROUP)
    _agent(session, "b", group_name="growth")
    _invite(session, from_agent="a", to_agent="b", status="accepted")

    pairs = allowed_pairs(session, config)

    assert pairs == {("agent:a", "b")}


# --- Accepted invites: directional, never implied backward ------------------


def test_accepted_invite_adds_only_the_forward_direction(session, config):
    _agent(session, "a", group_name="growth")
    _agent(session, "b", group_name="payments-eng")
    _invite(session, from_agent="a", to_agent="b", status="accepted")

    pairs = allowed_pairs(session, config)

    assert ("agent:a", "b") in pairs
    assert ("agent:b", "a") not in pairs


def test_pending_invite_grants_no_pair(session, config):
    _agent(session, "a", group_name="growth")
    _agent(session, "b", group_name="payments-eng")
    _invite(session, from_agent="a", to_agent="b", status="pending")

    pairs = allowed_pairs(session, config)

    assert pairs == set()


def test_declined_invite_grants_no_pair(session, config):
    _agent(session, "a", group_name="growth")
    _agent(session, "b", group_name="payments-eng")
    _invite(session, from_agent="a", to_agent="b", status="declined")

    pairs = allowed_pairs(session, config)

    assert pairs == set()


def test_revoked_invite_grants_no_pair(session, config):
    _agent(session, "a", group_name="growth")
    _agent(session, "b", group_name="payments-eng")
    _invite(session, from_agent="a", to_agent="b", status="revoked")

    pairs = allowed_pairs(session, config)

    assert pairs == set()


def test_revoking_a_previously_accepted_invite_removes_the_pair(session, config):
    # Same row, status flipped after the fact -- mirrors what the revoke
    # route actually does (UPDATE, not DELETE) rather than modeling it as a
    # fresh row.
    _agent(session, "a", group_name="growth")
    _agent(session, "b", group_name="payments-eng")
    invite = _invite(session, from_agent="a", to_agent="b", status="accepted")
    assert ("agent:a", "b") in allowed_pairs(session, config)

    invite.status = "revoked"
    session.commit()

    assert allowed_pairs(session, config) == set()


def test_accepted_invite_between_group_less_agents_still_grants_the_pair(
    session, config
):
    # An invite is group-agnostic: it doesn't require either side to belong
    # to any group at all, only that both are currently READY.
    _agent(session, "a", group_name=None)
    _agent(session, "b", group_name=None)
    _invite(session, from_agent="a", to_agent="b", status="accepted")

    pairs = allowed_pairs(session, config)

    assert pairs == {("agent:a", "b")}


# --- Destroyed/absent agents drop out naturally -----------------------------


def test_non_ready_agent_produces_no_same_group_pair(session, config):
    _agent(session, "a", group_name="growth", status=AgentStatus.PROVISIONING)
    _agent(session, "b", group_name="growth")

    pairs = allowed_pairs(session, config)

    assert pairs == set()


def test_invite_naming_a_destroyed_agent_drops_out(session, config):
    # "Destroyed" here means the row is simply gone (kobo deletes an
    # agent's row outright on full teardown, per AgentStatus's own
    # docstring) -- the invite row survives (its own history), but the
    # named agent no longer resolves to a READY row.
    _agent(session, "b", group_name="payments-eng")
    _invite(session, from_agent="a", to_agent="b", status="accepted")

    pairs = allowed_pairs(session, config)

    assert pairs == set()


def test_no_agents_or_invites_returns_empty_set(session, config):
    assert allowed_pairs(session, config) == set()


# --- sync_dispatch_pairs: the raising core (Task C4) -------------------------
#
# `sync_dispatch_pairs(cabal, session, config)` just computes `allowed_
# pairs` and hands it to `CabalClient.put_dispatch_pairs` -- it never
# tolerates a failure itself (mirrors `kobo_api.directory.sync_directory`'s
# own raising contract), leaving every caller (below, and `kobo_api.main`/
# `kobo_api.destroy`/`kobo_api.provisioner`) to decide its own fail-soft
# story via `sync_dispatch_pairs_best_effort`.


def test_sync_dispatch_pairs_pushes_the_computed_set(session, config):
    _agent(session, "a", group_name="growth")
    _agent(session, "b", group_name="growth")
    fake_cabal = FakeCabal()

    sync_dispatch_pairs(fake_cabal, session, config)

    assert fake_cabal.put_dispatch_pairs_calls == [
        sorted({("agent:a", "b"), ("agent:b", "a")})
    ]


def test_sync_dispatch_pairs_returns_the_pushed_count(session, config):
    # Final-review fix round, HIGH-2: the operator resync route reports
    # this count straight back to the caller, and the boot-time sync logs
    # it -- `sync_dispatch_pairs` must hand back whatever `put_dispatch_
    # pairs` itself returned, not just fire-and-forget.
    _agent(session, "a", group_name="growth")
    _agent(session, "b", group_name="growth")
    fake_cabal = FakeCabal()

    count = sync_dispatch_pairs(fake_cabal, session, config)

    assert count == 2


def test_sync_dispatch_pairs_pushes_empty_set_when_nothing_is_allowed(session, config):
    fake_cabal = FakeCabal()

    sync_dispatch_pairs(fake_cabal, session, config)

    assert fake_cabal.put_dispatch_pairs_calls == [[]]


def test_sync_dispatch_pairs_propagates_cabal_error(session, config):
    fake_cabal = FakeCabal(put_dispatch_pairs_error="switchboard unreachable")

    with pytest.raises(CabalError):
        sync_dispatch_pairs(fake_cabal, session, config)


# --- sync_dispatch_pairs_best_effort: the shared fail-soft seam --------------
#
# The ONE helper every pair-mutating trigger in this room calls (invite
# accept/revoke in kobo_api.main, the destroy-forget step in kobo_api.
# destroy, the READY flip in kobo_api.provisioner): skip entirely when the
# switchboard isn't wired, and never let a failure escape to whatever
# triggered the call.


def _enabled_cabal_config(config):
    return config.model_copy(
        update={
            "cabal_url": "http://cabal.internal",
            "agent_endpoint_template": "http://agent-{name}.default.svc.cluster.local",
        }
    )


def _clients_with_cabal(cabal):
    return Clients(github=None, gojira=None, cloud=None, agent=None, kubectl=None, cabal=cabal)


def test_best_effort_skips_entirely_when_switchboard_not_configured(session, config):
    # Neither org key set, no CABAL_OPERATOR_TOKEN -- cabal_enabled is
    # false, so this must never touch the (present) cabal client at all.
    fake_cabal = FakeCabal()
    clients = _clients_with_cabal(fake_cabal)

    sync_dispatch_pairs_best_effort(clients, config, session)

    assert fake_cabal.put_dispatch_pairs_calls == []


def test_best_effort_skips_when_config_enabled_but_client_missing(
    session, config, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_cabal_config(config)
    clients = _clients_with_cabal(None)

    sync_dispatch_pairs_best_effort(clients, cfg, session)  # must not raise (no client to call)


def test_best_effort_pushes_when_fully_wired(session, config, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_cabal_config(config)
    _agent(session, "a", group_name="growth")
    _agent(session, "b", group_name="growth")
    fake_cabal = FakeCabal()
    clients = _clients_with_cabal(fake_cabal)

    sync_dispatch_pairs_best_effort(clients, cfg, session)

    assert fake_cabal.put_dispatch_pairs_calls == [
        sorted({("agent:a", "b"), ("agent:b", "a")})
    ]


def test_best_effort_swallows_cabal_error_and_logs_a_warning(session, config, monkeypatch, caplog):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_cabal_config(config)
    fake_cabal = FakeCabal(put_dispatch_pairs_error="switchboard unreachable")
    clients = _clients_with_cabal(fake_cabal)

    with caplog.at_level(logging.WARNING):
        sync_dispatch_pairs_best_effort(clients, cfg, session)  # must not raise

    assert any(rec.levelname == "WARNING" for rec in caplog.records)


def test_best_effort_swallows_a_raw_httpx_transport_error(session, config, monkeypatch, caplog):
    # Not only a typed CabalError -- a raw httpx transport failure (the
    # switchboard unreachable at the network level, never wrapped into
    # CabalError since CabalClient's own non-2xx handling never runs) must
    # be tolerated here too, same as every other fail-soft cabal caller in
    # this codebase (kobo_api.destroy's _destroy_directory, kobo_api.main's
    # _sync_directory_best_effort).
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_cabal_config(config)
    fake_cabal = FakeCabal()

    def _raise_transport_error(pairs):
        raise httpx.ConnectError("connection refused")

    fake_cabal.put_dispatch_pairs = _raise_transport_error
    clients = _clients_with_cabal(fake_cabal)

    with caplog.at_level(logging.WARNING):
        sync_dispatch_pairs_best_effort(clients, cfg, session)  # must not raise

    assert any(rec.levelname == "WARNING" for rec in caplog.records)


# --- sync_dispatch_pairs_best_effort's return value (final-review fix
# round, HIGH-2): the pushed count on success, None when skipped or failed
# -- the boot-time sync (kobo_api.main._boot_sync_dispatch_pairs) is the
# one caller that reads this; every pre-existing trigger still ignores it.


def test_best_effort_returns_none_when_switchboard_not_configured(session, config):
    fake_cabal = FakeCabal()
    clients = _clients_with_cabal(fake_cabal)

    assert sync_dispatch_pairs_best_effort(clients, config, session) is None


def test_best_effort_returns_the_pushed_count_on_success(session, config, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_cabal_config(config)
    _agent(session, "a", group_name="growth")
    _agent(session, "b", group_name="growth")
    fake_cabal = FakeCabal()
    clients = _clients_with_cabal(fake_cabal)

    assert sync_dispatch_pairs_best_effort(clients, cfg, session) == 2


def test_best_effort_returns_none_on_a_swallowed_failure(session, config, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_cabal_config(config)
    fake_cabal = FakeCabal(put_dispatch_pairs_error="switchboard unreachable")
    clients = _clients_with_cabal(fake_cabal)

    assert sync_dispatch_pairs_best_effort(clients, cfg, session) is None


def test_best_effort_retries_on_the_next_call_after_a_failure_even_with_unchanged_pairs(
    session, config, monkeypatch
):
    # The reconciling-retry guarantee (HIGH-2b): a failed sync must not
    # wedge this seam shut -- the NEXT call (an organic trigger, or the
    # boot-time sync) tries again and succeeds, even though nothing about
    # the underlying pairs changed between the two calls.
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_cabal_config(config)
    _agent(session, "a", group_name="growth")
    _agent(session, "b", group_name="growth")
    fake_cabal = FakeCabal(put_dispatch_pairs_error="switchboard unreachable")
    clients = _clients_with_cabal(fake_cabal)

    first = sync_dispatch_pairs_best_effort(clients, cfg, session)
    assert first is None
    assert len(fake_cabal.put_dispatch_pairs_calls) == 1

    fake_cabal._put_dispatch_pairs_error = None  # the switchboard recovers
    second = sync_dispatch_pairs_best_effort(clients, cfg, session)

    assert second == 2
    assert len(fake_cabal.put_dispatch_pairs_calls) == 2
    assert fake_cabal.put_dispatch_pairs_calls[0] == fake_cabal.put_dispatch_pairs_calls[1]


# --- Boot-time sync (kobo_api.main._boot_sync_dispatch_pairs, final-review
# fix round, HIGH-2a): pushes once at process boot, right beside the
# boot-time org-config clone, so enforcement doesn't wait on an organic
# trigger that might never come. -------------------------------------------


def test_boot_sync_skips_when_switchboard_not_wired(session_factory, config):
    fake_cabal = FakeCabal()
    clients = _clients_with_cabal(fake_cabal)

    _boot_sync_dispatch_pairs(clients, config, session_factory)

    assert fake_cabal.put_dispatch_pairs_calls == []


def test_boot_sync_pushes_the_current_pairs_when_wired(session_factory, config, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_cabal_config(config)
    session = session_factory()
    _agent(session, "a", group_name="growth")
    _agent(session, "b", group_name="growth")
    session.close()
    fake_cabal = FakeCabal()
    clients = _clients_with_cabal(fake_cabal)

    _boot_sync_dispatch_pairs(clients, cfg, session_factory)

    assert fake_cabal.put_dispatch_pairs_calls == [
        sorted({("agent:a", "b"), ("agent:b", "a")})
    ]


def test_boot_sync_logs_the_pushed_count(session_factory, config, monkeypatch, caplog):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_cabal_config(config)
    session = session_factory()
    _agent(session, "a", group_name="growth")
    _agent(session, "b", group_name="growth")
    session.close()
    fake_cabal = FakeCabal()
    clients = _clients_with_cabal(fake_cabal)

    with caplog.at_level(logging.INFO):
        _boot_sync_dispatch_pairs(clients, cfg, session_factory)

    assert any("2" in rec.message for rec in caplog.records)


def test_boot_sync_never_raises_when_the_push_fails(session_factory, config, monkeypatch, caplog):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_cabal_config(config)
    fake_cabal = FakeCabal(put_dispatch_pairs_error="switchboard unreachable")
    clients = _clients_with_cabal(fake_cabal)

    with caplog.at_level(logging.WARNING):
        _boot_sync_dispatch_pairs(clients, cfg, session_factory)  # must not raise

    assert any(rec.levelname == "WARNING" for rec in caplog.records)
