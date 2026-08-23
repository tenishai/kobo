"""Tests for kobo's MCP server object + its four read tools (MCP server
arc, Task 7): `kobo_api.mcp.server.build_mcp_server`/`_KoboTokenVerifier`/
`get_user_email`, and `kobo_api.mcp.tools.read`'s `kobo_whoami`/
`kobo_list_agents`/`kobo_get_agent`/`kobo_feed`.

**Driving tools through the FastMCP tool-call path.** Per this task's
brief: exercise the SDK-blessed call path, not a hand-rolled shortcut.
`mcp._tool_manager.call_tool(name, arguments, context=ctx)` is that path
-- it is the exact same method `FastMCP.call_tool` (the SDK's own public
entry point) delegates to, and it runs the real pipeline a live request
would: `Tool.from_function`'s registration-time introspection already
found `ctx` via `find_context_parameter` (real, not stubbed), pydantic
validates `arguments` against the tool's real JSON-schema-derived
argument model, `fn_metadata.call_fn_with_arg_validation` injects `ctx`
into the wrapped call, `@audited(...)` runs for real (rate limit, timing,
the audit row), and only then does the tool body itself run. The one
thing this path skips is the ASGI/HTTP/JSON-RPC transport itself
(`BearerAuthBackend`, the streamable-HTTP session) -- exercising that
would mean standing up a live HTTP server and driving the wire protocol,
which buys nothing beyond what `_KoboTokenVerifier`'s own direct
`verify_token` tests already cover. `context=` is a real `mcp.server.
fastmcp.Context` wrapping a real `mcp.shared.context.RequestContext`
whose `request` is a plain stub carrying `.user.access_token.user_email`
-- the identical shape `_KoboTokenVerifier` -> `BearerAuthBackend` ->
Starlette's `AuthenticationMiddleware` would produce for a real bearer
token, confirmed against the installed SDK (see `kobo_api.mcp.server`'s
own module docstring and this task's report for the full trace).

No pytest-asyncio in this repo (house convention, `test_mcp_audit.py`):
`asyncio.run(...)` drives every call.
"""

from __future__ import annotations

import asyncio

import pytest
from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.context import RequestContext
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.clients import Clients
from kobo_api.config import SHARED_GROUP, Config, GroupChoice, MCPConfig
from kobo_api.main import create_app
from kobo_api.mcp import audit, rate_limit
from kobo_api.mcp.jwt import mint
from kobo_api.mcp.server import (
    _KoboAccessToken,
    _KoboTokenVerifier,
    build_mcp_server,
    get_user_email,
)
from kobo_api.models import Agent, AgentStatus, AgentStep, Base
from kobo_api.purpose_tags import encode_tags
from tests.fakes import (
    FakeAgent,
    FakeCabal,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
)

PERSONA = "helpful-generalist"
REPO_TEMPLATE = "acme/agent-{name}"

GROUP_A = "growth"
GROUP_B = "design-team"
ADMIN_GROUP = "infra-engineering"

AUDIENCE = "test-iap-audience"
CLIENT_ID = "test-mcp-client"
WORKSPACE_DOMAIN = "example.com"
ISSUER_BASE_URL = "https://kobo.example.org"

OWNER_A_EMAIL = "owner-a@example.com"
OWNER_B_EMAIL = "owner-b@example.com"
MEMBER_A_EMAIL = "member-a@example.com"
OPERATOR_EMAIL = "ops@example.com"
ADMIN_MEMBER_EMAIL = "admin-member@example.com"
STRANGER_EMAIL = "stranger@example.com"


class FakeGroupResolver:
    """The plainest possible `GroupResolver` stand-in, borrowed from
    `tests/test_mcp_authz.py`: a bare object exposing `groups_for(email)
    -> list[str] | None`, mapping-driven, or always-`None` ("the
    directory is down") when `down=True`."""

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
            ADMIN_MEMBER_EMAIL: [ADMIN_GROUP],
        }
    )


