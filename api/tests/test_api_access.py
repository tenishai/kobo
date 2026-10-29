"""Access through bab (design 2026-10-27): the picker, an owner's request, the
reviewer's decision, the grant on the agent page, a revoke, and the topology
the room pushes. FakeBab stands in for the permission service; the routes are
the contract."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.clients import Clients
from kobo_api.config import Config
from kobo_api.main import create_app
from kobo_api.models import Agent, AgentStatus, Base
from tests.fakes import (
    FakeAgent,
    FakeBab,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
)
from tests.test_api_org_status import (
    ASSERTION_HEADER,
    WHOAMI_AUDIENCE,
    _fetcher,
    _generate_jwk,
    _sign_whoami,
)


def _room(tmp_path, fake_storage, *, bab):
    engine = create_engine(f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    config = Config(workdir=tmp_path / "work", iap_audience=WHOAMI_AUDIENCE, operators=["ops@example.com"],
                    admin_panel=True, bab_url="http://bab.bab.svc:8080" if bab is not None else "",
                    cloud_accounts=[{"vendor": "aws", "name": "aws-prod"}, {"vendor": "aws", "name": "acme-data"},
                                    {"vendor": "gcp", "name": "acme-platform-prod"}])
    private_key, jwk = _generate_jwk()
    clients = Clients(github=FakeGitHub(), gojira=FakeGojira(), cloud=FakeCloud(), agent=FakeAgent(),
                      kubectl=FakeKubectl(), storage=fake_storage)
    clients.bab = bab
    app = create_app(session_factory, config=config, clients=clients, iap_jwks_fetcher=_fetcher(jwk))
    with session_factory() as s:
        s.add(Agent(name="ledger", persona="helpful-generalist", model_id="vertex_ai/claude-sonnet-4-5", skills=[],
                    status=AgentStatus.READY, repo_full_name="example-org/agent-ledger", team="shared",
                    created_by="ops@example.com"))
        s.commit()
    return TestClient(app), {ASSERTION_HEADER: _sign_whoami(private_key, email="ops@example.com")}


def test_ask_review_hold_revoke_and_the_topology_push(tmp_path, fake_storage):
    bab = FakeBab()
    c, h = _room(tmp_path, fake_storage, bab=bab)

    # The picker: vendor, then account (connected or listed but not yet), then
    # the vendor's permissions grouped its way with the tier as a column.
    vendors = {v["id"]: v for v in c.get("/api/access/picker", headers=h).json()["vendors"]}
    aws, gcp, dd = vendors["aws"], vendors["gcp"], vendors["datadog"]
    assert (aws["kind"], aws["label"], aws["account_noun"]) == ("cloud", "AWS", "account")
    assert [(a["id"], a["connected"]) for a in aws["accounts"]] == [("aws-prod", True), ("acme-data", False)]
    assert aws["accounts"][0]["ceiling"] == ["aws_cloudtrail_lake_query", "aws_inventory_read"]
    flat = {p["name"]: p for g in aws["groups"] for p in g["permissions"]}
    assert flat["aws_ecs_update_service"]["tier"] == "mutate"
    # A cloud the org lists but bab has no catalogue for still shows its account, not connected.
    assert gcp["kind"] == "cloud" and [(a["id"], a["connected"]) for a in gcp["accounts"]] == [("acme-platform-prod", False)]
    # A service vendor is one connection; none connected yet shows a placeholder.
    assert dd["kind"] == "service" and dd["accounts"] == [{"id": "datadog", "name": "Datadog", "connected": False, "ceiling": []}]

    # The owner asks; the request is pending on the agent page and in Review, with the owner named.
    r = c.post("/api/agents/ledger/access/requests", headers=h,
               json={"integration": "aws-prod", "permissions": ["aws_cloudtrail_lake_query"], "note": "audit lane"})
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    page = c.get("/api/agents/ledger/access", headers=h).json()
    assert page["grants"] == [] and [p["id"] for p in page["pending"]] == [rid]
    assert page["can_request"] is True and page["can_decide"] is True
    (row,) = c.get("/api/admin/access/requests", headers=h).json()["requests"]
    assert (row["id"], row["owner"], row["team"], row["requested_by"]) == (rid, "ops@example.com", "shared", "ops@example.com")

    # A denial needs a reason; an approval turns the request into grants.
    assert c.post("/api/admin/access/decide", headers=h, json={"ids": [rid], "verdict": "deny"}).status_code == 422
    out = c.post("/api/admin/access/decide", headers=h, json={"ids": [rid], "verdict": "approve"}).json()["results"]
    assert out[0]["status"] == "approved" and out[0]["outcome"] == {"aws_cloudtrail_lake_query": "granted"}
    page = c.get("/api/agents/ledger/access", headers=h).json()
    (grant,) = page["grants"]
    assert (grant["permission"], grant["expires_at"], grant["granted_by"]) == ("aws_cloudtrail_lake_query", None, "ops@example.com")
    assert page["pending"] == []

    # Revoke now is an admin's; leases come from bab.
    assert c.delete(f"/api/agents/ledger/access/grants/{grant['id']}", headers=h).json() == {"revoked": grant["id"]}
    assert bab.revoked == [(grant["id"], "ops@example.com")]
    assert c.get("/api/agents/ledger/leases", headers=h).json() == {"available": True, "leases": []}

    # The room tells bab which agents exist, with the pod identity that is each one.
    sync = c.post("/api/admin/access/sync", headers=h).json()
    assert sync["agents"] == 1
    actor, agents = bab.topologies[-1]
    assert actor == "ops@example.com"
    assert agents == [{"name": "ledger", "team": "shared", "namespace": "ledger", "service_account": "agent-ledger"}]


def test_access_is_off_without_bab(tmp_path, fake_storage):
    c, h = _room(tmp_path, fake_storage, bab=None)
    assert c.get("/api/access/picker", headers=h).status_code == 404
    assert c.get("/api/agents/ledger/access", headers=h).status_code == 404
    assert c.get("/api/admin/access/requests", headers=h).status_code == 404
    assert c.get("/api/agents/ledger/leases", headers=h).json()["available"] is False
    cards = {a["id"]: a for a in c.get("/api/admin/addons", headers=h).json()["addons"]}
    assert cards["access"]["state"] == "off"


def test_a_bab_outage_is_a_502_with_its_sentence_not_a_crash(tmp_path, fake_storage):
    c, h = _room(tmp_path, fake_storage, bab=FakeBab(error="the permission service is unreachable: ConnectError"))
    r = c.get("/api/agents/ledger/access", headers=h)
    assert r.status_code == 502 and "unreachable" in r.json()["error"]
    assert c.get("/api/agents/ledger/leases", headers=h).json()["available"] is False
    cards = {a["id"]: a for a in c.get("/api/admin/addons", headers=h).json()["addons"]}
    assert cards["access"]["state"] == "error"
