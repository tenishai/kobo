"""Daily learnings: model, validation, storage, lifecycle, scheduling."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from kobo_api.models import (
    LEARNING_PENDING,
    AgentLearning,
    Base,
)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    yield session
    session.close()


def _learning(**overrides):
    defaults = dict(
        agent_name="wren",
        day="2026-09-01",
        content="the canary gate is manual",
        kind="fact",
        source="Slack thread, Sep 1",
        evidence="gate is manual now",
        content_hash="a" * 64,
    )
    defaults.update(overrides)
    return AgentLearning(**defaults)


def test_learning_row_roundtrips_with_pending_default(session):
    session.add(_learning())
    session.commit()

    row = session.query(AgentLearning).one()

    assert row.status == LEARNING_PENDING
    assert row.decided_by is None
    assert isinstance(row.created_at, datetime)


def test_same_agent_same_hash_is_rejected(session):
    session.add(_learning())
    session.commit()
    session.add(_learning(day="2026-09-02"))

    with pytest.raises(IntegrityError):
        session.commit()
