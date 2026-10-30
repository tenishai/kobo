"""Agent PR broker (2026-08-27 spec): the two-wall token model's room half.

Route tests drive the same `create_app` client every other route test uses;
the GitHub mint call and the cabal JWT verification are seamed out
(`mint_scoped_token`'s http_client / monkeypatched `verify_agent_token`) so
nothing here touches a network. Dev parity throughout: no iap_audience means
the operator gates answer None, the same as every other gated route's tests.
"""

import httpx
import pytest
from fastapi.testclient import TestClient

from kobo_api.clients import ClientError
from kobo_api.config import Config
from kobo_api.main import create_app
from kobo_api.models import Agent, AgentStatus, RepoAccess
from kobo_api.pr_broker import PR_PERMISSIONS, mint_scoped_token


@pytest.fixture
def broker_config(tmp_path):
    key = tmp_path / "pr-app.pem"
    key.write_text("not-a-real-key")
    return Config(
        workdir=tmp_path / "work",
        pr_app_id="4741722",
        pr_app_installation_id="157070479",
        pr_app_private_key_path=key,
        cabal_url="http://cabal.test",
    )


@pytest.fixture
def broker_client(session_factory, broker_config):
    return TestClient(create_app(session_factory, config=broker_config))


def _agent(session_factory, name="ward"):
    session = session_factory()
    agent = Agent(
        name=name, persona="helpful-generalist", model_id="m", status=AgentStatus.READY
    )
    session.add(agent)
    session.commit()
    session.close()
    return name


# --- feature gate -------------------------------------------------------------


def test_routes_answer_503_when_broker_unconfigured(client):
    assert client.post(
        "/api/agents/x/repo-access", json={"repositories": ["acme/a"]}
    ).status_code == 503
    assert client.get("/api/repo-access").status_code == 503
    assert client.post(
        "/api/repo-access/decide", json={"ids": [1], "decision": "approved"}
    ).status_code == 503
    assert client.post("/broker/agents/x/github-token").status_code == 503


# --- request + batch decide ----------------------------------------------------


def test_request_batch_then_batch_approve(broker_client, session_factory):
    name = _agent(session_factory)
    r = broker_client.post(
        f"/api/agents/{name}/repo-access",
        json={"repositories": ["acme/services-clip", "acme/global-infra-tools"]},
    )
    assert r.status_code == 200
    rows = r.json()["requested"]
    assert [x["status"] for x in rows] == ["requested", "requested"]

    r = broker_client.post(
        "/api/repo-access/decide",
        json={"ids": [x["id"] for x in rows], "decision": "approved"},
    )
    assert r.status_code == 200
    assert [x["status"] for x in r.json()["decided"]] == ["approved", "approved"]

    listed = broker_client.get("/api/repo-access", params={"status": "approved"}).json()
    assert len(listed["results"]) == 2


def test_rerequest_never_demotes_an_approved_grant(broker_client, session_factory):
    name = _agent(session_factory)
    rows = broker_client.post(
        f"/api/agents/{name}/repo-access", json={"repositories": ["acme/a"]}
    ).json()["requested"]
    broker_client.post(
        "/api/repo-access/decide", json={"ids": [rows[0]["id"]], "decision": "approved"}
    )
    again = broker_client.post(
        f"/api/agents/{name}/repo-access", json={"repositories": ["acme/a"]}
    ).json()["requested"]
    assert again[0]["status"] == "approved"


def test_revoked_row_comes_back_as_requested_not_duplicate(
    broker_client, session_factory
):
    name = _agent(session_factory)
    rows = broker_client.post(
        f"/api/agents/{name}/repo-access", json={"repositories": ["acme/a"]}
    ).json()["requested"]
    broker_client.post(
        "/api/repo-access/decide", json={"ids": [rows[0]["id"]], "decision": "revoked"}
    )
    again = broker_client.post(
        f"/api/agents/{name}/repo-access", json={"repositories": ["acme/a"]}
    ).json()["requested"]
    assert again[0]["status"] == "requested"
    assert again[0]["id"] == rows[0]["id"]  # same row, no duplicate

    session = session_factory()
    assert session.query(RepoAccess).count() == 1
    session.close()


def test_repo_shape_is_validated(broker_client, session_factory):
    name = _agent(session_factory)
    r = broker_client.post(
        f"/api/agents/{name}/repo-access", json={"repositories": ["not-a-repo"]}
    )
    assert r.status_code == 422


def test_request_against_unknown_agent_404s(broker_client):
    r = broker_client.post(
        "/api/agents/nobody/repo-access", json={"repositories": ["acme/a"]}
    )
    assert r.status_code == 404


# --- the broker route ----------------------------------------------------------


def test_token_route_401s_without_a_bearer(broker_client, session_factory):
    name = _agent(session_factory)
    assert broker_client.post(f"/broker/agents/{name}/github-token").status_code == 401


