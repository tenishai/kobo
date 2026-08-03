"""FastAPI app factory for pnyx-api."""

from __future__ import annotations

import base64
import logging
import re
import threading
import time
from collections.abc import Iterator
from typing import TYPE_CHECKING

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from pnyx_api.avatars import avatar_prompt, set_avatar
from pnyx_api.catalog import list_personas, list_skills, validate_name
from pnyx_api.clients import ClientError
from pnyx_api.config import Config
from pnyx_api.db import init_db, make_engine, make_session_factory
from pnyx_api.knowledge import (
    KnowledgeError,
    delete_knowledge_file,
    delivery_state,
    read_knowledge,
    save_knowledge_file,
)
from pnyx_api.models import Agent, AgentStatus
from pnyx_api.org import OrgState, load_org_config
from pnyx_api.provisioner import (
    PIPELINE,
    STEP_LABELS,
    reset_failed_steps,
    run_pipeline,
    sweep_interrupted,
)
from pnyx_api.search import CATALOG_TTL_S, ingest_indexes
from pnyx_api.search import search as catalog_search
from pnyx_api.search import teams as catalog_teams_query
from pnyx_api.slack_setup import render_manifest, wire_slack

log = logging.getLogger(__name__)

# Type-only: keeps the real (httpx-backed) `Clients` import out of this
# module's own import graph, so tests that only need `create_app` (most of
# them) don't pay for it. `build_default_app` below does the real import,
# right where `Clients.real(...)` is actually constructed.
if TYPE_CHECKING:
    from pnyx_api.clients import Clients


class CreateAgentRequest(BaseModel):
    name: str
    persona: str
    skills: list[str] = []
    model: str | None = None


class ChatMessagePayload(BaseModel):
    role: str
    text: str


class ChatRequest(BaseModel):
    messages: list[ChatMessagePayload]


# Chat message text is bounded to keep one a2a task's payload (and its cost)
# sane — matches the brief's guard exactly, not an arbitrary framework default.
CHAT_MESSAGE_TEXT_MAX_LEN = 4000

CHAT_MESSAGE_ROLES = {"user", "agent"}


class KnowledgeSavePayload(BaseModel):
    content: str


class SlackConnectRequest(BaseModel):
    channel: str
    bot_token: str
    app_token: str


# The delivery-status route's sha path param must look like a real git
# commit sha (as returned by save_knowledge_file/delete_knowledge_file, both
# `github.local_head`) before it ever reaches pnyx_api.knowledge's
# delivery_state, which takes any str with no shape check of its own — this
# is purely an HTTP-layer guard against a malformed path segment. Matches
# git's own short-to-full hex sha range.
KNOWLEDGE_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")

# Slack channel id shape (Global Constraints, P2c plan): format-only — never
# a semantic check that the channel actually exists (that's Slack's job when
# the agent tries to post to it). The rendered manifest's scopes/events
# (channels:history + groups:history, message.channels + message.groups)
# cover both public and private channels, since modern private channels are
# C-prefixed just like public ones. Legacy G-prefixed channel ids are not
# supported: Slack has migrated private channels to C ids, so there's no
# live channel this regex would need to admit.
SLACK_CHANNEL_RE = re.compile(r"^C[A-Z0-9]{6,}$")

# Avatar generation (P2d, Global Constraint): reject a generated image bigger
# than this BEFORE it's ever pushed to the repo or cached on the Agent row —
# a plain backend-result problem (the model returned something oversized),
# not a user-input problem, hence the 502 rather than a 422.
AVATAR_MAX_BYTES = 4 * 1024 * 1024


def _model_label(cfg: Config, model_id: str) -> str:
    """The catalog label for `model_id`, or the raw id itself when it isn't
    (or is no longer) in `cfg.models` — an agent created under an older
    config whose model has since been retired from the catalog must still
    render, not 500."""
    for model in cfg.models:
        if model.id == model_id:
            return model.label
    return model_id


def _catalogs_configured(clients: Clients | None, config: Config) -> bool:
    """Whether the FTS catalog has anything to refresh from: storage wired
    AND at least one bucket named. Shared by the TTL hook and the explicit
    refresh route's 503 check (Task 3 brief: "storage is None or both bucket
    names empty") so the two can never disagree about what "unwired" means.
    """
    if clients is None or clients.storage is None:
        return False
    return bool(config.skills_bucket or config.personas_bucket)