@pytest.fixture(autouse=True)
def _reset_rate_limit_and_audit():
    """Every test starts with a clean sliding window and an unconfigured
    audit layer -- same discipline `test_mcp_audit.py` holds, needed here
    too since `build_mcp_server` calls `audit.configure` for real."""
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
        admin_groups=[ADMIN_GROUP],
        groups=[
            GroupChoice(name=GROUP_A, okta_group="okta-growth"),
            GroupChoice(name=GROUP_B, okta_group="okta-design-team"),
            GroupChoice(name=ADMIN_GROUP, okta_group="okta-infra-engineering"),
        ],
        iap_audience=AUDIENCE,
        mcp=MCPConfig(
            oauth_client_id=CLIENT_ID,
            workspace_domain=WORKSPACE_DOMAIN,
            issuer_base_url=ISSUER_BASE_URL,
        ),
    )


def _app(session_factory, config, *, groups_resolver=None, cabal=None):
    clients = Clients(
        github=FakeGitHub(),
        gojira=FakeGojira(),
        cloud=FakeCloud(),
        agent=FakeAgent(),
        kubectl=FakeKubectl(),
        groups=groups_resolver,
        cabal=cabal,
    )
    return create_app(session_factory, config=config, clients=clients)


def _seed_agent(
    session_factory,
    *,
    name,
    created_by=None,
    group_name=None,
    team=None,
    purpose_tags=None,
    status=AgentStatus.READY,
):
    session = session_factory()
    session.add(
        Agent(
            name=name,
            persona=PERSONA,
            model_id="vertex_ai/claude-sonnet-4-5",
            status=status,
            repo_full_name=REPO_TEMPLATE.format(name=name),
            created_by=created_by,
            group_name=group_name,
            team=team,
            purpose_tags=encode_tags(purpose_tags) if purpose_tags else None,
        )
    )
    session.commit()
    session.close()


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


# --- build_mcp_server / _KoboTokenVerifier / get_user_email ----------------


def test_build_mcp_server_registers_the_four_read_tools(session_factory, config):
    app = _app(session_factory, config, groups_resolver=_resolver())

    mcp = build_mcp_server(app)

    # Task 8 adds the six write tools alongside these on the same `mcp` --
    # this assertion only pins that the four READ tools are still exactly
    # these four (a superset check, not equality), so this file stays
    # focused on read-tool registration without needing to track the
    # write-tools module's own tool set too (see test_mcp_tools_write.py).
    names = {tool.name for tool in mcp._tool_manager.list_tools()}
    assert {"kobo_whoami", "kobo_list_agents", "kobo_get_agent", "kobo_feed"} <= names


def test_build_mcp_server_configures_audit_with_the_apps_session_factory(session_factory, config):
    app = _app(session_factory, config, groups_resolver=_resolver())

    build_mcp_server(app)

    assert audit._session_factory is session_factory


def test_token_verifier_returns_access_token_with_user_email_for_a_valid_token(
    session_factory, config
):
    app = _app(session_factory, config, groups_resolver=_resolver())
    verifier = _KoboTokenVerifier(app)
    token = mint("access_token", OWNER_A_EMAIL, CLIENT_ID, 900)

    result = asyncio.run(verifier.verify_token(token))

    assert isinstance(result, _KoboAccessToken)
    assert result.user_email == OWNER_A_EMAIL
    assert result.subject == OWNER_A_EMAIL


def test_token_verifier_returns_none_for_a_garbage_token(session_factory, config):
    app = _app(session_factory, config, groups_resolver=_resolver())
    verifier = _KoboTokenVerifier(app)

    assert asyncio.run(verifier.verify_token("not-a-jwt")) is None


def test_token_verifier_returns_none_for_the_wrong_token_type(session_factory, config):
    app = _app(session_factory, config, groups_resolver=_resolver())
    verifier = _KoboTokenVerifier(app)
    # A refresh_token minted for the same subject/audience -- verify_token
    # must reject it: an access token check is typ-specific.
    token = mint("refresh_token", OWNER_A_EMAIL, CLIENT_ID, 900)

    assert asyncio.run(verifier.verify_token(token)) is None


