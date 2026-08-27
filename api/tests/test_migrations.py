"""Tests for the alembic migration path introduced ahead of the Postgres
engine change (Task 11): the schema used to evolve by `create_all` plus a
hand-written `ALTER TABLE` run by an operator, which cannot be trusted
against a real Postgres deployment. This module proves the baseline
migration applies cleanly to an empty database and produces exactly the
schema `kobo_api.models.Base.metadata` describes today, on both engines
this room supports.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, text

from kobo_api.models import Base

API_ROOT = Path(__file__).resolve().parents[1]

POSTGRES_TEST_URL = os.environ.get("POSTGRES_TEST_URL", "").strip()


def _alembic_config(db_url: str) -> AlembicConfig:
    cfg = AlembicConfig(str(API_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def _assert_baseline_matches_models(db_url: str) -> None:
    cfg = _alembic_config(db_url)

    command.upgrade(cfg, "head")

    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            migration_ctx = MigrationContext.configure(conn)
            diff = compare_metadata(migration_ctx, Base.metadata)
        assert diff == [], f"baseline migration does not match current models: {diff}"
    finally:
        engine.dispose()


def test_baseline_migration_applies_to_empty_sqlite_db_and_matches_current_models(tmp_path):
    db_path = tmp_path / "baseline.db"
    _assert_baseline_matches_models(f"sqlite:///{db_path}")


def _drop_everything_postgres(db_url: str) -> None:
    """A fresh slate for a migration proof run against a real, long-lived
    Postgres instance (unlike SQLite's fresh tmp_path file per test): drop
    every table `Base.metadata`'s own models track, alembic's own
    version-tracking table, AND `catalog_fts` (M10 fix).

    `catalog_fts` (`kobo_api.search.ensure_fts`'s own Postgres table) is
    created OUTSIDE `Base.metadata`/alembic's tracked schema entirely, so
    it was never one of the tables this loop dropped -- an ordering flake
    against a shared `POSTGRES_TEST_URL`: whichever test module happens to
    run `test_search.py`'s Postgres path first leaves `catalog_fts`
    sitting in the database, and `compare_metadata` below then reports a
    spurious "table exists in the DB but not in the models" diff that has
    nothing to do with the actual migration under test. This module has no
    business being sensitive to what `test_search.py` did or didn't run
    first, so the fresh slate now covers `catalog_fts` explicitly too.
    """
    engine = create_engine(db_url)
    try:
        with engine.begin() as conn:
            for table in (
                *reversed(Base.metadata.sorted_tables), "alembic_version", "catalog_fts",
            ):
                name = table if isinstance(table, str) else table.name
                conn.execute(text(f'DROP TABLE IF EXISTS "{name}" CASCADE'))
    finally:
        engine.dispose()


@pytest.mark.skipif(
    not POSTGRES_TEST_URL,
    reason="POSTGRES_TEST_URL not set; skipping the real-Postgres migration proof",
)
def test_baseline_migration_applies_to_empty_postgres_db_and_matches_current_models():
    _drop_everything_postgres(POSTGRES_TEST_URL)
    _assert_baseline_matches_models(POSTGRES_TEST_URL)


@pytest.mark.skipif(
    not POSTGRES_TEST_URL,
    reason="POSTGRES_TEST_URL not set; skipping the real-Postgres migration proof",
)
def test_baseline_migration_fresh_slate_survives_a_leftover_catalog_fts_table():
    """M10 fix, the discriminating regression test: simulates the exact
    ordering that flaked -- `catalog_fts` already sitting in the database
    (as if `test_search.py`'s own Postgres path ran first against this
    same shared `POSTGRES_TEST_URL`) -- and proves the fresh slate this
    module now runs drops it too, so `compare_metadata` stays clean
    regardless of what ran here before."""
    from kobo_api.search import ensure_fts

    engine = create_engine(POSTGRES_TEST_URL)
    try:
        ensure_fts(engine)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT to_regclass('catalog_fts')")).scalar() is not None
    finally:
        engine.dispose()

    _drop_everything_postgres(POSTGRES_TEST_URL)
    _assert_baseline_matches_models(POSTGRES_TEST_URL)
