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
from kobo_api.mcp import audit, rate_limit
from kobo_api.mcp.server import build_mcp_server
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
    def __init__(self, mapping: dict[str, list[str]] | None = None):
        self._mapping = dict(mapping) if mapping else {}

    def groups_for(self, email: str) -> list[str] | None:
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