def test_token_verifier_returns_none_for_the_wrong_audience(session_factory, config):
    app = _app(session_factory, config, groups_resolver=_resolver())
    verifier = _KoboTokenVerifier(app)
    token = mint("access_token", OWNER_A_EMAIL, "some-other-client", 900)

    assert asyncio.run(verifier.verify_token(token)) is None


def test_get_user_email_reads_the_full_chain():
    assert get_user_email(_ctx_for(OWNER_A_EMAIL)) == OWNER_A_EMAIL


def test_get_user_email_missing_chain_returns_empty():
    class _Empty:
        pass

    assert get_user_email(_Empty()) == ""


def test_get_user_email_none_ctx_returns_empty():
    assert get_user_email(None) == ""


# --- kobo_whoami ------------------------------------------------------------


def test_whoami_reports_email_and_resolved_groups(session_factory, config):
    app = _app(session_factory, config, groups_resolver=_resolver())
    mcp = build_mcp_server(app)

    result = _call_tool(mcp, "kobo_whoami", _ctx_for(MEMBER_A_EMAIL))

    assert result == {
        "email": MEMBER_A_EMAIL,
        "operator": False,
        "groups": [GROUP_A],
        "groups_unavailable": False,
    }


def test_whoami_operator_flag_true_for_admin_group_member(session_factory, config):
    app = _app(session_factory, config, groups_resolver=_resolver())
    mcp = build_mcp_server(app)

    result = _call_tool(mcp, "kobo_whoami", _ctx_for(ADMIN_MEMBER_EMAIL))

    assert result["operator"] is True
    assert result["groups"] == [ADMIN_GROUP]


def test_whoami_operator_flag_true_for_plain_email_operator(session_factory, config):
    app = _app(session_factory, config, groups_resolver=_resolver())
    mcp = build_mcp_server(app)

    result = _call_tool(mcp, "kobo_whoami", _ctx_for(OPERATOR_EMAIL))

    assert result["operator"] is True


def test_whoami_groups_unavailable_when_the_resolver_is_down(session_factory, config):
    app = _app(session_factory, config, groups_resolver=FakeGroupResolver(down=True))
    mcp = build_mcp_server(app)

    result = _call_tool(mcp, "kobo_whoami", _ctx_for(MEMBER_A_EMAIL))

    assert result["groups"] == []
    assert result["groups_unavailable"] is True


def test_whoami_is_not_agent_scoped_a_stranger_with_no_agents_still_gets_an_answer(
    session_factory, config
):
    app = _app(session_factory, config, groups_resolver=_resolver())
    mcp = build_mcp_server(app)

    result = _call_tool(mcp, "kobo_whoami", _ctx_for(STRANGER_EMAIL))

    assert result["email"] == STRANGER_EMAIL
    assert result["operator"] is False


# --- kobo_list_agents --------------------------------------------------------


def test_list_agents_returns_name_group_team_owner_status_purpose(session_factory, config):
    _seed_agent(
        session_factory,
        name="agent-a",
        created_by=OWNER_A_EMAIL,
        group_name=GROUP_A,
        team="growth-team",
        purpose_tags=["billing", "support"],
    )
    app = _app(session_factory, config, groups_resolver=_resolver())
    mcp = build_mcp_server(app)

    result = _call_tool(mcp, "kobo_list_agents", _ctx_for(OWNER_A_EMAIL))

    assert result == [
        {
            "name": "agent-a",
            "group": GROUP_A,
            "team": "growth-team",
            "owner": OWNER_A_EMAIL,
            "status": AgentStatus.READY,
            "purpose": ["billing", "support"],
        }
    ]


def test_list_agents_visibility_parity_email_a_sees_its_own_not_email_bs(
    session_factory, config
):
    _seed_agent(session_factory, name="agent-a", created_by=OWNER_A_EMAIL, group_name=GROUP_A)
    _seed_agent(session_factory, name="agent-b", created_by=OWNER_B_EMAIL, group_name=GROUP_B)
    app = _app(session_factory, config, groups_resolver=_resolver())
    mcp = build_mcp_server(app)

    names_a = {row["name"] for row in _call_tool(mcp, "kobo_list_agents", _ctx_for(OWNER_A_EMAIL))}
    names_b = {row["name"] for row in _call_tool(mcp, "kobo_list_agents", _ctx_for(OWNER_B_EMAIL))}

    assert names_a == {"agent-a"}
    assert names_b == {"agent-b"}


