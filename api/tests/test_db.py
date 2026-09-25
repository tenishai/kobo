"""Tests for kobo_api.db's engine construction.

No real database is opened: `create_engine` is replaced with a recorder and
the kwargs it was handed are the assertion, so the Postgres branch is
provable without a Postgres.
"""

from __future__ import annotations

import pytest

from kobo_api import db


@pytest.fixture
def recorded(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def fake_create_engine(url, **kwargs):
        calls.append((url, kwargs))
        return object()

    monkeypatch.setattr(db, "create_engine", fake_create_engine)
    return calls


def test_postgres_engine_pins_the_session_timezone_to_utc(recorded):
    # Both columns the interruption sweep compares (`created_at`, written
    # as an aware datetime, and `run_heartbeat_at`, written naive UTC) land
    # in TIMESTAMP WITHOUT TIME ZONE. A session on any other TimeZone would
    # skew one against the other, silently, against a 60-second cutoff.
    db.make_engine("postgresql+psycopg2://kobo:kobo@localhost:5432/kobo")

    _, kwargs = recorded[0]
    assert kwargs["connect_args"]["options"] == "-c timezone=UTC"
    assert "check_same_thread" not in kwargs["connect_args"]


def test_bare_postgresql_scheme_gets_the_same_option(recorded):
    db.make_engine("postgresql://kobo@localhost/kobo")

    assert recorded[0][1]["connect_args"]["options"] == "-c timezone=UTC"


def test_bare_postgresql_url_names_psycopg2_before_sqlalchemy_picks_a_driver(recorded):
    """SQLAlchemy 2.1 made a bare `postgresql://` mean psycopg v3. The image
    ships psycopg2-binary and nothing else, and the first engine the room
    builds at boot is alembic's, so a build that resolved 2.1 died with
    "No module named 'psycopg'" while the SQLite-backed tests stayed green
    (kobo:6488f3d, 2026-09-25). The driver is named here, once, for
    everyone."""
    db.make_engine("postgresql://kobo:kobo@localhost:5432/kobo")

    url, _ = recorded[0]
    assert url == "postgresql+psycopg2://kobo:kobo@localhost:5432/kobo"


def test_driver_qualified_leaves_an_explicit_driver_and_other_schemes_alone():
    assert (
        db.driver_qualified("postgresql+psycopg2://u:p@h/d") == "postgresql+psycopg2://u:p@h/d"
    )
    assert db.driver_qualified("postgresql+psycopg://u:p@h/d") == "postgresql+psycopg://u:p@h/d"
    assert db.driver_qualified("sqlite:///kobo.db") == "sqlite:///kobo.db"
    assert db.driver_qualified("postgres://u:p@h/d") == "postgres://u:p@h/d"  # not ours to guess


def test_sqlite_engine_keeps_cross_thread_access_and_waits_out_a_busy_lock(recorded):
    # The pipeline thread and its heartbeat ticker write the same file from
    # two threads; the default 5s busy timeout is what turns a slow commit
    # into "database is locked".
    db.make_engine("sqlite:///kobo.db")

    _, kwargs = recorded[0]
    assert kwargs["connect_args"] == {
        "check_same_thread": False,
        "timeout": db.SQLITE_BUSY_TIMEOUT_S,
    }
    assert db.SQLITE_BUSY_TIMEOUT_S == 30


def test_an_unknown_scheme_gets_no_connect_args_of_its_own(recorded):
    db.make_engine("mysql://kobo@localhost/kobo")

    assert recorded[0][1]["connect_args"] == {}
