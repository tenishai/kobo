"""Tests for kobo's six MCP write tools (MCP server arc, Task 8):
`kobo_api.mcp.tools.write`'s `kobo_create_agent`/`kobo_teach`/`kobo_chat`/
`kobo_request_colleague`/`kobo_accept_invite`/`kobo_wire_slack`.

Same harness discipline as test_mcp_tools_read.py: tools are driven through
the real SDK call path (`mcp._tool_manager.call_tool`), `ctx` is a real
`Context`/`RequestContext` wrapping a plain stubbed request carrying
`.user.access_token.user_email`, and `asyncio.run(...)` drives every call
(no pytest-asyncio in this repo). Fixtures/helpers are deliberately
duplicated locally rather than imported from that module or from
test_api_agents.py/test_api_slack.py -- same "don't cross-import test
fixtures" convention those files already follow.

Every write tool here calls the SAME route handler `kobo_api.main` already
exposes over HTTP (see `kobo_api.mcp.tools.write`'s own module docstring
for the mechanism) -- these tests exist to prove that WIRING and the
status-code translation, not to re-prove business logic the underlying
routes' own test files (test_api_agents.py, test_api_slack.py,
test_api_invites.py-shaped coverage in test_api_agents.py itself) already
pin exhaustively.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.context import RequestContext
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.clients import Clients
from kobo_api.config import Config, GroupChoice, MCPConfig
from kobo_api.main import create_app
from kobo_api.marketplace import marketplace_repo
from kobo_api.mcp import audit, rate_limit
from kobo_api.mcp.server import build_mcp_server
from kobo_api.mcp.tools.read import _MARKETPLACE_DID_NOT_ANSWER
from kobo_api.models import (
    INVITE_PENDING,
    A2AInvite,
    Agent,
    AgentStatus,
    Base,
    MCPToolCallLog,
)
from kobo_api.personas import seed_default_personas
from kobo_api.slack_state import encode_channels
from tests.fakes import (
    FakeAgent,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
    FakeSlack,
    FakeStorage,
)

PERSONA = "helpful-generalist"
REPO_TEMPLATE = "acme/agent-{name}"

GROUP_A = "growth"
GROUP_B = "design-team"

AUDIENCE = "test-iap-audience"
CLIENT_ID = "test-mcp-client"
WORKSPACE_DOMAIN = "example.com"
ISSUER_BASE_URL = "https://kobo.example.org"

SKILLS_BUCKET = "skills-bucket"
PERSONAS_BUCKET = "personas-bucket"

OWNER_A_EMAIL = "owner-a@example.com"
OWNER_B_EMAIL = "owner-b@example.com"
MEMBER_A_EMAIL = "member-a@example.com"
OPERATOR_EMAIL = "ops@example.com"
STRANGER_EMAIL = "stranger@example.com"

BOT_TOKEN = "xoxb-super-secret-bot-token"
APP_TOKEN = "xapp-super-secret-app-token"
APP_ID = "A0EXISTING"
CHANNEL = "C1234567"

_AGENT_YAML_TRANSPORTS = [{"kind": "a2a", "host": "0.0.0.0", "port": 8080}]


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """kobo_create_agent's pipeline (OnboardStep/WireStep/BuildStep) and
    kobo_wire_slack's wait_active both poll in a loop -- never let either
    sleep for real (same discipline as test_api_agents.py/test_api_slack.py's
    own copies of this fixture)."""
    import kobo_api.slack_setup as slack_setup_module
    import kobo_api.steps as steps_module

    monkeypatch.setattr(steps_module, "_sleep", lambda seconds: None)
    monkeypatch.setattr(slack_setup_module, "_sleep", lambda seconds: None)


@pytest.fixture(autouse=True)
def _reset_rate_limit_and_audit():
    rate_limit.reset()
    audit.configure(None)
    yield
    rate_limit.reset()
    audit.configure(None)


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config(tmp_path):
    return Config(
        workdir=tmp_path / "work",
        operators=[OPERATOR_EMAIL],
        groups=[
            GroupChoice(name=GROUP_A, okta_group="okta-growth"),
            GroupChoice(name=GROUP_B, okta_group="okta-design-team"),
        ],
        iap_audience=AUDIENCE,
        skills_bucket=SKILLS_BUCKET,
        personas_bucket=PERSONAS_BUCKET,
        mcp=MCPConfig(
            oauth_client_id=CLIENT_ID,
            workspace_domain=WORKSPACE_DOMAIN,
            issuer_base_url=ISSUER_BASE_URL,
        ),
    )


@pytest.fixture
def fake_storage():
    """Seeded the same way conftest.py's own `fake_storage` is -- kept as a
    local copy (not imported) so this file's `config` fixture, which needs
    the MCP block conftest's own `config` doesn't carry, isn't forced to
    also match conftest's bucket-name constants by coincidence."""
    storage = FakeStorage()
    seed_default_personas(storage, bucket=PERSONAS_BUCKET)
    return storage


class FakeGroupResolver:
    """Mapping-driven, or always-`None` ("the directory is down") when
    `down=True` -- same seam `test_mcp_tools_read.py`'s own copy of this
    fake uses for `kobo_whoami`'s `groups_unavailable` case, reused here
    for `kobo_create_skill`/`kobo_create_tool`'s membership gate (MCP
    marketplace wave, Task 5)."""

    def __init__(self, mapping: dict[str, list[str]] | None = None, *, down: bool = False):
        self._mapping = dict(mapping) if mapping else {}
        self._down = down

    def groups_for(self, email: str) -> list[str] | None:
        if self._down:
            return None
        return self._mapping.get(email.lower(), [])


def _resolver():
    return FakeGroupResolver(
        {
            MEMBER_A_EMAIL: [GROUP_A],
            OWNER_A_EMAIL: [GROUP_A],
            OWNER_B_EMAIL: [GROUP_B],
        }
    )


def _app(
    session_factory,
    config,
    *,
    groups_resolver=None,
    storage=None,
    github=None,
    gojira=None,
    agent=None,
    slack=None,
):
    clients = Clients(
        github=github if github is not None else FakeGitHub(),
        gojira=gojira if gojira is not None else FakeGojira(),
        cloud=FakeCloud(),
        agent=agent if agent is not None else FakeAgent(),
        kubectl=FakeKubectl(),
        groups=groups_resolver,
        storage=storage,
        slack=slack,
    )
    return create_app(session_factory, config=config, clients=clients)


def _seed_agent(
    session_factory,
    *,
    name,
    created_by=None,
    group_name=None,
    team=None,
    status=AgentStatus.READY,
    repo_full_name=None,
    gojira_app_id=None,
    slack_app_id=None,
    slack_channels=None,
    slack_wired=False,
    skills=None,
):
    session = session_factory()
    session.add(
        Agent(
            name=name,
            persona=PERSONA,
            model_id="vertex_ai/claude-sonnet-4-5",
            status=status,
            repo_full_name=repo_full_name or REPO_TEMPLATE.format(name=name),
            created_by=created_by,
            group_name=group_name,
            team=team,
            gojira_app_id=gojira_app_id,
            slack_app_id=slack_app_id,
            slack_channels=encode_channels(slack_channels) if slack_channels is not None else None,
            slack_wired=slack_wired,
            skills=skills if skills is not None else [],
        )
    )
    session.commit()
    session.close()


def _onboarded_gojira(name, repo, **kwargs):
    kwargs.setdefault("activate_after", 1)
    gojira = FakeGojira(**kwargs)
    app_id = gojira.create_app(
        name=name, repo_full_name=repo, cluster="example-cluster", namespace=name
    )
    return gojira, app_id


# --- stub ctx: a real Context/RequestContext, a plain stubbed request ------


class _StubAccessToken:
    def __init__(self, user_email):
        self.user_email = user_email


class _StubUser:
    def __init__(self, user_email):
        self.access_token = _StubAccessToken(user_email)


class _StubRequest:
    def __init__(self, user_email):
        self.user = _StubUser(user_email)


def _ctx_for(email):
    request_context = RequestContext(
        request_id="test-request",
        meta=None,
        session=None,
        lifespan_context=None,
        request=_StubRequest(email),
    )
    return Context(request_context=request_context)


def _call_tool(mcp, tool_name, ctx, **arguments):
    return asyncio.run(mcp._tool_manager.call_tool(tool_name, arguments, context=ctx))


def _last_audit_row(session_factory) -> MCPToolCallLog:
    session = session_factory()
    try:
        return session.query(MCPToolCallLog).order_by(MCPToolCallLog.id.desc()).first()
    finally:
        session.close()


# --- _endpoint: fail loud on a wiring bug -------------------------------------


def test_endpoint_raises_at_lookup_time_for_a_route_that_does_not_exist(
    session_factory, config, fake_storage
):
    from kobo_api.mcp.tools.write import _endpoint

    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)

    with pytest.raises(RuntimeError, match="no route wired"):
        _endpoint(app, "POST", "/api/agents/{name}/does-not-exist")


def test_endpoint_finds_the_real_create_agent_route(session_factory, config, fake_storage):
    from kobo_api.mcp.tools.write import _endpoint

    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)

    endpoint = _endpoint(app, "POST", "/api/agents")

    assert endpoint.__name__ == "create_agent"


# --- registration ------------------------------------------------------------


def test_build_mcp_server_registers_the_six_write_tools_alongside_the_read_ones(
    session_factory, config, fake_storage
):
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)

    mcp = build_mcp_server(app)

    names = {tool.name for tool in mcp._tool_manager.list_tools()}
    assert {
        "kobo_create_agent",
        "kobo_teach",
        "kobo_chat",
        "kobo_request_colleague",
        "kobo_accept_invite",
        "kobo_wire_slack",
    } <= names


