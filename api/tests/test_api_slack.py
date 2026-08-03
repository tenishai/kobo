"""Tests for the two Slack-connect HTTP routes (P2c) — app creation
(`POST .../slack/app`) and token delivery/wiring (`POST .../slack/connect`)
— plus the `"slack"` field `GET /api/agents/{name}` gains once either has
run. Same harness shape as test_api_knowledge.py: fakes only
(`tests/fakes.py`), no real git/kubectl/Slack network involved anywhere.

`cortex_api.slack_setup`'s own module-level behavior (manifest exactness,
`wire_slack`'s repo edits, the full reprovision/no-reprovision matrix) is
already pinned by test_slack_setup.py — these tests only need to prove the
routes wire that module in correctly: guards in the right order, the
request/response shapes the brief pins, and that a submitted token/channel
never leaks into an error body or a file written to the repo clone.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cortex_api.clients import ClientError, Clients
from cortex_api.config import Config
from cortex_api.main import create_app
from cortex_api.models import Agent, AgentStatus, Base
from tests.fakes import (
    FAKE_LOCAL_HEAD_DEFAULT,
    FakeAgent,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
    FakeSlack,
)

AGENT_NAME = "clio"
PERSONA = "helpful-generalist"
PERSONA_DESCRIPTION = "A friendly, concise generalist assistant."
REPO = "acme/agent-clio"

BOT_TOKEN = "xoxb-super-secret-bot-token"
APP_TOKEN = "xapp-super-secret-app-token"
CHANNEL = "C1234567"

# FakeGitHub.clone only writes bundle/agent.yaml when agent_yaml_transports
# is given; every /connect test needs the file on disk for wire_slack to
# edit, so _CapturingGitHub below defaults it (same fix/idiom as
# test_slack_setup.py's own _CapturingGitHub).
_DEFAULT_AGENT_YAML_TRANSPORTS = [{"kind": "a2a", "host": "0.0.0.0", "port": 8080}]


class _CapturingGitHub(FakeGitHub):
    """Snapshots bundle/agent.yaml and gojira.yaml content at the moment
    commit_push is called — the only window in which wire_slack's cloned
    workdir still exists before it's discarded. Same trick as
    test_api_knowledge.py's/test_slack_setup.py's own _CapturingGitHub,
    duplicated locally rather than imported cross-file."""

    def __init__(self, agent_yaml_transports=None, **kwargs):
        if agent_yaml_transports is None:
            agent_yaml_transports = _DEFAULT_AGENT_YAML_TRANSPORTS
        super().__init__(agent_yaml_transports=agent_yaml_transports, **kwargs)
        self.captured_agent_yaml = ""
        self.captured_gojira_yaml = ""

    def commit_push(self, workdir: Path, message: str) -> bool:
        self.captured_agent_yaml = (workdir / "bundle" / "agent.yaml").read_text()
        self.captured_gojira_yaml = (workdir / "gojira.yaml").read_text()
        return super().commit_push(workdir, message)


class _RaisingGitHub(FakeGitHub):
    """clone raises ClientError — the /slack/connect route's github-side 502
    seam (mirrors test_api_knowledge.py's own fixture of the same name)."""

    def clone(self, repo: str, dest: Path) -> None:
        raise ClientError("gh: rate limited")


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """wire_slack's wait_active polls gojira in a loop; never let it sleep
    for real (same discipline as test_slack_setup.py's own fixture)."""
    import cortex_api.slack_setup as slack_setup_module

    monkeypatch.setattr(slack_setup_module, "_sleep", lambda seconds: None)


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/cortex.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config(tmp_path):
    return Config(workdir=tmp_path / "work")


# Sentinel distinguishing "slack kwarg not passed" (-> default FakeSlack())
# from an explicit `slack=None` (-> Clients.slack is None, the "Slack isn't
# configured" 503 path) — plain `None` can't serve as both.
_SLACK_UNSET = object()


def _make_client(
    session_factory, config, *, github=None, kubectl=None, gojira=None, slack=_SLACK_UNSET
):
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
            ),
        )
    )


def _seed_agent(
    session_factory,
    config,
    *,
    status=AgentStatus.READY,
    repo_full_name=REPO,
    gojira_app_id=None,
    slack_app_id=None,
    slack_channel=None,
):
    session = session_factory()
    session.add(
        Agent(
            name=AGENT_NAME,
            persona=PERSONA,
            model_id=config.models[0].id,
            status=status,
            repo_full_name=repo_full_name,
            gojira_app_id=gojira_app_id,
            slack_app_id=slack_app_id,
            slack_channel=slack_channel,
        )
    )
    session.commit()
    session.close()


def _onboarded_gojira(**kwargs):
    """A FakeGojira with one already-onboarded app (mirrors a READY agent:
    OnboardStep already ran create_app for it during original provisioning),
    at the cadence connect_slack's wait_active expects (activate_after=1
    unless a test overrides it)."""
    gojira = FakeGojira(activate_after=1, **kwargs)
    app_id = gojira.create_app(
        name=AGENT_NAME, repo_full_name=REPO, cluster="example-cluster", namespace=AGENT_NAME
    )
    return gojira, app_id


