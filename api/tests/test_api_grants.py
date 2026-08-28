"""Tests for per-user visibility grants (access-control arc, Task C1):
`GET/POST/DELETE /api/agents/{name}/grants`, and the wiring into
`_visible_agents` (a granted email sees the agent as if it owned it).

Same real-JWT idiom as the sibling access-control test files.
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
from kobo_api.config import Config
from kobo_api.iap import ASSERTION_HEADER, IAP_ISSUER
from kobo_api.main import create_app
from kobo_api.models import Agent, AgentStatus, AgentUserGrant, Base
from tests.fakes import FakeAgent, FakeCloud, FakeGitHub, FakeGojira, FakeKubectl

PERSONA = "helpful-generalist"
REPO_TEMPLATE = "acme/agent-{name}"

OWNER_EMAIL = "owner@example.com"
OPERATOR_EMAIL = "ops@example.com"
GRANTEE_EMAIL = "grantee@example.com"
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
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config(tmp_path):
    return Config(workdir=tmp_path / "work", operators=[OPERATOR_EMAIL], iap_audience=AUDIENCE)


def _make_client(session_factory, config, iap_jwks_fetcher, *, groups_resolver=None):
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


def _seed_grant(session_factory, *, agent_name, email, added_by=None):
    session = session_factory()
    session.add(AgentUserGrant(agent_name=agent_name, email=email, added_by=added_by))
    session.commit()
    session.close()


@pytest.fixture
def client(session_factory, config):
    private_key, jwk = _generate_jwk()
    c = _make_client(session_factory, config, _fetcher(jwk))
    return c, private_key


# --- POST /api/agents/{name}/grants ------------------------------------------


def test_owner_may_add_a_grant(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_EMAIL)

    response = c.post(
        "/api/agents/a/grants",
        json={"email": GRANTEE_EMAIL},
        headers=_headers(key, email=OWNER_EMAIL),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["email"] == GRANTEE_EMAIL
    assert body["added_by"] == OWNER_EMAIL


def test_operator_may_add_a_grant_without_owning_the_agent(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_EMAIL)

    response = c.post(
        "/api/agents/a/grants",
        json={"email": GRANTEE_EMAIL},
        headers=_headers(key, email=OPERATOR_EMAIL),
    )

    assert response.status_code == 201, response.text


def test_stranger_may_not_add_a_grant(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_EMAIL)

    response = c.post(
        "/api/agents/a/grants",
        json={"email": GRANTEE_EMAIL},
        headers=_headers(key, email=STRANGER_EMAIL),
    )

    assert response.status_code == 403
    assert response.json() == VERBATIM_403


def test_add_grant_unknown_agent_is_404(session_factory, client):
    c, key = client

    response = c.post(
        "/api/agents/ghost/grants",
        json={"email": GRANTEE_EMAIL},
        headers=_headers(key, email=STRANGER_EMAIL),
    )

    assert response.status_code == 404


def test_adding_the_same_grant_twice_is_idempotent(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_EMAIL)

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


def test_empty_email_is_422(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_EMAIL)

    response = c.post(
        "/api/agents/a/grants",
        json={"email": "   "},
        headers=_headers(key, email=OWNER_EMAIL),
    )

    assert response.status_code == 422
    assert "—" not in response.json()["error"]


# --- GET /api/agents/{name}/grants -------------------------------------------


def test_owner_lists_grants(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_EMAIL)
    _seed_grant(session_factory, agent_name="a", email=GRANTEE_EMAIL, added_by=OWNER_EMAIL)

    response = c.get("/api/agents/a/grants", headers=_headers(key, email=OWNER_EMAIL))

    assert response.status_code == 200
    body = response.json()
    assert body["grants"] == [
        {"email": GRANTEE_EMAIL, "added_by": OWNER_EMAIL}
    ] or body["grants"][0]["email"] == GRANTEE_EMAIL


def test_stranger_may_not_list_grants(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_EMAIL)

    response = c.get("/api/agents/a/grants", headers=_headers(key, email=STRANGER_EMAIL))

    assert response.status_code == 403
    assert response.json() == VERBATIM_403


# --- DELETE /api/agents/{name}/grants/{email} --------------------------------


def test_owner_may_remove_a_grant(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_EMAIL)
    _seed_grant(session_factory, agent_name="a", email=GRANTEE_EMAIL)

    response = c.delete(
        f"/api/agents/a/grants/{GRANTEE_EMAIL}", headers=_headers(key, email=OWNER_EMAIL)
    )

    assert response.status_code == 200, response.text

    listing = c.get("/api/agents/a/grants", headers=_headers(key, email=OWNER_EMAIL))
    assert listing.json()["grants"] == []


def test_stranger_may_not_remove_a_grant(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_EMAIL)
    _seed_grant(session_factory, agent_name="a", email=GRANTEE_EMAIL)

    response = c.delete(
        f"/api/agents/a/grants/{GRANTEE_EMAIL}", headers=_headers(key, email=STRANGER_EMAIL)
    )

    assert response.status_code == 403
    assert response.json() == VERBATIM_403


def test_remove_unknown_grant_is_404(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_EMAIL)

    response = c.delete(
        f"/api/agents/a/grants/{GRANTEE_EMAIL}", headers=_headers(key, email=OWNER_EMAIL)
    )

    assert response.status_code == 404


# --- Grants extend visibility through _visible_agents ------------------------
#
# A resolver that answers "wired, but you belong to nothing" is used below
# rather than no resolver at all: with no resolver wired, `_visible_agents`
# is fully open to everyone regardless of grants (the "absent = feature
# off" phase gate), which would make these tests pass even if the grant
# wiring were entirely missing. Wiring a resolver that always answers `[]`
# activates real group-derived filtering (to nothing) so ownership/grants
# are the only things left that can explain a agent showing up.


class _EmptyGroupResolver:
    def groups_for(self, email: str) -> list[str] | None:
        return []


def test_granted_email_sees_the_agent_as_if_owned(session_factory, config):
    private_key, jwk = _generate_jwk()
    c = _make_client(session_factory, config, _fetcher(jwk), groups_resolver=_EmptyGroupResolver())
    _seed_agent(session_factory, name="a", created_by=OWNER_EMAIL, group_name="growth")
    _seed_grant(session_factory, agent_name="a", email=GRANTEE_EMAIL)

    response = c.get("/api/agents", headers=_headers(private_key, email=GRANTEE_EMAIL))

    assert response.status_code == 200
    assert {a["name"] for a in response.json()["agents"]} == {"a"}


def test_granted_email_can_reach_the_agent_detail_route(session_factory, config):
    private_key, jwk = _generate_jwk()
    c = _make_client(session_factory, config, _fetcher(jwk), groups_resolver=_EmptyGroupResolver())
    _seed_agent(session_factory, name="a", created_by=OWNER_EMAIL, group_name="growth")
    _seed_grant(session_factory, agent_name="a", email=GRANTEE_EMAIL)

    response = c.get("/api/agents/a", headers=_headers(private_key, email=GRANTEE_EMAIL))

    assert response.status_code == 200


def test_ungranted_stranger_still_sees_nothing(session_factory, config):
    private_key, jwk = _generate_jwk()
    c = _make_client(session_factory, config, _fetcher(jwk), groups_resolver=_EmptyGroupResolver())
    _seed_agent(session_factory, name="a", created_by=OWNER_EMAIL, group_name="growth")
    _seed_grant(session_factory, agent_name="a", email=GRANTEE_EMAIL)

    response = c.get("/api/agents", headers=_headers(private_key, email=STRANGER_EMAIL))

    assert response.status_code == 200
    assert response.json()["agents"] == []


def test_grant_extends_visibility_even_when_the_group_resolver_is_down(session_factory, config):
    # A grant is kobo's own record, not Okta-derived -- it must survive an
    # Okta outage untouched (Global Constraint's "fail-closed on grants" is
    # about GROUP-derived visibility vanishing, not about rows in this
    # table).
    class DownResolver:
        def groups_for(self, email: str) -> list[str] | None:
            return None

    private_key, jwk = _generate_jwk()
    c = _make_client(session_factory, config, _fetcher(jwk), groups_resolver=DownResolver())
    _seed_agent(session_factory, name="a", created_by=OWNER_EMAIL, group_name="growth")
    _seed_grant(session_factory, agent_name="a", email=GRANTEE_EMAIL)

    response = c.get("/api/agents", headers=_headers(private_key, email=GRANTEE_EMAIL))

    assert response.status_code == 200
    body = response.json()
    assert body["groups_unavailable"] is True
    assert {a["name"] for a in body["agents"]} == {"a"}


def test_grant_email_match_is_case_insensitive(session_factory, config):
    private_key, jwk = _generate_jwk()
    c = _make_client(session_factory, config, _fetcher(jwk), groups_resolver=_EmptyGroupResolver())
    _seed_agent(session_factory, name="a", created_by=OWNER_EMAIL, group_name="growth")
    _seed_grant(session_factory, agent_name="a", email="Grantee@Example.com")

    response = c.get("/api/agents", headers=_headers(private_key, email=GRANTEE_EMAIL.upper()))

    assert response.status_code == 200
    assert {a["name"] for a in response.json()["agents"]} == {"a"}


# --- global reader role (reader-role wave, 2026-08-28) --------------------------


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


def test_reader_group_member_sees_every_agent(session_factory, config, tmp_path):
    private_key, jwk = _generate_jwk()
    config = config.model_copy(
        update={"reader_groups": ["engineering_operations_sre_lead"]}
    )
    _seed_agent(session_factory, name="ward", created_by=OWNER_EMAIL, group_name="infra-engineering")
    _seed_agent(session_factory, name="clio", created_by=OWNER_EMAIL, group_name="data-engineering")
    client = _make_client(
        session_factory, config, _fetcher(jwk),
        groups_resolver=_RawResolver(["engineering_operations_sre_lead", "unrelated_group"]),
    )
    body = client.get("/api/agents", headers=_headers(private_key, email=STRANGER_EMAIL)).json()
    assert sorted(a["name"] for a in body["agents"]) == ["clio", "ward"]


def test_reader_gains_no_mutation_power(session_factory, config, tmp_path):
    """Visibility only: a reader who can SEE an agent still cannot teach it,
    grant on it, or destroy it -- the owner-or-operator gates never consult
    the reader role."""
    private_key, jwk = _generate_jwk()
    config = config.model_copy(
        update={"reader_groups": ["engineering_operations_sre_lead"]}
    )
    _seed_agent(session_factory, name="ward", created_by=OWNER_EMAIL, group_name="infra-engineering")
    client = _make_client(
        session_factory, config, _fetcher(jwk),
        groups_resolver=_RawResolver(["engineering_operations_sre_lead"]),
    )
    headers = _headers(private_key, email=STRANGER_EMAIL)
    r = client.post("/api/agents/ward/grants", json={"email": "x@example.com"}, headers=headers)
    assert r.status_code == 403
    r = client.delete("/api/agents/ward", headers=headers)
    assert r.status_code == 403


def test_directory_outage_reads_as_not_a_reader(session_factory, config, tmp_path):
    private_key, jwk = _generate_jwk()
    config = config.model_copy(
        update={"reader_groups": ["engineering_operations_sre_lead"]}
    )
    _seed_agent(session_factory, name="ward", created_by=OWNER_EMAIL, group_name="infra-engineering")

    class _DownResolver:
        def groups_for(self, email):
            return None

        def raw_groups_for(self, email):
            return None

    client = _make_client(
        session_factory, config, _fetcher(jwk), groups_resolver=_DownResolver()
    )
    body = client.get("/api/agents", headers=_headers(private_key, email=STRANGER_EMAIL)).json()
    assert body["agents"] == []