def test_build_mcp_server_registers_exactly_the_eighteen_named_tools(
    session_factory, config, fake_storage
):
    """Coverage folded in from Task 8's review: the subset check just
    above (and its read-tools twin in test_mcp_tools_read.py,
    `test_build_mcp_server_registers_the_six_read_tools`) each pin only
    their own file's tools with `<=`, deliberately, so neither file also
    has to track the other's count -- which means nothing anywhere pins
    the FULL registered set. This is that missing test: `build_mcp_server`
    must register EXACTLY these eighteen tools, no more, no fewer,
    matching the design's own "Tool surface" table
    (docs/superpowers/specs/2026-08-22-mcp-server-design.md) plus the MCP
    marketplace wave's Task 2 pair (`kobo_list_skills`/`kobo_list_tools`),
    Task 3's `kobo_set_agent_skills`, Task 4's `kobo_add_agent_tool`, Task
    5's `kobo_create_skill`/`kobo_create_tool`, and the persona wave's own
    `kobo_list_personas` (Task 4) plus `kobo_create_persona` (Task 5), one-
    for-one. A stray extra registration (a copy-paste twin, a leftover
    experiment) or a dropped one would fail this even though both
    narrower `<=` checks above would stay green.
    """
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)

    mcp = build_mcp_server(app)

    names = {tool.name for tool in mcp._tool_manager.list_tools()}
    assert names == {
        "kobo_whoami",
        "kobo_list_agents",
        "kobo_get_agent",
        "kobo_feed",
        "kobo_list_skills",
        "kobo_list_tools",
        "kobo_list_personas",
        "kobo_create_agent",
        "kobo_teach",
        "kobo_chat",
        "kobo_request_colleague",
        "kobo_accept_invite",
        "kobo_wire_slack",
        "kobo_set_agent_skills",
        "kobo_add_agent_tool",
        "kobo_create_skill",
        "kobo_create_tool",
        "kobo_create_persona",
    }


# --- kobo_create_agent --------------------------------------------------------


def test_create_agent_happy_path_returns_name_status_and_note(
    session_factory, config, fake_storage
):
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    result = _call_tool(
        mcp,
        "kobo_create_agent",
        _ctx_for(MEMBER_A_EMAIL),
        who_they_are=PERSONA,
        group=GROUP_A,
        name="clio",
    )

    assert result["name"] == "clio"
    assert result["status"] == AgentStatus.PENDING
    assert result.get("note")
    session = session_factory()
    try:
        agent = session.query(Agent).filter_by(name="clio").one()
        assert agent.group_name == GROUP_A
        assert agent.created_by == MEMBER_A_EMAIL
    finally:
        session.close()


def test_create_agent_without_a_name_uses_the_naming_stations_first_suggestion(
    session_factory, config, fake_storage
):
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    result = _call_tool(
        mcp,
        "kobo_create_agent",
        _ctx_for(MEMBER_A_EMAIL),
        who_they_are=PERSONA,
        group=GROUP_A,
    )

    assert result["name"]
    session = session_factory()
    try:
        assert session.query(Agent).filter_by(name=result["name"]).first() is not None
    finally:
        session.close()


def test_create_agent_non_member_of_claimed_group_surfaces_the_routes_own_consent_rejection(
    session_factory, config, fake_storage
):
    """The controller's own confirmation test: a caller who is NOT a member
    of the group they name must get the route's real consent rejection --
    proof the tool calls the real gate, not a bypass -- never the generic
    "agent not found" (there is no existing agent here to protect)."""
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError) as exc_info:
        _call_tool(
            mcp,
            "kobo_create_agent",
            _ctx_for(MEMBER_A_EMAIL),
            who_they_are=PERSONA,
            group=GROUP_B,
            name="intruder",
        )

    assert "not a member" in str(exc_info.value)
    session = session_factory()
    try:
        assert session.query(Agent).filter_by(name="intruder").first() is None
    finally:
        session.close()


def test_create_agent_operator_may_claim_any_configured_group(
    session_factory, config, fake_storage
):
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    result = _call_tool(
        mcp,
        "kobo_create_agent",
        _ctx_for(OPERATOR_EMAIL),
        who_they_are=PERSONA,
        group=GROUP_B,
        name="opscreated",
    )

    assert result["name"] == "opscreated"


def test_create_agent_unknown_persona_surfaces_the_routes_own_422(
    session_factory, config, fake_storage
):
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match="unknown persona"):
        _call_tool(
            mcp,
            "kobo_create_agent",
            _ctx_for(MEMBER_A_EMAIL),
            who_they_are="no-such-persona",
            group=GROUP_A,
            name="clio",
        )


def test_create_agent_archived_persona_surfaces_the_routes_own_422(
    session_factory, config, fake_storage
):
    """THE INHERITANCE PROOF for the archived arm (task 6): an archived
    persona is dropped from the ingested index (Task 4's `ingest_indexes`),
    so `create_agent_route`'s own lookup misses it exactly like an unknown
    persona would -- but the route's `_persona_archived_in_index` check
    (kobo_api.main) then answers the honest, deliberately-not-folded-into-
    anti-enumeration 422 `persona {id!r} is archived`, and this tool must
    surface that route message VERBATIM, never the generic "unknown
    persona" answer. The persona is created and archived through the REAL
    routes (`kobo_create_persona` for the create, the direct `POST
    /api/personas/{id}/archive` route -- not an MCP tool, none exists in
    this module's 18 -- for the archive, called via `_endpoint` +
    `authz.room_request` the same way this file's other direct-route
    helpers do), never a hand-rolled fixture that skips the archive
    route's own gate."""
    from kobo_api.mcp import authz
    from kobo_api.mcp.tools.write import _endpoint

    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    created = _call_tool(
        mcp,
        "kobo_create_persona",
        _ctx_for(MEMBER_A_EMAIL),
        name="Retiring Reviewer",
        who_they_are="a reviewer about to be archived",
        what_they_do="Reviews things, briefly.",
    )
    persona_id = created["id"]

    archive_endpoint = _endpoint(app, "POST", "/api/personas/{persona_id}/archive")
    req = authz.room_request(app, OPERATOR_EMAIL)
    session = session_factory()
    try:
        archive_response = archive_endpoint(persona_id=persona_id, request=req, session=session)
    finally:
        session.close()
    assert archive_response.status_code == 200, archive_response.body

    with pytest.raises(ToolError, match=f"persona {persona_id!r} is archived"):
        _call_tool(
            mcp,
            "kobo_create_agent",
            _ctx_for(MEMBER_A_EMAIL),
            who_they_are=persona_id,
            group=GROUP_A,
            name="clio",
        )


def test_create_agent_name_taken_surfaces_the_routes_own_409(
    session_factory, config, fake_storage
):
    _seed_agent(session_factory, name="clio", created_by=MEMBER_A_EMAIL, group_name=GROUP_A)
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match="name taken"):
        _call_tool(
            mcp,
            "kobo_create_agent",
            _ctx_for(MEMBER_A_EMAIL),
            who_they_are=PERSONA,
            group=GROUP_A,
            name="clio",
        )


def test_create_agent_passes_skip_baseline(session_factory, config, fake_storage):
    """Baseline tooling pack (Task 6): the tool's own `skip_baseline` reaches
    the create route's payload and lands on the row -- and defaults to False
    (the pack ships) when the caller says nothing about it."""
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    _call_tool(
        mcp,
        "kobo_create_agent",
        _ctx_for(MEMBER_A_EMAIL),
        who_they_are=PERSONA,
        group=GROUP_A,
        name="opted-out",
        skip_baseline=True,
    )
    _call_tool(
        mcp,
        "kobo_create_agent",
        _ctx_for(MEMBER_A_EMAIL),
        who_they_are=PERSONA,
        group=GROUP_A,
        name="default-agent",
    )

    session = session_factory()
    try:
        assert session.query(Agent).filter_by(name="opted-out").one().skip_baseline is True
        assert session.query(Agent).filter_by(name="default-agent").one().skip_baseline is False
    finally:
        session.close()


# --- kobo_teach ----------------------------------------------------------------


def test_teach_happy_path_archives_the_note(session_factory, config, fake_storage):
    fake_github = FakeGitHub()
    _seed_agent(session_factory, name="ward", created_by=OWNER_A_EMAIL, group_name=GROUP_A)
    app = _app(
        session_factory, config, groups_resolver=_resolver(), storage=fake_storage, github=fake_github
    )
    mcp = build_mcp_server(app)

    result = _call_tool(
        mcp, "kobo_teach", _ctx_for(MEMBER_A_EMAIL), agent="ward", note="the sky is blue"
    )

    assert result["mode"] == "archive"
    assert result["filename"]


def test_teach_is_open_to_a_visible_agents_non_owner(session_factory, config, fake_storage):
    """Teach is deliberately open to anyone who can chat -- not owner-gated,
    unlike invites/slack."""
    _seed_agent(session_factory, name="commons", created_by=OWNER_B_EMAIL, group_name=GROUP_A)
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    result = _call_tool(
        mcp, "kobo_teach", _ctx_for(MEMBER_A_EMAIL), agent="commons", note="a shared note"
    )

    assert result["mode"] == "archive"


def test_teach_invisible_agent_raises_agent_not_found(session_factory, config, fake_storage):
    _seed_agent(session_factory, name="hidden", created_by=OWNER_B_EMAIL, group_name=GROUP_B)
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match="agent not found"):
        _call_tool(mcp, "kobo_teach", _ctx_for(MEMBER_A_EMAIL), agent="hidden", note="nope")


# --- kobo_chat -----------------------------------------------------------------


def test_chat_happy_path_returns_the_reply_text(session_factory, config, fake_storage):
    fake_agent = FakeAgent(reply="hello yourself")
    _seed_agent(session_factory, name="ward", created_by=OWNER_A_EMAIL, group_name=GROUP_A)
    app = _app(
        session_factory, config, groups_resolver=_resolver(), storage=fake_storage, agent=fake_agent
    )
    mcp = build_mcp_server(app)

    result = _call_tool(mcp, "kobo_chat", _ctx_for(MEMBER_A_EMAIL), agent="ward", message="hi")

    assert result == {"reply": "hello yourself"}
    assert len(fake_agent.converse_calls) == 1


def test_chat_invisible_agent_raises_agent_not_found(session_factory, config, fake_storage):
    _seed_agent(session_factory, name="hidden", created_by=OWNER_B_EMAIL, group_name=GROUP_B)
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match="agent not found"):
        _call_tool(mcp, "kobo_chat", _ctx_for(MEMBER_A_EMAIL), agent="hidden", message="hi")


def test_chat_not_ready_agent_surfaces_the_routes_own_409(session_factory, config, fake_storage):
    _seed_agent(
        session_factory,
        name="ward",
        created_by=OWNER_A_EMAIL,
        group_name=GROUP_A,
        status=AgentStatus.PENDING,
    )
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match="not ready to chat"):
        _call_tool(mcp, "kobo_chat", _ctx_for(MEMBER_A_EMAIL), agent="ward", message="hi")


# --- kobo_request_colleague -----------------------------------------------------


