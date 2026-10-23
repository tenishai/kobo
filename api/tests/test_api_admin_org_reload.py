"""Reload the org config from the admin panel (#185): a model added to
org.yaml lands without a restart."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.clients import Clients
from kobo_api.config import Config
from kobo_api.main import create_app
from kobo_api.models import Base
from tests.fakes import FakeAgent, FakeCloud, FakeGitHub, FakeGojira, FakeKubectl
from tests.test_api_org_status import (
    ASSERTION_HEADER,
    WHOAMI_AUDIENCE,
    _fetcher,
    _generate_jwk,
    _sign_whoami,
)

ORG_REPO = "example-org/kobo-config"
ONE_MODEL = "models:\n  - {id: vertex_ai/claude-sonnet-4-5, label: Sonnet}\n"
TWO_MODELS = ONE_MODEL + "  - {id: vertex_ai/claude-sonnet-5-5, label: Sonnet 5.5}\n"


def _client(tmp_path, fake_storage, github: FakeGitHub, *, local: Config):
    engine = create_engine(f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    private_key, jwk = _generate_jwk()
    app = create_app(
        session_factory,
        config=local,
        clients=Clients(github=github, gojira=FakeGojira(), cloud=FakeCloud(), agent=FakeAgent(),
                        kubectl=FakeKubectl(), storage=fake_storage),
        iap_jwks_fetcher=_fetcher(jwk),
    )
    app.state.local_config = local
    return TestClient(app), {ASSERTION_HEADER: _sign_whoami(private_key, email="ops@example.com")}


def test_reload_lands_a_new_model_without_a_restart(tmp_path, fake_storage):
    github = FakeGitHub(existing_repos={ORG_REPO}, org_yaml=ONE_MODEL)
    local = Config(workdir=tmp_path / "work", iap_audience=WHOAMI_AUDIENCE, operators=["ops@example.com"],
                   admin_panel=True, org_repo=ORG_REPO)
    client, headers = _client(tmp_path, fake_storage, github, local=local)

    github._org_yaml = TWO_MODELS
    response = client.post("/api/admin/org/reload", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["wired"] is True and body["models"][-1] == "vertex_ai/claude-sonnet-5-5"
    assert "need a restart" in body["note"]
    ids = [m["id"] for m in client.get("/api/catalog/models", headers=headers).json()["models"]]
    assert ids == ["vertex_ai/claude-sonnet-4-5", "vertex_ai/claude-sonnet-5-5"]


def test_reload_without_a_boot_config_says_so(tmp_path, fake_storage):
    github = FakeGitHub()
    local = Config(workdir=tmp_path / "work", iap_audience=WHOAMI_AUDIENCE, operators=["ops@example.com"], admin_panel=True)
    client, headers = _client(tmp_path, fake_storage, github, local=local)
    client.app.state.local_config = None

    response = client.post("/api/admin/org/reload", headers=headers)

    assert response.status_code == 409
    assert "nothing to reload" in response.json()["error"]
