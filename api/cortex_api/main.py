"""FastAPI app factory for cortex-api."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session, sessionmaker

from cortex_api.config import Config
from cortex_api.db import init_db, make_engine, make_session_factory


def get_session(request: Request) -> Iterator[Session]:
    """Dependency yielding a SQLAlchemy session bound to the app's session factory."""
    session_factory: sessionmaker[Session] = request.app.state.session_factory
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


def create_app(
    session_factory: sessionmaker[Session],
    *,
    config: Config,
    clients: "Clients | None" = None,  # noqa: F821, UP037 (Clients arrives in Task 5)
) -> FastAPI:
    app = FastAPI(title="cortex-api")
    app.state.config = config
    app.state.clients = clients
    app.state.session_factory = session_factory

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/api/catalog/models")
    def catalog_models() -> dict:
        cfg: Config = app.state.config
        return {
            "models": [{"id": m.id, "label": m.label} for m in cfg.models],
            "default": cfg.models[0].id,
        }

    return app


def build_default_app() -> FastAPI:
    """Wire the real config and database (clients arrive in Task 5)."""
    config = Config.load(None)
    config.workdir.mkdir(parents=True, exist_ok=True)
    engine = make_engine(config.db_url)
    init_db(engine)
    session_factory = make_session_factory(engine)
    return create_app(session_factory, config=config)
