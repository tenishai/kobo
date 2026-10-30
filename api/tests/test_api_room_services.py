"""Room services (issue #284): the admin gate on the routes, values stored
but never echoed, and the fallback order (room over deployment) the Slack
flag and the pull request broker follow."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.clients import Clients
from kobo_api.config import Config
from kobo_api.main import create_app
from kobo_api.models import Base
from kobo_api.room_services import (
    LayeredSlackTokenStore,
    RoomServices,
    RoomSlackTokenStore,
)
from tests.fakes import FakeAgent, FakeCloud, FakeGitHub, FakeGojira, FakeKubectl
from tests.test_api_org_status import (
    ASSERTION_HEADER,
    WHOAMI_AUDIENCE,
    _fetcher,
    _generate_jwk,
    _sign_whoami,
)

SLACK_PAIR = {"config_token": "xoxe.xoxp-1-room-config", "refresh_token": "xoxe-1-room-refresh"}
GITHUB_APP = {"app_id": "4741722", "installation_id": "157070479", "private_key": "-----BEGIN RSA PRIVATE KEY-----\nroom\n-----END RSA PRIVATE KEY-----"}


class _TokenStoreSlack:
    """A Slack client whose availability is its store's answer, the way
    `HttpxSlackClient` exposes `token_store`."""

    def __init__(self, store):
        self.token_store = store


def _client(tmp_path, config: Config, *, kubectl: FakeKubectl | None, namespace: str | None = "kobo"):
    engine = create_engine(f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    private_key, jwk = _generate_jwk()
    kubectl = kubectl or FakeKubectl()
    services = RoomServices(kubectl, namespace, config) if namespace else None
    slack = _TokenStoreSlack(LayeredSlackTokenStore(RoomSlackTokenStore(services), None)) if services else None
    client = TestClient(
        create_app(
            session_factory,
            config=config,
            clients=Clients(
                github=FakeGitHub(), gojira=FakeGojira(), cloud=FakeCloud(), agent=FakeAgent(),
                kubectl=kubectl, slack=slack, room_services=services,
            ),
            iap_jwks_fetcher=_fetcher(jwk),
        )
    )
    return client, kubectl, (lambda email="ops@example.com": {ASSERTION_HEADER: _sign_whoami(private_key, email=email)})


def _config(tmp_path, **overrides) -> Config:
    values = {"workdir": tmp_path / "work", "iap_audience": WHOAMI_AUDIENCE, "operators": ["ops@example.com"], "admin_panel": True}
    values.update(overrides)
    return Config(**values)


def test_room_services_are_for_admins_only(tmp_path):
    client, _, headers = _client(tmp_path, _config(tmp_path), kubectl=None)

    for method, path, body in (
        ("GET", "/api/admin/room-services", None),
        ("PUT", "/api/admin/room-services/slack", SLACK_PAIR),
        ("DELETE", "/api/admin/room-services/slack", None),
    ):
        response = client.request(method, path, json=body, headers=headers("someone@example.com"))
        assert response.status_code == 403, (method, path)


def test_values_are_stored_in_the_rooms_secret_and_never_echoed(tmp_path):
    client, kubectl, headers = _client(tmp_path, _config(tmp_path), kubectl=None)

    put = client.put("/api/admin/room-services/github", json=GITHUB_APP, headers=headers())

    assert put.status_code == 200, put.text
    assert kubectl.secrets[("kobo", "kobo-room-github-app")]["private_key"] == GITHUB_APP["private_key"]
    status = client.get("/api/admin/room-services", headers=headers()).json()
    assert status["services"]["github"] == {
        "configured": True, "source": "room", "updated_at": status["services"]["github"]["updated_at"], "updated_by": "ops@example.com",
    }
    audit = client.get("/api/admin/audit", headers=headers()).json()["events"]
    assert audit[0]["action"] == "room_service.set"
    for text in (put.text, str(status), str(audit)):
        assert "4741722" not in text and "BEGIN RSA" not in text
    # A shape that is not a Slack config token is refused before anything is stored.
    bad = client.put("/api/admin/room-services/slack", json={**SLACK_PAIR, "config_token": "xoxb-bot"}, headers=headers())
    assert bad.status_code == 400 and ("kobo", "kobo-room-slack") not in kubectl.secrets


def test_room_values_win_over_deployment_config_and_removal_falls_back(tmp_path):
    key = tmp_path / "pr-app.pem"
    key.write_text("-----BEGIN RSA PRIVATE KEY-----\ndeployment\n-----END RSA PRIVATE KEY-----")
    config = _config(tmp_path, pr_app_id="1", pr_app_installation_id="2", pr_app_private_key_path=key)
    client, _, headers = _client(tmp_path, config, kubectl=None)
    services = client.app.state.clients.room_services

    assert client.get("/api/admin/room-services", headers=headers()).json()["services"] == {
        "slack": {"configured": False, "source": "none", "updated_at": None, "updated_by": None},
        "github": {"configured": True, "source": "deployment", "updated_at": None, "updated_by": None},
    }
    assert services.github_app().app_id == "1"

    client.put("/api/admin/room-services/github", json=GITHUB_APP, headers=headers())
    client.put("/api/admin/room-services/slack", json=SLACK_PAIR, headers=headers())
    creds = services.github_app()
    assert (creds.source, creds.app_id, creds.pem()) == ("room", GITHUB_APP["app_id"], GITHUB_APP["private_key"])
    assert client.app.state.clients.slack.token_store.read() == (SLACK_PAIR["config_token"], SLACK_PAIR["refresh_token"])

    removed = client.delete("/api/admin/room-services/github", headers=headers())
    assert removed.status_code == 200
    assert removed.json()["services"]["github"]["source"] == "deployment"
    assert services.github_app().app_id == "1"


def test_without_a_namespace_the_room_says_it_has_nowhere_to_store(tmp_path):
    client, _, headers = _client(tmp_path, _config(tmp_path), kubectl=None, namespace=None)

    status = client.get("/api/admin/room-services", headers=headers()).json()
    assert status["can_store"] is False and status["note"]
    assert client.put("/api/admin/room-services/slack", json=SLACK_PAIR, headers=headers()).status_code == 409
