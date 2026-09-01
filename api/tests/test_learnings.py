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


import json
from datetime import timedelta

from kobo_api.clients import _build_learnings_envelope
from kobo_api.learnings import (
    CANDIDATE_CAP,
    CONTENT_MAX_CHARS,
    content_hash,
    due_agents,
    expire_stale,
    parse_reply,
    seconds_until_next_utc_midnight,
    store_candidates,
)
from kobo_api.models import LEARNING_DENIED, LEARNING_EXPIRED

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _candidate(content="the canary gate is manual", **overrides):
    base = dict(content=content, kind="fact", source="Slack, Sep 1", evidence="quote")
    base.update(overrides)
    return base


def test_parse_reply_valid_and_garbage():
    good = json.dumps({"candidates": [_candidate()]})
    assert len(parse_reply(good)) == 1
    assert parse_reply("not json at all") == []
    assert parse_reply(json.dumps({"candidates": "x"})) == []


def test_parse_reply_clips_and_caps():
    long_one = json.dumps({"candidates": [_candidate(content="y" * 999)]})
    assert len(parse_reply(long_one)[0]["content"]) == CONTENT_MAX_CHARS
    many = json.dumps({"candidates": [_candidate(content=f"c{i}") for i in range(30)]})
    assert len(parse_reply(many)) == CANDIDATE_CAP


def test_store_candidates_skips_known_hash_any_status(session):
    stored = store_candidates(session, "wren", "2026-09-01", [_candidate()])
    assert stored == 1
    row = session.query(AgentLearning).one()
    row.status = LEARNING_DENIED
    session.commit()

    again = store_candidates(session, "wren", "2026-09-02", [_candidate()])

    assert again == 0
    assert session.query(AgentLearning).count() == 1


def test_store_candidates_hash_is_sha256_of_content(session):
    store_candidates(session, "wren", "2026-09-01", [_candidate()])
    row = session.query(AgentLearning).one()
    assert row.content_hash == content_hash("the canary gate is manual")


def test_expire_stale_flips_only_old_pending(session):
    session.add(_learning(content_hash="b" * 64))
    session.commit()
    old = session.query(AgentLearning).one()
    old.created_at = (NOW - timedelta(days=15)).replace(tzinfo=None)
    session.add(_learning(content_hash="c" * 64))
    session.commit()

    flipped = expire_stale(session, NOW)

    assert flipped == 1
    statuses = {r.content_hash[0]: r.status for r in session.query(AgentLearning).all()}
    assert statuses["b"] == LEARNING_EXPIRED
    assert statuses["c"] == LEARNING_PENDING


def test_due_agents_no_rows_or_stale_rows(session):
    session.add(_learning(agent_name="fresh", content_hash="d" * 64))
    session.commit()
    fresh_row = session.query(AgentLearning).filter_by(agent_name="fresh").one()
    fresh_row.created_at = (NOW - timedelta(hours=2)).replace(tzinfo=None)
    session.add(_learning(agent_name="stale", content_hash="e" * 64))
    session.commit()
    stale_row = session.query(AgentLearning).filter_by(agent_name="stale").one()
    stale_row.created_at = (NOW - timedelta(hours=30)).replace(tzinfo=None)
    session.commit()

    due = due_agents(session, ["fresh", "stale", "never"], NOW)

    assert due == ["stale", "never"]


def test_seconds_until_next_utc_midnight():
    assert seconds_until_next_utc_midnight(NOW) == 12 * 3600
    almost = datetime(2026, 9, 1, 23, 59, 59, tzinfo=UTC)
    assert seconds_until_next_utc_midnight(almost) == 60.0  # floor


def test_learnings_envelope_shape():
    envelope = _build_learnings_envelope("2026-09-01")

    assert envelope["metadata"]["system_task"] == "learnings"
    assert envelope["metadata"]["date"] == "2026-09-01"
    assert "2026-09-01" in envelope["status"]["message"]["parts"][0]["text"]
