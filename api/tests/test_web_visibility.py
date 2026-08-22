"""Tests for the visibility filter (access-control Task B2): "each person
sees their own aisle." Covers `_visible_agents`/whoami's real `groups`
through the HTTP surface (`GET /api/agents`, `GET /api/whoami`), the
detail/sub-route 404-not-403 wiring (`_visible_agent_or_404`, the single
choke point every agent-scoped route resolves its agent through), and the
create route's group_name validation.

Same real-JWT idiom test_web_teach.py/test_web_ownership_gate.py use for
their own IAP-attribution slices: a locally generated ES256 keypair signs
the assertion, verified through the actual `IapMiddleware`/`IapVerifier`,
not a mock. The group resolver is faked with the plainest possible stand-in
for the `GroupResolver` Protocol -- a bare object with a `groups_for`
method, nothing else -- exactly the shape `kobo_api.groups.GroupResolver`
declares and the only thing any caller here is allowed to depend on.
"""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.clients import Clients
from kobo_api.config import SHARED_GROUP, Config, GroupChoice
from kobo_api.iap import ASSERTION_HEADER, IAP_ISSUER
from kobo_api.main import create_app
from kobo_api.models import Agent, AgentStatus, Base
from tests.fakes import (
    FakeAgent,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
    FakeStorage,
)

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
        iap_audience=AUDIENCE,
        groups=GROUPS,
    )


def _make_client(session_factory, config, *, groups_resolver=None, iap_jwks_fetcher):
    return TestClient(
        create_app(
            session_factory,
            config=config,
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


def _seed_agent(session_factory, config, *, name, created_by=None, group_name=None):
    session = session_factory()
    session.add(
        Agent(
            name=name,
            persona=PERSONA,
            model_id=config.models[0].id,
            status=AgentStatus.READY,
            repo_full_name=REPO_TEMPLATE.format(name=name),
            created_by=created_by,
            group_name=group_name,
        )
    )
    session.commit()
    session.close()


# --- The visibility matrix, via GET /api/agents -----------------------------
#
# Three seeded agents:
#   "group-agent"     group_name=payments-eng, created_by=nobody@example.com
#   "owner-agent"     group_name=growth,       created_by=OWNER_EMAIL
#   "invisible-agent" group_name=growth,       created_by=nobody@example.com
#
# A member of payments-eng (MEMBER_EMAIL) should see "group-agent" (group
# match) and nothing else. OWNER_EMAIL should see "owner-agent" (owns it)
# even though it's in a group OWNER_EMAIL doesn't belong to -- ownership
# survives a group mismatch. STRANGER_EMAIL (no ownership, no group
# membership anywhere) should see neither. OPERATOR_EMAIL sees all three,
# same as today.


def _seed_matrix(session_factory, config):
    _seed_agent(session_factory, config, name="group-agent", group_name="payments-eng")
    _seed_agent(session_factory, config, name="owner-agent", created_by=OWNER_EMAIL, group_name="growth")
    _seed_agent(session_factory, config, name="invisible-agent", group_name="growth")


def test_operator_sees_every_agent(session_factory, config):
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({MEMBER_EMAIL: ["payments-eng"]})
    client = _make_client(session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))
    _seed_matrix(session_factory, config)

    response = client.get("/api/agents", headers=_headers(private_key, email=OPERATOR_EMAIL))

    assert response.status_code == 200
    body = response.json()
    assert body["groups_unavailable"] is False
    assert {a["name"] for a in body["agents"]} == {"group-agent", "owner-agent", "invisible-agent"}


def test_group_member_sees_owned_union_group_agents(session_factory, config):
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({MEMBER_EMAIL: ["payments-eng"]})
    client = _make_client(session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))
    _seed_matrix(session_factory, config)

    response = client.get("/api/agents", headers=_headers(private_key, email=MEMBER_EMAIL))

    assert response.status_code == 200
    body = response.json()
    assert body["groups_unavailable"] is False
    assert {a["name"] for a in body["agents"]} == {"group-agent"}


