"""Tests for `kobo_api.kinds` and the three `Agent` columns it names.

`kind` is the one marker four separate readers consult (the pipeline
selector, the destroy ledger, both read routes, the UI's tab model), so
the value strings themselves are pinned here rather than left to whatever
each reader happens to spell -- the `archetype` precedent, which is
compared against its named constant and never truth-tested.

The default matters as much as the values: every row that predates this
column, and every row the ordinary create route writes, is `composed`.
"""

from __future__ import annotations

from kobo_api.kinds import (
    KIND_COMPOSED,
    KIND_CONNECTED,
    RUN_MODE_HEADLESS,
    RUN_MODE_HTTP,
    is_connected,
)
from kobo_api.models import Agent


def _agent(**kwargs) -> Agent:
    return Agent(name="clio", persona="helpful-generalist", model_id="model-1", **kwargs)


def test_the_kind_and_run_mode_values_are_exactly_these_four_strings():
    assert KIND_COMPOSED == "composed"
    assert KIND_CONNECTED == "connected"
    assert RUN_MODE_HTTP == "http"
    assert RUN_MODE_HEADLESS == "headless"


def test_a_stored_agent_row_defaults_to_composed_with_no_run_mode_and_no_secrets(
    session_factory,
):
    session = session_factory()
    try:
        session.add(_agent())
        session.commit()
        agent = session.query(Agent).filter_by(name="clio").one()

        assert agent.kind == KIND_COMPOSED
        assert agent.run_mode is None
        assert agent.secret_names == []
    finally:
        session.close()


def test_is_connected_is_false_for_a_composed_row_and_true_for_a_connected_one():
    assert is_connected(_agent()) is False
    assert is_connected(_agent(kind=KIND_COMPOSED)) is False
    assert is_connected(_agent(kind=KIND_CONNECTED)) is True


def test_a_connected_row_stores_its_run_mode_and_secret_names(session_factory):
    session = session_factory()
    try:
        session.add(
            _agent(
                kind=KIND_CONNECTED,
                run_mode=RUN_MODE_HEADLESS,
                secret_names=["clio-api", "clio-db"],
            )
        )
        session.commit()
        agent = session.query(Agent).filter_by(name="clio").one()

        assert agent.kind == KIND_CONNECTED
        assert agent.run_mode == RUN_MODE_HEADLESS
        assert agent.secret_names == ["clio-api", "clio-db"]
    finally:
        session.close()