def test_request_colleague_happy_path_creates_a_pending_invite(
    session_factory, config, fake_storage
):
    _seed_agent(session_factory, name="alpha", created_by=OWNER_A_EMAIL, group_name=GROUP_A)
    _seed_agent(session_factory, name="beta", created_by=OWNER_B_EMAIL, group_name=GROUP_B)
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    result = _call_tool(
        mcp,
        "kobo_request_colleague",
        _ctx_for(OWNER_A_EMAIL),
        agent="alpha",
        colleague="beta",
    )

    assert result["from_agent"] == "alpha"
    assert result["to_agent"] == "beta"
    assert result["status"] == INVITE_PENDING
    session = session_factory()
    try:
        invite = session.query(A2AInvite).filter_by(from_agent="alpha", to_agent="beta").one()
        assert invite.requested_by == OWNER_A_EMAIL
    finally:
        session.close()


def test_request_colleague_non_owner_of_the_from_agent_raises_agent_not_found(
    session_factory, config, fake_storage
):
    _seed_agent(session_factory, name="alpha", created_by=OWNER_A_EMAIL, group_name=GROUP_A)
    _seed_agent(session_factory, name="beta", created_by=OWNER_B_EMAIL, group_name=GROUP_B)
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match="agent not found"):
        _call_tool(
            mcp,
            "kobo_request_colleague",
            _ctx_for(MEMBER_A_EMAIL),
            agent="alpha",
            colleague="beta",
        )


def test_request_colleague_unknown_colleague_raises_agent_not_found(
    session_factory, config, fake_storage
):
    _seed_agent(session_factory, name="alpha", created_by=OWNER_A_EMAIL, group_name=GROUP_A)
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match="agent not found"):
        _call_tool(
            mcp,
            "kobo_request_colleague",
            _ctx_for(OWNER_A_EMAIL),
            agent="alpha",
            colleague="ghost",
        )


def test_request_colleague_duplicate_surfaces_the_routes_own_409(
    session_factory, config, fake_storage
):
    _seed_agent(session_factory, name="alpha", created_by=OWNER_A_EMAIL, group_name=GROUP_A)
    _seed_agent(session_factory, name="beta", created_by=OWNER_B_EMAIL, group_name=GROUP_B)
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)
    _call_tool(
        mcp, "kobo_request_colleague", _ctx_for(OWNER_A_EMAIL), agent="alpha", colleague="beta"
    )

    with pytest.raises(ToolError, match="already pending or accepted"):
        _call_tool(
            mcp,
            "kobo_request_colleague",
            _ctx_for(OWNER_A_EMAIL),
            agent="alpha",
            colleague="beta",
        )


# --- kobo_accept_invite ----------------------------------------------------------


def _pending_invite(session_factory, *, from_agent, to_agent):
    session = session_factory()
    invite = A2AInvite(from_agent=from_agent, to_agent=to_agent, status=INVITE_PENDING)
    session.add(invite)
    session.commit()
    invite_id = invite.id
    session.close()
    return invite_id


def test_accept_invite_happy_path_owner_of_receiving_agent_succeeds(
    session_factory, config, fake_storage
):
    _seed_agent(session_factory, name="alpha", created_by=OWNER_A_EMAIL, group_name=GROUP_A)
    _seed_agent(session_factory, name="beta", created_by=OWNER_B_EMAIL, group_name=GROUP_B)
    invite_id = _pending_invite(session_factory, from_agent="alpha", to_agent="beta")
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    result = _call_tool(
        mcp, "kobo_accept_invite", _ctx_for(OWNER_B_EMAIL), invite_id=invite_id
    )

    assert result["status"] == "accepted"
    assert result["to_agent"] == "beta"


def test_accept_invite_non_owner_of_receiving_agent_raises_agent_not_found(
    session_factory, config, fake_storage
):
    """The controller's own owner-or-operator confirmation test: the FROM
    side's own owner (who has standing to SEE this invite, just not to
    accept it) must get the identical "agent not found" answer -- never a
    distinguishable forbidden."""
    _seed_agent(session_factory, name="alpha", created_by=OWNER_A_EMAIL, group_name=GROUP_A)
    _seed_agent(session_factory, name="beta", created_by=OWNER_B_EMAIL, group_name=GROUP_B)
    invite_id = _pending_invite(session_factory, from_agent="alpha", to_agent="beta")
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match="agent not found"):
        _call_tool(mcp, "kobo_accept_invite", _ctx_for(OWNER_A_EMAIL), invite_id=invite_id)


def test_accept_invite_unknown_id_raises_agent_not_found(session_factory, config, fake_storage):
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match="agent not found"):
        _call_tool(mcp, "kobo_accept_invite", _ctx_for(OWNER_B_EMAIL), invite_id=999999)


def test_accept_invite_already_decided_surfaces_the_routes_own_409(
    session_factory, config, fake_storage
):
    _seed_agent(session_factory, name="alpha", created_by=OWNER_A_EMAIL, group_name=GROUP_A)
    _seed_agent(session_factory, name="beta", created_by=OWNER_B_EMAIL, group_name=GROUP_B)
    invite_id = _pending_invite(session_factory, from_agent="alpha", to_agent="beta")
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)
    _call_tool(mcp, "kobo_accept_invite", _ctx_for(OWNER_B_EMAIL), invite_id=invite_id)

    with pytest.raises(ToolError, match="already accepted"):
        _call_tool(mcp, "kobo_accept_invite", _ctx_for(OWNER_B_EMAIL), invite_id=invite_id)


# --- kobo_wire_slack -------------------------------------------------------------


def test_wire_slack_happy_path_wires_tokens_and_never_returns_them(
    session_factory, config, fake_storage
):
    fake_github = FakeGitHub(agent_yaml_transports=_AGENT_YAML_TRANSPORTS)
    fake_gojira, app_id = _onboarded_gojira("ward", REPO_TEMPLATE.format(name="ward"))
    _seed_agent(
        session_factory,
        name="ward",
        created_by=OWNER_A_EMAIL,
        group_name=GROUP_A,
        gojira_app_id=app_id,
        slack_app_id=APP_ID,
    )
    app = _app(
        session_factory,
        config,
        groups_resolver=_resolver(),
        storage=fake_storage,
        github=fake_github,
        gojira=fake_gojira,
        slack=FakeSlack(),
    )
    mcp = build_mcp_server(app)

    result = _call_tool(
        mcp,
        "kobo_wire_slack",
        _ctx_for(OWNER_A_EMAIL),
        agent="ward",
        bot_token=BOT_TOKEN,
        app_token=APP_TOKEN,
        channel_id=CHANNEL,
    )

    assert result["connected"] is True
    assert BOT_TOKEN not in json.dumps(result)
    assert APP_TOKEN not in json.dumps(result)
    session = session_factory()
    try:
        agent = session.query(Agent).filter_by(name="ward").one()
        assert agent.slack_wired is True
    finally:
        session.close()


def test_wire_slack_non_owner_of_a_visible_agent_raises_agent_not_found(
    session_factory, config, fake_storage
):
    """The controller's own owner-or-operator confirmation test for
    wire_slack: a non-owner of a VISIBLE, READY agent must get "agent not
    found" -- never a distinguishable forbidden."""
    fake_gojira, app_id = _onboarded_gojira("ward", REPO_TEMPLATE.format(name="ward"))
    _seed_agent(
        session_factory,
        name="ward",
        created_by=OWNER_A_EMAIL,
        group_name=GROUP_A,
        gojira_app_id=app_id,
        slack_app_id=APP_ID,
    )
    app = _app(
        session_factory,
        config,
        groups_resolver=_resolver(),
        storage=fake_storage,
        gojira=fake_gojira,
        slack=FakeSlack(),
    )
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match="agent not found"):
        _call_tool(
            mcp,
            "kobo_wire_slack",
            _ctx_for(MEMBER_A_EMAIL),
            agent="ward",
            bot_token=BOT_TOKEN,
            app_token=APP_TOKEN,
            channel_id=CHANNEL,
        )


def test_wire_slack_no_app_connected_yet_raises_a_clear_error(
    session_factory, config, fake_storage
):
    _seed_agent(session_factory, name="ward", created_by=OWNER_A_EMAIL, group_name=GROUP_A)
    app = _app(
        session_factory, config, groups_resolver=_resolver(), storage=fake_storage, slack=FakeSlack()
    )
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match="Connect one"):
        _call_tool(
            mcp,
            "kobo_wire_slack",
            _ctx_for(OWNER_A_EMAIL),
            agent="ward",
            bot_token=BOT_TOKEN,
            app_token=APP_TOKEN,
            channel_id=CHANNEL,
        )

    session = session_factory()
    try:
        agent = session.query(Agent).filter_by(name="ward").one()
        assert agent.slack_wired is False
    finally:
        session.close()


def test_wire_slack_audit_row_redacts_both_tokens_but_not_the_channel(
    session_factory, config, fake_storage
):
    fake_github = FakeGitHub(agent_yaml_transports=_AGENT_YAML_TRANSPORTS)
    fake_gojira, app_id = _onboarded_gojira("ward", REPO_TEMPLATE.format(name="ward"))
    _seed_agent(
        session_factory,
        name="ward",
        created_by=OWNER_A_EMAIL,
        group_name=GROUP_A,
        gojira_app_id=app_id,
        slack_app_id=APP_ID,
    )
    app = _app(
        session_factory,
        config,
        groups_resolver=_resolver(),
        storage=fake_storage,
        github=fake_github,
        gojira=fake_gojira,
        slack=FakeSlack(),
    )
    mcp = build_mcp_server(app)

    _call_tool(
        mcp,
        "kobo_wire_slack",
        _ctx_for(OWNER_A_EMAIL),
        agent="ward",
        bot_token=BOT_TOKEN,
        app_token=APP_TOKEN,
        channel_id=CHANNEL,
    )

    row = _last_audit_row(session_factory)
    assert row is not None
    assert row.tool == "kobo_wire_slack"
    assert row.status == "ok"
    logged = json.loads(row.redacted_args)
    assert logged["bot_token"] == "***REDACTED***"
    assert logged["app_token"] == "***REDACTED***"
    assert logged["channel_id"] == CHANNEL
    assert BOT_TOKEN not in row.redacted_args
    assert APP_TOKEN not in row.redacted_args


# --- kobo_set_agent_skills (MCP marketplace wave, Task 3) ---------------------

SKILL_ID = "ptr-primer"
SKILL_TEAM = "sre"


