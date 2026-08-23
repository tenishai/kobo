"""Tests for the MCP authz shim (MCP server arc, Task 5):
`kobo_api.mcp.authz` turns a verified email into a synthetic room
`Request` and calls straight through to the EXISTING access-control gates
in `kobo_api.main` -- `_visible_agents`, `_visible_agent_or_404`,
`_owner_or_operator_or_403`, `_is_admin`. No new authz logic is under
test here; these tests pin PARITY (the shim answers exactly what the real
route-level gates already answer for the same email/agent) and the
403-to-404 NORMALIZATION (a visible-but-not-owned agent must raise the
identical `NotFound` a genuinely missing agent raises -- never anything
403-shaped survives past this module).

Fixture style borrowed from `tests/test_web_admin_groups.py` (two-group +
admin-group setup, `FakeGroupResolver`) and `tests/test_api_agents.py`
(`session_factory`/`config`/`clients` shape) -- no real IAP JWTs are
needed here since the shim never goes through `IapMiddleware` at all; it
builds `request.state.iap_email` directly, which is the entire point of
`room_request`.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.clients import Clients
from kobo_api.config import SHARED_GROUP, Config, GroupChoice
from kobo_api.main import create_app
from kobo_api.mcp.authz import (
    NotFound,
    is_admin,
    require_owner_or_operator,
    room_request,
    visible_agent_or_not_found,
    visible_agents,
)
from kobo_api.models import Agent, AgentStatus, Base
from tests.fakes import FakeAgent, FakeCloud, FakeGitHub, FakeGojira, FakeKubectl

PERSONA = "helpful-generalist"
REPO_TEMPLATE = "acme/agent-{name}"

GROUP_A = "growth"
GROUP_B = "design-team"
ADMIN_GROUP = "infra-engineering"

AUDIENCE = "test-audience"

OWNER_A_EMAIL = "owner-a@example.com"
OWNER_B_EMAIL = "owner-b@example.com"
MEMBER_A_EMAIL = "member-a@example.com"
OPERATOR_EMAIL = "ops@example.com"
ADMIN_MEMBER_EMAIL = "admin-member@example.com"
STRANGER_EMAIL = "stranger@example.com"


class FakeGroupResolver:
    """The plainest possible `GroupResolver` stand-in (borrowed verbatim
    in spirit from `tests/test_web_admin_groups.py`): a bare object
    exposing `groups_for(email) -> list[str] | None`, mapping-driven."""

    def __init__(self, mapping: dict[str, list[str]] | None = None, *, down: bool = False):
        self._mapping = dict(mapping) if mapping else {}
        self._down = down

    def groups_for(self, email: str) -> list[str] | None:
        if self._down:
            return None
        return self._mapping.get(email.lower(), [])


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
    )


def _resolver():
    return FakeGroupResolver(
        {
            MEMBER_A_EMAIL: [GROUP_A],
            OWNER_A_EMAIL: [GROUP_A],
            OWNER_B_EMAIL: [GROUP_B],
            ADMIN_MEMBER_EMAIL: [ADMIN_GROUP],
        }
    )


def _app(session_factory, config, *, groups_resolver=None):
    clients = Clients(
        github=FakeGitHub(),
        gojira=FakeGojira(),
        cloud=FakeCloud(),
        agent=FakeAgent(),
        kubectl=FakeKubectl(),
        groups=groups_resolver,
    )
    return create_app(session_factory, config=config, clients=clients)


def _seed_agent(session_factory, *, name, created_by=None, group_name=None):
    session = session_factory()
    session.add(
        Agent(
            name=name,
            persona=PERSONA,
            model_id="vertex_ai/claude-sonnet-4-5",
            status=AgentStatus.READY,
            repo_full_name=REPO_TEMPLATE.format(name=name),
            created_by=created_by,
            group_name=group_name,
        )
    )
    session.commit()
    session.close()


@pytest.fixture
def seeded(session_factory):
    _seed_agent(session_factory, name="agent-a", created_by=OWNER_A_EMAIL, group_name=GROUP_A)
    _seed_agent(session_factory, name="agent-b", created_by=OWNER_B_EMAIL, group_name=GROUP_B)
    _seed_agent(session_factory, name="agent-shared", group_name=SHARED_GROUP)
    return session_factory


# --- room_request -------------------------------------------------------


def test_room_request_carries_the_real_app_and_the_stamped_email(session_factory, config):
    app = _app(session_factory, config, groups_resolver=_resolver())

    req = room_request(app, MEMBER_A_EMAIL)

    assert req.app is app
    assert req.state.iap_email == MEMBER_A_EMAIL


# --- visible_agents: group + shared-commons parity -----------------------


def test_member_sees_exactly_their_group_plus_shared(seeded, config):
    app = _app(seeded, config, groups_resolver=_resolver())
    session = seeded()

    names = {a.name for a in visible_agents(session, app, MEMBER_A_EMAIL)}

    assert names == {"agent-a", "agent-shared"}


def test_stranger_to_every_group_sees_only_shared(seeded, config):
    app = _app(seeded, config, groups_resolver=_resolver())
    session = seeded()

    names = {a.name for a in visible_agents(session, app, STRANGER_EMAIL)}

    assert names == {"agent-shared"}


def test_operator_sees_everything(seeded, config):
    app = _app(seeded, config, groups_resolver=_resolver())
    session = seeded()

    names = {a.name for a in visible_agents(session, app, OPERATOR_EMAIL)}

    assert names == {"agent-a", "agent-b", "agent-shared"}


def test_admin_group_member_sees_everything(seeded, config):
    app = _app(seeded, config, groups_resolver=_resolver())
    session = seeded()

    names = {a.name for a in visible_agents(session, app, ADMIN_MEMBER_EMAIL)}

    assert names == {"agent-a", "agent-b", "agent-shared"}


# --- visible_agent_or_not_found -------------------------------------------


def test_visible_agent_or_not_found_returns_the_agent_when_visible(seeded, config):
    app = _app(seeded, config, groups_resolver=_resolver())
    session = seeded()

    agent = visible_agent_or_not_found(session, app, MEMBER_A_EMAIL, "agent-a")

    assert agent.name == "agent-a"


def test_visible_agent_or_not_found_raises_for_the_other_groups_agent(seeded, config):
    app = _app(seeded, config, groups_resolver=_resolver())
    session = seeded()

    with pytest.raises(NotFound):
        visible_agent_or_not_found(session, app, MEMBER_A_EMAIL, "agent-b")


def test_visible_agent_or_not_found_raises_for_a_genuinely_missing_agent(seeded, config):
    app = _app(seeded, config, groups_resolver=_resolver())
    session = seeded()

    with pytest.raises(NotFound):
        visible_agent_or_not_found(session, app, MEMBER_A_EMAIL, "ghost")


# --- require_owner_or_operator: the 403->404 normalization ----------------


def test_require_owner_or_operator_passes_for_the_owner(seeded, config):
    app = _app(seeded, config, groups_resolver=_resolver())
    session = seeded()
    agent = visible_agent_or_not_found(session, app, OWNER_A_EMAIL, "agent-a")

    require_owner_or_operator(agent, app, OWNER_A_EMAIL)  # must not raise


def test_require_owner_or_operator_passes_for_the_operator(seeded, config):
    app = _app(seeded, config, groups_resolver=_resolver())
    session = seeded()
    agent = visible_agent_or_not_found(session, app, OPERATOR_EMAIL, "agent-a")

    require_owner_or_operator(agent, app, OPERATOR_EMAIL)  # must not raise


def test_require_owner_or_operator_passes_for_an_admin_group_member(seeded, config):
    app = _app(seeded, config, groups_resolver=_resolver())
    session = seeded()
    agent = visible_agent_or_not_found(session, app, ADMIN_MEMBER_EMAIL, "agent-a")

    require_owner_or_operator(agent, app, ADMIN_MEMBER_EMAIL)  # must not raise


def test_require_owner_or_operator_raises_not_found_not_forbidden_for_a_visible_agent(
    seeded, config
):
    # The normalization pin: agent-shared is visible to MEMBER_A_EMAIL (the
    # commons), but they neither own it nor hold operator standing. The
    # underlying gate (`_owner_or_operator_or_403`) answers a 403-shaped
    # JSONResponse for exactly this case -- this must surface as NotFound,
    # never that JSONResponse or any 403-flavored exception.
    app = _app(seeded, config, groups_resolver=_resolver())
    session = seeded()
    agent = visible_agent_or_not_found(session, app, MEMBER_A_EMAIL, "agent-shared")

    with pytest.raises(NotFound):
        require_owner_or_operator(agent, app, MEMBER_A_EMAIL)


# --- is_admin --------------------------------------------------------------


def test_is_admin_true_for_operator(session_factory, config):
    app = _app(session_factory, config, groups_resolver=_resolver())

    assert is_admin(app, OPERATOR_EMAIL) is True


def test_is_admin_true_for_admin_group_member(session_factory, config):
    app = _app(session_factory, config, groups_resolver=_resolver())

    assert is_admin(app, ADMIN_MEMBER_EMAIL) is True


def test_is_admin_false_for_plain_member(session_factory, config):
    app = _app(session_factory, config, groups_resolver=_resolver())

    assert is_admin(app, MEMBER_A_EMAIL) is False