def test_owner_sees_own_agent_even_outside_their_groups(session_factory, config):
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({OWNER_EMAIL: ["payments-eng"]})
    client = _make_client(session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))
    _seed_matrix(session_factory, config)

    response = client.get("/api/agents", headers=_headers(private_key, email=OWNER_EMAIL))

    assert response.status_code == 200
    body = response.json()
    # OWNER_EMAIL is a payments-eng member (sees group-agent too) AND owns
    # owner-agent (a growth-namespaced agent OWNER_EMAIL doesn't belong to
    # by group) -- both routes into visibility fire, "invisible-agent" is
    # neither owned nor in a group OWNER_EMAIL belongs to.
    assert {a["name"] for a in body["agents"]} == {"group-agent", "owner-agent"}


def test_stranger_sees_nothing_they_neither_own_nor_belong_to(session_factory, config):
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({MEMBER_EMAIL: ["payments-eng"]})
    client = _make_client(session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))
    _seed_matrix(session_factory, config)

    response = client.get("/api/agents", headers=_headers(private_key, email=STRANGER_EMAIL))

    assert response.status_code == 200
    body = response.json()
    assert body["groups_unavailable"] is False
    assert body["agents"] == []


def test_resolver_down_falls_back_to_owned_only_and_flags_unavailable(session_factory, config):
    # Fail-closed (Global Constraint): the directory is unreachable, so
    # group-derived visibility vanishes -- owner rights survive, nothing
    # wider. MEMBER_EMAIL owns "member-owned" and would normally also see
    # "group-agent" via payments-eng membership, but not while the
    # resolver is down.
    _seed_agent(session_factory, config, name="member-owned", created_by=MEMBER_EMAIL)
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver(down=True)
    client = _make_client(session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))
    _seed_matrix(session_factory, config)

    response = client.get("/api/agents", headers=_headers(private_key, email=MEMBER_EMAIL))

    assert response.status_code == 200
    body = response.json()
    assert body["groups_unavailable"] is True
    assert {a["name"] for a in body["agents"]} == {"member-owned"}


def test_no_resolver_configured_is_the_honest_phase_gate_everyone_sees_everything(
    session_factory, config
):
    # Absent token = groups feature off entirely (kobo_api.clients.Clients.
    # real's own gate) -- the room behaves exactly as it did before this
    # arc, plus ownership: full visibility, never narrower just because
    # groups haven't been turned on.
    private_key, jwk = _generate_jwk()
    client = _make_client(session_factory, config, groups_resolver=None, iap_jwks_fetcher=_fetcher(jwk))
    _seed_matrix(session_factory, config)

    response = client.get("/api/agents", headers=_headers(private_key, email=STRANGER_EMAIL))

    assert response.status_code == 200
    body = response.json()
    assert body["groups_unavailable"] is False
    assert {a["name"] for a in body["agents"]} == {"group-agent", "owner-agent", "invisible-agent"}


def test_iap_disabled_is_full_visibility_same_as_no_resolver(session_factory):
    # Dev parity: no IAP means no verified identity to filter by at all --
    # same escape hatch every other gate in main.py uses.
    config = Config(operators=[], iap_audience="", groups=GROUPS)
    client = TestClient(
        create_app(
            session_factory,
            config=config,
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
    _seed_matrix(session_factory, config)

    response = client.get("/api/agents")

    assert response.status_code == 200
    body = response.json()
    assert body["groups_unavailable"] is False
    assert {a["name"] for a in body["agents"]} == {"group-agent", "owner-agent", "invisible-agent"}


# --- whoami's real groups (Task B2 makes A1's hardcoded [] real) ------------


def test_whoami_reports_resolved_groups(session_factory, config):
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({MEMBER_EMAIL: ["payments-eng", "growth"]})
    client = _make_client(session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))

    response = client.get("/api/whoami", headers=_headers(private_key, email=MEMBER_EMAIL))

    assert response.status_code == 200
    body = response.json()
    assert set(body["groups"]) == {"payments-eng", "growth"}
    assert body["groups_unavailable"] is False


def test_whoami_flags_groups_unavailable_when_resolver_is_down(session_factory, config):
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver(down=True)
    client = _make_client(session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))

    response = client.get("/api/whoami", headers=_headers(private_key, email=MEMBER_EMAIL))

    assert response.status_code == 200
    body = response.json()
    assert body["groups"] == []
    assert body["groups_unavailable"] is True