def test_token_route_404s_with_zero_approvals(
    broker_client, session_factory, monkeypatch
):
    """Still a 404, but after the token verifies the message says why: the
    caller is proven to be the agent, so "no such agent" would be false
    (issue #11)."""
    import kobo_api.main as main_mod

    name = _agent(session_factory)
    monkeypatch.setattr(main_mod, "verify_agent_token", lambda *a, **k: None)
    r = broker_client.post(
        f"/broker/agents/{name}/github-token", headers={"Authorization": "Bearer x"}
    )
    assert r.status_code == 404
    error = r.json()["error"]
    assert "no such agent" not in error
    assert "none has been requested" in error
    assert f"POST /api/agents/{name}/repo-access" in error


def test_token_route_404_names_requests_still_waiting_for_approval(
    broker_client, session_factory, monkeypatch
):
    import kobo_api.main as main_mod

    name = _agent(session_factory)
    broker_client.post(
        f"/api/agents/{name}/repo-access", json={"repositories": ["acme/b", "acme/a"]}
    )
    monkeypatch.setattr(main_mod, "verify_agent_token", lambda *a, **k: None)
    r = broker_client.post(
        f"/broker/agents/{name}/github-token", headers={"Authorization": "Bearer x"}
    )
    assert r.status_code == 404
    error = r.json()["error"]
    assert "acme/a, acme/b requested, waiting for an operator to approve" in error
    assert "POST /api/repo-access/decide" in error


def test_token_route_404_says_when_access_was_revoked(
    broker_client, session_factory, monkeypatch
):
    import kobo_api.main as main_mod

    name = _agent(session_factory)
    rows = broker_client.post(
        f"/api/agents/{name}/repo-access", json={"repositories": ["acme/a"]}
    ).json()["requested"]
    broker_client.post(
        "/api/repo-access/decide", json={"ids": [rows[0]["id"]], "decision": "revoked"}
    )
    monkeypatch.setattr(main_mod, "verify_agent_token", lambda *a, **k: None)
    r = broker_client.post(
        f"/broker/agents/{name}/github-token", headers={"Authorization": "Bearer x"}
    )
    assert r.status_code == 404
    assert "acme/a revoked" in r.json()["error"]


def test_token_route_mints_scoped_to_approved_rows_only(
    broker_client, session_factory, monkeypatch
):
    import kobo_api.main as main_mod

    name = _agent(session_factory)
    rows = broker_client.post(
        f"/api/agents/{name}/repo-access",
        json={"repositories": ["acme/a", "acme/b"]},
    ).json()["requested"]
    # Approve only the first; the second stays requested.
    broker_client.post(
        "/api/repo-access/decide", json={"ids": [rows[0]["id"]], "decision": "approved"}
    )

    monkeypatch.setattr(main_mod, "verify_agent_token", lambda *a, **k: None)
    calls = {}

    def fake_mint(**kwargs):
        calls.update(kwargs)
        return {"token": "ghs_x", "expires_at": "later", "repositories": kwargs["repositories"]}

    monkeypatch.setattr(main_mod, "mint_scoped_token", fake_mint)
    r = broker_client.post(
        f"/broker/agents/{name}/github-token", headers={"Authorization": "Bearer x"}
    )
    assert r.status_code == 200
    assert calls["repositories"] == ["acme/a"]  # wall 2: approved rows only
    assert r.json()["token"] == "ghs_x"


def test_token_route_401s_when_verification_fails(
    broker_client, session_factory, monkeypatch
):
    import kobo_api.main as main_mod
    from kobo_api.pr_broker import BrokerAuthError

    name = _agent(session_factory)

    def refuse(*a, **k):
        raise BrokerAuthError("no")

    monkeypatch.setattr(main_mod, "verify_agent_token", refuse)
    r = broker_client.post(
        f"/broker/agents/{name}/github-token", headers={"Authorization": "Bearer x"}
    )
    assert r.status_code == 401


# --- mint_scoped_token unit ----------------------------------------------------


class _FakeHttp:
    """Records the mint POST and answers like GitHub's 201."""

    def __init__(self, status=201):
        self.status = status
        self.sent = None

    def post(self, url, headers=None, json=None):
        self.sent = {"url": url, "json": json}
        request = httpx.Request("POST", url)
        return httpx.Response(
            self.status,
            request=request,
            json={"token": "ghs_scoped", "expires_at": "2026-08-27T23:00:00Z"},
        )


def _rsa_key_pem(tmp_path):
    # PyJWT needs a real RSA key to sign the App JWT; generate a throwaway.
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    p = tmp_path / "k.pem"
    p.write_bytes(pem)
    return p