class _SkillSeedingGitHub(FakeGitHub):
    """A github fake whose clone() also materializes an existing
    bundle/skills/<id>/SKILL.md tree -- the local-repo state the removal
    tests need already in place before delete_skill's own remove_skill call
    runs. Local copy of test_web_skills.py's own fixture of the same
    name/shape -- this file's own "don't cross-import test fixtures"
    convention (see module docstring) applies here too."""

    def __init__(self, *, local_skills=None, **kwargs):
        super().__init__(**kwargs)
        self._local_skills = local_skills or {}

    def clone(self, repo, dest):
        super().clone(repo, dest)
        for rel_path, content in self._local_skills.items():
            target = dest / "bundle" / "skills" / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)


def _seed_marketplace_skill(storage, skill_id, *, owner="sre", description="A sample skill."):
    """Seeds `storage`'s skills bucket with one marketplace-dialect catalog
    entry (persona-metadata plan Task 1b shape -- a top-level object with a
    dict-shaped "skills" mapping and a "count" key, no per-entry "version",
    so materialization falls back to the mutable head copy) plus the
    SKILL.md content `kobo_api.skills.add_catalog_skill` downloads for it.
    Local copy of test_mcp_tools_read.py's own `_marketplace_index_bytes`
    idiom, per this file's no-cross-import-fixtures convention.
    `owner=None` seeds an entry that carries no owner key at all, for the
    "no owner -> shared" resolution case."""
    entry = {"name": skill_id, "description": description, "tags": []}
    if owner is not None:
        entry["owner"] = owner
    storage.write_bytes(
        SKILLS_BUCKET,
        "index.json",
        json.dumps({"skills": {skill_id: entry}, "count": 1}).encode(),
    )
    storage.write_bytes(
        SKILLS_BUCKET,
        f"catalog/{skill_id}/SKILL.md",
        f"---\nname: {skill_id}\ndescription: {description}\n---\nBody.\n".encode(),
    )


def test_set_agent_skills_adds_and_records_db_picks(session_factory, config, fake_storage):
    _seed_marketplace_skill(fake_storage, SKILL_ID, owner=SKILL_TEAM)
    _seed_agent(session_factory, name="ward", created_by=OWNER_A_EMAIL, group_name=GROUP_A)
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    result = _call_tool(
        mcp, "kobo_set_agent_skills", _ctx_for(OWNER_A_EMAIL), agent="ward", add=[SKILL_ID]
    )

    assert result["added"][0]["id"] == SKILL_ID
    assert result["added"][0]["sha"]
    assert result["removed"] == []
    session = session_factory()
    try:
        agent = session.query(Agent).filter_by(name="ward").one()
        assert agent.skills[0]["name"] == SKILL_ID  # the canary seam, closed
        assert agent.skills[0]["team"] == SKILL_TEAM
    finally:
        session.close()


def test_set_agent_skills_remove(session_factory, config, fake_storage):
    github = _SkillSeedingGitHub(local_skills={f"{SKILL_ID}/SKILL.md": "content"})
    _seed_agent(
        session_factory,
        name="ward",
        created_by=OWNER_A_EMAIL,
        group_name=GROUP_A,
        skills=[{"name": SKILL_ID, "team": SKILL_TEAM, "sha": "abc123", "source": "catalog"}],
    )
    app = _app(
        session_factory, config, groups_resolver=_resolver(), storage=fake_storage, github=github
    )
    mcp = build_mcp_server(app)

    result = _call_tool(
        mcp, "kobo_set_agent_skills", _ctx_for(OWNER_A_EMAIL), agent="ward", remove=[SKILL_ID]
    )

    assert result["added"] == []
    assert result["removed"] == [SKILL_ID]
    session = session_factory()
    try:
        agent = session.query(Agent).filter_by(name="ward").one()
        assert agent.skills == []
    finally:
        session.close()


def test_set_agent_skills_conflict_is_tool_error(session_factory, config, fake_storage):
    _seed_marketplace_skill(fake_storage, SKILL_ID, owner=SKILL_TEAM)
    _seed_agent(
        session_factory,
        name="ward",
        created_by=OWNER_A_EMAIL,
        group_name=GROUP_A,
        skills=[{"name": SKILL_ID, "team": SKILL_TEAM, "sha": "abc123", "source": "catalog"}],
    )
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match=f"already carries a skill named {SKILL_ID!r}"):
        _call_tool(
            mcp, "kobo_set_agent_skills", _ctx_for(OWNER_A_EMAIL), agent="ward", add=[SKILL_ID]
        )


def test_set_agent_skills_invisible_agent_not_found(session_factory, config, fake_storage):
    _seed_marketplace_skill(fake_storage, SKILL_ID, owner=SKILL_TEAM)
    _seed_agent(session_factory, name="hidden", created_by=OWNER_B_EMAIL, group_name=GROUP_B)
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match="agent not found"):
        _call_tool(
            mcp,
            "kobo_set_agent_skills",
            _ctx_for(STRANGER_EMAIL),
            agent="hidden",
            add=[SKILL_ID],
        )


def test_set_agent_skills_non_owner_of_a_visible_agent_raises_agent_not_found(
    session_factory, config, fake_storage
):
    """Final-review pin (MUST-FIX 2): `test_set_agent_skills_invisible_
    agent_not_found` above only pins the 404 shape (a DIFFERENT group, so
    the agent is not visible at all). Nothing in this file's own suite
    pinned the 403 shape -- a caller in the SAME group as the agent (so it
    IS visible) who is neither the owner nor a room operator -- even
    though `_agent_route_result`'s own table (A) folds 403 and 404 into
    the identical byte-for-byte "agent not found" on purpose (the whole
    point of the anti-enumeration property). Mirrors this file's own
    `test_wire_slack_non_owner_of_a_visible_agent_raises_agent_not_found`
    idiom (owner-or-operator confirmation test) for `kobo_set_agent_skills`.
    """
    _seed_marketplace_skill(fake_storage, SKILL_ID, owner=SKILL_TEAM)
    _seed_agent(session_factory, name="ward", created_by=OWNER_A_EMAIL, group_name=GROUP_A)
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match="agent not found"):
        _call_tool(
            mcp,
            "kobo_set_agent_skills",
            _ctx_for(MEMBER_A_EMAIL),
            agent="ward",
            add=[SKILL_ID],
        )


def test_set_agent_skills_team_resolved_from_catalog(session_factory, config, fake_storage):
    # The route needs payload.team: the tool resolves it from the
    # marketplace catalog entry's owner -- callers pass ids only.
    other_team = "finance-guild"
    _seed_marketplace_skill(fake_storage, SKILL_ID, owner=other_team)
    _seed_agent(session_factory, name="ward", created_by=OWNER_A_EMAIL, group_name=GROUP_A)
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    _call_tool(mcp, "kobo_set_agent_skills", _ctx_for(OWNER_A_EMAIL), agent="ward", add=[SKILL_ID])

    session = session_factory()
    try:
        agent = session.query(Agent).filter_by(name="ward").one()
        assert agent.skills[0]["team"] == other_team
    finally:
        session.close()


def test_set_agent_skills_ownerless_catalog_entry_resolves_to_shared(
    session_factory, config, fake_storage
):
    _seed_marketplace_skill(fake_storage, SKILL_ID, owner=None)
    _seed_agent(session_factory, name="ward", created_by=OWNER_A_EMAIL, group_name=GROUP_A)
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    _call_tool(mcp, "kobo_set_agent_skills", _ctx_for(OWNER_A_EMAIL), agent="ward", add=[SKILL_ID])

    session = session_factory()
    try:
        agent = session.query(Agent).filter_by(name="ward").one()
        assert agent.skills[0]["team"] == "shared"
    finally:
        session.close()


def test_set_agent_skills_unknown_skill_id_raises_before_any_route_call(
    session_factory, config, fake_storage
):
    _seed_marketplace_skill(fake_storage, SKILL_ID, owner=SKILL_TEAM)
    github = FakeGitHub()
    _seed_agent(session_factory, name="ward", created_by=OWNER_A_EMAIL, group_name=GROUP_A)
    app = _app(
        session_factory, config, groups_resolver=_resolver(), storage=fake_storage, github=github
    )
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match="no such skill: 'ghost'"):
        _call_tool(
            mcp, "kobo_set_agent_skills", _ctx_for(OWNER_A_EMAIL), agent="ward", add=["ghost"]
        )

    assert github.cloned == []
    session = session_factory()
    try:
        agent = session.query(Agent).filter_by(name="ward").one()
        assert agent.skills == []
    finally:
        session.close()


def test_set_agent_skills_catalog_unavailable_raises_tool_error(
    session_factory, config, fake_storage
):
    # fake_storage carries no skills-bucket index at all (only the seeded
    # personas bucket) -- read_marketplace_catalog answers None, same as a
    # legacy-dialect bucket or unreadable JSON would (its own docstring).
    _seed_agent(session_factory, name="ward", created_by=OWNER_A_EMAIL, group_name=GROUP_A)
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match="marketplace catalog is unavailable"):
        _call_tool(
            mcp, "kobo_set_agent_skills", _ctx_for(OWNER_A_EMAIL), agent="ward", add=[SKILL_ID]
        )


def test_set_agent_skills_storage_transport_failure_wraps_client_error(session_factory, config):
    storage = FakeStorage(read_error="read_bytes('skills-bucket') failed (403)")
    _seed_agent(session_factory, name="ward", created_by=OWNER_A_EMAIL, group_name=GROUP_A)
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=storage)
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match=_MARKETPLACE_DID_NOT_ANSWER) as exc_info:
        _call_tool(
            mcp, "kobo_set_agent_skills", _ctx_for(OWNER_A_EMAIL), agent="ward", add=[SKILL_ID]
        )
    assert "403" not in str(exc_info.value)
    assert SKILLS_BUCKET not in str(exc_info.value)


def test_set_agent_skills_partial_failure_leaves_earlier_adds_applied(
    session_factory, config, fake_storage
):
    _seed_marketplace_skill(fake_storage, SKILL_ID, owner=SKILL_TEAM)
    _seed_agent(session_factory, name="ward", created_by=OWNER_A_EMAIL, group_name=GROUP_A)
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match="no such skill: 'ghost'"):
        _call_tool(
            mcp,
            "kobo_set_agent_skills",
            _ctx_for(OWNER_A_EMAIL),
            agent="ward",
            add=[SKILL_ID, "ghost"],
        )

    session = session_factory()
    try:
        agent = session.query(Agent).filter_by(name="ward").one()
        assert [entry["name"] for entry in agent.skills] == [SKILL_ID]
    finally:
        session.close()


