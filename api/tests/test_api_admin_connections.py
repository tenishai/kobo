"""Connections (#173 step 3): a lane is a grant row, applied live through the
gateway's admin plane with the room's own token and written to the grants
file as a pull request; every agent is a row even with no lane."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.clients import ClientError, Clients
from kobo_api.config import Config
from kobo_api.liminal_admin import GatewayGrant, RoomAdminToken
from kobo_api.main import create_app
from kobo_api.models import Agent, AgentStatus, Base
from tests.fakes import (
    FakeAgent,
    FakeCabal,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
    FakeSero,
)
from tests.test_api_org_status import (
    ASSERTION_HEADER,
    WHOAMI_AUDIENCE,
    _fetcher,
    _generate_jwk,
    _sign_whoami,
)

GATEWAY_REPO = "example-org/gateway-config"
GRANTS = '[[grant]]\nissuer = "cabal-core"\npersona = "janus"\nscopes = ["github:read"]\nallowed_handlers = ["github", "scratchpad"]\n'


class FakeLiminalAdmin:
    def __init__(self, grants: list[GatewayGrant] | None = None, *, fail: str | None = None):
        self.grants = {(g.issuer, g.persona): g for g in (grants or [])}
        self.writes: list[GatewayGrant] = []
        self.revokes: list[tuple[str, str]] = []
        self._fail = fail

    def list_grants(self) -> list[GatewayGrant]:
        if self._fail:
            raise ClientError(self._fail)
        return list(self.grants.values())

    def grant(self, grant: GatewayGrant) -> None:
        if self._fail:
            raise ClientError(self._fail)
        self.writes.append(grant)
        self.grants[(grant.issuer, grant.persona)] = grant

    def revoke(self, issuer: str, persona: str) -> None:
        self.revokes.append((issuer, persona))
        self.grants.pop((issuer, persona), None)


def _client(tmp_path, fake_storage, *, github: FakeGitHub, admin, config_overrides=None):
    engine = create_engine(f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    values = {
        "workdir": tmp_path / "work", "iap_audience": WHOAMI_AUDIENCE, "operators": ["ops@example.com"],
        "admin_panel": True, "sero_url": "http://sero.internal", "gateway_config_repo": GATEWAY_REPO,
        "gateway_namespace": "liminal", "liminal_admin_url": "http://liminal-admin.liminal.svc",
    }
    values.update(config_overrides or {})
    private_key, jwk = _generate_jwk()
    clients = Clients(github=github, gojira=FakeGojira(), cloud=FakeCloud(), agent=FakeAgent(),
                      kubectl=FakeKubectl(), storage=fake_storage, sero=FakeSero(), cabal=FakeCabal())
    clients.liminal_admin = admin
    app = create_app(session_factory, config=Config(**values), clients=clients, iap_jwks_fetcher=_fetcher(jwk))
    with session_factory() as s:
        for name, group in (("janus", "platform"), ("sage", "data")):
            s.add(Agent(name=name, persona="helpful-generalist", model_id="vertex_ai/claude-sonnet-4-5", skills=[],
                        status=AgentStatus.READY, repo_full_name=f"example-org/agent-{name}", group_name=group))
        s.commit()
    return TestClient(app), {ASSERTION_HEADER: _sign_whoami(private_key, email="ops@example.com")}


def _connect_datadog(client, headers, base_access="read"):
    r = client.post("/api/admin/integrations", json={"vendor": "datadog", "name": "Datadog", "base_access": base_access,
                    "credential": "dd-token", "address": "https://mcp.datadoghq.com", "trust": "untrusted"}, headers=headers)
    assert r.status_code == 201, r.text


def test_every_agent_is_a_row_and_a_lane_applies_live_and_as_a_pull_request(tmp_path, fake_storage):
    github = FakeGitHub(existing_repos={GATEWAY_REPO}, extra_clone_files={"grants.toml": GRANTS, "targets.toml": ""}, track_pushed_content=True)
    admin = FakeLiminalAdmin([GatewayGrant("cabal-core", "janus", ("github:read",), ("github", "scratchpad"))])
    client, headers = _client(tmp_path, fake_storage, github=github, admin=admin)
    _connect_datadog(client, headers)

    view = client.get("/api/admin/connections", headers=headers).json()
    assert [a["agent"] for a in view["agents"]] == ["janus", "sage"]
    assert view["agents"][0]["lanes"] == [] and view["live"] is True

    r = client.put("/api/admin/connections/janus/datadog", json={"access": "read"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "applied" and body["outcome"]["live"] == "live at the gateway"
    # Live: one write for the persona, merged with what the gateway held.
    [written] = admin.writes
    assert written.persona == "janus"
    assert written.scopes == ("datadog:read", "github:read")
    assert written.allowed_handlers == ("datadog", "github", "scratchpad")
    # Durable: the grants file's janus block is rewritten in a pull request.
    grants = github.pushed_file("grants.toml")
    assert grants.count('persona = "janus"') == 1 and '"datadog:read"' in grants
    assert "pull request open on the gateway" in body["outcome"]["file"]


def test_a_write_lane_is_never_wider_than_the_integration(tmp_path, fake_storage):
    github = FakeGitHub(existing_repos={GATEWAY_REPO}, extra_clone_files={"grants.toml": ""})
    client, headers = _client(tmp_path, fake_storage, github=github, admin=FakeLiminalAdmin())
    _connect_datadog(client, headers, base_access="read")

    r = client.put("/api/admin/connections/sage/datadog", json={"access": "write"}, headers=headers)

    assert r.status_code == 409 and "read-only" in r.json()["error"]


def test_revoke_removes_the_lane_live_and_from_the_file(tmp_path, fake_storage):
    github = FakeGitHub(existing_repos={GATEWAY_REPO}, extra_clone_files={"grants.toml": ""}, track_pushed_content=True)
    admin = FakeLiminalAdmin()
    client, headers = _client(tmp_path, fake_storage, github=github, admin=admin)
    _connect_datadog(client, headers)
    client.put("/api/admin/connections/sage/datadog", json={"access": "read"}, headers=headers)

    r = client.delete("/api/admin/connections/sage/datadog", headers=headers)

    assert r.status_code == 200 and r.json()["state"] == "revoked"
    assert admin.revokes == [("cabal-core", "sage")]
    assert 'persona = "sage"' not in github.pushed_file("grants.toml")


def test_without_an_admin_plane_the_lane_waits_on_the_pull_request(tmp_path, fake_storage):
    github = FakeGitHub(existing_repos={GATEWAY_REPO}, extra_clone_files={"grants.toml": ""})
    client, headers = _client(tmp_path, fake_storage, github=github, admin=None, config_overrides={"liminal_admin_url": ""})
    _connect_datadog(client, headers)

    r = client.put("/api/admin/connections/sage/datadog", json={"access": "read"}, headers=headers)

    assert r.status_code == 200
    assert r.json()["state"] == "pending" and "no admin plane" in r.json()["outcome"]["live"]
    assert client.get("/api/admin/connections", headers=headers).json()["live"] is False


def test_a_refusing_admin_plane_marks_the_lane_failed_with_the_reason(tmp_path, fake_storage):
    github = FakeGitHub(existing_repos={GATEWAY_REPO}, extra_clone_files={"grants.toml": ""})
    client, headers = _client(tmp_path, fake_storage, github=github, admin=FakeLiminalAdmin(fail="HTTP 403"))
    _connect_datadog(client, headers)

    r = client.put("/api/admin/connections/sage/datadog", json={"access": "read"}, headers=headers)

    assert r.status_code == 200 and r.json()["state"] == "failed"
    assert "refused: HTTP 403" in r.json()["state_detail"]


def test_room_admin_token_is_minted_for_kobo_room_and_renewed_a_day_early():
    cabal = FakeCabal()
    clock = {"t": 1_000_000.0}
    provider = RoomAdminToken(cabal, ttl_days=7, now=lambda: clock["t"])

    assert provider() == "fake-room-admin-token"
    [call] = cabal.mint_service_calls
    assert call == {"sub": "kobo-room", "persona": "kobo-room", "aud": "liminal", "scope": ["admin:*"], "ttl_days": 7}
    provider()
    assert len(cabal.mint_service_calls) == 1
    clock["t"] += 6 * 86400 + 1
    provider()
    assert len(cabal.mint_service_calls) == 2
