"""FastAPI app factory for cortex-api."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from cortex_api.catalog import NAME_RE, list_personas, list_skills
from cortex_api.config import Config
from cortex_api.db import init_db, make_engine, make_session_factory
from cortex_api.models import Agent, AgentStatus
from cortex_api.provisioner import (
    PIPELINE,
    STEP_LABELS,
    reset_failed_steps,
    run_pipeline,
)

# Type-only: keeps the real (httpx-backed) `Clients` import out of this
# module's own import graph, so tests that only need `create_app` (most of
# them) don't pay for it. `build_default_app` below does the real import,
# right where `Clients.real(...)` is actually constructed.
if TYPE_CHECKING:
    from cortex_api.clients import Clients


class CreateAgentRequest(BaseModel):
    name: str
    persona: str
    skills: list[str] = []
    model: str | None = None


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
    clients: Clients | None = None,
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

    @app.get("/api/catalog/personas")
    def catalog_personas() -> dict:
        cfg: Config = app.state.config
        personas = list_personas(cfg.personas_dir)
        return {"personas": [{"id": p.id, "description": p.description} for p in personas]}

    @app.get("/api/catalog/skills")
    def catalog_skills() -> dict:
        cfg: Config = app.state.config
        skills = list_skills(cfg.skills_repo_path)
        return {
            "skills": [
                {"id": s.id, "description": s.description, "sha": s.sha} for s in skills
            ]
        }

    @app.get("/api/availability")
    def availability(
        name: str, session: Session = Depends(get_session)  # noqa: B008
    ) -> dict:
        if not NAME_RE.fullmatch(name):
            return {"available": False, "reason": "invalid name"}

        existing = session.query(Agent).filter_by(name=name).first()
        if existing is not None:
            return {"available": False, "reason": "name taken"}

        return {"available": True, "reason": None}

    @app.post("/api/agents", status_code=201)
    def create_agent(
        payload: CreateAgentRequest, request: Request, session: Session = Depends(get_session)  # noqa: B008
    ) -> JSONResponse:
        cfg: Config = app.state.config

        if not NAME_RE.fullmatch(payload.name):
            return JSONResponse({"error": f"invalid name: {payload.name!r}"}, status_code=422)

        personas = {p.id: p for p in list_personas(cfg.personas_dir)}
        if payload.persona not in personas:
            return JSONResponse(
                {"error": f"unknown persona: {payload.persona!r}"}, status_code=422
            )

        skills_catalog = {s.id: s for s in list_skills(cfg.skills_repo_path)}
        unknown_skills = [s for s in payload.skills if s not in skills_catalog]
        if unknown_skills:
            return JSONResponse(
                {"error": f"unknown skills: {unknown_skills}"}, status_code=422
            )

        model_ids = {m.id for m in cfg.models}
        if payload.model is not None and payload.model not in model_ids:
            return JSONResponse(
                {"error": f"unknown model: {payload.model!r}"}, status_code=422
            )
        model_id = payload.model if payload.model is not None else cfg.models[0].id

        # Checked last (a DB round-trip) rather than first: a malformed request
        # is a 422 regardless of whether the name happens to collide too.
        existing = session.query(Agent).filter_by(name=payload.name).first()
        if existing is not None:
            return JSONResponse({"error": f"name taken: {payload.name!r}"}, status_code=409)

        agent = Agent(
            name=payload.name,
            persona=payload.persona,
            model_id=model_id,
            skills=[{"name": s, "sha": skills_catalog[s].sha} for s in payload.skills],
            status=AgentStatus.PENDING,
        )
        session.add(agent)
        try:
            session.commit()
        except IntegrityError:
            # The pre-check above is read-then-act, not a lock: a second
            # request for the same name can pass it and lose the race here
            # instead, discovering the collision only when the unique
            # constraint fires on commit. Map that to the same 409 the
            # pre-check returns rather than letting the raw IntegrityError
            # surface as a 500 — same idiom as retry_agent's FAILED gate,
            # just enforced by the DB instead of a status column.
            session.rollback()
            return JSONResponse({"error": f"name taken: {payload.name!r}"}, status_code=409)

        # Capture the just-inserted status into a local BEFORE starting the
        # pipeline thread and use only that in the response below. The
        # pipeline's own session (opened fresh in run_pipeline's daemon
        # thread) flips the agent to PROVISIONING almost immediately — the
        # gojira lesson this mirrors is that reading status again afterward
        # (a fresh query, a refresh, anything that re-hits the row) risks
        # racing that flip and reporting the wrong status for a 201 that's
        # supposed to mean "just accepted, nothing has run yet".
        status = agent.status
        agent_id = agent.id

        run_pipeline(
            request.app.state.session_factory,
            agent_id,
            config=cfg,
            clients=app.state.clients,
            steps=PIPELINE,
        )

        return JSONResponse({"name": payload.name, "status": status}, status_code=201)

    @app.get("/api/agents")
    def list_agents(session: Session = Depends(get_session)) -> dict:  # noqa: B008
        agents = session.query(Agent).order_by(Agent.id).all()
        return {
            "agents": [
                {
                    "name": a.name,
                    "status": a.status,
                    "model": a.model_id,
                    "created_at": a.created_at.isoformat(),
                }
                for a in agents
            ]
        }

    @app.get("/api/agents/{name}")
    def get_agent(name: str, session: Session = Depends(get_session)) -> JSONResponse:  # noqa: B008
        agent = session.query(Agent).filter_by(name=name).first()
        if agent is None:
            return JSONResponse({"error": f"no such agent: {name!r}"}, status_code=404)

        steps = [
            {
                "name": step.name,
                "label": STEP_LABELS.get(step.name, step.name),
                "status": step.status,
                "error": step.error,
            }
            for step in agent.steps
        ]

        return JSONResponse(
            {
                "name": agent.name,
                "persona": agent.persona,
                "model": agent.model_id,
                "skills": agent.skills,
                "status": agent.status,
                "repo_full_name": agent.repo_full_name,
                "error": agent.error,
                "created_at": agent.created_at.isoformat(),
                "steps": steps,
                "first_reply": agent.first_reply,
            }
        )

    @app.post("/api/agents/{name}/retry", status_code=202)
    def retry_agent(name: str, request: Request, session: Session = Depends(get_session)) -> JSONResponse:  # noqa: B008
        agent = session.query(Agent).filter_by(name=name).first()
        if agent is None:
            return JSONResponse({"error": f"no such agent: {name!r}"}, status_code=404)

        if agent.status != AgentStatus.FAILED:
            return JSONResponse(
                {"error": f"cannot retry agent in status {agent.status!r}"}, status_code=409
            )

        reset_failed_steps(session, agent.id)

        # Flip to PROVISIONING (and clear the prior error) synchronously,
        # before starting the pipeline thread — mirrors create_agent's own
        # status-before-thread discipline. The FAILED gate above is
        # read-then-act: without this, two retries racing right after each
        # other both read FAILED and both pass, each firing its own pipeline
        # thread against the same AgentStep rows. Committing PROVISIONING
        # here closes that window — a second retry request now reads
        # PROVISIONING and 409s instead of double-launching. The engine
        # (`_run_pipeline_body`) also sets PROVISIONING itself; redundant but
        # idempotent, so this doesn't change its behavior.
        agent.status = AgentStatus.PROVISIONING
        agent.error = None
        session.commit()

        agent_id = agent.id
        cfg: Config = app.state.config

        run_pipeline(
            request.app.state.session_factory,
            agent_id,
            config=cfg,
            clients=app.state.clients,
            steps=PIPELINE,
        )

        # Same status-before-thread discipline as create_agent: report the
        # state this call put the agent into (about to re-provision), never
        # a value re-read after the pipeline thread is already running.
        return JSONResponse({"name": name, "status": AgentStatus.PROVISIONING}, status_code=202)

    return app


def build_default_app() -> FastAPI:
    """Wire the real config, database, and platform clients."""
    # Local import: `cortex_api.clients` pulls in httpx/subprocess-backed
    # implementations that most tests (which only need `create_app` and a
    # fake/None clients object) never touch — keeping this import inside the
    # function keeps importing `cortex_api.main` itself cheap for them.
    from cortex_api.clients import Clients

    config = Config.load(None)
    config.workdir.mkdir(parents=True, exist_ok=True)
    engine = make_engine(config.db_url)
    init_db(engine)
    session_factory = make_session_factory(engine)
    # One real Clients (and the httpx.Client(s) it owns) per process, built
    # once here rather than per-request: the accepted pattern for this app's
    # lifetime (a longer-lived httpx client pool is the deferred concern, not
    # a correctness issue for the single-process deployment this targets).
    clients = Clients.real(config)
    return create_app(session_factory, config=config, clients=clients)