# --- kobo_set_agent_skills review round 1 (operator-ruled deviation + minors) -


def test_set_agent_skills_remove_unknown_id_surfaces_the_routes_own_message(
    session_factory, config, fake_storage
):
    """Operator-ruled deviation: a `remove` id the agent doesn't actually
    carry gets `delete_skill`'s own 404 message verbatim, not the generic
    "agent not found" -- this fires strictly post-gate (the caller already
    has standing on THIS agent by the time remove_skill even runs), so
    folding it would hide which id failed for zero enumeration benefit."""
    _seed_agent(session_factory, name="ward", created_by=OWNER_A_EMAIL, group_name=GROUP_A)
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match="skill not found: ghost"):
        _call_tool(
            mcp, "kobo_set_agent_skills", _ctx_for(OWNER_A_EMAIL), agent="ward", remove=["ghost"]
        )


def test_set_agent_skills_add_id_gone_from_catalog_surfaces_the_routes_own_message(
    session_factory, config, fake_storage
):
    """The other half of the operator-ruled deviation, on the ADD side: the
    marketplace index still lists the id (so this tool's own pre-route
    catalog check passes and resolves a team), but the index's promised
    SKILL.md content is gone from the bucket by the time
    add_catalog_skill's own materialization step tries to download it --
    an index/content drift the route reports as "skill no longer in the
    catalog: <id>", which must surface verbatim, not fold into "agent not
    found"."""
    fake_storage.write_bytes(
        SKILLS_BUCKET,
        "index.json",
        json.dumps(
            {
                "skills": {
                    SKILL_ID: {
                        "name": SKILL_ID,
                        "description": "d",
                        "tags": [],
                        "owner": SKILL_TEAM,
                    }
                },
                "count": 1,
            }
        ).encode(),
    )
    # Deliberately no catalog/<id>/SKILL.md written -- the index promises a
    # skill the content bucket no longer actually carries.
    _seed_agent(session_factory, name="ward", created_by=OWNER_A_EMAIL, group_name=GROUP_A)
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match=f"skill no longer in the catalog: {SKILL_ID}"):
        _call_tool(
            mcp, "kobo_set_agent_skills", _ctx_for(OWNER_A_EMAIL), agent="ward", add=[SKILL_ID]
        )


def test_set_agent_skills_remove_on_invisible_agent_raises_agent_not_found(
    session_factory, config, fake_storage
):
    """The gate path still normalizes on the remove side too: an invisible
    agent's own 404 (the visibility gate, fired before remove_skill is
    ever called) is never one of the two skill-scoped messages, so it
    keeps folding into the byte-identical "agent not found" every other
    tool in this module already gives."""
    _seed_agent(session_factory, name="hidden", created_by=OWNER_B_EMAIL, group_name=GROUP_B)
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match="agent not found"):
        _call_tool(
            mcp,
            "kobo_set_agent_skills",
            _ctx_for(STRANGER_EMAIL),
            agent="hidden",
            remove=["whatever"],
        )


def test_set_agent_skills_bucket_not_configured_raises_tool_error(
    session_factory, config, fake_storage
):
    cfg = config.model_copy(update={"skills_bucket": ""})
    _seed_agent(session_factory, name="ward", created_by=OWNER_A_EMAIL, group_name=GROUP_A)
    app = _app(session_factory, cfg, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match="marketplace skills bucket is not configured"):
        _call_tool(
            mcp, "kobo_set_agent_skills", _ctx_for(OWNER_A_EMAIL), agent="ward", add=[SKILL_ID]
        )


def test_set_agent_skills_is_marked_destructive(session_factory, config, fake_storage):
    """remove deletes a whole bundle/skills/<id>/ tree from the agent's
    repo -- this tool's own annotation must say so, distinct from the
    module-wide `destructiveHint=False` every other write tool here
    carries."""
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == "kobo_set_agent_skills")

    assert tool.annotations.destructiveHint is True


def test_set_agent_skills_add_then_remove_applies_adds_before_removals(
    session_factory, config, fake_storage
):
    _seed_marketplace_skill(fake_storage, SKILL_ID, owner=SKILL_TEAM)
    github = _SkillSeedingGitHub(local_skills={"old-skill/SKILL.md": "content"})
    _seed_agent(
        session_factory,
        name="ward",
        created_by=OWNER_A_EMAIL,
        group_name=GROUP_A,
        skills=[{"name": "old-skill", "team": SKILL_TEAM, "sha": "abc", "source": "catalog"}],
    )
    app = _app(
        session_factory, config, groups_resolver=_resolver(), storage=fake_storage, github=github
    )
    mcp = build_mcp_server(app)

    result = _call_tool(
        mcp,
        "kobo_set_agent_skills",
        _ctx_for(OWNER_A_EMAIL),
        agent="ward",
        add=[SKILL_ID],
        remove=["old-skill"],
    )

    assert result["added"][0]["id"] == SKILL_ID
    assert result["removed"] == ["old-skill"]
    # Pins the order this module's own docstring promises ("adds are
    # applied first, then removals") -- if the loop order ever flipped,
    # this would see "kobo: remove skill old-skill" pushed first.
    push_messages = [message for _, message in github.pushes]
    assert push_messages == [
        f"kobo: add skill {SKILL_ID}",
        "kobo: remove skill old-skill",
    ]


def test_set_agent_skills_audit_row_logs_tool_status_and_arguments(
    session_factory, config, fake_storage
):
    _seed_marketplace_skill(fake_storage, SKILL_ID, owner=SKILL_TEAM)
    _seed_agent(session_factory, name="ward", created_by=OWNER_A_EMAIL, group_name=GROUP_A)
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    _call_tool(mcp, "kobo_set_agent_skills", _ctx_for(OWNER_A_EMAIL), agent="ward", add=[SKILL_ID])

    row = _last_audit_row(session_factory)
    assert row is not None
    assert row.tool == "kobo_set_agent_skills"
    assert row.status == "ok"
    logged = json.loads(row.redacted_args)
    assert logged["agent"] == "ward"
    assert logged["add"] == [SKILL_ID]


# --- kobo_add_agent_tool (MCP marketplace wave, Task 4) -----------------------

TOOL_ID = "ptr-guard"

# Satisfies kobo_api.tooling's own validator: a top-level `post_draft` hook,
# no import beyond the standard library.
TOOL_PY = (
    '"""Sample marketplace tool."""\n'
    "import re\n"
    "def post_draft(draft):\n"
    "    return re.sub(r'secret-[0-9]+', '[redacted]', draft)\n"
)

# Neither `pre_task` nor `post_draft` at module top level -- the exact shape
# `kobo_api.tooling._check_entrypoint` rejects.
BAD_HOOK_PY = "def helper():\n    return 1\n"


def test_add_agent_tool_from_marketplace(session_factory, config, fake_storage):
    """`tool_id` path: `fetch_tool_py` pulls the marketplace repo's own
    `tools/<id>/<id>.py`, then the SAME upload route the inline path also
    calls lands it on the agent's repo -- the response carries both the
    resolved filename and the route's own commit sha."""
    config.skills_repo = "org-skills"
    filename = f"{TOOL_ID}.py"
    fake_github = FakeGitHub(
        tooling_source_files={
            marketplace_repo(config): {f"tools/{TOOL_ID}/{filename}": TOOL_PY}
        }
    )
    _seed_agent(session_factory, name="ward", created_by=OWNER_A_EMAIL, group_name=GROUP_A)
    app = _app(
        session_factory,
        config,
        groups_resolver=_resolver(),
        storage=fake_storage,
        github=fake_github,
    )
    mcp = build_mcp_server(app)

    result = _call_tool(
        mcp, "kobo_add_agent_tool", _ctx_for(OWNER_A_EMAIL), agent="ward", tool_id=TOOL_ID
    )

    assert result["filename"] == filename
    assert result["sha"]
    assert fake_github.pushed_file(f"bundle/tools/{filename}") == TOOL_PY


def test_add_agent_tool_inline_content_bad_hook_is_tool_error(
    session_factory, config, fake_storage
):
    """`filename`+`content` path hits the same upload route; content that
    fails the tooling validator (here: no `pre_task`/`post_draft` hook)
    surfaces as a `ToolError` carrying the validator's own message
    verbatim -- proof the inline path reaches the same real gate the
    marketplace path does, not a shortcut around it."""
    _seed_agent(session_factory, name="ward", created_by=OWNER_A_EMAIL, group_name=GROUP_A)
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match="tooling file defines neither pre_task nor post_draft"):
        _call_tool(
            mcp,
            "kobo_add_agent_tool",
            _ctx_for(OWNER_A_EMAIL),
            agent="ward",
            filename="no-hook.py",
            content=BAD_HOOK_PY,
        )


def test_add_agent_tool_requires_exactly_one_source(session_factory, config, fake_storage):
    """Neither source, both sources, or only half of the inline pair
    (`filename` without `content`, or vice versa) -- all four shapes raise
    the identical arbitration `ToolError`, before any route is ever
    called."""
    _seed_agent(session_factory, name="ward", created_by=OWNER_A_EMAIL, group_name=GROUP_A)
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match="pass exactly one source"):
        _call_tool(mcp, "kobo_add_agent_tool", _ctx_for(OWNER_A_EMAIL), agent="ward")

    with pytest.raises(ToolError, match="pass exactly one source"):
        _call_tool(
            mcp,
            "kobo_add_agent_tool",
            _ctx_for(OWNER_A_EMAIL),
            agent="ward",
            tool_id=TOOL_ID,
            filename="x.py",
            content=TOOL_PY,
        )

    with pytest.raises(ToolError, match="pass exactly one source"):
        _call_tool(
            mcp, "kobo_add_agent_tool", _ctx_for(OWNER_A_EMAIL), agent="ward", filename="x.py"
        )

    with pytest.raises(ToolError, match="pass exactly one source"):
        _call_tool(
            mcp, "kobo_add_agent_tool", _ctx_for(OWNER_A_EMAIL), agent="ward", content=TOOL_PY
        )


def test_add_agent_tool_invisible_agent_not_found(session_factory, config, fake_storage):
    _seed_agent(session_factory, name="hidden", created_by=OWNER_B_EMAIL, group_name=GROUP_B)
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match="agent not found"):
        _call_tool(
            mcp,
            "kobo_add_agent_tool",
            _ctx_for(MEMBER_A_EMAIL),
            agent="hidden",
            filename="ptr-guard.py",
            content=TOOL_PY,
        )