def test_mint_pins_repositories_and_pr_permissions(tmp_path):
    fake = _FakeHttp()
    out = mint_scoped_token(
        app_id="1",
        installation_id="2",
        private_key_path=_rsa_key_pem(tmp_path),
        repositories=["acme/services-clip", "acme/sero"],
        http_client=fake,
        now=lambda: 1_000_000,
    )
    assert fake.sent["json"]["repositories"] == ["services-clip", "sero"]  # bare names
    assert fake.sent["json"]["permissions"] == PR_PERMISSIONS
    assert out["token"] == "ghs_scoped"
    assert out["repositories"] == ["acme/services-clip", "acme/sero"]


def test_mint_refuses_an_empty_scope(tmp_path):
    with pytest.raises(ClientError):
        mint_scoped_token(
            app_id="1",
            installation_id="2",
            private_key_path=_rsa_key_pem(tmp_path),
            repositories=[],
        )


def test_mint_raises_clienterror_on_github_refusal(tmp_path):
    with pytest.raises(ClientError):
        mint_scoped_token(
            app_id="1",
            installation_id="2",
            private_key_path=_rsa_key_pem(tmp_path),
            repositories=["acme/a"],
            http_client=_FakeHttp(status=422),
            now=lambda: 1_000_000,
        )



def test_destroy_forget_deletes_repo_access_rows(broker_client, session_factory, broker_config):
    """A future agent reusing a destroyed agent's name must inherit ZERO
    grants -- destroy-forget clears repo_access by name."""
    from kobo_api.clients import Clients
    from kobo_api.destroy import DestroyCtx, _destroy_forget

    name = _agent(session_factory)
    rows = broker_client.post(
        f"/api/agents/{name}/repo-access", json={"repositories": ["acme/a"]}
    ).json()["requested"]
    broker_client.post(
        "/api/repo-access/decide", json={"ids": [rows[0]["id"]], "decision": "approved"}
    )

    session = session_factory()
    agent = session.query(Agent).filter_by(name=name).one()
    ctx = DestroyCtx(
        agent=agent,
        session=session,
        config=broker_config,
        clients=Clients(github=None, gojira=None, cloud=None, agent=None, kubectl=None),
    )
    _destroy_forget(ctx)
    assert session.query(RepoAccess).count() == 0
    session.close()


# --- issue #199: a re-wired agent's older pass is refused -----------------------


def test_verify_refuses_a_pass_issued_before_the_last_mint(monkeypatch):
    from datetime import UTC, datetime

    from kobo_api import pr_broker

    class _Jwks:
        def get_signing_key_from_jwt(self, bearer):
            class _K:
                key = "k"

            return _K()

    monkeypatch.setattr(pr_broker, "_jwks_client", lambda url: _Jwks())
    monkeypatch.setattr(pr_broker.pyjwt, "decode", lambda *a, **k: {"sub": "agent:ward", "iat": 1_000})

    assert pr_broker.verify_agent_token("http://cabal", "t", "ward")["iat"] == 1_000
    assert pr_broker.verify_agent_token(
        "http://cabal", "t", "ward", not_before=datetime.fromtimestamp(900, UTC)
    )
    with pytest.raises(pr_broker.BrokerAuthError):
        pr_broker.verify_agent_token(
            "http://cabal", "t", "ward", not_before=datetime.fromtimestamp(1_100, UTC)
        )
    # Naive timestamps (the DateTime column's shape) are read as UTC.
    with pytest.raises(pr_broker.BrokerAuthError):
        pr_broker.verify_agent_token(
            "http://cabal", "t", "ward", not_before=datetime.fromtimestamp(1_100, UTC).replace(tzinfo=None)
        )


def test_jwks_client_is_built_once_per_switchboard(monkeypatch):
    from kobo_api import pr_broker

    built = []
    monkeypatch.setattr(pr_broker, "PyJWKClient", lambda url, **kw: built.append(url) or object())
    pr_broker._jwks_clients.clear()
    a = pr_broker._jwks_client("http://cabal")
    b = pr_broker._jwks_client("http://cabal/")
    assert a is b and built == ["http://cabal/.well-known/jwks.json"]
    pr_broker._jwks_clients.clear()


def test_token_route_hands_the_agents_mint_instant_to_the_verifier(
    broker_client, session_factory, monkeypatch
):
    from datetime import UTC, datetime

    import kobo_api.main as main_mod

    name = _agent(session_factory)
    minted = datetime(2026, 10, 24, 12, 0, tzinfo=UTC).replace(tzinfo=None)
    session = session_factory()
    session.query(Agent).filter(Agent.name == name).one().pass_minted_at = minted
    session.commit()
    session.close()

    seen = {}

    def record(cabal_url, bearer, agent_name, *, not_before=None):
        seen["not_before"] = not_before
        return {}

    monkeypatch.setattr(main_mod, "verify_agent_token", record)
    broker_client.post(f"/broker/agents/{name}/github-token", headers={"Authorization": "Bearer x"})
    assert seen["not_before"] == minted