# --- Detail + sub-routes: out-of-visibility is a 404, never a 403 ----------
#
# One agent, owned by OWNER_EMAIL, namespaced into payments-eng.
# STRANGER_EMAIL belongs to no group and doesn't own it -- every route that
# resolves this agent by name must answer 404 for STRANGER_EMAIL, as if it
# didn't exist (existence is itself information). Chat and knowledge are
# the two representatives the brief calls for: chat because it's open BY
# DESIGN to any signed-in user who can see the agent (A3) -- proving that
# openness is bounded by visibility, not a bypass of it; knowledge's PUT
# because it's owner-or-operator GATED (A3) -- proving visibility's 404
# fires before that 403 would, at the same shared fetch seam.


@pytest.fixture
def visible_client(session_factory, config):
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({})
    client = _make_client(session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, config, name="clio", created_by=OWNER_EMAIL, group_name="payments-eng")
    return client, private_key


def test_get_agent_detail_404s_for_a_stranger_to_the_group(visible_client):
    client, private_key = visible_client

    response = client.get("/api/agents/clio", headers=_headers(private_key, email=STRANGER_EMAIL))

    assert response.status_code == 404


def test_get_agent_detail_200s_for_the_owner(visible_client):
    client, private_key = visible_client

    response = client.get("/api/agents/clio", headers=_headers(private_key, email=OWNER_EMAIL))

    assert response.status_code == 200


def test_chat_404s_for_a_stranger_to_the_group_despite_being_open_by_design(visible_client):
    client, private_key = visible_client

    response = client.post(
        "/api/agents/clio/chat",
        json={"messages": [{"role": "user", "text": "hi"}]},
        headers=_headers(private_key, email=STRANGER_EMAIL),
    )

    assert response.status_code == 404


def test_chat_200s_for_the_owner(visible_client):
    client, private_key = visible_client

    response = client.post(
        "/api/agents/clio/chat",
        json={"messages": [{"role": "user", "text": "hi"}]},
        headers=_headers(private_key, email=OWNER_EMAIL),
    )

    assert response.status_code == 200


def test_knowledge_save_404s_for_a_stranger_not_403(visible_client):
    # The exact ordering claim: without visibility wired at the shared fetch
    # seam, a stranger here would hit the owner-or-operator gate and get a
    # 403 (they're neither the owner nor an operator) -- with it wired,
    # they never get that far. 404, not 403.
    client, private_key = visible_client

    response = client.put(
        "/api/agents/clio/knowledge/gotchas.md",
        json={"content": "whatever"},
        headers=_headers(private_key, email=STRANGER_EMAIL),
    )

    assert response.status_code == 404


def test_knowledge_list_404s_for_a_stranger(visible_client):
    client, private_key = visible_client

    response = client.get(
        "/api/agents/clio/knowledge", headers=_headers(private_key, email=STRANGER_EMAIL)
    )

    assert response.status_code == 404


def test_knowledge_list_200s_for_a_fellow_group_member(session_factory, config):
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({MEMBER_EMAIL: ["payments-eng"]})
    client = _make_client(session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, config, name="clio", created_by=OWNER_EMAIL, group_name="payments-eng")

    response = client.get(
        "/api/agents/clio/knowledge", headers=_headers(private_key, email=MEMBER_EMAIL)
    )

    assert response.status_code == 200