NON_READY_STATUSES = [AgentStatus.PENDING, AgentStatus.PROVISIONING, AgentStatus.FAILED]


# --- POST /api/agents/{name}/slack/app ----------------------------------------------


def test_create_slack_app_renders_manifest_creates_app_persists_id(session_factory, config):
    slack = FakeSlack(app_id="A0REAL")
    client = _make_client(session_factory, config, slack=slack)
    _seed_agent(session_factory, config)

    response = client.post(f"/api/agents/{AGENT_NAME}/slack/app")

    assert response.status_code == 200
    assert response.json() == {
        "app_id": "A0REAL",
        "configure_url": "https://api.slack.com/apps/A0REAL",
    }

    manifest = slack.manifests[-1]
    assert manifest["display_information"]["name"] == AGENT_NAME
    assert manifest["display_information"]["description"] == PERSONA_DESCRIPTION
    assert manifest["features"]["bot_user"]["display_name"] == AGENT_NAME
    assert manifest["oauth_config"]["scopes"]["bot"] == [
        "app_mentions:read",
        "channels:history",
        "chat:write",
        "reactions:read",
        "reactions:write",
    ]

    detail = client.get(f"/api/agents/{AGENT_NAME}").json()
    assert detail["slack"] == {"app_id": "A0REAL", "channel": None}


def test_create_slack_app_falls_back_when_persona_removed_from_catalog(session_factory, config):
    slack = FakeSlack()
    client = _make_client(session_factory, config, slack=slack)
    _seed_agent(session_factory, config)
    # Simulate the persona having been retired from the catalog since this
    # agent was created: the route must not crash, and must say something
    # sensible rather than looking the id up and getting nothing back.
    session = session_factory()
    agent = session.query(Agent).filter_by(name=AGENT_NAME).first()
    agent.persona = "retired-persona"
    session.commit()
    session.close()

    response = client.post(f"/api/agents/{AGENT_NAME}/slack/app")

    assert response.status_code == 200
    description = slack.manifests[-1]["display_information"]["description"]
    assert "retired-persona" in description


def test_create_slack_app_unknown_agent_returns_404(session_factory, config):
    client = _make_client(session_factory, config)

    response = client.post(f"/api/agents/{AGENT_NAME}/slack/app")

    assert response.status_code == 404
    assert "error" in response.json()


@pytest.mark.parametrize("status", NON_READY_STATUSES)
def test_create_slack_app_non_ready_agent_returns_409(session_factory, config, status):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, status=status)

    response = client.post(f"/api/agents/{AGENT_NAME}/slack/app")

    assert response.status_code == 409
    assert response.json() == {"error": "agent is not ready to connect to Slack yet"}


def test_create_slack_app_returns_503_when_slack_not_configured(session_factory, config):
    client = _make_client(session_factory, config, slack=None)
    _seed_agent(session_factory, config)

    response = client.post(f"/api/agents/{AGENT_NAME}/slack/app")

    assert response.status_code == 503
    assert response.json() == {"error": "Slack is not configured"}


def test_create_slack_app_already_set_up_returns_409(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, slack_app_id="A0EXISTING")

    response = client.post(f"/api/agents/{AGENT_NAME}/slack/app")

    assert response.status_code == 409
    assert response.json() == {"error": "Slack is already set up for this agent"}


def test_create_slack_app_client_error_returns_502(session_factory, config):
    client = _make_client(session_factory, config, slack=FakeSlack(create_error="boom"))
    _seed_agent(session_factory, config)

    response = client.post(f"/api/agents/{AGENT_NAME}/slack/app")

    assert response.status_code == 502
    assert response.json() == {"error": "couldn't set up Slack — try again"}


# --- POST /api/agents/{name}/slack/connect ------------------------------------------


def test_connect_slack_wires_secret_transport_and_secrets_from_returns_sha(
    session_factory, config
):
    github = _CapturingGitHub()
    kubectl = FakeKubectl()
    gojira, app_id = _onboarded_gojira()
    client = _make_client(session_factory, config, github=github, kubectl=kubectl, gojira=gojira)
    _seed_agent(session_factory, config, gojira_app_id=app_id, slack_app_id="A0EXISTING")

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"channel": CHANNEL, "bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
    )

    assert response.status_code == 200
    assert response.json() == {"sha": FAKE_LOCAL_HEAD_DEFAULT}

    # secret delivered via kubectl with both tokens
    assert kubectl.secrets_applied == [
        (
            AGENT_NAME,
            f"{AGENT_NAME}-slack",
            {"SLACK_BOT_TOKEN": BOT_TOKEN, "SLACK_APP_TOKEN": APP_TOKEN},
        )
    ]

    # transport + secrets_from wired into the repo, commit message exact
    agent_yaml = yaml.safe_load(github.captured_agent_yaml)
    assert {"kind": "slack", "channel": CHANNEL} in agent_yaml["transports"]
    gojira_yaml = yaml.safe_load(github.captured_gojira_yaml)
    assert gojira_yaml["secrets_from"] == [f"{AGENT_NAME}-slack"]
    assert github.pushes[-1][1] == "cortex: connect slack"

    # a changed push (the default) triggers a reprovision
    assert gojira.reprovision_calls == [app_id]

    # tokens never land in a file written to the clone
    assert BOT_TOKEN not in github.captured_agent_yaml
    assert APP_TOKEN not in github.captured_agent_yaml
    assert BOT_TOKEN not in github.captured_gojira_yaml
    assert APP_TOKEN not in github.captured_gojira_yaml

    # persisted
    detail = client.get(f"/api/agents/{AGENT_NAME}").json()
    assert detail["slack"] == {"app_id": "A0EXISTING", "channel": CHANNEL}


