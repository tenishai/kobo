"""Tests for the `slack_operators` gate helper (`_slack_operator_block`,
Task 5) and, historically, the two Slack-connect routes it used to guard.

Slack self-service (2026-08-17) retired both of those call sites: the
config-token app-creation route (`POST .../slack/app`) is gone outright,
and `POST .../slack/connect` dropped its gate call entirely (self-service
means anyone can walk the connect ceremony, not just a listed operator) —
along with the `GET /api/agents/{name}` detail's `"slack_admin_visible"`
field the gate used to drive. `_slack_operator_block` itself is left in
place, unused by any route today: `slack_operators` remains in the org
config "for the room's other uses" (the design spec's own words), so its
gate helper stays available rather than being deleted along with its two
former callers.

Two layers of coverage remain, same shape as test_iap.py:

- Unit-level: `_slack_operator_block` called directly against a minimal
  stand-in for `Request` (only `.state` is read) — no HTTP involved. Still
  fully valid: the function's behavior is unchanged, just uncalled by
  routes for now.
- Route-level: real `TestClient` requests proving connect's new,
  degated reality (non-operator identity still gets all the way to a 200),
  plus one regression proof that dropping the operator gate didn't also
  drop IAP: an anonymous request still 401s via the app-wide middleware,
  same idiom as test_iap.py's own matrix.

Existing slack route tests (test_api_slack.py) are untouched by this file —
they all use `Config()`'s default `slack_operators == []`, so the (now
nonexistent) gate never engaged for them even before this change.
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
from kobo_api.iap import ASSERTION_HEADER, IAP_ISSUER, UNAUTHORIZED_BODY
from kobo_api.main import _slack_operator_block, create_app
from kobo_api.models import Agent, AgentStatus, Base
from kobo_api.personas import seed_default_personas
from kobo_api.slack_state import encode_channels
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
REPO = "acme/agent-clio"

APP_ID = "A0EXISTING"
BOT_TOKEN = "xoxb-super-secret-bot-token"
APP_TOKEN = "xapp-super-secret-app-token"
CHANNEL = "C1234567"

OPERATOR_EMAIL = "Ops@Example.com"
NON_OPERATOR_EMAIL = "someone-else@example.com"

VERBATIM_403 = {"error": "Slack setup is operator-only on this room"}

# FakeGitHub.clone only writes bundle/agent.yaml when agent_yaml_transports
# is given; the one connect happy-path test in this file needs the file on
# disk for wire_slack to edit — same fixture/idiom as test_api_slack.py's
# own _CapturingGitHub (duplicated locally, not imported: this file's own
# "don't cross-import test fixtures" convention, see its docstring above).
_DEFAULT_AGENT_YAML_TRANSPORTS = [{"kind": "a2a", "host": "0.0.0.0", "port": 8080}]

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


def _make_client(
    session_factory,
    config,
    *,
    github=None,
    kubectl=None,
    gojira=None,
    slack=_SLACK_UNSET,
    iap_jwks_fetcher=None,
):
    storage = FakeStorage()
    seed_default_personas(storage, bucket=config.personas_bucket)
    return TestClient(
        create_app(
            session_factory,
            config=config,
            clients=Clients(
                github=github if github is not None else FakeGitHub(),
                gojira=gojira if gojira is not None else FakeGojira(activate_after=1),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=kubectl if kubectl is not None else FakeKubectl(),
                slack=FakeSlack() if slack is _SLACK_UNSET else slack,
                storage=storage,
            ),
            iap_jwks_fetcher=iap_jwks_fetcher,
        )
    )


def _onboarded_gojira(**kwargs):
    """A FakeGojira with one already-onboarded app (mirrors a READY agent),
    at the cadence connect_slack's wait_active expects. Duplicated locally
    from test_api_slack.py's own helper of the same name, same
    "don't cross-import test fixtures" convention this file follows
    throughout."""
    kwargs.setdefault("activate_after", 1)
    gojira = FakeGojira(**kwargs)
    app_id = gojira.create_app(
        name=AGENT_NAME, repo_full_name=REPO, cluster="example-cluster", namespace=AGENT_NAME
    )
    return gojira, app_id


def _seed_agent(
    session_factory,
    config,
    *,
    status=AgentStatus.READY,
    slack_app_id=None,
    gojira_app_id=None,
    slack_channels=None,
):
    session = session_factory()
    session.add(
        Agent(
            name=AGENT_NAME,
            persona=PERSONA,
            model_id=config.models[0].id,
            status=status,
            repo_full_name=REPO,
            slack_app_id=slack_app_id,
            gojira_app_id=gojira_app_id,
            slack_channels=encode_channels(slack_channels) if slack_channels is not None else None,
        )
    )
    session.commit()
    session.close()


# --- empty slack_operators: open, existing 503 flow unchanged ---------------


def test_empty_operators_connect_slack_still_503s_when_slack_unconfigured(
    session_factory, config
):
    config = config.model_copy(update={"slack_operators": []})
    client = _make_client(session_factory, config, slack=None)
    _seed_agent(session_factory, config, slack_app_id=APP_ID)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"app_id": APP_ID, "bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
    )

    assert response.status_code == 503
    assert response.json() == {"error": "Slack is not configured"}


# --- non-empty operators + IAP on + non-listed email: connect still 200 -----
#
# Slack self-service (2026-08-17) retired connect's operator gate outright:
# a non-operator identity now gets exactly the same 200 an operator would,
# proven end to end (secret delivered, transport wired, tokens still never
# echoed — the token-safety proof survives even though there's no more
# error path here to hang it on).


def test_non_operator_gets_200_from_connect_slack(session_factory, config):
    private_key, jwk = _generate_jwk()
    op_config = config.model_copy(
        update={"slack_operators": ["ops@example.com"], "iap_audience": AUDIENCE}
    )
    github = FakeGitHub(agent_yaml_transports=_DEFAULT_AGENT_YAML_TRANSPORTS)
    gojira, app_id = _onboarded_gojira()
    client = _make_client(
        session_factory, op_config, github=github, gojira=gojira,
        iap_jwks_fetcher=_fetcher(jwk),
    )
    _seed_agent(
        session_factory, op_config, gojira_app_id=app_id, slack_channels=[CHANNEL]
    )
    token = _sign(private_key, email=NON_OPERATOR_EMAIL)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"app_id": APP_ID, "bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
        headers={ASSERTION_HEADER: token},
    )

    assert response.status_code == 200
    assert BOT_TOKEN not in response.text
    assert APP_TOKEN not in response.text


def test_anonymous_gets_401_from_connect_slack_via_middleware(session_factory, config):
    # Regression proof: dropping connect's operator gate must not also drop
    # IAP. With no assertion header at all, the app-wide middleware still
    # 401s before the route ever runs — same idiom as test_iap.py's matrix.
    op_config = config.model_copy(update={"iap_audience": AUDIENCE})
    client = _make_client(session_factory, op_config)
    _seed_agent(session_factory, op_config, slack_channels=[CHANNEL])

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"app_id": APP_ID, "bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
    )

    assert response.status_code == 401
    assert response.json() == UNAUTHORIZED_BODY


# --- non-empty operators + IAP on + listed email: passes through to 503 -----


def test_listed_email_uppercase_token_lowercase_operator_list_passes_through(
    session_factory, config
):
    # Even though connect no longer gates on the operator list at all, this
    # 503-passthrough case-insensitivity proof still holds unchanged (there
    # was never anything gate-specific about it beyond the email match,
    # which `_slack_operator_block`'s own unit tests above already cover
    # from every case-combination angle).
    private_key, jwk = _generate_jwk()
    op_config = config.model_copy(
        update={"slack_operators": [OPERATOR_EMAIL.lower()], "iap_audience": AUDIENCE}
    )
    client = _make_client(session_factory, op_config, slack=None, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, op_config, slack_app_id=APP_ID)
    token = _sign(private_key, email=OPERATOR_EMAIL.upper())

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"app_id": APP_ID, "bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
        headers={ASSERTION_HEADER: token},
    )

    assert response.status_code == 503
    assert response.json() == {"error": "Slack is not configured"}


# --- non-empty operators + IAP OFF: dev parity, unaffected either way -------


def test_operators_set_but_iap_off_connect_slack_passes_through(session_factory, config):
    op_config = config.model_copy(update={"slack_operators": ["ops@example.com"]})
    client = _make_client(session_factory, op_config, slack=None)
    _seed_agent(session_factory, op_config, slack_app_id=APP_ID)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"app_id": APP_ID, "bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
    )

    assert response.status_code == 503
    assert response.json() == {"error": "Slack is not configured"}
