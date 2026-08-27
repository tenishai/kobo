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


@pytest.mark.skipif(
    not POSTGRES_TEST_URL,
    reason="POSTGRES_TEST_URL not set; skipping the real-Postgres migration proof",
)
def test_baseline_migration_applies_to_empty_postgres_db_and_matches_current_models():
    engine = create_engine(POSTGRES_TEST_URL)
    try:
        with engine.begin() as conn:
            # A fresh slate for this proof run: drop every table alembic's
            # own baseline (plus its own version-tracking table) would
            # otherwise collide with on a second run against the same
            # long-lived scratch database.
            for table in (*reversed(Base.metadata.sorted_tables), "alembic_version"):
                name = table if isinstance(table, str) else table.name
                conn.execute(text(f'DROP TABLE IF EXISTS "{name}" CASCADE'))
    finally:
        engine.dispose()

    _assert_baseline_matches_models(POSTGRES_TEST_URL)