def test_list_agents_operator_sees_every_agent(session_factory, config):
    _seed_agent(session_factory, name="agent-a", created_by=OWNER_A_EMAIL, group_name=GROUP_A)
    _seed_agent(session_factory, name="agent-b", created_by=OWNER_B_EMAIL, group_name=GROUP_B)
    app = _app(session_factory, config, groups_resolver=_resolver())
    mcp = build_mcp_server(app)

    names = {row["name"] for row in _call_tool(mcp, "kobo_list_agents", _ctx_for(OPERATOR_EMAIL))}

    assert names == {"agent-a", "agent-b"}


def test_list_agents_fails_closed_when_the_group_resolver_is_down(session_factory, config):
    # T5 coverage, folded in here: a member sees only what they own plus
    # the shared commons when the directory is down -- group-DERIVED
    # visibility must vanish, never widen, on a resolver outage. Exercised
    # THROUGH kobo_list_agents (not the authz shim directly), per this
    # task's brief.
    _seed_agent(session_factory, name="agent-owned", created_by=MEMBER_A_EMAIL)
    _seed_agent(session_factory, name="agent-shared", group_name=SHARED_GROUP)
    # Visible to MEMBER_A_EMAIL only via group membership (GROUP_A) when
    # the resolver actually answers -- must NOT show up while it's down.
    _seed_agent(session_factory, name="agent-group-gated", created_by=OWNER_A_EMAIL, group_name=GROUP_A)
    app = _app(session_factory, config, groups_resolver=FakeGroupResolver(down=True))
    mcp = build_mcp_server(app)

    names = {row["name"] for row in _call_tool(mcp, "kobo_list_agents", _ctx_for(MEMBER_A_EMAIL))}

    assert names == {"agent-owned", "agent-shared"}
    assert "agent-group-gated" not in names


# --- kobo_get_agent -----------------------------------------------------------


def test_get_agent_returns_a_detail_dict_with_status_checklist(session_factory, config):
    _seed_agent(
        session_factory,
        name="agent-a",
        created_by=OWNER_A_EMAIL,
        group_name=GROUP_A,
        team="growth-team",
        purpose_tags=["billing"],
    )
    session = session_factory()
    agent = session.query(Agent).filter_by(name="agent-a").one()
    agent.steps.append(AgentStep(name="clone", status="done", error=None))
    session.commit()
    session.close()

    app = _app(session_factory, config, groups_resolver=_resolver())
    mcp = build_mcp_server(app)

    result = _call_tool(mcp, "kobo_get_agent", _ctx_for(OWNER_A_EMAIL), name="agent-a")

    assert result["name"] == "agent-a"
    assert result["persona"] == PERSONA
    assert result["group"] == GROUP_A
    assert result["team"] == "growth-team"
    assert result["owner"] == OWNER_A_EMAIL
    assert result["purpose"] == ["billing"]
    assert result["status"] == AgentStatus.READY
    assert result["steps"] == [{"name": "clone", "status": "done", "error": None}]
    assert "created_at" in result


def test_get_agent_raises_tool_error_for_a_genuinely_missing_agent(session_factory, config):
    app = _app(session_factory, config, groups_resolver=_resolver())
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match="agent not found"):
        _call_tool(mcp, "kobo_get_agent", _ctx_for(MEMBER_A_EMAIL), name="ghost")