# --- Create route: group_name required + validated once groups exist ------
#
# Deliberately no IAP here at all -- group_name validation is a pure
# config-vs-payload check (`cfg.groups`), independent of identity, so a
# plain unauthenticated `Config(groups=GROUPS, ...)` room is enough; using
# the module's own `config` fixture (which turns IAP on) would just make
# every one of these calls 401 before ever reaching create_agent.


@pytest.fixture
def groups_config(tmp_path):
    return Config(
        workdir=tmp_path / "work",
        skills_bucket="skills-bucket",
        personas_bucket="personas-bucket",
        groups=GROUPS,
    )


@pytest.fixture
def groups_client(session_factory, groups_config, fake_storage):
    return TestClient(
        create_app(
            session_factory,
            config=groups_config,
            clients=Clients(
                github=FakeGitHub(),
                gojira=FakeGojira(),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=FakeKubectl(),
                storage=fake_storage,
            ),
        )
    )


def test_create_agent_requires_group_name_when_groups_configured(groups_client):
    response = groups_client.post(
        "/api/agents", json={"name": "clio", "persona": "helpful-generalist", "team": "shared"}
    )

    assert response.status_code == 422
    assert "—" not in response.json()["error"]


def test_create_agent_rejects_unknown_group_name_when_groups_configured(groups_client):
    response = groups_client.post(
        "/api/agents",
        json={
            "name": "clio",
            "persona": "helpful-generalist",
            "team": "shared",
            "group_name": "not-a-real-group",
        },
    )

    assert response.status_code == 422
    assert "—" not in response.json()["error"]


def test_create_agent_accepts_a_configured_group_name(session_factory, groups_client):
    response = groups_client.post(
        "/api/agents",
        json={
            "name": "clio",
            "persona": "helpful-generalist",
            "team": "shared",
            "group_name": "payments-eng",
        },
    )

    assert response.status_code == 201

    session = session_factory()
    row = session.query(Agent).filter_by(name="clio").first()
    session.close()
    assert row.group_name == "payments-eng"


# --- Create route: claiming a group takes membership (final-review fix
# round, HIGH-1: consent) ----------------------------------------------------
#
# Once IAP is on and cfg.groups is non-empty, being a NAME in cfg.groups is
# no longer enough for a non-operator to claim a group_name -- it must also
# be one of the CALLER's own resolved groups (`clients.groups.groups_for`).
# Operators are exempt. IAP-off rooms are untouched (pinned above by
# test_create_agent_accepts_a_configured_group_name, which claims "payments-
# eng" through the IAP-off `groups_client` with no resolver wired at all).


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


def _consent_client(session_factory, consent_config, fake_storage, *, groups_resolver, iap_jwks_fetcher):
    return TestClient(
        create_app(
            session_factory,
            config=consent_config,
            clients=Clients(
                github=FakeGitHub(),
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


def test_member_creates_in_own_group_gets_201(session_factory, consent_config, fake_storage):
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({MEMBER_EMAIL: ["payments-eng"]})
    client = _consent_client(
        session_factory, consent_config, fake_storage,
        groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk),
    )

    response = client.post(
        "/api/agents",
        json=_create_payload("clio", "payments-eng"),
        headers=_headers(private_key, email=MEMBER_EMAIL),
    )

    assert response.status_code == 201, response.text


def test_member_claiming_a_group_they_are_not_in_is_403_verbatim(
    session_factory, consent_config, fake_storage
):
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({MEMBER_EMAIL: ["payments-eng"]})
    client = _consent_client(
        session_factory, consent_config, fake_storage,
        groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk),
    )

    response = client.post(
        "/api/agents",
        json=_create_payload("clio", "growth"),
        headers=_headers(private_key, email=MEMBER_EMAIL),
    )

    assert response.status_code == 403
    assert response.json() == {"error": "You are not a member of growth."}


def test_operator_claims_any_configured_group_gets_201(
    session_factory, consent_config, fake_storage
):
    private_key, jwk = _generate_jwk()
    # The operator belongs to no configured group by directory membership at
    # all -- still allowed, since operators are exempt from the membership
    # check entirely.
    resolver = FakeGroupResolver({})
    client = _consent_client(
        session_factory, consent_config, fake_storage,
        groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk),
    )

    response = client.post(
        "/api/agents",
        json=_create_payload("clio", "growth"),
        headers=_headers(private_key, email=OPERATOR_EMAIL),
    )

    assert response.status_code == 201, response.text


