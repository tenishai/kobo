"""Tests for `POST /api/admin/sync-pairs` (access-control arc, final-review
fix round, HIGH-2b): an operator-triggered, on-demand resync of the
switchboard's dispatch-pairs policy -- the third leg (alongside the boot-
time sync and the "every trigger re-pushes the full current set" guarantee,
both covered in test_pairs.py) of "a lost revoke heals at the next boot,
next trigger, or operator action."

Same real-JWT idiom test_web_destroy.py/test_api_invites.py use: a locally
generated ES256 keypair signs the IAP assertion, verified through the
actual `IapMiddleware`/`IapVerifier`. Gate coverage mirrors test_web_
destroy.py's own shape (unit-level `_admin_operator_block` calls, then
route-level stranger/operator/IAP-off cases); the sync behavior itself
(success/failure/unwired) is proven against a `FakeCabal`, same idiom
test_api_invites.py's `cabal_client` fixture already established.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.clients import Clients
from kobo_api.config import Config
from kobo_api.iap import ASSERTION_HEADER, IAP_ISSUER
from kobo_api.main import _admin_operator_block, create_app
from kobo_api.models import Agent, AgentStatus, Base
from tests.fakes import (
    FakeAgent,
    FakeCabal,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
)

PERSONA = "helpful-generalist"
MODEL_ID = "vertex_ai/claude-sonnet-4-5"
REPO_TEMPLATE = "acme/agent-{name}"

OPERATOR_EMAIL = "ops@example.com"
STRANGER_EMAIL = "stranger@example.com"

AUDIENCE = "test-audience"
KID = "test-key-1"

VERBATIM_403 = {"error": "Syncing the switchboard is operator-only on this room"}
SWITCHBOARD_NOT_CONFIGURED = {"error": "the switchboard is not configured"}


def _req(email: str | None = None):
    state = SimpleNamespace()
    if email is not None:
        state.iap_email = email
    return SimpleNamespace(state=state)


# --- Unit-level: _admin_operator_block called directly -----------------------


def test_helper_empty_operators_is_none_even_with_iap_on_and_no_email():
    config = Config(operators=[], iap_audience=AUDIENCE)

    assert _admin_operator_block(_req(), config) is None


def test_helper_iap_off_is_none_even_with_operators_set_and_no_email():
    config = Config(operators=["ops@example.com"], iap_audience="")

    assert _admin_operator_block(_req(), config) is None


def test_helper_non_operator_email_returns_403_body_verbatim():
    config = Config(operators=["ops@example.com"], iap_audience=AUDIENCE)

    result = _admin_operator_block(_req(STRANGER_EMAIL), config)

    assert result is not None
    assert result.status_code == 403
    assert json.loads(result.body) == VERBATIM_403


def test_helper_no_email_at_all_returns_403():
    config = Config(operators=["ops@example.com"], iap_audience=AUDIENCE)

    result = _admin_operator_block(_req(None), config)

    assert result is not None
    assert result.status_code == 403


def test_helper_operator_email_case_insensitive_is_none():
    config = Config(operators=["OPS@EXAMPLE.COM"], iap_audience=AUDIENCE)

    assert _admin_operator_block(_req("ops@example.com"), config) is None


# --- Route-level: real TestClient, real IAP JWT -------------------------------


def _generate_jwk(kid: str = KID):
    private_key = ec.generate_private_key(ec.SECP256R1())
    jwk = jwt.algorithms.ECAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk["kid"] = kid
    jwk["use"] = "sig"
    jwk["alg"] = "ES256"
    return private_key, jwk


def _sign(private_key, *, email: str, kid: str = KID) -> str:
    now = int(time.time())
    claims = {
        "aud": AUDIENCE,
        "iss": IAP_ISSUER,
        "iat": now,
        "exp": now + 3600,
        "sub": "accounts.google.com:1234567890",
        "email": email,
    }
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


def _enabled_cabal_config(config):
    return config.model_copy(
        update={
            "cabal_url": "http://cabal.internal",
            "agent_endpoint_template": "http://agent-{name}.default.svc.cluster.local",
        }
    )


def _make_client(session_factory, config, iap_jwks_fetcher, *, cabal=None):
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
                cabal=cabal,
            ),
            iap_jwks_fetcher=iap_jwks_fetcher,
        )
    )


def _seed_agent(session_factory, *, name, group_name=None, status=AgentStatus.READY):
    session = session_factory()
    session.add(
        Agent(
            name=name,
            persona=PERSONA,
            model_id=MODEL_ID,
            status=status,
            repo_full_name=REPO_TEMPLATE.format(name=name),
            group_name=group_name,
        )
    )
    session.commit()
    session.close()


def test_stranger_is_403_verbatim(session_factory, config, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_cabal_config(config)
    private_key, jwk = _generate_jwk()
    client = _make_client(session_factory, cfg, _fetcher(jwk), cabal=FakeCabal())

    response = client.post(
        "/api/admin/sync-pairs", headers=_headers(private_key, email=STRANGER_EMAIL)
    )

    assert response.status_code == 403
    assert response.json() == VERBATIM_403


def test_operator_gets_200_and_the_pushed_count(session_factory, config, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_cabal_config(config)
    private_key, jwk = _generate_jwk()
    _seed_agent(session_factory, name="a", group_name="growth")
    _seed_agent(session_factory, name="b", group_name="growth")
    fake_cabal = FakeCabal()
    client = _make_client(session_factory, cfg, _fetcher(jwk), cabal=fake_cabal)

    response = client.post(
        "/api/admin/sync-pairs", headers=_headers(private_key, email=OPERATOR_EMAIL)
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"count": 2}
    assert fake_cabal.put_dispatch_pairs_calls == [
        sorted({("agent:a", "b"), ("agent:b", "a")})
    ]


def test_operator_gets_409_when_switchboard_not_configured(session_factory, config):
    # No cabal_url/token/client at all -- cabal_enabled is false.
    private_key, jwk = _generate_jwk()
    client = _make_client(session_factory, config, _fetcher(jwk))

    response = client.post(
        "/api/admin/sync-pairs", headers=_headers(private_key, email=OPERATOR_EMAIL)
    )

    assert response.status_code == 409
    assert response.json() == SWITCHBOARD_NOT_CONFIGURED


def test_operator_gets_502_when_the_push_fails(session_factory, config, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_cabal_config(config)
    private_key, jwk = _generate_jwk()
    fake_cabal = FakeCabal(put_dispatch_pairs_error="switchboard unreachable")
    client = _make_client(session_factory, cfg, _fetcher(jwk), cabal=fake_cabal)

    response = client.post(
        "/api/admin/sync-pairs", headers=_headers(private_key, email=OPERATOR_EMAIL)
    )

    assert response.status_code == 502
    assert "—" not in response.json()["error"]


def test_iap_off_treats_every_caller_as_operator(session_factory, tmp_path, monkeypatch):
    # Dev parity, same escape hatch _operator_block/_slack_operator_block
    # already use: no verified perimeter means the room trusts every
    # caller.
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    config = Config(
        workdir=tmp_path / "work",
        operators=[OPERATOR_EMAIL],
        iap_audience="",
        cabal_url="http://cabal.internal",
        agent_endpoint_template="http://agent-{name}.default.svc.cluster.local",
    )
    fake_cabal = FakeCabal()
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
                cabal=fake_cabal,
            ),
        )
    )

    response = client.post("/api/admin/sync-pairs")

    assert response.status_code == 200
    assert response.json() == {"count": 0}