def test_get_agent_raises_the_identical_error_for_an_invisible_agent_no_existence_leak(
    session_factory, config
):
    _seed_agent(session_factory, name="agent-b", created_by=OWNER_B_EMAIL, group_name=GROUP_B)
    app = _app(session_factory, config, groups_resolver=_resolver())
    mcp = build_mcp_server(app)

    missing_message = None
    invisible_message = None
    try:
        _call_tool(mcp, "kobo_get_agent", _ctx_for(MEMBER_A_EMAIL), name="ghost")
    except ToolError as exc:
        missing_message = str(exc)
    try:
        _call_tool(mcp, "kobo_get_agent", _ctx_for(MEMBER_A_EMAIL), name="agent-b")
    except ToolError as exc:
        invisible_message = str(exc)

    assert missing_message is not None
    assert missing_message == invisible_message


# --- kobo_feed ----------------------------------------------------------------


def _task_result(*, ask_text: str, reply_text: str) -> dict:
    # Real daedalus shape: history holds the FULL transcript [question, reply],
    # not just the question. history[-1] is therefore the AGENT reply -- the
    # feed must take the ask from the user-role message. See the same fixture
    # + the flatten fix in test_api_colleagues / main._flatten_conversation_entries.
    return {
        "id": "irrelevant-to-the-feed",
        "status": {
            "state": "completed",
            "message": {"role": "agent", "parts": [{"kind": "text", "text": reply_text}]},
        },
        "history": [
            {"role": "user", "parts": [{"kind": "text", "text": ask_text}]},
            {"role": "agent", "parts": [{"kind": "text", "text": reply_text}]},
        ],
    }


def _feed_summary(task_id: str, *, target: str, caller: str) -> dict:
    return {
        "task_id": task_id,
        "target_agent": target,
        "target_generation": 1,
        "caller_agent": caller,
        "state": "completed",
        "cabal_escalated_from": None,
        "created_at": "unused-by-the-feed-tool",
        "updated_at": "unused-by-the-feed-tool",
        "result_snippet": "unused-by-the-feed-tool",
    }


def _feed_detail(task_id, *, target, caller, created_at, updated_at, ask_text, reply_text) -> dict:
    return {
        "conversation": {
            "task_id": task_id,
            "target_agent": target,
            "caller_agent": caller,
            "created_at": created_at,
            "updated_at": updated_at,
            "result": _task_result(ask_text=ask_text, reply_text=reply_text),
        },
        "events": [],
    }


def _enabled_config(config):
    return config.model_copy(
        update={
            "cabal_url": "http://cabal.internal",
            "agent_endpoint_template": "http://agent-{name}.default.svc.cluster.local",
        }
    )


def test_feed_visibility_gate_fires_before_the_switchboard_is_ever_asked(
    session_factory, config
):
    _seed_agent(session_factory, name="agent-b", created_by=OWNER_B_EMAIL, group_name=GROUP_B)
    fake_cabal = FakeCabal()
    cfg = _enabled_config(config)
    app = _app(session_factory, cfg, groups_resolver=_resolver(), cabal=fake_cabal)
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match="agent not found"):
        _call_tool(mcp, "kobo_feed", _ctx_for(MEMBER_A_EMAIL), agent="agent-b")

    assert fake_cabal.list_conversations_calls == []


def test_feed_switchboard_not_configured_raises_tool_error(session_factory, config):
    _seed_agent(session_factory, name="ward")
    app = _app(session_factory, config, groups_resolver=_resolver())
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match="switchboard is not configured"):
        _call_tool(mcp, "kobo_feed", _ctx_for(OPERATOR_EMAIL), agent="ward")


