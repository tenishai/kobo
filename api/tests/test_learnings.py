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
    run_nightly,
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


# --- run_nightly (Task 6) -----------------------------------------------------
#
# `Config`'s `cabal_enabled` is a three-way AND (`cabal_url`,
# `agent_endpoint_template`, and the `CABAL_OPERATOR_TOKEN` env var --
# kobo_api.clients.cabal_enabled) -- the "enabled" helper below sets both
# config keys, and every enabled-cabal test also sets the env var via
# monkeypatch (auto-reverted per test), same convention test_api_agents.py's
# switchboard-relay tests already use. `Agent` needs non-null `persona`/
# `model_id` columns beyond bare `name`/`status` (mirrors test_web_teach.py's
# `_seed_agent` shape) or the seed commit fails a NOT NULL constraint.

from types import SimpleNamespace

from kobo_api.clients import CabalError
from kobo_api.config import Config
from kobo_api.models import Agent, AgentStatus


def _cabal_enabled_config():
    return Config(
        cabal_url="http://cabal.test",
        agent_endpoint_template="http://agent-{name}.default.svc.cluster.local",
    )


def _cabal_disabled_config():
    return Config()


class StubCabal:
    """Relays are recorded; the reply is the same task_to_a2a shape the
    daedalus runtime actually returns."""

    def __init__(self, reply_text):
        self.reply_text = reply_text
        self.calls = []

    def relay(self, name, envelope):
        self.calls.append((name, envelope))
        if isinstance(self.reply_text, Exception):
            raise self.reply_text
        return {
            "kind": "task",
            "id": "t1",
            "status": {
                "state": "completed",
                "message": {
                    "kind": "message",
                    "role": "agent",
                    "parts": [{"kind": "text", "text": self.reply_text}],
                },
            },
            "history": [],
            "metadata": {},
        }


def _app(session_factory_fn, cabal, config):
    return SimpleNamespace(
        state=SimpleNamespace(
            config=config,
            clients=SimpleNamespace(cabal=cabal),
            session_factory=session_factory_fn,
        )
    )


def _seed_ready(session, name="wren"):
    session.add(
        Agent(
            name=name,
            persona="helpful-generalist",
            model_id="vertex_ai/claude-sonnet-4-5",
            status=AgentStatus.READY,
        )
    )
    session.commit()


def test_run_nightly_stores_candidates_for_due_agent(session, monkeypatch):
    # session fixture gives one Session; run_nightly opens its own from the
    # factory, so hand it a factory returning this same session with a
    # no-op close.
    monkeypatch.setattr(session, "close", lambda: None)
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    _seed_ready(session)
    reply = json.dumps({"candidates": [_candidate()]})
    cabal = StubCabal(reply)
    config = _cabal_enabled_config()

    run_nightly(_app(lambda: session, cabal, config))

    assert len(cabal.calls) == 1
    assert cabal.calls[0][0] == "wren"
    assert session.query(AgentLearning).filter_by(agent_name="wren").count() == 1


def test_run_nightly_skips_recently_summarized_agent(session, monkeypatch):
    monkeypatch.setattr(session, "close", lambda: None)
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    _seed_ready(session)
    store_candidates(session, "wren", "2026-09-01", [_candidate()])
    cabal = StubCabal(json.dumps({"candidates": []}))

    run_nightly(_app(lambda: session, cabal, _cabal_enabled_config()))

    assert cabal.calls == []


def test_run_nightly_survives_relay_failure(session, monkeypatch):
    monkeypatch.setattr(session, "close", lambda: None)
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    _seed_ready(session)
    cabal = StubCabal(CabalError("switchboard down"))

    run_nightly(_app(lambda: session, cabal, _cabal_enabled_config()))  # must not raise

    assert session.query(AgentLearning).count() == 0


def test_run_nightly_without_cabal_still_expires(session, monkeypatch):
    monkeypatch.setattr(session, "close", lambda: None)
    session.add(_learning(content_hash="f" * 64))
    session.commit()
    row = session.query(AgentLearning).one()
    row.created_at = (datetime.now(UTC) - timedelta(days=20)).replace(tzinfo=None)
    session.commit()

    run_nightly(_app(lambda: session, None, _cabal_disabled_config()))

    assert session.query(AgentLearning).one().status == LEARNING_EXPIRED
