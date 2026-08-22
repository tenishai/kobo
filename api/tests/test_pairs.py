"""Tests for `kobo_api.pairs.allowed_pairs` (access-control arc, Task C1):
the pure derivation of the flat caller-may-reach-target pairs the
switchboard will eventually enforce. No HTTP, no `Clients` -- just a plain
session and a plain `Config`, exercising the properties the brief calls
for: same-group symmetry (both directions), invite directionality (never
implied backward), revocation removes the pair, and group-less agents never
produce an implicit pair with anyone (including each other).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.config import Config
from kobo_api.models import A2AInvite, Agent, AgentStatus, Base
from kobo_api.pairs import allowed_pairs

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
