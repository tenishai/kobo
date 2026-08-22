"""Tests for cross-group colleagueship invites (access-control arc, Task
C1): `POST/GET /api/agents/{name}/invites`, `POST /api/invites/{id}/accept`,
`POST /api/invites/{id}/decline`, `DELETE /api/invites/{id}`.

Same real-JWT idiom test_web_ownership_gate.py/test_web_visibility.py use:
a locally generated ES256 keypair signs the assertion, verified through the
actual `IapMiddleware`/`IapVerifier`. Covers the full lifecycle, the
permission matrix per route (404-before-403 discipline on the FROM side),
and the leak-shape rule on the TO agent embedded in every invite response
(name + group only, never anything else about an agent the requester may
not otherwise be able to see).
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


def _seed_agent(session_factory, *, name, created_by=None, group_name=None, status=AgentStatus.READY):
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
        )
    )
    session.commit()
    session.close()


def _seed_invite(session_factory, *, from_agent, to_agent, status="pending", requested_by=None):
    session = session_factory()
    invite = A2AInvite(
        from_agent=from_agent, to_agent=to_agent, status=status, requested_by=requested_by
    )
    session.add(invite)
    session.commit()
    invite_id = invite.id
    session.close()
    return invite_id


@pytest.fixture
def client(session_factory, config):
    private_key, jwk = _generate_jwk()
    c = _make_client(session_factory, config, _fetcher(jwk))
    return c, private_key


# --- POST /api/agents/{name}/invites: creation --------------------------------


def test_owner_of_from_agent_may_create_an_invite(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent(session_factory, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")

    response = c.post(
        "/api/agents/a/invites",
        json={"to_agent": "b"},
        headers=_headers(key, email=OWNER_A_EMAIL),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["from_agent"] == "a"
    assert body["to_agent"] == {"name": "b", "group_name": "payments-eng"}
    assert body["status"] == "pending"
    assert body["requested_by"] == OWNER_A_EMAIL
    assert body["decided_by"] is None
    assert body["decided_at"] is None


def test_operator_may_create_an_invite_for_an_agent_they_do_not_own(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent(session_factory, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")

    response = c.post(
        "/api/agents/a/invites",
        json={"to_agent": "b"},
        headers=_headers(key, email=OPERATOR_EMAIL),
    )

    assert response.status_code == 201, response.text


def test_stranger_to_from_agent_is_403(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent(session_factory, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")

    response = c.post(
        "/api/agents/a/invites",
        json={"to_agent": "b"},
        headers=_headers(key, email=STRANGER_EMAIL),
    )

    assert response.status_code == 403
    assert response.json() == VERBATIM_403


def test_unknown_from_agent_is_404_before_the_403_would_fire(session_factory, client):
    # The 404-before-403 discipline: an unknown FROM agent name reads as
    # plain not-found, never leaking that a gate even exists for a name
    # that was never real to begin with.
    c, key = client
    _seed_agent(session_factory, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")

    response = c.post(
        "/api/agents/ghost/invites",
        json={"to_agent": "b"},
        headers=_headers(key, email=STRANGER_EMAIL),
    )

    assert response.status_code == 404


def test_unknown_to_agent_is_404(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")

    response = c.post(
        "/api/agents/a/invites",
        json={"to_agent": "ghost"},
        headers=_headers(key, email=OWNER_A_EMAIL),
    )

    assert response.status_code == 404


def test_to_agent_outside_callers_own_visibility_is_still_reachable_by_name(
    session_factory, client
):
    # The adjudicated design: requesting access to a visible-in-directory-
    # but-not-yours agent is the feature. The TO agent here belongs to a
    # different group and a different owner than the caller entirely, and
    # the request still succeeds -- existence of a directory-known name is
    # not a leak (spec section 5, the a2a directory is deliberately global).
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent(session_factory, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")

    response = c.post(
        "/api/agents/a/invites",
        json={"to_agent": "b"},
        headers=_headers(key, email=OWNER_A_EMAIL),
    )

    assert response.status_code == 201, response.text


def test_self_invite_is_422(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")

    response = c.post(
        "/api/agents/a/invites",
        json={"to_agent": "a"},
        headers=_headers(key, email=OWNER_A_EMAIL),
    )

    assert response.status_code == 422
    assert "—" not in response.json()["error"]


def test_same_group_pair_is_422_and_says_so(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent(session_factory, name="b", created_by=OWNER_B_EMAIL, group_name="growth")

    response = c.post(
        "/api/agents/a/invites",
        json={"to_agent": "b"},
        headers=_headers(key, email=OWNER_A_EMAIL),
    )

    assert response.status_code == 422
    assert "—" not in response.json()["error"]


def test_two_group_less_agents_may_still_invite_each_other(session_factory, client):
    # Group-less agents have no implicit pair at all (pairs.py's own
    # property) -- so unlike the same-group case, this is NOT redundant and
    # must be allowed.
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name=None)
    _seed_agent(session_factory, name="b", created_by=OWNER_B_EMAIL, group_name=None)

    response = c.post(
        "/api/agents/a/invites",
        json={"to_agent": "b"},
        headers=_headers(key, email=OWNER_A_EMAIL),
    )

    assert response.status_code == 201, response.text


def test_duplicate_pending_invite_is_409(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent(session_factory, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")
    _seed_invite(session_factory, from_agent="a", to_agent="b", status="pending")

    response = c.post(
        "/api/agents/a/invites",
        json={"to_agent": "b"},
        headers=_headers(key, email=OWNER_A_EMAIL),
    )

    assert response.status_code == 409
    assert "—" not in response.json()["error"]


def test_duplicate_accepted_invite_is_409(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent(session_factory, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")
    _seed_invite(session_factory, from_agent="a", to_agent="b", status="accepted")

    response = c.post(
        "/api/agents/a/invites",
        json={"to_agent": "b"},
        headers=_headers(key, email=OWNER_A_EMAIL),
    )

    assert response.status_code == 409


def test_a_declined_invite_does_not_block_a_fresh_request(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent(session_factory, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")
    _seed_invite(session_factory, from_agent="a", to_agent="b", status="declined")

    response = c.post(
        "/api/agents/a/invites",
        json={"to_agent": "b"},
        headers=_headers(key, email=OWNER_A_EMAIL),
    )

    assert response.status_code == 201, response.text


def test_a_revoked_invite_does_not_block_a_fresh_request(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent(session_factory, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")
    _seed_invite(session_factory, from_agent="a", to_agent="b", status="revoked")

    response = c.post(
        "/api/agents/a/invites",
        json={"to_agent": "b"},
        headers=_headers(key, email=OWNER_A_EMAIL),
    )

    assert response.status_code == 201, response.text


def test_reverse_direction_invite_is_independent_of_an_existing_one(session_factory, client):
    # a->b pending does not block b->a: direction matters, same as
    # pairs.py's own directionality property.
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent(session_factory, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")
    _seed_invite(session_factory, from_agent="a", to_agent="b", status="pending")

    response = c.post(
        "/api/agents/b/invites",
        json={"to_agent": "a"},
        headers=_headers(key, email=OWNER_B_EMAIL),
    )

    assert response.status_code == 201, response.text


# --- GET /api/agents/{name}/invites: incoming + outgoing ---------------------


def test_owner_sees_incoming_and_outgoing_invites(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent(session_factory, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")
    _seed_agent(session_factory, name="c", created_by=OWNER_B_EMAIL, group_name="payments-eng")
    _seed_invite(session_factory, from_agent="a", to_agent="b", status="pending")
    _seed_invite(session_factory, from_agent="c", to_agent="a", status="pending")

    response = c.get("/api/agents/a/invites", headers=_headers(key, email=OWNER_A_EMAIL))

    assert response.status_code == 200
    body = response.json()
    assert len(body["outgoing"]) == 1
    assert body["outgoing"][0]["to_agent"] == {"name": "b", "group_name": "payments-eng"}
    assert len(body["incoming"]) == 1
    assert body["incoming"][0]["from_agent"] == "c"


def test_get_invites_is_404_for_an_unknown_agent(session_factory, client):
    c, key = client

    response = c.get("/api/agents/ghost/invites", headers=_headers(key, email=STRANGER_EMAIL))

    assert response.status_code == 404


def test_get_invites_is_403_for_a_stranger(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")

    response = c.get("/api/agents/a/invites", headers=_headers(key, email=STRANGER_EMAIL))

    assert response.status_code == 403
    assert response.json() == VERBATIM_403


def test_operator_sees_invites_without_owning_the_agent(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")

    response = c.get("/api/agents/a/invites", headers=_headers(key, email=OPERATOR_EMAIL))

    assert response.status_code == 200
    assert response.json() == {"incoming": [], "outgoing": []}


# --- POST /api/invites/{id}/accept, /decline ---------------------------------


def test_owner_of_to_agent_may_accept(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent(session_factory, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")
    invite_id = _seed_invite(session_factory, from_agent="a", to_agent="b", status="pending")

    response = c.post(
        f"/api/invites/{invite_id}/accept", headers=_headers(key, email=OWNER_B_EMAIL)
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "accepted"
    assert body["decided_by"] == OWNER_B_EMAIL
    assert body["decided_at"] is not None


def test_owner_of_to_agent_may_decline(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent(session_factory, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")
    invite_id = _seed_invite(session_factory, from_agent="a", to_agent="b", status="pending")

    response = c.post(
        f"/api/invites/{invite_id}/decline", headers=_headers(key, email=OWNER_B_EMAIL)
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "declined"


def test_operator_may_accept_without_owning_the_to_agent(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent(session_factory, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")
    invite_id = _seed_invite(session_factory, from_agent="a", to_agent="b", status="pending")

    response = c.post(
        f"/api/invites/{invite_id}/accept", headers=_headers(key, email=OPERATOR_EMAIL)
    )

    assert response.status_code == 200, response.text


def test_owner_of_from_agent_may_not_accept_their_own_outgoing_request(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent(session_factory, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")
    invite_id = _seed_invite(session_factory, from_agent="a", to_agent="b", status="pending")

    response = c.post(
        f"/api/invites/{invite_id}/accept", headers=_headers(key, email=OWNER_A_EMAIL)
    )

    assert response.status_code == 403
    assert response.json() == VERBATIM_403


def test_stranger_may_not_accept(session_factory, client):
    # Folds 1+2 (C1 review): a caller with zero standing on EITHER side of
    # the invite (not owner/operator of `a` or `b`) must not be able to
    # tell "this invite exists but isn't mine" from "this invite id was
    # never real" -- so this reads as the same invite-shaped 404 a missing
    # id gets, not the 403 an owner of one side (who already knows the
    # invite exists) would see.
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent(session_factory, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")
    invite_id = _seed_invite(session_factory, from_agent="a", to_agent="b", status="pending")

    response = c.post(
        f"/api/invites/{invite_id}/accept", headers=_headers(key, email=STRANGER_EMAIL)
    )

    assert response.status_code == 404
    assert response.json() == {"error": f"no such invite: {invite_id}"}


def test_accept_by_a_stranger_matches_the_missing_invite_body_shape(session_factory, client):
    # Fold 1 (C1 review): the accept path used to fetch the TO agent through
    # `_visible_agent_or_404`, which leaked the TO agent's real name in its
    # 404 body ({"error": "no such agent: 'b'"}) -- distinguishable from a
    # genuinely missing invite id's body by content alone even though both
    # were 404s. Both are pinned to the identical `"no such invite: {id}"`
    # template (each with its own id, never an agent name) rather than to
    # each other verbatim, since the two ids necessarily differ.
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent(session_factory, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")
    invite_id = _seed_invite(session_factory, from_agent="a", to_agent="b", status="pending")

    real_invite_response = c.post(
        f"/api/invites/{invite_id}/accept", headers=_headers(key, email=STRANGER_EMAIL)
    )
    missing_invite_response = c.post(
        "/api/invites/999999/accept", headers=_headers(key, email=STRANGER_EMAIL)
    )

    assert real_invite_response.status_code == missing_invite_response.status_code == 404
    assert real_invite_response.json() == {"error": f"no such invite: {invite_id}"}
    assert missing_invite_response.json() == {"error": "no such invite: 999999"}


def test_decline_by_a_stranger_matches_the_missing_invite_body_shape(session_factory, client):
    # Same fold, the decline route (shares `_decide_invite` with accept, but
    # pinned separately since it's its own public route).
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent(session_factory, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")
    invite_id = _seed_invite(session_factory, from_agent="a", to_agent="b", status="pending")

    real_invite_response = c.post(
        f"/api/invites/{invite_id}/decline", headers=_headers(key, email=STRANGER_EMAIL)
    )
    missing_invite_response = c.post(
        "/api/invites/999999/decline", headers=_headers(key, email=STRANGER_EMAIL)
    )

    assert real_invite_response.status_code == missing_invite_response.status_code == 404
    assert real_invite_response.json() == {"error": f"no such invite: {invite_id}"}
    assert missing_invite_response.json() == {"error": "no such invite: 999999"}


def test_accept_unknown_invite_id_is_404(session_factory, client):
    c, key = client

    response = c.post("/api/invites/999/accept", headers=_headers(key, email=STRANGER_EMAIL))

    assert response.status_code == 404


def test_accept_an_already_accepted_invite_is_409(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent(session_factory, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")
    invite_id = _seed_invite(session_factory, from_agent="a", to_agent="b", status="accepted")

    response = c.post(
        f"/api/invites/{invite_id}/accept", headers=_headers(key, email=OWNER_B_EMAIL)
    )

    assert response.status_code == 409


def test_decline_an_already_declined_invite_is_409(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent(session_factory, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")
    invite_id = _seed_invite(session_factory, from_agent="a", to_agent="b", status="declined")

    response = c.post(
        f"/api/invites/{invite_id}/decline", headers=_headers(key, email=OWNER_B_EMAIL)
    )

    assert response.status_code == 409


# --- DELETE /api/invites/{id}: revoke, either side's owner -------------------


def test_from_owner_may_revoke_a_pending_invite(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent(session_factory, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")
    invite_id = _seed_invite(session_factory, from_agent="a", to_agent="b", status="pending")

    response = c.delete(
        f"/api/invites/{invite_id}", headers=_headers(key, email=OWNER_A_EMAIL)
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "revoked"


def test_to_owner_may_also_revoke_an_accepted_invite(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent(session_factory, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")
    invite_id = _seed_invite(session_factory, from_agent="a", to_agent="b", status="accepted")

    response = c.delete(
        f"/api/invites/{invite_id}", headers=_headers(key, email=OWNER_B_EMAIL)
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "revoked"


def test_operator_may_revoke(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent(session_factory, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")
    invite_id = _seed_invite(session_factory, from_agent="a", to_agent="b", status="pending")

    response = c.delete(
        f"/api/invites/{invite_id}", headers=_headers(key, email=OPERATOR_EMAIL)
    )

    assert response.status_code == 200


def test_stranger_to_both_sides_may_not_revoke(session_factory, client):
    # Fold 2 (C1 review): this used to be the 403-vs-404 oracle itself --
    # a stranger to both sides got 403 here but 404 against a missing id
    # (test_revoke_unknown_invite_id_is_404 below), letting a caller with
    # zero standing learn "this id names a real invite" from the status
    # code alone, with no body leak even required. Now it's the identical
    # invite-shaped 404.
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent(session_factory, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")
    invite_id = _seed_invite(session_factory, from_agent="a", to_agent="b", status="pending")

    response = c.delete(
        f"/api/invites/{invite_id}", headers=_headers(key, email=STRANGER_EMAIL)
    )

    assert response.status_code == 404
    assert response.json() == {"error": f"no such invite: {invite_id}"}


def test_revoke_by_a_stranger_matches_the_missing_invite_body_shape(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent(session_factory, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")
    invite_id = _seed_invite(session_factory, from_agent="a", to_agent="b", status="pending")

    real_invite_response = c.delete(
        f"/api/invites/{invite_id}", headers=_headers(key, email=STRANGER_EMAIL)
    )
    missing_invite_response = c.delete(
        "/api/invites/999999", headers=_headers(key, email=STRANGER_EMAIL)
    )

    assert real_invite_response.status_code == missing_invite_response.status_code == 404
    assert real_invite_response.json() == {"error": f"no such invite: {invite_id}"}
    assert missing_invite_response.json() == {"error": "no such invite: 999999"}


def test_revoke_unknown_invite_id_is_404(session_factory, client):
    c, key = client

    response = c.delete("/api/invites/999", headers=_headers(key, email=STRANGER_EMAIL))

    assert response.status_code == 404


def test_revoke_an_already_declined_invite_is_409(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent(session_factory, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")
    invite_id = _seed_invite(session_factory, from_agent="a", to_agent="b", status="declined")

    response = c.delete(
        f"/api/invites/{invite_id}", headers=_headers(key, email=OWNER_A_EMAIL)
    )

    assert response.status_code == 409


def test_revoke_an_already_revoked_invite_is_409(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent(session_factory, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")
    invite_id = _seed_invite(session_factory, from_agent="a", to_agent="b", status="revoked")

    response = c.delete(
        f"/api/invites/{invite_id}", headers=_headers(key, email=OWNER_A_EMAIL)
    )

    assert response.status_code == 409


def test_revoke_still_works_when_the_from_agent_no_longer_exists(session_factory, client):
    # The FROM agent was destroyed after the invite was created -- the TO
    # side's owner must still be able to revoke it, treating the vanished
    # FROM side as ownerless (fail-closed for THAT side alone) rather than
    # raising or refusing entirely.
    c, key = client
    _seed_agent(session_factory, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")
    invite_id = _seed_invite(session_factory, from_agent="ghost", to_agent="b", status="pending")

    response = c.delete(
        f"/api/invites/{invite_id}", headers=_headers(key, email=OWNER_B_EMAIL)
    )

    assert response.status_code == 200, response.text


# --- Full lifecycle ----------------------------------------------------------


def test_full_lifecycle_request_accept_then_revoke(session_factory, client):
    c, key = client
    _seed_agent(session_factory, name="a", created_by=OWNER_A_EMAIL, group_name="growth")
    _seed_agent(session_factory, name="b", created_by=OWNER_B_EMAIL, group_name="payments-eng")

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