def test_connect_slack_unknown_agent_returns_404(session_factory, config):
    client = _make_client(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"channel": CHANNEL, "bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
    )

    assert response.status_code == 404
    assert "error" in response.json()


@pytest.mark.parametrize("status", NON_READY_STATUSES)
def test_connect_slack_non_ready_agent_returns_409(session_factory, config, status):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, status=status)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"channel": CHANNEL, "bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
    )

    assert response.status_code == 409
    assert response.json() == {"error": "agent is not ready to connect to Slack yet"}


def test_connect_slack_returns_503_when_slack_not_configured(session_factory, config):
    client = _make_client(session_factory, config, slack=None)
    _seed_agent(session_factory, config, slack_app_id="A0EXISTING")

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"channel": CHANNEL, "bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
    )

    assert response.status_code == 503
    assert response.json() == {"error": "Slack is not configured"}


def test_connect_slack_no_app_yet_returns_409(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)  # slack_app_id still None

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"channel": CHANNEL, "bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
    )

    assert response.status_code == 409
    assert response.json() == {"error": "set up a Slack app for this agent first"}


def test_connect_slack_bad_channel_format_returns_422(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, slack_app_id="A0EXISTING")

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"channel": "not-a-channel", "bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
    )

    assert response.status_code == 422
    assert "error" in response.json()


def test_connect_slack_bad_bot_token_returns_422_and_never_echoes_it(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, slack_app_id="A0EXISTING")
    bad_token = "not-a-bot-token"

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"channel": CHANNEL, "bot_token": bad_token, "app_token": APP_TOKEN},
    )

    assert response.status_code == 422
    body = response.json()
    assert "error" in body
    assert bad_token not in response.text
    assert APP_TOKEN not in response.text


def test_connect_slack_bad_app_token_returns_422_and_never_echoes_it(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, slack_app_id="A0EXISTING")
    bad_token = "not-an-app-token"

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"channel": CHANNEL, "bot_token": BOT_TOKEN, "app_token": bad_token},
    )

    assert response.status_code == 422
    body = response.json()
    assert "error" in body
    assert bad_token not in response.text
    assert BOT_TOKEN not in response.text


def test_connect_slack_client_error_from_kubectl_returns_502(session_factory, config):
    kubectl = FakeKubectl(apply_secret_error="kubectl: connection refused")
    gojira, app_id = _onboarded_gojira()
    client = _make_client(session_factory, config, kubectl=kubectl, gojira=gojira)
    _seed_agent(session_factory, config, gojira_app_id=app_id, slack_app_id="A0EXISTING")

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"channel": CHANNEL, "bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
    )

    assert response.status_code == 502
    assert response.json() == {"error": "couldn't set up Slack — try again"}
    assert BOT_TOKEN not in response.text
    assert APP_TOKEN not in response.text


def test_connect_slack_client_error_from_github_returns_502(session_factory, config):
    gojira, app_id = _onboarded_gojira()
    client = _make_client(session_factory, config, github=_RaisingGitHub(), gojira=gojira)
    _seed_agent(session_factory, config, gojira_app_id=app_id, slack_app_id="A0EXISTING")

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"channel": CHANNEL, "bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
    )

    assert response.status_code == 502
    assert response.json() == {"error": "couldn't set up Slack — try again"}
    assert BOT_TOKEN not in response.text
    assert APP_TOKEN not in response.text


# --- GET /api/agents/{name}: "slack" field ------------------------------------------


def test_get_agent_slack_is_null_before_any_setup(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}")

    assert response.status_code == 200
    assert response.json()["slack"] is None


def test_get_agent_slack_carries_app_id_and_channel_after_connect(session_factory, config):
    github = _CapturingGitHub()
    gojira, app_id = _onboarded_gojira()
    client = _make_client(session_factory, config, github=github, gojira=gojira)
    _seed_agent(session_factory, config, gojira_app_id=app_id, slack_app_id="A0EXISTING")

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"channel": CHANNEL, "bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
    )
    assert response.status_code == 200

    detail = client.get(f"/api/agents/{AGENT_NAME}").json()
    assert detail["slack"] == {"app_id": "A0EXISTING", "channel": CHANNEL}