def test_resolver_down_member_create_is_403_with_honest_copy(
    session_factory, consent_config, fake_storage
):
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver(down=True)
    client = _consent_client(
        session_factory, consent_config, fake_storage,
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


def test_iap_off_room_keeps_todays_group_claim_behavior_unchanged(groups_client):
    # groups_client (module fixture above) has IAP entirely off and no
    # resolver wired -- this HIGH-1 fix must change nothing about that room:
    # claiming any configured group_name still just needs to be a name
    # cfg.groups knows about.
    response = groups_client.post(
        "/api/agents",
        json=_create_payload("clio", "growth"),
    )

    assert response.status_code == 201, response.text


# --- GET /api/catalog/groups: names only, no Okta names leaked -------------
#
# Shared-group + group-requests wave: SHARED_GROUP ("shared") is now always
# the FIRST entry, marked `kind: "shared"` so the UI can section it apart
# from the room's own named groups (each marked `kind: "engineering"`) --
# present whether or not `cfg.groups` names anything at all.


def test_catalog_groups_returns_shared_first_then_configured_names(
    session_factory, groups_config
):
    client = TestClient(create_app(session_factory, config=groups_config))

    response = client.get("/api/catalog/groups")

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "groups": [
            {"name": "shared", "kind": "shared"},
            {"name": "payments-eng", "kind": "engineering"},
            {"name": "growth", "kind": "engineering"},
        ]
    }
    assert "okta-payments" not in response.text
    assert "okta-growth" not in response.text


def test_catalog_groups_is_just_shared_when_unconfigured(session_factory):
    config = Config()
    client = TestClient(create_app(session_factory, config=config))

    response = client.get("/api/catalog/groups")

    assert response.status_code == 200
    assert response.json() == {"groups": [{"name": "shared", "kind": "shared"}]}


# --- Shared commons: visible to everyone, in every _visible_agents branch --
#
# The reserved SHARED_GROUP group name: an agent namespaced into it is
# commons, visible to every signed-in caller regardless of group
# membership or ownership -- including the two branches that otherwise
# NARROW visibility (a resolver that's down, and a stranger with no group
# membership at all). Fail-closed still applies to GROUP-derived visibility
# (an Okta outage narrows that to nothing); a shared agent isn't a grant, so
# it isn't narrowed by that same fail-closed rule.


def test_shared_agent_visible_to_a_stranger_with_no_group_membership(
    session_factory, config
):
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({})
    client = _make_client(
        session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk)
    )
    _seed_agent(session_factory, config, name="shared-agent", group_name=SHARED_GROUP)
    _seed_matrix(session_factory, config)

    response = client.get("/api/agents", headers=_headers(private_key, email=STRANGER_EMAIL))

    assert response.status_code == 200
    body = response.json()
    assert body["groups_unavailable"] is False
    assert "shared-agent" in {a["name"] for a in body["agents"]}


def test_shared_agent_visible_even_when_the_resolver_is_down(session_factory, config):
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver(down=True)
    client = _make_client(
        session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk)
    )
    _seed_agent(session_factory, config, name="shared-agent", group_name=SHARED_GROUP)

    response = client.get("/api/agents", headers=_headers(private_key, email=STRANGER_EMAIL))

    assert response.status_code == 200
    body = response.json()
    # Fail-closed still narrows GROUP-derived visibility to nothing (no
    # ownership, no grant) -- the commons agent is the only thing left
    # standing, proving it isn't itself treated as a grant subject to the
    # same narrowing.
    assert body["groups_unavailable"] is True
    assert {a["name"] for a in body["agents"]} == {"shared-agent"}


