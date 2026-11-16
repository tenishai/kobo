"""Access control: who may see, change and pair with what.

The visibility model (owned, group, shared, grants, readers, admin groups,
and the honest fallbacks when the directory is down), ownership at birth,
persona team gates, the consent model between agents (implicit pairs within
a group, invites across groups, the switchboard push that enforces it),
thread privacy, and the permission service's leases and organizations.
Folded on 2026-10-29 from test_web_visibility.py, test_api_grants.py,
test_web_admin_groups.py, test_mcp_authz.py, test_api_personas.py,
test_api_invites.py, test_pairs.py, test_api_threads.py, test_steps.py,
test_api_access.py, test_api_admin_integrations.py and others.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from types import SimpleNamespace

import httpx
import jwt
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.context import RequestContext
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api import admin_store
from kobo_api.clients import (
    COMMIT_AUTHOR_EMAIL,
    BranchComparison,
    ClientError,
    Clients,
    InstallationRepo,
)
from kobo_api.composer import BundleSpec, compose
from kobo_api.config import SHARED_GROUP, Config, GroupChoice, MCPConfig
from kobo_api.connect_scan import REPO_NOT_IN_ORG, scan_repo
from kobo_api.groups import OktaGroupResolver
from kobo_api.iap import ASSERTION_HEADER, IAP_ISSUER
from kobo_api.kinds import KIND_COMPOSED, KIND_CONNECTED, RUN_MODE_HTTP
from kobo_api.liminal_admin import TARGETS_UNSUPPORTED, GatewayGrant, TargetNotFound
from kobo_api.main import _flatten_conversation_entries, _is_admin, create_app
from kobo_api.mcp import audit, rate_limit
from kobo_api.mcp.authz import (
    NotFound,
    require_owner_or_operator,
    visible_agent_or_not_found,
    visible_agents,
)
from kobo_api.mcp.server import build_mcp_server
from kobo_api.models import (
    INVITE_ACCEPTED,
    LEARNING_APPROVED,
    LEARNING_DENIED,
    LEARNING_PENDING,
    A2AInvite,
    Agent,
    AgentLearning,
    AgentStatus,
    AgentUserGrant,
    Base,
)
from kobo_api.pairs import (
    allowed_pairs,
    implicitly_paired,
    sync_dispatch_pairs,
    sync_dispatch_pairs_best_effort,
)
from kobo_api.personas import seed_default_personas
from kobo_api.provisioner import Ctx, run_pipeline
from kobo_api.purpose_tags import encode_tags
from kobo_api.repo_search import RepoIndex
from kobo_api.slack_state import encode_channels
from kobo_api.steps import (
    AccessStep,
    OnboardStep,
    StagedSkill,
    _agents_who_can_see,
    _colleague_roster,
    refresh_colleague_rosters,
)
from tests.fakes import (
    GCP_CATALOGUE,
    FakeAgent,
    FakeBab,
    FakeCabal,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeImage,
    FakeKubectl,
    FakeName,
    FakeSero,
    FakeSlack,
    FakeStorage,
)

# --- from test_web_visibility.py ---


PERSONA = "helpful-generalist"


REPO_TEMPLATE = "acme/agent-{name}"


OPERATOR_EMAIL = "ops@example.com"


OWNER_EMAIL = "owner@example.com"


MEMBER_EMAIL = "member@example.com"


STRANGER_EMAIL = "stranger@example.com"


AUDIENCE = "test-audience"


KID = "test-key-1"


GROUPS = [
    GroupChoice(name="payments-eng", okta_group="okta-payments"),
    GroupChoice(name="growth", okta_group="okta-growth"),
]


class FakeGroupResolver:
    """The plainest possible stand-in for `GroupResolver` (kobo_api.groups):
    a bare object exposing `groups_for(email) -> list[str] | None`, nothing
    more -- exactly the Protocol shape, so a test using this can never
    accidentally depend on anything the real `OktaGroupResolver` carries
    beyond it.

    `mapping` answers a known email with its configured groups; any email
    not in it answers `[]` (a directory that successfully reports "belongs
    to nothing"), UNLESS `down=True`, in which case every call answers
    `None` regardless of `mapping` -- the fail-closed "the directory itself
    couldn't be reached" case (kobo_api.groups' own None/`[]` distinction).
    """

    def __init__(self, mapping: dict[str, list[str]] | None = None, *, down: bool = False):
        self._mapping = dict(mapping) if mapping else {}
        self._down = down

    def groups_for(self, email: str) -> list[str] | None:
        if self._down:
            return None
        return self._mapping.get(email.lower(), [])


def _generate_jwk(kid: str = KID):
    private_key = ec.generate_private_key(ec.SECP256R1())
    jwk = jwt.algorithms.ECAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk["kid"] = kid
    jwk["use"] = "sig"
    jwk["alg"] = "ES256"
    return private_key, jwk


def _sign(private_key, *, email: str | None, kid: str = KID) -> str:
    now = int(time.time())
    claims = {
        "aud": AUDIENCE,
        "iss": IAP_ISSUER,
        "iat": now,
        "exp": now + 3600,
        "sub": "accounts.google.com:1234567890",
    }
    if email is not None:
        claims["email"] = email
    return jwt.encode(claims, private_key, algorithm="ES256", headers={"kid": kid})


def _fetcher(jwk):
    def fetch():
        return {"keys": [jwk]}

    return fetch


def _headers(private_key, *, email: str) -> dict:
    return {ASSERTION_HEADER: _sign(private_key, email=email)}


@pytest.fixture
def session_factory__web_visibility(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__web_visibility(tmp_path):
    return Config(
        workdir=tmp_path / "work",
        operators=[OPERATOR_EMAIL],
        iap_audience=AUDIENCE,
        groups=GROUPS,
    )


def _make_client(session_factory__web_visibility, config__web_visibility, *, groups_resolver=None, iap_jwks_fetcher):
    return TestClient(
        create_app(
            session_factory__web_visibility,
            config=config__web_visibility,
            clients=Clients(
                github=FakeGitHub(),
                gojira=FakeGojira(),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=FakeKubectl(),
                storage=FakeStorage(),
                groups=groups_resolver,
            ),
            iap_jwks_fetcher=iap_jwks_fetcher,
        )
    )


def _seed_agent(session_factory__web_visibility, config__web_visibility, *, name, created_by=None, group_name=None):
    session = session_factory__web_visibility()
    session.add(
        Agent(
            name=name,
            persona=PERSONA,
            model_id=config__web_visibility.models[0].id,
            status=AgentStatus.READY,
            repo_full_name=REPO_TEMPLATE.format(name=name),
            created_by=created_by,
            group_name=group_name,
        )
    )
    session.commit()
    session.close()


def _seed_matrix(session_factory__web_visibility, config__web_visibility):
    _seed_agent(session_factory__web_visibility, config__web_visibility, name="group-agent", group_name="payments-eng")
    _seed_agent(session_factory__web_visibility, config__web_visibility, name="owner-agent", created_by=OWNER_EMAIL, group_name="growth")
    _seed_agent(session_factory__web_visibility, config__web_visibility, name="invisible-agent", group_name="growth")


@pytest.fixture
def consent_config(tmp_path):
    return Config(
        workdir=tmp_path / "work",
        skills_bucket="skills-bucket",
        personas_bucket="personas-bucket",
        operators=[OPERATOR_EMAIL],
        iap_audience=AUDIENCE,
        groups=GROUPS,
    )


def _consent_client(
    session_factory__web_visibility,
    consent_config,
    fake_storage,
    *,
    groups_resolver,
    iap_jwks_fetcher,
    github=None,
):
    return TestClient(
        create_app(
            session_factory__web_visibility,
            config=consent_config,
            clients=Clients(
                github=github if github is not None else FakeGitHub(),
                gojira=FakeGojira(),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=FakeKubectl(),
                storage=fake_storage,
                groups=groups_resolver,
            ),
            iap_jwks_fetcher=iap_jwks_fetcher,
        )
    )


def _create_payload(name, group_name):
    return {
        "name": name,
        "persona": "helpful-generalist",
        "team": "shared",
        "group_name": group_name,
    }


def test_operator_sees_every_agent(session_factory__web_visibility, config__web_visibility):
    """Gate (visibility): Operator sees every agent."""
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({MEMBER_EMAIL: ["payments-eng"]})
    client = _make_client(session_factory__web_visibility, config__web_visibility, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))
    _seed_matrix(session_factory__web_visibility, config__web_visibility)

    response = client.get("/api/agents", headers=_headers(private_key, email=OPERATOR_EMAIL))

    assert response.status_code == 200
    body = response.json()
    assert body["groups_unavailable"] is False
    assert {a["name"] for a in body["agents"]} == {"group-agent", "owner-agent", "invisible-agent"}


def test_group_member_sees_owned_union_group_agents(session_factory__web_visibility, config__web_visibility):
    """Gate (visibility): Group member sees owned union group agents."""
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({MEMBER_EMAIL: ["payments-eng"]})
    client = _make_client(session_factory__web_visibility, config__web_visibility, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))
    _seed_matrix(session_factory__web_visibility, config__web_visibility)

    response = client.get("/api/agents", headers=_headers(private_key, email=MEMBER_EMAIL))

    assert response.status_code == 200
    body = response.json()
    assert body["groups_unavailable"] is False
    assert {a["name"] for a in body["agents"]} == {"group-agent"}


def test_owner_sees_own_agent_even_outside_their_groups(session_factory__web_visibility, config__web_visibility):
    """Gate (visibility): Owner sees own agent even outside their groups."""
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({OWNER_EMAIL: ["payments-eng"]})
    client = _make_client(session_factory__web_visibility, config__web_visibility, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))
    _seed_matrix(session_factory__web_visibility, config__web_visibility)

    response = client.get("/api/agents", headers=_headers(private_key, email=OWNER_EMAIL))

    assert response.status_code == 200
    body = response.json()
    # OWNER_EMAIL is a payments-eng member (sees group-agent too) AND owns
    # owner-agent (a growth-namespaced agent OWNER_EMAIL doesn't belong to
    # by group) -- both routes into visibility fire, "invisible-agent" is
    # neither owned nor in a group OWNER_EMAIL belongs to.
    assert {a["name"] for a in body["agents"]} == {"group-agent", "owner-agent"}


def test_stranger_sees_nothing_they_neither_own_nor_belong_to(session_factory__web_visibility, config__web_visibility):
    """Gate (visibility): Stranger sees nothing they neither own nor belong to."""
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({MEMBER_EMAIL: ["payments-eng"]})
    client = _make_client(session_factory__web_visibility, config__web_visibility, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))
    _seed_matrix(session_factory__web_visibility, config__web_visibility)

    response = client.get("/api/agents", headers=_headers(private_key, email=STRANGER_EMAIL))

    assert response.status_code == 200
    body = response.json()
    assert body["groups_unavailable"] is False
    assert body["agents"] == []


def test_resolver_down_falls_back_to_owned_only_and_flags_unavailable(session_factory__web_visibility, config__web_visibility):
    # Fail-closed (Global Constraint): the directory is unreachable, so
    # group-derived visibility vanishes -- owner rights survive, nothing
    # wider. MEMBER_EMAIL owns "member-owned" and would normally also see
    # "group-agent" via payments-eng membership, but not while the
    # resolver is down.
    """Gate (visibility): Resolver down falls back to owned only and flags unavailable."""
    _seed_agent(session_factory__web_visibility, config__web_visibility, name="member-owned", created_by=MEMBER_EMAIL)
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver(down=True)
    client = _make_client(session_factory__web_visibility, config__web_visibility, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))
    _seed_matrix(session_factory__web_visibility, config__web_visibility)

    response = client.get("/api/agents", headers=_headers(private_key, email=MEMBER_EMAIL))

    assert response.status_code == 200
    body = response.json()
    assert body["groups_unavailable"] is True
    assert {a["name"] for a in body["agents"]} == {"member-owned"}


def test_no_resolver_configured_is_the_honest_phase_gate_everyone_sees_everything(
    session_factory__web_visibility, config__web_visibility
):
    # Absent token = groups feature off entirely (kobo_api.clients.Clients.
    # real's own gate) -- the room behaves exactly as it did before this
    # arc, plus ownership: full visibility, never narrower just because
    # groups haven't been turned on.
    """Gate (visibility): No resolver configured is the honest phase gate everyone sees everything."""
    private_key, jwk = _generate_jwk()
    client = _make_client(session_factory__web_visibility, config__web_visibility, groups_resolver=None, iap_jwks_fetcher=_fetcher(jwk))
    _seed_matrix(session_factory__web_visibility, config__web_visibility)

    response = client.get("/api/agents", headers=_headers(private_key, email=STRANGER_EMAIL))

    assert response.status_code == 200
    body = response.json()
    assert body["groups_unavailable"] is False
    assert {a["name"] for a in body["agents"]} == {"group-agent", "owner-agent", "invisible-agent"}


def test_iap_disabled_is_full_visibility_same_as_no_resolver(session_factory__web_visibility):
    # Dev parity: no IAP means no verified identity to filter by at all --
    # same escape hatch every other gate in main.py uses.
    """Gate (visibility): Iap disabled is full visibility same as no resolver."""
    config__web_visibility = Config(operators=[], iap_audience="", groups=GROUPS)
    client = TestClient(
        create_app(
            session_factory__web_visibility,
            config=config__web_visibility,
            clients=Clients(
                github=FakeGitHub(),
                gojira=FakeGojira(),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=FakeKubectl(),
                storage=FakeStorage(),
                groups=FakeGroupResolver({}),
            ),
        )
    )
    _seed_matrix(session_factory__web_visibility, config__web_visibility)

    response = client.get("/api/agents")

    assert response.status_code == 200
    body = response.json()
    assert body["groups_unavailable"] is False
    assert {a["name"] for a in body["agents"]} == {"group-agent", "owner-agent", "invisible-agent"}


def test_member_claiming_a_group_they_are_not_in_is_403_verbatim(
    session_factory__web_visibility, consent_config, fake_storage
):
    """Gate (visibility): Member claiming a group they are not in is 403 verbatim."""
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({MEMBER_EMAIL: ["payments-eng"]})
    client = _consent_client(
        session_factory__web_visibility, consent_config, fake_storage,
        groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk),
    )

    response = client.post(
        "/api/agents",
        json=_create_payload("clio", "growth"),
        headers=_headers(private_key, email=MEMBER_EMAIL),
    )

    assert response.status_code == 403
    assert response.json() == {"error": "You are not a member of growth."}


def test_resolver_down_member_create_is_403_with_honest_copy(
    session_factory__web_visibility, consent_config, fake_storage
):
    """Gate (visibility): Resolver down member create is 403 with honest copy."""
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver(down=True)
    client = _consent_client(
        session_factory__web_visibility, consent_config, fake_storage,
        groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk),
    )

    response = client.post(
        "/api/agents",
        json=_create_payload("clio", "payments-eng"),
        headers=_headers(private_key, email=MEMBER_EMAIL),
    )

    assert response.status_code == 403
    assert response.json() == {
        "error": "Group membership could not be confirmed right now. Try again shortly."
    }
    assert "—" not in response.json()["error"]


def test_member_connecting_into_a_group_they_are_not_in_is_403_verbatim(
    session_factory__web_visibility, consent_config, fake_storage
):
    """Gate (visibility): Member connecting into a group they are not in is 403 verbatim.

    Connect puts a new agent in a group exactly as create does, and runs
    the same gate to do it (`_group_choice_block`, one helper both routes
    call). The gate answers before the room reads the repository at all, so
    a refused connect has not told the caller anything about it either.
    """
    private_key, jwk = _generate_jwk()
    github = FakeGitHub(existing_repos={"example-org/team-chatbot"})
    client = _consent_client(
        session_factory__web_visibility, consent_config, fake_storage,
        groups_resolver=FakeGroupResolver({MEMBER_EMAIL: ["payments-eng"]}),
        iap_jwks_fetcher=_fetcher(jwk),
        github=github,
    )

    response = client.post(
        "/api/agents/connect",
        json={
            "repo": "example-org/team-chatbot",
            "name": "clio",
            "run_mode": "http",
            "group_name": "growth",
            "confirm_repo_owner": True,
        },
        headers=_headers(private_key, email=MEMBER_EMAIL),
    )

    assert response.status_code == 403
    assert response.json() == {"error": "You are not a member of growth."}
    assert github.calls == []


def test_shared_agent_visible_to_a_stranger_with_no_group_membership(
    session_factory__web_visibility, config__web_visibility
):
    """Gate (visibility): Shared agent visible to a stranger with no group membership."""
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({})
    client = _make_client(
        session_factory__web_visibility, config__web_visibility, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk)
    )
    _seed_agent(session_factory__web_visibility, config__web_visibility, name="shared-agent", group_name=SHARED_GROUP)
    _seed_matrix(session_factory__web_visibility, config__web_visibility)

    response = client.get("/api/agents", headers=_headers(private_key, email=STRANGER_EMAIL))

    assert response.status_code == 200
    body = response.json()
    assert body["groups_unavailable"] is False
    assert "shared-agent" in {a["name"] for a in body["agents"]}


def test_shared_agent_visible_even_when_the_resolver_is_down(session_factory__web_visibility, config__web_visibility):
    """Gate (visibility): Shared agent visible even when the resolver is down."""
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver(down=True)
    client = _make_client(
        session_factory__web_visibility, config__web_visibility, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk)
    )
    _seed_agent(session_factory__web_visibility, config__web_visibility, name="shared-agent", group_name=SHARED_GROUP)

    response = client.get("/api/agents", headers=_headers(private_key, email=STRANGER_EMAIL))

    assert response.status_code == 200
    body = response.json()
    # Fail-closed still narrows GROUP-derived visibility to nothing (no
    # ownership, no grant) -- the commons agent is the only thing left
    # standing, proving it isn't itself treated as a grant subject to the
    # same narrowing.
    assert body["groups_unavailable"] is True
    assert {a["name"] for a in body["agents"]} == {"shared-agent"}


def test_shared_group_create_bypasses_membership_check_even_when_resolver_is_down(
    session_factory__web_visibility, consent_config, fake_storage
):
    """Gate (visibility): Shared group create bypasses membership check even when resolver is down."""
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver(down=True)
    client = _consent_client(
        session_factory__web_visibility, consent_config, fake_storage,
        groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk),
    )

    response = client.post(
        "/api/agents",
        json=_create_payload("clio", SHARED_GROUP),
        headers=_headers(private_key, email=MEMBER_EMAIL),
    )

    assert response.status_code == 201, response.text


# --- from test_api_grants.py ---


PERSONA = "helpful-generalist"


REPO_TEMPLATE = "acme/agent-{name}"


OWNER_EMAIL = "owner@example.com"


OPERATOR_EMAIL = "ops@example.com"


GRANTEE_EMAIL = "grantee@example.com"


STRANGER_EMAIL = "stranger@example.com"


AUDIENCE = "test-audience"


KID = "test-key-1"


def _generate_jwk(kid: str = KID):
    private_key = ec.generate_private_key(ec.SECP256R1())
    jwk = jwt.algorithms.ECAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk["kid"] = kid
    jwk["use"] = "sig"
    jwk["alg"] = "ES256"
    return private_key, jwk


def _sign(private_key, *, email: str | None, kid: str = KID) -> str:
    now = int(time.time())
    claims = {
        "aud": AUDIENCE,
        "iss": IAP_ISSUER,
        "iat": now,
        "exp": now + 3600,
        "sub": "accounts.google.com:1234567890",
    }
    if email is not None:
        claims["email"] = email
    return jwt.encode(claims, private_key, algorithm="ES256", headers={"kid": kid})


def _fetcher(jwk):
    def fetch():
        return {"keys": [jwk]}

    return fetch


def _headers(private_key, *, email: str) -> dict:
    return {ASSERTION_HEADER: _sign(private_key, email=email)}


@pytest.fixture
def session_factory__api_grants(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__api_grants(tmp_path):
    return Config(workdir=tmp_path / "work", operators=[OPERATOR_EMAIL], iap_audience=AUDIENCE)


def _make_client__api_grants(session_factory__api_grants, config__api_grants, iap_jwks_fetcher, *, groups_resolver=None):
    return TestClient(
        create_app(
            session_factory__api_grants,
            config=config__api_grants,
            clients=Clients(
                github=FakeGitHub(),
                gojira=FakeGojira(),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=FakeKubectl(),
                groups=groups_resolver,
            ),
            iap_jwks_fetcher=iap_jwks_fetcher,
        )
    )


def _seed_agent__api_grants(session_factory__api_grants, *, name, created_by=None, group_name=None):
    session = session_factory__api_grants()
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


def _seed_grant(session_factory__api_grants, *, agent_name, email, added_by=None):
    session = session_factory__api_grants()
    session.add(AgentUserGrant(agent_name=agent_name, email=email, added_by=added_by))
    session.commit()
    session.close()


@pytest.fixture
def client__api_grants(session_factory__api_grants, config__api_grants):
    private_key, jwk = _generate_jwk()
    c = _make_client__api_grants(session_factory__api_grants, config__api_grants, _fetcher(jwk))
    return c, private_key


class _EmptyGroupResolver:
    def groups_for(self, email: str) -> list[str] | None:
        return []


class _RawResolver:
    """A resolver whose raw directory answer is scripted; configured-name
    translation deliberately returns nothing, so any visibility a reader
    gains comes from the reader branch alone, never group derivation."""

    def __init__(self, raw):
        self._raw = raw

    def groups_for(self, email):
        return []

    def raw_groups_for(self, email):
        return self._raw


def test_adding_the_same_grant_twice_is_idempotent(session_factory__api_grants, client__api_grants):
    """Gate (visibility): Adding the same grant twice is idempotent."""
    c, key = client__api_grants
    _seed_agent__api_grants(session_factory__api_grants, name="a", created_by=OWNER_EMAIL)

    first = c.post(
        "/api/agents/a/grants",
        json={"email": GRANTEE_EMAIL},
        headers=_headers(key, email=OWNER_EMAIL),
    )
    second = c.post(
        "/api/agents/a/grants",
        json={"email": GRANTEE_EMAIL},
        headers=_headers(key, email=OWNER_EMAIL),
    )

    assert first.status_code == 201
    assert second.status_code == 200

    listing = c.get("/api/agents/a/grants", headers=_headers(key, email=OWNER_EMAIL))
    assert len(listing.json()["grants"]) == 1


def test_reposting_a_grant_with_the_other_role_changes_it(session_factory__api_grants, client__api_grants):
    """Data integrity: a grant is one row per person; posting the other role
    (issue #309) changes that row, and the listing says which role it holds."""
    c, key = client__api_grants
    _seed_agent__api_grants(session_factory__api_grants, name="a", created_by=OWNER_EMAIL)
    owner = _headers(key, email=OWNER_EMAIL)

    first = c.post("/api/agents/a/grants", json={"email": GRANTEE_EMAIL}, headers=owner)
    second = c.post("/api/agents/a/grants", json={"email": GRANTEE_EMAIL, "role": "manage"}, headers=owner)

    assert first.json()["role"] == "view"
    assert second.status_code == 200
    listing = c.get("/api/agents/a/grants", headers=owner).json()["grants"]
    assert [g["role"] for g in listing] == ["manage"]


def test_granted_email_sees_the_agent_as_if_owned(session_factory__api_grants, config__api_grants):
    """Gate (visibility): Granted email sees the agent as if owned."""
    private_key, jwk = _generate_jwk()
    c = _make_client__api_grants(session_factory__api_grants, config__api_grants, _fetcher(jwk), groups_resolver=_EmptyGroupResolver())
    _seed_agent__api_grants(session_factory__api_grants, name="a", created_by=OWNER_EMAIL, group_name="growth")
    _seed_grant(session_factory__api_grants, agent_name="a", email=GRANTEE_EMAIL)

    response = c.get("/api/agents", headers=_headers(private_key, email=GRANTEE_EMAIL))

    assert response.status_code == 200
    assert {a["name"] for a in response.json()["agents"]} == {"a"}


def test_an_agent_moved_to_the_commons_is_seen_by_everyone(session_factory__api_grants, config__api_grants):
    """Gate (visibility): An agent moved to the commons is seen by everyone.

    Moving an agent to the commons has to do what creating one there
    does. It used to store no group at all, so the room said "shared" and
    every list outside the owner's came back without it."""
    private_key, jwk = _generate_jwk()
    c = _make_client__api_grants(session_factory__api_grants, config__api_grants, _fetcher(jwk), groups_resolver=_EmptyGroupResolver())
    _seed_agent__api_grants(session_factory__api_grants, name="a", created_by=OWNER_EMAIL, group_name="growth")

    before = c.get("/api/agents", headers=_headers(private_key, email=STRANGER_EMAIL))
    assert before.json()["agents"] == []

    moved = c.patch(
        "/api/agents/a/metadata",
        json={"group": "shared"},
        headers=_headers(private_key, email=OPERATOR_EMAIL),
    )
    assert moved.status_code == 200, moved.text

    after = c.get("/api/agents", headers=_headers(private_key, email=STRANGER_EMAIL))
    assert {a["name"] for a in after.json()["agents"]} == {"a"}


def test_grant_extends_visibility_even_when_the_group_resolver_is_down(session_factory__api_grants, config__api_grants):
    # A grant is kobo's own record, not Okta-derived -- it must survive an
    # Okta outage untouched (Global Constraint's "fail-closed on grants" is
    # about GROUP-derived visibility vanishing, not about rows in this
    # table).
    """Gate (visibility): Grant extends visibility even when the group resolver is down."""
    class DownResolver:
        def groups_for(self, email: str) -> list[str] | None:
            return None

    private_key, jwk = _generate_jwk()
    c = _make_client__api_grants(session_factory__api_grants, config__api_grants, _fetcher(jwk), groups_resolver=DownResolver())
    _seed_agent__api_grants(session_factory__api_grants, name="a", created_by=OWNER_EMAIL, group_name="growth")
    _seed_grant(session_factory__api_grants, agent_name="a", email=GRANTEE_EMAIL)

    response = c.get("/api/agents", headers=_headers(private_key, email=GRANTEE_EMAIL))

    assert response.status_code == 200
    body = response.json()
    assert body["groups_unavailable"] is True
    assert {a["name"] for a in body["agents"]} == {"a"}


def test_reader_group_member_sees_every_agent(session_factory__api_grants, config__api_grants, tmp_path):
    """Gate (visibility): Reader group member sees every agent."""
    private_key, jwk = _generate_jwk()
    config__api_grants = config__api_grants.model_copy(
        update={"reader_groups": ["engineering_operations_sre_lead"]}
    )
    _seed_agent__api_grants(session_factory__api_grants, name="ward", created_by=OWNER_EMAIL, group_name="infra-engineering")
    _seed_agent__api_grants(session_factory__api_grants, name="clio", created_by=OWNER_EMAIL, group_name="data-engineering")
    client__api_grants = _make_client__api_grants(
        session_factory__api_grants, config__api_grants, _fetcher(jwk),
        groups_resolver=_RawResolver(["engineering_operations_sre_lead", "unrelated_group"]),
    )
    body = client__api_grants.get("/api/agents", headers=_headers(private_key, email=STRANGER_EMAIL)).json()
    assert sorted(a["name"] for a in body["agents"]) == ["clio", "ward"]


def test_reader_gains_no_mutation_power(session_factory__api_grants, config__api_grants, tmp_path):
    """Gate (visibility): Reader gains no mutation power.

    Visibility only: a reader who can SEE an agent still cannot teach it,
    grant on it, or destroy it -- the owner-or-operator gates never consult
    the reader role."""
    private_key, jwk = _generate_jwk()
    config__api_grants = config__api_grants.model_copy(
        update={"reader_groups": ["engineering_operations_sre_lead"]}
    )
    _seed_agent__api_grants(session_factory__api_grants, name="ward", created_by=OWNER_EMAIL, group_name="infra-engineering")
    client__api_grants = _make_client__api_grants(
        session_factory__api_grants, config__api_grants, _fetcher(jwk),
        groups_resolver=_RawResolver(["engineering_operations_sre_lead"]),
    )
    headers = _headers(private_key, email=STRANGER_EMAIL)
    r = client__api_grants.post("/api/agents/ward/grants", json={"email": "x@example.com"}, headers=headers)
    assert r.status_code == 403
    r = client__api_grants.delete("/api/agents/ward", headers=headers)
    assert r.status_code == 403


def test_directory_outage_reads_as_not_a_reader(session_factory__api_grants, config__api_grants, tmp_path):
    """Gate (visibility): Directory outage reads as not a reader."""
    private_key, jwk = _generate_jwk()
    config__api_grants = config__api_grants.model_copy(
        update={"reader_groups": ["engineering_operations_sre_lead"]}
    )
    _seed_agent__api_grants(session_factory__api_grants, name="ward", created_by=OWNER_EMAIL, group_name="infra-engineering")

    class _DownResolver:
        def groups_for(self, email):
            return None

        def raw_groups_for(self, email):
            return None

    client__api_grants = _make_client__api_grants(
        session_factory__api_grants, config__api_grants, _fetcher(jwk), groups_resolver=_DownResolver()
    )
    body = client__api_grants.get("/api/agents", headers=_headers(private_key, email=STRANGER_EMAIL)).json()
    assert body["agents"] == []


# --- from test_web_admin_groups.py ---


AGENT_NAME = "clio"


PERSONA = "helpful-generalist"


REPO_TEMPLATE = "acme/agent-{name}"


ADMIN_GROUP = "infra-engineering"


OTHER_GROUP = "design-team"


OPERATOR_EMAIL = "ops@example.com"


ADMIN_MEMBER_EMAIL = "admin-member@example.com"


PLAIN_MEMBER_EMAIL = "member@example.com"


OWNER_EMAIL = "owner@example.com"


SECOND_OWNER_EMAIL = "owner-b@example.com"


AUDIENCE = "test-audience"


KID = "test-key-1"


class FakeGroupResolver__web_admin_groups:
    """The plainest possible stand-in for `GroupResolver` (kobo_api.groups):
    a bare object exposing `groups_for(email) -> list[str] | None`, nothing
    more -- the exact Protocol shape, so a test using this can never
    accidentally depend on anything the real `OktaGroupResolver` carries
    beyond it. `calls` records every email actually looked up, so a test can
    pin "the resolver was never even consulted" (the admin_groups-empty
    short circuit) rather than just "it answered nothing."
    """

    def __init__(self, mapping: dict[str, list[str]] | None = None, *, down: bool = False):
        self._mapping = dict(mapping) if mapping else {}
        self._down = down
        self.calls: list[str] = []

    def groups_for(self, email: str) -> list[str] | None:
        self.calls.append(email)
        if self._down:
            return None
        return self._mapping.get(email.lower(), [])


def _req(email: str | None, config__web_admin_groups: Config, clients=None):
    """A minimal stand-in for `Request` -- `_is_admin` only ever reads
    `request.state.iap_email` (via `getattr`), `request.app.state.config`,
    and `request.app.state.clients` (also via `getattr`, so a stand-in that
    omits `clients` entirely still works, matching test_web_ownership_gate.
    py's own `_req`)."""
    state = SimpleNamespace()
    if email is not None:
        state.iap_email = email
    return SimpleNamespace(
        state=state,
        app=SimpleNamespace(state=SimpleNamespace(config=config__web_admin_groups, clients=clients)),
    )


def _generate_jwk(kid: str = KID):
    private_key = ec.generate_private_key(ec.SECP256R1())
    jwk = jwt.algorithms.ECAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk["kid"] = kid
    jwk["use"] = "sig"
    jwk["alg"] = "ES256"
    return private_key, jwk


def _sign(private_key, *, email: str | None, kid: str = KID) -> str:
    now = int(time.time())
    claims = {
        "aud": AUDIENCE,
        "iss": IAP_ISSUER,
        "iat": now,
        "exp": now + 3600,
        "sub": "accounts.google.com:1234567890",
    }
    if email is not None:
        claims["email"] = email
    return jwt.encode(claims, private_key, algorithm="ES256", headers={"kid": kid})


def _fetcher(jwk):
    def fetch():
        return {"keys": [jwk]}

    return fetch


def _headers(private_key, *, email: str) -> dict:
    return {ASSERTION_HEADER: _sign(private_key, email=email)}


@pytest.fixture
def session_factory__web_admin_groups(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__web_admin_groups(tmp_path):
    return Config(
        workdir=tmp_path / "work",
        operators=[OPERATOR_EMAIL],
        admin_groups=[ADMIN_GROUP],
        groups=[GroupChoice(name=ADMIN_GROUP, okta_group="okta-infra-engineering")],
        iap_audience=AUDIENCE,
    )


def _make_client__web_admin_groups(session_factory__web_admin_groups, config__web_admin_groups, *, groups_resolver=None, storage=None, iap_jwks_fetcher):
    return TestClient(
        create_app(
            session_factory__web_admin_groups,
            config=config__web_admin_groups,
            clients=Clients(
                github=FakeGitHub(),
                gojira=FakeGojira(),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=FakeKubectl(),
                storage=storage if storage is not None else FakeStorage(),
                groups=groups_resolver,
            ),
            iap_jwks_fetcher=iap_jwks_fetcher,
        )
    )


def _seed_agent__web_admin_groups(
    session_factory__web_admin_groups,
    config__web_admin_groups,
    *,
    name: str,
    created_by: str | None = None,
    group_name: str | None = None,
    status=AgentStatus.READY,
):
    session = session_factory__web_admin_groups()
    session.add(
        Agent(
            name=name,
            persona=PERSONA,
            model_id=config__web_admin_groups.models[0].id,
            status=status,
            repo_full_name=REPO_TEMPLATE.format(name=name),
            created_by=created_by,
            group_name=group_name,
        )
    )
    session.commit()
    session.close()


def test_is_admin_resolver_down_group_grant_off():
    # The directory itself failed to answer for this email (fail-closed,
    # kobo_api.groups' own None-vs-[] distinction) -- grants nothing, same
    # as no resolver at all.
    """Gate (authorization): Is admin resolver down group grant off."""
    config__web_admin_groups = Config(
        admin_groups=[ADMIN_GROUP],
        groups=[GroupChoice(name=ADMIN_GROUP, okta_group="okta-infra-engineering")],
        iap_audience=AUDIENCE,
    )
    resolver = FakeGroupResolver__web_admin_groups(down=True)
    clients = SimpleNamespace(groups=resolver)

    assert _is_admin(_req(ADMIN_MEMBER_EMAIL, config__web_admin_groups, clients=clients)) is False


def test_admin_group_member_sees_every_agent(session_factory__web_admin_groups, config__web_admin_groups):
    """Gate (authorization): Admin group member sees every agent."""
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver__web_admin_groups(
        {ADMIN_MEMBER_EMAIL: [ADMIN_GROUP], PLAIN_MEMBER_EMAIL: [OTHER_GROUP]}
    )
    client = _make_client__web_admin_groups(
        session_factory__web_admin_groups, config__web_admin_groups, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk)
    )
    _seed_agent__web_admin_groups(session_factory__web_admin_groups, config__web_admin_groups, name="a", created_by=OWNER_EMAIL, group_name="growth")
    _seed_agent__web_admin_groups(
        session_factory__web_admin_groups, config__web_admin_groups, name="b", created_by=SECOND_OWNER_EMAIL, group_name="payments-eng"
    )

    response = client.get("/api/agents", headers=_headers(private_key, email=ADMIN_MEMBER_EMAIL))

    assert response.status_code == 200, response.text
    names = {a["name"] for a in response.json()["agents"]}
    assert names == {"a", "b"}


def test_admin_group_member_may_mutate_an_agent_they_do_not_own(session_factory__web_admin_groups, config__web_admin_groups):
    """Gate (authorization): Admin group member may mutate an agent they do not own."""
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver__web_admin_groups({ADMIN_MEMBER_EMAIL: [ADMIN_GROUP]})
    client = _make_client__web_admin_groups(
        session_factory__web_admin_groups, config__web_admin_groups, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk)
    )
    _seed_agent__web_admin_groups(session_factory__web_admin_groups, config__web_admin_groups, name=AGENT_NAME, created_by=OWNER_EMAIL)

    response = client.patch(
        f"/api/agents/{AGENT_NAME}/metadata",
        json={"team": "acme-team"},
        headers=_headers(private_key, email=ADMIN_MEMBER_EMAIL),
    )

    assert response.status_code == 200, response.text


# --- from test_api_group_requests.py ---


ADMIN_ONLY_403 = {"error": "Group requests are visible to room admins only."}