def _maybe_refresh_catalogs(app: FastAPI, engine) -> None:
    """TTL-gated catalog refresh (Task 3 controller decision #1), shared by
    both search endpoints and the teams endpoint: re-ingests the bucket
    indexes into `catalog_fts` when the table is stale (or has never been
    loaded) and storage is configured.

    A refresh failure here (a bucket blip, a transient storage error) is
    logged and swallowed — these routes must never 500 over it, they just
    keep serving whatever's already in the table (empty, on a cold app).
    Contrast with the explicit `POST /api/catalogs/refresh` route below,
    which surfaces the same failure as a 502 — that route exists precisely
    for an operator who wants to know a refresh failed.

    `app.state.catalog_lock` double-checks freshness after acquiring the
    lock: two requests landing at once both fail the outer TTL check, but
    only one performs the ingest — the other, once it gets the lock, finds
    the first request's refresh already covers it and returns without
    ingesting a second time.
    """
    state = app.state
    if not _catalogs_configured(state.clients, state.config):
        return

    now = time.monotonic()
    if state.catalog_loaded_at is not None and now - state.catalog_loaded_at < CATALOG_TTL_S:
        return

    with state.catalog_lock:
        now = time.monotonic()
        if state.catalog_loaded_at is not None and now - state.catalog_loaded_at < CATALOG_TTL_S:
            return
        try:
            ingest_indexes(
                engine,
                state.clients.storage,
                skills_bucket=state.config.skills_bucket,
                personas_bucket=state.config.personas_bucket,
            )
        except ClientError as exc:
            log.warning("catalog TTL refresh failed, serving the stale table: %s", exc)
            return
        state.catalog_loaded_at = time.monotonic()


def _fail_launch(
    session_factory: sessionmaker[Session],
    session: Session,
    agent_id: int,
    exc: Exception,
) -> JSONResponse:
    """Record a `run_pipeline` launch failure (e.g. `Thread.start()` itself
    raising) as FAILED and return the 503 `JSONResponse` both launch guards
    (`create_agent`, `retry_agent`) report for it.

    `session` (the request's own, from `Depends(get_session)`) may itself be
    poisoned by whatever raised `exc` — a dead connection mid-commit, say —
    so a FAILED write on that same session can raise too, turning what should
    be a caller-actionable 503 into an unhandled 500 on top of an agent still
    wedged in its pre-failure status: exactly what this guard exists to
    prevent. Mirrors `provisioner._run_pipeline_body`'s engine-level recovery
    (`recovery_session = session_factory()`) for the same reason: best-effort
    rollback+close the possibly-poisoned `session`, then do the actual FAILED
    write through a brand-new session opened fresh from `session_factory`.
    Even if that fresh-session write itself fails, log it and still return
    the 503 below — this function's return value must never depend on the
    write landing.
    """
    error_message = f"failed to start provisioning: {exc}"

    try:
        session.rollback()
    except Exception:  # best-effort cleanup only
        log.warning(
            "_fail_launch: rollback of possibly-poisoned session failed",
            exc_info=True,
        )
    finally:
        # Guarded the same way as the rollback above: a sufficiently
        # poisoned session (a genuinely dead connection) can raise on
        # close() too, and an unguarded raise here would propagate out of
        # this function before ever reaching the fresh-session FAILED write
        # below — turning the caller-actionable 503 this function exists to
        # guarantee into a raw 500 instead.
        try:
            session.close()
        except Exception:  # best-effort cleanup only
            log.warning(
                "_fail_launch: close of possibly-poisoned session failed",
                exc_info=True,
            )

    recovery_session = session_factory()
    try:
        agent = recovery_session.get(Agent, agent_id)
        if agent is None:
            log.warning(
                "_fail_launch: agent %s not found while recording launch failure",
                agent_id,
            )
        else:
            agent.status = AgentStatus.FAILED
            agent.error = error_message
            recovery_session.commit()
    except Exception:
        # Even the fresh session's write failed — still return the 503
        # below rather than let this become a raw 500 on top of the launch
        # failure it was trying to record.
        log.exception(
            "_fail_launch: failed to record FAILED for agent %s after launch failure",
            agent_id,
        )
    finally:
        recovery_session.close()

    return JSONResponse({"error": error_message}, status_code=503)


