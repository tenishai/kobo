"""SQLAlchemy engine/session plumbing for kobo-api.

File-based SQLite with `check_same_thread=False` (gojira's threading pattern) so a
single engine can be shared across FastAPI's threaded request handlers.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from kobo_api.models import Base

# How long a SQLite connection waits for a write lock before giving up.
# The default is 5s, which a single-process room never noticed -- but the
# provisioning heartbeat now writes the agents table from a second thread
# while the pipeline is committing its own rows, and "database is locked"
# there would cost a beat (or, on the pipeline's side, a step). Only ever
# a WAIT: it changes nothing when there is no contention.
SQLITE_BUSY_TIMEOUT_S = 30

_BARE_POSTGRES = "postgresql://"
_PSYCOPG2_POSTGRES = "postgresql+psycopg2://"


def driver_qualified(url: str) -> str:
    """The URL with its Postgres driver named, so SQLAlchemy's default cannot
    choose one for us.

    A bare `postgresql://` leaves the driver to SQLAlchemy, and SQLAlchemy 2.1
    moved that default from psycopg2 to psycopg (v3). The image ships
    psycopg2-binary, the driver every deployed room has run on, so a bare URL
    is read as psycopg2 here, in the one place the app, alembic and the
    schema tripwire all take their engine from. A URL that already names a
    driver, and every non-Postgres URL, is returned untouched.
    """
    if url.startswith(_BARE_POSTGRES):
        return _PSYCOPG2_POSTGRES + url[len(_BARE_POSTGRES) :]
    return url


def make_engine(url: str) -> Engine:
    url = driver_qualified(url)
    connect_args: dict[str, object] = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        connect_args["timeout"] = SQLITE_BUSY_TIMEOUT_S
    elif url.startswith("postgresql"):
        # Pin the SESSION's timezone, not just the server's default. Two
        # columns the interruption sweep (kobo_api.provisioner) compares
        # against one 60-second cutoff are written differently -- an agent's
        # `created_at` as an aware datetime, its `run_heartbeat_at` as naive
        # UTC -- and both land in TIMESTAMP WITHOUT TIME ZONE, which keeps
        # whatever wall-clock numbers it is given. A session running on any
        # other TimeZone would skew one against the other by hours, quietly,
        # and the only symptom would be live runs swept (or dead ones never
        # swept) near a boundary.
        connect_args["options"] = "-c timezone=UTC"
    return create_engine(url, connect_args=connect_args)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)
