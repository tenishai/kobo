"""The directory sweep (issue #274): destruction safety (it never retires an
entry the room did not register) and the admin gate on its routes."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.clients import Clients
from kobo_api.config import Config
from kobo_api.directory import sweep_directory
from kobo_api.main import create_app
from kobo_api.models import Agent, AgentStatus, Base
from tests.fakes import FakeAgent, FakeCabal, FakeCloud, FakeGitHub, FakeGojira, FakeKubectl
from tests.test_api_org_status import (
    ASSERTION_HEADER,
    WHOAMI_AUDIENCE,
    _fetcher,
    _generate_jwk,
    _sign_whoami,
)

TEMPLATE = "http://agent-{name}.default.svc.cluster.local"


def _room_entry(name: str, **over) -> dict:
    return {"name": name, "status": "active", "kind": "federated", "endpoint": TEMPLATE.format(name=name), **over}


def _session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_sweep_retires_only_the_rooms_own_entries_for_agents_it_no_longer_has(tmp_path):
    """Destruction safety: an entry without the room's registration marker
    (another endpoint, or not a graduated row) is reported, never absorbed;
    an entry for any agent row the room still holds is kept, whatever its
    state; the dry run absorbs nothing at all."""
    factory = _session_factory(tmp_path)
    session = factory()
    session.add(Agent(name="kept", persona="p", model_id="m", status=AgentStatus.DESTROYING, repo_full_name="o/r"))
    session.commit()
    cfg = Config(workdir=tmp_path / "work", agent_endpoint_template=TEMPLATE)
    cabal = FakeCabal(agents_result=[
        _room_entry("kept"),
        _room_entry("gone"),
        _room_entry("bespoke", endpoint="http://somewhere.else"),
        _room_entry("local", kind="member"),
        _room_entry("retired-already", status="retired"),
        {"name": "ops-session", "status": "active", "kind": "session", "endpoint": None},
    ])

    preview = sweep_directory(cabal, cfg, session, dry_run=True)
    assert cabal.absorb_calls == []
    assert (preview["retired"], preview["kept"], preview["unknown"]) == (["gone"], ["kept"], ["bespoke", "local"])

    result = sweep_directory(cabal, cfg, session, dry_run=False)
    assert cabal.absorb_calls == ["gone"]
    assert result["retired"] == ["gone"] and result["failed"] == []
    session.close()


def test_sweep_route_is_for_admins_only(tmp_path, monkeypatch):
    """Authorization: a signed-in non-admin gets 403 from both the preview
    and the sweep, and the switchboard is never asked."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    private_key, jwk = _generate_jwk()
    cfg = Config(
        workdir=tmp_path / "work", iap_audience=WHOAMI_AUDIENCE, operators=["ops@example.com"],
        admin_panel=True, cabal_url="http://cabal.internal", agent_endpoint_template=TEMPLATE,
    )
    cabal = FakeCabal(agents_result=[_room_entry("gone")])
    client = TestClient(create_app(
        _session_factory(tmp_path), config=cfg, iap_jwks_fetcher=_fetcher(jwk),
        clients=Clients(github=FakeGitHub(), gojira=FakeGojira(), cloud=FakeCloud(), agent=FakeAgent(), kubectl=FakeKubectl(), cabal=cabal),
    ))
    stranger = {ASSERTION_HEADER: _sign_whoami(private_key, email="someone@example.com")}

    assert client.get("/api/admin/directory/sweep", headers=stranger).status_code == 403
    assert client.post("/api/admin/directory/sweep", headers=stranger).status_code == 403
    assert cabal.list_agents_calls == 0 and cabal.absorb_calls == []

    admin = {ASSERTION_HEADER: _sign_whoami(private_key, email="ops@example.com")}
    swept = client.post("/api/admin/directory/sweep", headers=admin)
    assert swept.status_code == 200 and swept.json()["retired"] == ["gone"]
    assert cabal.absorb_calls == ["gone"]
