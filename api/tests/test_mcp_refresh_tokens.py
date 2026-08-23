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


# --- consume: lost race on the guarded UPDATE falls back to a replay ----


def test_a_lost_update_race_revokes_the_family_as_replay(session_factory, monkeypatch):
    """The rowcount-guarded UPDATE exists for exactly this: a caller whose
    own pre-read looked rotatable, but who loses the race to another
    caller that rotates the same jti first. Two Session objects share one
    file-backed sqlite engine (via `session_factory`) so each can hold its
    own view of the same row.

    Session B takes its pre-read before A ever touches the row, then A
    runs a full rotation (rotate + insert child + commit). B then calls
    `consume()` for the same jti. To deterministically land B in the
    FALLBACK branch (rowcount 0 after a clean-looking pre-check) rather
    than the pre-check branch, B's own `session.get` is monkeypatched to
    keep handing back its stale pre-read for this jti, no matter how many
    times `consume()` calls it internally.

    Note: the brief's preferred approach was relying on SQLAlchemy's
    identity map alone (seed it with a pre-read, let `consume()`'s own
    `session.get()` hit the cache). That was tried first and found to be
    genuinely flaky here: across repeated runs of the identical test body,
    B's internal `session.get()` sometimes returned the stale cached row
    (landing in the fallback, as intended) and sometimes returned a fresh
    row reflecting A's committed rotation (landing in the pre-check
    instead) -- apparently timing-dependent on how the shared engine's
    connection pool hands out and reclaims connections between the two
    sessions. A test that only sometimes exercises the branch it exists to
    prove is worse than not having it, so this uses the explicitly
    sanctioned fallback: monkeypatching the lookup seam directly.
    """
    session_a = session_factory()
    session_b = session_factory()
    try:
        create_family(session_a, jti="jti-0", email=EMAIL)
        session_a.commit()

        # B's clean-looking pre-read, taken before A rotates anything.
        # Detached so it can't be silently refreshed by anything B's
        # session does later.
        stale_row = session_b.get(MCPRefreshToken, "jti-0")
        session_b.expunge(stale_row)

        # A completes a full rotation and commits it to disk.
        new_jti = consume(session_a, jti="jti-0", email=EMAIL)
        session_a.commit()
        assert new_jti is not None

        # Pin B's session.get for this jti to always return the stale,
        # pre-rotation snapshot -- deterministically reproducing "B's
        # pre-check saw an unrotated row" regardless of identity-map or
        # connection-pool timing. Every other call (there are none here)
        # falls through to the real session.get.
        real_get = session_b.get

        def _stale_get(model, pk):
            if model is MCPRefreshToken and pk == "jti-0":
                return stale_row
            return real_get(model, pk)

        monkeypatch.setattr(session_b, "get", _stale_get)

        # B now runs consume() for the same jti. Its internal session.get
        # returns the pinned stale row, so B sails past the "already
        # rotated" pre-check and reaches the guarded UPDATE, which runs
        # for real against the database and matches zero rows (A already
        # rotated jti-0 on disk) -- the fallback branch, not the
        # pre-check, is what fires here.
        result = consume(session_b, jti="jti-0", email=EMAIL)
        session_b.commit()

        assert result is None

        session_a.expire_all()
        original_row = session_a.get(MCPRefreshToken, "jti-0")
        child_row = session_a.get(MCPRefreshToken, new_jti)
        assert original_row.revoked_at is not None
        assert original_row.revoked_reason == "replay_detected"
        assert child_row.revoked_at is not None
        assert child_row.revoked_reason == "replay_detected"
    finally:
        session_a.close()
        session_b.close()


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