def _validation_error_message(exc: RequestValidationError) -> str:
    """A plain-language message for the app-wide `RequestValidationError`
    handler (`create_app`, below) — the P2c final-review fix for FastAPI's
    *default* 422 handler, which was still live (no handler had been
    registered to override it). That default renders `exc.errors()`
    verbatim, and Pydantic's own "missing field" error carries an `input`
    key holding the ENTIRE submitted request body — so a `/slack/connect`
    call missing `channel` but carrying `bot_token`/`app_token` echoed both
    pasted Slack tokens straight back in the response body. This handler
    replaces that default globally (every route, not just Slack's) with the
    house `{"error": ...}` shape, built ONLY from each error's field
    location (`loc`) — never from `input`, which must never appear here or
    in anything this function returns.

    Field names alone (e.g. "channel", "messages.0.role") are just the
    request's own declared shape, not secret, so naming them is safe and
    more useful than a fully generic message.
    """
    fields = []
    for error in exc.errors():
        loc = [str(part) for part in error.get("loc", ()) if part != "body"]
        if loc:
            fields.append(".".join(loc))
    if not fields:
        return "that request wasn't quite right — check the fields and try again"
    # dict.fromkeys: de-dupe while preserving first-seen order.
    names = ", ".join(dict.fromkeys(fields))
    return f"that request wasn't quite right — check: {names}"


