"""Revoking a person's MCP access from the admin panel (issue #197): the route
cuts every family, records an audit event, and the verifier refuses the
person's older access tokens."""

from __future__ import annotations

import asyncio
import time

from kobo_api.admin_store import audit_events
from kobo_api.config import Config, MCPConfig
from kobo_api.mcp import refresh_tokens as rt
from kobo_api.mcp import server as mcp_server
from kobo_api.mcp.jwt import ACCESS_TTL_S, mint
from tests.test_api_org_status import (
    ASSERTION_HEADER,
    WHOAMI_AUDIENCE,
    _generate_jwk,
    _iap_client,
    _sign_whoami,
)

KEY = b"k" * 32


def _client(tmp_path):
    values = {
        "workdir": tmp_path / "work",
        "iap_audience": WHOAMI_AUDIENCE,
        "operators": ["ops@example.com"],
        "admin_panel": True,
        "admin_groups": [],
        "mcp": MCPConfig(
            oauth_client_id="cid",
            workspace_domain="example.com",
            issuer_base_url="https://room.example.com",
        ),
    }
    private_key, jwk = _generate_jwk()
    client = _iap_client(Config(**values), tmp_path, jwk=jwk)
    return client, {
        ASSERTION_HEADER: _sign_whoami(private_key, email="ops@example.com")
    }


def test_revoke_cuts_the_families_logs_the_action_and_the_verifier_refuses_old_tokens(
    tmp_path, monkeypatch
):
    client, headers = _client(tmp_path)
    factory = client.app.state.session_factory
    with factory() as session:
        rt.create_family(session, jti="j1", email="gone@example.com")
        session.commit()
    monkeypatch.setattr(
        "kobo_api.mcp.server.verify", lambda token, typ, aud: _claims(token)
    )
    monkeypatch.setattr("kobo_api.mcp.jwt.signing_key", lambda *a, **k: KEY)
    verifier = mcp_server._KoboTokenVerifier(client.app)
    old_token = mint(
        "access_token", "gone@example.com", "cid", ACCESS_TTL_S, key=KEY, scope="mcp"
    )
    mcp_server.forget_revocation_cache()
    assert asyncio.run(verifier.verify_token(old_token)) is not None

    time.sleep(
        1.1
    )  # the revocation instant must be after the token's iat (second precision)
    resp = client.post(
        "/api/admin/mcp/revocations",
        json={"email": "Gone@example.com", "reason": "left"},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json() == {"email": "gone@example.com", "refresh_tokens_revoked": 1}

    assert asyncio.run(verifier.verify_token(old_token)) is None
    listed = client.get("/api/admin/mcp/revocations", headers=headers).json()[
        "revocations"
    ]
    assert [r["email"] for r in listed] == ["gone@example.com"]
    with factory() as session:
        actions = [(e.action, e.object_id) for e in audit_events(session)]
    assert ("mcp.revoke", "gone@example.com") in actions


def test_revocations_are_for_admins_only(tmp_path):
    client, _ = _client(tmp_path)
    private_key, _jwk = _generate_jwk()
    stranger = {
        ASSERTION_HEADER: _sign_whoami(private_key, email="someone@example.com")
    }
    assert client.post(
        "/api/admin/mcp/revocations", json={"email": "x@example.com"}, headers=stranger
    ).status_code in (401, 403)


def _claims(token: str) -> dict:
    import jwt as pyjwt

    return pyjwt.decode(token, KEY, algorithms=["HS256"], audience="cid")