def test_add_agent_tool_audit_row_redacts_content(session_factory, config, fake_storage):
    """Mirrors `test_wire_slack_audit_row_redacts_both_tokens_but_not_the_
    channel` -- `content` must join the audit redact set, the same way
    `bot_token`/`app_token` already do, while `filename` (not sensitive)
    logs as given. Also pins the inline path's own happy-path result and
    pushed content (review round 1, minor 4) -- the audit assertions alone
    never checked that this call actually succeeded and landed the file."""
    fake_github = FakeGitHub()
    _seed_agent(session_factory, name="ward", created_by=OWNER_A_EMAIL, group_name=GROUP_A)
    app = _app(
        session_factory,
        config,
        groups_resolver=_resolver(),
        storage=fake_storage,
        github=fake_github,
    )
    mcp = build_mcp_server(app)

    result = _call_tool(
        mcp,
        "kobo_add_agent_tool",
        _ctx_for(OWNER_A_EMAIL),
        agent="ward",
        filename="ptr-guard.py",
        content=TOOL_PY,
    )

    assert result["filename"] == "ptr-guard.py"
    assert result["sha"]
    assert fake_github.pushed_file("bundle/tools/ptr-guard.py") == TOOL_PY

    row = _last_audit_row(session_factory)
    assert row is not None
    assert row.tool == "kobo_add_agent_tool"
    assert row.status == "ok"
    logged = json.loads(row.redacted_args)
    assert logged["content"] == "***REDACTED***"
    assert logged["filename"] == "ptr-guard.py"
    assert TOOL_PY not in row.redacted_args


def test_add_agent_tool_is_marked_destructive(session_factory, config, fake_storage):
    """A same-named upload silently REPLACES whatever `bundle/tools` file
    was already there (`kobo_api.tooling.upload_tooling_file`'s own
    "Write (or overwrite)" docstring, an unconditional `write_text`) --
    including a live, enforced hook -- so this tool's own annotation must
    say so, matching `kobo_set_agent_skills`'s own `destructiveHint=True`
    rather than the module-wide `False` most other write tools carry
    (review round 1, important 2)."""
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == "kobo_add_agent_tool")

    assert tool.annotations.destructiveHint is True


def test_add_agent_tool_marketplace_repo_not_configured_is_tool_error(
    session_factory, config, fake_storage
):
    """`config.skills_repo` unset (the bare `config` fixture's own default)
    -> `marketplace.marketplace_repo` raises `MarketplaceError("the
    marketplace repo is not configured")` from inside `fetch_tool_py`,
    before any clone -- this tool's own `except MarketplaceError` arm must
    preserve that message verbatim (review round 1, important 1)."""
    assert not config.skills_repo
    _seed_agent(session_factory, name="ward", created_by=OWNER_A_EMAIL, group_name=GROUP_A)
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match="the marketplace repo is not configured"):
        _call_tool(
            mcp, "kobo_add_agent_tool", _ctx_for(OWNER_A_EMAIL), agent="ward", tool_id=TOOL_ID
        )


def test_add_agent_tool_marketplace_transport_failure_is_sanitized(
    session_factory, config, fake_storage
):
    """A `ClientError` off the marketplace clone itself (a real transport
    failure, distinct from `MarketplaceError`'s own "no such tool"/
    "not configured" cases) must surface as the same sanitized "did not
    answer" wording `kobo_set_agent_skills`'s own catalog-read failure
    already uses -- never the raw transport text (review round 1,
    important 1)."""
    config.skills_repo = "org-skills"
    transport_message = "github clone failed: rate limited by the origin"
    fake_github = FakeGitHub(clone_error=transport_message)
    _seed_agent(session_factory, name="ward", created_by=OWNER_A_EMAIL, group_name=GROUP_A)
    app = _app(
        session_factory,
        config,
        groups_resolver=_resolver(),
        storage=fake_storage,
        github=fake_github,
    )
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match=_MARKETPLACE_DID_NOT_ANSWER) as exc_info:
        _call_tool(
            mcp, "kobo_add_agent_tool", _ctx_for(OWNER_A_EMAIL), agent="ward", tool_id=TOOL_ID
        )

    assert transport_message not in str(exc_info.value)


def test_add_agent_tool_marketplace_invalid_utf8_surfaces_as_tool_error(
    session_factory, config, fake_storage
):
    """`fetch_tool_py`'s own UnicodeDecodeError-to-MarketplaceError wrap
    (final review, MUST-FIX 1b) reaches this tool's `except MarketplaceError`
    arm the same way every other typed marketplace failure already does,
    surfacing as a clean `ToolError` rather than an unhandled
    `UnicodeDecodeError` breaking the call -- one cheap integration proof
    on top of `test_marketplace.py`'s own unit-level coverage of the wrap
    itself."""
    config.skills_repo = "org-skills"
    filename = f"{TOOL_ID}.py"
    bad_bytes = b'"""caf\xe9 bug."""\ndef post_draft(d):\n    return d\n'
    fake_github = FakeGitHub(
        tooling_source_files={marketplace_repo(config): {f"tools/{TOOL_ID}/{filename}": bad_bytes}}
    )
    _seed_agent(session_factory, name="ward", created_by=OWNER_A_EMAIL, group_name=GROUP_A)
    app = _app(
        session_factory,
        config,
        groups_resolver=_resolver(),
        storage=fake_storage,
        github=fake_github,
    )
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match="not valid UTF-8"):
        _call_tool(
            mcp, "kobo_add_agent_tool", _ctx_for(OWNER_A_EMAIL), agent="ward", tool_id=TOOL_ID
        )


# --- kobo_create_skill / kobo_create_tool (MCP marketplace wave, Task 5) -----
#
# Unlike every other tool in this module, these two call `kobo_api.
# marketplace.create_skill`/`create_tool` DIRECTLY -- there is no existing
# route to reuse (module docstring's whole reuse mechanism does not apply
# here), so the membership gate that stands in for a route's own
# visibility/ownership check is this module's own `_require_team_
# membership`. `config.skills_repo` is unset on the bare `config` fixture,
# so every test below that expects to reach `marketplace.create_skill`/
# `create_tool` sets it first, the same way `test_marketplace.py`'s own
# `marketplace_github` fixture and this file's `test_add_agent_tool_*`
# marketplace-path tests already do.

NEW_SKILL_ID = "onboarding-faq"
NEW_SKILL_CONTENT = (
    "---\ntags: [onboarding]\n---\n"
    "# Onboarding FAQ\n\nAnswers the most common onboarding questions.\n"
)

NEW_TOOL_ID = "redact-guard"


def test_create_skill_member_of_team_commits(session_factory, config, fake_storage):
    """The caller's own resolved groups (`_resolver()`'s `MEMBER_A_EMAIL ->
    [GROUP_A]`) contain the claimed `team` -> the membership gate passes
    and `marketplace.create_skill` commits for real."""
    config.skills_repo = "org-skills"
    fake_github = FakeGitHub()
    app = _app(
        session_factory,
        config,
        groups_resolver=_resolver(),
        storage=fake_storage,
        github=fake_github,
    )
    mcp = build_mcp_server(app)

    result = _call_tool(
        mcp,
        "kobo_create_skill",
        _ctx_for(MEMBER_A_EMAIL),
        skill_id=NEW_SKILL_ID,
        content=NEW_SKILL_CONTENT,
        team=GROUP_A,
    )

    assert result["commit"]
    assert result["note"] == (
        "appears in the catalog after the publish pipeline and the room's "
        "catalog refresh (minutes)"
    )
    written = fake_github.pushed_file(f"skills/{NEW_SKILL_ID}/SKILL.md")
    assert f"owner: {GROUP_A}" in written


def test_create_skill_non_member_refused(session_factory, config, fake_storage):
    """`STRANGER_EMAIL` resolves to no groups at all (`_resolver()`'s own
    default `[]` for an email absent from its mapping) -- refused before
    any clone, naming the claimed team."""
    config.skills_repo = "org-skills"
    fake_github = FakeGitHub()
    app = _app(
        session_factory,
        config,
        groups_resolver=_resolver(),
        storage=fake_storage,
        github=fake_github,
    )
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match=f"not a member of team {GROUP_A!r}"):
        _call_tool(
            mcp,
            "kobo_create_skill",
            _ctx_for(STRANGER_EMAIL),
            skill_id=NEW_SKILL_ID,
            content=NEW_SKILL_CONTENT,
            team=GROUP_A,
        )

    assert fake_github.push_count == 0


def test_create_skill_resolver_down_fail_closed(session_factory, config, fake_storage):
    """A resolver that cannot answer right now (`groups_for` -> `None`)
    refuses even a caller who might really be a member -- fail closed,
    the same posture `kobo_api.main`'s own group-consent gate holds for
    `kobo_create_agent`. Not an operator, so the membership gate is the
    only thing standing between this caller and a commit."""
    config.skills_repo = "org-skills"
    fake_github = FakeGitHub()
    app = _app(
        session_factory,
        config,
        groups_resolver=FakeGroupResolver(down=True),
        storage=fake_storage,
        github=fake_github,
    )
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match="directory lookup unavailable"):
        _call_tool(
            mcp,
            "kobo_create_skill",
            _ctx_for(MEMBER_A_EMAIL),
            skill_id=NEW_SKILL_ID,
            content=NEW_SKILL_CONTENT,
            team=GROUP_A,
        )

    assert fake_github.push_count == 0


def test_create_skill_operator_any_team(session_factory, config, fake_storage):
    """`OPERATOR_EMAIL` is a plain-email operator (`config.operators`) --
    `authz.is_admin` short-circuits the membership gate entirely, so this
    commits even for `GROUP_B`, a team the operator is not resolved into
    at all by `_resolver()`."""
    config.skills_repo = "org-skills"
    fake_github = FakeGitHub()
    app = _app(
        session_factory,
        config,
        groups_resolver=_resolver(),
        storage=fake_storage,
        github=fake_github,
    )
    mcp = build_mcp_server(app)

    result = _call_tool(
        mcp,
        "kobo_create_skill",
        _ctx_for(OPERATOR_EMAIL),
        skill_id=NEW_SKILL_ID,
        content=NEW_SKILL_CONTENT,
        team=GROUP_B,
    )

    assert result["commit"]
    written = fake_github.pushed_file(f"skills/{NEW_SKILL_ID}/SKILL.md")
    assert f"owner: {GROUP_B}" in written


