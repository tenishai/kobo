"""Tests for group-based operator status (operator-status-by-group ruling):
"the infra-engineering team should be able to send and accept requests for
any agents, as they are the admin team over kobo." Encoded as `_is_admin`
(kobo_api.main), the one place a room's `admin_groups` ALSO grant operator
status, alongside the plain `operators` email list `_is_operator` already
matches.

Two layers of coverage, same shape as test_web_ownership_gate.py/
test_web_visibility.py:

- Unit-level: `_is_admin` called directly against a minimal stand-in for
  `Request` (only `.state.iap_email`, `.app.state.config`, `.app.state.
  clients` are read) and the plainest possible stand-in for a `GroupResolver`
  -- a bare object with a `groups_for` method, the exact shape kobo_api.
  groups.GroupResolver declares.
- Route-level: real `TestClient` requests, real IAP JWTs (the test_web_teach.
  py idiom: a locally generated ES256 keypair signs the assertion, verified
  through the actual `IapMiddleware`/`IapVerifier`, not a mock) -- proving an
  admin-group member gets full visibility, passes a mutating owner-or-
  operator gate on an agent they do not own, may create and accept
  cross-group invites on either side without owning it, and whoami's
  operator flag reflects it. A plain member of some OTHER group, a resolver
  that is not wired at all, and an empty `admin_groups` are each pinned
  right alongside, so the "group grant is additive, fail-closed, and off by
  default" contract never quietly drifts.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.clients import Clients
from kobo_api.config import Config, GroupChoice
from kobo_api.iap import ASSERTION_HEADER, IAP_ISSUER
from kobo_api.main import _is_admin, create_app
from kobo_api.models import A2AInvite, Agent, AgentStatus, Base
from tests.fakes import (
    FakeAgent,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
    FakeStorage,
)

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

VERBATIM_403 = {"error": "Only this agent's owner or a room operator can change it."}

AUDIENCE = "test-audience"
KID = "test-key-1"


class FakeGroupResolver:
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


# --- Unit-level: _is_admin called directly ------------------------------------


def _req(email: str | None, config: Config, clients=None):
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
        app=SimpleNamespace(state=SimpleNamespace(config=config, clients=clients)),
    )


def test_is_admin_operator_email_passes_with_no_clients_at_all():
    config = Config(
        operators=[OPERATOR_EMAIL],
        admin_groups=[ADMIN_GROUP],
        groups=[GroupChoice(name=ADMIN_GROUP, okta_group="okta-infra-engineering")],
        iap_audience=AUDIENCE,
    )

    assert _is_admin(_req(OPERATOR_EMAIL, config, clients=None)) is True


def test_is_admin_group_member_passes():
    config = Config(
        admin_groups=[ADMIN_GROUP],
        groups=[GroupChoice(name=ADMIN_GROUP, okta_group="okta-infra-engineering")],
        iap_audience=AUDIENCE,
    )
    resolver = FakeGroupResolver({ADMIN_MEMBER_EMAIL: [ADMIN_GROUP]})
    clients = SimpleNamespace(groups=resolver)

    assert _is_admin(_req(ADMIN_MEMBER_EMAIL, config, clients=clients)) is True


def test_is_admin_email_case_insensitive_for_group_member():
    config = Config(
        admin_groups=[ADMIN_GROUP],
        groups=[GroupChoice(name=ADMIN_GROUP, okta_group="okta-infra-engineering")],
        iap_audience=AUDIENCE,
    )
    resolver = FakeGroupResolver({ADMIN_MEMBER_EMAIL: [ADMIN_GROUP]})
    clients = SimpleNamespace(groups=resolver)

    assert _is_admin(_req(ADMIN_MEMBER_EMAIL.upper(), config, clients=clients)) is True


def test_is_admin_plain_member_of_another_group_fails():
    config = Config(
        admin_groups=[ADMIN_GROUP],
        groups=[GroupChoice(name=ADMIN_GROUP, okta_group="okta-infra-engineering")],
        iap_audience=AUDIENCE,
    )
    resolver = FakeGroupResolver({PLAIN_MEMBER_EMAIL: [OTHER_GROUP]})
    clients = SimpleNamespace(groups=resolver)

    assert _is_admin(_req(PLAIN_MEMBER_EMAIL, config, clients=clients)) is False


def test_is_admin_resolver_none_group_grant_off_but_email_operator_still_passes():
    # No GroupResolver wired at all (the directory integration is simply not
    # turned on in this room yet) -- group-derived admin status vanishes,
    # but the plain email operators list is untouched.
    config = Config(
        operators=[OPERATOR_EMAIL],
        admin_groups=[ADMIN_GROUP],
        groups=[GroupChoice(name=ADMIN_GROUP, okta_group="okta-infra-engineering")],
        iap_audience=AUDIENCE,
    )

    assert _is_admin(_req(OPERATOR_EMAIL, config, clients=None)) is True
    assert _is_admin(_req(ADMIN_MEMBER_EMAIL, config, clients=None)) is False


def test_is_admin_clients_present_but_groups_resolver_unwired():
    config = Config(
        admin_groups=[ADMIN_GROUP],
        groups=[GroupChoice(name=ADMIN_GROUP, okta_group="okta-infra-engineering")],
        iap_audience=AUDIENCE,
    )
    clients = SimpleNamespace(groups=None)

    assert _is_admin(_req(ADMIN_MEMBER_EMAIL, config, clients=clients)) is False


def test_is_admin_resolver_down_group_grant_off():
    # The directory itself failed to answer for this email (fail-closed,
    # kobo_api.groups' own None-vs-[] distinction) -- grants nothing, same
    # as no resolver at all.
    config = Config(
        admin_groups=[ADMIN_GROUP],
        groups=[GroupChoice(name=ADMIN_GROUP, okta_group="okta-infra-engineering")],
        iap_audience=AUDIENCE,
    )
    resolver = FakeGroupResolver(down=True)
    clients = SimpleNamespace(groups=resolver)

    assert _is_admin(_req(ADMIN_MEMBER_EMAIL, config, clients=clients)) is False


def test_is_admin_admin_groups_empty_never_consults_the_resolver():
    # admin_groups unset (the default): today's behavior, byte for byte --
    # not merely "the answer happens to be False" but "the directory is
    # never even asked," so this can never depend on a resolver that isn't
    # wired yet in a room that hasn't opted in.
    config = Config(admin_groups=[], iap_audience=AUDIENCE)
    resolver = FakeGroupResolver({ADMIN_MEMBER_EMAIL: [ADMIN_GROUP]})
    clients = SimpleNamespace(groups=resolver)

    assert _is_admin(_req(ADMIN_MEMBER_EMAIL, config, clients=clients)) is False
    assert resolver.calls == []


def test_is_admin_no_email_at_all_is_false():
    config = Config(
        admin_groups=[ADMIN_GROUP],
        groups=[GroupChoice(name=ADMIN_GROUP, okta_group="okta-infra-engineering")],
        iap_audience=AUDIENCE,
    )

    assert _is_admin(_req(None, config)) is False


# --- Route-level: real TestClient, real IAP JWT --------------------------------


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
        admin_groups=[ADMIN_GROUP],
        groups=[GroupChoice(name=ADMIN_GROUP, okta_group="okta-infra-engineering")],
        iap_audience=AUDIENCE,
    )


def _make_client(session_factory, config, *, groups_resolver=None, storage=None, iap_jwks_fetcher):
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
                storage=storage if storage is not None else FakeStorage(),
                groups=groups_resolver,
            ),
            iap_jwks_fetcher=iap_jwks_fetcher,
        )
    )


def _seed_agent(
    session_factory,
    config,
    *,
    name: str,
    created_by: str | None = None,
    group_name: str | None = None,
    status=AgentStatus.READY,
):
    session = session_factory()
    session.add(
        Agent(
            name=name,
            persona=PERSONA,
            model_id=config.models[0].id,
            status=status,
            repo_full_name=REPO_TEMPLATE.format(name=name),
            created_by=created_by,
            group_name=group_name,
        )
    )
    session.commit()
    session.close()


def _seed_invite(session_factory, *, from_agent, to_agent, status="pending"):
    session = session_factory()
    invite = A2AInvite(from_agent=from_agent, to_agent=to_agent, status=status)
    session.add(invite)
    session.commit()
    invite_id = invite.id
    session.close()
    return invite_id


# --- visibility: an admin-group member sees every agent -----------------------


def test_admin_group_member_sees_every_agent(session_factory, config):
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver(
        {ADMIN_MEMBER_EMAIL: [ADMIN_GROUP], PLAIN_MEMBER_EMAIL: [OTHER_GROUP]}
    )
    client = _make_client(
        session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk)
    )
    _seed_agent(session_factory, config, name="a", created_by=OWNER_EMAIL, group_name="growth")
    _seed_agent(
        session_factory, config, name="b", created_by=SECOND_OWNER_EMAIL, group_name="payments-eng"
    )

    response = client.get("/api/agents", headers=_headers(private_key, email=ADMIN_MEMBER_EMAIL))

    assert response.status_code == 200, response.text
    names = {a["name"] for a in response.json()["agents"]}
    assert names == {"a", "b"}


def test_plain_member_sees_only_what_they_own(session_factory, config):
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver(
        {ADMIN_MEMBER_EMAIL: [ADMIN_GROUP], PLAIN_MEMBER_EMAIL: [OTHER_GROUP]}
    )
    client = _make_client(
        session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk)
    )
    _seed_agent(session_factory, config, name="a", created_by=OWNER_EMAIL, group_name="growth")

    response = client.get("/api/agents", headers=_headers(private_key, email=PLAIN_MEMBER_EMAIL))

    assert response.status_code == 200, response.text
    assert response.json()["agents"] == []


# --- mutating gate: an admin-group member passes on an agent they don't own ---


def test_admin_group_member_may_mutate_an_agent_they_do_not_own(session_factory, config):
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({ADMIN_MEMBER_EMAIL: [ADMIN_GROUP]})
    client = _make_client(
        session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk)
    )
    _seed_agent(session_factory, config, name=AGENT_NAME, created_by=OWNER_EMAIL)

    response = client.patch(
        f"/api/agents/{AGENT_NAME}/metadata",
        json={"team": "acme-team"},
        headers=_headers(private_key, email=ADMIN_MEMBER_EMAIL),
    )

    assert response.status_code == 200, response.text


def test_plain_member_still_gets_403_verbatim_on_the_same_gate(session_factory, config):
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({PLAIN_MEMBER_EMAIL: [OTHER_GROUP]})
    client = _make_client(
        session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk)
    )
    # Group-matched so the plain member can actually SEE the agent (the
    # 404-before-403 discipline would otherwise mask the interesting
    # comparison) -- seeing it is not the same as owning or operating it.
    _seed_agent(
        session_factory, config, name=AGENT_NAME, created_by=OWNER_EMAIL, group_name=OTHER_GROUP
    )

    response = client.patch(
        f"/api/agents/{AGENT_NAME}/metadata",
        json={"team": "acme-team"},
        headers=_headers(private_key, email=PLAIN_MEMBER_EMAIL),
    )

    assert response.status_code == 403
    assert response.json() == VERBATIM_403


# --- invites: an admin-group member may act on either side without owning it --


def test_admin_group_member_may_create_an_invite_for_an_agent_they_do_not_own(
    session_factory, config
):
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({ADMIN_MEMBER_EMAIL: [ADMIN_GROUP]})
    client = _make_client(
        session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk)
    )
    _seed_agent(session_factory, config, name="a", created_by=OWNER_EMAIL, group_name="growth")
    _seed_agent(
        session_factory, config, name="b", created_by=SECOND_OWNER_EMAIL, group_name="payments-eng"
    )

    response = client.post(
        "/api/agents/a/invites",
        json={"to_agent": "b"},
        headers=_headers(private_key, email=ADMIN_MEMBER_EMAIL),
    )

    assert response.status_code == 201, response.text


def test_admin_group_member_may_accept_for_a_to_agent_they_do_not_own(session_factory, config):
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({ADMIN_MEMBER_EMAIL: [ADMIN_GROUP]})
    client = _make_client(
        session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk)
    )
    _seed_agent(session_factory, config, name="a", created_by=OWNER_EMAIL, group_name="growth")
    _seed_agent(
        session_factory, config, name="b", created_by=SECOND_OWNER_EMAIL, group_name="payments-eng"
    )
    invite_id = _seed_invite(session_factory, from_agent="a", to_agent="b")

    response = client.post(
        f"/api/invites/{invite_id}/accept",
        headers=_headers(private_key, email=ADMIN_MEMBER_EMAIL),
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "accepted"


# --- whoami: the operator flag reflects _is_admin, not just _is_operator ------


def test_whoami_operator_flag_true_for_admin_group_member_false_for_plain_member(
    session_factory, config
):
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver(
        {ADMIN_MEMBER_EMAIL: [ADMIN_GROUP], PLAIN_MEMBER_EMAIL: [OTHER_GROUP]}
    )
    client = _make_client(
        session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk)
    )

    admin_response = client.get(
        "/api/whoami", headers=_headers(private_key, email=ADMIN_MEMBER_EMAIL)
    )
    plain_response = client.get(
        "/api/whoami", headers=_headers(private_key, email=PLAIN_MEMBER_EMAIL)
    )

    assert admin_response.json()["operator"] is True
    assert plain_response.json()["operator"] is False


# --- resolver not wired at all: group grant off, email operator unaffected ---


def test_resolver_none_group_grant_off_but_email_operator_still_passes(session_factory, config):
    private_key, jwk = _generate_jwk()
    # groups_resolver=None: the directory integration simply isn't wired in
    # this room yet (no okta_domain/groups configured, or the env token is
    # absent) -- Clients.groups stays None regardless of admin_groups.
    client = _make_client(
        session_factory, config, groups_resolver=None, iap_jwks_fetcher=_fetcher(jwk)
    )
    _seed_agent(session_factory, config, name=AGENT_NAME, created_by=OWNER_EMAIL)

    operator_response = client.patch(
        f"/api/agents/{AGENT_NAME}/metadata",
        json={"team": "acme-team"},
        headers=_headers(private_key, email=OPERATOR_EMAIL),
    )
    assert operator_response.status_code == 200, operator_response.text

    admin_group_response = client.patch(
        f"/api/agents/{AGENT_NAME}/metadata",
        json={"team": "acme-team-2"},
        headers=_headers(private_key, email=ADMIN_MEMBER_EMAIL),
    )
    assert admin_group_response.status_code == 403
    assert admin_group_response.json() == VERBATIM_403


# --- admin_groups empty: exactly today's behavior, even if the directory ------
# would have granted it


def test_admin_groups_empty_is_todays_behavior_even_if_the_directory_would_grant_it(
    session_factory, tmp_path
):
    config = Config(
        workdir=tmp_path / "work",
        operators=[OPERATOR_EMAIL],
        admin_groups=[],
        iap_audience=AUDIENCE,
    )
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({ADMIN_MEMBER_EMAIL: [ADMIN_GROUP]})
    client = _make_client(
        session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk)
    )
    # Group-matched (via the ordinary Agent.group_name visibility axis, not
    # admin_groups) so the caller can actually SEE the agent -- proving the
    # 403 below is the owner-or-operator gate doing its job, not just a
    # 404 that would mask the same conclusion either way.
    _seed_agent(
        session_factory, config, name=AGENT_NAME, created_by=OWNER_EMAIL, group_name=ADMIN_GROUP
    )

    response = client.patch(
        f"/api/agents/{AGENT_NAME}/metadata",
        json={"team": "acme-team"},
        headers=_headers(private_key, email=ADMIN_MEMBER_EMAIL),
    )

    assert response.status_code == 403
    assert response.json() == VERBATIM_403


# --- create-agent group-claim exemption: an admin-group member may also -------
# assign any configured group, the same standing an email operator already
# gets from `_owner_or_operator_or_403`'s neighboring gate (final-review fix
# round HIGH-1's consent check) -- both are "operator status everywhere."


def test_admin_group_member_claims_any_configured_group_at_create(
    session_factory, fake_storage, tmp_path
):
    cfg = Config(
        workdir=tmp_path / "work",
        skills_bucket="skills-bucket",
        personas_bucket="personas-bucket",
        operators=[OPERATOR_EMAIL],
        admin_groups=[ADMIN_GROUP],
        iap_audience=AUDIENCE,
        groups=[
            GroupChoice(name="growth", okta_group="okta-growth"),
            GroupChoice(name="payments-eng", okta_group="okta-payments"),
            GroupChoice(name=ADMIN_GROUP, okta_group="okta-infra-engineering"),
        ],
    )
    private_key, jwk = _generate_jwk()
    # The admin-group member belongs to no CONFIGURED room group at all by
    # directory membership -- still allowed to claim one.
    resolver = FakeGroupResolver({ADMIN_MEMBER_EMAIL: [ADMIN_GROUP]})
    client = _make_client(
        session_factory, cfg, groups_resolver=resolver, storage=fake_storage,
        iap_jwks_fetcher=_fetcher(jwk),
    )

    response = client.post(
        "/api/agents",
        json={
            "name": "clio",
            "persona": "helpful-generalist",
            "team": "shared",
            "group_name": "growth",
        },
        headers=_headers(private_key, email=ADMIN_MEMBER_EMAIL),
    )

    assert response.status_code == 201, response.text
