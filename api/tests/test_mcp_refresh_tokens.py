"""Tests for kobo_api.mcp.refresh_tokens (MCP server arc, Task 3): rotating
refresh-token families -- create a family, rotate its live jti on every
use, and revoke the whole family the moment a jti gets replayed.

No HTTP, no OAuth route -- just a plain session and the three functions
(`create_family`, `consume`, `revoke_family`), exercising the properties
the brief calls for: create-then-consume returns a new jti in the same
family; replaying an already-rotated jti kills every row in the family;
an unknown jti, an already-revoked family, and a wrong-email jti all
report back as a plain "no" with no side effects (except the logged
warning on the email-mismatch path).
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.mcp.refresh_tokens import consume, create_family, revoke_family
from kobo_api.models import Base, MCPRefreshToken

EMAIL = "person@example.com"
OTHER_EMAIL = "someone-else@example.com"


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


def _row(session, jti):
    return session.get(MCPRefreshToken, jti)


# --- create_family -----------------------------------------------------


def test_create_family_returns_a_fresh_uuid4_hex_family_id(session):
    family_id = create_family(session, jti="jti-0", email=EMAIL)
    session.commit()

    assert isinstance(family_id, str)
    assert len(family_id) == 32
    # uuid4().hex is exactly 32 lowercase hex characters, no dashes.
    int(family_id, 16)


def test_create_family_persists_a_fresh_unrotated_unrevoked_row(session):
    family_id = create_family(session, jti="jti-0", email=EMAIL)
    session.commit()

    row = _row(session, "jti-0")
    assert row is not None
    assert row.family_id == family_id
    assert row.email == EMAIL
    assert row.rotated_at is None
    assert row.revoked_at is None
    assert row.revoked_reason is None


def test_create_family_two_calls_get_different_family_ids(session):
    first = create_family(session, jti="jti-a", email=EMAIL)
    second = create_family(session, jti="jti-b", email=EMAIL)
    session.commit()

    assert first != second


# --- consume: happy path, rotation --------------------------------------


def test_consume_returns_a_new_jti_in_the_same_family(session):
    family_id = create_family(session, jti="jti-0", email=EMAIL)
    session.commit()

    new_jti = consume(session, jti="jti-0", email=EMAIL)
    session.commit()

    assert new_jti is not None
    assert new_jti != "jti-0"
    new_row = _row(session, new_jti)
    assert new_row is not None
    assert new_row.family_id == family_id
    assert new_row.rotated_at is None
    assert new_row.revoked_at is None


def test_consume_marks_the_old_jti_rotated_and_leaves_it_unrevoked(session):
    create_family(session, jti="jti-0", email=EMAIL)
    session.commit()

    consume(session, jti="jti-0", email=EMAIL)
    session.commit()

    old_row = _row(session, "jti-0")
    assert old_row.rotated_at is not None
    assert old_row.revoked_at is None


# --- consume: replay of an already-rotated jti revokes the family -------


def test_consume_replay_returns_none(session):
    create_family(session, jti="jti-0", email=EMAIL)
    session.commit()
    consume(session, jti="jti-0", email=EMAIL)
    session.commit()

    replay_result = consume(session, jti="jti-0", email=EMAIL)

    assert replay_result is None


def test_consume_replay_revokes_every_row_in_the_family(session):
    create_family(session, jti="jti-0", email=EMAIL)
    session.commit()
    new_jti = consume(session, jti="jti-0", email=EMAIL)
    session.commit()

    consume(session, jti="jti-0", email=EMAIL)
    session.commit()

    old_row = _row(session, "jti-0")
    new_row = _row(session, new_jti)
    assert old_row.revoked_at is not None
    assert old_row.revoked_reason == "replay_detected"
    assert new_row.revoked_at is not None
    assert new_row.revoked_reason == "replay_detected"


def test_consume_after_replay_the_previously_issued_new_jti_no_longer_works(session):
    create_family(session, jti="jti-0", email=EMAIL)
    session.commit()
    new_jti = consume(session, jti="jti-0", email=EMAIL)
    session.commit()
    consume(session, jti="jti-0", email=EMAIL)  # replay: kills the family
    session.commit()

    result = consume(session, jti=new_jti, email=EMAIL)

    assert result is None


# --- consume: unknown jti ------------------------------------------------


def test_consume_unknown_jti_returns_none(session):
    result = consume(session, jti="never-issued", email=EMAIL)

    assert result is None


# --- consume: family already revoked -------------------------------------


def test_consume_after_family_revocation_returns_none(session):
    family_id = create_family(session, jti="jti-0", email=EMAIL)
    session.commit()
    revoke_family(session, family_id=family_id, reason="operator_revoked")
    session.commit()

    result = consume(session, jti="jti-0", email=EMAIL)

    assert result is None


# --- consume: email mismatch never rotates, never revokes ----------------


def test_consume_email_mismatch_returns_none(session):
    create_family(session, jti="jti-0", email=EMAIL)
    session.commit()

    result = consume(session, jti="jti-0", email=OTHER_EMAIL)

    assert result is None


def test_consume_email_mismatch_does_not_mutate_the_row(session):
    create_family(session, jti="jti-0", email=EMAIL)
    session.commit()

    consume(session, jti="jti-0", email=OTHER_EMAIL)

    row = _row(session, "jti-0")
    assert row.rotated_at is None
    assert row.revoked_at is None


def test_consume_email_mismatch_logs_a_warning(session, caplog):
    create_family(session, jti="jti-0", email=EMAIL)
    session.commit()

    with caplog.at_level(logging.WARNING, logger="kobo_api.mcp.refresh_tokens"):
        consume(session, jti="jti-0", email=OTHER_EMAIL)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


# --- revoke_family ---------------------------------------------------------


def test_revoke_family_returns_the_count_of_rows_revoked(session):
    family_id = create_family(session, jti="jti-0", email=EMAIL)
    session.commit()
    consume(session, jti="jti-0", email=EMAIL)
    session.commit()

    revoked_count = revoke_family(session, family_id=family_id, reason="test_reason")

    assert revoked_count == 2


def test_revoke_family_only_counts_rows_not_already_revoked(session):
    family_id = create_family(session, jti="jti-0", email=EMAIL)
    session.commit()
    revoke_family(session, family_id=family_id, reason="first_reason")
    session.commit()

    second_count = revoke_family(session, family_id=family_id, reason="second_reason")

    assert second_count == 0
    row = _row(session, "jti-0")
    assert row.revoked_reason == "first_reason"


def test_revoke_family_does_not_touch_a_different_family(session):
    create_family(session, jti="jti-0", email=EMAIL)
    other_family_id = create_family(session, jti="jti-1", email=EMAIL)
    session.commit()

    revoke_family(session, family_id=other_family_id, reason="test_reason")

    untouched_row = _row(session, "jti-0")
    assert untouched_row.revoked_at is None
