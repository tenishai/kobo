"""Alembic environment: resolves its target URL from kobo_api's own Config
(the same `db_url` the app itself connects with) rather than a value
hardcoded in alembic.ini, so `alembic upgrade head` always targets whatever
backend the room is actually configured for (SQLite for local dev, Postgres
once deployed) with no separate migrations-only setting to keep in sync.

URL resolution order:
1. Whatever the alembic `Config` object already carries for
   `sqlalchemy.url` -- e.g. a caller that built its own `AlembicConfig` and
   called `set_main_option("sqlalchemy.url", ...)` before invoking a
   command (this room's own migration tests do exactly that, so they never
   touch whatever `kobo.db`/org config the ambient environment happens to
   have lying around).
2. `KOBO_DB_URL` env var -- a plain override for a one-off CLI run against
   a scratch database.
3. `kobo_api.config.Config.load()`'s own `db_url` -- the normal path (plain
   `alembic upgrade head` from a checkout), using the exact same resolution
   the app itself uses (`KOBO_CONFIG` env var, else `config.local.yaml`,
   else pure defaults).

`render_as_batch=True` is set unconditionally: SQLite cannot run most ALTER
TABLE forms directly, so every migration in this tree that touches an
existing column uses `op.batch_alter_table(...)`, which alembic executes as
a real in-place ALTER on Postgres and as SQLite's own copy-and-swap dance on
SQLite -- one migration script, correct on both engines.
"""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from kobo_api.config import Config
from kobo_api.models import Base

config = context.config

# Deliberately NOT calling logging.config.fileConfig(config.config_file_name)
# here, unlike alembic's own generated template: fileConfig() replaces the
# process's ROOT logging configuration wholesale, which is harmless for a
# one-shot `alembic upgrade head` CLI invocation but is process-global state
# that outlives a single call when this module runs inside the app's own
# test process (kobo_api's migration tests invoke `alembic.command.upgrade`
# in-process) -- it broke every OTHER test module's `caplog` fixture for the
# rest of that pytest session (root logger handlers/levels got clobbered by
# alembic.ini's [loggers]/[handlers] sections). Alembic's own log statements
# just fall back to whatever logging config the caller (this app, or pytest)
# already has, which is exactly the right default for an embedded caller.
target_metadata = Base.metadata


def _db_url() -> str:
    configured = config.get_main_option("sqlalchemy.url")
    if configured:
        return configured
    override = os.environ.get("KOBO_DB_URL")
    if override:
        return override
    return Config.load().db_url


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live DB connection (`alembic upgrade
    head --sql`). Not this room's normal path (migrations run online,
    against a real connection) but kept for parity with alembic's own
    generated template, since it costs nothing to support."""
    url = _db_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _db_url()
    connectable = engine_from_config(
        configuration, prefix="sqlalchemy.", poolclass=pool.NullPool
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