def _ready_agent_or_error(
    session: Session,
    name: str,
    *,
    not_ready_message: str = "agent is not ready for memory changes",
) -> Agent | JSONResponse:
    """Shared 404/409 guard for the four knowledge routes (P2b) and the two
    Slack connect routes (P2c) below.

    Unlike `chat_with_agent`/`retry_agent` — each of which inlines its own
    lookup because their 409 conditions/messages differ (READY-to-chat vs
    FAILED-to-retry) — the knowledge and Slack routes all share the exact
    same 404/READY check, just with different 409 wording per feature (hence
    `not_ready_message`, defaulted to the knowledge routes' original text so
    every existing call site is unaffected), so it's factored out once here
    instead of copy-pasted across routes.

    Returns the `Agent` row when the guard passes, or the `JSONResponse` a
    route should return immediately otherwise — callers must `isinstance`-
    check the result before using it as an `Agent`.
    """
    agent = session.query(Agent).filter_by(name=name).first()
    if agent is None:
        return JSONResponse({"error": f"no such agent: {name!r}"}, status_code=404)
    if agent.status != AgentStatus.READY:
        return JSONResponse({"error": not_ready_message}, status_code=409)
    return agent


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
    org: OrgState | None = None,
) -> FastAPI:
    app = FastAPI(title="pnyx-api")
    app.state.config = config
    app.state.clients = clients
    app.state.session_factory = session_factory
    # `org` defaults to None in the signature (not a shared OrgState(...)
    # instance) so every create_app() call gets its own object here rather
    # than every caller that omits `org` pointing at one mutable instance in
    # common — see the module's Task 2 wiring in build_default_app for how a
    # real boot constructs and passes one. Every pre-existing create_app call
    # (the whole test suite) keeps working unchanged, reading this same
    # "not configured" state Tasks 3/8 render honestly off of.
    app.state.org = org if org is not None else OrgState(False, None, "no org configured")
    # Catalog FTS TTL/refresh state (Task 3 controller decision #1): read and
    # written only by `_maybe_refresh_catalogs` and the refresh route below.
    app.state.catalog_loaded_at = None
    app.state.catalog_lock = threading.Lock()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Overrides FastAPI's default handler app-wide — see
        # `_validation_error_message`'s docstring for why (the token-echo
        # fix) and why every shape-422 across the app is harmonized onto the
        # same `{"error": ...}` convention the UI already expects elsewhere.
        return JSONResponse({"error": _validation_error_message(exc)}, status_code=422)

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

    # --- Catalog search (Task 3): FTS5 over the bucket-published indexes ---

    @app.get("/api/skills/search")
    def search_skills(
        q: str = "", team: str | None = None, session: Session = Depends(get_session)  # noqa: B008
    ) -> dict:
        engine = session.get_bind()
        _maybe_refresh_catalogs(app, engine)
        return {
            "results": [
                {
                    "id": e.id,
                    "name": e.name,
                    "description": e.description,
                    "team": e.team,
                    "tags": e.tags,
                    "sha": e.sha,
                }
                for e in catalog_search(engine, "skill", q, team=team)
            ]
        }

    @app.get("/api/personas/search")
    def search_personas(
        q: str = "", team: str | None = None, session: Session = Depends(get_session)  # noqa: B008
    ) -> dict:
        engine = session.get_bind()
        _maybe_refresh_catalogs(app, engine)
        return {
            "results": [
                {
                    "id": e.id,
                    "name": e.name,
                    "description": e.description,
                    "team": e.team,
                    "tags": e.tags,
                    "builtin": e.builtin,
                }
                for e in catalog_search(engine, "persona", q, team=team)
            ]
        }

    @app.get("/api/catalog/teams")
    def get_catalog_teams(session: Session = Depends(get_session)) -> dict:  # noqa: B008
        engine = session.get_bind()
        _maybe_refresh_catalogs(app, engine)
        return {"teams": catalog_teams_query(engine)}

    @app.post("/api/catalogs/refresh")
    def refresh_catalogs(session: Session = Depends(get_session)) -> JSONResponse:  # noqa: B008
        clients = app.state.clients
        cfg: Config = app.state.config
        if not _catalogs_configured(clients, cfg):
            return JSONResponse(
                {"error": "catalogs aren't set up yet"}, status_code=503
            )

        engine = session.get_bind()
        try:
            with app.state.catalog_lock:
                counts = ingest_indexes(
                    engine,
                    clients.storage,
                    skills_bucket=cfg.skills_bucket,
                    personas_bucket=cfg.personas_bucket,
                )
                app.state.catalog_loaded_at = time.monotonic()
        except ClientError as exc:
            log.warning("catalog refresh failed: %s", exc)
            return JSONResponse({"error": str(exc)}, status_code=502)

        return JSONResponse(counts)

    @app.get("/api/availability")
    def availability(
        name: str, session: Session = Depends(get_session)  # noqa: B008
    ) -> dict:
        reason = validate_name(name)
        if reason is not None:
            return {"available": False, "reason": reason}

        existing = session.query(Agent).filter_by(name=name).first()
        if existing is not None:
            return {"available": False, "reason": "name taken"}

        return {"available": True, "reason": None}

    @app.post("/api/agents", status_code=201)
    def create_agent(
        payload: CreateAgentRequest, request: Request, session: Session = Depends(get_session)  # noqa: B008
    ) -> JSONResponse:
        cfg: Config = app.state.config

        name_reason = validate_name(payload.name)
        if name_reason is not None:
            return JSONResponse(
                {"error": f"invalid name ({name_reason}): {payload.name!r}"}, status_code=422
            )

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

        try:
            run_pipeline(
                request.app.state.session_factory,
                agent_id,
                config=cfg,
                clients=app.state.clients,
                steps=PIPELINE,
            )
        except Exception as exc:
            # Broad by design: a request boundary, not a specific expected
            # failure mode (this repo's ruff config enables no BLE /
            # flake8-blind-except rules — there's no lint exemption being
            # invoked here) - the pipeline thread never got started (or never
            # got the chance to run), so nothing will ever move this agent
            # out of PENDING — land it in FAILED directly rather than leaving
            # it wedged identically to the restart-mid-provision case
            # `sweep_interrupted` covers (this is that same wedge, just
            # triggered by a `Thread.start()`/launch failure instead of a
            # process restart). `_fail_launch` does the actual recovery (see
            # its docstring for why it needs a fresh session rather than this
            # one) and always returns the 503 below, never a raw 500.
            log.exception(
                "create_agent: failed to start provisioning for agent %s", agent_id
            )
            return _fail_launch(request.app.state.session_factory, session, agent_id, exc)

        return JSONResponse({"name": payload.name, "status": status}, status_code=201)

    @app.get("/api/agents")
    def list_agents(session: Session = Depends(get_session)) -> dict:  # noqa: B008
        cfg: Config = app.state.config
        agents = session.query(Agent).order_by(Agent.id).all()
        return {
            "agents": [
                {
                    "name": a.name,
                    "status": a.status,
                    "model": a.model_id,
                    "model_label": _model_label(cfg, a.model_id),
                    "created_at": a.created_at.isoformat(),
                    "has_avatar": a.avatar_png is not None,
                }
                for a in agents
            ]
        }

    @app.get("/api/agents/{name}")
    def get_agent(name: str, session: Session = Depends(get_session)) -> JSONResponse:  # noqa: B008
        cfg: Config = app.state.config
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

        slack = (
            {"app_id": agent.slack_app_id, "channel": agent.slack_channel}
            if agent.slack_app_id is not None
            else None
        )

        return JSONResponse(
            {
                "name": agent.name,
                "persona": agent.persona,
                "model": agent.model_id,
                "model_label": _model_label(cfg, agent.model_id),
                "skills": agent.skills,
                "status": agent.status,
                "repo_full_name": agent.repo_full_name,
                "error": agent.error,
                "created_at": agent.created_at.isoformat(),
                "steps": steps,
                "first_reply": agent.first_reply,
                "slack": slack,
                "has_avatar": agent.avatar_png is not None,
            }
        )

    @app.post("/api/agents/{name}/chat")
    def chat_with_agent(
        name: str, payload: ChatRequest, session: Session = Depends(get_session)  # noqa: B008
    ) -> JSONResponse:
        agent = session.query(Agent).filter_by(name=name).first()
        if agent is None:
            return JSONResponse({"error": f"no such agent: {name!r}"}, status_code=404)

        if agent.status != AgentStatus.READY:
            return JSONResponse({"error": "agent is not ready to chat"}, status_code=409)

        messages = payload.messages
        if not messages:
            return JSONResponse({"error": "messages must not be empty"}, status_code=422)

        for message in messages:
            if message.role not in CHAT_MESSAGE_ROLES:
                return JSONResponse(
                    {"error": f"unknown role: {message.role!r}"}, status_code=422
                )
            if not message.text:
                return JSONResponse(
                    {"error": "message text must not be empty"}, status_code=422
                )
            if len(message.text) > CHAT_MESSAGE_TEXT_MAX_LEN:
                return JSONResponse(
                    {
                        "error": (
                            f"message text exceeds {CHAT_MESSAGE_TEXT_MAX_LEN} characters"
                        )
                    },
                    status_code=422,
                )

        if messages[-1].role != "user":
            return JSONResponse(
                {"error": "last message must have role 'user'"}, status_code=422
            )

        # Same namespace/service derivation as HelloStep (pnyx_api.steps)
        # for this same agent's a2a endpoint — both are just the agent's name.
        clients = app.state.clients
        try:
            reply = clients.agent.converse(
                namespace=agent.name,
                service=agent.name,
                messages=[{"role": m.role, "text": m.text} for m in messages],
            )
        except ClientError as exc:
            return JSONResponse({"error": f"agent unreachable: {exc}"}, status_code=502)

        return JSONResponse({"reply": reply})

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

        try:
            run_pipeline(
                request.app.state.session_factory,
                agent_id,
                config=cfg,
                clients=app.state.clients,
                steps=PIPELINE,
            )
        except Exception as exc:
            # Broad by design, same as create_agent's guard above (this
            # repo's ruff config enables no BLE / flake8-blind-except rules —
            # there's no lint exemption being invoked here). Same wedge as
            # create_agent's launch guard: the thread never started, so
            # nothing will ever move this agent out of PROVISIONING on its
            # own — fail it back so it stays retryable rather than getting
            # stuck past the FAILED-only retry gate above. `_fail_launch`
            # does the actual recovery (see its docstring for why it needs a
            # fresh session rather than this one) and always returns the 503
            # below, never a raw 500.
            log.exception(
                "retry_agent: failed to start provisioning for agent %s", agent_id
            )
            return _fail_launch(request.app.state.session_factory, session, agent_id, exc)

        # Same status-before-thread discipline as create_agent: report the
        # state this call put the agent into (about to re-provision), never
        # a value re-read after the pipeline thread is already running.
        return JSONResponse({"name": name, "status": AgentStatus.PROVISIONING}, status_code=202)

    # --- Knowledge (P2b): bundle/knowledge/*.md seed-memory editing -------------

    @app.get("/api/agents/{name}/knowledge")
    def list_knowledge(name: str, session: Session = Depends(get_session)) -> JSONResponse:  # noqa: B008
        agent = _ready_agent_or_error(session, name)
        if isinstance(agent, JSONResponse):
            return agent

        try:
            files = read_knowledge(app.state.clients.github, agent.repo_full_name)
        except ClientError as exc:
            log.warning("knowledge route failed: %s", exc)
            return JSONResponse(
                {"error": "couldn't reach your agent's files — try again"}, status_code=502
            )

        return JSONResponse(
            {"files": [{"name": f.name, "content": f.content} for f in files]}
        )

    @app.put("/api/agents/{name}/knowledge/{filename}")
    def save_knowledge(
        name: str,
        filename: str,
        payload: KnowledgeSavePayload,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> JSONResponse:
        agent = _ready_agent_or_error(session, name)
        if isinstance(agent, JSONResponse):
            return agent

        try:
            sha = save_knowledge_file(
                app.state.clients.github, agent.repo_full_name, filename, payload.content
            )
        except KnowledgeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        except ClientError as exc:
            log.warning("knowledge route failed: %s", exc)
            return JSONResponse(
                {"error": "couldn't reach your agent's files — try again"}, status_code=502
            )

        return JSONResponse({"sha": sha})

    @app.delete("/api/agents/{name}/knowledge/{filename}")
    def delete_knowledge(
        name: str, filename: str, session: Session = Depends(get_session)  # noqa: B008
    ) -> JSONResponse:
        agent = _ready_agent_or_error(session, name)
        if isinstance(agent, JSONResponse):
            return agent

        try:
            sha = delete_knowledge_file(
                app.state.clients.github, agent.repo_full_name, filename
            )
        except KnowledgeError as exc:
            # knowledge.py carries no structured discriminator between "bad
            # filename" and "file not there to delete" — both are a plain
            # KnowledgeError with only their message text to go on. The two
            # messages have disjoint, stable prefixes ("invalid knowledge
            # filename: " vs "knowledge file not found: "; see
            # pnyx_api.knowledge._validate_filename/delete_knowledge_file),
            # so matching on that prefix is the cleanest option available
            # without touching knowledge.py. It can't misfire: a bad filename
            # raises from _validate_filename before delete_knowledge_file
            # ever reaches its own not-found check.
            message = str(exc)
            if message.startswith("knowledge file not found:"):
                return JSONResponse(
                    {"error": f"no such knowledge file: {filename!r}"}, status_code=404
                )
            return JSONResponse({"error": message}, status_code=422)
        except ClientError as exc:
            log.warning("knowledge route failed: %s", exc)
            return JSONResponse(
                {"error": "couldn't reach your agent's files — try again"}, status_code=502
            )

        return JSONResponse({"sha": sha})

    @app.get("/api/agents/{name}/knowledge/delivery/{sha}")
    def knowledge_delivery(
        name: str, sha: str, session: Session = Depends(get_session)  # noqa: B008
    ) -> JSONResponse:
        agent = _ready_agent_or_error(session, name)
        if isinstance(agent, JSONResponse):
            return agent

        if not KNOWLEDGE_SHA_RE.match(sha):
            return JSONResponse({"error": f"invalid sha: {sha!r}"}, status_code=422)

        clients = app.state.clients
        try:
            state = delivery_state(
                clients.github,
                clients.kubectl,
                repo=agent.repo_full_name,
                namespace=agent.name,
                deploy=agent.name,
                sha=sha,
            )
        except ClientError as exc:
            log.warning("knowledge route failed: %s", exc)
            return JSONResponse(
                {"error": "couldn't check on the delivery — try again"}, status_code=502
            )

        return JSONResponse(state)

    # --- Slack connect (P2c): app creation + token delivery/wiring --------------

    @app.post("/api/agents/{name}/slack/app")
    def create_slack_app(name: str, session: Session = Depends(get_session)) -> JSONResponse:  # noqa: B008
        agent = _ready_agent_or_error(
            session, name, not_ready_message="agent is not ready to connect to Slack yet"
        )
        if isinstance(agent, JSONResponse):
            return agent

        clients = app.state.clients
        if clients.slack is None:
            return JSONResponse({"error": "Slack is not configured"}, status_code=503)

        if agent.slack_app_id is not None:
            return JSONResponse(
                {"error": "Slack is already set up for this agent"}, status_code=409
            )

        cfg: Config = app.state.config
        # persona_first_line source: the persona catalog's own one-line
        # `description` field (pnyx_api.catalog.PersonaInfo.description —
        # the same text the /api/catalog/personas route already returns),
        # looked up by the id recorded on `agent.persona` — NOT the id
        # itself and NOT the persona's full templated body. Falls back to
        # an id-derived phrase only if the persona has since been removed
        # from the catalog, mirroring `_model_label`'s own retired-catalog-
        # entry fallback above.
        personas = {p.id: p for p in list_personas(cfg.personas_dir)}
        persona_info = personas.get(agent.persona)
        if persona_info is not None:
            persona_first_line = persona_info.description
        else:
            persona_first_line = f"A {agent.persona} agent, reachable in Slack."

        manifest = render_manifest(agent.name, persona_first_line)

        try:
            slack_app_id = clients.slack.create_app(manifest)
        except ClientError as exc:
            log.warning("slack app creation failed: %s", exc)
            return JSONResponse(
                {"error": "couldn't set up Slack — try again"}, status_code=502
            )

        # Logged before persisting: if the commit below itself fails, this
        # line is the only record left of an app Slack now knows about that
        # pnyx's own DB doesn't yet — an orphan-recovery breadcrumb for an
        # operator to match against Slack's app list. App ids aren't secret
        # (they're already returned in this route's own 200 response below).
        log.info("slack app created: %s", slack_app_id)

        agent.slack_app_id = slack_app_id
        session.commit()

        return JSONResponse(
            {
                "app_id": slack_app_id,
                "configure_url": f"https://api.slack.com/apps/{slack_app_id}",
            }
        )

    @app.post("/api/agents/{name}/slack/connect")
    def connect_slack(
        name: str, payload: SlackConnectRequest, session: Session = Depends(get_session)  # noqa: B008
    ) -> JSONResponse:
        agent = _ready_agent_or_error(
            session, name, not_ready_message="agent is not ready to connect to Slack yet"
        )
        if isinstance(agent, JSONResponse):
            return agent

        clients = app.state.clients
        if clients.slack is None:
            return JSONResponse({"error": "Slack is not configured"}, status_code=503)

        if agent.slack_app_id is None:
            return JSONResponse(
                {"error": "set up a Slack app for this agent first"}, status_code=409
            )

        # Format checks only — never echo the submitted values back in an
        # error body, here or anywhere below.
        if not SLACK_CHANNEL_RE.match(payload.channel):
            return JSONResponse(
                {"error": "that doesn't look like a channel ID (it starts with C)"},
                status_code=422,
            )
        if not payload.bot_token.startswith("xoxb-"):
            return JSONResponse(
                {"error": "that doesn't look like a Slack bot token"}, status_code=422
            )
        if not payload.app_token.startswith("xapp-"):
            return JSONResponse(
                {"error": "that doesn't look like a Slack app-level token"}, status_code=422
            )

        cfg: Config = app.state.config

        try:
            sha = wire_slack(
                clients.github,
                clients.kubectl,
                clients.gojira,
                repo=agent.repo_full_name,
                name=agent.name,
                gojira_app_id=agent.gojira_app_id,
                channel=payload.channel,
                bot_token=payload.bot_token,
                app_token=payload.app_token,
                runtime_image=cfg.runtime_image,
            )
        except ClientError as exc:
            log.warning("slack connect failed: %s", exc)
            return JSONResponse(
                {"error": "couldn't set up Slack — try again"}, status_code=502
            )

        agent.slack_channel = payload.channel
        session.commit()

        return JSONResponse({"sha": sha})

    # --- Avatar (P2d): persona-derived face, repo-backed with a display cache ---

    @app.post("/api/agents/{name}/avatar")
    def generate_avatar(name: str, session: Session = Depends(get_session)) -> JSONResponse:  # noqa: B008
        agent = _ready_agent_or_error(
            session, name, not_ready_message="agent is not ready for an avatar yet"
        )
        if isinstance(agent, JSONResponse):
            return agent

        clients = app.state.clients
        if clients.image is None:
            return JSONResponse(
                {"error": "picture generation is not available"}, status_code=503
            )

        cfg: Config = app.state.config
        # Same catalog-lookup-by-persona-id idiom as create_slack_app's
        # persona_first_line above (PersonaInfo.description, looked up by
        # agent.persona), with the same id-derived fallback for a persona
        # that's since been removed from the catalog.
        personas = {p.id: p for p in list_personas(cfg.personas_dir)}
        persona_info = personas.get(agent.persona)
        if persona_info is not None:
            persona_description = persona_info.description
        else:
            persona_description = f"A {agent.persona} agent."

        prompt = avatar_prompt(agent.name, persona_description)

        try:
            png = clients.image.generate(prompt)
        except ClientError as exc:
            log.warning("avatar generation failed for %s: %s", name, exc)
            return JSONResponse({"error": str(exc)}, status_code=502)

        # Size cap enforced BEFORE any store (repo push or DB cache) — a
        # backend-result problem, not a user-input one, hence 502 not 422.
        if len(png) > AVATAR_MAX_BYTES:
            log.warning(
                "generated avatar for %s exceeded the %d-byte cap (%d bytes)",
                name, AVATAR_MAX_BYTES, len(png),
            )
            return JSONResponse(
                {"error": "that picture came out too large — try again"}, status_code=502
            )

        try:
            sha = set_avatar(clients.github, repo=agent.repo_full_name, png=png)
        except ClientError as exc:
            # The push side's own failure mode (repo unreachable, auth
            # lapsed, ...) — distinct from the image-generation failure
            # above, so it gets the same generic "couldn't reach your
            # agent's files" family of message the knowledge/Slack routes
            # use for this exact class of GitHub-side failure, rather than
            # this exception's own message.
            log.warning("avatar push failed for %s: %s", name, exc)
            return JSONResponse(
                {"error": "couldn't save your agent's picture — try again"}, status_code=502
            )

        agent.avatar_png = base64.b64encode(png).decode("ascii")
        session.commit()

        return JSONResponse({"sha": sha})

    @app.get("/api/agents/{name}/avatar")
    def get_avatar(name: str, session: Session = Depends(get_session)) -> Response:  # noqa: B008
        # Deliberately no READY gate here (unlike every route above): a
        # provisioning agent's page may still render, and an <img> tag just
        # gets a 404 until a cache exists — the guard would only block a
        # legitimate "not there yet" read the frontend is designed to tolerate.
        agent = session.query(Agent).filter_by(name=name).first()
        if agent is None:
            return JSONResponse({"error": f"no such agent: {name!r}"}, status_code=404)

        if agent.avatar_png is None:
            return JSONResponse({"error": "no picture yet"}, status_code=404)

        png = base64.b64decode(agent.avatar_png)
        return Response(content=png, media_type="image/png")

    return app


def build_default_app() -> FastAPI:
    """Wire the real config, org config, database, and platform clients."""
    # Local import: `pnyx_api.clients` pulls in httpx/subprocess-backed
    # implementations that most tests (which only need `create_app` and a
    # fake/None clients object) never touch — keeping this import inside the
    # function keeps importing `pnyx_api.main` itself cheap for them.
    from pnyx_api.clients import Clients, CommandRunner, GhCLI

    local_config = Config.load(None)
    # One GhCLI, built once, just for the org-config clone below — Clients.
    # real() (below) builds its own separate GhCLI for everything else. Boot
    # never crashes on an org problem: load_org_config catches every clone/
    # parse/validation failure itself and falls back to `local_config`
    # unchanged, paired with an unwired OrgState an unwired room can render
    # honestly (Tasks 3/8 consume `app.state.org`).
    github = GhCLI(runner=CommandRunner())
    config, org = load_org_config(local_config, github)

    config.workdir.mkdir(parents=True, exist_ok=True)
    engine = make_engine(config.db_url)
    init_db(engine)
    session_factory = make_session_factory(engine)
    # Startup sweep, before any request is served: an agent left in
    # PENDING/PROVISIONING by a prior process's death (killed mid-provision,
    # a bad deploy, ...) has no thread left to ever finish it. Only wired
    # here, never in create_app, so the many tests that build their own
    # isolated fixture (agents seeded directly, no restart involved) aren't
    # affected by it.
    sweep_interrupted(session_factory)
    # One real Clients (and the httpx.Client(s) it owns) per process, built
    # once here rather than per-request: the accepted pattern for this app's
    # lifetime (a longer-lived httpx client pool is the deferred concern, not
    # a correctness issue for the single-process deployment this targets).
    # Built from the MERGED config, same as the engine above — everything
    # downstream of boot sees org values as if they'd always been local.
    clients = Clients.real(config)
    return create_app(session_factory, config=config, clients=clients, org=org)