def test_group_requests_are_admin_gated_even_with_iap_entirely_off(tmp_path):
    # Deliberately the opposite escape hatch every other operator-only gate
    # in main.py takes ("IAP off = trust the perimeter, there's no identity
    # to check against"): a room that never turned IAP on, or never named an
    # operator, must still keep the queue closed by default -- there IS
    # something worth protecting here (another human's name and note) even
    # with no perimeter configured at all.
    """Gate (authorization): Group requests are admin gated even with iap entirely off."""
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    no_iap_session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    no_iap_client = TestClient(
        create_app(
            no_iap_session_factory,
            config=Config(),
            clients=Clients(
                github=FakeGitHub(), gojira=FakeGojira(), cloud=FakeCloud(),
                agent=FakeAgent(), kubectl=FakeKubectl(),
            ),
        )
    )

    response = no_iap_client.get("/api/group-requests")

    assert response.status_code == 403
    assert response.json() == ADMIN_ONLY_403


# --- from test_mcp_authz.py ---


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


class FakeGroupResolver__mcp_authz:
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
def session_factory__mcp_authz(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__mcp_authz(tmp_path):
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
    return FakeGroupResolver__mcp_authz(
        {
            MEMBER_A_EMAIL: [GROUP_A],
            OWNER_A_EMAIL: [GROUP_A],
            OWNER_B_EMAIL: [GROUP_B],
            ADMIN_MEMBER_EMAIL: [ADMIN_GROUP],
        }
    )


def _app(session_factory__mcp_authz, config__mcp_authz, *, groups_resolver=None):
    clients = Clients(
        github=FakeGitHub(),
        gojira=FakeGojira(),
        cloud=FakeCloud(),
        agent=FakeAgent(),
        kubectl=FakeKubectl(),
        groups=groups_resolver,
    )
    return create_app(session_factory__mcp_authz, config=config__mcp_authz, clients=clients)


def _seed_agent__mcp_authz(session_factory__mcp_authz, *, name, created_by=None, group_name=None):
    session = session_factory__mcp_authz()
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
def seeded(session_factory__mcp_authz):
    _seed_agent__mcp_authz(session_factory__mcp_authz, name="agent-a", created_by=OWNER_A_EMAIL, group_name=GROUP_A)
    _seed_agent__mcp_authz(session_factory__mcp_authz, name="agent-b", created_by=OWNER_B_EMAIL, group_name=GROUP_B)
    _seed_agent__mcp_authz(session_factory__mcp_authz, name="agent-shared", group_name=SHARED_GROUP)
    return session_factory__mcp_authz


def test_member_sees_exactly_their_group_plus_shared(seeded, config__mcp_authz):
    """Gate (visibility): Member sees exactly their group plus shared."""
    app = _app(seeded, config__mcp_authz, groups_resolver=_resolver())
    session = seeded()

    names = {a.name for a in visible_agents(session, app, MEMBER_A_EMAIL)}

    assert names == {"agent-a", "agent-shared"}


def test_stranger_to_every_group_sees_only_shared(seeded, config__mcp_authz):
    """Gate (visibility): Stranger to every group sees only shared."""
    app = _app(seeded, config__mcp_authz, groups_resolver=_resolver())
    session = seeded()

    names = {a.name for a in visible_agents(session, app, STRANGER_EMAIL)}

    assert names == {"agent-shared"}


def test_operator_sees_everything(seeded, config__mcp_authz):
    """Gate (visibility): Operator sees everything."""
    app = _app(seeded, config__mcp_authz, groups_resolver=_resolver())
    session = seeded()

    names = {a.name for a in visible_agents(session, app, OPERATOR_EMAIL)}

    assert names == {"agent-a", "agent-b", "agent-shared"}


def test_admin_group_member_sees_everything(seeded, config__mcp_authz):
    """Gate (visibility): Admin group member sees everything."""
    app = _app(seeded, config__mcp_authz, groups_resolver=_resolver())
    session = seeded()

    names = {a.name for a in visible_agents(session, app, ADMIN_MEMBER_EMAIL)}

    assert names == {"agent-a", "agent-b", "agent-shared"}


def test_visible_agent_or_not_found_raises_for_the_other_groups_agent(seeded, config__mcp_authz):
    """Gate (visibility): Visible agent or not found raises for the other groups agent."""
    app = _app(seeded, config__mcp_authz, groups_resolver=_resolver())
    session = seeded()

    with pytest.raises(NotFound):
        visible_agent_or_not_found(session, app, MEMBER_A_EMAIL, "agent-b")


def test_require_owner_or_operator_raises_not_found_not_forbidden_for_a_visible_agent(
    seeded, config__mcp_authz
):
    # The normalization pin: agent-shared is visible to MEMBER_A_EMAIL (the
    # commons), but they neither own it nor hold operator standing. The
    # underlying gate (`_owner_or_operator_or_403`) answers a 403-shaped
    # JSONResponse for exactly this case -- this must surface as NotFound,
    # never that JSONResponse or any 403-flavored exception.
    """Gate (visibility): Require owner or operator raises not found not forbidden for a visible agent."""
    app = _app(seeded, config__mcp_authz, groups_resolver=_resolver())
    session = seeded()
    agent = visible_agent_or_not_found(session, app, MEMBER_A_EMAIL, "agent-shared")

    with pytest.raises(NotFound):
        require_owner_or_operator(agent, app, MEMBER_A_EMAIL)


# --- from test_mcp_tools_read.py ---


PERSONA = "helpful-generalist"


REPO_TEMPLATE = "acme/agent-{name}"


GROUP_A = "growth"


GROUP_B = "design-team"


ADMIN_GROUP = "infra-engineering"


AUDIENCE__mcp_tools_read = "test-iap-audience"


CLIENT_ID = "test-mcp-client"


WORKSPACE_DOMAIN = "example.com"


ISSUER_BASE_URL = "https://kobo.example.org"


OWNER_A_EMAIL = "owner-a@example.com"


OWNER_B_EMAIL = "owner-b@example.com"


MEMBER_A_EMAIL = "member-a@example.com"


OPERATOR_EMAIL = "ops@example.com"


ADMIN_MEMBER_EMAIL = "admin-member@example.com"


class FakeGroupResolver__mcp_tools_read:
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
    return FakeGroupResolver__mcp_tools_read(
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
def session_factory__mcp_tools_read(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__mcp_tools_read(tmp_path):
    return Config(
        workdir=tmp_path / "work",
        operators=[OPERATOR_EMAIL],
        admin_groups=[ADMIN_GROUP],
        groups=[
            GroupChoice(name=GROUP_A, okta_group="okta-growth"),
            GroupChoice(name=GROUP_B, okta_group="okta-design-team"),
            GroupChoice(name=ADMIN_GROUP, okta_group="okta-infra-engineering"),
        ],
        iap_audience=AUDIENCE__mcp_tools_read,
        mcp=MCPConfig(
            allow_without_iap=True, oauth_client_id=CLIENT_ID,
            workspace_domain=WORKSPACE_DOMAIN,
            issuer_base_url=ISSUER_BASE_URL,
        ),
    )


def _app__mcp_tools_read(session_factory__mcp_tools_read, config__mcp_tools_read, *, groups_resolver=None, cabal=None, storage=None, github=None):
    clients = Clients(
        github=github if github is not None else FakeGitHub(),
        gojira=FakeGojira(),
        cloud=FakeCloud(),
        agent=FakeAgent(),
        kubectl=FakeKubectl(),
        groups=groups_resolver,
        cabal=cabal,
        storage=storage,
    )
    return create_app(session_factory__mcp_tools_read, config=config__mcp_tools_read, clients=clients)


def _seed_agent__mcp_tools_read(
    session_factory__mcp_tools_read,
    *,
    name,
    created_by=None,
    group_name=None,
    team=None,
    purpose_tags=None,
    status=AgentStatus.READY,
    kind=KIND_COMPOSED,
):
    session = session_factory__mcp_tools_read()
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
            kind=kind,
        )
    )
    session.commit()
    session.close()


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


def _seed_learning(session_factory__mcp_tools_read, agent_name, **overrides):
    session = session_factory__mcp_tools_read()
    try:
        defaults = {
            "agent_name": agent_name,
            "day": "2026-09-01",
            "content": "the canary gate is manual",
            "kind": "fact",
            "source": "Slack, Sep 1",
            "evidence": "quote",
            "content_hash": "a" * 64,
        }
        defaults.update(overrides)
        row = AgentLearning(**defaults)
        session.add(row)
        session.commit()
        return row.id
    finally:
        session.close()


def _enabled_config(config__mcp_tools_read):
    return config__mcp_tools_read.model_copy(
        update={
            "cabal_url": "http://cabal.internal",
            "agent_endpoint_template": "http://agent-{name}.default.svc.cluster.local",
        }
    )


def test_list_agents_fails_closed_when_the_group_resolver_is_down(session_factory__mcp_tools_read, config__mcp_tools_read):
    # T5 coverage, folded in here: a member sees only what they own plus
    # the shared commons when the directory is down -- group-DERIVED
    # visibility must vanish, never widen, on a resolver outage. Exercised
    # THROUGH kobo_list_agents (not the authz shim directly), per this
    # task's brief.
    """Gate (visibility): List agents fails closed when the group resolver is down."""
    _seed_agent__mcp_tools_read(session_factory__mcp_tools_read, name="agent-owned", created_by=MEMBER_A_EMAIL)
    _seed_agent__mcp_tools_read(session_factory__mcp_tools_read, name="agent-shared", group_name=SHARED_GROUP)
    # Visible to MEMBER_A_EMAIL only via group membership (GROUP_A) when
    # the resolver actually answers -- must NOT show up while it's down.
    _seed_agent__mcp_tools_read(session_factory__mcp_tools_read, name="agent-group-gated", created_by=OWNER_A_EMAIL, group_name=GROUP_A)
    app = _app__mcp_tools_read(session_factory__mcp_tools_read, config__mcp_tools_read, groups_resolver=FakeGroupResolver__mcp_tools_read(down=True))
    mcp = build_mcp_server(app)

    names = {row["name"] for row in _call_tool(mcp, "kobo_list_agents", _ctx_for(MEMBER_A_EMAIL))}

    assert names == {"agent-owned", "agent-shared"}
    assert "agent-group-gated" not in names


def test_get_agent_raises_the_identical_error_for_an_invisible_agent_no_existence_leak(
    session_factory__mcp_tools_read, config__mcp_tools_read
):
    """Gate (visibility): Get agent raises the identical error for an invisible agent no existence leak."""
    _seed_agent__mcp_tools_read(session_factory__mcp_tools_read, name="agent-b", created_by=OWNER_B_EMAIL, group_name=GROUP_B)
    app = _app__mcp_tools_read(session_factory__mcp_tools_read, config__mcp_tools_read, groups_resolver=_resolver())
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


def test_feed_visibility_gate_fires_before_the_switchboard_is_ever_asked(
    session_factory__mcp_tools_read, config__mcp_tools_read
):
    """Gate (visibility): Feed visibility gate fires before the switchboard is ever asked."""
    _seed_agent__mcp_tools_read(session_factory__mcp_tools_read, name="agent-b", created_by=OWNER_B_EMAIL, group_name=GROUP_B)
    fake_cabal = FakeCabal()
    cfg = _enabled_config(config__mcp_tools_read)
    app = _app__mcp_tools_read(session_factory__mcp_tools_read, cfg, groups_resolver=_resolver(), cabal=fake_cabal)
    mcp = build_mcp_server(app)

    with pytest.raises(ToolError, match="agent not found"):
        _call_tool(mcp, "kobo_feed", _ctx_for(MEMBER_A_EMAIL), agent="agent-b")

    assert fake_cabal.list_conversations_calls == []


def test_list_learnings_can_decide_false_for_non_owner_no_grant(session_factory__mcp_tools_read, config__mcp_tools_read):
    """Gate (visibility): List learnings can decide false for non owner no grant.

    A visible caller who is neither owner nor operator and holds no
    per-user grant still gets the learnings list back (visibility passed)
    but `can_decide` reports False -- mirrors `test_get_agent_carries_the_
    detail_routes_full_field_set`'s own can_decide_learnings distinction."""
    _seed_agent__mcp_tools_read(session_factory__mcp_tools_read, name="agent-a", created_by=OWNER_A_EMAIL, group_name=GROUP_A)
    _seed_learning(session_factory__mcp_tools_read, "agent-a")
    app = _app__mcp_tools_read(session_factory__mcp_tools_read, config__mcp_tools_read, groups_resolver=_resolver())
    mcp = build_mcp_server(app)

    result = _call_tool(mcp, "kobo_list_learnings", _ctx_for(MEMBER_A_EMAIL), agent="agent-a")

    assert len(result["learnings"]) == 1
    assert result["can_decide"] is False


# --- from test_mcp_tools_write.py ---


PERSONA = "helpful-generalist"


REPO_TEMPLATE = "acme/agent-{name}"


GROUP_A = "growth"


GROUP_B = "design-team"


AUDIENCE__mcp_tools_write = "test-iap-audience"


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
def _reset_rate_limit_and_audit__mcp_tools_write():
    rate_limit.reset()
    audit.configure(None)
    yield
    rate_limit.reset()
    audit.configure(None)


@pytest.fixture
def session_factory__mcp_tools_write(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__mcp_tools_write(tmp_path):
    return Config(
        workdir=tmp_path / "work",
        operators=[OPERATOR_EMAIL],
        groups=[
            GroupChoice(name=GROUP_A, okta_group="okta-growth"),
            GroupChoice(name=GROUP_B, okta_group="okta-design-team"),
        ],
        iap_audience=AUDIENCE__mcp_tools_write,
        skills_bucket=SKILLS_BUCKET,
        personas_bucket=PERSONAS_BUCKET,
        mcp=MCPConfig(
            allow_without_iap=True, oauth_client_id=CLIENT_ID,
            workspace_domain=WORKSPACE_DOMAIN,
            issuer_base_url=ISSUER_BASE_URL,
        ),
    )


@pytest.fixture
def fake_storage__mcp_tools_write():
    """Seeded the same way conftest.py's own `fake_storage` is -- kept as a
    local copy (not imported) so this file's `config` fixture, which needs
    the MCP block conftest's own `config` doesn't carry, isn't forced to
    also match conftest's bucket-name constants by coincidence."""
    storage = FakeStorage()
    seed_default_personas(storage, bucket=PERSONAS_BUCKET)
    return storage


class FakeGroupResolver__mcp_tools_write:
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


def _resolver__mcp_tools_write():
    return FakeGroupResolver__mcp_tools_write(
        {
            MEMBER_A_EMAIL: [GROUP_A],
            OWNER_A_EMAIL: [GROUP_A],
            OWNER_B_EMAIL: [GROUP_B],
        }
    )


def _app__mcp_tools_write(
    session_factory__mcp_tools_write,
    config__mcp_tools_write,
    *,
    groups_resolver=None,
    storage=None,
    github=None,
    gojira=None,
    agent=None,
    slack=None,
    cabal=None,
    name=None,
    sero=None,
    kubectl=None,
):
    clients = Clients(
        github=github if github is not None else FakeGitHub(),
        gojira=gojira if gojira is not None else FakeGojira(),
        cloud=FakeCloud(),
        agent=agent if agent is not None else FakeAgent(),
        kubectl=kubectl if kubectl is not None else FakeKubectl(),
        groups=groups_resolver,
        storage=storage,
        slack=slack,
        cabal=cabal,
        name=name,
        sero=sero,
    )
    return create_app(session_factory__mcp_tools_write, config=config__mcp_tools_write, clients=clients)


def _seed_agent__mcp_tools_write(
    session_factory__mcp_tools_write,
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
    kind=KIND_COMPOSED,
):
    session = session_factory__mcp_tools_write()
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
            kind=kind,
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


# Satisfies kobo_api.tooling's own validator: a top-level `post_draft` hook,
# no import beyond the standard library.
TOOL_PY = (
    '"""Sample marketplace tool."""\n'
    "import re\n"
    "def post_draft(draft):\n"
    "    return re.sub(r'secret-[0-9]+', '[redacted]', draft)\n"
)


NEW_SKILL_ID = "onboarding-faq"


NEW_SKILL_CONTENT = (
    "---\ntags: [onboarding]\n---\n"
    "# Onboarding FAQ\n\nAnswers the most common onboarding questions.\n"
)


SECRETS_AGENT = "clio"


SECRETS_REPO = REPO_TEMPLATE.format(name=SECRETS_AGENT)


SECRETS_GOJIRA_YAML = "name: clio\nservice_account: agent-clio\n"


def _secrets_app(
    session_factory__mcp_tools_write, config__mcp_tools_write, fake_storage__mcp_tools_write, *, sero=None, github=None, kubectl=None
):
    return _app__mcp_tools_write(
        session_factory__mcp_tools_write,
        config__mcp_tools_write,
        groups_resolver=_resolver__mcp_tools_write(),
        storage=fake_storage__mcp_tools_write,
        sero=sero,
        kubectl=kubectl,
        github=github
        or FakeGitHub(
            existing_repos={SECRETS_REPO},
            repo_files={"gojira.yaml": SECRETS_GOJIRA_YAML},
            extra_clone_files={"gojira.yaml": SECRETS_GOJIRA_YAML},
        ),
    )


def _seed_secrets_agent(session_factory__mcp_tools_write, *, created_by=MEMBER_A_EMAIL):
    _seed_agent__mcp_tools_write(
        session_factory__mcp_tools_write,
        name=SECRETS_AGENT,
        created_by=created_by,
        group_name=GROUP_A,
        repo_full_name=SECRETS_REPO,
    )


def test_wire_slack_non_owner_of_a_visible_agent_raises_agent_not_found(
    session_factory__mcp_tools_write, config__mcp_tools_write, fake_storage__mcp_tools_write
):
    """Gate (authorization): Wire slack non owner of a visible agent raises agent not found.

    The controller's own owner-or-operator confirmation test for
    wire_slack: a non-owner of a VISIBLE, READY agent must get "agent not
    found" -- never a distinguishable forbidden."""
    fake_gojira, app_id = _onboarded_gojira("ward", REPO_TEMPLATE.format(name="ward"))
    _seed_agent__mcp_tools_write(
        session_factory__mcp_tools_write,
        name="ward",
        created_by=OWNER_A_EMAIL,
        group_name=GROUP_A,
        gojira_app_id=app_id,
        slack_app_id=APP_ID,
    )
    app = _app__mcp_tools_write(
        session_factory__mcp_tools_write,
        config__mcp_tools_write,
        groups_resolver=_resolver__mcp_tools_write(),
        storage=fake_storage__mcp_tools_write,
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


def test_create_skill_non_member_refused(session_factory__mcp_tools_write, config__mcp_tools_write, fake_storage__mcp_tools_write):
    """Gate (authorization): Create skill non member refused.

    `STRANGER_EMAIL` resolves to no groups at all (`_resolver()`'s own
    default `[]` for an email absent from its mapping) -- refused before
    any clone, naming the claimed team."""
    config__mcp_tools_write.skills_repo = "org-skills"
    fake_github = FakeGitHub()
    app = _app__mcp_tools_write(
        session_factory__mcp_tools_write,
        config__mcp_tools_write,
        groups_resolver=_resolver__mcp_tools_write(),
        storage=fake_storage__mcp_tools_write,
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


def test_create_skill_resolver_down_fail_closed(session_factory__mcp_tools_write, config__mcp_tools_write, fake_storage__mcp_tools_write):
    """Gate (authorization): Create skill resolver down fail closed.

    A resolver that cannot answer right now (`groups_for` -> `None`)
    refuses even a caller who might really be a member -- fail closed,
    the same posture `kobo_api.main`'s own group-consent gate holds for
    `kobo_create_agent`. Not an operator, so the membership gate is the
    only thing standing between this caller and a commit."""
    config__mcp_tools_write.skills_repo = "org-skills"
    fake_github = FakeGitHub()
    app = _app__mcp_tools_write(
        session_factory__mcp_tools_write,
        config__mcp_tools_write,
        groups_resolver=FakeGroupResolver__mcp_tools_write(down=True),
        storage=fake_storage__mcp_tools_write,
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


def test_create_tool_baseline_prefix_refused_for_non_operator(
    session_factory__mcp_tools_write, config__mcp_tools_write, fake_storage__mcp_tools_write
):
    """Gate (authorization): Create tool baseline prefix refused for non operator.

    The baseline pack's reservation (Task 6 review, operator ruling): a
    `baseline-` id is what every NEW agent then ships automatically
    (`kobo_api.steps._stage_baseline_tools`), so publishing one is a
    room-wide act, not a team one. MEMBER_A_EMAIL is a real member of
    GROUP_A here -- the membership gate passes and this refusal can only
    come from the prefix gate."""
    config__mcp_tools_write.skills_repo = "org-skills"
    fake_github = FakeGitHub()
    app = _app__mcp_tools_write(
        session_factory__mcp_tools_write,
        config__mcp_tools_write,
        groups_resolver=_resolver__mcp_tools_write(),
        storage=fake_storage__mcp_tools_write,
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


def test_move_agent_is_an_operators_call(session_factory__mcp_tools_write, config__mcp_tools_write, fake_storage__mcp_tools_write):
    """Gate (authorization): Move agent is an operators call."""
    _seed_secrets_agent(session_factory__mcp_tools_write)  # owned by member A, who is not an operator
    mcp = build_mcp_server(_secrets_app(session_factory__mcp_tools_write, config__mcp_tools_write, fake_storage__mcp_tools_write))

    with pytest.raises(ToolError, match="operator"):
        _call_tool(
            mcp, "kobo_move_agent", _ctx_for(MEMBER_A_EMAIL), agent=SECRETS_AGENT, group=GROUP_B
        )


# --- from test_api_agents.py ---


AGENT_NAME = "clio"


PERSONA = "helpful-generalist"


TEAM = "clio-team"


# Real-IAP-JWT idiom (created_by attribution tests, Task A2 below): same
# audience/kid constants as test_web_teach.py's own copy.
AUDIENCE__api_agents = "test-audience"


KID = "test-key-1"


POLL_DEADLINE_S = 5


POLL_INTERVAL_S = 0.02


@pytest.fixture(autouse=True)
def _no_real_sleep__api_agents(monkeypatch):
    """The assembled pipeline's steps poll gojira/CI; never let those loops
    sleep for real (see module docstring re: the deadline-poll's own sleep)."""
    import kobo_api.steps as steps_module

    monkeypatch.setattr(steps_module, "_sleep", lambda seconds: None)


@pytest.fixture
def session_factory__api_agents(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__api_agents(tmp_path):
    return Config(
        workdir=tmp_path / "work",
        skills_bucket="skills-bucket",
        personas_bucket="personas-bucket",
    )


@pytest.fixture
def fake_github():
    return FakeGitHub()


@pytest.fixture
def fake_gojira():
    return FakeGojira(activate_after=1)


@pytest.fixture
def fake_agent():
    return FakeAgent(reply="hello yourself, human")


@pytest.fixture
def clients(fake_github, fake_gojira, fake_agent, fake_storage):
    return Clients(
        github=fake_github,
        gojira=fake_gojira,
        cloud=FakeCloud(),
        agent=fake_agent,
        kubectl=FakeKubectl(),
        storage=fake_storage,
    )


@pytest.fixture
def client__api_agents(session_factory__api_agents, config__api_agents, clients):
    return TestClient(create_app(session_factory__api_agents, config=config__api_agents, clients=clients))


def _poll_until(client__api_agents, name, statuses, deadline_s=POLL_DEADLINE_S):
    """Poll GET /api/agents/{name} until its status lands in `statuses`;
    fail with a clear message if `deadline_s` real (wall-clock) seconds pass
    before that happens — the pipeline itself never really sleeps (see
    `_no_real_sleep` above), so on a healthy run this returns almost
    immediately.
    """
    deadline = time.monotonic() + deadline_s
    body = None
    while time.monotonic() < deadline:
        response = client__api_agents.get(f"/api/agents/{name}")
        assert response.status_code == 200
        body = response.json()
        if body["status"] in statuses:
            return body
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"agent {name!r} did not reach {statuses} within {deadline_s}s: {body}")


def _generate_jwk(kid: str = KID):
    private_key = ec.generate_private_key(ec.SECP256R1())
    jwk = jwt.algorithms.ECAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk["kid"] = kid
    jwk["use"] = "sig"
    jwk["alg"] = "ES256"
    return private_key, jwk


def _sign(private_key, *, email: str | None, kid: str = KID) -> str:
    now = int(time.time())
    claims = {
        "aud": AUDIENCE__api_agents,
        "iss": IAP_ISSUER,
        "iat": now,
        "exp": now + 3600,
        "sub": "accounts.google.com:1234567890",
    }
    if email is not None:
        claims["email"] = email
    return jwt.encode(claims, private_key, algorithm="ES256", headers={"kid": kid})


def _fetcher(jwk):
    def fetch():
        return {"keys": [jwk]}

    return fetch


def _client_with_cabal_and_storage(
    session_factory__api_agents, config__api_agents, fake_github, fake_gojira, fake_agent, fake_storage, cabal
):
    return TestClient(
        create_app(
            session_factory__api_agents,
            config=config__api_agents,
            clients=Clients(
                github=fake_github,
                gojira=fake_gojira,
                cloud=FakeCloud(),
                agent=fake_agent,
                kubectl=FakeKubectl(),
                storage=fake_storage,
                cabal=cabal,
            ),
        )
    )


def _colleagues_deliveries(fake_cabal):
    """Every relay call in `fake_cabal.relay_calls` that carries a
    `colleagues.md` knowledge system task, keyed by the target agent name
    it was relayed to."""
    return [
        call["name"]
        for call in fake_cabal.relay_calls
        if call["task_envelope"].get("metadata", {}).get("filename") == "colleagues.md"
    ]


CABAL_ENDPOINT_TEMPLATE = "http://agent-{name}.default.svc.cluster.local"


def _enabled_cabal_config(config__api_agents):
    return config__api_agents.model_copy(
        update={"cabal_url": "http://cabal.internal", "agent_endpoint_template": CABAL_ENDPOINT_TEMPLATE}
    )


def test_create_agent_records_created_by_from_iap_email(session_factory__api_agents, config__api_agents, clients):
    """Gate (ownership): Create agent records created by from iap email."""
    private_key, jwk = _generate_jwk()
    iap_config = config__api_agents.model_copy(update={"iap_audience": AUDIENCE__api_agents})
    iap_client = TestClient(
        create_app(
            session_factory__api_agents, config=iap_config, clients=clients, iap_jwks_fetcher=_fetcher(jwk)
        )
    )
    token = _sign(private_key, email="creator@example.com")

    response = iap_client.post(
        "/api/agents",
        json={"name": AGENT_NAME, "persona": PERSONA, "team": TEAM},
        headers={ASSERTION_HEADER: token},
    )

    assert response.status_code == 201

    session = session_factory__api_agents()
    row = session.query(Agent).filter_by(name=AGENT_NAME).first()
    session.close()
    assert row.created_by == "creator@example.com"


def test_create_agent_ready_flip_redelivers_roster_without_cross_group_delivery(
    session_factory__api_agents, config__api_agents, fake_github, fake_gojira, fake_agent, fake_storage, monkeypatch
):
    """Gate (ownership): Create agent ready flip redelivers roster without cross group delivery.

    Global Constraints' load-bearing negative test, exercised through the
    real pipeline end to end: a room with agents in two groups must not
    cross-deliver. A same-group READY agent and an unrelated-group READY
    agent both pre-exist; once the newly created agent reaches READY, only
    the same-group agent's colleagues.md is redelivered."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_cabal_config(config__api_agents)
    fake_cabal = FakeCabal(
        relay_result={"status": {"state": "completed", "message": {"parts": [{"text": "ok"}]}}}
    )
    client__api_agents = _client_with_cabal_and_storage(
        session_factory__api_agents, cfg, fake_github, fake_gojira, fake_agent, fake_storage, fake_cabal
    )
    session = session_factory__api_agents()
    session.add(
        Agent(
            name="same-group",
            persona=PERSONA,
            model_id="vertex_ai/claude-sonnet-4-5",
            status=AgentStatus.READY,
            group_name="acme-acl",
        )
    )
    session.add(
        Agent(
            name="other-group",
            persona=PERSONA,
            model_id="vertex_ai/claude-sonnet-4-5",
            status=AgentStatus.READY,
            group_name="orbit-acl",
        )
    )
    session.commit()
    session.close()

    response = client__api_agents.post(
        "/api/agents",
        json={"name": AGENT_NAME, "persona": PERSONA, "group_name": "acme-acl"},
    )
    assert response.status_code == 201

    body = _poll_until(client__api_agents, AGENT_NAME, {"ready", "failed"})
    assert body["status"] == "ready"

    delivered_to = _colleagues_deliveries(fake_cabal)
    assert "same-group" in delivered_to
    assert "other-group" not in delivered_to


# --- from test_api_personas.py ---


def _index_bytes(entries: list[dict]) -> bytes:
    return json.dumps({"entries": entries}).encode()


SKILL_ENTRIES = [
    {
        "id": "code-review",
        "name": "Code review",
        "description": "Reviews pull requests",
        "team": "shared",
        "tags": ["review", "quality"],
        "sha": "abc123",
        "builtin": False,
    },
]


VALID_PAYLOAD = {
    "name": "Aria the Assistant",
    "who_they_are": "a calm troubleshooter for billing questions",
    "what_they_do": "Help customers troubleshoot billing issues.",
    # "shared" (Task 2's team middle gate): every test below that doesn't
    # care about team-gating posts through the plain `client` fixture with
    # no identity at all, and must land on the gate's no-membership-required
    # arm to reach the behavior it actually means to exercise.
    # test_create_persona_explicit_team_still_honored below overrides this
    # with a real member identity to prove a NAMED team is honored too.
    "team": "shared",
}


def _minimal_clients(**kwargs) -> Clients:
    """A Clients with every non-storage field a bare fake — this route
    never touches github/gojira/cloud/agent/kubectl, but the Clients
    dataclass requires them (no defaults)."""
    return Clients(
        github=FakeGitHub(),
        gojira=FakeGojira(),
        cloud=FakeCloud(),
        agent=FakeAgent(),
        kubectl=FakeKubectl(),
        **kwargs,
    )


def _make_client__api_personas(
    config__api_personas: Config, clients__api_personas: Clients, tmp_path, *, iap_jwks_fetcher=None
) -> TestClient:
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    return TestClient(
        create_app(
            session_factory,
            config=config__api_personas,
            clients=clients__api_personas,
            iap_jwks_fetcher=iap_jwks_fetcher,
        )
    )


@pytest.fixture
def fake_storage__api_personas():
    return FakeStorage(
        buckets={
            "skills-bucket": {"index.json": (_index_bytes(SKILL_ENTRIES), {})},
            "personas-bucket": {},
        }
    )


@pytest.fixture
def config__api_personas(tmp_path):
    return Config(
        workdir=tmp_path / "work",
        skills_bucket="skills-bucket",
        personas_bucket="personas-bucket",
    )


@pytest.fixture
def clients__api_personas(fake_storage__api_personas):
    return _minimal_clients(storage=fake_storage__api_personas)


@pytest.fixture
def client__api_personas(config__api_personas, clients__api_personas, tmp_path):
    return _make_client__api_personas(config__api_personas, clients__api_personas, tmp_path)


GATE_AUDIENCE = "test-persona-gate-audience"


GATE_KID = "test-persona-gate-key"


MEMBER_EMAIL = "member@example.com"


STRANGER_EMAIL = "stranger@example.com"


OPERATOR_EMAIL = "ops@example.com"


def _generate_jwk__api_personas(kid: str = GATE_KID):
    private_key = ec.generate_private_key(ec.SECP256R1())
    jwk = jwt.algorithms.ECAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk["kid"] = kid
    jwk["use"] = "sig"
    jwk["alg"] = "ES256"
    return private_key, jwk


def _sign__api_personas(private_key, *, email: str | None, kid: str = GATE_KID) -> str:
    now = int(time.time())
    claims = {
        "aud": GATE_AUDIENCE,
        "iss": IAP_ISSUER,
        "iat": now,
        "exp": now + 3600,
        "sub": "accounts.google.com:1234567890",
    }
    if email is not None:
        claims["email"] = email
    return jwt.encode(claims, private_key, algorithm="ES256", headers={"kid": kid})


def _gate_fetcher(jwk):
    def fetch():
        return {"keys": [jwk]}

    return fetch


def _gate_headers(private_key, *, email: str | None) -> dict:
    return {ASSERTION_HEADER: _sign__api_personas(private_key, email=email)}


class _GroupsStub:
    """Mapping-driven `groups_for` stand-in for the gate's resolver seam --
    same shape as the other gated-route test files' own local copies (e.g.
    test_mcp_tools_write.py's `FakeGroupResolver`), duplicated here rather
    than imported. An email with no entry in `mapping` resolves to `None`
    (unresolvable / "directory down"), matching `_persona_team_gate`'s own
    fail-closed reading of a `None` answer.
    """

    def __init__(self, mapping: dict[str, list[str]] | None = None):
        self._mapping = dict(mapping) if mapping else {}

    def groups_for(self, email: str) -> list[str] | None:
        return self._mapping.get(email.lower())


@pytest.fixture
def gate_config(tmp_path):
    return Config(
        workdir=tmp_path / "work",
        skills_bucket="skills-bucket",
        personas_bucket="personas-bucket",
        iap_audience=GATE_AUDIENCE,
        operators=[OPERATOR_EMAIL],
    )


def _gate_client(gate_config, fake_storage__api_personas, tmp_path, *, groups_resolver=None):
    private_key, jwk = _generate_jwk__api_personas()
    clients__api_personas = _minimal_clients(storage=fake_storage__api_personas, groups=groups_resolver)
    client__api_personas = _make_client__api_personas(
        gate_config, clients__api_personas, tmp_path, iap_jwks_fetcher=_gate_fetcher(jwk)
    )
    return client__api_personas, private_key


ARCHIVE_ENTRY = {"id": "wire-probe", "name": "Wire Probe", "description": "a test persona",
                 "team": "shared", "tags": [], "builtin": False}


TEAM_ARCHIVE_ENTRY = {"id": "stoic-mentor", "name": "stoic-mentor", "description": "d",
                      "team": "sre", "tags": [], "builtin": False}


def _seed_personas_index(fake_storage__api_personas, entries: list[dict]) -> None:
    fake_storage__api_personas.write_bytes("personas-bucket", "index.json", _index_bytes(entries))


SCOPE_ENTRIES = [
    {"id": "commons-one", "name": "commons-one", "description": "d",
     "team": "shared", "tags": [], "builtin": False},
    {"id": "sre-one", "name": "sre-one", "description": "d",
     "team": "sre", "tags": [], "builtin": False},
    {"id": "qa-one", "name": "qa-one", "description": "d",
     "team": "qa", "tags": [], "builtin": False},
    # A built-in counts as shared even when a stale index files it elsewhere.
    {"id": "helpful-generalist", "name": "helpful-generalist", "description": "d",
     "team": "legacy", "tags": [], "builtin": True},
]


def _scope_client(gate_config, fake_storage__api_personas, tmp_path, mapping):
    client__api_personas, key = _gate_client(
        gate_config, fake_storage__api_personas, tmp_path, groups_resolver=_GroupsStub(mapping)
    )
    _seed_personas_index(fake_storage__api_personas, SCOPE_ENTRIES)
    return client__api_personas, key


def _search_ids(client__api_personas, key, email, **params) -> set[str]:
    response = client__api_personas.get(
        "/api/personas/search", params=params, headers=_gate_headers(key, email=email)
    )
    assert response.status_code == 200, response.text
    return {e["id"] for e in response.json()["results"]}


CREATOR_EMAIL = "maker@example.com"


MOVE_SHARED = {"id": "wire-probe", "name": "Wire Probe", "description": "a test persona",
               "team": "shared", "tags": ["probe"], "builtin": False,
               "created_by": CREATOR_EMAIL}


MOVE_TEAM = {"id": "stoic-mentor", "name": "stoic-mentor", "description": "d",
             "team": "sre", "tags": ["calm"], "builtin": False,
             "created_by": CREATOR_EMAIL}


MOVE_BUILTIN = {"id": "helpful-generalist", "name": "helpful-generalist",
                "description": "d", "team": "shared", "tags": [], "builtin": True}


def _move_client(gate_config, fake_storage__api_personas, tmp_path, mapping, entries=None):
    client__api_personas, key = _gate_client(
        gate_config, fake_storage__api_personas, tmp_path, groups_resolver=_GroupsStub(mapping)
    )
    _seed_personas_index(
        fake_storage__api_personas, entries if entries is not None else [MOVE_SHARED, MOVE_TEAM, MOVE_BUILTIN]
    )
    return client__api_personas, key


def _move(client__api_personas, key, email, persona_id, team):
    return client__api_personas.patch(
        f"/api/personas/{persona_id}",
        json={"team": team},
        headers=_gate_headers(key, email=email),
    )


def _index_entry(fake_storage__api_personas, persona_id):
    index = json.loads(fake_storage__api_personas.read_bytes("personas-bucket", "index.json"))
    return next(e for e in index["entries"] if e["id"] == persona_id)


def test_persona_create_shared_team_needs_no_membership(gate_config, fake_storage__api_personas, tmp_path):
    """Gate (authorization): Persona create shared team needs no membership.

    team omitted entirely -> 201 even with a resolver that would refuse
    every team (resolver returning [] or None must not matter on this arm
    -- the gate never consults it for the default "shared" team)."""
    client__api_personas, key = _gate_client(gate_config, fake_storage__api_personas, tmp_path, groups_resolver=_GroupsStub())
    payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "team"}

    response = client__api_personas.post(
        "/api/personas", json=payload, headers=_gate_headers(key, email=STRANGER_EMAIL)
    )

    assert response.status_code == 201


def test_persona_create_named_team_non_member_403(gate_config, fake_storage__api_personas, tmp_path):
    """Gate (authorization): Persona create named team non member 403."""
    resolver = _GroupsStub({MEMBER_EMAIL: ["other-team"]})
    client__api_personas, key = _gate_client(gate_config, fake_storage__api_personas, tmp_path, groups_resolver=resolver)
    payload = {**VALID_PAYLOAD, "team": "sre"}

    response = client__api_personas.post(
        "/api/personas", json=payload, headers=_gate_headers(key, email=MEMBER_EMAIL)
    )

    assert response.status_code == 403
    assert response.json() == {
        "error": "you must be a member of team sre to file a persona under it"
    }


def test_persona_create_named_team_unresolvable_403(gate_config, fake_storage__api_personas, tmp_path):
    """Gate (authorization): Persona create named team unresolvable 403."""
    client__api_personas, key = _gate_client(gate_config, fake_storage__api_personas, tmp_path, groups_resolver=_GroupsStub())
    payload = {**VALID_PAYLOAD, "team": "sre"}

    response = client__api_personas.post(
        "/api/personas", json=payload, headers=_gate_headers(key, email=STRANGER_EMAIL)
    )

    assert response.status_code == 403


def test_persona_create_named_team_operator_passes(gate_config, fake_storage__api_personas, tmp_path):
    # OPERATOR_EMAIL resolves to no groups at all -- passes purely on
    # `_is_admin`'s operator-email check, never on team membership.
    """Gate (authorization): Persona create named team operator passes."""
    client__api_personas, key = _gate_client(gate_config, fake_storage__api_personas, tmp_path, groups_resolver=_GroupsStub())
    payload = {**VALID_PAYLOAD, "team": "sre"}

    response = client__api_personas.post(
        "/api/personas", json=payload, headers=_gate_headers(key, email=OPERATOR_EMAIL)
    )

    assert response.status_code == 201


def test_archive_team_persona_member_200_non_member_403(gate_config, fake_storage__api_personas, tmp_path):
    """Gate (authorization): Archive team persona member 200 non member 403."""
    resolver = _GroupsStub({MEMBER_EMAIL: ["sre"]})
    client__api_personas, key = _gate_client(gate_config, fake_storage__api_personas, tmp_path, groups_resolver=resolver)
    _seed_personas_index(fake_storage__api_personas, [TEAM_ARCHIVE_ENTRY])
    denied = client__api_personas.post(
        "/api/personas/stoic-mentor/archive", headers=_gate_headers(key, email=STRANGER_EMAIL)
    )
    assert denied.status_code == 403
    # Important 1 fix: same full-dict pin as above, on the named-team arm's
    # own distinct copy ("...filed under it", not the CREATE gate's "...to
    # file a persona under it").
    assert denied.json() == {
        "error": "you must be a member of team sre to archive a persona filed under it"
    }
    allowed = client__api_personas.post(
        "/api/personas/stoic-mentor/archive", headers=_gate_headers(key, email=MEMBER_EMAIL)
    )
    assert allowed.status_code == 200


def test_archive_builtin_operator_only(gate_config, fake_storage__api_personas, tmp_path):
    # Minor 6 fix (hardening wave review): the fixture's team must be
    # something OTHER than "shared" here, or the gate's other arm
    # (`entry.get("team") == "shared"`) alone would still route this to the
    # operator-only branch even with the `builtin` check removed entirely,
    # masking a mutation on that arm. A resolver that makes MEMBER_EMAIL a
    # genuine member of the entry's own named team is what isolates it: if
    # the `builtin` check ever silently dropped out, this non-operator team
    # member would wrongly pass through the team-gate's membership arm
    # instead of being refused by the operator-only branch.
    """Gate (authorization): Archive builtin operator only."""
    resolver = _GroupsStub({MEMBER_EMAIL: ["sre"]})
    builtin = {**ARCHIVE_ENTRY, "id": "helpful-generalist", "builtin": True, "team": "sre"}
    client__api_personas, key = _gate_client(gate_config, fake_storage__api_personas, tmp_path, groups_resolver=resolver)
    _seed_personas_index(fake_storage__api_personas, [builtin])
    denied = client__api_personas.post(
        "/api/personas/helpful-generalist/archive",
        headers=_gate_headers(key, email=MEMBER_EMAIL),
    )
    assert denied.status_code == 403
    assert denied.json() == {
        "error": "only a room operator can archive a persona filed under the commons"
    }
    allowed = client__api_personas.post(
        "/api/personas/helpful-generalist/archive",
        headers=_gate_headers(key, email=OPERATOR_EMAIL),
    )
    assert allowed.status_code == 200


def test_search_scope_mine_is_the_default_and_keeps_to_the_callers_groups(
    gate_config, fake_storage__api_personas, tmp_path
):
    """Gate (authorization): Search scope mine is the default and keeps to the callers groups."""
    client__api_personas, key = _scope_client(gate_config, fake_storage__api_personas, tmp_path, {MEMBER_EMAIL: ["sre"]})

    default = _search_ids(client__api_personas, key, MEMBER_EMAIL)
    mine = _search_ids(client__api_personas, key, MEMBER_EMAIL, scope="mine")

    assert default == mine == {"commons-one", "sre-one", "helpful-generalist"}


def test_search_scope_mine_with_the_directory_down_is_shared_only(
    gate_config, fake_storage__api_personas, tmp_path
):
    # No entry for the caller: the resolver answers None (couldn't tell).
    """Gate (authorization): Search scope mine with the directory down is shared only."""
    client__api_personas, key = _scope_client(gate_config, fake_storage__api_personas, tmp_path, {})

    assert _search_ids(client__api_personas, key, STRANGER_EMAIL) == {"commons-one", "helpful-generalist"}


def test_create_persona_records_who_made_it(gate_config, fake_storage__api_personas, tmp_path):
    """Gate (authorization): Create persona records who made it."""
    resolver = _GroupsStub({MEMBER_EMAIL: ["sre"]})
    client__api_personas, key = _gate_client(gate_config, fake_storage__api_personas, tmp_path, groups_resolver=resolver)

    response = client__api_personas.post(
        "/api/personas",
        json={**VALID_PAYLOAD, "team": "sre"},
        headers=_gate_headers(key, email=MEMBER_EMAIL.upper()),
    )

    assert response.status_code == 201
    index = json.loads(fake_storage__api_personas.read_bytes("personas-bucket", "index.json"))
    (entry,) = index["entries"]
    assert entry["created_by"] == MEMBER_EMAIL


def test_move_shared_persona_by_someone_else_403(gate_config, fake_storage__api_personas, tmp_path):
    """Gate (authorization): Move shared persona by someone else 403."""
    client__api_personas, key = _move_client(gate_config, fake_storage__api_personas, tmp_path, {MEMBER_EMAIL: ["qa"]})

    response = _move(client__api_personas, key, MEMBER_EMAIL, "wire-probe", "qa")

    assert response.status_code == 403
    assert response.json() == {
        "error": "only the person who made a shared persona, or a room operator, can move it"
    }
    assert _index_entry(fake_storage__api_personas, "wire-probe")["team"] == "shared"


def test_move_into_a_team_the_caller_is_not_in_403(gate_config, fake_storage__api_personas, tmp_path):
    # Allowed to move it (they made it), not allowed to file it there.
    """Gate (authorization): Move into a team the caller is not in 403."""
    client__api_personas, key = _move_client(gate_config, fake_storage__api_personas, tmp_path, {CREATOR_EMAIL: ["qa"]})

    response = _move(client__api_personas, key, CREATOR_EMAIL, "wire-probe", "sre")

    assert response.status_code == 403
    assert response.json() == {
        "error": "you must be a member of team sre to file a persona under it"
    }
    assert _index_entry(fake_storage__api_personas, "wire-probe")["team"] == "shared"


def test_move_builtin_persona_is_refused_even_for_an_operator(gate_config, fake_storage__api_personas, tmp_path):
    # The boot-time seed files every built-in back under the commons, so a
    # move would quietly undo itself on the next restart.
    """Gate (authorization): Move builtin persona is refused even for an operator."""
    client__api_personas, key = _move_client(gate_config, fake_storage__api_personas, tmp_path, {OPERATOR_EMAIL: []})

    response = _move(client__api_personas, key, OPERATOR_EMAIL, "helpful-generalist", "sre")

    assert response.status_code == 403
    assert response.json() == {"error": "built-in personas stay shared with everyone"}


# --- from test_api_connect.py ---


AGENT_NAME = "clio"


ORG = "example-org"


REPO = f"{ORG}/team-chatbot"


@pytest.fixture(autouse=True)
def _no_real_sleep__api_connect(monkeypatch):
    import kobo_api.steps as steps_module

    monkeypatch.setattr(steps_module, "_sleep", lambda seconds: None)


@pytest.fixture(autouse=True)
def _fresh_scan_window():
    """The scan limiter's window is module state shared by every caller in
    the process, so each case starts with an empty one rather than one
    carrying the previous case's scans."""
    rate_limit.reset()
    yield
    rate_limit.reset()


@pytest.fixture
def session_factory__api_connect(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__api_connect(tmp_path):
    return Config(
        workdir=tmp_path / "work",
        skills_bucket="skills-bucket",
        personas_bucket="personas-bucket",
        github_org=ORG,
    )


@pytest.fixture
def github():
    return FakeGitHub(existing_repos={REPO})


@pytest.fixture
def clients__api_connect(github, fake_storage):
    return Clients(
        github=github,
        gojira=FakeGojira(activate_after=1),
        cloud=FakeCloud(),
        agent=FakeAgent(reply="hello yourself, human"),
        kubectl=FakeKubectl(),
        storage=fake_storage,
        image=FakeImage(),
    )


@pytest.fixture
def client__api_connect(session_factory__api_connect, config__api_connect, clients__api_connect):
    return TestClient(create_app(session_factory__api_connect, config=config__api_connect, clients=clients__api_connect))


def _payload(**overrides) -> dict:
    payload = {
        "repo": REPO,
        "name": AGENT_NAME,
        "run_mode": RUN_MODE_HTTP,
        # The room cannot check who owns a repository, so every connect
        # carries the caller's word that they may. Tests for its absence
        # are in their own section below.
        "confirm_repo_owner": True,
    }
    payload.update(overrides)
    return payload


def _row(session_factory__api_connect, name=AGENT_NAME) -> Agent | None:
    session = session_factory__api_connect()
    try:
        return session.query(Agent).filter_by(name=name).first()
    finally:
        session.close()


CONVERT_PATH = f"/api/agents/{AGENT_NAME}/convert-to-connected"


def _seed_composed(
    session_factory__api_connect,
    *,
    status=AgentStatus.READY,
    kind=KIND_COMPOSED,
    name=AGENT_NAME,
    secret_names=None,
    group_name=None,
):
    """One agent row as the create route leaves it: the room's own repository,
    a real persona, and whatever status this case is about."""
    session = session_factory__api_connect()
    session.add(
        Agent(
            name=name,
            persona="helpful-generalist",
            model_id="m",
            skills=[],
            kind=kind,
            repo_full_name=f"{ORG}/agent-{name}",
            status=status,
            group_name=group_name,
            secret_names=secret_names if secret_names is not None else [],
            run_mode=RUN_MODE_HTTP if kind == KIND_CONNECTED else None,
        )
    )
    session.commit()
    session.close()


@pytest.fixture
def switchboard(session_factory__api_connect, config__api_connect, fake_storage, monkeypatch):
    """A room with the switchboard wired, the fakes the convert route must
    not touch, and a client over both.

    The switchboard has to be genuinely on for the retire to be attempted at
    all (`cabal_enabled` reads two config keys and the operator token), and
    the point of most of these cases is what the route does NOT do, which
    only means something when the clients that could have done it are there.
    """
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = config__api_connect.model_copy(
        update={
            "cabal_url": "http://cabal.internal",
            "agent_endpoint_template": "http://agent-{name}.internal",
        }
    )
    fakes = SimpleNamespace(
        github=FakeGitHub(existing_repos={REPO}),
        gojira=FakeGojira(),
        kubectl=FakeKubectl(),
        cabal=FakeCabal(),
        config=cfg,
    )
    clients__api_connect = Clients(
        github=fakes.github,
        gojira=fakes.gojira,
        cloud=FakeCloud(),
        agent=FakeAgent(),
        kubectl=fakes.kubectl,
        storage=fake_storage,
        image=FakeImage(),
        cabal=fakes.cabal,
    )
    fakes.client = TestClient(create_app(session_factory__api_connect, config=cfg, clients=clients__api_connect))
    return fakes


GROUP = "payments-eng"


PEER = "ward"


def _seed_peer(session_factory__api_connect, *, name=PEER, group_name=GROUP):
    session = session_factory__api_connect()
    session.add(
        Agent(
            name=name,
            persona="helpful-generalist",
            model_id="m",
            skills=[],
            repo_full_name=f"{ORG}/agent-{name}",
            status=AgentStatus.READY,
            group_name=group_name,
        )
    )
    session.commit()
    session.close()


def test_connect_refuses_without_the_callers_word_that_they_may(
    client__api_connect, session_factory__api_connect
):
    """Gate (authorization): Connect refuses without the callers word that they may."""
    response = client__api_connect.post(
        "/api/agents/connect", json=_payload(confirm_repo_owner=False)
    )

    assert response.status_code == 422
    assert response.json() == {
        "error": (
            "confirm that you own this repository or have its owner's agreement"
        )
    }
    assert _row(session_factory__api_connect) is None


def test_an_operator_does_not_have_to_answer_for_the_repository(
    client__api_connect, session_factory__api_connect, monkeypatch
):
    """Gate (authorization): An operator does not have to answer for the repository."""
    import kobo_api.main as main_module

    monkeypatch.setattr(main_module, "_is_admin", lambda request: True)

    response = client__api_connect.post(
        "/api/agents/connect", json=_payload(confirm_repo_owner=False)
    )

    assert response.status_code == 201, response.text
    assert _row(session_factory__api_connect) is not None


def test_connect_refuses_a_repository_in_another_organisation(client__api_connect, session_factory__api_connect):
    """Gate (authorization): Connect refuses a repository in another organisation."""
    response = client__api_connect.post(
        "/api/agents/connect", json=_payload(repo="elsewhere/team-chatbot")
    )

    assert response.status_code == 422
    assert [b["code"] for b in response.json()["blockers"]] == [REPO_NOT_IN_ORG]
    assert _row(session_factory__api_connect) is None


def test_convert_takes_the_agent_off_the_dispatch_pairs_in_both_directions(
    switchboard, session_factory__api_connect
):
    """Gate (authorization): Convert takes the agent off the dispatch pairs in both directions."""
    _seed_composed(session_factory__api_connect, group_name=GROUP)
    _seed_peer(session_factory__api_connect)

    switchboard.client.post(CONVERT_PATH, json={"run_mode": RUN_MODE_HTTP})

    assert switchboard.cabal.put_dispatch_pairs_calls, "no pair set was pushed"
    pushed = switchboard.cabal.put_dispatch_pairs_calls[-1]
    assert (f"agent:{AGENT_NAME}", PEER) not in pushed
    assert (f"agent:{PEER}", AGENT_NAME) not in pushed


# --- from test_connect_scan.py ---


ORG = "example-org"


REPO = f"{ORG}/team-chatbot"


NAME = "clio"


@pytest.fixture
def config__connect_scan(tmp_path):
    return Config(workdir=tmp_path / "work", github_org=ORG)


def _scan(session_factory, config__connect_scan, github, *, repo=REPO, name=NAME, run_mode=RUN_MODE_HTTP):
    session = session_factory()
    try:
        return scan_repo(
            github,
            FakeGojira(),
            session,
            config__connect_scan,
            repo=repo,
            name=name,
            run_mode=run_mode,
        )
    finally:
        session.close()


def _codes(findings):
    return [finding.code for finding in findings]


def test_a_repository_outside_the_org_is_refused_without_a_single_read(
    session_factory, config__connect_scan
):
    """Gate (authorization): A repository outside the org is refused without a single read.

    The room holds one GitHub App installation and cannot reach outside
    it, so there is nothing to ask GitHub about."""
    github = FakeGitHub()

    report = _scan(session_factory, config__connect_scan, github, repo="elsewhere/team-chatbot")

    assert _codes(report.blockers) == [REPO_NOT_IN_ORG]
    assert github.calls == []


# --- from test_web_learnings.py ---


AGENT_NAME = "clio"


PERSONA = "helpful-generalist"


REPO__web_learnings = "acme/agent-clio"


AUDIENCE = "test-audience"


KID = "test-key-1"


OWNER_EMAIL = "owner@example.com"


CALLER_EMAIL = "caller@example.com"


@pytest.fixture
def session_factory__web_learnings(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__web_learnings(tmp_path):
    return Config(workdir=tmp_path / "work")


def _make_client__web_learnings(session_factory__web_learnings, config__web_learnings, *, github=None, iap_jwks_fetcher=None, cabal=None):
    return TestClient(
        create_app(
            session_factory__web_learnings,
            config=config__web_learnings,
            clients=Clients(
                github=github if github is not None else FakeGitHub(),
                gojira=FakeGojira(),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=FakeKubectl(),
                storage=FakeStorage(),
                cabal=cabal,
            ),
            iap_jwks_fetcher=iap_jwks_fetcher,
        )
    )


def _seed_agent__web_learnings(
    session_factory__web_learnings,
    config__web_learnings,
    *,
    status=AgentStatus.READY,
    repo_full_name=REPO,
    created_by=None,
):
    session = session_factory__web_learnings()
    session.add(
        Agent(
            name=AGENT_NAME,
            persona=PERSONA,
            model_id=config__web_learnings.models[0].id,
            status=status,
            repo_full_name=repo_full_name,
            created_by=created_by,
        )
    )
    session.commit()
    session.close()


def _seed_learning__web_learnings(session_factory__web_learnings, agent_name=AGENT_NAME, **overrides):
    session = session_factory__web_learnings()
    try:
        defaults = {
            "agent_name": agent_name,
            "day": "2026-09-01",
            "content": "the canary gate is manual",
            "kind": "fact",
            "source": "Slack, Sep 1",
            "evidence": "quote",
            "said_by": "<@U0OWNER>",
            "content_hash": "a" * 64,
        }
        defaults.update(overrides)
        row = AgentLearning(**defaults)
        session.add(row)
        session.commit()
        return row.id
    finally:
        session.close()


def _generate_jwk(kid: str = KID):
    private_key = ec.generate_private_key(ec.SECP256R1())
    jwk = jwt.algorithms.ECAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk["kid"] = kid
    jwk["use"] = "sig"
    jwk["alg"] = "ES256"
    return private_key, jwk


def _sign(private_key, *, email: str | None, kid: str = KID) -> str:
    now = int(time.time())
    claims = {
        "aud": AUDIENCE,
        "iss": IAP_ISSUER,
        "iat": now,
        "exp": now + 3600,
        "sub": "accounts.google.com:1234567890",
    }
    if email is not None:
        claims["email"] = email
    return jwt.encode(claims, private_key, algorithm="ES256", headers={"kid": kid})


def _fetcher(jwk):
    def fetch():
        return {"keys": [jwk]}

    return fetch


def test_decide_gate_denies_non_owner_no_grant(session_factory__web_learnings, config__web_learnings):
    """Gate (authorization): Decide gate denies non owner no grant."""
    private_key, jwk = _generate_jwk()
    iap_config = config__web_learnings.model_copy(update={"iap_audience": AUDIENCE})
    client = _make_client__web_learnings(session_factory__web_learnings, iap_config, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent__web_learnings(session_factory__web_learnings, iap_config, created_by=OWNER_EMAIL)
    learning_id = _seed_learning__web_learnings(session_factory__web_learnings)

    token = _sign(private_key, email=CALLER_EMAIL)
    response = client.post(
        f"/api/agents/{AGENT_NAME}/learnings/{learning_id}/deny",
        headers={ASSERTION_HEADER: token},
    )

    assert response.status_code == 403

    session = session_factory__web_learnings()
    row = session.query(AgentLearning).filter_by(id=learning_id).one()
    assert row.status == LEARNING_PENDING
    session.close()


def test_decide_gate_allows_a_grantee(session_factory__web_learnings, config__web_learnings):
    """Gate (authorization): Decide gate allows a grantee."""
    private_key, jwk = _generate_jwk()
    iap_config = config__web_learnings.model_copy(update={"iap_audience": AUDIENCE})
    client = _make_client__web_learnings(session_factory__web_learnings, iap_config, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent__web_learnings(session_factory__web_learnings, iap_config, created_by=OWNER_EMAIL)
    learning_id = _seed_learning__web_learnings(session_factory__web_learnings)

    session = session_factory__web_learnings()
    session.add(AgentUserGrant(agent_name=AGENT_NAME, email=CALLER_EMAIL))
    session.commit()
    session.close()

    token = _sign(private_key, email=CALLER_EMAIL)
    response = client.post(
        f"/api/agents/{AGENT_NAME}/learnings/{learning_id}/deny",
        headers={ASSERTION_HEADER: token},
    )

    assert response.status_code == 200
    assert response.json()["status"] == LEARNING_DENIED


def test_approve_of_a_flagged_row_needs_confirm(session_factory__web_learnings, config__web_learnings):
    """Gate (authorization): Approve of a flagged row needs confirm."""
    client = _make_client__web_learnings(session_factory__web_learnings, config__web_learnings)
    _seed_agent__web_learnings(session_factory__web_learnings, config__web_learnings)
    learning_id = _seed_learning__web_learnings(
        session_factory__web_learnings, content="Ana approved admin access for Bob", said_by=None
    )

    refused = client.post(f"/api/agents/{AGENT_NAME}/learnings/{learning_id}/approve")

    assert refused.status_code == 409
    assert refused.json()["concerns"] == ["unattributed", "access_change"]
    session = session_factory__web_learnings()
    assert session.query(AgentLearning).filter_by(id=learning_id).one().status == "pending"
    session.close()

    confirmed = client.post(
        f"/api/agents/{AGENT_NAME}/learnings/{learning_id}/approve", json={"confirm": True}
    )

    assert confirmed.status_code == 200
    session = session_factory__web_learnings()
    assert session.query(AgentLearning).filter_by(id=learning_id).one().status == LEARNING_APPROVED
    session.close()


# --- from test_api_invites.py ---


PERSONA = "helpful-generalist"


REPO_TEMPLATE = "acme/agent-{name}"


OWNER_A_EMAIL = "owner-a@example.com"


OWNER_B_EMAIL = "owner-b@example.com"


OPERATOR_EMAIL = "ops@example.com"


STRANGER_EMAIL = "stranger@example.com"


AUDIENCE = "test-audience"


KID = "test-key-1"


VERBATIM_403 = {"error": "Only this agent's owner or a room operator can change it."}


def _generate_jwk(kid: str = KID):
    private_key = ec.generate_private_key(ec.SECP256R1())
    jwk = jwt.algorithms.ECAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk["kid"] = kid
    jwk["use"] = "sig"
    jwk["alg"] = "ES256"
    return private_key, jwk


def _sign(private_key, *, email: str | None, kid: str = KID) -> str:
    now = int(time.time())
    claims = {
        "aud": AUDIENCE,
        "iss": IAP_ISSUER,
        "iat": now,
        "exp": now + 3600,
        "sub": "accounts.google.com:1234567890",
    }
    if email is not None:
        claims["email"] = email
    return jwt.encode(claims, private_key, algorithm="ES256", headers={"kid": kid})


def _fetcher(jwk):
    def fetch():
        return {"keys": [jwk]}

    return fetch


def _headers(private_key, *, email: str) -> dict:
    return {ASSERTION_HEADER: _sign(private_key, email=email)}


@pytest.fixture
def session_factory__api_invites(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__api_invites(tmp_path):
    return Config(workdir=tmp_path / "work", operators=[OPERATOR_EMAIL], iap_audience=AUDIENCE)


def _make_client__api_invites(session_factory__api_invites, config__api_invites, iap_jwks_fetcher):
    return TestClient(
        create_app(
            session_factory__api_invites,
            config=config__api_invites,
            clients=Clients(
                github=FakeGitHub(),
                gojira=FakeGojira(),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=FakeKubectl(),
            ),
            iap_jwks_fetcher=iap_jwks_fetcher,
        )
    )


def _seed_agent__api_invites(session_factory__api_invites, *, name, created_by=None, group_name=None, status=AgentStatus.READY):
    session = session_factory__api_invites()
    session.add(
        Agent(
            name=name,
            persona=PERSONA,
            model_id="vertex_ai/claude-sonnet-4-5",
            status=status,
            repo_full_name=REPO_TEMPLATE.format(name=name),
            created_by=created_by,
            group_name=group_name,
        )
    )
    session.commit()
    session.close()


def _seed_invite(session_factory__api_invites, *, from_agent, to_agent, status="pending", requested_by=None):
    session = session_factory__api_invites()
    invite = A2AInvite(
        from_agent=from_agent, to_agent=to_agent, status=status, requested_by=requested_by
    )
    session.add(invite)
    session.commit()
    invite_id = invite.id
    session.close()
    return invite_id


@pytest.fixture
def client__api_invites(session_factory__api_invites, config__api_invites):
    private_key, jwk = _generate_jwk()
    c = _make_client__api_invites(session_factory__api_invites, config__api_invites, _fetcher(jwk))
    return c, private_key


def _enabled_cabal_config__api_invites(config__api_invites):
    return config__api_invites.model_copy(
        update={
            "cabal_url": "http://cabal.internal",
            "agent_endpoint_template": "http://agent-{name}.default.svc.cluster.local",
        }
    )


def _make_client_with_cabal(session_factory__api_invites, config__api_invites, iap_jwks_fetcher, cabal):
    return TestClient(
        create_app(
            session_factory__api_invites,
            config=config__api_invites,
            clients=Clients(
                github=FakeGitHub(),
                gojira=FakeGojira(),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=FakeKubectl(),
                cabal=cabal,
            ),
            iap_jwks_fetcher=iap_jwks_fetcher,
        )
    )


@pytest.fixture
def cabal_client(session_factory__api_invites, config__api_invites, monkeypatch):
    """Same shape as the plain `client` fixture above, but with the
    switchboard wired to a `FakeCabal` this test can inspect -- returns
    `(TestClient, private_key, FakeCabal)`."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_cabal_config__api_invites(config__api_invites)
    private_key, jwk = _generate_jwk()
    fake_cabal = FakeCabal()
    c = _make_client_with_cabal(session_factory__api_invites, cfg, _fetcher(jwk), fake_cabal)
    return c, private_key, fake_cabal


def test_to_agent_outside_callers_own_visibility_is_still_reachable_by_name(
    session_factory__api_invites, client__api_invites
):
    # The adjudicated design: requesting access to a visible-in-directory-
    # but-not-yours agent is the feature. The TO agent here belongs to a
    # different group and a different owner than the caller entirely, and
    # the request still succeeds -- existence of a directory-known name is
    # not a leak (spec section 5, the a2a directory is deliberately global).
    """Gate (authorization): To agent outside callers own visibility is still reachable by name."""
    c, key = client__api_invites
    _seed_agent__api_invites(session_factory__api_invites, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent__api_invites(session_factory__api_invites, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")

    response = c.post(
        "/api/agents/a/invites",
        json={"to_agent": "b"},
        headers=_headers(key, email=OWNER_A_EMAIL),
    )

    assert response.status_code == 201, response.text


def test_two_shared_group_agents_may_invite_each_other(session_factory__api_invites, client__api_invites):
    # Fix round (shared-group wave HIGH): SHARED_GROUP is a real, equal,
    # non-null group_name on both sides, but pairs.allowed_pairs excludes it
    # from implicit pairing on purpose (commons agents stay invite-gated
    # even though they're humanly visible to everyone). create_invite's own
    # "already in the same group" 422 must route through that same
    # implicitly_paired predicate, not the raw same-non-null-group check --
    # otherwise two commons agents could never invite each other at all.
    """Gate (authorization): Two shared group agents may invite each other."""
    c, key = client__api_invites
    _seed_agent__api_invites(session_factory__api_invites, name="a", created_by=OWNER_A_EMAIL, group_name=SHARED_GROUP)
    _seed_agent__api_invites(session_factory__api_invites, name="b", created_by=OWNER_B_EMAIL, group_name=SHARED_GROUP)

    response = c.post(
        "/api/agents/a/invites",
        json={"to_agent": "b"},
        headers=_headers(key, email=OWNER_A_EMAIL),
    )

    assert response.status_code == 201, response.text


def test_accepted_invite_between_shared_group_agents_grants_the_directional_pair(
    session_factory__api_invites, client__api_invites, config__api_invites
):
    # The reviewer's live repro, end to end: two READY shared agents can
    # invite each other, accepting the invite actually grants the
    # directional allowed_pairs entry (the wire the switchboard enforces),
    # and the reverse direction is still not implied.
    """Gate (authorization): Accepted invite between shared group agents grants the directional pair."""
    c, key = client__api_invites
    _seed_agent__api_invites(session_factory__api_invites, name="a", created_by=OWNER_A_EMAIL, group_name=SHARED_GROUP)
    _seed_agent__api_invites(session_factory__api_invites, name="b", created_by=OWNER_B_EMAIL, group_name=SHARED_GROUP)

    create_response = c.post(
        "/api/agents/a/invites",
        json={"to_agent": "b"},
        headers=_headers(key, email=OWNER_A_EMAIL),
    )
    assert create_response.status_code == 201, create_response.text
    invite_id = create_response.json()["id"]

    accept_response = c.post(
        f"/api/invites/{invite_id}/accept", headers=_headers(key, email=OWNER_B_EMAIL)
    )
    assert accept_response.status_code == 200, accept_response.text

    session = session_factory__api_invites()
    pairs = allowed_pairs(session, config__api_invites)
    session.close()

    assert ("agent:a", "b") in pairs
    assert ("agent:b", "a") not in pairs

    # The accepted invite is what grants the pair, not group membership --
    # team_pairs (auto-paired colleagues) must still not list "b" for "a":
    # an explicit invite is a revocable arrangement, never an implicit one.
    invites_response = c.get("/api/agents/a/invites", headers=_headers(key, email=OWNER_A_EMAIL))
    assert invites_response.json()["team_pairs"] == []


def test_owner_of_from_agent_may_not_accept_their_own_outgoing_request(session_factory__api_invites, client__api_invites):
    """Gate (authorization): Owner of from agent may not accept their own outgoing request."""
    c, key = client__api_invites
    _seed_agent__api_invites(session_factory__api_invites, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent__api_invites(session_factory__api_invites, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")
    invite_id = _seed_invite(session_factory__api_invites, from_agent="a", to_agent="b", status="pending")

    response = c.post(
        f"/api/invites/{invite_id}/accept", headers=_headers(key, email=OWNER_A_EMAIL)
    )

    assert response.status_code == 403
    assert response.json() == VERBATIM_403


def test_accept_by_a_stranger_matches_the_missing_invite_body_shape(session_factory__api_invites, client__api_invites):
    # Fold 1 (C1 review): the accept path used to fetch the TO agent through
    # `_visible_agent_or_404`, which leaked the TO agent's real name in its
    # 404 body ({"error": "no such agent: 'b'"}) -- distinguishable from a
    # genuinely missing invite id's body by content alone even though both
    # were 404s. Both are pinned to the identical `"no such invite: {id}"`
    # template (each with its own id, never an agent name) rather than to
    # each other verbatim, since the two ids necessarily differ.
    """Gate (authorization): Accept by a stranger matches the missing invite body shape."""
    c, key = client__api_invites
    _seed_agent__api_invites(session_factory__api_invites, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent__api_invites(session_factory__api_invites, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")
    invite_id = _seed_invite(session_factory__api_invites, from_agent="a", to_agent="b", status="pending")

    real_invite_response = c.post(
        f"/api/invites/{invite_id}/accept", headers=_headers(key, email=STRANGER_EMAIL)
    )
    missing_invite_response = c.post(
        "/api/invites/999999/accept", headers=_headers(key, email=STRANGER_EMAIL)
    )

    assert real_invite_response.status_code == missing_invite_response.status_code == 404
    assert real_invite_response.json() == {"error": f"no such invite: {invite_id}"}
    assert missing_invite_response.json() == {"error": "no such invite: 999999"}


def test_full_lifecycle_request_accept_then_revoke(session_factory__api_invites, client__api_invites):
    """Gate (authorization): Full lifecycle request accept then revoke."""
    c, key = client__api_invites
    _seed_agent__api_invites(session_factory__api_invites, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent__api_invites(session_factory__api_invites, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")

    created = c.post(
        "/api/agents/a/invites",
        json={"to_agent": "b"},
        headers=_headers(key, email=OWNER_A_EMAIL),
    )
    assert created.status_code == 201, created.text
    invite_id = created.json()["id"]

    accepted = c.post(
        f"/api/invites/{invite_id}/accept", headers=_headers(key, email=OWNER_B_EMAIL)
    )
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "accepted"

    revoked = c.delete(
        f"/api/invites/{invite_id}", headers=_headers(key, email=OWNER_A_EMAIL)
    )
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"

    # A fresh request between the same pair is allowed again afterward.
    fresh = c.post(
        "/api/agents/a/invites",
        json={"to_agent": "b"},
        headers=_headers(key, email=OWNER_A_EMAIL),
    )
    assert fresh.status_code == 201, fresh.text


def test_accept_syncs_dispatch_pairs_to_the_switchboard(session_factory__api_invites, cabal_client):
    """Gate (authorization): Accept syncs dispatch pairs to the switchboard."""
    c, key, fake_cabal = cabal_client
    _seed_agent__api_invites(session_factory__api_invites, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent__api_invites(session_factory__api_invites, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")
    invite_id = _seed_invite(session_factory__api_invites, from_agent="a", to_agent="b", status="pending")

    response = c.post(
        f"/api/invites/{invite_id}/accept", headers=_headers(key, email=OWNER_B_EMAIL)
    )

    assert response.status_code == 200, response.text
    assert fake_cabal.put_dispatch_pairs_calls == [[("agent:a", "b")]]


def test_accept_then_revoke_each_sync_exactly_once(session_factory__api_invites, cabal_client):
    """Gate (authorization): Accept then revoke each sync exactly once."""
    c, key, fake_cabal = cabal_client
    _seed_agent__api_invites(session_factory__api_invites, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent__api_invites(session_factory__api_invites, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")
    invite_id = _seed_invite(session_factory__api_invites, from_agent="a", to_agent="b", status="pending")

    c.post(f"/api/invites/{invite_id}/accept", headers=_headers(key, email=OWNER_B_EMAIL))
    c.delete(f"/api/invites/{invite_id}", headers=_headers(key, email=OWNER_A_EMAIL))

    assert fake_cabal.put_dispatch_pairs_calls == [[("agent:a", "b")], []]


# --- from test_api_requestable.py ---


PERSONA = "helpful-generalist"


REPO_TEMPLATE = "acme/agent-{name}"


OWNER_A_EMAIL = "owner-a@example.com"


OWNER_B_EMAIL = "owner-b@example.com"


OPERATOR_EMAIL = "ops@example.com"


AUDIENCE = "test-audience"


KID = "test-key-1"


def _generate_jwk(kid: str = KID):
    private_key = ec.generate_private_key(ec.SECP256R1())
    jwk = jwt.algorithms.ECAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk["kid"] = kid
    jwk["use"] = "sig"
    jwk["alg"] = "ES256"
    return private_key, jwk


def _sign(private_key, *, email: str | None, kid: str = KID) -> str:
    now = int(time.time())
    claims = {
        "aud": AUDIENCE,
        "iss": IAP_ISSUER,
        "iat": now,
        "exp": now + 3600,
        "sub": "accounts.google.com:1234567890",
    }
    if email is not None:
        claims["email"] = email
    return jwt.encode(claims, private_key, algorithm="ES256", headers={"kid": kid})


def _fetcher(jwk):
    def fetch():
        return {"keys": [jwk]}

    return fetch


def _headers(private_key, *, email: str) -> dict:
    return {ASSERTION_HEADER: _sign(private_key, email=email)}


@pytest.fixture
def session_factory__api_requestable(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__api_requestable(tmp_path):
    return Config(workdir=tmp_path / "work", operators=[OPERATOR_EMAIL], iap_audience=AUDIENCE)


def _make_client__api_requestable(session_factory__api_requestable, config__api_requestable, iap_jwks_fetcher):
    return TestClient(
        create_app(
            session_factory__api_requestable,
            config=config__api_requestable,
            clients=Clients(
                github=FakeGitHub(),
                gojira=FakeGojira(),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=FakeKubectl(),
            ),
            iap_jwks_fetcher=iap_jwks_fetcher,
        )
    )


def _seed_agent__api_requestable(
    session_factory__api_requestable,
    *,
    name,
    created_by=None,
    group_name=None,
    team=None,
    capability=None,
    purpose_tags=None,
    status=AgentStatus.READY,
):
    from kobo_api.purpose_tags import encode_tags

    session = session_factory__api_requestable()
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
            capability=capability,
            purpose_tags=encode_tags(purpose_tags) if purpose_tags is not None else None,
        )
    )
    session.commit()
    session.close()


@pytest.fixture
def client__api_requestable(session_factory__api_requestable, config__api_requestable):
    private_key, jwk = _generate_jwk()
    c = _make_client__api_requestable(session_factory__api_requestable, config__api_requestable, _fetcher(jwk))
    return c, private_key


def test_requestable_excludes_same_group_colleagues_already_auto_paired(session_factory__api_requestable, client__api_requestable):
    """Gate (authorization): Requestable excludes same group colleagues already auto paired."""
    c, key = client__api_requestable
    _seed_agent__api_requestable(session_factory__api_requestable, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent__api_requestable(session_factory__api_requestable, name="b", created_by=OWNER_B_EMAIL, group_name="growth")
    _seed_agent__api_requestable(session_factory__api_requestable, name="c", created_by=OWNER_B_EMAIL, group_name="payments-eng")

    response = c.get("/api/agents/a/requestable", headers=_headers(key, email=OWNER_A_EMAIL))

    assert response.status_code == 200
    names = [entry["name"] for entry in response.json()["requestable"]]
    assert names == ["c"]


# --- from test_pairs.py ---


PERSONA = "helpful-generalist"


MODEL_ID = "vertex_ai/claude-sonnet-4-5"


@pytest.fixture
def session_factory__pairs(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def session(session_factory__pairs):
    s = session_factory__pairs()
    yield s
    s.close()


@pytest.fixture
def config__pairs(tmp_path):
    return Config(workdir=tmp_path / "work")


def _agent(
    session, name, *, group_name=None, status=AgentStatus.READY, kind=KIND_COMPOSED
):
    agent = Agent(
        name=name,
        persona=PERSONA,
        model_id=MODEL_ID,
        status=status,
        repo_full_name=f"acme/agent-{name}",
        group_name=group_name,
        kind=kind,
    )
    session.add(agent)
    session.commit()
    return agent


def _invite(session, *, from_agent, to_agent, status):
    invite = A2AInvite(from_agent=from_agent, to_agent=to_agent, status=status)
    session.add(invite)
    session.commit()
    return invite


def _enabled_cabal_config__pairs(config__pairs):
    return config__pairs.model_copy(
        update={
            "cabal_url": "http://cabal.internal",
            "agent_endpoint_template": "http://agent-{name}.default.svc.cluster.local",
        }
    )


def _clients_with_cabal(cabal):
    return Clients(github=None, gojira=None, cloud=None, agent=None, kubectl=None, cabal=cabal)


def test_same_group_pair_is_symmetric_both_directions(session, config__pairs):
    """Gate (authorization): Same group pair is symmetric both directions."""
    _agent(session, "a", group_name="growth")
    _agent(session, "b", group_name="growth")

    pairs = allowed_pairs(session, config__pairs)

    assert ("agent:a", "b") in pairs
    assert ("agent:b", "a") in pairs


def test_different_groups_produce_no_pair(session, config__pairs):
    """Gate (authorization): Different groups produce no pair."""
    _agent(session, "a", group_name="growth")
    _agent(session, "b", group_name="payments-eng")

    pairs = allowed_pairs(session, config__pairs)

    assert pairs == set()


def test_a_connected_agent_is_not_paired_with_its_own_group(session, config__pairs):
    """Gate (authorization): A connected agent is not paired with its own group."""
    _agent(session, "a", group_name="growth")
    _agent(session, "b", group_name="growth", kind=KIND_CONNECTED)

    pairs = allowed_pairs(session, config__pairs)

    assert pairs == set()


def test_implicitly_paired_false_for_shared_group_even_though_equal():
    # The one exception: SHARED_GROUP is a real, equal, non-null group on
    # both sides, but the commons stays invite-gated -- the whole point of
    # this predicate existing as a single shared definition.
    """Gate (authorization): Implicitly paired false for shared group even though equal."""
    assert implicitly_paired(SHARED_GROUP, SHARED_GROUP) is False


def test_accepted_invite_adds_only_the_forward_direction(session, config__pairs):
    """Gate (authorization): Accepted invite adds only the forward direction."""
    _agent(session, "a", group_name="growth")
    _agent(session, "b", group_name="payments-eng")
    _invite(session, from_agent="a", to_agent="b", status="accepted")

    pairs = allowed_pairs(session, config__pairs)

    assert ("agent:a", "b") in pairs
    assert ("agent:b", "a") not in pairs


def test_pending_invite_grants_no_pair(session, config__pairs):
    """Gate (authorization): Pending invite grants no pair."""
    _agent(session, "a", group_name="growth")
    _agent(session, "b", group_name="payments-eng")
    _invite(session, from_agent="a", to_agent="b", status="pending")

    pairs = allowed_pairs(session, config__pairs)

    assert pairs == set()


def test_revoking_a_previously_accepted_invite_removes_the_pair(session, config__pairs):
    # Same row, status flipped after the fact -- mirrors what the revoke
    # route actually does (UPDATE, not DELETE) rather than modeling it as a
    # fresh row.
    """Gate (authorization): Revoking a previously accepted invite removes the pair."""
    _agent(session, "a", group_name="growth")
    _agent(session, "b", group_name="payments-eng")
    invite = _invite(session, from_agent="a", to_agent="b", status="accepted")
    assert ("agent:a", "b") in allowed_pairs(session, config__pairs)

    invite.status = "revoked"
    session.commit()

    assert allowed_pairs(session, config__pairs) == set()


def test_non_ready_agent_produces_no_same_group_pair(session, config__pairs):
    """Gate (authorization): Non ready agent produces no same group pair."""
    _agent(session, "a", group_name="growth", status=AgentStatus.PROVISIONING)
    _agent(session, "b", group_name="growth")

    pairs = allowed_pairs(session, config__pairs)

    assert pairs == set()


def test_sync_dispatch_pairs_pushes_empty_set_when_nothing_is_allowed(session, config__pairs):
    """Gate (authorization): Sync dispatch pairs pushes empty set when nothing is allowed."""
    fake_cabal = FakeCabal()

    sync_dispatch_pairs(fake_cabal, session, config__pairs)

    assert fake_cabal.put_dispatch_pairs_calls == [[]]


def test_best_effort_retries_on_the_next_call_after_a_failure_even_with_unchanged_pairs(
    session, config__pairs, monkeypatch
):
    # The reconciling-retry guarantee (HIGH-2b): a failed sync must not
    # wedge this seam shut -- the NEXT call (an organic trigger, or the
    # boot-time sync) tries again and succeeds, even though nothing about
    # the underlying pairs changed between the two calls.
    """Gate (authorization): Best effort retries on the next call after a failure even with unchanged pairs."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_cabal_config__pairs(config__pairs)
    _agent(session, "a", group_name="growth")
    _agent(session, "b", group_name="growth")
    fake_cabal = FakeCabal(put_dispatch_pairs_error="switchboard unreachable")
    clients = _clients_with_cabal(fake_cabal)

    first = sync_dispatch_pairs_best_effort(clients, cfg, session)
    assert first is None
    assert len(fake_cabal.put_dispatch_pairs_calls) == 1

    fake_cabal._put_dispatch_pairs_error = None  # the switchboard recovers
    second = sync_dispatch_pairs_best_effort(clients, cfg, session)

    assert second == 2
    assert len(fake_cabal.put_dispatch_pairs_calls) == 2
    assert fake_cabal.put_dispatch_pairs_calls[0] == fake_cabal.put_dispatch_pairs_calls[1]


# --- from test_api_threads.py ---


PERSONA = "helpful-generalist"


MODEL_ID = "vertex_ai/claude-sonnet-4-5"


REPO_TEMPLATE = "acme/agent-{name}"


ENDPOINT_TEMPLATE = "http://agent-{name}.default.svc.cluster.local"


OPERATOR_EMAIL = "ops@example.com"


INITIATOR_EMAIL = "initiator@example.com"


GROUP_MEMBER_EMAIL = "member@example.com"


STRANGER_EMAIL = "stranger@example.com"


AUDIENCE = "test-audience"


KID = "test-key-1"


GROUPS__api_threads = [
    GroupChoice(name="growth", okta_group="okta-growth"),
    GroupChoice(name="payments-eng", okta_group="okta-payments"),
]


CANNED_REPLY_TEXT = "Here's my take."


class FakeGroupResolver__api_threads:
    """The plainest possible stand-in for `GroupResolver` (kobo_api.groups),
    lifted verbatim from test_web_visibility.py's own copy: a bare object
    exposing `groups_for(email) -> list[str] | None`, nothing more."""

    def __init__(self, mapping: dict[str, list[str]] | None = None, *, down: bool = False):
        self._mapping = dict(mapping) if mapping else {}
        self._down = down

    def groups_for(self, email: str) -> list[str] | None:
        if self._down:
            return None
        return self._mapping.get(email.lower(), [])


def _generate_jwk(kid: str = KID):
    private_key = ec.generate_private_key(ec.SECP256R1())
    jwk = jwt.algorithms.ECAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk["kid"] = kid
    jwk["use"] = "sig"
    jwk["alg"] = "ES256"
    return private_key, jwk


def _sign(private_key, *, email: str | None, kid: str = KID) -> str:
    now = int(time.time())
    claims = {
        "aud": AUDIENCE,
        "iss": IAP_ISSUER,
        "iat": now,
        "exp": now + 3600,
        "sub": "accounts.google.com:1234567890",
    }
    if email is not None:
        claims["email"] = email
    return jwt.encode(claims, private_key, algorithm="ES256", headers={"kid": kid})


def _fetcher(jwk):
    def fetch():
        return {"keys": [jwk]}

    return fetch


def _headers(private_key, *, email: str) -> dict:
    return {ASSERTION_HEADER: _sign(private_key, email=email)}


@pytest.fixture
def session_factory__api_threads(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__api_threads(tmp_path):
    return Config(
        workdir=tmp_path / "work",
        operators=[OPERATOR_EMAIL],
        iap_audience=AUDIENCE,
        groups=GROUPS__api_threads,
        cabal_url="http://cabal.internal",
        agent_endpoint_template=ENDPOINT_TEMPLATE,
    )


def _make_client__api_threads(
    session_factory__api_threads,
    config__api_threads,
    *,
    groups_resolver=None,
    cabal=None,
    iap_jwks_fetcher,
) -> TestClient:
    return TestClient(
        create_app(
            session_factory__api_threads,
            config=config__api_threads,
            clients=Clients(
                github=FakeGitHub(),
                gojira=FakeGojira(),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=FakeKubectl(),
                groups=groups_resolver,
                cabal=cabal,
            ),
            iap_jwks_fetcher=iap_jwks_fetcher,
        )
    )


def _seed_agent__api_threads(
    session_factory__api_threads,
    *,
    name: str,
    group_name: str | None = None,
    status: str = AgentStatus.READY,
    created_by: str | None = None,
) -> None:
    session = session_factory__api_threads()
    session.add(
        Agent(
            name=name,
            persona=PERSONA,
            model_id=MODEL_ID,
            status=status,
            repo_full_name=REPO_TEMPLATE.format(name=name),
            created_by=created_by,
            group_name=group_name,
        )
    )
    session.commit()
    session.close()


def _seed_invite__api_threads(session_factory__api_threads, *, from_agent: str, to_agent: str, status: str = INVITE_ACCEPTED) -> None:
    session = session_factory__api_threads()
    session.add(A2AInvite(from_agent=from_agent, to_agent=to_agent, status=status))
    session.commit()
    session.close()


def _relay_result(text: str) -> dict:
    return {
        "status": {
            "state": "completed",
            "message": {"role": "agent", "parts": [{"kind": "text", "text": text}]},
        },
    }


def test_create_thread_agent_the_caller_cannot_see_is_refused_with_anti_enumeration_answer(
    session_factory__api_threads, config__api_threads
):
    """Gate (visibility): Create thread agent the caller cannot see is refused with anti enumeration answer.

    Adding an agent the initiator cannot see is refused with the room's
    existing anti-enumeration answer, not a distinguishable error -- the
    same body a genuinely unknown name gets above."""
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver__api_threads({INITIATOR_EMAIL: ["growth"]})
    client = _make_client__api_threads(session_factory__api_threads, config__api_threads, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent__api_threads(session_factory__api_threads, name="hidden", group_name="payments-eng")

    response = client.post(
        "/api/threads", json={"agent": "hidden"}, headers=_headers(private_key, email=INITIATOR_EMAIL)
    )

    assert response.status_code == 404
    assert response.json() == {"error": "no such agent: 'hidden'"}


def test_adding_a_cross_group_agent_without_an_accepted_invite_is_refused(session_factory__api_threads, config__api_threads):
    """Gate (visibility): Adding a cross group agent without an accepted invite is refused.

    Proven from a non-operator identity: INITIATOR_EMAIL never appears in
    config.operators. This reuses the existing consent handshake (`kobo_api.
    pairs.implicitly_paired` / `A2AInvite`) -- it introduces no new consent
    concept and must not bypass the existing one."""
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver__api_threads({INITIATOR_EMAIL: ["growth", "payments-eng"]})
    client = _make_client__api_threads(session_factory__api_threads, config__api_threads, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent__api_threads(session_factory__api_threads, name="a", group_name="growth")
    _seed_agent__api_threads(session_factory__api_threads, name="c", group_name="payments-eng")
    thread_id = client.post(
        "/api/threads", json={"agent": "a"}, headers=_headers(private_key, email=INITIATOR_EMAIL)
    ).json()["id"]

    response = client.post(
        f"/api/threads/{thread_id}/participants",
        json={"agent": "c"},
        headers=_headers(private_key, email=INITIATOR_EMAIL),
    )

    assert response.status_code == 422
    assert "not connected" in response.json()["error"]


def test_adding_a_cross_group_agent_with_an_accepted_invite_is_allowed(session_factory__api_threads, config__api_threads):
    """Gate (visibility): Adding a cross group agent with an accepted invite is allowed."""
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver__api_threads({INITIATOR_EMAIL: ["growth", "payments-eng"]})
    client = _make_client__api_threads(session_factory__api_threads, config__api_threads, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent__api_threads(session_factory__api_threads, name="a", group_name="growth")
    _seed_agent__api_threads(session_factory__api_threads, name="c", group_name="payments-eng")
    _seed_invite__api_threads(session_factory__api_threads, from_agent="a", to_agent="c", status=INVITE_ACCEPTED)
    thread_id = client.post(
        "/api/threads", json={"agent": "a"}, headers=_headers(private_key, email=INITIATOR_EMAIL)
    ).json()["id"]

    response = client.post(
        f"/api/threads/{thread_id}/participants",
        json={"agent": "c"},
        headers=_headers(private_key, email=INITIATOR_EMAIL),
    )

    assert response.status_code == 201, response.text
    assert response.json()["participants"] == ["a", "c"]


def test_thread_read_by_a_same_group_member_who_is_not_the_initiator_is_not_found(
    session_factory__api_threads, config__api_threads
):
    """Gate (visibility): Thread read by a same group member who is not the initiator is not found.

    Operator ruling: a thread is readable by its initiator and by
    nobody else. GROUP_MEMBER_EMAIL shares agent "a"'s own group (growth),
    so ordinary group-derived visibility lets them see "a" freely -- but
    they did not initiate this thread, and must get the SAME not-found
    answer a bogus thread id gets, never a 403 that would confirm the
    thread is real."""
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver__api_threads(
        {INITIATOR_EMAIL: ["growth"], GROUP_MEMBER_EMAIL: ["growth"]}
    )
    client = _make_client__api_threads(session_factory__api_threads, config__api_threads, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent__api_threads(session_factory__api_threads, name="a", group_name="growth")
    thread_id = client.post(
        "/api/threads", json={"agent": "a"}, headers=_headers(private_key, email=INITIATOR_EMAIL)
    ).json()["id"]

    response = client.get(
        f"/api/threads/{thread_id}", headers=_headers(private_key, email=GROUP_MEMBER_EMAIL)
    )

    assert response.status_code == 404
    assert response.json() == {"error": f"no such thread: {thread_id}"}
    # Not a visibility gap: this same caller can see agent "a" itself just fine.
    agent_response = client.get(
        "/api/agents/a", headers=_headers(private_key, email=GROUP_MEMBER_EMAIL)
    )
    assert agent_response.status_code == 200


def test_thread_read_by_an_operator_who_is_not_the_initiator_is_also_not_found(
    session_factory__api_threads, config__api_threads
):
    """Gate (visibility): Thread read by an operator who is not the initiator is also not found.

    No operator carve-out: unlike every owner-or-operator gate elsewhere
    in this room, the ruling says "by nobody else," full stop."""
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver__api_threads({INITIATOR_EMAIL: ["growth"]})
    client = _make_client__api_threads(session_factory__api_threads, config__api_threads, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent__api_threads(session_factory__api_threads, name="a", group_name="growth")
    thread_id = client.post(
        "/api/threads", json={"agent": "a"}, headers=_headers(private_key, email=INITIATOR_EMAIL)
    ).json()["id"]

    response = client.get(
        f"/api/threads/{thread_id}", headers=_headers(private_key, email=OPERATOR_EMAIL)
    )

    assert response.status_code == 404
    assert response.json() == {"error": f"no such thread: {thread_id}"}


def test_turn_by_a_non_initiator_is_not_found_and_never_relayed(session_factory__api_threads, config__api_threads, monkeypatch):
    """Gate (visibility): Turn by a non initiator is not found and never relayed."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver__api_threads({INITIATOR_EMAIL: ["growth"], STRANGER_EMAIL: []})
    fake_cabal = FakeCabal(relay_result=_relay_result(CANNED_REPLY_TEXT))
    client = _make_client__api_threads(
        session_factory__api_threads, config__api_threads, groups_resolver=resolver, cabal=fake_cabal, iap_jwks_fetcher=_fetcher(jwk)
    )
    _seed_agent__api_threads(session_factory__api_threads, name="a", group_name="growth")
    thread_id = client.post(
        "/api/threads", json={"agent": "a"}, headers=_headers(private_key, email=INITIATOR_EMAIL)
    ).json()["id"]

    response = client.post(
        f"/api/threads/{thread_id}/turns",
        json={"messages": [{"role": "user", "text": "hi"}]},
        headers=_headers(private_key, email=STRANGER_EMAIL),
    )

    assert response.status_code == 404
    assert response.json() == {"error": f"no such thread: {thread_id}"}
    assert fake_cabal.relay_calls == []


def test_flatten_conversation_entries_excludes_a_thread_tagged_conversation():
    """Gate (visibility): Flatten conversation entries excludes a thread tagged conversation.

    Direct, discriminating unit test of the filter itself: a completed
    conversation whose result carries `metadata.thread` contributes zero
    feed lines, mirroring the existing "incomplete conversation" early
    return this function already makes."""
    detail = {
        "conversation": {
            "task_id": "t-1",
            "target_agent": "a",
            "caller_agent": "operator:initiator@example.com",
            "created_at": "2026-08-27T00:00:00Z",
            "updated_at": "2026-08-27T00:00:01Z",
            "result": {
                "status": {
                    "state": "completed",
                    "message": {"role": "agent", "parts": [{"kind": "text", "text": "reply"}]},
                },
                "history": [{"role": "user", "parts": [{"kind": "text", "text": "ask"}]}],
                "metadata": {"thread": 7},
            },
        },
        "events": [],
    }

    assert _flatten_conversation_entries(detail) == []


def test_pairs_never_names_an_agent_the_caller_cannot_see(session_factory__api_threads, config__api_threads):
    """Gate (visibility): Pairs never names an agent the caller cannot see."""
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver__api_threads({INITIATOR_EMAIL: ["growth"]})
    client = _make_client__api_threads(session_factory__api_threads, config__api_threads, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent__api_threads(session_factory__api_threads, name="a", group_name="growth")
    _seed_agent__api_threads(session_factory__api_threads, name="c", group_name="payments-eng")
    _seed_invite__api_threads(session_factory__api_threads, from_agent="a", to_agent="c", status=INVITE_ACCEPTED)

    response = client.get("/api/agents/a/pairs", headers=_headers(private_key, email=INITIATOR_EMAIL))

    assert response.status_code == 200, response.text
    assert response.json()["pairs"] == []


# --- from test_api_colleagues.py ---


ENDPOINT_TEMPLATE = "http://agent-{name}.default.svc.cluster.local"


CONVERSATIONS_FIXTURE = [
    {
        "task_id": "t-1",
        "target_agent": "clio",
        "target_generation": 1,
        "caller_agent": "agent:iris",
        "state": "completed",
        "cabal_escalated_from": None,
        "created_at": "2026-08-19T10:00:00Z",
        "updated_at": "2026-08-19T10:00:05Z",
        "result_snippet": "Sure, here's how you configure that.",
    },
    {
        "task_id": "t-2",
        "target_agent": "clio",
        "target_generation": 1,
        "caller_agent": "operator:kgreiner@example.com",
        "state": "completed",
        "cabal_escalated_from": None,
        "created_at": "2026-08-19T11:00:00Z",
        "updated_at": "2026-08-19T11:00:02Z",
        "result_snippet": "Hello there.",
    },
    {
        "task_id": "t-3",
        "target_agent": "warden",
        "target_generation": 1,
        "caller_agent": "service:kobo",
        "state": "completed",
        "cabal_escalated_from": None,
        "created_at": "2026-08-19T12:00:00Z",
        "updated_at": "2026-08-19T12:00:01Z",
        "result_snippet": "System check ok.",
    },
]


@pytest.fixture
def session_factory__api_colleagues(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__api_colleagues(tmp_path):
    return Config(workdir=tmp_path / "work")


def _enabled_config__api_colleagues(config__api_colleagues):
    return config__api_colleagues.model_copy(
        update={
            "cabal_url": "http://cabal.internal",
            "agent_endpoint_template": ENDPOINT_TEMPLATE,
        }
    )


def _make_client__api_colleagues(session_factory__api_colleagues, config__api_colleagues, *, cabal=None):
    return TestClient(
        create_app(
            session_factory__api_colleagues,
            config=config__api_colleagues,
            clients=Clients(
                github=FakeGitHub(),
                gojira=FakeGojira(),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=FakeKubectl(),
                cabal=cabal,
            ),
        )
    )


def _thread_tagged_detail(
    task_id: str, *, target_agent: str, caller_agent: str, thread_id: int = 1
) -> dict:
    return {
        "conversation": {
            "task_id": task_id,
            "target_agent": target_agent,
            "caller_agent": caller_agent,
            "result": {
                "status": {
                    "state": "completed",
                    "message": {"role": "agent", "parts": [{"kind": "text", "text": "reply"}]},
                },
                "history": [{"role": "user", "parts": [{"kind": "text", "text": "ask"}]}],
                "metadata": {"thread": thread_id},
            },
        },
        "events": [],
    }


_THREAD_ROW = {
    "task_id": "t-thread",
    "target_agent": "clio",
    "target_generation": 1,
    "caller_agent": "operator:kgreiner@example.com",
    "state": "completed",
    "cabal_escalated_from": None,
    "created_at": "2026-08-19T13:00:00Z",
    "updated_at": "2026-08-19T13:00:01Z",
    "result_snippet": "the whole private thread transcript",
}


def _seed_agent__api_colleagues(session_factory__api_colleagues, config__api_colleagues, name: str = "ward", **kwargs) -> None:
    session = session_factory__api_colleagues()
    session.add(
        Agent(
            name=name,
            persona="helpful-generalist",
            model_id=config__api_colleagues.models[0].id,
            status=AgentStatus.READY,
            repo_full_name=f"acme/agent-{name}",
            **kwargs,
        )
    )
    session.commit()
    session.close()


def _task_result(*, ask_text: str, reply_text: str) -> dict:
    """A completed a2a Task body shaped like a REAL daedalus agent's persisted
    `result_json` (the shape `daedalus.transports.a2a.task_to_a2a` emits and
    the switchboard stores): `status.message` is the reply, and `history`
    holds the FULL transcript -- the user's question AND the agent's reply,
    in order. NOTE the earlier fixture modeled only cabal's builtin EchoAgent
    (history = [user] alone), where `history[-1]` was the ask; every real
    colleague (a daedalus agent) persists [user, agent], so `history[-1]` is
    the ANSWER, not the ask. The feed must take the ask from the user-role
    message, not blindly from `history[-1]`."""
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


_VISIBILITY_AUDIENCE = "feed-visibility-audience"


_VISIBILITY_KID = "feed-visibility-key-1"


class _StrangerGroupResolver:
    """The plainest possible `GroupResolver` stand-in: everyone belongs to
    nothing, so ownership is the only route to visibility left standing."""

    def groups_for(self, email: str) -> list[str] | None:
        return []


def _generate_jwk__api_colleagues(kid: str = _VISIBILITY_KID):
    private_key = ec.generate_private_key(ec.SECP256R1())
    jwk = jwt.algorithms.ECAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk["kid"] = kid
    jwk["use"] = "sig"
    jwk["alg"] = "ES256"
    return private_key, jwk


def _sign__api_colleagues(private_key, *, email: str, kid: str = _VISIBILITY_KID) -> str:
    now = int(time.time())
    claims = {
        "aud": _VISIBILITY_AUDIENCE,
        "iss": IAP_ISSUER,
        "iat": now,
        "exp": now + 3600,
        "sub": "accounts.google.com:1234567890",
        "email": email,
    }
    return jwt.encode(claims, private_key, algorithm="ES256", headers={"kid": kid})


def _iap_headers(private_key, *, email: str) -> dict:
    return {ASSERTION_HEADER: _sign__api_colleagues(private_key, email=email)}


def _visibility_room(session_factory__api_colleagues, tmp_path, monkeypatch, cabal):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    private_key, jwk = _generate_jwk__api_colleagues()
    cfg = Config(
        workdir=tmp_path / "work",
        cabal_url="http://cabal.internal",
        agent_endpoint_template=ENDPOINT_TEMPLATE,
        iap_audience=_VISIBILITY_AUDIENCE,
        groups=[GroupChoice(name="payments-eng", okta_group="okta-payments")],
    )
    _seed_agent__api_colleagues(
        session_factory__api_colleagues, cfg, name="ward", group_name="payments-eng",
        created_by="owner@example.com",
    )
    client = TestClient(
        create_app(
            session_factory__api_colleagues,
            config=cfg,
            clients=Clients(
                github=FakeGitHub(), gojira=FakeGojira(), cloud=FakeCloud(),
                agent=FakeAgent(), kubectl=FakeKubectl(), cabal=cabal,
                groups=_StrangerGroupResolver(),
            ),
            iap_jwks_fetcher=lambda: {"keys": [jwk]},
        )
    )
    return client, private_key


_HUMAN_ROW = {
    "task_id": "t-human",
    "target_agent": "ward",
    "target_generation": 1,
    "caller_agent": "operator:owner@example.com",
    "state": "completed",
    "cabal_escalated_from": None,
    "created_at": "2026-08-19T11:00:00Z",
    "updated_at": "2026-08-19T11:00:02Z",
    "result_snippet": "the hint is",
}


def _human_detail():
    return {
        "conversation": {
            **_HUMAN_ROW,
            "result": _task_result(ask_text="the rota hint?", reply_text="the hint is x"),
        },
        "events": [],
    }


def test_list_all_true_excludes_a_thread_tagged_conversation(session_factory__api_colleagues, config__api_colleagues, monkeypatch):
    """Gate (visibility): List all true excludes a thread tagged conversation."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config__api_colleagues(config__api_colleagues)
    fake_cabal = FakeCabal(
        conversations_result=CONVERSATIONS_FIXTURE + [_THREAD_ROW],
        conversation_results={
            "t-thread": _thread_tagged_detail(
                "t-thread", target_agent="clio", caller_agent="operator:kgreiner@example.com"
            )
        },
    )
    client = _make_client__api_colleagues(session_factory__api_colleagues, cfg, cabal=fake_cabal)

    response = client.get("/api/colleagues/conversations", params={"all": "true"})

    assert response.status_code == 200
    assert [c["task_id"] for c in response.json()["conversations"]] == ["t-1", "t-2", "t-3"]


def test_room_wide_routes_hide_conversations_of_agents_the_caller_cannot_see(
    session_factory__api_colleagues, tmp_path, monkeypatch
):
    """Gate (visibility): Room wide routes hide conversations of agents the caller cannot see."""
    cabal = FakeCabal(
        conversations_result=[_HUMAN_ROW], conversation_results={"t-human": _human_detail()}
    )
    client, key = _visibility_room(session_factory__api_colleagues, tmp_path, monkeypatch, cabal)
    stranger = _iap_headers(key, email="stranger@example.com")

    listed = client.get("/api/colleagues/conversations", params={"all": "true"}, headers=stranger)
    detail = client.get("/api/colleagues/conversations/t-human", headers=stranger)
    unscoped = client.get("/api/colleagues/stream", headers=stranger)
    scoped = client.get("/api/colleagues/stream", params={"agent": "ward"}, headers=stranger)

    assert (listed.status_code, listed.json()) == (200, {"conversations": []})
    assert detail.status_code == 404
    assert unscoped.status_code == 400
    assert scoped.status_code == 404


def test_in_colleague_scope_keeps_only_rows_with_a_visible_participant():
    """Gate (visibility): In colleague scope keeps only rows with a visible participant.

    A caller who sees exactly `ward` sees rows where ward is the target or
    the calling agent, and no others. An operator's chat follows the agent it
    was with. `bool(scope)` would pass the stranger and owner tests above and
    fail this one."""
    from kobo_api.main import _in_colleague_scope

    scope = {"ward"}
    assert _in_colleague_scope({"target_agent": "ward", "caller_agent": "agent:other"}, scope)
    assert _in_colleague_scope({"target_agent": "other", "caller_agent": "agent:ward"}, scope)
    assert _in_colleague_scope({"target_agent": "ward", "caller_agent": "operator:p@example.com"}, scope)
    assert not _in_colleague_scope({"target_agent": "other", "caller_agent": "agent:third"}, scope)
    assert not _in_colleague_scope({"target_agent": "other", "caller_agent": "operator:p@example.com"}, scope)
    # The bare name never matches the prefixed form by accident.
    assert not _in_colleague_scope({"target_agent": "agent:ward", "caller_agent": "ward"}, scope)
    assert _in_colleague_scope({"target_agent": "anything", "caller_agent": ""}, None)


# --- from test_api_metadata.py ---


AGENT_NAME = "clio"


PERSONA = "helpful-generalist"


REPO__api_metadata = "acme/agent-clio"


SKILLS_BUCKET = "skills-bucket"


PERSONAS_BUCKET = "personas-bucket"


@pytest.fixture
def session_factory__api_metadata(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__api_metadata(tmp_path):
    return Config(
        workdir=tmp_path / "work", skills_bucket=SKILLS_BUCKET, personas_bucket=PERSONAS_BUCKET
    )


def _make_client__api_metadata(session_factory__api_metadata, config__api_metadata, *, github=None, name=None, storage=None, cabal=None):
    if storage is None:
        storage = FakeStorage()
        seed_default_personas(storage, bucket=PERSONAS_BUCKET)
    return TestClient(
        create_app(
            session_factory__api_metadata,
            config=config__api_metadata,
            clients=Clients(
                github=github if github is not None else FakeGitHub(),
                gojira=FakeGojira(),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=FakeKubectl(),
                storage=storage,
                name=name,
                cabal=cabal,
            ),
        )
    )


def _enabled_config__api_metadata(config__api_metadata):
    """`config` plus the two org keys `cabal_enabled` needs — the caller
    must also monkeypatch CABAL_OPERATOR_TOKEN, the third leg, before this
    reads as enabled (colleagues switchboard plan, Task 2's metadata
    tails)."""
    return config__api_metadata.model_copy(
        update={
            "cabal_url": "http://cabal.internal",
            "agent_endpoint_template": "http://agent-{name}.default.svc.cluster.local",
        }
    )


def _seed_agent__api_metadata(
    session_factory__api_metadata,
    config__api_metadata,
    *,
    status=AgentStatus.READY,
    repo_full_name=REPO,
    team="shared",
    purpose_tags=None,
    capability=None,
    skills=None,
    group_name=None,
):
    session = session_factory__api_metadata()
    session.add(
        Agent(
            name=AGENT_NAME,
            persona=PERSONA,
            model_id=config__api_metadata.models[0].id,
            status=status,
            repo_full_name=repo_full_name,
            team=team,
            purpose_tags=encode_tags(purpose_tags) if purpose_tags is not None else None,
            capability=capability,
            skills=skills or [],
            group_name=group_name,
        )
    )
    session.commit()
    session.close()


def _seed_colleague(session_factory__api_metadata, name: str, *, group_name: str | None) -> None:
    """A second, READY agent (never `AGENT_NAME`) for the roster-refresh
    tests below -- `_seed_agent` above always names its row `AGENT_NAME`,
    so a colleague needs its own row built directly."""
    session = session_factory__api_metadata()
    session.add(
        Agent(
            name=name,
            persona=PERSONA,
            model_id="vertex_ai/claude-sonnet-4-5",
            status=AgentStatus.READY,
            group_name=group_name,
        )
    )
    session.commit()
    session.close()


def _grouped_config(config__api_metadata):
    from kobo_api.config import GroupChoice

    return config__api_metadata.model_copy(
        update={
            "groups": [
                GroupChoice(name="marketing", okta_group="okta-marketing", kind="non-engineering"),
                GroupChoice(name="sre", okta_group="okta-sre"),
            ]
        }
    )


def _seed_neighbor(session_factory__api_metadata, config__api_metadata, *, name, group_name):
    session = session_factory__api_metadata()
    session.add(
        Agent(
            name=name,
            persona=PERSONA,
            model_id=config__api_metadata.models[0].id,
            status=AgentStatus.READY,
            repo_full_name=f"acme/agent-{name}",
            team="shared",
            group_name=group_name,
        )
    )
    session.commit()
    session.close()


def test_regenerate_metadata_redelivers_roster_and_self_md_without_cross_group_delivery(
    session_factory__api_metadata, config__api_metadata, monkeypatch
):
    """Gate (authorization): Regenerate metadata redelivers roster and self md without cross group delivery.

    The load-bearing negative test, at the regenerate route: a room with
    agents in two groups must not cross-deliver."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config__api_metadata(config__api_metadata)
    fake_cabal = FakeCabal(
        relay_result={"status": {"state": "completed", "message": {"parts": [{"text": "ok"}]}}}
    )
    fake_name = FakeName(capability="Freshly regenerated sentence.")
    client = _make_client__api_metadata(session_factory__api_metadata, cfg, name=fake_name, cabal=fake_cabal)
    _seed_agent__api_metadata(session_factory__api_metadata, cfg, capability="Stale sentence.", group_name="acme-acl")
    _seed_colleague(session_factory__api_metadata, "same-group", group_name="acme-acl")
    _seed_colleague(session_factory__api_metadata, "other-group", group_name="orbit-acl")

    response = client.post(f"/api/agents/{AGENT_NAME}/metadata/regenerate")

    assert response.status_code == 200
    filenames_by_target: dict[str, set[str]] = {}
    for call in fake_cabal.relay_calls:
        filenames_by_target.setdefault(call["name"], set()).add(
            call["task_envelope"]["metadata"]["filename"]
        )
    assert filenames_by_target.get("same-group") == {"colleagues.md"}
    assert "other-group" not in filenames_by_target
    assert filenames_by_target.get(AGENT_NAME) == {"self.md"}


def test_a_group_move_tells_the_switchboard_who_may_call_whom(
    session_factory__api_metadata, config__api_metadata, monkeypatch
):
    """Gate (authorization): A group move tells the switchboard who may call whom.

    Agents in one group may call each other. A move changes the group, so
    it changes that set, and the switchboard only knows what the room pushes.
    Before this, a moved agent kept its old neighbors and had none of its new
    ones until some unrelated event happened to trigger a sync."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _grouped_config(_enabled_config__api_metadata(config__api_metadata))
    fake_cabal = FakeCabal()
    client = _make_client__api_metadata(session_factory__api_metadata, cfg, cabal=fake_cabal)
    _seed_agent__api_metadata(session_factory__api_metadata, cfg, group_name="marketing")
    _seed_neighbor(session_factory__api_metadata, cfg, name="otto", group_name="sre")

    response = client.patch(f"/api/agents/{AGENT_NAME}/metadata", json={"group": "sre"})

    assert response.status_code == 200
    assert fake_cabal.put_dispatch_pairs_calls, "the move pushed nothing"
    pushed = fake_cabal.put_dispatch_pairs_calls[-1]
    assert (f"agent:{AGENT_NAME}", "otto") in pushed
    assert ("agent:otto", AGENT_NAME) in pushed


# --- from test_steps.py ---


AGENT_NAME = "clio"


PERSONA = "helpful-generalist"


MODEL_ID = "vertex_ai/claude-sonnet-4-5"


@pytest.fixture(autouse=True)
def _no_real_sleep__steps(monkeypatch):
    """Every OnboardStep test runs a poll loop; never let it sleep for real."""
    import kobo_api.steps as steps_module

    sleep_calls = []

    def fake_sleep(seconds):
        sleep_calls.append(seconds)

    fake_sleep.calls = sleep_calls
    monkeypatch.setattr(steps_module, "_sleep", fake_sleep)


def _make_agent(
    session_factory,
    *,
    name: str = AGENT_NAME,
    persona: str = PERSONA,
    model_id: str = MODEL_ID,
    skills: list | None = None,
    repo_full_name: str | None = None,
    gojira_app_id: int | None = None,
    team: str | None = None,
    group_name: str | None = None,
    purpose_tags: str | None = None,
    skip_baseline: bool = False,
    archetype: str | None = None,
):
    session = session_factory()
    agent = Agent(
        archetype=archetype,
        name=name,
        persona=persona,
        model_id=model_id,
        skills=skills or [],
        repo_full_name=repo_full_name,
        gojira_app_id=gojira_app_id,
        team=team,
        group_name=group_name,
        purpose_tags=purpose_tags,
        skip_baseline=skip_baseline,
    )
    session.add(agent)
    session.commit()
    return session, agent


def _make_ctx(session, agent, config, clients) -> Ctx:
    workdir = config.workdir / agent.name
    workdir.mkdir(parents=True, exist_ok=True)
    return Ctx(agent=agent, session=session, config=config, clients=clients, workdir=workdir)


def _clients(
    *,
    github=None,
    gojira=None,
    cloud=None,
    storage=None,
    agent=None,
    kubectl=None,
    name=None,
    cabal=None,
    image=None,
) -> Clients:
    return Clients(
        github=github,
        gojira=gojira,
        cloud=cloud,
        agent=agent,
        kubectl=kubectl,
        storage=storage,
        name=name,
        cabal=cabal,
        image=image,
    )


def _ready_agent(session, name, *, group_name=None, capability=None):
    agent = Agent(
        name=name,
        persona=PERSONA,
        model_id=MODEL_ID,
        skills=[],
        status=AgentStatus.READY,
        group_name=group_name,
        capability=capability,
    )
    session.add(agent)
    session.commit()
    return agent


def _make_invite(session, *, from_agent, to_agent, status=INVITE_ACCEPTED):
    invite = A2AInvite(from_agent=from_agent, to_agent=to_agent, status=status)
    session.add(invite)
    session.commit()
    return invite


def _repo_full_name(config) -> str:
    return f"{config.github_org}/agent-{AGENT_NAME}"


HELLO_CABAL_ENDPOINT_TEMPLATE = "http://agent-{name}.default.svc.cluster.local"


def _hello_cabal_enabled_config(config):
    return config.model_copy(
        update={
            "cabal_url": "http://cabal.internal",
            "agent_endpoint_template": HELLO_CABAL_ENDPOINT_TEMPLATE,
        }
    )


def test_colleague_roster_leaves_out_a_connected_agent(session_factory, config):
    """Gate (visibility): Colleague roster leaves out a connected agent.

    A connected agent is nobody's colleague (connected-agents design
    2026-09-21): the room holds no inbound route to it, so naming it in a
    roster promises a hop that would fail. Same rule `allowed_pairs` keeps
    on the wire, kept here on the page the agent itself reads."""
    session, agent = _make_agent(session_factory, group_name="acme-acl")
    connected = Agent(
        name="ward",
        persona="connected",
        model_id=MODEL_ID,
        skills=[],
        status=AgentStatus.READY,
        group_name="acme-acl",
        kind=KIND_CONNECTED,
    )
    session.add(connected)
    session.commit()

    assert _colleague_roster(session, agent) == []


def test_agents_who_can_see_cross_group_without_invite_is_empty(session_factory):
    """Gate (visibility): Agents who can see cross group without invite is empty.

    The load-bearing negative half: two different (non-shared) groups,
    no invite between them -- the reversed rule must find nobody."""
    session, target = _make_agent(session_factory, name="target", group_name="acme-acl")
    target.status = AgentStatus.READY
    session.commit()
    _ready_agent(session, "stranger", group_name="orbit-acl")

    result = _agents_who_can_see(session, name=target.name, group_name=target.group_name)

    assert result == []


def test_agents_who_can_see_reverse_invite_direction_grants_nothing(session_factory):
    """Gate (visibility): Agents who can see reverse invite direction grants nothing.

    The other direction of the same invite (target invited someone else)
    must not grant that someone else visibility of the target -- only an
    invite the VIEWER itself sent does."""
    session, target = _make_agent(session_factory, name="target", group_name="acme-acl")
    target.status = AgentStatus.READY
    session.commit()
    _ready_agent(session, "someone", group_name="orbit-acl")
    _make_invite(session, from_agent="target", to_agent="someone", status=INVITE_ACCEPTED)

    result = _agents_who_can_see(session, name=target.name, group_name=target.group_name)

    assert result == []


def test_refresh_colleague_rosters_does_not_cross_deliver_between_two_groups(
    session_factory, config, monkeypatch
):
    """Gate (visibility): Refresh colleague rosters does not cross deliver between two groups.

    Global Constraints' load-bearing negative test: a room with agents in
    two groups must not cross-deliver. A same-group viewer gets the
    refresh; a viewer in an unrelated group, with no invite, never does."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _hello_cabal_enabled_config(config)
    session, target = _make_agent(session_factory, name="target", group_name="acme-acl")
    target.status = AgentStatus.READY
    session.commit()
    _ready_agent(session, "same-group", group_name="acme-acl")
    _ready_agent(session, "other-group", group_name="orbit-acl")
    fake_cabal = FakeCabal(
        relay_result={"status": {"state": "completed", "message": {"parts": [{"text": "ok"}]}}}
    )
    clients = _clients(cabal=fake_cabal)

    refresh_colleague_rosters(
        clients, cfg, session, name=target.name, group_name=target.group_name
    )

    delivered_to = [call["name"] for call in fake_cabal.relay_calls]
    assert delivered_to == ["same-group"]
    assert "other-group" not in delivered_to


def test_access_step_skips_the_vertex_grant_when_the_gateway_is_on(session_factory, config):
    """Gate (authorization, kobo#390): with the room's model gateway on the
    agent reaches models through the gateway's identity, so its own identity
    is made and bound but granted nothing for models, on either road."""
    cfg = config.model_copy(update={"models_gateway_url": "http://models-gateway.models.svc"})
    session, agent = _make_agent(session_factory, name="rae")
    cloud = FakeCloud(project=cfg.gcp_project)

    detail = AccessStep().run(_make_ctx(session, agent, cfg, _clients(cloud=cloud)))

    assert [c.split(":")[0] for c in cloud.calls] == ["ensure_gsa", "bind_workload_identity"]
    assert detail == "model access through the room's model gateway"


def test_access_step_grants_a_new_agent_read_on_its_own_repository(session_factory, config):
    """Gate (authorization, owner ruling 2026-11-02): with the permission service on and
    GitHub connected, the Access step writes the agent's repository set as its one
    repository and approves Read on it, once; a second run adds nothing, a set already
    picked stands, and a room without GitHub says so in the sentence and grants nothing."""
    from kobo_api.models import AuditLog

    session, agent = _make_agent(session_factory, name="rae", repo_full_name="example-org/agent-rae")
    bab = FakeBab(integrations=[dict(GITHUB_CONNECTION)])
    clients = _clients(cloud=FakeCloud(project=config.gcp_project))
    clients.bab = bab
    ctx = _make_ctx(session, agent, config, clients)

    detail = AccessStep().run(ctx)

    assert detail.endswith("; reads its own repository on GitHub")
    child = next(i for i in bab.integrations("x") if i["integration"] == "github-rae")
    assert child["parent"] == "github" and child["spec"]["repositories"] == ["example-org/agent-rae"]
    grants = [g for g in bab.grants("rae", "x") if g["integration"] == "github-rae"]
    assert [g["permission"] for g in grants] == ["github_repos_read"] and grants[0]["state"] == "active"
    rows = session.query(AuditLog).filter_by(action="access.default").all()
    assert len(rows) == 1 and "Read on its own repository example-org/agent-rae" in rows[0].summary

    again = AccessStep().run(ctx)
    assert "repository" not in again
    assert len([g for g in bab.grants("rae", "x") if g["integration"] == "github-rae"]) == 1
    session.commit()

    session2, other = _make_agent(session_factory, name="ivy", repo_full_name="example-org/agent-ivy")
    clients2 = _clients(cloud=FakeCloud(project=config.gcp_project))
    clients2.bab = FakeBab(integrations=[])
    assert AccessStep().run(_make_ctx(session2, other, config, clients2)).endswith(
        "; GitHub is not connected, so its own repository is not granted")
    assert clients2.bab.grants("ivy", "x") == []


def test_access_step_grants_prefix_scoped_to_this_agents_own_name(session_factory, config):
    """Gate (visibility): Access step grants prefix scoped to this agents own name.

    Two different agents against the SAME configured bucket must be
    granted two DIFFERENT prefixes -- never a bucket-wide grant either
    agent's identity could use to reach the other's replica."""
    cfg = config.model_copy(update={"memory_replica_bucket": "kobo-memory-replicas"})
    session, agent = _make_agent(session_factory, name="rae")
    cloud = FakeCloud(project=cfg.gcp_project)
    ctx = _make_ctx(session, agent, cfg, _clients(cloud=cloud))

    AccessStep().run(ctx)

    expected_email = f"agent-rae@{cfg.gcp_project}.iam.gserviceaccount.com"
    # The write grant stays prefix-scoped to this agent's own name; the
    # trailing list grant (Important 9 fix, below) is bucket-wide by
    # design (list is not scopable by object-name condition), so it names
    # only the bucket, never the agent's prefix.
    assert cloud.calls[-2] == f"grant_bucket_prefix_write:{expected_email}:kobo-memory-replicas:rae/"
    assert cloud.calls[-1] == f"grant_bucket_list:{expected_email}:kobo-memory-replicas"


def test_onboard_step_conflict_for_someone_elses_app_is_still_refused(session_factory, config):
    """Gate (visibility): Onboard step conflict for someone elses app is still refused."""
    repo = _repo_full_name(config)
    gojira = FakeGojira()
    gojira.create_app(name="other", repo_full_name=repo, cluster=config.gojira_cluster, namespace="other")
    gojira._conflict_repos.add(repo)
    session, agent = _make_agent(session_factory, repo_full_name=repo, gojira_app_id=None)
    ctx = _make_ctx(session, agent, config, _clients(gojira=gojira))

    with pytest.raises(RuntimeError, match="no app id is recorded"):
        OnboardStep().run(ctx)
    assert agent.gojira_app_id is None


# --- from test_engine.py ---


JOIN_TIMEOUT = 5


@dataclass
class RecordingStep:
    """Appends `name` to a shared `calls` list on every `run()`. Raises
    `fail_times` times (decrementing) before returning `detail`, so a test can
    force a failure and then a successful retry after `reset_failed_steps`.
    """

    name: str
    calls: list[str]
    fail_times: int = 0
    detail: str = "ok"

    def run(self, ctx) -> str:
        self.calls.append(self.name)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError(f"{self.name} boom")
        return self.detail


def _load(session_factory, agent_id: int):
    """Fetch the agent and its steps (by name) from a fresh session, then close it."""
    session = session_factory()
    agent = session.get(Agent, agent_id)
    rows = {s.name: s for s in agent.steps}
    session.close()
    return agent, rows


def _make_agent_with_group(session_factory, name: str, group_name: str) -> int:
    session = session_factory()
    agent = Agent(
        name=name,
        persona="helpful-generalist",
        model_id="vertex_ai/claude-sonnet-4-5",
        group_name=group_name,
    )
    session.add(agent)
    session.commit()
    agent_id = agent.id
    session.close()
    return agent_id


def _seed_ready_agent(session_factory, name: str, group_name: str) -> None:
    """A colleague already READY before the pipeline under test runs --
    this is the other half of the same-group pair a READY flip below
    should join."""
    session = session_factory()
    session.add(
        Agent(
            name=name,
            persona="helpful-generalist",
            model_id="vertex_ai/claude-sonnet-4-5",
            status=AgentStatus.READY,
            group_name=group_name,
        )
    )
    session.commit()
    session.close()


def _cabal_enabled_config(config):
    return config.model_copy(
        update={
            "cabal_url": "http://cabal.internal",
            "agent_endpoint_template": "http://agent-{name}.default.svc.cluster.local",
        }
    )


def test_ready_flip_syncs_dispatch_pairs_when_cabal_enabled(session_factory, config, monkeypatch):
    """Gate (authorization): Ready flip syncs dispatch pairs when cabal enabled.

    A brand-new agent flipping to READY joins its group's implicit
    pairs -- the switchboard push right after the flip must carry both
    directions of that pair, computed fresh off the DB, not anything this
    test hands the pipeline directly."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _cabal_enabled_config(config)
    _seed_ready_agent(session_factory, "b", "growth")
    agent_id = _make_agent_with_group(session_factory, "a", "growth")
    fake_cabal = FakeCabal()
    clients = Clients(
        github=None, gojira=None, cloud=None, agent=None, kubectl=None, cabal=fake_cabal
    )

    thread = run_pipeline(
        session_factory,
        agent_id,
        config=cfg,
        clients=clients,
        steps=[RecordingStep(name="step-a", calls=[])],
    )
    thread.join(timeout=JOIN_TIMEOUT)
    assert thread.is_alive() is False

    assert fake_cabal.put_dispatch_pairs_calls == [
        sorted({("agent:a", "b"), ("agent:b", "a")})
    ]


def test_ready_flip_completes_even_when_dispatch_pairs_sync_fails(
    session_factory, config, monkeypatch
):
    """Gate (authorization): Ready flip completes even when dispatch pairs sync fails.

    Fail-soft: a CabalError from put_dispatch_pairs must never leave the
    agent stuck out of READY or crash the pipeline thread."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _cabal_enabled_config(config)
    agent_id = _make_agent_with_group(session_factory, "a", "growth")
    fake_cabal = FakeCabal(put_dispatch_pairs_error="switchboard unreachable")
    clients = Clients(
        github=None, gojira=None, cloud=None, agent=None, kubectl=None, cabal=fake_cabal
    )

    thread = run_pipeline(
        session_factory,
        agent_id,
        config=cfg,
        clients=clients,
        steps=[RecordingStep(name="step-a", calls=[])],
    )
    thread.join(timeout=JOIN_TIMEOUT)
    assert thread.is_alive() is False

    agent, _ = _load(session_factory, agent_id)
    assert agent.status == AgentStatus.READY


# --- from test_groups.py ---


DOMAIN = "acme.okta.com"


TOKEN = "SUPER_SECRET_OKTA_TOKEN_9f3a"


UNIVERSE = {"SRE": ["okta-sre-group"], "QA": ["okta-qa-group"]}


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_empty_memberships_and_failed_lookup_are_distinguishable():
    """Gate (visibility): Empty memberships and failed lookup are distinguishable.

    A successful call with zero matching memberships is `[]`; a failed
    call is `None` — these must never be the same value, since callers
    branch on `is None` to decide whether to grant nothing at all."""
    ok_response = httpx.Response(200, json=[])
    ok_resolver = OktaGroupResolver(DOMAIN, TOKEN, UNIVERSE, client=_client(lambda r: ok_response))
    assert ok_resolver.groups_for("alice@example.com") == []

    failing_response = httpx.Response(500, text="internal error")
    failing_resolver = OktaGroupResolver(
        DOMAIN, TOKEN, UNIVERSE, client=_client(lambda r: failing_response)
    )
    result = failing_resolver.groups_for("alice@example.com")
    assert result is None
    assert result != []


# --- from test_api_org_status.py ---


WHOAMI_AUDIENCE = "test-audience"


# --- from test_api_org_status.py ---


def _fetcher__api_org_status(jwk):
    def fetch():
        return {"keys": [jwk]}

    return fetch


# --- from test_api_org_status.py ---


WHOAMI_KID = "test-key-1"


def _generate_jwk__api_org_status(kid: str = WHOAMI_KID):
    private_key = ec.generate_private_key(ec.SECP256R1())
    jwk = jwt.algorithms.ECAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk["kid"] = kid
    jwk["use"] = "sig"
    jwk["alg"] = "ES256"
    return private_key, jwk


# --- from test_api_org_status.py ---


def _sign_whoami(private_key, *, email: str | None, kid: str = WHOAMI_KID) -> str:
    now = int(time.time())
    claims = {
        "aud": WHOAMI_AUDIENCE,
        "iss": IAP_ISSUER,
        "iat": now,
        "exp": now + 3600,
        "sub": "accounts.google.com:1234567890",
    }
    if email is not None:
        claims["email"] = email
    return jwt.encode(claims, private_key, algorithm="ES256", headers={"kid": kid})


# --- from test_api_access.py ---


def _room(tmp_path, fake_storage, *, bab, github=None, **config_over):
    tmp_path.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    config = Config(workdir=tmp_path / "work", iap_audience=WHOAMI_AUDIENCE, operators=["ops@example.com"],
                    admin_panel=True, bab_url="http://bab.bab.svc:8080" if bab is not None else "",
                    cloud_accounts=[{"vendor": "aws", "name": "aws-prod"}, {"vendor": "aws", "name": "acme-data"},
                                    {"vendor": "gcp", "name": "acme-platform-prod"}], **config_over)
    private_key, jwk = _generate_jwk__api_org_status()
    clients = Clients(github=github or FakeGitHub(), gojira=FakeGojira(), cloud=FakeCloud(), agent=FakeAgent(),
                      kubectl=FakeKubectl(), storage=fake_storage)
    clients.bab = bab
    app = create_app(session_factory, config=config, clients=clients, iap_jwks_fetcher=_fetcher__api_org_status(jwk))
    with session_factory() as s:
        s.add(Agent(name="ledger", persona="helpful-generalist", model_id="vertex_ai/claude-sonnet-4-5", skills=[],
                    status=AgentStatus.READY, repo_full_name="example-org/agent-ledger", team="shared",
                    created_by="ops@example.com"))
        s.commit()
    return TestClient(app), {ASSERTION_HEADER: _sign_whoami(private_key, email="ops@example.com")}


def test_ask_review_hold_revoke_and_the_topology_push(tmp_path, fake_storage):
    """Gate (authorization): Ask review hold revoke and the topology push."""
    bab = FakeBab()
    c, h = _room(tmp_path, fake_storage, bab=bab)

    # The picker: vendor, then account (connected or listed but not yet), then
    # the vendor's permissions grouped its way with the tier as a column.
    vendors = {v["id"]: v for v in c.get("/api/access/picker", headers=h).json()["vendors"]}
    aws, dd = vendors["aws"], vendors["datadog"]
    # Vendors come from the permission service's catalogue alone (#301): a cloud the
    # old org file named but nothing is connected to is not a vendor here.
    assert "gcp" not in vendors
    assert (aws["kind"], aws["label"], aws["account_noun"]) == ("cloud", "AWS", "account")
    # The vendor says whether the room is connected to it at all (kobo#371): a cloud
    # with one connected account is; a service with no connection is not.
    assert aws["connected"] is True and dd["connected"] is False
    # Connected accounts only (#301): the org file's other names are not offered.
    assert [(a["id"], a["connected"]) for a in aws["accounts"]] == [("aws-prod", True)]
    assert aws["accounts"][0]["ceiling"] == ["aws_cloudtrail_lake_query", "aws_inventory_read"]
    flat = {p["name"]: p for g in aws["groups"] for p in g["permissions"]}
    assert flat["aws_ecs_update_service"]["tier"] == "mutate"
    # A service vendor is one connection; none connected yet shows a placeholder.
    assert dd["kind"] == "service" and dd["accounts"] == [{"id": "datadog", "name": "Datadog", "connected": False, "ceiling": [],
                                                           "organization": None, "account_id": None,
                                                           "repositories": [], "has_repo_set": False}]

    # The owner asks; the request is pending on the agent page and in Review, with the owner named.
    r = c.post("/api/agents/ledger/access/requests", headers=h,
               json={"integration": "aws-prod", "permissions": ["aws_cloudtrail_lake_query"], "note": "audit lane"})
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    page = c.get("/api/agents/ledger/access", headers=h).json()
    assert page["grants"] == [] and [p["id"] for p in page["pending"]] == [rid]
    assert page["can_request"] is True and page["can_decide"] is True
    (row,) = c.get("/api/admin/access/requests", headers=h).json()["requests"]
    assert (row["id"], row["owner"], row["team"], row["requested_by"]) == (rid, "ops@example.com", "shared", "ops@example.com")

    # A denial needs a reason; an approval turns the request into grants.
    assert c.post("/api/admin/access/decide", headers=h, json={"ids": [rid], "verdict": "deny"}).status_code == 422
    out = c.post("/api/admin/access/decide", headers=h, json={"ids": [rid], "verdict": "approve"}).json()["results"]
    assert out[0]["status"] == "approved" and out[0]["outcome"] == {"aws_cloudtrail_lake_query": "granted"}
    page = c.get("/api/agents/ledger/access", headers=h).json()
    (grant,) = page["grants"]
    assert (grant["permission"], grant["expires_at"], grant["granted_by"]) == ("aws_cloudtrail_lake_query", None, "ops@example.com")
    assert page["pending"] == []

    # Revoke now is an admin's; leases come from bab.
    assert c.delete(f"/api/agents/ledger/access/grants/{grant['id']}", headers=h).json() == {"revoked": grant["id"]}
    assert bab.revoked == [(grant["id"], "ops@example.com")]
    assert c.get("/api/agents/ledger/leases", headers=h).json() == {"available": True, "leases": []}

    # Borrowed now, room-wide: only leases whose hour has not run out.
    bab._leases.extend([
        {"id": 1, "agent": "ledger", "integration": "aws-prod", "permissions": ["aws_cloudtrail_lake_query"], "task": "audit",
         "minter": "aws", "issued_at": "2099-01-01T00:00:00+00:00", "expires_at": "2099-01-01T01:00:00+00:00"},
        {"id": 2, "agent": "ledger", "integration": "aws-prod", "permissions": ["aws_inventory_read"], "task": "old",
         "minter": "aws", "issued_at": "2020-01-01T00:00:00+00:00", "expires_at": "2020-01-01T01:00:00+00:00"},
    ])
    assert [lease["id"] for lease in c.get("/api/admin/access/leases", headers=h).json()["leases"]] == [1]

    # The room tells bab which agents exist, with the pod identity that is each one.
    sync = c.post("/api/admin/access/sync", headers=h).json()
    assert sync["agents"] == 1
    actor, agents = bab.topologies[-1]
    assert actor == "ops@example.com"
    assert agents == [{"name": "ledger", "team": "shared", "namespace": "ledger", "service_account": "agent-ledger"}]


def test_access_is_off_without_bab(tmp_path, fake_storage):
    """Gate (authorization): Access is off without bab."""
    c, h = _room(tmp_path, fake_storage, bab=None)
    assert c.get("/api/access/picker", headers=h).status_code == 404
    assert c.get("/api/agents/ledger/access", headers=h).status_code == 404
    assert c.get("/api/admin/access/requests", headers=h).status_code == 404
    assert c.get("/api/agents/ledger/leases", headers=h).json()["available"] is False
    cards = {a["id"]: a for a in c.get("/api/admin/addons", headers=h).json()["addons"]}
    assert cards["access"]["state"] == "off"


# --- from test_admin_store.py ---


@pytest.fixture
def session__admin_store(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/kobo.db")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    s = factory()
    yield s
    s.close()


def test_connect_an_integration_then_a_grant_no_wider_than_its_base_access(session__admin_store):
    """Gate (authorization): Connect an integration then a grant no wider than its base access."""
    row = admin_store.put_integration(
        session__admin_store, vendor="Datadog", name="Datadog", base_access="read",
        spec={"address": "https://mcp.datadoghq.com"}, credential_ref="apps/kobo/integrations/datadog",
        actor="kyle@example.com",
    )
    session__admin_store.commit()
    assert row.id == "datadog" and row.state == "pending"

    grant = admin_store.put_grant(session__admin_store, agent_name="sage", integration_id="datadog", access="read", actor="kyle@example.com")
    session__admin_store.commit()
    assert grant.state == "pending"

    with pytest.raises(admin_store.AdminStoreError, match="read-only"):
        admin_store.put_grant(session__admin_store, agent_name="sage", integration_id="datadog", access="write", actor="kyle@example.com")

    actions = [e.action for e in admin_store.audit_events(session__admin_store)]
    assert actions == ["grant.create", "integration.connect"]


def test_disconnect_takes_the_grants_with_it(session__admin_store):
    """Gate (authorization): Disconnect takes the grants with it."""
    admin_store.put_integration(session__admin_store, vendor="Jira", name="Jira", base_access="read", spec={}, credential_ref=None, actor="a@example.com")
    admin_store.put_grant(session__admin_store, agent_name="sage", integration_id="jira", access="read", actor="a@example.com")
    session__admin_store.commit()

    admin_store.remove_integration(session__admin_store, "jira", actor="a@example.com")
    session__admin_store.commit()

    assert admin_store.list_integrations(session__admin_store) == []
    assert admin_store.list_grants(session__admin_store) == []
    assert admin_store.audit_events(session__admin_store)[0].action == "integration.disconnect"


def test_a_builtin_role_cannot_be_deleted(session__admin_store):
    """Gate (authorization): A builtin role cannot be deleted."""
    admin_store.put_role(session__admin_store, name="Admin", description="", groups=["platform-admins"], spec={}, actor="a@example.com", builtin=True)
    session__admin_store.commit()
    with pytest.raises(admin_store.AdminStoreError, match="built-in"):
        admin_store.delete_role(session__admin_store, "admin", actor="a@example.com")
    admin_store.put_role(session__admin_store, name="Temp", description="", groups=[], spec={}, actor="a@example.com")
    session__admin_store.commit()
    admin_store.delete_role(session__admin_store, "temp", actor="a@example.com")
    session__admin_store.commit()
    assert [r.id for r in admin_store.list_roles(session__admin_store)] == ["admin"]


# --- from test_api_admin_connections.py ---


GATEWAY_REPO = "example-org/gateway-config"


class FakeLiminalAdmin:
    def __init__(self, grants: list[GatewayGrant] | None = None, *, fail: str | None = None,
                 targets_unsupported: bool = False, refuse_target: str | None = None):
        self.grants = {(g.issuer, g.persona): g for g in (grants or [])}
        self.writes: list[GatewayGrant] = []
        self.revokes: list[tuple[str, str]] = []
        self._fail = fail
        # Targets through the admin plane (liminal 0.9.0, liminal#222): what the gateway
        # holds live, and every call in order. `targets_unsupported` is an older gateway
        # without the routes; `refuse_target` is the 409/422 sentence a PUT gets.
        self.targets: dict[str, dict] = {}
        self.target_calls: list[tuple[str, str]] = []
        self._targets_unsupported = targets_unsupported
        self._refuse_target = refuse_target

    def list_targets(self) -> list[dict]:
        self.target_calls.append(("list", ""))
        return [{"name": n, "source": "live", "status": "ready", "spec": dict(s)} for n, s in self.targets.items()]

    def put_target(self, name: str, spec: dict) -> dict:
        self.target_calls.append(("put", name))
        if self._targets_unsupported:
            raise ClientError(TARGETS_UNSUPPORTED)
        if self._refuse_target:
            raise ClientError(self._refuse_target)
        self.targets[name] = dict(spec)
        return {"name": name, "status": "ready"}

    def delete_target(self, name: str) -> dict:
        self.target_calls.append(("delete", name))
        if self._targets_unsupported:
            raise ClientError(TARGETS_UNSUPPORTED)
        if name not in self.targets:
            raise TargetNotFound(f"no live target {name!r}")
        del self.targets[name]
        return {"removed": name}

    def list_grants(self) -> list[GatewayGrant]:
        if self._fail:
            raise ClientError(self._fail)
        return list(self.grants.values())

    def grant(self, grant: GatewayGrant) -> None:
        if self._fail:
            raise ClientError(self._fail)
        self.writes.append(grant)
        self.grants[(grant.issuer, grant.persona)] = grant

    def revoke(self, issuer: str, persona: str) -> None:
        self.revokes.append((issuer, persona))
        self.grants.pop((issuer, persona), None)


def _client__api_admin_connections(tmp_path, fake_storage, *, github: FakeGitHub, admin, config_overrides=None):
    engine = create_engine(f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    values = {
        "workdir": tmp_path / "work", "iap_audience": WHOAMI_AUDIENCE, "operators": ["ops@example.com"],
        "admin_panel": True, "sero_url": "http://sero.internal", "gateway_config_repo": GATEWAY_REPO,
        "gateway_namespace": "liminal", "liminal_admin_url": "http://liminal-admin.liminal.svc",
    }
    values.update(config_overrides or {})
    private_key, jwk = _generate_jwk__api_org_status()
    clients = Clients(github=github, gojira=FakeGojira(), cloud=FakeCloud(), agent=FakeAgent(),
                      kubectl=FakeKubectl(), storage=fake_storage, sero=FakeSero(), cabal=FakeCabal())
    clients.liminal_admin = admin
    app = create_app(session_factory, config=Config(**values), clients=clients, iap_jwks_fetcher=_fetcher__api_org_status(jwk))
    with session_factory() as s:
        for name, group in (("janus", "platform"), ("sage", "data")):
            s.add(Agent(name=name, persona="helpful-generalist", model_id="vertex_ai/claude-sonnet-4-5", skills=[],
                        status=AgentStatus.READY, repo_full_name=f"example-org/agent-{name}", group_name=group))
        s.commit()
    return TestClient(app), {ASSERTION_HEADER: _sign_whoami(private_key, email="ops@example.com")}


def _connect_datadog(client, headers, base_access="read"):
    r = client.post("/api/admin/integrations", json={"vendor": "datadog", "name": "Datadog", "base_access": base_access,
                    "credential": "dd-token", "address": "https://mcp.datadoghq.com", "trust": "untrusted"}, headers=headers)
    assert r.status_code == 201, r.text


def test_a_write_lane_is_never_wider_than_the_integration(tmp_path, fake_storage):
    """Gate (authorization): A write lane is never wider than the integration."""
    github = FakeGitHub(existing_repos={GATEWAY_REPO}, extra_clone_files={"grants.toml": ""})
    client, headers = _client__api_admin_connections(tmp_path, fake_storage, github=github, admin=FakeLiminalAdmin())
    _connect_datadog(client, headers, base_access="read")

    r = client.put("/api/admin/connections/sage/datadog", json={"access": "write"}, headers=headers)

    assert r.status_code == 409 and "read-only" in r.json()["error"]


def test_revoke_removes_the_lane_live_and_from_the_file(tmp_path, fake_storage):
    """Gate (authorization): Revoke removes the lane live and from the file."""
    github = FakeGitHub(existing_repos={GATEWAY_REPO}, extra_clone_files={"grants.toml": ""}, track_pushed_content=True)
    admin = FakeLiminalAdmin()
    client, headers = _client__api_admin_connections(tmp_path, fake_storage, github=github, admin=admin)
    _connect_datadog(client, headers)
    client.put("/api/admin/connections/sage/datadog", json={"access": "read"}, headers=headers)

    r = client.delete("/api/admin/connections/sage/datadog", headers=headers)

    assert r.status_code == 200 and r.json()["state"] == "revoked"
    assert admin.revokes == [("cabal-core", "sage")]
    assert 'persona = "sage"' not in github.pushed_file("grants.toml")


# --- from test_api_admin_integrations.py ---


GATEWAY_REPO = "example-org/gateway-config"


def _client__api_admin_integrations(tmp_path, fake_storage, config: Config, *, github: FakeGitHub, sero: FakeSero | None):
    engine = create_engine(f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    private_key, jwk = _generate_jwk__api_org_status()
    client = TestClient(
        create_app(
            session_factory,
            config=config,
            clients=Clients(
                github=github, gojira=FakeGojira(), cloud=FakeCloud(), agent=FakeAgent(),
                kubectl=FakeKubectl(), storage=fake_storage, sero=sero,
            ),
            iap_jwks_fetcher=_fetcher__api_org_status(jwk),
        )
    )
    return client, {ASSERTION_HEADER: _sign_whoami(private_key, email="ops@example.com")}


def _config(tmp_path, **overrides) -> Config:
    values = {
        "workdir": tmp_path / "work", "iap_audience": WHOAMI_AUDIENCE, "operators": ["ops@example.com"],
        "admin_panel": True, "sero_url": "http://sero.internal", "gateway_config_repo": GATEWAY_REPO,
        "gateway_namespace": "liminal",
    }
    values.update(overrides)
    return Config(**values)


class FakeAwsAdmin:
    """The gateway's admin plane as the AWS flow sees it: who the gateway is,
    and whether AWS lets it assume a role."""

    def __init__(self, *, refuse: str | None = None, identity_error: str | None = None):
        self.probes: list[tuple[str, str]] = []
        self._refuse = refuse
        self._identity_error = identity_error

    def aws_identity(self, audience: str) -> dict[str, str]:
        if self._identity_error:
            raise ClientError(self._identity_error)
        return {"issuer": "https://accounts.google.com", "subject": "1234567890", "email": "gw@p.iam.gserviceaccount.com", "audience": audience}

    def aws_probe(self, role_arn: str, audience: str) -> dict[str, str]:
        self.probes.append((role_arn, audience))
        if self._refuse:
            raise ClientError(self._refuse)
        return {"account": role_arn.split(":")[4], "arn": f"arn:aws:sts::{role_arn.split(':')[4]}:assumed-role/KoboAccess-Role/probe", "role_arn": role_arn}

    def list_grants(self):
        return []


REGISTRY = 'service_audience = "liminal"\n\n[tool_extras]\n\n[tool_extras."gateway.clusters"."dev/a"]\nproject_number = "1"\n'


AWS_ROLE = "arn:aws:iam::123456789012:role/KoboAccess-Role"


def _access_client(tmp_path, fake_storage, *, github=None, bab=None):
    client, headers = _client__api_admin_integrations(tmp_path, fake_storage, _config(tmp_path, bab_url="http://bab.bab.svc:8080"),
                              github=github or FakeGitHub(existing_repos={GATEWAY_REPO}, extra_clone_files={"registry.toml": REGISTRY}, track_pushed_content=True),
                              sero=FakeSero())
    client.app.state.clients.bab = bab or FakeBab(integrations=[])
    return client, headers


HUB = "arn:aws:iam::999999999999:role/KoboAccess-Hub"


def test_a_role_aws_refuses_is_not_written_anywhere(tmp_path, fake_storage):
    """Gate (authorization): A role aws refuses is not written anywhere."""
    github = FakeGitHub(existing_repos={GATEWAY_REPO}, extra_clone_files={"registry.toml": REGISTRY}, track_pushed_content=True)
    client, headers = _client__api_admin_integrations(tmp_path, fake_storage, _config(tmp_path), github=github, sero=FakeSero())
    client.app.state.clients.liminal_admin = FakeAwsAdmin(refuse="AWS refused the role: AccessDenied Not authorized to perform sts:AssumeRoleWithWebIdentity")
    body = {"vendor": "aws", "name": "AWS", "base_access": "read", "auth": "aws_role", "identity": {"role_arn": AWS_ROLE}}
    response = client.post("/api/admin/integrations", json=body, headers=headers)
    assert response.status_code == 409
    assert "AccessDenied" in response.json()["error"] and "connect again" in response.json()["error"]
    assert client.get("/api/admin/integrations", headers=headers).json()["integrations"] == []
    assert not github._pushed_trees, "nothing was pushed to the gateway repo"


def test_with_access_on_an_aws_role_is_probed_by_bab_and_connected_there_not_on_the_gateway(tmp_path, fake_storage):
    """Gate (authorization): With access on an aws role is probed by bab and connected there not on the gateway."""
    bab = FakeBab(integrations=[])
    github = FakeGitHub(existing_repos={GATEWAY_REPO}, extra_clone_files={"registry.toml": REGISTRY}, track_pushed_content=True)
    client, headers = _access_client(tmp_path, fake_storage, github=github, bab=bab)
    body = {"vendor": "aws", "name": "acme-audit", "base_access": "read", "auth": "aws_role", "identity": {"role_arn": AWS_ROLE}}
    row = client.post("/api/admin/integrations", json=body, headers=headers).json()
    assert bab.probes == [(AWS_ROLE, "sts.amazonaws.com")]
    assert row["state"] == "applied" and "the permission service assumed the role" in row["state_detail"]
    assert "connected in the permission service" in row["state_detail"]
    (integ,) = bab.integrations("x")
    assert integ["integration"] == row["id"] and integ["vendor"] == "aws"
    assert integ["spec"] == {"role_arn": AWS_ROLE, "audience": "sts.amazonaws.com", "name": "acme-audit"}
    # The ceiling is every read permission the catalogue offers for the vendor.
    assert integ["ceiling"] == ["aws_cloudtrail_lake_query", "aws_inventory_read"]
    assert not github._pushed_trees, "the gateway has nothing to learn: leases come from the permission service"
    # A refused role is saved nowhere.
    r = client.post("/api/admin/integrations", json={**body, "name": "bad", "identity": {"role_arn": "arn:aws:iam::123456789012:role/refuse-me"}}, headers=headers)
    assert r.status_code == 409 and "AccessDenied" in r.json()["error"]
    assert len(bab.integrations("x")) == 1
    # Disconnect takes it out of the permission service too.
    out = client.delete(f"/api/admin/integrations/{row['id']}", headers=headers).json()
    assert out["access"].startswith("removed")
    assert bab.integrations("x") == []


def test_an_aws_account_connected_for_its_bedrock_models_only_has_no_ceiling_and_is_never_offered(tmp_path, fake_storage):
    """Gate (authorization, owner ruling 2026-11-02): the models-only shape of the AWS sheet
    is a role with the Bedrock permissions and nothing else. Its setup page is a plain role
    with the trust policy and the Bedrock policy (no module, no template); connecting it
    puts the Bedrock models on the row and the row into the permission service with an
    empty ceiling; and the Access picker leaves it out, because nothing could be granted
    there, while the row still stands under Integrations."""
    bab = FakeBab(integrations=[])
    client, headers = _access_client(tmp_path, fake_storage, bab=bab)

    page = client.get("/api/admin/integrations/aws/setup?mode=models&integration=acme-models", headers=headers).json()
    assert page["mode"] == "models" and page["role_name"] == "KoboModels-Bedrock" and page["chosen"] == []
    assert 'resource "aws_iam_role" "kobo_models"' in page["terraform"] and 'resource "aws_iam_role_policy" "kobo_bedrock"' in page["terraform"]
    assert "module " not in page["terraform"] and 'output "kobo_role_arn"' in page["terraform"]
    assert page["bedrock_policy"] and page["cloudformation"] == {"template": "", "parameters": {}}
    role_page = client.get("/api/admin/integrations/aws/setup?mode=role&integration=acme-models", headers=headers).json()
    assert page["trust_policy"] == role_page["trust_policy"]

    body = {"vendor": "aws", "name": "acme-models", "base_access": "read", "auth": "aws_role",
            "identity": {"role_arn": AWS_ROLE, "models_only": "1", "models_region": "us-east-1"}}
    row = client.post("/api/admin/integrations", json=body, headers=headers).json()
    assert row["state"] == "applied", row
    assert row["spec"]["models"] == {"provider": "bedrock", "region": "us-east-1", "role_arn": AWS_ROLE}
    assert row["spec"]["models_only"] is True
    assert row["spec"]["identity"] == {"role_arn": AWS_ROLE, "audience": "sts.amazonaws.com"}
    assert "nothing to grant" in row["state_detail"] and "Bedrock models only" in row["state_detail"]
    (integ,) = bab.integrations("x")
    assert integ["integration"] == "acme-models" and integ["ceiling"] == []

    # A second account, connected the ordinary way, is offered; the models-only one is not.
    client.post("/api/admin/integrations", json={**body, "name": "acme-audit", "identity": {"role_arn": AWS_ROLE}}, headers=headers)
    aws = next(v for v in client.get("/api/access/picker", headers=headers).json()["vendors"] if v["id"] == "aws")
    assert [a["id"] for a in aws["accounts"]] == ["acme-audit"] and aws["connected"] is True
    assert sorted(r["id"] for r in client.get("/api/admin/integrations", headers=headers).json()["integrations"]) == ["acme-audit", "acme-models"]


def test_connections_the_permission_service_holds_show_in_the_list_and_can_be_removed(tmp_path, fake_storage):
    """Gate (authorization): Connections the permission service holds show in the list and can be removed."""
    bab = FakeBab()  # its default integration, connected straight into bab, never through the panel
    client, headers = _access_client(tmp_path, fake_storage, bab=bab)
    rows = client.get("/api/admin/integrations", headers=headers).json()["integrations"]
    (row,) = rows
    assert row["id"] == "aws-prod" and row["spec"]["source"] == "access" and row["state"] == "applied"
    assert "connected in the permission service" in row["state_detail"]
    out = client.delete("/api/admin/integrations/aws-prod", headers=headers).json()
    assert out["access"].startswith("removed") and bab.integrations("x") == []


def test_an_organization_then_its_accounts_register_themselves(tmp_path, fake_storage):
    """Gate (authorization): An organization then its accounts register themselves."""
    bab = FakeBab(integrations=[])
    github = FakeGitHub(existing_repos={GATEWAY_REPO}, extra_clone_files={"registry.toml": REGISTRY}, track_pushed_content=True)
    client, headers = _access_client(tmp_path, fake_storage, github=github, bab=bab)

    # The organization's set-up page: the hub role, trusting the room's identity (oaud, not aud),
    # and the one spoke template every member account gets, with the organization's external id.
    page = client.get("/api/admin/integrations/aws/setup?mode=org&integration=acme-aws", headers=headers).json()
    assert page["mode"] == "org" and page["role_name"] == "KoboAccess-Hub" and page["spoke_role_name"] == "KoboAccess-Role"
    assert 'room_service_account_unique_id = "102000000000000000000"' in page["terraform"]
    assert '"accounts.google.com:oaud": "sts.amazonaws.com"' in page["trust_policy"]
    assert "sts:AssumeRole" in page["permissions_policy"] and "organizations:ListAccounts" in page["permissions_policy"]
    assert page["external_id"] == "ext-acme-aws" and page["hub_role_arn"] is None
    assert 'external_id    = "ext-acme-aws"' in page["spoke_terraform"]
    assert "create-stack-set" in page["stackset"] and "ParameterKey=ExternalId,ParameterValue=ext-acme-aws" in page["stackset"]
    assert page["cloudformation"]["parameters"]["ExternalId"] == "ext-acme-aws"

    # Connect the organization: probed, a bab row of kind org with no ceiling and the external
    # id, then its accounts checked through the hub: the one carrying the role is connected.
    row = client.post("/api/admin/integrations", json={"vendor": "aws", "name": "acme-aws", "base_access": "read", "auth": "aws_hub",
                                                        "identity": {"hub_role_arn": HUB}}, headers=headers).json()
    assert row["id"] == "acme-aws" and row["state"] == "applied" and row["spec"]["kind"] == "org", row
    assert "1 account(s) carry the role and are connected, 1 do not yet" in row["state_detail"]
    assert bab.probes[0] == (HUB, "sts.amazonaws.com") and bab.discoveries == [("acme-aws", "KoboAccess-Role")]
    rows = {i["integration"]: i for i in bab.integrations("x")}
    assert rows["acme-aws"]["kind"] == "org" and rows["acme-aws"]["ceiling"] == [] and rows["acme-aws"]["hub_role_arn"] == HUB
    assert rows["acme-aws"]["spec"]["external_id"] == "ext-acme-aws"
    acct = rows["acme-prod"]
    assert acct["parent"] == "acme-aws" and acct["kind"] == "account" and acct["account_id"] == "111111111111"
    assert acct["spec"]["spoke_role_arn"] == "arn:aws:iam::111111111111:role/KoboAccess-Role"
    assert "external_id" not in acct["spec"], "a discovered account leases with the organization's external id"
    assert acct["ceiling"] == ["aws_cloudtrail_lake_query", "aws_inventory_read"]
    assert not github._pushed_trees, "nothing goes to the gateway: leases come from the permission service"

    # Now the page knows the hub, and the organization's accounts list one connected, one waiting.
    page = client.get("/api/admin/integrations/aws/setup?mode=org&integration=acme-aws", headers=headers).json()
    assert page["hub_role_arn"] == HUB and f'hub_role_arn   = "{HUB}"' in page["spoke_terraform"]
    accts = {a["id"]: a for a in client.get("/api/admin/integrations/acme-aws/accounts", headers=headers).json()["accounts"]}
    assert accts["111111111111"]["connected"] is True and accts["111111111111"]["integration"] == "acme-prod"
    assert accts["222222222222"]["connected"] is False
    panel = {r["id"]: r for r in client.get("/api/admin/integrations", headers=headers).json()["integrations"]}
    assert panel["acme-aws"]["spec"]["kind"] == "org" and panel["acme-prod"]["spec"]["parent"] == "acme-aws"
    assert panel["acme-prod"]["spec"]["account_id"] == "111111111111" and panel["acme-prod"]["state"] == "applied"

    # Check again later: the second account gained the role; it is connected, the first left alone.
    bab.reachable_accounts = {"111111111111", "222222222222"}
    found = client.post("/api/admin/integrations/acme-aws/accounts/check", headers=headers).json()
    assert [a["id"] for a in found["already"]] == ["111111111111"]
    assert [(a["id"], a["integration"]) for a in found["connected"]] == [("222222222222", "acme-data")]
    assert found["without_role"] == []
    bad = client.post("/api/admin/integrations/acme-prod/accounts/check", headers=headers)
    assert bad.status_code == 409

    # An organization connected before ids were per organization gets its id on the first check.
    bab._integrations.append({"integration": "older-aws", "vendor": "aws", "ceiling": [], "kind": "org", "parent": None, "name": "older-aws",
                              "account_id": None, "hub_role_arn": HUB, "spec": {"hub_role_arn": HUB, "name": "older-aws"},
                              "created_at": "2026-10-28T12:00:00", "updated_at": "2026-10-28T12:00:00"})
    found = client.post("/api/admin/integrations/older-aws/accounts/check", headers=headers).json()
    older = next(i for i in bab.integrations("x") if i["integration"] == "older-aws")
    assert older["spec"]["external_id"] == "ext-older-aws" and older["kind"] == "org"
    assert [a["id"] for a in found["connected"]] == ["111111111111", "222222222222"]

    # The picker offers both accounts under their organization; the organization itself never.
    picker = {v["id"]: v for v in client.get("/api/access/picker", headers=headers).json()["vendors"]}["aws"]
    assert {o["id"] for o in picker["organizations"]} == {"acme-aws", "older-aws"}
    by_id = {a["id"]: a for a in picker["accounts"]}
    assert by_id["acme-prod"]["connected"] is True and by_id["acme-prod"]["organization"] == "acme-aws"
    assert by_id["acme-data"]["connected"] is True
    assert "acme-aws" not in by_id, "an organization itself is never offered for a lease"

    # An account's own set-up page still exists for a role made by hand: it names the organization's id.
    page = client.get("/api/admin/integrations/aws/setup?mode=account&integration=acme-x&parent=acme-aws&account_id=333333333333", headers=headers).json()
    assert page["external_id"] == "ext-acme-aws" and '"sts:ExternalId": "ext-acme-aws"' in page["trust_policy"]
    bad = client.get("/api/admin/integrations/aws/setup?mode=account&integration=x&parent=nope&account_id=111111111111", headers=headers)
    assert bad.status_code == 409


GCP_PROJECT = "example-prod-1234"


def _gcp_bab() -> FakeBab:
    bab = FakeBab(integrations=[])
    bab._catalogue["gcp"] = GCP_CATALOGUE
    return bab


def test_the_picker_says_which_permissions_open_no_tool():
    """Gate (kobo#474): a permission that could be granted and that no tool uses is
    marked in the picker, so the page greys it instead of offering access that does
    nothing. Google Cloud and Datadog go by permission (the model-access row and
    Datadog's SLOs open no tool), AWS serves every permission through one tool,
    GitHub goes by level, and a vendor the room has no lane for (Azure), or a row
    the catalogue gained since, opens nothing until the room learns its tool. And
    whatever the room says opens a tool has a lane to carry it to the agent."""
    from kobo_api import gateway_lane
    from kobo_api.bab import picker

    bab = _gcp_bab()
    bab._catalogue["azure"] = {"vendor": "azure", "tiers": [], "groups": [{"name": "Management", "permissions": [
        {"name": "azure_resources_read", "tier": "read", "description": "Everything in the subscription, read-only"}]}]}
    bab._catalogue["datadog"]["groups"][0]["permissions"].append({"name": "datadog_slos_read", "tier": "read", "description": "SLOs"})
    bab._catalogue["github"]["groups"][0]["permissions"].append({"name": "github_releases_write", "tier": "mutate", "description": "new"})

    tools = {v["id"]: {p["name"]: p["tool"] for g in v["groups"] for p in g["permissions"]} for v in picker(bab, "x")}

    assert tools["gcp"] == {"gcp_compute_read": True, "gcp_logging_read": True, "gcp_vertex_ai_user": False}
    assert tools["datadog"] == {"datadog_monitors_read": True, "datadog_slos_read": False}
    assert all(tools["aws"].values()) and tools["aws"]
    assert tools["github"] == {"github_repos_read": True, "github_pulls_review": True, "github_pulls_write": True,
                               "github_releases_write": False}
    assert tools["azure"] == {"azure_resources_read": False}
    for vendor, permissions in gateway_lane.TOOLED.items():
        assert all(gateway_lane.lane_for(vendor, p) for p in permissions), vendor


def test_a_google_cloud_project_is_probed_through_the_permission_service_and_held_there_with_the_read_ceiling(tmp_path, fake_storage):
    """Gate (authorization, kobo#390): a project connects only after the permission service
    reads it as its own identity (and Vertex AI there when the models box is ticked); the
    row is held there with every gcp read permission as the ceiling, the room's row carries
    the models spec the model gateway reads, and a refused or malformed project is written
    nowhere."""
    bab = _gcp_bab()
    github = FakeGitHub(existing_repos={GATEWAY_REPO}, extra_clone_files={"registry.toml": REGISTRY}, track_pushed_content=True)
    client, headers = _access_client(tmp_path, fake_storage, github=github, bab=bab)
    body = {"vendor": "gcp", "name": "Example prod", "base_access": "read", "auth": "gcp_identity",
            "identity": {"project": GCP_PROJECT, "models_provider": "vertex_ai", "models_location": "us-east5"}}
    response = client.post("/api/admin/integrations", json=body, headers=headers)
    assert response.status_code == 201, response.text
    row = response.json()
    # The probe named the project, no service account, and the Vertex AI read at the location.
    assert bab.gcp_probes == [{
        "project": GCP_PROJECT, "service_account": None, "vertex_location": "us-east5",
        "vertex_url": f"https://us-east5-aiplatform.googleapis.com/v1/projects/{GCP_PROJECT}/locations/us-east5/publishers/google/models?pageSize=1",
    }]
    assert row["id"] == "example-prod" and row["state"] == "applied" and row["credential_ref"] is None
    assert "Google Cloud agreed" in row["state_detail"] and "Vertex AI answers at us-east5" in row["state_detail"]
    assert row["spec"]["kind"] == "account" and row["spec"]["project"] == GCP_PROJECT
    assert row["spec"]["models"] == {"provider": "vertex_ai", "project": GCP_PROJECT, "location": "us-east5"}
    (integ,) = bab.integrations("x")
    assert integ["vendor"] == "gcp" and integ["kind"] == "account" and integ["parent"] is None
    assert integ["spec"] == {"project": GCP_PROJECT, "service_account": None, "name": "Example prod"}
    assert integ["ceiling"] == ["gcp_compute_read", "gcp_logging_read", "gcp_vertex_ai_user"]
    assert not github._pushed_trees, "the gateway has nothing to learn: leases come from the permission service"
    # The picker reads it as a project, under no parent.
    picker = {v["id"]: v for v in client.get("/api/access/picker", headers=headers).json()["vendors"]}["gcp"]
    assert picker["account_noun"] == "project" and picker["connected"] is True
    assert [(a["id"], a["organization"]) for a in picker["accounts"]] == [("example-prod", None)]
    # Without the box, no models spec and no Vertex read; the global location uses the plain host.
    plain = client.post("/api/admin/integrations", json={**body, "name": "Example data", "identity": {"project": "example-data-5678"}},
                        headers=headers).json()
    assert "models" not in plain["spec"] and bab.gcp_probes[-1] == {"project": "example-data-5678", "service_account": None}
    client.post("/api/admin/integrations", json={**body, "name": "Example lab", "identity": {
        "project": "example-lab-9012", "service_account": "narrow@example-lab-9012.iam.gserviceaccount.com",
        "models_provider": "vertex_ai", "models_location": "global"}}, headers=headers)
    assert bab.gcp_probes[-1]["vertex_url"].startswith("https://aiplatform.googleapis.com/v1/projects/example-lab-9012/locations/global/")
    assert bab.gcp_probes[-1]["service_account"] == "narrow@example-lab-9012.iam.gserviceaccount.com"
    # A project the identity cannot read, and a malformed id, are saved nowhere.
    refused = client.post("/api/admin/integrations", json={**body, "name": "nope", "identity": {"project": "refuse-me-1234"}}, headers=headers)
    assert refused.status_code == 409 and "lacks access" in refused.json()["error"] and "connect again" in refused.json()["error"]
    bad = client.post("/api/admin/integrations", json={**body, "name": "bad", "identity": {"project": "My Project"}}, headers=headers)
    assert bad.status_code == 409 and "project's id" in bad.json()["error"]
    assert len(bab.gcp_probes) == 4 and len(bab.integrations("x")) == 3, "the refused project was probed, the malformed one never reached the service"
    assert {r["id"] for r in client.get("/api/admin/integrations", headers=headers).json()["integrations"]} == {"example-prod", "example-data", "example-lab"}
    # Disconnect takes it out of the permission service too.
    out = client.delete(f"/api/admin/integrations/{row['id']}", headers=headers).json()
    assert out["access"].startswith("removed") and len(bab.integrations("x")) == 2


def test_a_google_cloud_folder_lists_its_whole_tree_and_an_admin_picks_the_projects_to_connect(tmp_path, fake_storage):
    """Gate (authorization, kobo#390, picked since 2026-11-09): a folder or organization is
    the hub shape: a row with no ceiling and nothing connected under it by itself. Check
    projects lists the whole tree (where each project sits, whether the identity can read it)
    and the admin picks; a picked project is read once, then held under ITS OWN id with the
    read ceiling. Unreadable, unlisted and repeated picks are reported, not written."""
    bab = _gcp_bab()
    client, headers = _access_client(tmp_path, fake_storage, bab=bab)
    row = client.post("/api/admin/integrations", json={"vendor": "gcp", "name": "example-platform", "base_access": "read",
                                                        "auth": "gcp_parent", "identity": {"parent_resource": "folders/123"}},
                      headers=headers).json()
    assert row["id"] == "example-platform" and row["state"] == "applied" and row["spec"]["kind"] == "org", row
    assert row["spec"]["parent_resource"] == "folders/123"
    # Nothing under it connects by itself (2026-11-09): the detail counts the tree and points at the picker.
    assert "4 project(s) found under it, none connected yet: pick them under Check projects" in row["state_detail"]
    assert bab.gcp_probes == [] and bab.discoveries == []
    rows = {i["integration"]: i for i in bab.integrations("x")}
    assert rows["example-platform"]["kind"] == "org" and rows["example-platform"]["ceiling"] == []
    assert rows["example-platform"]["spec"] == {"parent_resource": "folders/123", "name": "example-platform"}
    assert [k for k in rows if k != "example-platform"] == []
    # Check projects lists the whole tree for picking: where each sits, whether the identity can read it.
    found = client.post("/api/admin/integrations/example-platform/accounts/check", headers=headers).json()
    assert found["connected"] == [] and found["already"] == [] and bab.discoveries == [("example-platform", None)]
    by_id = {a["id"]: a for a in found["accounts"]}
    assert [a["id"] for a in found["accounts"]] == [GCP_PROJECT, "example-data-5678", "example-lab-9012", "example-nested-3456"]
    assert by_id[GCP_PROJECT]["reachable"] is True and by_id[GCP_PROJECT]["folder_path"] == [] and by_id[GCP_PROJECT]["project_number"] == "100000000000"
    assert by_id["example-nested-3456"]["folder_path"] == ["Dev"] and by_id["example-nested-3456"]["reachable"] is False
    assert [a["id"] for a in found["without_role"]] == ["example-lab-9012", "example-nested-3456"]
    # The admin picks: a readable project connects under ITS OWN id (the gateway leases by it), one the
    # identity cannot read is refused with the service's sentence, one not under the parent is refused, and
    # a repeat leaves the connected one alone.
    bab.refused_projects.add("example-lab-9012")
    picked = client.post("/api/admin/integrations/example-platform/accounts/connect",
                         json={"projects": [GCP_PROJECT, "example-lab-9012", "elsewhere-9999"]}, headers=headers).json()
    assert [(a["id"], a["integration"]) for a in picked["connected"]] == [(GCP_PROJECT, GCP_PROJECT)]
    assert [(a["id"], a["reason"].split(":")[0]) for a in picked["refused"]] == [
        ("example-lab-9012", "the permission service refused the project example-lab-9012"),
        ("elsewhere-9999", "not under example-platform as the permission service lists it")]
    assert bab.gcp_probes[-2:] == [{"project": GCP_PROJECT}, {"project": "example-lab-9012"}]
    rows = {i["integration"]: i for i in bab.integrations("x")}
    prod = rows[GCP_PROJECT]
    assert prod["parent"] == "example-platform" and prod["kind"] == "account"
    assert prod["spec"] == {"project": GCP_PROJECT, "service_account": None, "name": "Example prod"}
    assert prod["ceiling"] == ["gcp_compute_read", "gcp_logging_read", "gcp_vertex_ai_user"]
    assert "example-lab-9012" not in rows and "elsewhere-9999" not in rows
    panel = {r["id"]: r for r in client.get("/api/admin/integrations", headers=headers).json()["integrations"]}
    assert panel[GCP_PROJECT]["name"] == "Example prod" and panel[GCP_PROJECT]["spec"]["parent"] == "example-platform"
    assert panel[GCP_PROJECT]["spec"]["project"] == GCP_PROJECT and panel[GCP_PROJECT]["spec"]["project_number"] == "123456789012"
    assert panel[GCP_PROJECT]["spec"]["folder_path"] == [] and panel[GCP_PROJECT]["state"] == "applied"
    again = client.post("/api/admin/integrations/example-platform/accounts/connect", json={"projects": [GCP_PROJECT]}, headers=headers).json()
    assert [a["id"] for a in again["already"]] == [GCP_PROJECT] and again["connected"] == []
    # Later the lab project is granted: the picker shows it readable and it connects on the next pick.
    bab.reachable_projects.add("example-lab-9012")
    bab.refused_projects.discard("example-lab-9012")
    assert {a["id"]: a["reachable"] for a in client.post("/api/admin/integrations/example-platform/accounts/check", headers=headers).json()["accounts"]}["example-lab-9012"] is True
    picked = client.post("/api/admin/integrations/example-platform/accounts/connect", json={"projects": ["example-lab-9012"]}, headers=headers).json()
    assert [a["integration"] for a in picked["connected"]] == ["example-lab-9012"]
    # The parent's projects, each connected or not; an empty or oversized pick is refused.
    projects = {a["id"]: a for a in client.get("/api/admin/integrations/example-platform/accounts", headers=headers).json()["accounts"]}
    assert projects[GCP_PROJECT]["connected"] is True and projects["example-data-5678"]["connected"] is False
    assert client.post("/api/admin/integrations/example-platform/accounts/connect", json={"projects": []}, headers=headers).status_code == 422
    assert client.post("/api/admin/integrations/example-platform/accounts/connect", json={"projects": ["a"] * 51}, headers=headers).status_code == 422
    assert client.post("/api/admin/integrations/example-prod-1234/accounts/connect", json={"projects": ["x-1234567"]}, headers=headers).status_code == 409
    # The picker offers the connected projects under their parent; the parent itself never.
    picker = {v["id"]: v for v in client.get("/api/access/picker", headers=headers).json()["vendors"]}["gcp"]
    assert [o["id"] for o in picker["organizations"]] == ["example-platform"]
    by_id = {a["id"]: a for a in picker["accounts"]}
    assert by_id[GCP_PROJECT]["organization"] == "example-platform" and "example-platform" not in by_id
    assert "example-data-5678" not in by_id, "an unpicked project is not offered"
    # An organization is the other parent shape; a badly named one is refused.
    assert client.post("/api/admin/integrations", json={"vendor": "gcp", "name": "x", "base_access": "read", "auth": "gcp_parent",
                                                         "identity": {"parent_resource": "folder-123"}}, headers=headers).status_code == 409


def test_an_organization_with_the_vertex_tick_names_the_models_project_and_the_gateway_serves_it(tmp_path, fake_storage):
    """Wire shape the model gateway reads (kobo#405): an organization row connected with
    the Vertex AI box ticked carries exactly `models = {provider, project, location}` for
    the NAMED project, the permission service listed that project's models at the location
    before anything was written, and `providers_from_rows` serves that project. A Vertex AI
    project the identity cannot read refuses the whole connect: no row here, none there."""
    from kobo_api import model_providers
    from kobo_api.models import AdminIntegration

    bab = _gcp_bab()
    client, headers = _access_client(tmp_path, fake_storage, bab=bab)
    body = {"vendor": "gcp", "name": "example-org", "base_access": "read", "auth": "gcp_parent",
            "identity": {"parent_resource": "organizations/123456789012", "models_provider": "vertex_ai",
                         "models_project": "my-vertex-project", "models_location": "us-east5"}}
    refused = client.post("/api/admin/integrations", json={**body, "identity": {**body["identity"], "models_project": "refuse-me-1234"}},
                          headers=headers)
    assert refused.status_code == 409 and "lacks access" in refused.json()["error"] and "models pack" in refused.json()["error"]
    assert client.get("/api/admin/integrations", headers=headers).json()["integrations"] == [] and bab.integrations("x") == []
    unnamed = client.post("/api/admin/integrations", json={**body, "identity": {**body["identity"], "models_project": ""}}, headers=headers)
    assert unnamed.status_code == 409 and "Vertex AI project" in unnamed.json()["error"]

    row = client.post("/api/admin/integrations", json=body, headers=headers).json()
    assert row["state"] == "applied" and row["spec"]["kind"] == "org", row
    assert row["spec"]["models"] == {"provider": "vertex_ai", "project": "my-vertex-project", "location": "us-east5"}
    assert bab.gcp_probes[-1] == {
        "project": "my-vertex-project", "service_account": None, "vertex_location": "us-east5",
        "vertex_url": "https://us-east5-aiplatform.googleapis.com/v1/projects/my-vertex-project/locations/us-east5/publishers/google/models?pageSize=1",
    }
    assert "Vertex AI answers at us-east5 in my-vertex-project" in row["state_detail"]
    # The organization itself is held with no ceiling, as before; its projects wait for the picker (2026-11-09).
    assert {i["integration"]: i["kind"] for i in bab.integrations("x")} == {"example-org": "org"}
    assert bab.integrations("x")[0]["spec"] == {"parent_resource": "organizations/123456789012", "name": "example-org"}
    rows = [AdminIntegration(id=r["id"], vendor=r["vendor"], name=r["name"], state=r["state"], spec=r["spec"], credential_ref=None)
            for r in client.get("/api/admin/integrations", headers=headers).json()["integrations"]]
    (provider,) = model_providers.providers_from_rows(rows)
    assert (provider.row_id, provider.provider, provider.project, provider.location) == ("example-org", "vertex_ai", "my-vertex-project", "us-east5")


def test_a_datadog_probe_the_permission_service_refuses_connects_nothing(tmp_path, fake_storage, monkeypatch):
    """Gate (integrity): a Datadog service account the permission service cannot mint from is
    connected nowhere: the Secret written for the probe is taken back, no row here, none in
    the service. A service not yet configured for Datadog keeps the Secret for its next
    restart and the row waits, saying so (kobo#322)."""
    from kobo_api import admin_integrations
    monkeypatch.setattr(admin_integrations, "DATADOG_PROBE_PAUSE_S", 0)
    bab = FakeBab(integrations=[])
    client, headers = _access_client(tmp_path, fake_storage, bab=bab)
    kubectl = client.app.state.clients.kubectl
    body = {"vendor": "datadog", "name": "Datadog", "base_access": "read", "auth": "datadog_service_account",
            "identity": {"service_account_id": "refuse-me"}, "secrets": {"API_KEY": "dd-api", "MGMT_APP_KEY": "dd-mgmt"}}
    response = client.post("/api/admin/integrations", json=body, headers=headers)
    assert response.status_code == 409 and "connect again" in response.json()["error"]
    assert bab.integrations("x") == [] and client.get("/api/admin/integrations", headers=headers).json()["integrations"] == []
    assert ("bab", "bab-datadog") not in kubectl.secrets
    # Not read yet: the probe is tried for a while (the mounted Secret takes the kubelet a
    # moment), then the Secret stays, the row waits and says so.
    response = client.post("/api/admin/integrations", json={**body, "identity": {"service_account_id": "unconfigured-sa"}}, headers=headers)
    assert response.status_code == 201 and response.json()["state"] == "pending"
    assert "within a minute or two" in response.json()["state_detail"] and ("bab", "bab-datadog") in kubectl.secrets
    assert bab.datadog_probes.count("unconfigured-sa") == admin_integrations.DATADOG_PROBE_TRIES


def test_datadog_through_the_permission_service_adds_its_mcp_server_to_the_gateway(tmp_path, fake_storage):
    """Wire shape (gateway config): Datadog through the permission service opens the pull request
    that adds Datadog's MCP server as the `datadog-mcp` target with `service-headers` auth, so each
    agent's calls carry its own leased key; disconnecting opens the one that removes it, sub-tables
    and all (kobo#327)."""
    bab = FakeBab(integrations=[])
    github = FakeGitHub(existing_repos={GATEWAY_REPO}, extra_clone_files={"registry.toml": REGISTRY}, track_pushed_content=True,
                        comparisons={"kobo/integration-datadog": BranchComparison(found=True, files=("targets.toml",), authors=(COMMIT_AUTHOR_EMAIL,))})
    client, headers = _access_client(tmp_path, fake_storage, github=github, bab=bab)
    body = {"vendor": "datadog", "name": "Datadog", "base_access": "read", "auth": "datadog_service_account",
            "identity": {"service_account_id": "11111111-2222-3333-4444-555555555555", "site": "us3.datadoghq.com"},
            "secrets": {"API_KEY": "dd-api", "MGMT_APP_KEY": "dd-mgmt"}}
    row = client.post("/api/admin/integrations", json=body, headers=headers).json()
    assert row["state"] == "pending" and row["spec"]["pull_request"].startswith("https://"), row
    assert row["spec"]["address"] == "https://mcp.us3.datadoghq.com/v1", "derived from the site when nothing is pasted"
    targets = github.pushed_file("targets.toml")
    for line in ('name = "datadog-mcp"', 'auth_method = "service-headers"', 'address = "https://mcp.us3.datadoghq.com/v1"',
                 "[target.credential_headers]", 'DD_APPLICATION_KEY = "DD_APP_KEY"', 'required_scope = "datadog:read"'):
        assert line in targets, line
    assert "dd-api" not in targets and "dd-mgmt" not in targets
    # The edit went out as the pull request's branch and never as a push to the gateway's
    # default branch (kobo 0.3.17: that push once doubled the stanza on main and took the
    # gateway down, since liminal refuses a duplicate target name).
    assert github.push_count == 0 and github.open_pr_calls[-1]["branch"] == "kobo/integration-datadog"
    assert targets.count('name = "datadog-mcp"') == 1
    # A stanza an operator wrote by hand under the same name, with its own comment, is
    # replaced rather than doubled.
    from kobo_api.admin_integrations import _without_stanza
    hand = ('[[target]]\nname = "other"\naddress = "https://o"\n\n# Datadog, by hand (two\n# comment lines).\n[[target]]\n'
            'name = "datadog-mcp"\naddress = "https://old"\n[target.credential_headers]\nDD_API_KEY = "DD_API_KEY"\n')
    assert _without_stanza(hand, "datadog-mcp") == '[[target]]\nname = "other"\naddress = "https://o"\n'
    # The add pull request merges: the next look at the pane finds out, the row is Connected
    # and the link is gone (kobo#327); the disconnect then opens the one that removes the
    # target whole, on its own branch.
    github.close_pr(GATEWAY_REPO, row["spec"]["pull_request"], "merged")
    listed = next(r for r in client.get("/api/admin/integrations", headers=headers).json()["integrations"] if r["id"] == "datadog")
    assert listed["state"] == "applied" and "pull_request" not in listed["spec"] and "merged" in listed["state_detail"], listed
    out = client.delete(f"/api/admin/integrations/{row['id']}", headers=headers).json()
    assert out["gateway"].startswith("pull request open") and out["secret"] == "removed"
    assert github.open_pr_calls[-1]["branch"] == "kobo/integration-datadog-remove"
    targets = github.pushed_file("targets.toml")
    assert "datadog-mcp" not in targets and "[target.credential_headers]" not in targets
    # Adding again while that removal is still open closes it first: the row never links to a
    # pull request that would take its target away (seen live on dev, 2026-10-30).
    removal = out["gateway"].split(": ", 1)[1]
    row = client.post("/api/admin/integrations", json=body, headers=headers).json()
    assert any(url == removal for _repo, url, _why in github.close_pr_calls), github.close_pr_calls
    assert github.open_pr_calls[-1]["branch"] == "kobo/integration-datadog"  # the fake hands every PR one url
    client.delete(f"/api/admin/integrations/{row['id']}", headers=headers)
    # A pasted address is kept without the `/mcp` the gateway adds itself.
    row = client.post("/api/admin/integrations", json={**body, "address": "https://mcp.us5.datadoghq.com/v1/mcp"}, headers=headers).json()
    assert row["spec"]["address"] == "https://mcp.us5.datadoghq.com/v1"


DATADOG_SA_BODY = {"vendor": "datadog", "name": "Datadog", "base_access": "read", "auth": "datadog_service_account",
                   "identity": {"service_account_id": "11111111-2222-3333-4444-555555555555", "site": "us3.datadoghq.com"},
                   "secrets": {"API_KEY": "dd-api", "MGMT_APP_KEY": "dd-mgmt"}}


def test_with_the_admin_plane_wired_datadog_mcp_target_goes_live_and_no_pull_request_opens(tmp_path, fake_storage):
    """Wire shape (gateway admin plane, liminal#222): a Datadog connect PUTs the `datadog-mcp`
    target to the gateway's admin plane as the stanza's keys, with `service-headers` auth and
    the leased-key headers, and opens no pull request; the disconnect DELETEs it and opens none
    either (kobo#327)."""
    from kobo_api.admin_integrations import datadog_target_spec
    from kobo_api.models import AdminIntegration
    admin = FakeLiminalAdmin()
    github = FakeGitHub(existing_repos={GATEWAY_REPO}, extra_clone_files={"registry.toml": REGISTRY}, track_pushed_content=True)
    client, headers = _access_client(tmp_path, fake_storage, github=github, bab=FakeBab(integrations=[]))
    client.app.state.clients.liminal_admin = admin
    row = client.post("/api/admin/integrations", json=DATADOG_SA_BODY, headers=headers).json()
    assert row["state"] == "applied" and "live on the gateway" in row["state_detail"], row
    assert row["spec"]["gateway"] == "live" and "pull_request" not in row["spec"]
    assert github.open_pr_calls == []
    expected = datadog_target_spec(AdminIntegration(id=row["id"], vendor="datadog", name="Datadog", base_access="read",
                                                    created_by="ops@example.com", spec=row["spec"]))
    target = admin.targets["datadog-mcp"]
    assert target == expected
    assert target["auth_method"] == "service-headers" and target["required_scope"] == "datadog:read"
    assert target["credential_headers"] == {"DD_API_KEY": "DD_API_KEY", "DD_APPLICATION_KEY": "DD_APP_KEY"}
    assert "dd-api" not in str(target) and "dd-mgmt" not in str(target)
    out = client.delete(f"/api/admin/integrations/{row['id']}", headers=headers).json()
    assert out["gateway"] == "removed from the gateway" and "datadog-mcp" not in admin.targets
    assert github.open_pr_calls == []


def test_an_admin_plane_without_target_routes_falls_back_to_the_pull_request(tmp_path, fake_storage):
    """Wire shape (gateway config): a gateway whose admin plane predates targets (before liminal
    0.9.0) still gets the `datadog-mcp` target by pull request, and the row says why."""
    admin = FakeLiminalAdmin(targets_unsupported=True)
    github = FakeGitHub(existing_repos={GATEWAY_REPO}, extra_clone_files={"registry.toml": REGISTRY}, track_pushed_content=True)
    client, headers = _access_client(tmp_path, fake_storage, github=github, bab=FakeBab(integrations=[]))
    client.app.state.clients.liminal_admin = admin
    row = client.post("/api/admin/integrations", json=DATADOG_SA_BODY, headers=headers).json()
    assert row["state"] == "pending" and row["spec"]["pull_request"].startswith("https://"), row
    assert "does not take targets yet" in row["state_detail"] and admin.targets == {}
    assert 'name = "datadog-mcp"' in github.pushed_file("targets.toml")


def test_a_target_the_gateway_refuses_fails_the_row_and_opens_no_pull_request(tmp_path, fake_storage):
    """Gate (integrity): a spec the gateway's admin plane refuses (409 file-owned, 422 invalid) is
    not sent round again as a pull request; the row is Failed with the gateway's sentence."""
    admin = FakeLiminalAdmin(refuse_target="target 'datadog-mcp' is owned by the gateway's config file")
    github = FakeGitHub(existing_repos={GATEWAY_REPO}, extra_clone_files={"registry.toml": REGISTRY}, track_pushed_content=True)
    client, headers = _access_client(tmp_path, fake_storage, github=github, bab=FakeBab(integrations=[]))
    client.app.state.clients.liminal_admin = admin
    row = client.post("/api/admin/integrations", json=DATADOG_SA_BODY, headers=headers).json()
    assert row["state"] == "failed" and "the gateway refused the target: target 'datadog-mcp' is owned" in row["state_detail"], row
    assert github.open_pr_calls == [] and "pull_request" not in row["spec"]


def test_a_target_the_gateway_already_carries_connects_without_a_pull_request(tmp_path, fake_storage):
    """Gate (integrity): when the gateway's targets.toml already holds the room's stanza, the
    connect has nothing to change there; the row is Connected and says so, not "add by hand"."""
    from kobo_api.admin_integrations import datadog_target_stanza
    from kobo_api.models import AdminIntegration
    row = AdminIntegration(id="datadog", vendor="datadog", name="Datadog", base_access="read", created_by="ops@example.com",
                           spec={"auth": "datadog_service_account", "address": "https://mcp.us3.datadoghq.com/v1"})
    github = FakeGitHub(existing_repos={GATEWAY_REPO}, track_pushed_content=True,
                        extra_clone_files={"registry.toml": REGISTRY, "targets.toml": "config_version = 1\n" + datadog_target_stanza(row)})
    client, headers = _access_client(tmp_path, fake_storage, github=github, bab=FakeBab(integrations=[]))
    body = {"vendor": "datadog", "name": "Datadog", "base_access": "read", "auth": "datadog_service_account",
            "identity": {"service_account_id": "11111111-2222-3333-4444-555555555555", "site": "us3.datadoghq.com"},
            "secrets": {"API_KEY": "dd-api", "MGMT_APP_KEY": "dd-mgmt"}}
    out = client.post("/api/admin/integrations", json=body, headers=headers).json()
    assert out["state"] == "applied" and "already carries the datadog-mcp target" in out["state_detail"], out
    assert not github.open_pr_calls and "pull_request" not in out["spec"]


def test_an_email_and_api_token_vendor_is_checked_then_served_as_basic_auth(tmp_path, fake_storage, monkeypatch):
    """Gate (integrity): Atlassian's MCP server takes a user's API token only as Basic auth with the
    account's email (a Bearer is for its service-account keys). The room checks the pair with the
    server the way the gateway will, stores them joined as the one value the gateway's basic scheme
    sends, drops the /mcp the docs put on the address (the gateway's client adds it), and the target
    carries the per-agent session and the vendor's read scope so only a granted agent reaches it."""
    from kobo_api import admin_integrations
    probes: list[tuple] = []
    monkeypatch.setattr(admin_integrations, "_basic_probe", lambda name, address, user, token: probes.append((name, address, user, token)))
    bab = FakeBab(integrations=[])
    client, headers = _access_client(tmp_path, fake_storage, bab=bab)
    admin = FakeLiminalAdmin()
    client.app.state.clients.liminal_admin = admin
    sero = client.app.state.clients.sero
    body = {"vendor": "atlassian", "name": "Atlassian", "base_access": "read", "auth": "basic_token",
            "identity": {"email": "bot@example.com"}, "secrets": {"TOKEN": "tok-1"}, "address": "https://mcp.atlassian.com/v2/mcp"}
    row = client.post("/api/admin/integrations", json=body, headers=headers).json()
    assert row["state"] == "applied" and "Atlassian accepted the email and API token" in row["state_detail"], row
    assert probes == [("Atlassian", "https://mcp.atlassian.com/v2", "bot@example.com", "tok-1")]
    target = admin.targets["atlassian"]
    assert target["auth_method"] == "bearer" and target["auth_scheme"] == "basic"
    assert target["address"] == "https://mcp.atlassian.com/v2"
    assert target["session"] == "per-persona" and target["required_scope"] == "atlassian:read"
    # v2 lists writing tools beside the reads: the Jira field tools behind the scope the Write issues
    # permission opens, the Jira prose tools and the executors behind one no lane ever will (the words
    # come from the signed jira-write handler), Confluence's behind one no lane grants yet.
    assert target["tool_scopes"]["editJiraIssue"] == "atlassian:jira-write" and target["tool_scopes"]["transitionJiraIssue"] == "atlassian:jira-write"
    assert target["tool_scopes"]["createJiraIssue"] == "atlassian:never" and target["tool_scopes"]["addOrEditJiraIssueComment"] == "atlassian:never"
    assert target["tool_scopes"]["createConfluenceContent"] == "atlassian:confluence-write"
    assert target["tool_scopes"]["executeDestructive"] == "atlassian:never" and target["tool_scopes"]["executeWrite"] == "atlassian:never"
    assert "getJiraIssue" not in target["tool_scopes"] and "searchJiraIssuesUsingJql" not in target["tool_scopes"]
    # Connected for reading, the connection's ceiling is the read tier: no agent is offered Write issues.
    (integ,) = bab.integrations("x")
    assert "atlassian_jira_issues_write" not in integ["ceiling"]
    (put,) = [p for p in sero.puts if p[1] == "apps/kobo/integrations/atlassian"]
    assert put[2] == {"TOKEN": "bot@example.com:tok-1"}
    assert row["spec"]["identity"] == {"email": "bot@example.com"} and row["spec"]["secret_keys"] == ["TOKEN"]
    (integ,) = bab.integrations("x")
    assert integ["vendor"] == "atlassian"

    # A pair the server refuses is stored nowhere.
    def refuse(name, address, user, token):
        raise admin_integrations.ConnectRefused(f"{name} did not accept the email and API token (HTTP 401)")
    monkeypatch.setattr(admin_integrations, "_basic_probe", refuse)
    r = client.post("/api/admin/integrations", json={**body, "vendor": "other-atlassian", "name": "Other"}, headers=headers)
    assert r.status_code == 409 and "did not accept" in r.json()["error"]
    assert len(sero.puts) == 1 and len(bab.integrations("x")) == 1
    # The token alone is refused before anything is checked.
    r = client.post("/api/admin/integrations", json={**body, "identity": {}}, headers=headers)
    assert r.status_code == 409 and "email and its API token" in r.json()["error"]


ATLASSIAN_CATALOGUE = {"atlassian": {"vendor": "atlassian", "groups": [{"name": "Jira", "permissions": [
    {"name": "atlassian_jira_issues_read", "tier": "read", "description": "Read issues and search with JQL"},
    {"name": "atlassian_jira_issues_write", "tier": "mutate", "label": "Write issues",
     "description": "Create and edit issues, add comments and move issues between statuses"}]}],
    "tiers": [{"name": "read", "lease_seconds": 3600, "validity_days": None}, {"name": "mutate", "lease_seconds": 3600, "validity_days": 30}]}}


def test_an_atlassian_connection_for_read_and_write_offers_write_issues_and_its_lane_opens_only_the_jira_writing_tools(tmp_path, fake_storage, monkeypatch):
    """Gate (integrity): "Agents may read and write" on the Atlassian sheet lifts the connection's
    ceiling to the catalogue's writing level, so an agent's owner may ask for Write issues; the grant
    earns the `atlassian:jira-write` scope, which the target puts on the four Jira writing tools and
    on nothing else (Confluence's writers and the generic executors stay behind scopes no grant opens)."""
    from kobo_api import admin_integrations, gateway_lane
    monkeypatch.setattr(admin_integrations, "_basic_probe", lambda *a: None)
    bab = FakeBab(integrations=[], catalogue=ATLASSIAN_CATALOGUE)
    client, headers = _access_client(tmp_path, fake_storage, bab=bab)
    client.app.state.clients.liminal_admin = FakeLiminalAdmin()
    body = {"vendor": "atlassian", "name": "Atlassian", "base_access": "write", "auth": "basic_token",
            "identity": {"email": "bot@example.com"}, "secrets": {"TOKEN": "tok-1"}, "address": "https://mcp.atlassian.com/v2"}
    row = client.post("/api/admin/integrations", json=body, headers=headers).json()
    assert row["state"] == "applied", row
    (integ,) = bab.integrations("x")
    assert integ["ceiling"] == ["atlassian_jira_issues_read", "atlassian_jira_issues_write"]
    read = gateway_lane.lane_for("atlassian", "atlassian_jira_issues_read")
    write = gateway_lane.lane_for("atlassian", "atlassian_jira_issues_write")
    assert read.scopes == ("atlassian:read",) and write.scopes == ("atlassian:read", "atlassian:jira-write")
    assert read.handlers == ("atlassian",) and write.handlers == ("atlassian", "jira-write")
    assert write.allow == ("atlassian.*", "jira-write.*")
    scopes = client.app.state.clients.liminal_admin.targets["atlassian"]["tool_scopes"]
    assert {t for t, s in scopes.items() if s == "atlassian:jira-write"} == {"editJiraIssue", "transitionJiraIssue"}
    # The words a person reads (a new issue, a comment) only ever come from the signed handler.
    assert scopes["createJiraIssue"] == scopes["addOrEditJiraIssueComment"] == "atlassian:never"
    assert "atlassian:jira-write" in gateway_lane.MANAGED_SCOPES


OKTA_CATALOGUE = {"okta": {"vendor": "okta", "groups": [
    {"name": "Directory", "permissions": [
        {"name": "okta_directory_read", "tier": "read", "label": "Look up people and groups", "description": "Find people and groups"}]},
    {"name": "Access", "permissions": [
        {"name": "okta_access_write", "tier": "mutate", "label": "Change group membership and assignments",
         "description": "Add a person to a group, assign a person to an app"}]}],
    "tiers": [{"name": "read", "lease_seconds": 3600, "validity_days": None}, {"name": "mutate", "lease_seconds": 3600, "validity_days": 30}]}}


def test_okta_connects_with_a_checked_token_the_gateway_reads_and_its_two_levels_open_one_handler(tmp_path, fake_storage, monkeypatch):
    """Gate (authorization, secrets): Okta is one admin API token every agent shares, so the room
    checks it with the organization before anything is stored, keeps it beside the domain in the one
    Secret the gateway's `okta` handler mounts (`integration-okta`, keys TOKEN and DOMAIN), never
    echoes it, writes no gateway target, and holds the connection with the read ceiling: the level
    that changes things is offered only on a connection made for reading and writing, and only that
    level earns the `okta:write` scope the handler's changing tools ask for."""
    from kobo_api import admin_integrations, gateway_lane
    probes: list[tuple] = []
    monkeypatch.setattr(admin_integrations, "_okta_probe", lambda name, domain, token: probes.append((name, domain, token)))
    bab = FakeBab(integrations=[], catalogue=OKTA_CATALOGUE)
    github = FakeGitHub(existing_repos={GATEWAY_REPO}, extra_clone_files={"registry.toml": REGISTRY}, track_pushed_content=True)
    client, headers = _access_client(tmp_path, fake_storage, bab=bab, github=github)
    admin = FakeLiminalAdmin()
    client.app.state.clients.liminal_admin = admin
    sero = client.app.state.clients.sero
    body = {"vendor": "okta", "name": "Okta", "base_access": "read", "auth": "okta_token",
            "identity": {"domain": "https://Example.okta.com/"}, "secrets": {"TOKEN": "00-okta-token"}}

    response = client.post("/api/admin/integrations", json=body, headers=headers)

    row = response.json()
    assert row["id"] == "okta" and row["state"] == "applied", row
    assert "Okta accepted the API token for example.okta.com" in row["state_detail"]
    assert probes == [("Okta", "example.okta.com", "00-okta-token")]
    (put,) = [p for p in sero.puts if p[1] == "apps/kobo/integrations/okta"]
    assert put[2] == {"TOKEN": "00-okta-token", "DOMAIN": "example.okta.com"}
    assert [(s[2], s[3], s[4]) for s in sero.syncs] == [("apps/kobo/integrations/okta", "liminal", "integration-okta")]
    assert row["spec"]["identity"] == {"domain": "example.okta.com"} and row["spec"]["secret_keys"] == ["DOMAIN", "TOKEN"]
    assert "00-okta-token" not in response.text
    # The gateway serves Okta from a handler of its own: no target, no pull request.
    assert admin.targets == {} and not github.open_pr_calls
    (integ,) = bab.integrations("x")
    assert integ["vendor"] == "okta" and integ["ceiling"] == ["okta_directory_read"]
    # Connected for reading and writing, the ceiling carries the level that changes things.
    row = client.post("/api/admin/integrations", json={**body, "base_access": "write"}, headers=headers).json()
    assert row["id"] == "okta" and row["state"] == "applied", row
    (integ,) = bab.integrations("x")
    assert sorted(integ["ceiling"]) == ["okta_access_write", "okta_directory_read"]
    read = gateway_lane.lane_for("okta", "okta_directory_read")
    write = gateway_lane.lane_for("okta", "okta_access_write")
    assert read.scopes == ("okta:read",) and write.scopes == ("okta:read", "okta:write")
    assert read.handlers == write.handlers == ("okta",) and read.allow == write.allow == ("okta.*",)
    assert gateway_lane.opens_tool("okta", "okta_directory_read") and "okta:write" in gateway_lane.MANAGED_SCOPES

    # A token Okta refuses is stored nowhere, and a domain that is not Okta's is refused before it is asked.
    def refuse(name, domain, token):
        raise admin_integrations.ConnectRefused(f"{name} did not accept the API token (HTTP 401)")
    monkeypatch.setattr(admin_integrations, "_okta_probe", refuse)
    stored = len(sero.puts)
    r = client.post("/api/admin/integrations", json=body, headers=headers)
    assert r.status_code == 409 and "did not accept" in r.json()["error"] and len(sero.puts) == stored
    for domain in ("example.com", "example.okta.com.evil.test", "okta.com"):
        r = client.post("/api/admin/integrations", json={**body, "identity": {"domain": domain}}, headers=headers)
        assert r.status_code == 409 and "organization's domain" in r.json()["error"], domain
    r = client.post("/api/admin/integrations", json={**body, "secrets": {}}, headers=headers)
    assert r.status_code == 409 and "API token" in r.json()["error"]
    assert len(sero.puts) == stored


def test_the_okta_probe_sends_the_token_only_to_the_named_domain_and_never_follows_a_redirect(monkeypatch):
    """Gate (secrets): the connect check reads one user at the organization's own address with the
    token as Okta takes it; a redirect is an answer, not a place to send the token next, and a
    refusal never repeats the token."""
    from kobo_api import admin_integrations
    calls: list[dict] = []
    answer = {"status": 200}

    def fake_get(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return httpx.Response(answer["status"], headers={"Location": "https://elsewhere.test/"})
    monkeypatch.setattr(admin_integrations.httpx, "get", fake_get)
    admin_integrations.probe_okta("Okta", "example.okta.com", "00-okta-token")
    assert calls[0]["url"] == "https://example.okta.com/api/v1/users" and calls[0]["follow_redirects"] is False
    assert calls[0]["headers"]["Authorization"] == "SSWS 00-okta-token"
    for answer["status"] in (302, 401, 403, 404):
        with pytest.raises(admin_integrations.ConnectRefused) as refused:
            admin_integrations.probe_okta("Okta", "example.okta.com", "00-okta-token")
        assert "00-okta-token" not in str(refused.value) and "nothing was connected" in str(refused.value)


def test_an_atlassian_service_account_key_is_checked_as_a_bearer_then_served_as_one(tmp_path, fake_storage, monkeypatch):
    """Gate (integrity): a service account's API key is the credential shape that belongs to no
    person; Atlassian's server takes it only as a Bearer (a user's token only as Basic). The room
    checks the key that way before it saves, stores the key alone, and the target sends it as the
    gateway's default bearer scheme, with the same per-agent session and scopes as the user shape."""
    from kobo_api import admin_integrations
    probes: list[tuple] = []
    monkeypatch.setattr(admin_integrations, "_bearer_probe", lambda name, address, token: probes.append((name, address, token)))
    bab = FakeBab(integrations=[])
    client, headers = _access_client(tmp_path, fake_storage, bab=bab)
    admin = FakeLiminalAdmin()
    client.app.state.clients.liminal_admin = admin
    sero = client.app.state.clients.sero
    body = {"vendor": "atlassian", "name": "Atlassian", "base_access": "read", "auth": "token",
            "identity": {}, "secrets": {"TOKEN": "key-1"}, "address": "https://mcp.atlassian.com/v2/mcp"}

    # A key the server refuses is stored nowhere (the probe is per vendor: only Atlassian's server is asked).
    def refuse(name, address, token):
        raise admin_integrations.ConnectRefused(f"{name} did not accept the service account API key (HTTP 401)")
    monkeypatch.setattr(admin_integrations, "_bearer_probe", refuse)
    r = client.post("/api/admin/integrations", json=body, headers=headers)
    assert r.status_code == 409 and "service account API key" in r.json()["error"]
    assert sero.puts == [] and bab.integrations("x") == []

    monkeypatch.setattr(admin_integrations, "_bearer_probe", lambda name, address, token: probes.append((name, address, token)))
    row = client.post("/api/admin/integrations", json=body, headers=headers).json()
    assert row["state"] == "applied" and "accepted the service account API key" in row["state_detail"], row
    assert probes == [("Atlassian", "https://mcp.atlassian.com/v2", "key-1")]
    target = admin.targets["atlassian"]
    assert target["auth_method"] == "bearer" and "auth_scheme" not in target
    assert target["session"] == "per-persona" and target["required_scope"] == "atlassian:read"
    assert target["tool_scopes"]["editJiraIssue"] == "atlassian:jira-write" and target["tool_scopes"]["createJiraIssue"] == "atlassian:never"
    (put,) = [p for p in sero.puts if p[1] == "apps/kobo/integrations/atlassian"]
    assert put[2] == {"TOKEN": "key-1"}
    # A token vendor without an MCP server to ask is kept without a probe, as before.
    r = client.post("/api/admin/integrations", json={"vendor": "fastly", "name": "Fastly", "base_access": "read", "auth": "token",
                                                      "identity": {}, "secrets": {"TOKEN": "f-1"}, "address": "https://mcp.fastly.example"}, headers=headers)
    assert r.status_code in (200, 201) and len(probes) == 1


class _McpServer:
    """A fake of Atlassian's MCP server behind `httpx.post`: initialize opens a
    session; the scope probe answers with `tool_text` (a tool result's text)."""

    def __init__(self, tool_text: str | None, *, stream: bool = False):
        self.tool_text, self.stream, self.calls = tool_text, stream, []

    def __call__(self, url, *, json=None, timeout=None, headers=None):
        self.calls.append((json or {}).get("method"))
        import json as _json
        method = (json or {}).get("method")
        if method == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}}, headers={"Mcp-Session-Id": "s-1"})
        if method == "tools/call":
            body = {"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": self.tool_text}]}}
            if self.tool_text is None:
                body = {"jsonrpc": "2.0", "id": 2, "error": {"code": -32602, "message": "unknown tool"}}
            if self.stream:
                return httpx.Response(200, text="event: message\ndata: " + _json.dumps(body) + "\n\n",
                                      headers={"content-type": "text/event-stream"})
            return httpx.Response(200, json=body)
        return httpx.Response(202)


def test_the_atlassian_probe_refuses_a_token_the_mcp_server_takes_but_cannot_use(monkeypatch):
    """Gate (integrity): Atlassian's server opens a session for any valid API token and only
    refuses the first tool call when the token lacks the scopes it reads (a classic token, or
    one made under the Jira app). The room's probe makes that call, so the sheet refuses with
    a sentence about where the token is made instead of the agent failing later in chat."""
    import httpx as _httpx

    from kobo_api import admin_integrations

    refused = _McpServer('{"error":true,"message":"Unable to resolve user scopes from the user-context token for '
                         '\\"getAccessibleAtlassianResources\\". The session token is missing the scope claim required to authorize this operation.","statusCode":401}')
    monkeypatch.setattr(admin_integrations.httpx, "post", refused)
    with pytest.raises(admin_integrations.ConnectRefused, match="none of the scopes its MCP server reads"):
        admin_integrations.probe_bearer_mcp("Atlassian", "https://mcp.example/v2", "key-1")
    assert refused.calls == ["initialize", "notifications/initialized", "tools/call"]

    # A token with scopes answers with data (here over an event stream): the probe passes.
    good = _McpServer('{"data":{"resources":[{"cloudId":"c-1","url":"https://acme.atlassian.net"}]}}', stream=True)
    monkeypatch.setattr(admin_integrations.httpx, "post", good)
    admin_integrations.probe_basic_mcp("Atlassian", "https://mcp.example/v2", "bot@example.com", "tok-1")
    # A server without that tool is not a refusal either.
    monkeypatch.setattr(admin_integrations.httpx, "post", _McpServer(None))
    admin_integrations.probe_bearer_mcp("Atlassian", "https://mcp.example/v2", "key-1")
    # The door itself still refuses first.
    def shut(url, **kwargs):
        return _httpx.Response(401, json={"error": "invalid_token"})
    monkeypatch.setattr(admin_integrations.httpx, "post", shut)
    with pytest.raises(admin_integrations.ConnectRefused, match="did not accept the service account API key"):
        admin_integrations.probe_bearer_mcp("Atlassian", "https://mcp.example/v2", "key-1")


def test_the_lane_sync_retires_the_token_broker_github_entries_an_old_bundle_still_lists(tmp_path, fake_storage):
    """Gate (integrity): the room's GitHub token broker is gone (kobo#365) and its Jira read lane
    retired (2026-11-04); an agent whose bundle still allows their tools by name (github.search_prs,
    github.open_pr, jira.get_issue) is offered dead tools. The next lane sync withdraws them under
    the retired prefixes, and says so."""
    import yaml as _yaml

    bab = FakeBab(integrations=[{"integration": "datadog", "vendor": "datadog", "ceiling": ["datadog_monitors_read"],
                                 "created_at": "2026-10-29T12:00:00", "updated_at": "2026-10-29T12:00:00"}])
    bare = ("name: ledger\ncard: agent-card.json\npersona: persona.md\n"
            "transports:\n  - {kind: a2a, host: 0.0.0.0, port: 8080}\n"
            "tools: {provider: colleagues, liminal: {url: http://liminal.test, token_env: CABAL_COLLEAGUE_TOKEN, exchange_url: http://cabal.test}, "
            "allow: [colleagues.list_colleagues, github.search_prs, github.open_pr, jira.get_issue]}\nmemory: {personal: techne, home: /techne}\n")
    github = FakeGitHub(existing_repos={"example-org/agent-ledger"}, extra_clone_files={"bundle/agent.yaml": bare}, track_pushed_content=True)
    c, h = _room(tmp_path, fake_storage, bab=bab, github=github)
    c.app.state.clients.liminal_admin = FakeLiminalAdmin([GatewayGrant(issuer="cabal-core", persona="ledger", scopes=("x:read",), allowed_handlers=("scratchpad",))])
    rid = c.post("/api/agents/ledger/access/requests", headers=h,
                 json={"integration": "datadog", "permissions": ["datadog_monitors_read"], "note": "monitors"}).json()["id"]
    out = c.post("/api/admin/access/decide", headers=h, json={"ids": [rid], "verdict": "approve"}).json()["results"]
    assert out[0]["status"] == "approved"
    pushed = _yaml.safe_load(github.pushed_file("bundle/agent.yaml"))
    # The broker entries and the retired jira lane's entry go; the rest stays.
    assert pushed["tools"]["allow"] == ["colleagues.list_colleagues", "datadog-mcp.*", "datadog.*"], pushed["tools"]
    assert github.pushes[-1][1] == "kobo: allow the datadog tools; withdraw the github, jira tools"


def test_removing_an_integration_twice_is_not_a_refusal(tmp_path, fake_storage):
    """Gate (integrity): a second Remove while the first is still opening the gateway's pull
    request finds nothing on either side; that is "already removed", not an error."""
    client, headers = _access_client(tmp_path, fake_storage, bab=FakeBab(integrations=[]))
    response = client.delete("/api/admin/integrations/datadog", headers=headers)
    assert response.status_code == 200 and response.json()["access"] == "already removed", response.text


def test_the_gateway_lane_follows_the_datadog_permission(tmp_path, fake_storage):
    """Gate (authorization): approving an agent's first Datadog permission opens its lane on the
    gateway (scope `datadog:read`, handlers `datadog` and `datadog-mcp`) beside what its grant
    already held; revoking the last one closes it and leaves the rest alone (kobo#327)."""
    import yaml as _yaml

    bab = FakeBab(integrations=[{"integration": "datadog", "vendor": "datadog", "ceiling": ["datadog_monitors_read"],
                                 "created_at": "2026-10-29T12:00:00", "updated_at": "2026-10-29T12:00:00"}])
    bare = ("name: ledger\ncard: agent-card.json\npersona: persona.md\n"
            "transports:\n  - {kind: a2a, host: 0.0.0.0, port: 8080}\n"
            "tools: {provider: colleagues, liminal: {url: http://liminal.test, token_env: CABAL_COLLEAGUE_TOKEN, exchange_url: http://cabal.test}, "
            "allow: [colleagues.list_colleagues]}\nmemory: {personal: techne, home: /techne}\n")
    github = FakeGitHub(existing_repos={"example-org/agent-ledger"}, extra_clone_files={"bundle/agent.yaml": bare}, track_pushed_content=True)
    c, h = _room(tmp_path, fake_storage, bab=bab, github=github)
    admin = FakeLiminalAdmin([GatewayGrant(issuer="cabal-core", persona="ledger", scopes=("x:read",), allowed_handlers=("scratchpad",))])
    c.app.state.clients.liminal_admin = admin
    rid = c.post("/api/agents/ledger/access/requests", headers=h,
                 json={"integration": "datadog", "permissions": ["datadog_monitors_read"], "note": "monitors"}).json()["id"]

    out = c.post("/api/admin/access/decide", headers=h, json={"ids": [rid], "verdict": "approve"}).json()["results"]

    assert out[0]["status"] == "approved"
    lane = admin.grants[("cabal-core", "ledger")]
    assert set(lane.scopes) == {"x:read", "datadog:read"}
    assert set(lane.allowed_handlers) == {"scratchpad", "datadog", "datadog-mcp"}
    # The approve also opens the bundle: the runtime offers a gateway tool only when the
    # allow list matches it, so the vendor's patterns are pushed once (one restart).
    pushed = _yaml.safe_load(github.pushed_file("bundle/agent.yaml"))
    assert pushed["tools"]["allow"] == ["colleagues.list_colleagues", "datadog-mcp.*", "datadog.*"], pushed["tools"]
    assert github.pushes[-1][1] == "kobo: allow the datadog tools"
    (grant,) = bab.grants("ledger", "x")
    assert c.delete(f"/api/agents/ledger/access/grants/{grant['id']}", headers=h).status_code == 200
    lane = admin.grants[("cabal-core", "ledger")]
    assert set(lane.scopes) == {"x:read"} and set(lane.allowed_handlers) == {"scratchpad"}
    assert c.post("/api/admin/access/sync", headers=h).json()["gateway_lanes"] == {"set": 0, "unchanged": 1, "failed": 0}
    # A bundle that already admits the vendor's tools is left alone on a later approve.
    already = bare.replace("allow: [colleagues.list_colleagues]", "allow: [colleagues.list_colleagues, datadog.*, datadog-mcp.*]")
    github3 = FakeGitHub(existing_repos={"example-org/agent-ledger"}, extra_clone_files={"bundle/agent.yaml": already}, track_pushed_content=True)
    bab3 = FakeBab(integrations=[{"integration": "datadog", "vendor": "datadog", "ceiling": ["datadog_monitors_read"],
                                  "created_at": "2026-10-29T12:00:00", "updated_at": "2026-10-29T12:00:00"}])
    c3, h3 = _room(tmp_path / "three", fake_storage, bab=bab3, github=github3)
    c3.app.state.clients.liminal_admin = FakeLiminalAdmin([])
    rid3 = c3.post("/api/agents/ledger/access/requests", headers=h3,
                   json={"integration": "datadog", "permissions": ["datadog_monitors_read"], "note": "monitors"}).json()["id"]
    c3.post("/api/admin/access/decide", headers=h3, json={"ids": [rid3], "verdict": "approve"})
    assert github3.push_count == 0


# --- from test_composer.py ---


_RUNTIME_IMAGE = "registry.example/agents/daedalus-runtime:latest"


def _make_spec(
    skills: list[StagedSkill] | None = None,
    *,
    name: str = "sam",
    team: str = "shared",
    group: str | None = None,
    purpose_tags: list[str] | None = None,
    capability: str | None = None,
    colleagues: list[dict] | None = None,
    cabal_url: str | None = None,
    memory_replica_bucket: str = "",
    liminal_url: str = "",
    archetype: str | None = None,
) -> BundleSpec:
    return BundleSpec(
        name=name,
        persona_body=f"You are {name}, a helpful assistant.\n",
        model_id="vertex_ai/claude-sonnet-4-5",
        skills=skills or [],
        runtime_image=_RUNTIME_IMAGE,
        vertex_project="example-model-project",
        vertex_location="us-east5",
        team=team,
        group=group,
        purpose_tags=purpose_tags if purpose_tags is not None else [],
        capability=capability,
        colleagues=colleagues if colleagues is not None else [],
        cabal_url=cabal_url,
        memory_replica_bucket=memory_replica_bucket,
        liminal_url=liminal_url,
        archetype=archetype,
    )


def test_team_guide_archetype_composes_no_github_and_no_liminal(tmp_path):
    """Gate (authorization): Team guide archetype composes no github and no liminal."""
    dest = tmp_path / "out"

    compose(
        _make_spec(
            name="atlas",
            cabal_url="http://cabal.example.com",
            liminal_url="http://liminal.example.com",
            archetype="team_guide",
        ),
        dest,
    )

    text = (dest / "bundle" / "agent.yaml").read_text()
    data = yaml.safe_load(text)
    tools = data["tools"]
    # The kiosk's tools block carries exactly the colleagues trio -- no
    # sidecar key can ride in, structurally, because the compose-time gate
    # returns before either sidecar arm runs (memory lives as its own
    # sibling stanza on `data`, not inside `tools`).
    assert set(tools) == {"provider", "allow", "colleagues"}
    # Colleagues survive untouched -- the guide still talks.
    assert tools["provider"] == "colleagues"
    # It never acts, in Slack either: no Slack tools (issue #484).
    assert "slack.*" not in tools["allow"]
    assert tools["colleagues"]["url"] == "http://cabal.example.com"
    # No action surface at all: no sidecar blocks, no github allow entries.
    assert "github" not in tools
    assert "liminal" not in tools
    assert not any(t.startswith("github.") for t in tools["allow"])
    assert tools["allow"] == [
        "colleagues.list_colleagues",
        "colleagues.ask_colleague",
        "colleagues.remember",
    ]
    # Not merely absent from the parsed mapping: absent from the bytes.
    assert "github" not in text
    assert "liminal" not in text
    # Memory is untouched by the suppression.
    assert data["memory"]["personal"] == "techne"


# ---- GitHub through the permission service (kobo#338) ----------------------------

GITHUB_APP_BODY = {"vendor": "github", "name": "GitHub", "base_access": "read", "auth": "github_app",
                   "identity": {}, "secrets": {}}
GITHUB_LEVELS = ["github_pulls_review", "github_pulls_write", "github_repos_read"]
PEM = "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----"


def _room_store(client):
    """The room's own Secret store (kobo#377), over the test client's kubectl."""
    from kobo_api.room_services import RoomServices
    store = RoomServices(client.app.state.clients.kubectl, "kobo", client.app.state.config)
    client.app.state.clients.room_services = store
    return store


def _room_github_app(client, app_id="4487276", installation_id="151256501"):
    """The app the room already holds (the deployment config's, or a row being replaced)."""
    store = _room_store(client)
    store.put("github", {"app_id": app_id, "installation_id": installation_id, "private_key": PEM}, actor="ops@example.com")


def test_a_github_connect_uses_the_rooms_app_writes_the_secret_and_puts_the_github_mcp_target_live(tmp_path, fake_storage):
    """Wire shape (kobo#338, GitHub on the Datadog template; kobo#365): a connect with the app
    fields blank takes the room's own GitHub App, writes the permission service's `bab-github`
    Secret, probes the installation, connects the integration in the service with EVERY github
    permission as its ceiling (the levels are each agent's to ask), and PUTs two targets through
    the admin plane with no pull request: `github-mcp` (service-token, read-only on every request,
    `github:read`) and `github-mcp-write` (no read-only header, `github:review`, the creating
    tools behind `github:write`, merge and delete behind a scope no lane grants). An older
    `repositories` field pasted at connect time is dropped: the set is per agent now. Remove
    takes all of it back, both targets included."""
    from kobo_api.admin_integrations import github_target_spec, github_write_target_spec
    from kobo_api.models import AdminIntegration
    admin = FakeLiminalAdmin()
    github = FakeGitHub(existing_repos={GATEWAY_REPO}, extra_clone_files={"registry.toml": REGISTRY}, track_pushed_content=True)
    bab = FakeBab(integrations=[])
    client, headers = _access_client(tmp_path, fake_storage, github=github, bab=bab)
    client.app.state.clients.liminal_admin = admin
    _room_github_app(client)
    kubectl = client.app.state.clients.kubectl
    r = client.post("/api/admin/integrations", json={**GITHUB_APP_BODY, "identity": {"repositories": "infra, web"}}, headers=headers)
    assert r.status_code == 201, r.text
    row = r.json()
    assert row["state"] == "applied" and "live on the gateway" in row["state_detail"], row
    assert "GitHub agreed" in row["state_detail"] and "3 repositories" in row["state_detail"]
    assert "picks its repositories on its Access tab" in row["state_detail"]
    assert row["spec"]["identity"] == {"app_id": "4487276", "installation_id": "151256501", "source": "room", "account": "acme"}
    assert row["spec"]["gateway"] == "live" and "pull_request" not in row["spec"] and row["spec"]["secret_keys"] == []
    # The connection's credential is the room's own Secret (kobo#377): the room's pull requests read it there.
    assert row["credential_ref"] == "secret:kobo/kobo-room-github-app"
    assert kubectl.secrets[("kobo", "kobo-room-github-app")]["private_key"] == PEM
    assert github.open_pr_calls == []
    # The Secret the minter reads, the probe it answered, the integration it holds.
    assert kubectl.secrets[("bab", "bab-github")] == {"GITHUB_APP_ID": "4487276", "GITHUB_APP_INSTALLATION_ID": "151256501",
                                                      "GITHUB_APP_PRIVATE_KEY": PEM}
    assert bab.github_probes == ["151256501"]
    held = next(i for i in bab._integrations if i["integration"] == row["id"])
    assert held["vendor"] == "github" and held["ceiling"] == GITHUB_LEVELS and held["spec"]["account"] == "acme"
    assert "repositories" not in held["spec"]
    # The targets, exactly as the spec builders say, and never the key.
    model = AdminIntegration(id=row["id"], vendor="github", name="GitHub", base_access="read",
                             created_by="ops@example.com", spec=row["spec"])
    assert set(admin.targets) == {"github-mcp", "github-mcp-write"}
    target = admin.targets["github-mcp"]
    assert target == github_target_spec(model)
    assert target["auth_method"] == "service-token" and target["credential_key"] == "GITHUB_TOKEN"
    assert target["required_scope"] == "github:read" and target["headers"] == {"X-MCP-Readonly": "true"}
    assert target["catalog_token_provider"] == "github-app" and "catalog_headers_env" not in target
    assert target["address"] == "https://api.githubcopilot.com" and "abc" not in str(admin.targets)
    write = admin.targets["github-mcp-write"]
    assert write == github_write_target_spec(model)
    assert write["required_scope"] == "github:review" and "headers" not in write
    assert {k: v for k, v in write.items() if k not in ("required_scope", "tool_scopes")} == \
        {k: v for k, v in target.items() if k not in ("required_scope", "headers")}
    assert write["tool_scopes"]["create_branch"] == "github:write" and write["tool_scopes"]["push_files"] == "github:write"
    assert write["tool_scopes"]["merge_pull_request"] == "github:never" and write["tool_scopes"]["delete_file"] == "github:never"
    # The prose-carrying tools are closed on the vendor's server: the agent makes comments,
    # reviews and new pull requests through the gateway's signing `github-write` handler.
    assert write["tool_scopes"]["create_pull_request"] == "github:never"
    assert write["tool_scopes"]["add_issue_comment"] == "github:never"
    assert write["tool_scopes"]["pull_request_review_write"] == "github:never"
    out = client.delete(f"/api/admin/integrations/{row['id']}", headers=headers).json()
    assert out["gateway"] == "removed from the gateway" and admin.targets == {}
    assert out["secret"] == "removed" and ("bab", "bab-github") not in kubectl.secrets
    assert ("kobo", "kobo-room-github-app") not in kubectl.secrets
    assert github.open_pr_calls == []


def test_a_github_connect_refuses_without_an_app_or_with_half_of_one_or_a_refused_installation(tmp_path, fake_storage):
    """Gate (authorization): no room app and nothing pasted is a refusal that says where to go;
    half an app is a refusal; an installation GitHub turns down takes the Secret back."""
    admin = FakeLiminalAdmin()
    bab = FakeBab(integrations=[])
    client, headers = _access_client(tmp_path, fake_storage, bab=bab)
    client.app.state.clients.liminal_admin = admin
    _room_store(client)
    kubectl = client.app.state.clients.kubectl
    r = client.post("/api/admin/integrations", json=GITHUB_APP_BODY, headers=headers)
    assert r.status_code == 409 and "paste the app id" in r.json()["error"]
    half = {**GITHUB_APP_BODY, "identity": {"app_id": "1", "installation_id": "2"}}
    r = client.post("/api/admin/integrations", json=half, headers=headers)
    assert r.status_code == 409 and "together" in r.json()["error"]
    letters = {**GITHUB_APP_BODY, "identity": {"app_id": "one", "installation_id": "2"}, "secrets": {"PRIVATE_KEY": PEM}}
    r = client.post("/api/admin/integrations", json=letters, headers=headers)
    assert r.status_code == 409 and "numbers" in r.json()["error"]
    pasted = {**GITHUB_APP_BODY, "identity": {"app_id": "1", "installation_id": "2999"}, "secrets": {"PRIVATE_KEY": PEM}}
    r = client.post("/api/admin/integrations", json=pasted, headers=headers)
    assert r.status_code == 409 and "connect again" in r.json()["error"], r.text
    assert ("bab", "bab-github") not in kubectl.secrets and ("bab", "bab-github") in {(ns, n) for ns, n, _ in kubectl.secrets_applied}
    # The room's own copy is taken back with it: a refused app connects nothing anywhere.
    assert ("kobo", "kobo-room-github-app") not in kubectl.secrets
    assert admin.targets == {} and bab._integrations == []


def test_a_github_grant_opens_the_lane_its_level_names():
    """Gate (authorization, kobo#365): GitHub's lane is per permission. Read opens the read-only
    target; Review pull requests opens the write target; Open pull requests adds the scope the
    creating tools want. The gateway's `github` handler is in no lane and is withdrawn."""
    from kobo_api.gateway_lane import (
        GATEWAY_LANES,
        MANAGED_HANDLERS,
        PERMISSION_LANES,
        lane_for,
    )
    assert "github" not in GATEWAY_LANES
    read, review, write = (PERMISSION_LANES[("github", p)] for p in ("github_repos_read", "github_pulls_review", "github_pulls_write"))
    assert (read.scopes, read.handlers, read.allow) == (("github:read",), ("github-mcp",), ("github-mcp.*",))
    # Review and Open pull requests open the vendor's write target AND the gateway's signing
    # `github-write` handler (comments, reviews and new pull requests carry the agent's name).
    assert (review.scopes, review.handlers, review.allow) == (
        ("github:review",), ("github-mcp-write", "github-write"), ("github-mcp-write.*", "github-write.*"))
    assert (write.scopes, write.handlers, write.allow) == (
        ("github:review", "github:write"), ("github-mcp-write", "github-write"), ("github-mcp-write.*", "github-write.*"))
    assert lane_for("datadog", "datadog_monitors_read") is GATEWAY_LANES["datadog"] and lane_for("github", "unknown") is None
    assert "github" in MANAGED_HANDLERS and not any("github" in lane.handlers for lane in PERMISSION_LANES.values())


# ---- GitHub per agent: a repository set, then a level (kobo#365) -----------------

GITHUB_CONNECTION = {"integration": "github", "vendor": "github", "kind": "connection", "name": "GitHub",
                     "ceiling": GITHUB_LEVELS, "spec": {"installation_id": "151256501", "name": "GitHub"},
                     "created_at": "2026-10-29T12:00:00", "updated_at": "2026-10-29T12:00:00"}
_BARE_AGENT_YAML = ("name: ledger\ncard: agent-card.json\npersona: persona.md\n"
                    "transports:\n  - {kind: a2a, host: 0.0.0.0, port: 8080}\n"
                    "tools: {provider: colleagues, liminal: {url: http://liminal.test, token_env: CABAL_COLLEAGUE_TOKEN, "
                    "exchange_url: http://cabal.test}, allow: [colleagues.list_colleagues, github.*]}\n"
                    "memory: {personal: techne, home: /techne}\n")


def _github_room(tmp_path, fake_storage, *, connected=True):
    bab = FakeBab(integrations=[dict(GITHUB_CONNECTION)] if connected else [])
    github = FakeGitHub(existing_repos={"example-org/agent-ledger", "acme/infra", "acme/web"},
                        extra_clone_files={"bundle/agent.yaml": _BARE_AGENT_YAML}, track_pushed_content=True)
    c, h = _room(tmp_path, fake_storage, bab=bab, github=github)
    admin = FakeLiminalAdmin([GatewayGrant(issuer="cabal-core", persona="ledger", scopes=("x:read",),
                                           allowed_handlers=("scratchpad", "github"))])
    c.app.state.clients.liminal_admin = admin
    return c, h, bab, github, admin


def test_an_assistant_reads_an_agents_access_picks_its_repositories_and_asks(tmp_path, fake_storage):
    """Wire shape (the room's MCP tools): the Access tab from chat. The read
    carries what the agent holds, what waits, what it can ask for, and its
    repository set, with a search over the room's repositories in place of
    the whole list. The pick and the ask go through the page's own routes, so
    the request waits for a reviewer exactly as one made on the page does.
    A refusal that is about the room or the request says so; one about who
    is asking is "agent not found"."""
    on = MCPConfig(
        allow_without_iap=True, oauth_client_id="cid", workspace_domain="example.com",
        issuer_base_url="https://room.example.com",
    )
    c, _, bab, _, _ = _github_room(tmp_path, fake_storage)
    c.app.state.config = c.app.state.config.model_copy(update={"mcp": on})
    mcp = build_mcp_server(c.app)
    ops = _ctx_for("ops@example.com")

    page = _call_tool(mcp, "kobo_get_agent_access", ops, agent="ledger", repository_search="ACME")
    assert (page["grants"], page["pending"], page["can_request"]) == ([], [], True)
    assert "github" in {v["id"] for v in page["vendors"]}
    assert page["github"] == {"available": True, "selected": [], "waiting": None, "total": 3, "note": None,
                              "matches": ["acme/infra", "acme/web"]}

    saved = _call_tool(mcp, "kobo_set_agent_repositories", ops, agent="ledger", repositories=["acme/web"])
    assert saved == {"agent": "ledger", "repositories": ["acme/web"], "changed": True, "waiting": None}
    asked = _call_tool(mcp, "kobo_request_access", ops, agent="ledger", integration="github-ledger",
                       permissions=["github_repos_read"], note="reads the web app's code")
    after = _call_tool(mcp, "kobo_get_agent_access", ops, agent="ledger")
    assert [p["id"] for p in after["pending"]] == [asked["requested"]["id"]] and after["grants"] == []
    assert after["github"]["selected"] == ["acme/web"] and "matches" not in after["github"]
    assert bab.grants("ledger", "x") == []  # asking grants nothing

    with pytest.raises(ToolError, match="acme/nope"):
        _call_tool(mcp, "kobo_set_agent_repositories", ops, agent="ledger", repositories=["acme/nope"])
    with pytest.raises(ToolError, match="agent not found"):
        _call_tool(mcp, "kobo_get_agent_access", ops, agent="ghost")
    with pytest.raises(ToolError, match="agent not found"):
        _call_tool(mcp, "kobo_request_access", _ctx_for("stranger@example.com"), agent="ledger",
                   integration="github-ledger", permissions=["github_repos_read"])

    off, _ = _room(tmp_path / "off", fake_storage, bab=None, mcp=on)
    with pytest.raises(ToolError, match="access is off for this room"):
        _call_tool(build_mcp_server(off.app), "kobo_get_agent_access", ops, agent="ledger")


def test_the_access_tab_shows_github_as_the_agents_own_repository_set(tmp_path, fake_storage):
    """Gate (authorization) and wire shape (kobo#365): with no set picked, GitHub shows as the
    connection with an empty set; the owner lists the installation's repositories and picks
    some; the room writes the child `<connection>-<agent>` under the connection with every
    level in its ceiling, and from then on the Access tab shows the child, never the
    connection and never another agent's child. Strays, an empty set and a room without
    GitHub are refused; the room-wide picker still shows the connection alone. The listing
    comes from the room's warm index (kobo#368): one cold read fills it, the save never walks
    GitHub again, the payload carries `total`, and a room whose index holds a listing never
    asks the client at all."""
    c, h, bab, github, _ = _github_room(tmp_path, fake_storage)
    page = c.get("/api/agents/ledger/access", headers=h).json()
    gh = next(v for v in page["vendors"] if v["id"] == "github")
    assert gh["connected"] is True  # a parentless connection exists (kobo#371)
    assert gh["accounts"] == [{"id": "github", "name": "GitHub", "connected": True, "ceiling": GITHUB_LEVELS, "organization": None,
                               "account_id": None, "repositories": [], "has_repo_set": False}]
    assert {p["name"]: p["label"] for g in gh["groups"] for p in g["permissions"]} == {
        "github_repos_read": "Read", "github_pulls_review": "Review pull requests", "github_pulls_write": "Open pull requests"}
    assert all("label" in p for v in page["vendors"] for g in v["groups"] for p in g["permissions"])

    listing = c.get("/api/agents/ledger/github/repositories", headers=h).json()
    assert listing["available"] is True and listing["selected"] == [] and listing["note"] is None
    assert [r["full_name"] for r in listing["repositories"]] == ["acme/infra", "acme/web", "example-org/agent-ledger"]
    assert listing["total"] == 3
    assert set(listing["repositories"][0]) == {"full_name", "private", "default_branch"}
    assert github.calls.count("list_installation_repos") == 1

    r = c.put("/api/agents/ledger/github/repositories", headers=h, json={"repositories": ["acme/web", "acme/infra"]})
    assert r.status_code == 200, r.text
    assert r.json() == {"integration": "github-ledger", "repositories": ["acme/infra", "acme/web"], "changed": True}
    child = next(i for i in bab.integrations("x") if i["integration"] == "github-ledger")
    assert child["parent"] == "github" and child["vendor"] == "github" and child["ceiling"] == GITHUB_LEVELS
    assert child["spec"] == {"repositories": ["acme/infra", "acme/web"], "name": "GitHub for ledger", "installation_id": "151256501"}
    assert c.get("/api/agents/ledger/github/repositories", headers=h).json()["selected"] == ["acme/infra", "acme/web"]
    page = c.get("/api/agents/ledger/access", headers=h).json()
    gh = next(v for v in page["vendors"] if v["id"] == "github")
    assert gh["accounts"] == [{"id": "github-ledger", "name": "GitHub", "connected": True, "ceiling": GITHUB_LEVELS,
                               "organization": None, "account_id": None, "repositories": ["acme/infra", "acme/web"],
                               "has_repo_set": True}]
    # Another agent's child is nobody else's business; the room-wide picker shows the connection.
    bab.put_integration("github-other", "github", {"repositories": ["acme/web"], "name": "GitHub for other"}, GITHUB_LEVELS, "x",
                        parent="github")
    gh = next(v for v in c.get("/api/agents/ledger/access", headers=h).json()["vendors"] if v["id"] == "github")
    assert [a["id"] for a in gh["accounts"]] == ["github-ledger"]
    gh = next(v for v in c.get("/api/access/picker", headers=h).json()["vendors"] if v["id"] == "github")
    assert [a["id"] for a in gh["accounts"]] == ["github"]

    same = c.put("/api/agents/ledger/github/repositories", headers=h, json={"repositories": ["acme/infra", "acme/web"]})
    assert same.json()["changed"] is False
    # Three reads and two saves so far, one walk of GitHub: the rest came out of the warm listing.
    assert github.calls.count("list_installation_repos") == 1

    class WarmIndex:
        def listing(self, now=None):
            return [InstallationRepo(full_name="acme/warm", archived=False, private=True, default_branch="main", pushed_at="")]

    c.app.state.repo_index = WarmIndex()
    warm = c.get("/api/agents/ledger/github/repositories", headers=h).json()
    assert [r["full_name"] for r in warm["repositories"]] == ["acme/warm"] and warm["total"] == 1
    assert c.put("/api/agents/ledger/github/repositories", headers=h, json={"repositories": ["acme/warm"]}).status_code == 200
    assert github.calls.count("list_installation_repos") == 1
    c.app.state.repo_index = RepoIndex(github)
    stray = c.put("/api/agents/ledger/github/repositories", headers=h, json={"repositories": ["acme/infra", "acme/nope"]})
    assert stray.status_code == 422 and stray.json()["strays"] == ["acme/nope"] and "acme/nope" in stray.json()["error"]
    assert c.put("/api/agents/ledger/github/repositories", headers=h, json={"repositories": []}).status_code == 422
    assert c.put("/api/agents/ledger/github/repositories", headers=h, json={"repositories": ["not-a-repo"]}).status_code == 422

    c2, h2, _, _, _ = _github_room(tmp_path / "none", fake_storage, connected=False)
    assert c2.put("/api/agents/ledger/github/repositories", headers=h2, json={"repositories": ["acme/infra"]}).status_code == 404
    off = c2.get("/api/agents/ledger/github/repositories", headers=h2).json()
    assert off["available"] is False and off["repositories"] == [] and "Admin > Integrations" in off["note"]


def test_a_repository_added_to_a_set_the_agent_holds_a_level_on_waits_for_a_reviewer(tmp_path, fake_storage):
    """Gate (authorization, bab 0.4.22): an owner who is no reviewer picks any repository the
    installation carries. Before the agent holds a level the pick lands. Once it does, what
    the pick removes lands and what it adds waits: the save answers 202 with the set in force
    and `waiting`, the picker's listing and the Access tab's waiting rows carry it, and the
    Review pane shows the request with what it would add beside the set as it stands.
    Approving applies it to the set and follows no gateway lane (no permission changed);
    saving the set without the addition withdraws what waited."""
    c, h, bab, _, admin = _github_room(tmp_path, fake_storage)
    bab.reviewers = {"sec@example.com"}  # the signed-in owner is on neither team

    def decide(rid):  # a reviewer's turn: the same sign-in, on the team for the one call
        bab.reviewers.add("ops@example.com")
        try:
            return c.post("/api/admin/access/decide", headers=h, json={"ids": [rid], "verdict": "approve"})
        finally:
            bab.reviewers.discard("ops@example.com")

    assert c.put("/api/agents/ledger/github/repositories", headers=h, json={"repositories": ["acme/infra"]}).status_code == 200
    rid = c.post("/api/agents/ledger/access/requests", headers=h,
                 json={"integration": "github-ledger", "permissions": ["github_repos_read"], "note": "read"}).json()["id"]
    assert decide(rid).status_code == 200
    lanes = len(admin.writes)

    r = c.put("/api/agents/ledger/github/repositories", headers=h, json={"repositories": ["acme/web", "acme/infra"]})
    assert r.status_code == 202, r.text
    waiting = {"id": rid + 1, "added": ["acme/web"], "removed": [], "requested_by": "ops@example.com",
               "created_at": "2026-10-29T12:00:00"}
    assert r.json() == {"integration": "github-ledger", "repositories": ["acme/infra"], "changed": False, "waiting": waiting}
    listing = c.get("/api/agents/ledger/github/repositories", headers=h).json()
    assert listing["selected"] == ["acme/infra"] and listing["waiting"] == waiting
    row = next(p for p in c.get("/api/agents/ledger/access", headers=h).json()["pending"] if p["kind"] == "repositories")
    assert row["proposal"] == {"added": ["acme/web"], "removed": []} and row["permissions"] == ["github_repos_read"]
    review = next(p for p in c.get("/api/admin/access/requests", headers=h).json()["requests"] if p["id"] == rid + 1)
    assert review["proposal"]["added"] == ["acme/web"] and review["repositories"] == ["acme/infra"]
    assert review["owner"] == "ops@example.com"

    out = decide(rid + 1).json()["results"]
    assert out[0]["status"] == "approved" and out[0]["outcome"] == {"acme/web": "added"}
    listing = c.get("/api/agents/ledger/github/repositories", headers=h).json()
    assert listing["selected"] == ["acme/infra", "acme/web"] and listing["waiting"] is None
    assert len(admin.writes) == lanes  # the set changed, not what the agent holds
    trail = c.get("/api/admin/audit", headers=h, params={"q": "acme/web"}).json()
    summaries = [e["summary"] for e in trail["rows"]]
    assert "Approved adding acme/web for ledger on github-ledger" in summaries
    assert any(s.endswith("asked to add acme/web to ledger's repositories on GitHub; waiting for review") for s in summaries)

    # A pick that drops one and adds another: the drop lands now, the addition waits.
    r = c.put("/api/agents/ledger/github/repositories", headers=h,
              json={"repositories": ["acme/web", "example-org/agent-ledger"]})
    assert r.status_code == 202 and r.json()["repositories"] == ["acme/web"] and r.json()["changed"] is True
    assert r.json()["waiting"]["added"] == ["example-org/agent-ledger"]
    # Saving the set without the addition withdraws it.
    r = c.put("/api/agents/ledger/github/repositories", headers=h, json={"repositories": ["acme/web"]})
    assert r.status_code == 200 and r.json() == {"integration": "github-ledger", "repositories": ["acme/web"], "changed": False}
    assert c.get("/api/agents/ledger/github/repositories", headers=h).json()["waiting"] is None
    assert [p["status"] for p in bab.requests("all", "ledger", "x") if p["kind"] == "repositories"] == ["approved", "withdrawn"]


def test_the_gateway_lane_follows_each_github_level_and_withdraws_what_is_revoked(tmp_path, fake_storage):
    """Gate (authorization, kobo#365): Read approved opens the read-only target's lane and the
    bundle admits `github-mcp.*`; Open pull requests approved adds the write target's lane with
    `github:review` and `github:write`; revoking Read takes the read lane and its allow pattern
    back while the write lane stays. The retired `github` handler and `github.*` pattern are
    withdrawn on the first follow, whatever the agent held before."""
    import yaml as _yaml

    c, h, bab, github, admin = _github_room(tmp_path, fake_storage)
    assert c.put("/api/agents/ledger/github/repositories", headers=h, json={"repositories": ["acme/infra"]}).status_code == 200
    rid = c.post("/api/agents/ledger/access/requests", headers=h,
                 json={"integration": "github-ledger", "permissions": ["github_repos_read"], "note": "read"}).json()["id"]
    out = c.post("/api/admin/access/decide", headers=h, json={"ids": [rid], "verdict": "approve"}).json()["results"]
    assert out[0]["status"] == "approved" and out[0]["repositories"] == ["acme/infra"]
    lane = admin.grants[("cabal-core", "ledger")]
    assert set(lane.scopes) == {"x:read", "github:read"} and set(lane.allowed_handlers) == {"scratchpad", "github-mcp"}
    # The token `git` can use is for an agent that runs its own code, never a built one (kobo#483).
    assert "github-clone" not in lane.allowed_handlers
    pushed = _yaml.safe_load(github.pushed_file("bundle/agent.yaml"))["tools"]["allow"]
    assert pushed == ["colleagues.list_colleagues", "github-mcp.*"], pushed
    assert github.pushes[-1][1] == "kobo: allow the github tools; withdraw the github tools"

    rid = c.post("/api/agents/ledger/access/requests", headers=h,
                 json={"integration": "github-ledger", "permissions": ["github_pulls_write"], "note": "open PRs"}).json()["id"]
    c.post("/api/admin/access/decide", headers=h, json={"ids": [rid], "verdict": "approve"})
    lane = admin.grants[("cabal-core", "ledger")]
    assert set(lane.scopes) == {"x:read", "github:read", "github:review", "github:write"}
    assert set(lane.allowed_handlers) == {"scratchpad", "github-mcp", "github-mcp-write", "github-write"}
    assert set(_yaml.safe_load(github.pushed_file("bundle/agent.yaml"))["tools"]["allow"]) == {
        "colleagues.list_colleagues", "github-mcp.*", "github-mcp-write.*", "github-write.*"}

    read_grant = next(g for g in bab.grants("ledger", "x") if g["permission"] == "github_repos_read")
    assert c.delete(f"/api/agents/ledger/access/grants/{read_grant['id']}", headers=h).status_code == 200
    lane = admin.grants[("cabal-core", "ledger")]
    assert set(lane.scopes) == {"x:read", "github:review", "github:write"}
    assert set(lane.allowed_handlers) == {"scratchpad", "github-mcp-write", "github-write"}
    pushed = _yaml.safe_load(github.pushed_file("bundle/agent.yaml"))["tools"]["allow"]
    assert "github-mcp.*" not in pushed and "github-mcp-write.*" in pushed and "github-write.*" in pushed
    assert "github.*" not in pushed
    assert c.post("/api/admin/access/sync", headers=h).json()["gateway_lanes"]["failed"] == 0


def test_read_opens_the_clone_token_for_an_agent_that_runs_its_own_code_and_for_no_other(tmp_path, fake_storage):
    """Gate (authorization, kobo#483): Read on GitHub also opens the gateway's `github-clone`
    handler, which hands out a token `git` can use, for an agent that runs its team's own code
    and never for one the room builds (a model must not hold a credential). It leaves with the
    permission, the heal pass owes it to the same agents, and it is withdrawn from a built
    agent that somehow carries it."""
    from kobo_api import gateway_lane
    from kobo_api.models import Agent

    c, h, bab, _github, admin = _github_room(tmp_path, fake_storage)
    with c.app.state.session_factory() as s:
        s.query(Agent).filter_by(name="ledger").one().kind = "connected"
        s.commit()
    assert c.put("/api/agents/ledger/github/repositories", headers=h, json={"repositories": ["acme/infra"]}).status_code == 200
    rid = c.post("/api/agents/ledger/access/requests", headers=h,
                 json={"integration": "github-ledger", "permissions": ["github_repos_read"], "note": "clone"}).json()["id"]
    assert c.post("/api/admin/access/decide", headers=h, json={"ids": [rid], "verdict": "approve"}).status_code == 200
    lane = admin.grants[("cabal-core", "ledger")]
    assert set(lane.scopes) == {"x:read", "github:read"}
    assert set(lane.allowed_handlers) == {"scratchpad", "github-mcp", "github-clone"}

    # Something rewrote the grant: the heal pass owes the handler to the agent that runs its
    # own code, and takes it from one the room builds.
    admin.grants[("cabal-core", "ledger")] = GatewayGrant(
        issuer="cabal-core", persona="ledger", scopes=("github:read",), allowed_handlers=("github-mcp",))
    assert gateway_lane.heal(c.app.state.clients, bab, ["ledger"], "room@kobo", own_code=frozenset({"ledger"})) == ["ledger"]
    assert set(admin.grants[("cabal-core", "ledger")].allowed_handlers) == {"github-mcp", "github-clone"}
    assert gateway_lane.heal(c.app.state.clients, bab, ["ledger"], "room@kobo") == ["ledger"]
    assert set(admin.grants[("cabal-core", "ledger")].allowed_handlers) == {"github-mcp"}
    assert "github-clone" in gateway_lane.MANAGED_HANDLERS

    admin.grants[("cabal-core", "ledger")] = GatewayGrant(
        issuer="cabal-core", persona="ledger", scopes=("x:read", "github:read"),
        allowed_handlers=("scratchpad", "github-mcp", "github-clone"))
    read_grant = next(g for g in bab.grants("ledger", "x") if g["permission"] == "github_repos_read")
    assert c.delete(f"/api/agents/ledger/access/grants/{read_grant['id']}", headers=h).status_code == 200
    lane = admin.grants[("cabal-core", "ledger")]
    assert set(lane.scopes) == {"x:read"} and set(lane.allowed_handlers) == {"scratchpad"}


def test_the_room_puts_a_gateway_target_an_applied_connection_owes_and_the_gateway_lacks(session__admin_store):
    """Gate (authorization): GitHub connected on 2026-10-31 had `github-mcp` on the gateway and
    never `github-mcp-write`, which arrived with kobo#365 the next day; connect was the only
    writer, so an agent granted Review pull requests earned a lane to a door that was not there
    (2026-11-06). The reconcile pass puts what an applied connection owes and the gateway lacks,
    leaves what it serves untouched whatever its spec, skips pending and room-only rows, and
    counts what it did."""
    from kobo_api import admin_integrations
    session = session__admin_store
    row = admin_store.put_integration(
        session, vendor="github", name="GitHub", base_access="read",
        spec={"auth": admin_integrations.GITHUB_AUTH, "address": "https://api.githubcopilot.com",
              "identity": {"app_id": "1", "installation_id": "2", "source": "room"}},
        credential_ref="apps/kobo/integrations/github", actor="kyle@example.com",
    )
    admin_store.set_integration_state(session, row.id, state=admin_store.STATE_APPLIED, detail="live")
    pending = admin_store.put_integration(
        session, vendor="Datadog", name="Datadog", base_access="read",
        spec={"auth": admin_integrations.DATADOG_AUTH, "address": "https://mcp.datadoghq.com"},
        credential_ref="apps/kobo/integrations/datadog", actor="kyle@example.com",
    )
    session.commit()
    admin = FakeLiminalAdmin()
    admin.targets["github-mcp"] = {"required_scope": "github:read", "headers": {"X-MCP-Readonly": "true"}, "note": "as connect left it"}

    class _Clients:
        liminal_admin = admin

    counts = admin_integrations.reconcile_targets(session, _Clients())
    assert counts == {"put": 1, "present": 1, "failed": 0}
    assert admin.targets["github-mcp"]["note"] == "as connect left it"
    write = admin.targets["github-mcp-write"]
    assert write["required_scope"] == "github:review" and "headers" not in write
    assert write["tool_scopes"]["create_branch"] == "github:write"
    assert write["tool_scopes"]["add_issue_comment"] == "github:never"
    assert write["tool_scopes"]["create_repository"] == "github:never"
    assert admin_integrations.gateway_target_name(pending) not in admin.targets
    assert admin_integrations.reconcile_targets(session, _Clients()) == {"put": 0, "present": 2, "failed": 0}
    assert admin_integrations.reconcile_targets(session, None) == {"put": 0, "present": 0, "failed": 0}


def test_the_room_sets_back_a_gateway_grant_something_else_rewrote():
    """Gate (authorization): the gateway's own boot seed re-applied a file stanza over ledger's
    live grant and took its GitHub lane away for a day (2026-11-04). The heal pass puts back what
    the agent's permissions owe, keeps what the room never managed, and leaves an agent that
    holds no permission alone, so a grant that lives only in the gateway's file is not touched."""
    from kobo_api import gateway_lane

    bab = FakeBab(integrations=[
        {"integration": "github-ledger", "vendor": "github", "ceiling": ["github_repos_read"],
         "created_at": "2026-10-29T12:00:00", "updated_at": "2026-10-29T12:00:00"},
        {"integration": "atlassian", "vendor": "atlassian", "ceiling": ["atlassian_jira_issues_read"],
         "created_at": "2026-10-29T12:00:00", "updated_at": "2026-10-29T12:00:00"},
    ])
    bab._grants[1] = {"id": 1, "agent": "ledger", "integration": "github-ledger", "permission": "github_repos_read",
                      "state": "active"}
    bab._grants[2] = {"id": 2, "agent": "ledger", "integration": "atlassian", "permission": "atlassian_jira_issues_read",
                      "state": "active"}
    admin = FakeLiminalAdmin([
        GatewayGrant(issuer="cabal-core", persona="ledger", scopes=("atlassian:read",), allowed_handlers=("atlassian", "scratchpad")),
        GatewayGrant(issuer="cabal-core", persona="janus", scopes=("github:read",), allowed_handlers=("github", "scratchpad")),
    ])

    class _Clients:
        liminal_admin = admin

    healed = gateway_lane.heal(_Clients(), bab, ["ledger", "janus"], "room@kobo")

    assert healed == ["ledger"]
    ledger = admin.grants[("cabal-core", "ledger")]
    assert set(ledger.scopes) == {"atlassian:read", "github:read"}
    assert set(ledger.allowed_handlers) == {"atlassian", "github-mcp", "scratchpad"}
    assert admin.grants[("cabal-core", "janus")].allowed_handlers == ("github", "scratchpad")
    assert gateway_lane.heal(_Clients(), bab, ["ledger", "janus"], "room@kobo") == []


def test_a_google_cloud_project_connected_for_its_models_alone_is_held_with_no_ceiling_and_never_offered(tmp_path, fake_storage):
    """Gate (authorization, 2026-11-09): the `models only` tick on a project (the AWS Bedrock
    shape, here Vertex AI) connects it with an empty ceiling in the permission service and
    `models_only` on the room's row, so the Models pane lists it and no agent's Access tab or
    picker ever offers it. Models only without the Vertex tick is refused: nothing to connect for."""
    bab = _gcp_bab()
    client, headers = _access_client(tmp_path, fake_storage, bab=bab)
    body = {"vendor": "gcp", "name": "Vertex", "base_access": "read", "auth": "gcp_identity",
            "identity": {"project": "example-vertex-1234", "models_only": "1"}}
    refused = client.post("/api/admin/integrations", json=body, headers=headers)
    assert refused.status_code == 409 and "needs its Vertex AI models ticked" in refused.json()["error"]
    body["identity"].update({"models_provider": "vertex_ai", "models_location": "us-east5"})
    row = client.post("/api/admin/integrations", json=body, headers=headers).json()
    assert row["state"] == "applied" and row["spec"]["models_only"] is True and row["spec"]["models"]["project"] == "example-vertex-1234", row
    assert "for its models alone, no agent is offered it" in row["state_detail"]
    (held,) = bab.integrations("x")
    assert held["integration"] == "vertex" and held["ceiling"] == [] and held["kind"] == "account"
    picker = {v["id"]: v for v in client.get("/api/access/picker", headers=headers).json()["vendors"]}
    assert "vertex" not in {a["id"] for a in picker.get("gcp", {}).get("accounts", [])}
    panel = {r["id"]: r for r in client.get("/api/admin/integrations", headers=headers).json()["integrations"]}
    assert panel["vertex"]["spec"]["models_only"] is True


def test_a_google_cloud_read_permission_opens_the_gateways_gcp_handler():
    """Gate (authorization, 2026-11-09): every `gcp_*_read` permission is one lane, `gcp:read`
    on the `gcp` handler with `gcp.*` allowed, so a grant on any connected project opens the
    handler that leases that project's token per call."""
    from kobo_api.gateway_lane import (
        GATEWAY_LANES,
        MANAGED_HANDLERS,
        MANAGED_SCOPES,
        lane_for,
    )
    lane = lane_for("gcp", "gcp_compute_read")
    assert lane is GATEWAY_LANES["gcp"] and lane_for("gcp", "gcp_gke_read") is lane
    assert (lane.scopes, lane.handlers, lane.allow) == (("gcp:read",), ("gcp",), ("gcp.*",))
    assert "gcp:read" in MANAGED_SCOPES and "gcp" in MANAGED_HANDLERS

def test_an_aws_permission_opens_the_gateways_aws_handler_and_a_cloud_with_no_lane_is_named():
    """Gate (authorization, 2026-11-13): an agent granted CloudTrail read on an account had no
    tool to use it with, since AWS had no lane. Every AWS permission, reading or changing, is
    one lane: `aws:read` on the `aws` handler with `aws.*` allowed (AWS narrows the leased
    session to what is held). A cloud the Access tab can offer and no lane serves is named
    here, so the next one cannot arrive granted and toolless without this test saying so."""
    from kobo_api.bab import CLOUD_LABELS
    from kobo_api.gateway_lane import (
        GATEWAY_LANES,
        MANAGED_ALLOW,
        MANAGED_HANDLERS,
        MANAGED_SCOPES,
        lane_for,
    )
    lane = lane_for("aws", "aws_cloudtrail_read")
    assert lane is GATEWAY_LANES["aws"] and lane_for("aws", "aws_ecs_update_service") is lane
    assert (lane.scopes, lane.handlers, lane.allow) == (("aws:read",), ("aws",), ("aws.*",))
    assert "aws:read" in MANAGED_SCOPES and "aws" in MANAGED_HANDLERS and "aws.*" in MANAGED_ALLOW
    assert {v for v in CLOUD_LABELS if lane_for(v, "") is None} == {"azure"}


def test_a_lane_new_to_a_release_reaches_an_agent_that_already_held_the_permission(tmp_path, fake_storage):
    """Gate (authorization, 2026-11-13): scooby-doo held an AWS permission before the room had
    an AWS lane. Nobody grants again after the release that adds one: the heal pass finds the
    gateway grant short, sets it, and the bundle admits `aws.*` in the same pass (one commit).
    A repository that could not be written is tried again on the next pass, and a pass with
    nothing owed writes nothing."""
    import yaml as _yaml

    from kobo_api import gateway_lane

    c, _, bab, github, admin = _github_room(tmp_path, fake_storage, connected=False)
    bab._integrations.append({"integration": "fleet-audit", "vendor": "aws", "ceiling": ["aws_cloudtrail_read"],
                              "created_at": "2026-10-29T12:00:00", "updated_at": "2026-10-29T12:00:00"})
    bab._grants[1] = {"id": 1, "agent": "ledger", "integration": "fleet-audit", "permission": "aws_cloudtrail_read",
                      "state": "active"}

    github._clone_error = "the repository host is not answering"
    assert gateway_lane._heal_ready_agents(c.app) == ["ledger"]
    grant = admin.grants[("cabal-core", "ledger")]
    assert set(grant.scopes) == {"x:read", "aws:read"} and set(grant.allowed_handlers) == {"scratchpad", "aws"}
    assert github.pushes == [] and c.app.state.allow_owed == {"ledger"}

    github._clone_error = None
    assert gateway_lane._heal_ready_agents(c.app) == []
    assert _yaml.safe_load(github.pushed_file("bundle/agent.yaml"))["tools"]["allow"] == ["colleagues.list_colleagues", "aws.*"]
    assert github.pushes[-1][1] == "kobo: allow the aws tools; withdraw the github tools"
    assert c.app.state.allow_owed == set()

    pushes = len(github.pushes)
    assert gateway_lane._heal_ready_agents(c.app) == [] and len(github.pushes) == pushes