def test_feed_flattens_reusing_the_exact_route_helpers(session_factory, config, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    _seed_agent(session_factory, name="ward")
    summaries = [_feed_summary("t-1", target="network", caller="agent:ward")]
    detail = _feed_detail(
        "t-1",
        target="network",
        caller="agent:ward",
        created_at="2026-08-19T10:00:00Z",
        updated_at="2026-08-19T10:00:05Z",
        ask_text="What's the config?",
        reply_text="Here's the config.",
    )
    fake_cabal = FakeCabal(conversations_result=summaries, conversation_results={"t-1": detail})
    cfg = _enabled_config(config)
    app = _app(session_factory, cfg, groups_resolver=_resolver(), cabal=fake_cabal)
    mcp = build_mcp_server(app)

    result = _call_tool(mcp, "kobo_feed", _ctx_for(OPERATOR_EMAIL), agent="ward")

    assert result == {
        "entries": [
            {
                "ts": "2026-08-19T10:00:00Z",
                "conversation_id": "t-1",
                "from": "ward",
                "to": "network",
                "role": "ask",
                "text": "What's the config?",
            },
            {
                "ts": "2026-08-19T10:00:05Z",
                "conversation_id": "t-1",
                "from": "network",
                "to": "ward",
                "role": "reply",
                "text": "Here's the config.",
            },
        ],
        "truncated": False,
    }
    assert fake_cabal.list_conversations_calls[-2:] == [
        {"limit": 200, "target": "ward", "caller": None},
        {"limit": 200, "target": None, "caller": "agent:ward"},
    ]


def test_feed_caps_at_the_callers_limit_when_smaller_than_the_scrollback_cap(
    session_factory, config, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    _seed_agent(session_factory, name="ward")
    n = 10
    summaries = [_feed_summary(f"t-{i}", target="network", caller="agent:ward") for i in range(n)]
    details = {
        f"t-{i}": _feed_detail(
            f"t-{i}",
            target="network",
            caller="agent:ward",
            created_at=f"2026-08-19T10:{i:02d}:00Z",
            updated_at=f"2026-08-19T10:{i:02d}:05Z",
            ask_text=f"ask {i}",
            reply_text=f"reply {i}",
        )
        for i in range(n)
    }
    fake_cabal = FakeCabal(conversations_result=summaries, conversation_results=details)
    cfg = _enabled_config(config)
    app = _app(session_factory, cfg, groups_resolver=_resolver(), cabal=fake_cabal)
    mcp = build_mcp_server(app)

    result = _call_tool(mcp, "kobo_feed", _ctx_for(OPERATOR_EMAIL), agent="ward", limit=5)

    assert len(result["entries"]) == 5
    assert result["truncated"] is True
    # Newest last -- the most recent asks/replies survive the cap.
    assert result["entries"][-1]["text"] == "reply 9"


@pytest.mark.parametrize("limit", [0, -1, -50])
def test_feed_non_positive_limit_returns_empty_not_the_whole_feed(
    session_factory, config, monkeypatch, limit
):
    """A zero or negative limit must floor at an empty feed, not slice
    `entries[-0:]` (Python's -0 == 0) and hand back everything while
    claiming truncation -- the review-caught inversion of a zero-length ask."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    _seed_agent(session_factory, name="ward")
    n = 3
    summaries = [_feed_summary(f"t-{i}", target="network", caller="agent:ward") for i in range(n)]
    details = {
        f"t-{i}": _feed_detail(
            f"t-{i}",
            target="network",
            caller="agent:ward",
            created_at=f"2026-08-19T10:{i:02d}:00Z",
            updated_at=f"2026-08-19T10:{i:02d}:05Z",
            ask_text=f"ask {i}",
            reply_text=f"reply {i}",
        )
        for i in range(n)
    }
    fake_cabal = FakeCabal(conversations_result=summaries, conversation_results=details)
    cfg = _enabled_config(config)
    app = _app(session_factory, cfg, groups_resolver=_resolver(), cabal=fake_cabal)
    mcp = build_mcp_server(app)

    result = _call_tool(mcp, "kobo_feed", _ctx_for(OPERATOR_EMAIL), agent="ward", limit=limit)

    assert result["entries"] == []
    assert result["truncated"] is True  # entries existed; the ask asked for none


def test_feed_upstream_cabal_error_raises_tool_error(session_factory, config, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    _seed_agent(session_factory, name="ward")
    fake_cabal = FakeCabal(conversations_error="switchboard unreachable")
    cfg = _enabled_config(config)
    app = _app(session_factory, cfg, groups_resolver=_resolver(), cabal=fake_cabal)
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match="did not answer"):
        _call_tool(mcp, "kobo_feed", _ctx_for(OPERATOR_EMAIL), agent="ward")
