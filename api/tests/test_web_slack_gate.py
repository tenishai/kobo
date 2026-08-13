"""Tests for the `slack_operators` gate (Task 5): the two Slack-connect
routes (`POST .../slack/app`, `POST .../slack/connect`) and the
`GET /api/agents/{name}` detail's `"slack_visible"` field.

Slack connect is operator-assisted by design (P2c) — this gate hides/blocks
it from anyone else once an org names its operator emails. Two layers of
coverage, same shape as test_iap.py:

- Unit-level: `_slack_operator_block` called directly against a minimal
  stand-in for `Request` (only `.state` is read) — no HTTP involved.
- Route-level: real `TestClient` requests, IAP enabled via a real ES256
  keypair/JWT (same idiom as test_iap.py's `_generate_jwk`/`_sign`) so the
  "email not listed" / "email listed, case-insensitive" / "IAP off" cases
  are proven through the actual middleware, not a mock.

Existing slack route tests (test_api_slack.py) are untouched by this file —
they all use `Config()`'s default `slack_operators == []`, so the gate never
engages for them (see the empty-list-is-open tests below for the direct
proof of that default).
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
from kobo_api.main import _slack_operator_block, create_app
from kobo_api.models import Agent, AgentStatus, Base
from kobo_api.personas import seed_default_personas
from tests.fakes import (
    FakeAgent,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
    FakeSlack,
    FakeStorage,
)

AGENT_NAME = "clio"
PERSONA = "helpful-generalist"

BOT_TOKEN = "xoxb-super-secret-bot-token"
APP_TOKEN = "xapp-super-secret-app-token"
CHANNEL = "C1234567"

OPERATOR_EMAIL = "Ops@Example.com"
NON_OPERATOR_EMAIL = "someone-else@example.com"

VERBATIM_403 = {"error": "Slack setup is operator-only on this room"}

AUDIENCE = "test-audience"
KID = "test-key-1"


# --- Unit-level: _slack_operator_block called directly -----------------------


def _req(email: str | None = None):
    """A minimal stand-in for `Request` — the helper only ever reads
    `request.state.iap_email` via `getattr`, so a bare namespace is enough;
    when `email` is None, `.state` carries no `iap_email` attribute at all
    (mirrors a real request that never went through IapMiddleware)."""
    state = SimpleNamespace()
    if email is not None:
        state.iap_email = email
    return SimpleNamespace(state=state)


def test_helper_empty_operators_is_none_even_with_iap_on_and_no_email():
    config = Config(slack_operators=[], iap_audience=AUDIENCE)

    assert _slack_operator_block(_req(), config) is None


def test_helper_iap_off_is_none_even_with_operators_set_and_no_email():
    config = Config(slack_operators=["ops@example.com"], iap_audience="")

    assert _slack_operator_block(_req(), config) is None


def test_helper_non_listed_email_returns_403_body_verbatim():
    config = Config(slack_operators=["ops@example.com"], iap_audience=AUDIENCE)

    result = _slack_operator_block(_req(NON_OPERATOR_EMAIL), config)

    assert result is not None
    assert result.status_code == 403
    assert json.loads(result.body) == VERBATIM_403


def test_helper_no_email_at_all_returns_403():
    config = Config(slack_operators=["ops@example.com"], iap_audience=AUDIENCE)

    result = _slack_operator_block(_req(None), config)

    assert result is not None
    assert result.status_code == 403


def test_helper_listed_email_exact_case_is_none():
    config = Config(slack_operators=["ops@example.com"], iap_audience=AUDIENCE)

    assert _slack_operator_block(_req("ops@example.com"), config) is None


def test_helper_listed_email_operator_list_uppercase_request_lowercase_is_none():
    config = Config(slack_operators=["OPS@EXAMPLE.COM"], iap_audience=AUDIENCE)

    assert _slack_operator_block(_req("ops@example.com"), config) is None


def test_helper_listed_email_operator_list_lowercase_request_uppercase_is_none():
    config = Config(slack_operators=["ops@example.com"], iap_audience=AUDIENCE)

    assert _slack_operator_block(_req("OPS@EXAMPLE.COM"), config) is None


# --- Route-level: real TestClient, real IAP JWT (same idiom as test_iap.py) --


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
        skills_bucket="skills-bucket",
        personas_bucket="personas-bucket",
    )


# Sentinel distinguishing "slack kwarg not passed" (-> default FakeSlack())
# from an explicit `slack=None` (-> Clients.slack is None, the "Slack isn't
# configured" 503 path) — same distinction as test_api_slack.py's own
# _SLACK_UNSET, since plain `None` can't serve as both.
_SLACK_UNSET = object()


def _make_client(session_factory, config, *, slack=_SLACK_UNSET, iap_jwks_fetcher=None):
    storage = FakeStorage()
    seed_default_personas(storage, bucket=config.personas_bucket)
    return TestClient(
        create_app(
            session_factory,
            config=config,
            clients=Clients(
                github=FakeGitHub(),
                gojira=FakeGojira(activate_after=1),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=FakeKubectl(),
                slack=FakeSlack() if slack is _SLACK_UNSET else slack,
                storage=storage,
            ),
            iap_jwks_fetcher=iap_jwks_fetcher,
        )
    )


def _seed_agent(session_factory, config, *, status=AgentStatus.READY, slack_app_id=None):
    session = session_factory()
    session.add(
        Agent(
            name=AGENT_NAME,
            persona=PERSONA,
            model_id=config.models[0].id,
            status=status,
            repo_full_name="acme/agent-clio",
            slack_app_id=slack_app_id,
        )
    )
    session.commit()
    session.close()


# --- empty slack_operators: open, existing 503/flow unchanged ----------------


def test_empty_operators_create_slack_app_still_503s_when_slack_unconfigured(
    session_factory, config
):
    config = config.model_copy(update={"slack_operators": []})
    client = _make_client(session_factory, config, slack=None)
    _seed_agent(session_factory, config)

    response = client.post(f"/api/agents/{AGENT_NAME}/slack/app")

    assert response.status_code == 503
    assert response.json() == {"error": "Slack is not configured"}


def test_empty_operators_connect_slack_still_503s_when_slack_unconfigured(
    session_factory, config
):
    config = config.model_copy(update={"slack_operators": []})
    client = _make_client(session_factory, config, slack=None)
    _seed_agent(session_factory, config, slack_app_id="A0EXISTING")

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"channel": CHANNEL, "bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
    )

    assert response.status_code == 503
    assert response.json() == {"error": "Slack is not configured"}


def test_empty_operators_slack_visible_true(session_factory, config):
    config = config.model_copy(update={"slack_operators": []})
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}")

    assert response.status_code == 200
    assert response.json()["slack_visible"] is True


# --- non-empty operators + IAP on + non-listed email: 403 before anything ----


def test_non_operator_gets_403_from_create_slack_app_before_agent_lookup(
    session_factory, config
):
    """The gate runs before the agent 404 check too — a non-operator gets
    the same 403 whether or not the named agent even exists."""
    private_key, jwk = _generate_jwk()
    op_config = config.model_copy(
        update={"slack_operators": ["ops@example.com"], "iap_audience": AUDIENCE}
    )
    client = _make_client(session_factory, op_config, iap_jwks_fetcher=_fetcher(jwk))
    token = _sign(private_key, email=NON_OPERATOR_EMAIL)

    response = client.post(
        "/api/agents/no-such-agent/slack/app", headers={ASSERTION_HEADER: token}
    )

    assert response.status_code == 403
    assert response.json() == VERBATIM_403


def test_non_operator_gets_403_from_create_slack_app_even_when_agent_ready(
    session_factory, config
):
    private_key, jwk = _generate_jwk()
    op_config = config.model_copy(
        update={"slack_operators": ["ops@example.com"], "iap_audience": AUDIENCE}
    )
    client = _make_client(session_factory, op_config, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, op_config)
    token = _sign(private_key, email=NON_OPERATOR_EMAIL)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/app", headers={ASSERTION_HEADER: token}
    )

    assert response.status_code == 403
    assert response.json() == VERBATIM_403


def test_non_operator_gets_403_from_connect_slack(session_factory, config):
    private_key, jwk = _generate_jwk()
    op_config = config.model_copy(
        update={"slack_operators": ["ops@example.com"], "iap_audience": AUDIENCE}
    )
    client = _make_client(session_factory, op_config, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, op_config, slack_app_id="A0EXISTING")
    token = _sign(private_key, email=NON_OPERATOR_EMAIL)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"channel": CHANNEL, "bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
        headers={ASSERTION_HEADER: token},
    )

    assert response.status_code == 403
    assert response.json() == VERBATIM_403
    assert BOT_TOKEN not in response.text
    assert APP_TOKEN not in response.text


def test_non_operator_slack_visible_false(session_factory, config):
    private_key, jwk = _generate_jwk()
    op_config = config.model_copy(
        update={"slack_operators": ["ops@example.com"], "iap_audience": AUDIENCE}
    )
    client = _make_client(session_factory, op_config, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, op_config)
    token = _sign(private_key, email=NON_OPERATOR_EMAIL)

    response = client.get(
        f"/api/agents/{AGENT_NAME}", headers={ASSERTION_HEADER: token}
    )

    assert response.status_code == 200
    assert response.json()["slack_visible"] is False


# --- non-empty operators + IAP on + listed email: passes through ------------


def test_listed_email_passes_through_to_existing_503(session_factory, config):
    private_key, jwk = _generate_jwk()
    op_config = config.model_copy(
        update={"slack_operators": [OPERATOR_EMAIL], "iap_audience": AUDIENCE}
    )
    client = _make_client(session_factory, op_config, slack=None, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, op_config)
    # Case-insensitive both directions: operator list carries mixed case,
    # the token's email claim is all-lowercase.
    token = _sign(private_key, email=OPERATOR_EMAIL.lower())

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/app", headers={ASSERTION_HEADER: token}
    )

    assert response.status_code == 503
    assert response.json() == {"error": "Slack is not configured"}


def test_listed_email_uppercase_token_lowercase_operator_list_passes_through(
    session_factory, config
):
    private_key, jwk = _generate_jwk()
    op_config = config.model_copy(
        update={"slack_operators": [OPERATOR_EMAIL.lower()], "iap_audience": AUDIENCE}
    )
    client = _make_client(session_factory, op_config, slack=None, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, op_config, slack_app_id="A0EXISTING")
    token = _sign(private_key, email=OPERATOR_EMAIL.upper())

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"channel": CHANNEL, "bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
        headers={ASSERTION_HEADER: token},
    )

    assert response.status_code == 503
    assert response.json() == {"error": "Slack is not configured"}


def test_listed_email_slack_visible_true(session_factory, config):
    private_key, jwk = _generate_jwk()
    op_config = config.model_copy(
        update={"slack_operators": [OPERATOR_EMAIL], "iap_audience": AUDIENCE}
    )
    client = _make_client(session_factory, op_config, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, op_config)
    token = _sign(private_key, email=OPERATOR_EMAIL.lower())

    response = client.get(
        f"/api/agents/{AGENT_NAME}", headers={ASSERTION_HEADER: token}
    )

    assert response.status_code == 200
    assert response.json()["slack_visible"] is True


# --- non-empty operators + IAP OFF: dev parity, treated as operator ---------


def test_operators_set_but_iap_off_create_slack_app_passes_through(session_factory, config):
    op_config = config.model_copy(update={"slack_operators": ["ops@example.com"]})
    assert op_config.iap_audience == ""
    client = _make_client(session_factory, op_config, slack=None)
    _seed_agent(session_factory, op_config)

    response = client.post(f"/api/agents/{AGENT_NAME}/slack/app")

    assert response.status_code == 503
    assert response.json() == {"error": "Slack is not configured"}


def test_operators_set_but_iap_off_connect_slack_passes_through(session_factory, config):
    op_config = config.model_copy(update={"slack_operators": ["ops@example.com"]})
    client = _make_client(session_factory, op_config, slack=None)
    _seed_agent(session_factory, op_config, slack_app_id="A0EXISTING")

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"channel": CHANNEL, "bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
    )

    assert response.status_code == 503
    assert response.json() == {"error": "Slack is not configured"}


def test_operators_set_but_iap_off_slack_visible_true(session_factory, config):
    op_config = config.model_copy(update={"slack_operators": ["ops@example.com"]})
    client = _make_client(session_factory, op_config)
    _seed_agent(session_factory, op_config)

    response = client.get(f"/api/agents/{AGENT_NAME}")

    assert response.status_code == 200
    assert response.json()["slack_visible"] is True
