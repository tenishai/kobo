"""Tests for `GET /api/agents/{name}/requestable` (access-tab refinement
wave, requestable directory): owner-or-operator of `{name}`, ALL other ready
agents REGARDLESS of the caller's own personal visibility (the deliberate
spec-5 "directory reads are global" surface -- consent needs a directory to
ask from; existence of a directory-global name is not itself a leak), each
capped to `{"name", "group", "team", "capability", "purpose_tags"}`,
excluding `{name}` itself, its same-group colleagues (already auto-paired,
kobo_api.pairs.allowed_pairs), and any agent `{name}` already has a pending
or accepted invite toward (surfaced instead under `"already"`).

Same real-JWT/owner-or-operator harness shape as test_api_invites.py.
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
from kobo_api.config import SHARED_GROUP, Config
from kobo_api.iap import ASSERTION_HEADER, IAP_ISSUER
from kobo_api.main import create_app
from kobo_api.models import A2AInvite, Agent, AgentStatus, Base
from tests.fakes import FakeAgent, FakeCloud, FakeGitHub, FakeGojira, FakeKubectl

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
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config(tmp_path):
    return Config(workdir=tmp_path / "work", operators=[OPERATOR_EMAIL], iap_audience=AUDIENCE)


def _make_client(session_factory, config, iap_jwks_fetcher):
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
            ),
            iap_jwks_fetcher=iap_jwks_fetcher,
        )
    )


def _seed_agent(
    session_factory,
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
            capability=capability,
            purpose_tags=encode_tags(purpose_tags) if purpose_tags is not None else None,
        )
    )
    session.commit()
    session.close()


def _seed_invite(session_factory, *, from_agent, to_agent, status="pending"):
    session = session_factory()
    invite = A2AInvite(from_agent=from_agent, to_agent=to_agent, status=status)
    session.add(invite)
    session.commit()
    session.close()


@pytest.fixture
def client(session_factory, config):
    private_key, jwk = _generate_jwk()
    c = _make_client(session_factory, config, _fetcher(jwk))
    return c, private_key


# --- Owner-or-operator gate, 404-before-403 discipline --------------------------


def test_unknown_agent_is_404(session_factory, client):
    c, key = client

    response = c.get("/api/agents/ghost/requestable", headers=_headers(key, email=STRANGER_EMAIL))

    assert response.status_code == 404


def test_stranger_is_403(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")

    response = c.get("/api/agents/a/requestable", headers=_headers(key, email=STRANGER_EMAIL))

    assert response.status_code == 403
    assert response.json() == VERBATIM_403


def test_owner_may_list(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent(session_factory, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")

    response = c.get("/api/agents/a/requestable", headers=_headers(key, email=OWNER_A_EMAIL))

    assert response.status_code == 200


def test_operator_may_list_without_owning(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent(session_factory, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")

    response = c.get("/api/agents/a/requestable", headers=_headers(key, email=OPERATOR_EMAIL))

    assert response.status_code == 200


# --- Directory reads are global: visibility bypassed -----------------------------


def test_requestable_includes_agents_outside_the_callers_own_visibility(session_factory, client):
    # "a" is owned by owner-a, group growth; "b" is owned by owner-b, a
    # completely different group -- with no grant and no shared group, "b"
    # would normally sit outside owner-a's own visible set (access-control
    # Task B2). It must still show up here: the whole point of this route
    # is that consent needs a directory of names to ask from.
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent(session_factory, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")

    response = c.get("/api/agents/a/requestable", headers=_headers(key, email=OWNER_A_EMAIL))

    assert response.status_code == 200
    names = [entry["name"] for entry in response.json()["requestable"]]
    assert names == ["b"]


# --- Response shape: capped fields ------------------------------------------------


def test_requestable_entry_shape_is_capped_to_five_fields(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent(
        session_factory,
        name="b",
        created_by=OWNER_B_EMAIL,
        group_name="payments-eng",
        team="payments",
        capability="Handles refund inquiries.",
        purpose_tags=["billing", "refunds"],
    )

    response = c.get("/api/agents/a/requestable", headers=_headers(key, email=OWNER_A_EMAIL))

    assert response.status_code == 200
    entry = response.json()["requestable"][0]
    assert entry == {
        "name": "b",
        "group": "payments-eng",
        "team": "payments",
        "capability": "Handles refund inquiries.",
        "purpose_tags": ["billing", "refunds"],
    }


def test_requestable_entry_with_no_capability_or_tags_reads_honestly(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent(session_factory, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")

    response = c.get("/api/agents/a/requestable", headers=_headers(key, email=OWNER_A_EMAIL))

    assert response.status_code == 200
    entry = response.json()["requestable"][0]
    assert entry == {
        "name": "b",
        "group": "payments-eng",
        "team": None,
        "capability": None,
        "purpose_tags": [],
    }


# --- Exclusions ---------------------------------------------------------------


def test_requestable_excludes_the_agent_itself(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")

    response = c.get("/api/agents/a/requestable", headers=_headers(key, email=OWNER_A_EMAIL))

    assert response.status_code == 200
    assert response.json() == {"requestable": [], "already": [], "not_ready": []}


def test_requestable_excludes_same_group_colleagues_already_auto_paired(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent(session_factory, name="b", created_by=OWNER_B_EMAIL, group_name="growth")
    _seed_agent(session_factory, name="c", created_by=OWNER_B_EMAIL, group_name="payments-eng")

    response = c.get("/api/agents/a/requestable", headers=_headers(key, email=OWNER_A_EMAIL))

    assert response.status_code == 200
    names = [entry["name"] for entry in response.json()["requestable"]]
    assert names == ["c"]


def test_requestable_includes_other_shared_group_agents(session_factory, client):
    # Fix round (shared-group wave HIGH): two commons agents share a real,
    # equal, non-null group_name (SHARED_GROUP) but are NOT auto-paired
    # (pairs.allowed_pairs deliberately excludes SHARED_GROUP from implicit
    # pairing) -- so unlike a real shared group, they must NOT vanish from
    # each other's requestable list. This route's exclusion has to go
    # through the same implicitly_paired predicate allowed_pairs itself
    # uses, not the raw same-group-name check.
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name=SHARED_GROUP)
    _seed_agent(session_factory, name="b", created_by=OWNER_B_EMAIL, group_name=SHARED_GROUP)

    response = c.get("/api/agents/a/requestable", headers=_headers(key, email=OWNER_A_EMAIL))

    assert response.status_code == 200
    names = [entry["name"] for entry in response.json()["requestable"]]
    assert names == ["b"]


def test_requestable_excludes_not_ready_agents(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent(
        session_factory,
        name="b",
        created_by=OWNER_B_EMAIL,
        group_name="payments-eng",
        status=AgentStatus.PROVISIONING,
    )

    response = c.get("/api/agents/a/requestable", headers=_headers(key, email=OWNER_A_EMAIL))

    assert response.status_code == 200
    assert response.json() == {
        "requestable": [],
        "already": [],
        "not_ready": [{"name": "b", "group": "payments-eng", "status": "provisioning"}],
    }


def test_requestable_moves_pending_invite_target_to_already(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent(session_factory, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")
    _seed_invite(session_factory, from_agent="a", to_agent="b", status="pending")

    response = c.get("/api/agents/a/requestable", headers=_headers(key, email=OWNER_A_EMAIL))

    assert response.status_code == 200
    body = response.json()
    assert body["requestable"] == []
    assert body["already"] == [{"name": "b", "status": "pending"}]


def test_requestable_moves_accepted_invite_target_to_already(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent(session_factory, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")
    _seed_invite(session_factory, from_agent="a", to_agent="b", status="accepted")

    response = c.get("/api/agents/a/requestable", headers=_headers(key, email=OWNER_A_EMAIL))

    assert response.status_code == 200
    body = response.json()
    assert body["requestable"] == []
    assert body["already"] == [{"name": "b", "status": "accepted"}]


def test_requestable_a_declined_invite_target_stays_requestable(session_factory, client):
    # Mirrors create_invite's own "declined doesn't block a fresh request"
    # rule (test_api_invites.py) -- a declined invite is not "in flight".
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent(session_factory, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")
    _seed_invite(session_factory, from_agent="a", to_agent="b", status="declined")

    response = c.get("/api/agents/a/requestable", headers=_headers(key, email=OWNER_A_EMAIL))

    assert response.status_code == 200
    body = response.json()
    assert [e["name"] for e in body["requestable"]] == ["b"]
    assert body["already"] == []


def test_requestable_a_revoked_invite_target_stays_requestable(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent(session_factory, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")
    _seed_invite(session_factory, from_agent="a", to_agent="b", status="revoked")

    response = c.get("/api/agents/a/requestable", headers=_headers(key, email=OWNER_A_EMAIL))

    assert response.status_code == 200
    body = response.json()
    assert [e["name"] for e in body["requestable"]] == ["b"]
    assert body["already"] == []


def test_requestable_an_invite_in_the_reverse_direction_does_not_count_as_already(
    session_factory, client
):
    # "already" only tracks invites FROM `{name}`, never incoming ones --
    # b having asked a does not mean a has asked b.
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent(session_factory, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")
    _seed_invite(session_factory, from_agent="b", to_agent="a", status="pending")

    response = c.get("/api/agents/a/requestable", headers=_headers(key, email=OWNER_A_EMAIL))

    assert response.status_code == 200
    body = response.json()
    assert [e["name"] for e in body["requestable"]] == ["b"]
    assert body["already"] == []