def test_create_skill_collision_surfaces(session_factory, config, fake_storage):
    """`marketplace.create_skill`'s own "already exists" `MarketplaceError`
    (the repo's `clone_tree` already carries this skill's slot) surfaces
    as a `ToolError` with that message preserved verbatim -- the
    membership gate passed, this is `marketplace.create_skill`'s own
    collision check."""
    config.skills_repo = "org-skills"
    fake_github = FakeGitHub(
        clone_tree={
            f"skills/{NEW_SKILL_ID}/SKILL.md": (
                f"---\nname: {NEW_SKILL_ID}\nowner: {GROUP_A}\n---\nAlready there.\n"
            )
        }
    )
    app = _app(
        session_factory,
        config,
        groups_resolver=_resolver(),
        storage=fake_storage,
        github=fake_github,
    )
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match=f"skill {NEW_SKILL_ID!r} already exists"):
        _call_tool(
            mcp,
            "kobo_create_skill",
            _ctx_for(MEMBER_A_EMAIL),
            skill_id=NEW_SKILL_ID,
            content=NEW_SKILL_CONTENT,
            team=GROUP_A,
        )


def test_create_skill_marketplace_transport_failure_is_sanitized(
    session_factory, config, fake_storage
):
    """A `ClientError` off the marketplace clone itself (a real transport
    failure, distinct from `MarketplaceError`'s own typed cases) must
    surface as the same sanitized "did not answer" wording every other
    marketplace-transport failure in this arc already uses, never the raw
    transport text -- mirrors `test_add_agent_tool_marketplace_transport_
    failure_is_sanitized`. Review round 1, Important 1."""
    config.skills_repo = "org-skills"
    transport_message = "github clone failed: rate limited by the origin"
    fake_github = FakeGitHub(clone_error=transport_message)
    app = _app(
        session_factory,
        config,
        groups_resolver=_resolver(),
        storage=fake_storage,
        github=fake_github,
    )
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match=_MARKETPLACE_DID_NOT_ANSWER) as exc_info:
        _call_tool(
            mcp,
            "kobo_create_skill",
            _ctx_for(MEMBER_A_EMAIL),
            skill_id=NEW_SKILL_ID,
            content=NEW_SKILL_CONTENT,
            team=GROUP_A,
        )

    assert transport_message not in str(exc_info.value)


def test_create_tool_member_of_team_commits(session_factory, config, fake_storage):
    """`kobo_create_tool`'s own happy path -- distinct from `kobo_create_
    skill`'s: a different marketplace fixture (`TOOL.md` + `<id>.py`) and
    a distinct, pipeline-free `note` (this tool's listing reads the repo
    directly, per this module's own `register` docstring addition)."""
    config.skills_repo = "org-skills"
    fake_github = FakeGitHub()
    app = _app(
        session_factory,
        config,
        groups_resolver=_resolver(),
        storage=fake_storage,
        github=fake_github,
    )
    mcp = build_mcp_server(app)

    result = _call_tool(
        mcp,
        "kobo_create_tool",
        _ctx_for(MEMBER_A_EMAIL),
        tool_id=NEW_TOOL_ID,
        content=TOOL_PY,
        team=GROUP_A,
        description="Redacts secret-shaped strings from a draft.",
        tags=["security"],
    )

    assert result["commit"]
    assert "catalog refresh" not in result["note"]  # kobo_create_skill's note, not this one
    assert "kobo_list_tools" in result["note"]
    assert "immediately" in result["note"]
    assert fake_github.pushed_file(f"tools/{NEW_TOOL_ID}/{NEW_TOOL_ID}.py") == TOOL_PY
    written_md = fake_github.pushed_file(f"tools/{NEW_TOOL_ID}/TOOL.md")
    assert f"owner: {GROUP_A}" in written_md


def test_create_tool_non_member_refused(session_factory, config, fake_storage):
    """Mirrors `test_create_skill_non_member_refused`: the membership gate
    (`_require_team_membership`) is shared code, but nothing else in this
    file calls `kobo_create_tool` with a non-member -- without this test,
    deleting the gate's call site inside `kobo_create_tool` specifically
    (as opposed to the shared helper itself) would go unnoticed. Review
    round 1, Important 2."""
    config.skills_repo = "org-skills"
    fake_github = FakeGitHub()
    app = _app(
        session_factory,
        config,
        groups_resolver=_resolver(),
        storage=fake_storage,
        github=fake_github,
    )
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match=f"not a member of team {GROUP_A!r}"):
        _call_tool(
            mcp,
            "kobo_create_tool",
            _ctx_for(STRANGER_EMAIL),
            tool_id=NEW_TOOL_ID,
            content=TOOL_PY,
            team=GROUP_A,
            description="A guard.",
        )

    assert fake_github.push_count == 0


def test_create_tool_baseline_prefix_refused_for_non_operator(
    session_factory, config, fake_storage
):
    """The baseline pack's reservation (Task 6 review, operator ruling): a
    `baseline-` id is what every NEW agent then ships automatically
    (`kobo_api.steps._stage_baseline_tools`), so publishing one is a
    room-wide act, not a team one. MEMBER_A_EMAIL is a real member of
    GROUP_A here -- the membership gate passes and this refusal can only
    come from the prefix gate."""
    config.skills_repo = "org-skills"
    fake_github = FakeGitHub()
    app = _app(
        session_factory,
        config,
        groups_resolver=_resolver(),
        storage=fake_storage,
        github=fake_github,
    )
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match="reserved for room operators"):
        _call_tool(
            mcp,
            "kobo_create_tool",
            _ctx_for(MEMBER_A_EMAIL),
            tool_id="baseline-leakguard",
            content=TOOL_PY,
            team=GROUP_A,
            description="A guard.",
        )

    assert fake_github.push_count == 0


def test_create_tool_baseline_prefix_allowed_for_operator(
    session_factory, config, fake_storage
):
    """The other side of the reservation: an operator may publish into the
    pack. OPERATOR_EMAIL isn't in GROUP_A's resolved membership either, so
    this one call clears BOTH operator exemptions at once."""
    config.skills_repo = "org-skills"
    fake_github = FakeGitHub()
    app = _app(
        session_factory,
        config,
        groups_resolver=_resolver(),
        storage=fake_storage,
        github=fake_github,
    )
    mcp = build_mcp_server(app)

    result = _call_tool(
        mcp,
        "kobo_create_tool",
        _ctx_for(OPERATOR_EMAIL),
        tool_id="baseline-leakguard",
        content=TOOL_PY,
        team=GROUP_A,
        description="Redacts secret-shaped strings from every reply.",
    )

    assert result["commit"]
    assert (
        fake_github.pushed_file("tools/baseline-leakguard/baseline-leakguard.py") == TOOL_PY
    )


def test_create_tool_resolver_down_fail_closed(session_factory, config, fake_storage):
    """Mirrors `test_create_skill_resolver_down_fail_closed` for
    `kobo_create_tool` -- same "deleting the gate's call site inside this
    specific tool would go unnoticed" reasoning as `test_create_tool_non_
    member_refused` just above. Review round 1, Important 2."""
    config.skills_repo = "org-skills"
    fake_github = FakeGitHub()
    app = _app(
        session_factory,
        config,
        groups_resolver=FakeGroupResolver(down=True),
        storage=fake_storage,
        github=fake_github,
    )
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match="directory lookup unavailable"):
        _call_tool(
            mcp,
            "kobo_create_tool",
            _ctx_for(MEMBER_A_EMAIL),
            tool_id=NEW_TOOL_ID,
            content=TOOL_PY,
            team=GROUP_A,
            description="A guard.",
        )

    assert fake_github.push_count == 0


def test_create_tool_invalid_hook_never_commits(session_factory, config, fake_storage):
    """`marketplace.create_tool` runs `kobo_api.tooling`'s own mechanical
    validator (size/UTF-8/non-empty/ast/import-allowlist/entrypoint)
    BEFORE any clone -- `BAD_HOOK_PY` (no top-level `pre_task`/
    `post_draft`, the same fixture `test_add_agent_tool_inline_content_
    bad_hook_is_tool_error` already uses) surfaces that validator's own
    message, and `FakeGitHub.push_count` proves no commit was ever made."""
    config.skills_repo = "org-skills"
    fake_github = FakeGitHub()
    app = _app(
        session_factory,
        config,
        groups_resolver=_resolver(),
        storage=fake_storage,
        github=fake_github,
    )
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match="neither pre_task nor post_draft"):
        _call_tool(
            mcp,
            "kobo_create_tool",
            _ctx_for(MEMBER_A_EMAIL),
            tool_id=NEW_TOOL_ID,
            content=BAD_HOOK_PY,
            team=GROUP_A,
            description="A guard.",
        )

    assert fake_github.push_count == 0


def test_create_tool_marketplace_transport_failure_is_sanitized(
    session_factory, config, fake_storage
):
    """`kobo_create_tool`'s own twin of `test_create_skill_marketplace_
    transport_failure_is_sanitized` -- pins the same sanitization on the
    OTHER tool's own `except ClientError` arm, since the two are separate
    lines of code that could independently regress to leaking `str(exc)`.
    Review round 1, Important 1."""
    config.skills_repo = "org-skills"
    transport_message = "github clone failed: rate limited by the origin"
    fake_github = FakeGitHub(clone_error=transport_message)
    app = _app(
        session_factory,
        config,
        groups_resolver=_resolver(),
        storage=fake_storage,
        github=fake_github,
    )
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match=_MARKETPLACE_DID_NOT_ANSWER) as exc_info:
        _call_tool(
            mcp,
            "kobo_create_tool",
            _ctx_for(MEMBER_A_EMAIL),
            tool_id=NEW_TOOL_ID,
            content=TOOL_PY,
            team=GROUP_A,
            description="A guard.",
        )

    assert transport_message not in str(exc_info.value)


def test_create_content_redacted_in_audit(session_factory, config, fake_storage):
    """`content` already joins `kobo_api.mcp.audit.SENSITIVE_KEYS` (Task 4
    wired the substring mechanism) -- this pins that `kobo_create_skill`'s
    own audit row shows it redacted, the same idiom `test_add_agent_tool_
    audit_row_redacts_content` already follows for `kobo_add_agent_tool`."""
    config.skills_repo = "org-skills"
    fake_github = FakeGitHub()
    app = _app(
        session_factory,
        config,
        groups_resolver=_resolver(),
        storage=fake_storage,
        github=fake_github,
    )
    mcp = build_mcp_server(app)

    _call_tool(
        mcp,
        "kobo_create_skill",
        _ctx_for(MEMBER_A_EMAIL),
        skill_id=NEW_SKILL_ID,
        content=NEW_SKILL_CONTENT,
        team=GROUP_A,
    )

    row = _last_audit_row(session_factory)
    assert row is not None
    assert row.tool == "kobo_create_skill"
    assert row.status == "ok"
    logged = json.loads(row.redacted_args)
    assert logged["content"] == "***REDACTED***"
    assert logged["skill_id"] == NEW_SKILL_ID
    assert logged["team"] == GROUP_A
    assert NEW_SKILL_CONTENT not in row.redacted_args


