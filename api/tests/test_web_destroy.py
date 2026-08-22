"""Tests for the `operators` gate (Task 4: agent destruction) and the
`DELETE /api/agents/{name}` route: the two-layer coverage shape
test_web_slack_gate.py established for `slack_operators`/`_slack_operator_
block` —

- Unit-level: `_operator_block` called directly against a minimal stand-in
  for `Request` — no HTTP involved.
- Route-level: real `TestClient` requests, IAP enabled via a real ES256
  keypair/JWT (same idiom as test_iap.py/test_web_slack_gate.py) so the
  "email not listed" / "email listed, case-insensitive" / "IAP off" cases
  are proven through the actual middleware, not a mock.

Plus the route's own status-code matrix (403/404/409/202/202-resume), the
`"destroy_visible"` detail field, the launch-failure guard (mirrors
`retry_agent`'s), and a couple of regression checks that existing mutation
routes already reject a destroying/destroy_failed agent for free (via the
same READY-only gates they already had) — no new gating code needed there.
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
from kobo_api.main import _operator_block, create_app
from kobo_api.models import Agent, AgentStatus, AgentStep, Base, StepStatus
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
OWNER = "acme-corp"
REPO = f"{OWNER}/agent-{AGENT_NAME}"

OPERATOR_EMAIL = "Ops@Example.com"
NON_OPERATOR_EMAIL = "someone-else@example.com"

VERBATIM_403 = {"error": "Destroying agents is operator-only on this room"}
IN_FLIGHT_409 = {"error": "the agent is still being created, retry or wait"}

AUDIENCE = "test-audience"
KID = "test-key-1"

JOIN_DEADLINE_S = 5
POLL_INTERVAL_S = 0.02


# --- Unit-level: _operator_block called directly ------------------------------


def _req(email: str | None, config: Config):
    """A minimal stand-in for `Request` -- `_operator_block` reads `request.
    state.iap_email` directly, but now also reaches `_is_admin(request)`
    (operator-status-by-group ruling), which reads `request.app.state.
    config`/`.clients` -- so this stand-in carries the same `config` the
    test already passes as `_operator_block`'s own second argument, same
    idiom test_web_ownership_gate.py's own `_req` uses. `clients` is left
    unset (not even `None`-valued): `_is_admin` reaches it through `getattr`
    with a default, so a resolver-free room needs nothing here at all."""
    state = SimpleNamespace()
    if email is not None:
        state.iap_email = email
    return SimpleNamespace(state=state, app=SimpleNamespace(state=SimpleNamespace(config=config)))


def test_helper_empty_operators_is_none_even_with_iap_on_and_no_email():
    config = Config(operators=[], iap_audience=AUDIENCE)

    assert _operator_block(_req(None, config), config) is None


def test_helper_iap_off_is_none_even_with_operators_set_and_no_email():
    config = Config(operators=["ops@example.com"], iap_audience="")

    assert _operator_block(_req(None, config), config) is None


def test_helper_non_listed_email_returns_403_body_verbatim():
    config = Config(operators=["ops@example.com"], iap_audience=AUDIENCE)

    result = _operator_block(_req(NON_OPERATOR_EMAIL, config), config)

    assert result is not None
    assert result.status_code == 403
    assert json.loads(result.body) == VERBATIM_403


def test_helper_no_email_at_all_returns_403():
    config = Config(operators=["ops@example.com"], iap_audience=AUDIENCE)

    result = _operator_block(_req(None, config), config)

    assert result is not None
    assert result.status_code == 403


def test_helper_listed_email_exact_case_is_none():
    config = Config(operators=["ops@example.com"], iap_audience=AUDIENCE)

    assert _operator_block(_req("ops@example.com", config), config) is None


def test_helper_listed_email_operator_list_uppercase_request_lowercase_is_none():
    config = Config(operators=["OPS@EXAMPLE.COM"], iap_audience=AUDIENCE)

    assert _operator_block(_req("ops@example.com", config), config) is None


def test_helper_listed_email_operator_list_lowercase_request_uppercase_is_none():
    config = Config(operators=["ops@example.com"], iap_audience=AUDIENCE)

    assert _operator_block(_req("OPS@EXAMPLE.COM", config), config) is None


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


def _make_client(
    session_factory,
    config,
    *,
    github=None,
    gojira=None,
    slack=None,
    iap_jwks_fetcher=None,
):
    storage = FakeStorage()
    seed_default_personas(storage, bucket=config.personas_bucket)
    return TestClient(
        create_app(
            session_factory,
            config=config,
            clients=Clients(
                github=github if github is not None else FakeGitHub(existing_repos={REPO}),
                gojira=gojira if gojira is not None else FakeGojira(),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=FakeKubectl(),
                slack=slack if slack is not None else FakeSlack(),
                storage=storage,
            ),
            iap_jwks_fetcher=iap_jwks_fetcher,
        )
    )


def _seed_agent(
    session_factory,
    config,
    *,
    name: str = AGENT_NAME,
    status=AgentStatus.READY,
    gojira_app_id: int | None = 7,
    slack_app_id: str | None = "A0FAKE",
    repo_full_name: str | None = REPO,
    error: str | None = None,
):
    session = session_factory()
    session.add(
        Agent(
            name=name,
            persona=PERSONA,
            model_id=config.models[0].id,
            status=status,
            gojira_app_id=gojira_app_id,
            slack_app_id=slack_app_id,
            repo_full_name=repo_full_name,
            error=error,
        )
    )
    session.commit()
    session.close()


def _poll_until_gone(client, name, deadline_s=JOIN_DEADLINE_S, headers=None):
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        response = client.get(f"/api/agents/{name}", headers=headers)
        if response.status_code == 404:
            return
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"agent {name!r} was never forgotten within {deadline_s}s")


# --- empty operators: open, gate never engages --------------------------------


def test_empty_operators_destroy_returns_202_and_agent_is_eventually_forgotten(
    session_factory, config
):
    config = config.model_copy(update={"operators": []})
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)

    response = client.delete(f"/api/agents/{AGENT_NAME}")

    assert response.status_code == 202
    assert response.json() == {"status": "destroying"}
    _poll_until_gone(client, AGENT_NAME)


def test_empty_operators_destroy_visible_true(session_factory, config):
    config = config.model_copy(update={"operators": []})
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}")

    assert response.status_code == 200
    assert response.json()["destroy_visible"] is True


def test_destroy_unknown_agent_returns_404_when_gate_open(session_factory, config):
    config = config.model_copy(update={"operators": []})
    client = _make_client(session_factory, config)

    response = client.delete("/api/agents/does-not-exist")

    assert response.status_code == 404
    assert "error" in response.json()


@pytest.mark.parametrize("status", [AgentStatus.PENDING, AgentStatus.PROVISIONING])
def test_destroy_provisioning_in_flight_returns_409(session_factory, config, status):
    config = config.model_copy(update={"operators": []})
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, status=status, gojira_app_id=None, slack_app_id=None, repo_full_name=None)

    response = client.delete(f"/api/agents/{AGENT_NAME}")

    assert response.status_code == 409
    assert response.json() == IN_FLIGHT_409


def test_destroy_failed_agent_is_still_destroyable(session_factory, config):
    """A FAILED (never fully provisioned) agent is not blocked — nothing
    about destruction requires a READY agent, only that creation isn't
    still in flight."""
    config = config.model_copy(update={"operators": []})
    client = _make_client(session_factory, config)
    _seed_agent(
        session_factory, config, status=AgentStatus.FAILED,
        gojira_app_id=None, slack_app_id=None, repo_full_name=None,
    )

    response = client.delete(f"/api/agents/{AGENT_NAME}")

    assert response.status_code == 202
    _poll_until_gone(client, AGENT_NAME)


def test_destroy_idempotent_redelete_while_destroy_failed_resumes_to_completion(
    session_factory, config
):
    """Seed the agent already mid-ledger (platform done, access done, slack
    failed) as a prior partial run would leave it, then DELETE again
    ("Destroy again"): 202, and this time slack (now working) completes, and
    the run continues through to full removal.

    Also the destroy-steps payload-label case: a GET taken right after
    seeding (before the redelete) proves the "Model access" label rides the
    payload between the platform and Slack rows, in the ledger's own order —
    not just that DESTROY_STEP_LABELS has the right entry, but that it
    actually reaches the wire for a real destroy-access row.
    """
    config = config.model_copy(update={"operators": []})
    client = _make_client(session_factory, config)
    _seed_agent(
        session_factory, config, status=AgentStatus.DESTROY_FAILED,
        error="slack app deletion failed: slack outage",
    )
    session = session_factory()
    agent = session.query(Agent).filter_by(name=AGENT_NAME).first()
    session.add(AgentStep(agent_id=agent.id, name="destroy-platform", status=StepStatus.DONE))
    session.add(AgentStep(agent_id=agent.id, name="destroy-access", status=StepStatus.DONE))
    session.add(
        AgentStep(
            agent_id=agent.id, name="destroy-slack", status=StepStatus.FAILED,
            error="slack app deletion failed: slack outage",
        )
    )
    session.commit()
    session.close()

    before = client.get(f"/api/agents/{AGENT_NAME}")
    names = [s["name"] for s in before.json()["steps"]]
    labels = {s["name"]: s["label"] for s in before.json()["steps"]}
    assert names == ["destroy-platform", "destroy-access", "destroy-slack"]
    assert labels["destroy-platform"] == "Platform teardown"
    assert labels["destroy-access"] == "Model access"
    assert labels["destroy-slack"] == "Slack app"

    response = client.delete(f"/api/agents/{AGENT_NAME}")

    assert response.status_code == 202
    assert response.json() == {"status": "destroying"}
    _poll_until_gone(client, AGENT_NAME)


# --- non-empty operators + IAP on + non-listed email: 403 before anything ----


def test_non_operator_gets_403_before_agent_lookup(session_factory, config):
    """The gate runs before the agent 404 check too — a non-operator gets
    the same 403 whether or not the named agent even exists."""
    private_key, jwk = _generate_jwk()
    op_config = config.model_copy(update={"operators": ["ops@example.com"], "iap_audience": AUDIENCE})
    client = _make_client(session_factory, op_config, iap_jwks_fetcher=_fetcher(jwk))
    token = _sign(private_key, email=NON_OPERATOR_EMAIL)

    response = client.delete("/api/agents/no-such-agent", headers={ASSERTION_HEADER: token})

    assert response.status_code == 403
    assert response.json() == VERBATIM_403


def test_non_operator_gets_403_even_when_agent_ready(session_factory, config):
    private_key, jwk = _generate_jwk()
    op_config = config.model_copy(update={"operators": ["ops@example.com"], "iap_audience": AUDIENCE})
    client = _make_client(session_factory, op_config, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, op_config)
    token = _sign(private_key, email=NON_OPERATOR_EMAIL)

    response = client.delete(f"/api/agents/{AGENT_NAME}", headers={ASSERTION_HEADER: token})

    assert response.status_code == 403
    assert response.json() == VERBATIM_403


def test_non_operator_destroy_visible_false(session_factory, config):
    private_key, jwk = _generate_jwk()
    op_config = config.model_copy(update={"operators": ["ops@example.com"], "iap_audience": AUDIENCE})
    client = _make_client(session_factory, op_config, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, op_config)
    token = _sign(private_key, email=NON_OPERATOR_EMAIL)

    response = client.get(f"/api/agents/{AGENT_NAME}", headers={ASSERTION_HEADER: token})

    assert response.status_code == 200
    assert response.json()["destroy_visible"] is False


# --- non-empty operators + IAP on + listed email: passes through ------------


def test_listed_email_destroy_passes_through_to_202(session_factory, config):
    private_key, jwk = _generate_jwk()
    op_config = config.model_copy(update={"operators": [OPERATOR_EMAIL], "iap_audience": AUDIENCE})
    client = _make_client(session_factory, op_config, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, op_config)
    # Case-insensitive both directions.
    token = _sign(private_key, email=OPERATOR_EMAIL.lower())

    response = client.delete(f"/api/agents/{AGENT_NAME}", headers={ASSERTION_HEADER: token})

    assert response.status_code == 202
    _poll_until_gone(client, AGENT_NAME, headers={ASSERTION_HEADER: token})


def test_listed_email_destroy_visible_true(session_factory, config):
    private_key, jwk = _generate_jwk()
    op_config = config.model_copy(update={"operators": [OPERATOR_EMAIL], "iap_audience": AUDIENCE})
    client = _make_client(session_factory, op_config, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, op_config)
    token = _sign(private_key, email=OPERATOR_EMAIL.lower())

    response = client.get(f"/api/agents/{AGENT_NAME}", headers={ASSERTION_HEADER: token})

    assert response.status_code == 200
    assert response.json()["destroy_visible"] is True


# --- non-empty operators + IAP OFF: dev parity, treated as operator ---------


def test_operators_set_but_iap_off_destroy_passes_through(session_factory, config):
    op_config = config.model_copy(update={"operators": ["ops@example.com"]})
    assert op_config.iap_audience == ""
    client = _make_client(session_factory, op_config)
    _seed_agent(session_factory, op_config)

    response = client.delete(f"/api/agents/{AGENT_NAME}")

    assert response.status_code == 202
    _poll_until_gone(client, AGENT_NAME)


def test_operators_set_but_iap_off_destroy_visible_true(session_factory, config):
    op_config = config.model_copy(update={"operators": ["ops@example.com"]})
    client = _make_client(session_factory, op_config)
    _seed_agent(session_factory, op_config)

    response = client.get(f"/api/agents/{AGENT_NAME}")

    assert response.status_code == 200
    assert response.json()["destroy_visible"] is True


# --- launch-failure guard (mirrors retry_agent's) ------------------------------


def test_destroy_launch_failure_returns_503_and_a_later_destroy_still_succeeds(
    session_factory, config, monkeypatch
):
    import kobo_api.main as main_module

    config = config.model_copy(update={"operators": []})
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)

    def _raising_run_destroy(session_factory, agent_id, clients, config):
        raise RuntimeError("no more threads")

    monkeypatch.setattr(main_module, "run_destroy", _raising_run_destroy)

    first = client.delete(f"/api/agents/{AGENT_NAME}")

    assert first.status_code == 503
    assert first.json() == {"error": "failed to start destruction: no more threads"}

    session = session_factory()
    agent = session.query(Agent).filter_by(name=AGENT_NAME).first()
    assert agent.status == AgentStatus.DESTROY_FAILED
    assert agent.error == "failed to start destruction: no more threads"
    session.close()

    monkeypatch.undo()

    second = client.delete(f"/api/agents/{AGENT_NAME}")

    assert second.status_code == 202
    _poll_until_gone(client, AGENT_NAME)


# --- regression: existing mutation routes already reject destroying/-------
# destroy_failed agents for free, via the READY-only gates they already had


def test_chat_rejects_destroying_agent_409(session_factory, config):
    config = config.model_copy(update={"operators": []})
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, status=AgentStatus.DESTROYING)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/chat", json={"messages": [{"role": "user", "text": "hi"}]}
    )

    assert response.status_code == 409
    assert response.json() == {"error": "agent is not ready to chat"}


def test_knowledge_save_rejects_destroy_failed_agent_409(session_factory, config):
    config = config.model_copy(update={"operators": []})
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, status=AgentStatus.DESTROY_FAILED)

    response = client.put(
        f"/api/agents/{AGENT_NAME}/knowledge/notes.md", json={"content": "hello"}
    )

    assert response.status_code == 409
    assert "error" in response.json()


def test_retry_rejects_destroying_agent_409(session_factory, config):
    config = config.model_copy(update={"operators": []})
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, status=AgentStatus.DESTROYING)

    response = client.post(f"/api/agents/{AGENT_NAME}/retry")

    assert response.status_code == 409


# --- per-agent in-flight guard (review Finding 1): the duplicate-launch fix ---


def test_redelete_while_destroy_in_flight_returns_202_without_double_launch(
    session_factory, config
):
    """A re-DELETE against an agent already DESTROYING must keep returning
    202 (the idempotent-redelete contract, unchanged) but must NOT launch a
    second concurrent run_destroy thread. Simulated deterministically by
    registering the agent's in-flight slot directly in kobo_api.destroy's
    module-level registry before issuing the DELETE — no mocking of the
    route or of run_destroy itself, just the same registry `run_destroy`
    checks on every call."""
    from kobo_api import destroy as destroy_module

    config = config.model_copy(update={"operators": []})
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, status=AgentStatus.DESTROYING)
    session = session_factory()
    agent = session.query(Agent).filter_by(name=AGENT_NAME).first()
    agent_id = agent.id
    session.close()

    destroy_module._destroying_agent_ids.add(agent_id)
    try:
        response = client.delete(f"/api/agents/{AGENT_NAME}")
    finally:
        destroy_module._destroying_agent_ids.discard(agent_id)

    assert response.status_code == 202
    assert response.json() == {"status": "destroying"}

    # No duplicate thread launched — the agent is left exactly as it was
    # (still DESTROYING; no teardown work happened from this second call,
    # since run_destroy's own launch was a no-op skip).
    session = session_factory()
    agent = session.query(Agent).filter_by(name=AGENT_NAME).first()
    assert agent is not None
    assert agent.status == AgentStatus.DESTROYING
    session.close()
