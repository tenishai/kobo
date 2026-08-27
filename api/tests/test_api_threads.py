"""Tests for threads (threads arc, Tasks 8-9): a shared, multi-agent
conversation where every participant sees the same messages.

Operator rulings (law) this file pins:
- The initiating human accepts an agent's suggestion or adds participants
  directly (`POST /api/threads`, `POST /api/threads/{id}/participants`).
- Same-group agents join a thread freely; a cross-group agent needs an
  EXISTING accepted `A2AInvite` with every agent already seated -- the same
  consent model `kobo_api.pairs`/`A2AInvite` already establish, no new
  concept.

Same real-JWT idiom test_api_invites.py/test_web_visibility.py use: a
locally generated ES256 keypair signs the assertion, verified through the
actual `IapMiddleware`/`IapVerifier`. Group-derived visibility is faked
with `FakeGroupResolver`, the same plain `groups_for` stand-in test_web_
visibility.py defines for the `GroupResolver` Protocol.
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
from kobo_api.config import Config, GroupChoice
from kobo_api.iap import ASSERTION_HEADER, IAP_ISSUER
from kobo_api.main import create_app
from kobo_api.models import INVITE_ACCEPTED, A2AInvite, Agent, AgentStatus, Base
from tests.fakes import (
    FakeAgent,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
)

PERSONA = "helpful-generalist"
MODEL_ID = "vertex_ai/claude-sonnet-4-5"
REPO_TEMPLATE = "acme/agent-{name}"

OPERATOR_EMAIL = "ops@example.com"
INITIATOR_EMAIL = "initiator@example.com"
GROUP_MEMBER_EMAIL = "member@example.com"
STRANGER_EMAIL = "stranger@example.com"

AUDIENCE = "test-audience"
KID = "test-key-1"

GROUPS = [
    GroupChoice(name="growth", okta_group="okta-growth"),
    GroupChoice(name="payments-eng", okta_group="okta-payments"),
]


class FakeGroupResolver:
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


def _make_client(
    session_factory,
    config,
    *,
    groups_resolver=None,
    iap_jwks_fetcher,
) -> TestClient:
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
                groups=groups_resolver,
            ),
            iap_jwks_fetcher=iap_jwks_fetcher,
        )
    )


def _seed_agent(
    session_factory,
    *,
    name: str,
    group_name: str | None = None,
    status: str = AgentStatus.READY,
    created_by: str | None = None,
) -> None:
    session = session_factory()
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


def _seed_invite(session_factory, *, from_agent: str, to_agent: str, status: str = INVITE_ACCEPTED) -> None:
    session = session_factory()
    session.add(A2AInvite(from_agent=from_agent, to_agent=to_agent, status=status))
    session.commit()
    session.close()


# --- Task 8: thread creation and participation ------------------------------


def test_initiator_creates_thread_naming_one_agent_it_can_see(session_factory, config):
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({INITIATOR_EMAIL: ["growth"]})
    client = _make_client(session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, name="a", group_name="growth")

    response = client.post(
        "/api/threads", json={"agent": "a"}, headers=_headers(private_key, email=INITIATOR_EMAIL)
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["initiator_email"] == INITIATOR_EMAIL
    assert body["participants"] == ["a"]
    assert isinstance(body["id"], int)
    assert "created_at" in body


def test_create_thread_agent_not_ready_is_409(session_factory, config):
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({INITIATOR_EMAIL: ["growth"]})
    client = _make_client(session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, name="a", group_name="growth", status=AgentStatus.PROVISIONING)

    response = client.post(
        "/api/threads", json={"agent": "a"}, headers=_headers(private_key, email=INITIATOR_EMAIL)
    )

    assert response.status_code == 409


def test_create_thread_unknown_agent_is_404(session_factory, config):
    private_key, jwk = _generate_jwk()
    client = _make_client(session_factory, config, iap_jwks_fetcher=_fetcher(jwk))

    response = client.post(
        "/api/threads", json={"agent": "ghost"}, headers=_headers(private_key, email=INITIATOR_EMAIL)
    )

    assert response.status_code == 404
    assert response.json() == {"error": "no such agent: 'ghost'"}


def test_create_thread_agent_the_caller_cannot_see_is_refused_with_anti_enumeration_answer(
    session_factory, config
):
    """Adding an agent the initiator cannot see is refused with the room's
    existing anti-enumeration answer, not a distinguishable error -- the
    same body a genuinely unknown name gets above."""
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({INITIATOR_EMAIL: ["growth"]})
    client = _make_client(session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, name="hidden", group_name="payments-eng")

    response = client.post(
        "/api/threads", json={"agent": "hidden"}, headers=_headers(private_key, email=INITIATOR_EMAIL)
    )

    assert response.status_code == 404
    assert response.json() == {"error": "no such agent: 'hidden'"}


def test_adding_a_same_group_agent_succeeds(session_factory, config):
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({INITIATOR_EMAIL: ["growth"]})
    client = _make_client(session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, name="a", group_name="growth")
    _seed_agent(session_factory, name="b", group_name="growth")
    thread_id = client.post(
        "/api/threads", json={"agent": "a"}, headers=_headers(private_key, email=INITIATOR_EMAIL)
    ).json()["id"]

    response = client.post(
        f"/api/threads/{thread_id}/participants",
        json={"agent": "b"},
        headers=_headers(private_key, email=INITIATOR_EMAIL),
    )

    assert response.status_code == 201, response.text
    assert response.json()["participants"] == ["a", "b"]


def test_adding_a_cross_group_agent_without_an_accepted_invite_is_refused(session_factory, config):
    """Proven from a non-operator identity: INITIATOR_EMAIL never appears in
    config.operators. This reuses the existing consent handshake (`kobo_api.
    pairs.implicitly_paired` / `A2AInvite`) -- it introduces no new consent
    concept and must not bypass the existing one."""
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({INITIATOR_EMAIL: ["growth", "payments-eng"]})
    client = _make_client(session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, name="a", group_name="growth")
    _seed_agent(session_factory, name="c", group_name="payments-eng")
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


def test_adding_a_cross_group_agent_with_an_accepted_invite_is_allowed(session_factory, config):
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({INITIATOR_EMAIL: ["growth", "payments-eng"]})
    client = _make_client(session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, name="a", group_name="growth")
    _seed_agent(session_factory, name="c", group_name="payments-eng")
    _seed_invite(session_factory, from_agent="a", to_agent="c", status=INVITE_ACCEPTED)
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


def test_adding_an_agent_the_initiator_cannot_see_is_refused_with_anti_enumeration_answer(
    session_factory, config
):
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({INITIATOR_EMAIL: ["growth"]})
    client = _make_client(session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, name="a", group_name="growth")
    _seed_agent(session_factory, name="hidden", group_name="payments-eng")
    thread_id = client.post(
        "/api/threads", json={"agent": "a"}, headers=_headers(private_key, email=INITIATOR_EMAIL)
    ).json()["id"]

    response = client.post(
        f"/api/threads/{thread_id}/participants",
        json={"agent": "hidden"},
        headers=_headers(private_key, email=INITIATOR_EMAIL),
    )

    assert response.status_code == 404
    assert response.json() == {"error": "no such agent: 'hidden'"}


def test_adding_a_participant_by_a_non_initiator_is_not_found(session_factory, config):
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({INITIATOR_EMAIL: ["growth"], STRANGER_EMAIL: ["growth"]})
    client = _make_client(session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, name="a", group_name="growth")
    _seed_agent(session_factory, name="b", group_name="growth")
    thread_id = client.post(
        "/api/threads", json={"agent": "a"}, headers=_headers(private_key, email=INITIATOR_EMAIL)
    ).json()["id"]

    response = client.post(
        f"/api/threads/{thread_id}/participants",
        json={"agent": "b"},
        headers=_headers(private_key, email=STRANGER_EMAIL),
    )

    assert response.status_code == 404
    assert response.json() == {"error": f"no such thread: {thread_id}"}


def test_adding_an_already_seated_agent_is_409(session_factory, config):
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({INITIATOR_EMAIL: ["growth"]})
    client = _make_client(session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, name="a", group_name="growth")
    thread_id = client.post(
        "/api/threads", json={"agent": "a"}, headers=_headers(private_key, email=INITIATOR_EMAIL)
    ).json()["id"]

    response = client.post(
        f"/api/threads/{thread_id}/participants",
        json={"agent": "a"},
        headers=_headers(private_key, email=INITIATOR_EMAIL),
    )

    assert response.status_code == 409

