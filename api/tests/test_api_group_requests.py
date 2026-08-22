"""Tests for group requests (shared-group + group-requests wave): the
dropdown's escape hatch for "my team's group isn't listed." `POST
/api/group-requests` is open to any signed-in caller; reading the queue
(`GET /api/group-requests`) and closing a request out (`POST .../done`,
`POST .../declined`) are admin-only, gated by `_is_admin` directly. Also
covers whoami's `pending_group_requests` badge, present only for admins.

Same real-JWT idiom as the sibling access-control test files
(test_api_grants.py, test_web_visibility.py): a locally generated ES256
keypair signs the assertion, verified through the actual `IapMiddleware`/
`IapVerifier`, not a mock.
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
from kobo_api.models import (
    GROUP_REQUEST_DECLINED,
    GROUP_REQUEST_DONE,
    Base,
    GroupRequest,
)
from tests.fakes import FakeAgent, FakeCloud, FakeGitHub, FakeGojira, FakeKubectl

OPERATOR_EMAIL = "ops@example.com"
REQUESTER_EMAIL = "requester@example.com"
OTHER_EMAIL = "other@example.com"

AUDIENCE = "test-audience"
KID = "test-key-1"

ADMIN_ONLY_403 = {"error": "Group requests are visible to room admins only."}


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


@pytest.fixture
def client(session_factory, config):
    private_key, jwk = _generate_jwk()
    c = _make_client(session_factory, config, _fetcher(jwk))
    return c, private_key


def _seed_request(session_factory, *, group_name, requested_by=None, note=None, status="pending"):
    session = session_factory()
    row = GroupRequest(
        group_name=group_name, requested_by=requested_by, note=note, status=status
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    row_id = row.id
    session.close()
    return row_id


# --- POST /api/group-requests: any signed-in caller -------------------------


def test_any_signed_in_caller_may_file_a_request(session_factory, client):
    c, key = client

    response = c.post(
        "/api/group-requests",
        json={"group_name": "growth-emea", "note": "we're a new regional pod"},
        headers=_headers(key, email=REQUESTER_EMAIL),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["group_name"] == "growth-emea"
    assert body["note"] == "we're a new regional pod"
    assert body["requested_by"] == REQUESTER_EMAIL
    assert body["status"] == "pending"
    assert body["decided_by"] is None
    assert body["decided_at"] is None


def test_note_is_optional(session_factory, client):
    c, key = client

    response = c.post(
        "/api/group-requests",
        json={"group_name": "growth-emea"},
        headers=_headers(key, email=REQUESTER_EMAIL),
    )

    assert response.status_code == 201, response.text
    assert response.json()["note"] is None


def test_empty_group_name_is_422(session_factory, client):
    c, key = client

    response = c.post(
        "/api/group-requests",
        json={"group_name": "   "},
        headers=_headers(key, email=REQUESTER_EMAIL),
    )

    assert response.status_code == 422
    assert "—" not in response.json()["error"]


def test_group_name_over_max_length_is_422(session_factory, client):
    c, key = client

    response = c.post(
        "/api/group-requests",
        json={"group_name": "x" * 101},
        headers=_headers(key, email=REQUESTER_EMAIL),
    )

    assert response.status_code == 422


def test_group_name_at_max_length_is_accepted(session_factory, client):
    c, key = client

    response = c.post(
        "/api/group-requests",
        json={"group_name": "x" * 100},
        headers=_headers(key, email=REQUESTER_EMAIL),
    )

    assert response.status_code == 201, response.text


def test_duplicate_pending_request_is_409(session_factory, client):
    c, key = client
    _seed_request(session_factory, group_name="growth-emea", requested_by=OTHER_EMAIL)

    response = c.post(
        "/api/group-requests",
        json={"group_name": "growth-emea"},
        headers=_headers(key, email=REQUESTER_EMAIL),
    )

    assert response.status_code == 409


def test_duplicate_check_is_case_and_whitespace_insensitive(session_factory, client):
    c, key = client
    _seed_request(session_factory, group_name="growth-emea", requested_by=OTHER_EMAIL)

    response = c.post(
        "/api/group-requests",
        json={"group_name": "  Growth-EMEA  "},
        headers=_headers(key, email=REQUESTER_EMAIL),
    )

    assert response.status_code == 409


def test_a_new_request_is_fine_after_the_earlier_one_was_declined(session_factory, client):
    # Only PENDING rows count as a duplicate -- a decided one is history,
    # not an open ask still sitting in the queue.
    c, key = client
    _seed_request(
        session_factory, group_name="growth-emea", requested_by=OTHER_EMAIL,
        status=GROUP_REQUEST_DECLINED,
    )

    response = c.post(
        "/api/group-requests",
        json={"group_name": "growth-emea"},
        headers=_headers(key, email=REQUESTER_EMAIL),
    )

    assert response.status_code == 201, response.text


def test_two_different_pending_requests_from_different_people_both_succeed(
    session_factory, client
):
    c, key = client

    first = c.post(
        "/api/group-requests",
        json={"group_name": "growth-emea"},
        headers=_headers(key, email=REQUESTER_EMAIL),
    )
    second = c.post(
        "/api/group-requests",
        json={"group_name": "growth-apac"},
        headers=_headers(key, email=OTHER_EMAIL),
    )

    assert first.status_code == 201
    assert second.status_code == 201


# --- GET /api/group-requests: admin only ------------------------------------


def test_non_admin_may_not_list_requests(session_factory, client):
    c, key = client
    _seed_request(session_factory, group_name="growth-emea")

    response = c.get("/api/group-requests", headers=_headers(key, email=REQUESTER_EMAIL))

    assert response.status_code == 403
    assert response.json() == ADMIN_ONLY_403


def test_admin_lists_requests_newest_first(session_factory, client):
    c, key = client
    older_id = _seed_request(session_factory, group_name="growth-emea", requested_by=REQUESTER_EMAIL)
    newer_id = _seed_request(session_factory, group_name="growth-apac", requested_by=OTHER_EMAIL)

    response = c.get("/api/group-requests", headers=_headers(key, email=OPERATOR_EMAIL))

    assert response.status_code == 200
    ids = [r["id"] for r in response.json()["requests"]]
    assert ids.index(newer_id) < ids.index(older_id)


def test_unauthenticated_caller_may_not_list_requests_when_iap_is_on(session_factory, client):
    c, _key = client

    response = c.get("/api/group-requests")

    # IapMiddleware itself rejects this before the route ever runs -- a
    # missing assertion header is a 401, not this route's own 403.
    assert response.status_code == 401


def test_group_requests_are_admin_gated_even_with_iap_entirely_off(tmp_path):
    # Deliberately the opposite escape hatch every other operator-only gate
    # in main.py takes ("IAP off = trust the perimeter, there's no identity
    # to check against"): a room that never turned IAP on, or never named an
    # operator, must still keep the queue closed by default -- there IS
    # something worth protecting here (another human's name and note) even
    # with no perimeter configured at all.
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


# --- POST /api/group-requests/{id}/done and /declined: admin only ----------


def test_non_admin_may_not_close_a_request(session_factory, client):
    c, key = client
    request_id = _seed_request(session_factory, group_name="growth-emea")

    response = c.post(
        f"/api/group-requests/{request_id}/done",
        headers=_headers(key, email=REQUESTER_EMAIL),
    )

    assert response.status_code == 403
    assert response.json() == ADMIN_ONLY_403


def test_admin_marks_a_request_done(session_factory, client):
    c, key = client
    request_id = _seed_request(session_factory, group_name="growth-emea")

    response = c.post(
        f"/api/group-requests/{request_id}/done",
        headers=_headers(key, email=OPERATOR_EMAIL),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == GROUP_REQUEST_DONE
    assert body["decided_by"] == OPERATOR_EMAIL
    assert body["decided_at"] is not None


def test_admin_declines_a_request(session_factory, client):
    c, key = client
    request_id = _seed_request(session_factory, group_name="growth-emea")

    response = c.post(
        f"/api/group-requests/{request_id}/declined",
        headers=_headers(key, email=OPERATOR_EMAIL),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == GROUP_REQUEST_DECLINED
    assert body["decided_by"] == OPERATOR_EMAIL


def test_closing_an_already_done_request_is_409(session_factory, client):
    c, key = client
    request_id = _seed_request(session_factory, group_name="growth-emea", status=GROUP_REQUEST_DONE)

    response = c.post(
        f"/api/group-requests/{request_id}/done",
        headers=_headers(key, email=OPERATOR_EMAIL),
    )

    assert response.status_code == 409


def test_declining_an_already_declined_request_is_409(session_factory, client):
    c, key = client
    request_id = _seed_request(
        session_factory, group_name="growth-emea", status=GROUP_REQUEST_DECLINED
    )

    response = c.post(
        f"/api/group-requests/{request_id}/declined",
        headers=_headers(key, email=OPERATOR_EMAIL),
    )

    assert response.status_code == 409


def test_closing_an_unknown_request_id_is_404(session_factory, client):
    c, key = client

    response = c.post(
        "/api/group-requests/999999/done",
        headers=_headers(key, email=OPERATOR_EMAIL),
    )

    assert response.status_code == 404


# --- whoami's pending_group_requests badge: admin-only ----------------------


def test_whoami_reports_pending_count_for_an_admin(session_factory, client):
    c, key = client
    _seed_request(session_factory, group_name="growth-emea")
    _seed_request(session_factory, group_name="growth-apac")
    _seed_request(session_factory, group_name="growth-latam", status=GROUP_REQUEST_DONE)

    response = c.get("/api/whoami", headers=_headers(key, email=OPERATOR_EMAIL))

    assert response.status_code == 200
    assert response.json()["pending_group_requests"] == 2


def test_whoami_omits_pending_count_for_a_non_admin(session_factory, client):
    c, key = client
    _seed_request(session_factory, group_name="growth-emea")

    response = c.get("/api/whoami", headers=_headers(key, email=REQUESTER_EMAIL))

    assert response.status_code == 200
    assert "pending_group_requests" not in response.json()


def test_whoami_reports_zero_for_an_admin_with_an_empty_queue(session_factory, client):
    c, key = client

    response = c.get("/api/whoami", headers=_headers(key, email=OPERATOR_EMAIL))

    assert response.status_code == 200
    assert response.json()["pending_group_requests"] == 0