def test_create_skill_is_marked_not_destructive(session_factory, config, fake_storage):
    """A `skill_id` collision refuses rather than overwrites (`marketplace.
    create_skill`'s own "already exists" check) -- unlike `kobo_set_agent_
    skills`/`kobo_add_agent_tool`'s `destructiveHint=True`, this tool only
    ever creates a new, never-before-existing slot, so `False` is
    accurate."""
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == "kobo_create_skill")

    assert tool.annotations.destructiveHint is False


def test_create_tool_is_marked_not_destructive(session_factory, config, fake_storage):
    """Same reasoning as `test_create_skill_is_marked_not_destructive`:
    `marketplace.create_tool`'s own collision check refuses rather than
    overwrites."""
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == "kobo_create_tool")

    assert tool.annotations.destructiveHint is False


# --- kobo_create_persona (persona wave, Task 5) ------------------------------
#
# Unlike kobo_create_skill/kobo_create_tool just above, this tool calls a
# REAL route (POST /api/personas, create_persona_route in kobo_api.main) --
# these tests exist to prove the WIRING and status-code translation
# (_create_result, table (B), same as kobo_create_agent's own tests
# further up this file), never to re-prove the persona builder's own
# business logic -- that's test_api_personas.py's job, exhaustively.


def test_create_persona_shared_lands_and_is_instantly_listable(
    session_factory, config, fake_storage
):
    """The commons team needs no membership at all (`_persona_team_gate`'s
    own no-gate arm) -- the happy path lands under `team="shared"` and,
    because `create_persona_route` re-ingests with the FULL catalog mapping
    right after a successful create (kobo_api.main, Task 4 controller
    decision #1), the new persona is immediately visible to
    `kobo_list_personas` through this same shim -- no separate refresh
    call needed."""
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    result = _call_tool(
        mcp,
        "kobo_create_persona",
        _ctx_for(MEMBER_A_EMAIL),
        name="Night Auditor",
        who_they_are="a meticulous overnight reconciler",
        what_they_do="Reconcile the day's transactions and flag anomalies.",
    )

    assert result["team"] == "shared"
    assert result["id"] == "night-auditor"

    listed = _call_tool(mcp, "kobo_list_personas", _ctx_for(MEMBER_A_EMAIL))
    assert any(entry["id"] == result["id"] for entry in listed["result"])


def test_create_persona_named_team_member_passes(session_factory, config, fake_storage):
    """A caller resolved into the claimed team lands under it -- the
    route's own `_persona_team_gate` membership arm, not a bypass."""
    resolver = FakeGroupResolver({MEMBER_A_EMAIL: ["sre"]})
    app = _app(session_factory, config, groups_resolver=resolver, storage=fake_storage)
    mcp = build_mcp_server(app)

    result = _call_tool(
        mcp,
        "kobo_create_persona",
        _ctx_for(MEMBER_A_EMAIL),
        name="SRE Sentinel",
        who_they_are="an on-call reliability engineer",
        what_they_do="Watch dashboards and page the right team.",
        team="sre",
    )

    assert result["team"] == "sre"


def test_create_persona_with_rules_lands_in_the_rendered_body(
    session_factory, config, fake_storage
):
    """`rules` (optional always/never one-liners) must actually reach the
    created persona, not just clear validation -- the 201 body never
    echoes `rules` back (see `create_persona_route`'s own response shape),
    so this reads the PERSISTED persona.md straight off `fake_storage`,
    the same way `test_create_persona_shared_lands_and_is_instantly_
    listable` proves its own create landed via `kobo_list_personas`
    rather than trusting the return value alone."""
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    result = _call_tool(
        mcp,
        "kobo_create_persona",
        _ctx_for(MEMBER_A_EMAIL),
        name="Ledger Keeper",
        who_they_are="a scrupulous bookkeeper",
        what_they_do="Track every transaction to the penny.",
        rules=["always cite the ledger"],
    )

    assert result["id"] == "ledger-keeper"
    assert result["team"] == "shared"
    body = fake_storage.read_bytes(PERSONAS_BUCKET, f"{result['id']}/persona.md").decode()
    assert "- always cite the ledger" in body


def test_create_persona_rules_none_explicit_succeeds(session_factory, config, fake_storage):
    """WHY this test exists: FastMCP materializes every declared default at
    call time, so a caller who never mentions `rules` at all still sends
    `rules=None` into the tool body -- there is no "key omitted" case
    inside the function, only "rules is None" or "rules is a list".
    `CreatePersonaRequest.rules` (kobo_api.main/kobo_api.personas) is
    typed `list[str]` with `Field(default_factory=list)`, NOT
    `Optional[list[str]]` -- passing `None` straight through raises a
    pydantic `ValidationError`. `write.py`'s own `rules=rules or []` is
    the ONLY thing standing between the common, rules-less call and that
    failure. This test pins that guard directly, with `rules=None` passed
    explicitly, against a future "simplification" to plain `rules=rules`
    that would quietly break every rules-less `kobo_create_persona` call."""
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    result = _call_tool(
        mcp,
        "kobo_create_persona",
        _ctx_for(MEMBER_A_EMAIL),
        name="Quiet Auditor",
        who_they_are="an unassuming reviewer",
        what_they_do="Check the numbers without commentary.",
        rules=None,
    )

    assert result["id"] == "quiet-auditor"
    assert result["team"] == "shared"


def test_create_persona_named_team_non_member_toolerror(session_factory, config, fake_storage):
    """THE INHERITANCE PROOF (task 5 mutation check): a caller resolved
    into some OTHER team, naming "sre", must get the route's own 403 copy
    verbatim -- never a bypass, never a generic error. Commenting out
    `create_persona_route`'s own `_persona_team_gate` call must make this
    test fail through this shim (recorded in the task report)."""
    resolver = FakeGroupResolver({MEMBER_A_EMAIL: ["other"]})
    app = _app(session_factory, config, groups_resolver=resolver, storage=fake_storage)
    mcp = build_mcp_server(app)

    with pytest.raises(
        ToolError, match="you must be a member of team sre to file a persona under it"
    ):
        _call_tool(
            mcp,
            "kobo_create_persona",
            _ctx_for(MEMBER_A_EMAIL),
            name="SRE Sentinel",
            who_they_are="an on-call reliability engineer",
            what_they_do="Watch dashboards and page the right team.",
            team="sre",
        )


def test_create_persona_exists_conflict_verbatim(session_factory, config, fake_storage):
    """`create_persona`'s own `PersonaExists` (kobo_api.personas), folded
    into the route's 409, surfaces through `_create_result` verbatim."""
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)
    kwargs = dict(
        name="Duplicate Auditor",
        who_they_are="a careful reviewer",
        what_they_do="Review submissions for accuracy.",
    )

    _call_tool(mcp, "kobo_create_persona", _ctx_for(MEMBER_A_EMAIL), **kwargs)

    with pytest.raises(ToolError, match="a persona with that name already exists"):
        _call_tool(mcp, "kobo_create_persona", _ctx_for(MEMBER_A_EMAIL), **kwargs)


def test_create_persona_render_overflow_verbatim(session_factory, config, fake_storage):
    """The merged render's own 200-line ceiling (kobo_api.personas.
    _check_rendered_size, raised as PersonaError, folded into the route's
    422) surfaces through `_create_result` verbatim -- 201 newline-joined
    "line" tokens in `what_they_do` overflow it. Asserts the MESSAGE SHAPE
    ("renders to <n> lines; the limit is 200", with n > 200) rather than
    pinning the persona template's own fixed-line count (currently 7): a
    template change that shifts that count must not break this test, only
    a change to the 200-line ceiling itself or the message wording should."""
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)
    overflowing_what_they_do = "\n".join(["line"] * 201)

    with pytest.raises(ToolError, match=r"renders to \d+ lines; the limit is 200") as exc_info:
        _call_tool(
            mcp,
            "kobo_create_persona",
            _ctx_for(MEMBER_A_EMAIL),
            name="Overflow Auditor",
            who_they_are="a very thorough reviewer",
            what_they_do=overflowing_what_they_do,
        )

    message = str(exc_info.value)
    assert message.endswith("the limit is 200")
    reported_lines = int(message.split("renders to ")[1].split(" lines")[0])
    assert reported_lines > 200


def test_create_persona_slug_law_verbatim(session_factory, config, fake_storage):
    """A name that clears pydantic's own length bound but can't slugify to
    a valid id (kobo_api.personas.persona_id's own PersonaError, folded
    into the route's 422) surfaces through `_create_result` verbatim."""
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match=r"could not derive a valid persona id from name: '!!!'"):
        _call_tool(
            mcp,
            "kobo_create_persona",
            _ctx_for(MEMBER_A_EMAIL),
            name="!!!",
            who_they_are="a persona with an unslugifiable name",
            what_they_do="Do something.",
        )


def test_create_persona_audit_row_bounded(session_factory, config, fake_storage):
    """`what_they_do` is not a `SENSITIVE_KEYS` name (no "content"/"note"/
    "message"/... substring in it) so it is never redacted whole -- but it
    IS value-bounded (Task 3's `_ARG_TEXT_CAP`/`_bounded`), so a 2000-char
    body must still land in the audit row carrying the truncation marker,
    never the full text."""
    app = _app(session_factory, config, groups_resolver=_resolver(), storage=fake_storage)
    mcp = build_mcp_server(app)
    long_what_they_do = "x" * 2000

    _call_tool(
        mcp,
        "kobo_create_persona",
        _ctx_for(MEMBER_A_EMAIL),
        name="Verbose Auditor",
        who_they_are="a wordy assistant",
        what_they_do=long_what_they_do,
    )

    row = _last_audit_row(session_factory)
    assert row is not None
    assert row.tool == "kobo_create_persona"
    assert row.status == "ok"
    logged = json.loads(row.redacted_args)
    assert "...[truncated" in logged["what_they_do"]
    assert long_what_they_do not in row.redacted_args