def test_shared_agent_visible_with_no_resolver_configured(session_factory, config):
    private_key, jwk = _generate_jwk()
    client = _make_client(
        session_factory, config, groups_resolver=None, iap_jwks_fetcher=_fetcher(jwk)
    )
    _seed_agent(session_factory, config, name="shared-agent", group_name=SHARED_GROUP)

    response = client.get("/api/agents", headers=_headers(private_key, email=STRANGER_EMAIL))

    assert response.status_code == 200
    assert "shared-agent" in {a["name"] for a in response.json()["agents"]}


def test_shared_agent_visible_when_iap_is_disabled(session_factory):
    config = Config(operators=[], iap_audience="", groups=GROUPS)
    client = TestClient(
        create_app(
            session_factory,
            config=config,
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
    _seed_agent(session_factory, config, name="shared-agent", group_name=SHARED_GROUP)

    response = client.get("/api/agents")

    assert response.status_code == 200
    assert "shared-agent" in {a["name"] for a in response.json()["agents"]}


def test_shared_agent_detail_route_200s_for_a_stranger(session_factory, config):
    # The single visibility choke point (_visible_agent_or_404) must honor
    # the same commons rule for the detail/sub-route seam, not just the
    # list route.
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({})
    client = _make_client(
        session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk)
    )
    _seed_agent(session_factory, config, name="shared-agent", group_name=SHARED_GROUP)

    response = client.get(
        "/api/agents/shared-agent", headers=_headers(private_key, email=STRANGER_EMAIL)
    )

    assert response.status_code == 200


# --- Create route: SHARED_GROUP is always valid, membership-free -----------
#
# The reserved SHARED_GROUP name is never one of cfg.groups' own entries,
# yet the create route must accept it from ANY signed-in caller. The HIGH-1
# consent check exists to stop a caller CLAIMING someone else's real group
# without asking; "shared" isn't anyone's group to protect, so it's exempt
# from that check entirely, not just from whatever the resolver happens to
# answer.


def test_shared_group_name_is_valid_even_though_it_is_not_a_configured_group(
    session_factory, consent_config, fake_storage
):
    private_key, jwk = _generate_jwk()
    # MEMBER_EMAIL belongs to neither configured group -- proves this isn't
    # sneaking through via directory membership in "shared".
    resolver = FakeGroupResolver({MEMBER_EMAIL: ["payments-eng"]})
    client = _consent_client(
        session_factory, consent_config, fake_storage,
        groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk),
    )

    response = client.post(
        "/api/agents",
        json=_create_payload("clio", SHARED_GROUP),
        headers=_headers(private_key, email=MEMBER_EMAIL),
    )

    assert response.status_code == 201, response.text


def test_shared_group_create_bypasses_membership_check_even_when_resolver_is_down(
    session_factory, consent_config, fake_storage
):
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver(down=True)
    client = _consent_client(
        session_factory, consent_config, fake_storage,
        groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk),
    )

    response = client.post(
        "/api/agents",
        json=_create_payload("clio", SHARED_GROUP),
        headers=_headers(private_key, email=MEMBER_EMAIL),
    )

    assert response.status_code == 201, response.text


def test_shared_group_create_ok_with_no_resolver_wired_at_all(
    session_factory, consent_config, fake_storage
):
    private_key, jwk = _generate_jwk()
    client = _consent_client(
        session_factory, consent_config, fake_storage,
        groups_resolver=None, iap_jwks_fetcher=_fetcher(jwk),
    )

    response = client.post(
        "/api/agents",
        json=_create_payload("clio", SHARED_GROUP),
        headers=_headers(private_key, email=MEMBER_EMAIL),
    )

    assert response.status_code == 201, response.text
