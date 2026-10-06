"""Tests for the Slack-connect HTTP routes: the manifest render
(`GET .../slack/manifest`), the channels lane (`PUT .../slack/channels`,
Two-lane Slack, 2026-08-17), and token delivery/wiring
(`POST .../slack/connect`) — plus the always-present `"slack"` block
`GET /api/agents/{name}` carries. Same harness shape as
test_api_knowledge.py: fakes only (`tests/fakes.py`), no real
git/kubectl/Slack network involved anywhere.

`kobo_api.slack_setup`'s own module-level behavior (manifest exactness,
`wire_slack`/`rewire_slack_channels`'s repo edits, the full
reprovision/no-reprovision matrix) is already pinned by
test_slack_setup.py — these tests only need to prove the routes wire that
module in correctly: guards in the right order, the request/response
shapes the brief pins, and that a submitted token/channel never leaks into
an error body or a file written to the repo clone.

Slack self-service (2026-08-17) retired the config-token app-creation
route (`POST .../slack/app`): the manifest render it used to hand
`clients.slack.create_app` moved onto its own GET route instead, open to
any signed-in user (no operator gate, like the channels lane below).
Connect lost its own operator gate in the same change and gained
`app_id` in its body (the app already exists by connect time, made
directly at Slack's app site) — the former "set up a Slack app for this
agent first" 409 precondition is gone along with the route it guarded.
`clients.slack`/`FakeSlack` stay wired in this file's harness (the
config-token client itself is not retired — destruction still uses it to
delete legacy apps), just uncalled by any route these tests exercise.

Two-lane Slack moved the channel list off `/slack/connect` entirely (it
now only carries tokens, plus the app id) and onto its own
`PUT /slack/channels` route, open to everyone in the room (no operator
gate) unlike connect, which — until self-service retired the gate
entirely — stayed operator-assisted since it carries real Slack tokens.
"""

from __future__ import annotations

import time
from pathlib import Path

import jwt
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.clients import ClientError, Clients
from kobo_api.config import Config
from kobo_api.iap import ASSERTION_HEADER, IAP_ISSUER, UNAUTHORIZED_BODY
from kobo_api.main import create_app
from kobo_api.models import Agent, AgentStatus, Base
from kobo_api.personas import seed_default_personas
from kobo_api.slack_state import encode_channels
from tests.fakes import (
    FAKE_DOCKERFILE_OLD_IMAGE,
    FAKE_LOCAL_HEAD_DEFAULT,
    FakeAgent,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
    FakeLadon,
    FakeSlack,
    FakeStorage,
)

AGENT_NAME = "clio"
PERSONA = "helpful-generalist"
PERSONA_DESCRIPTION = "A friendly, plain-spoken generalist for everyday questions, drafts and quick research."
REPO = "acme/agent-clio"

APP_ID = "A0EXISTING"
BOT_TOKEN = "xoxb-super-secret-bot-token"
APP_TOKEN = "xapp-super-secret-app-token"
CHANNEL = "C1234567"
CHANNEL_2 = "C7654321"

# --- IAP JWT helpers (no-operator-gate proofs: channels lane, manifest route) --
#
# Duplicated locally rather than imported from test_web_slack_gate.py, same
# "don't cross-import test fixtures" convention this file's own
# _CapturingGitHub/_RaisingGitHub docstrings already follow.
OPERATOR_EMAIL = "ops@example.com"
NON_OPERATOR_EMAIL = "someone-else@example.com"
IAP_AUDIENCE = "test-audience"
IAP_KID = "test-key-1"


def _generate_jwk(kid: str = IAP_KID):
    private_key = ec.generate_private_key(ec.SECP256R1())
    jwk = jwt.algorithms.ECAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk["kid"] = kid
    jwk["use"] = "sig"
    jwk["alg"] = "ES256"
    return private_key, jwk


def _sign(private_key, *, email: str | None, kid: str = IAP_KID) -> str:
    now = int(time.time())
    claims = {
        "aud": IAP_AUDIENCE,
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
        self.captured_dockerfile = ""

    def commit_push(self, workdir: Path, message: str) -> bool:
        self.captured_agent_yaml = (workdir / "bundle" / "agent.yaml").read_text()
        self.captured_gojira_yaml = (workdir / "gojira.yaml").read_text()
        self.captured_dockerfile = (workdir / "Dockerfile").read_text()
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
    import kobo_api.slack_setup as slack_setup_module

    monkeypatch.setattr(slack_setup_module, "_sleep", lambda seconds: None)


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config(tmp_path):
    # Task 5: create_slack_app's persona_first_line lookup now reads the
    # bucket catalog (via search.get_entry) instead of the old local-clone
    # scanner — buckets must be named for that lookup to find anything.
    return Config(
        workdir=tmp_path / "work",
        skills_bucket="skills-bucket",
        personas_bucket="personas-bucket",
    )


# Sentinel distinguishing "slack kwarg not passed" (-> default FakeSlack())
# from an explicit `slack=None` (-> Clients.slack is None, the "Slack isn't
# configured" 503 path) — plain `None` can't serve as both.
_SLACK_UNSET = object()


def _make_client(
    session_factory,
    config,
    *,
    github=None,
    kubectl=None,
    gojira=None,
    slack=_SLACK_UNSET,
    storage=None,
    ladon=None,
    iap_jwks_fetcher=None,
):
    if storage is None:
        # Seeded the same way a real bootstrap seeds a fresh org's bucket —
        # gives create_slack_app's persona lookup something real to find
        # ("helpful-generalist") without every call site here needing to set
        # this up itself.
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
                # None (the default) is a room with no secrets platform
                # configured: token delivery stays the direct Secret write.
                ladon=ladon,
            ),
            iap_jwks_fetcher=iap_jwks_fetcher,
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
    slack_channels=None,
    slack_wired=False,
    created_by=None,
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
            slack_channels=encode_channels(slack_channels) if slack_channels is not None else None,
            slack_wired=slack_wired,
            created_by=created_by,
        )
    )
    session.commit()
    session.close()


def _onboarded_gojira(**kwargs):
    """A FakeGojira with one already-onboarded app (mirrors a READY agent:
    OnboardStep already ran create_app for it during original provisioning),
    at the cadence connect_slack's wait_active expects (activate_after=1
    unless a test overrides it)."""
    kwargs.setdefault("activate_after", 1)
    gojira = FakeGojira(**kwargs)
    app_id = gojira.create_app(
        name=AGENT_NAME, repo_full_name=REPO, cluster="example-cluster", namespace=AGENT_NAME
    )
    return gojira, app_id


NON_READY_STATUSES = [AgentStatus.PENDING, AgentStatus.PROVISIONING, AgentStatus.FAILED]


# --- GET /api/agents/{name}/slack/manifest ------------------------------------------
#
# Direct successor of the retired POST .../slack/app: every manifest-
# content assertion that route's tests made now lives here (nothing
# proven is lost), minus anything about actually calling
# `clients.slack.create_app` or persisting an `app_id` — that flow is
# retired along with the route, since self-service means kobo never
# calls Slack's API to create an app at all anymore.


def test_slack_manifest_renders_agent_manifest(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}/slack/manifest")

    assert response.status_code == 200
    manifest = response.json()["manifest"]
    assert manifest["display_information"]["name"] == AGENT_NAME
    assert manifest["display_information"]["description"] == PERSONA_DESCRIPTION
    assert manifest["features"]["bot_user"]["display_name"] == AGENT_NAME
    assert manifest["oauth_config"]["scopes"]["bot"] == [
        "app_mentions:read",
        "channels:history",
        "groups:history",
        "groups:read",
        "channels:read",
        "im:history",
        "im:read",
        "im:write",
        "mpim:history",
        "mpim:read",
        "chat:write",
        "files:read",
        "reactions:read",
        "reactions:write",
    ]


def test_slack_manifest_falls_back_when_persona_removed_from_catalog(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)
    # Simulate the persona having been retired from the catalog since this
    # agent was created: the route must not crash, and must say something
    # sensible rather than looking the id up and getting nothing back.
    session = session_factory()
    agent = session.query(Agent).filter_by(name=AGENT_NAME).first()
    agent.persona = "retired-persona"
    session.commit()
    session.close()

    response = client.get(f"/api/agents/{AGENT_NAME}/slack/manifest")

    assert response.status_code == 200
    description = response.json()["manifest"]["display_information"]["description"]
    assert "retired-persona" in description


def test_slack_manifest_catalogs_unconfigured_falls_back_and_warns(session_factory, config, caplog):
    # Storage with empty personas bucket (unconfigured)
    empty_storage = FakeStorage()
    client = _make_client(session_factory, config, storage=empty_storage)
    _seed_agent(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}/slack/manifest")

    assert response.status_code == 200
    description = response.json()["manifest"]["display_information"]["description"]
    assert f"A {PERSONA} agent" in description
    assert "caplog" in dir()  # verify caplog fixture is available
    assert any(
        f"persona {PERSONA!r} not in the catalog" in record.message
        and "slack manifest" in record.message
        for record in caplog.records
    )


def test_slack_manifest_unknown_agent_returns_404(session_factory, config):
    client = _make_client(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}/slack/manifest")

    assert response.status_code == 404
    assert "error" in response.json()


@pytest.mark.parametrize("status", NON_READY_STATUSES)
def test_slack_manifest_non_ready_agent_returns_409(session_factory, config, status):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, status=status)

    response = client.get(f"/api/agents/{AGENT_NAME}/slack/manifest")

    assert response.status_code == 409
    assert response.json() == {"error": "agent is not ready to connect to Slack yet"}


def test_slack_manifest_no_operator_gate(session_factory, config):
    # The manifest route is ready-gated like other agent routes, no
    # operator gate at all: a non-operator identity still gets 200, even
    # with slack_operators/IAP fully engaged.
    private_key, jwk = _generate_jwk()
    op_config = config.model_copy(
        update={"slack_operators": [OPERATOR_EMAIL], "iap_audience": IAP_AUDIENCE}
    )
    client = _make_client(session_factory, op_config, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, op_config)
    token = _sign(private_key, email=NON_OPERATOR_EMAIL)

    response = client.get(
        f"/api/agents/{AGENT_NAME}/slack/manifest", headers={ASSERTION_HEADER: token}
    )

    assert response.status_code == 200


def test_slack_manifest_anonymous_blocked_by_middleware(session_factory, config):
    # Reuses the existing IAP matrix idiom (test_iap.py): with IAP on, a
    # request carrying no assertion header 401s before the route ever
    # runs — proof this new route isn't accidentally exempt.
    op_config = config.model_copy(update={"iap_audience": IAP_AUDIENCE})
    client = _make_client(session_factory, op_config)
    _seed_agent(session_factory, op_config)

    response = client.get(f"/api/agents/{AGENT_NAME}/slack/manifest")

    assert response.status_code == 401
    assert response.json() == UNAUTHORIZED_BODY


def test_create_slack_app_route_is_gone(session_factory, config):
    # Retired (Slack self-service, 2026-08-17): the config-token
    # app-creation route no longer exists — every one of its former
    # tests either moved to the manifest tests above (content) or was
    # retired outright (the API-creation-flow assertions: there's no
    # more `clients.slack.create_app` call, no more `app_id`/
    # `configure_url` response, no more "already set up"/503/502
    # create-flow guards to prove).
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)

    response = client.post(f"/api/agents/{AGENT_NAME}/slack/app")

    assert response.status_code == 404


# --- POST /api/agents/{name}/slack/connect ------------------------------------------


def test_connect_slack_wires_secret_transport_and_secrets_from_returns_sha(
    session_factory, config
):
    github = _CapturingGitHub()
    kubectl = FakeKubectl()
    gojira, app_id = _onboarded_gojira()
    client = _make_client(session_factory, config, github=github, kubectl=kubectl, gojira=gojira)
    _seed_agent(
        session_factory, config, gojira_app_id=app_id, slack_app_id=APP_ID,
        slack_channels=[CHANNEL],
    )

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"app_id": APP_ID, "bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
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
    assert github.pushes[-1][1] == "kobo: connect slack"

    # a changed push (the default) triggers a reprovision
    assert gojira.reprovision_calls == [app_id]

    # tokens never land in a file written to the clone
    assert BOT_TOKEN not in github.captured_agent_yaml
    assert APP_TOKEN not in github.captured_agent_yaml
    assert BOT_TOKEN not in github.captured_gojira_yaml
    assert APP_TOKEN not in github.captured_gojira_yaml

    # persisted: slack_wired flips True, state derives to "connected"
    detail = client.get(f"/api/agents/{AGENT_NAME}").json()
    assert detail["slack"] == {
        "app_id": APP_ID, "channels": [CHANNEL], "state": "connected",
        "wake": "mention",
    }


def test_connect_starting_from_null_app_id_stores_app_id_and_self_service_flag(
    session_factory, config
):
    # Slack self-service (2026-08-17): the old "set up a Slack app for this
    # agent first" 409 precondition is gone — connect now works starting
    # from a null slack_app_id (the app was created directly at Slack's
    # own app site, never through kobo). Success stores BOTH the id and
    # the provenance flag, never flipped back, so destroy-slack can later
    # read it directly instead of inferring the same distinction from
    # Slack's error codes.
    gojira, app_id = _onboarded_gojira()
    client = _make_client(session_factory, config, github=_CapturingGitHub(), gojira=gojira)
    _seed_agent(session_factory, config, gojira_app_id=app_id, slack_channels=[CHANNEL])
    # slack_app_id left at its default: None

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"app_id": APP_ID, "bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
    )

    assert response.status_code == 200

    session = session_factory()
    agent = session.query(Agent).filter_by(name=AGENT_NAME).first()
    assert agent.slack_app_id == APP_ID
    assert agent.slack_app_self_service is True
    session.close()


def test_connect_slack_rewrites_dockerfile_to_configured_runtime_image(session_factory, config):
    # The runtime-bump incident fix: a repo's Dockerfile pins its base image
    # at compose time, so a connect that only edits agent.yaml/gojira.yaml
    # can leave a stale base in place — the runtime then crashloops on a
    # transport whose pip extra that stale image never had. The route must
    # pass the config's CURRENT runtime_image through to wire_slack, which
    # re-pins the clone's Dockerfile FROM line to it. FakeGitHub's default
    # Dockerfile starts on FAKE_DOCKERFILE_OLD_IMAGE, distinct from
    # `config.runtime_image`'s default, so this proves the rewrite actually
    # happened rather than the two already matching by coincidence.
    assert FAKE_DOCKERFILE_OLD_IMAGE != config.runtime_image

    github = _CapturingGitHub()
    kubectl = FakeKubectl()
    gojira, app_id = _onboarded_gojira()
    client = _make_client(session_factory, config, github=github, kubectl=kubectl, gojira=gojira)
    _seed_agent(
        session_factory, config, gojira_app_id=app_id, slack_app_id=APP_ID,
        slack_channels=[CHANNEL],
    )

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"app_id": APP_ID, "bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
    )

    assert response.status_code == 200
    assert github.captured_dockerfile == (
        "# Agent image: the shared runtime plus this agent's bundle.\n"
        f"FROM {config.runtime_image}\n"
        "COPY bundle/ /bundle\n"
    )
    assert BOT_TOKEN not in github.captured_dockerfile
    assert APP_TOKEN not in github.captured_dockerfile


def test_connect_slack_through_the_secrets_platform_records_the_caller(
    session_factory, config
):
    # The lane configured: the tokens go to the secrets platform instead of
    # straight into the namespace, recorded against the VERIFIED caller
    # doing the wiring -- not the agent's creator, who is someone else here.
    private_key, jwk = _generate_jwk()
    iap_config = config.model_copy(update={"iap_audience": IAP_AUDIENCE})
    ladon = FakeLadon()
    kubectl = FakeKubectl()
    gojira, app_id = _onboarded_gojira()
    client = _make_client(
        session_factory, iap_config, github=_CapturingGitHub(), kubectl=kubectl,
        gojira=gojira, ladon=ladon, iap_jwks_fetcher=_fetcher(jwk),
    )
    _seed_agent(
        session_factory, iap_config, gojira_app_id=app_id, slack_app_id=APP_ID,
        slack_channels=[CHANNEL], created_by=OPERATOR_EMAIL,
    )

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"app_id": APP_ID, "bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
        headers={ASSERTION_HEADER: _sign(private_key, email=OPERATOR_EMAIL)},
    )

    assert response.status_code == 200
    assert ladon.puts == [
        (
            OPERATOR_EMAIL,
            f"apps/{AGENT_NAME}/slack",
            {"SLACK_BOT_TOKEN": BOT_TOKEN, "SLACK_APP_TOKEN": APP_TOKEN},
        )
    ]
    assert ladon.syncs == [
        (
            OPERATOR_EMAIL,
            AGENT_NAME,
            f"apps/{AGENT_NAME}/slack",
            AGENT_NAME,
            f"{AGENT_NAME}-slack",
        )
    ]
    # Never both writers for one Secret name.
    assert kubectl.secrets_applied == []


def test_connect_slack_through_the_secrets_platform_falls_back_to_the_creator(
    session_factory, config
):
    # No identity perimeter in front of the room (IAP off): the agent's
    # creator is the only person there is to name, same as the background
    # pipeline's colleague-pass delivery.
    ladon = FakeLadon()
    gojira, app_id = _onboarded_gojira()
    client = _make_client(
        session_factory, config, github=_CapturingGitHub(), gojira=gojira, ladon=ladon
    )
    _seed_agent(
        session_factory, config, gojira_app_id=app_id, slack_app_id=APP_ID,
        slack_channels=[CHANNEL], created_by=OPERATOR_EMAIL,
    )

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"app_id": APP_ID, "bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
    )

    assert response.status_code == 200
    assert ladon.puts[0][0] == OPERATOR_EMAIL


def test_connect_slack_with_nobody_to_record_the_write_against_is_a_409(
    session_factory, config
):
    # Not the retryable 502: retrying changes nothing, since there is no
    # verified caller and the agent has no creator on record either. The
    # refusal says so in one sentence naming only the agent.
    github = _CapturingGitHub()
    ladon = FakeLadon()
    kubectl = FakeKubectl()
    gojira, app_id = _onboarded_gojira()
    client = _make_client(
        session_factory, config, github=github, kubectl=kubectl, gojira=gojira, ladon=ladon
    )
    _seed_agent(
        session_factory, config, gojira_app_id=app_id, slack_app_id=APP_ID,
        slack_channels=[CHANNEL], created_by=None,
    )

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"app_id": APP_ID, "bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
    )

    assert response.status_code == 409
    error = response.json()["error"]
    assert AGENT_NAME in error
    assert BOT_TOKEN not in error
    assert APP_TOKEN not in error
    # Nothing was attempted: no delivery, no direct write, no push.
    assert ladon.puts == []
    assert kubectl.secrets_applied == []
    assert github.pushes == []


def test_connect_slack_with_no_acting_email_is_unaffected_when_the_lane_is_off(
    session_factory, config
):
    # The refusal belongs to the secrets platform's lane alone: a room
    # writing Secrets directly never needed a person's name for it, and
    # nothing about that changes here.
    gojira, app_id = _onboarded_gojira()
    client = _make_client(
        session_factory, config, github=_CapturingGitHub(), gojira=gojira
    )
    _seed_agent(
        session_factory, config, gojira_app_id=app_id, slack_app_id=APP_ID,
        slack_channels=[CHANNEL], created_by=None,
    )

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"app_id": APP_ID, "bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
    )

    assert response.status_code == 200


def test_connect_slack_secrets_platform_failure_is_the_same_502_as_a_failed_write(
    session_factory, config
):
    github = _CapturingGitHub()
    gojira, app_id = _onboarded_gojira()
    client = _make_client(
        session_factory, config, github=github, gojira=gojira,
        ladon=FakeLadon(put_error="the secrets platform is unavailable right now"),
    )
    _seed_agent(
        session_factory, config, gojira_app_id=app_id, slack_app_id=APP_ID,
        slack_channels=[CHANNEL], created_by=OPERATOR_EMAIL,
    )

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"app_id": APP_ID, "bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
    )

    assert response.status_code == 502
    assert response.json() == {"error": "couldn't set up Slack. Try again."}
    assert github.pushes == []
    # Not recorded as connected: nothing was delivered.
    session = session_factory()
    assert session.query(Agent).filter_by(name=AGENT_NAME).first().slack_wired is False
    session.close()


def test_connect_slack_returns_503_when_slack_not_configured(session_factory, config):
    client = _make_client(session_factory, config, slack=None)
    _seed_agent(session_factory, config, slack_app_id=APP_ID)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"app_id": APP_ID, "bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
    )

    assert response.status_code == 503
    assert response.json() == {"error": "Slack is not configured"}


def test_connect_requires_saved_channels(session_factory, config):
    # Two-lane Slack (2026-08-17): connect no longer carries a channel of
    # its own — it wires whatever's already saved via PUT .../slack/
    # channels. Nothing saved yet is a 409, not a silent empty-list wire.
    # Preserved unchanged by Slack self-service: a well-formed app_id
    # doesn't bypass this guard.
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)  # no channels saved

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"app_id": APP_ID, "bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
    )

    assert response.status_code == 409
    assert response.json() == {"error": "Add at least one channel first."}


def test_connect_body_no_longer_takes_channel(session_factory, config):
    # An old-shape body (still carrying "channel") parses fine — pydantic
    # ignores the extra key by default, no 422 — but the route must wire
    # the STORED channel list (agent.slack_channels), never anything from
    # the request body: proof the field is truly gone, not just silently
    # unvalidated.
    github = _CapturingGitHub()
    gojira, app_id = _onboarded_gojira()
    client = _make_client(session_factory, config, github=github, gojira=gojira)
    _seed_agent(
        session_factory, config, gojira_app_id=app_id, slack_app_id=APP_ID,
        slack_channels=[CHANNEL],
    )

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={
            "channel": "C9999999", "app_id": APP_ID,
            "bot_token": BOT_TOKEN, "app_token": APP_TOKEN,
        },
    )

    assert response.status_code == 200
    agent_yaml = yaml.safe_load(github.captured_agent_yaml)
    assert {"kind": "slack", "channel": CHANNEL} in agent_yaml["transports"]
    assert "C9999999" not in github.captured_agent_yaml


def test_connect_sets_wired_and_payload_state_connected(session_factory, config):
    gojira, app_id = _onboarded_gojira()
    client = _make_client(session_factory, config, github=_CapturingGitHub(), gojira=gojira)
    _seed_agent(
        session_factory, config, gojira_app_id=app_id, slack_app_id=APP_ID,
        slack_channels=[CHANNEL],
    )

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"app_id": APP_ID, "bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
    )

    assert response.status_code == 200

    detail = client.get(f"/api/agents/{AGENT_NAME}").json()
    assert detail["slack"]["state"] == "connected"
    assert detail["slack"]["channels"] == [CHANNEL]


def test_connect_slack_app_id_validated_before_token_formats(session_factory, config):
    # app_id is validated FIRST, ahead of either token's format: a request
    # with a malformed app_id AND malformed tokens gets the app_id's 422,
    # never a token one.
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, slack_channels=[CHANNEL])

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={
            "app_id": "not-an-app-id", "bot_token": "also-bad", "app_token": "also-bad",
        },
    )

    assert response.status_code == 422
    assert response.json() == {
        "error": "that doesn't look like a Slack app ID (it starts with A)"
    }


def test_connect_slack_bad_app_id_returns_422_and_never_echoes_tokens(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, slack_channels=[CHANNEL])
    bad_app_id = "C1234567"  # well-formed channel id, not an app id

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"app_id": bad_app_id, "bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
    )

    assert response.status_code == 422
    assert response.json() == {
        "error": "that doesn't look like a Slack app ID (it starts with A)"
    }
    assert BOT_TOKEN not in response.text
    assert APP_TOKEN not in response.text


def test_connect_slack_missing_bot_token_returns_422_without_echoing_tokens(
    session_factory, config
):
    # Final-review Finding 1 (CRITICAL), retargeted: with SlackConnectRequest
    # now carrying an app id plus tokens, the field that can go missing and
    # trip the app-wide RequestValidationError handler is a token field
    # itself (app_id is supplied here so only app_token is missing). Same
    # proof as before: the handler must build its message from `loc` only,
    # never from Pydantic's `input`, which would otherwise echo the
    # submitted token verbatim.
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, slack_app_id=APP_ID, slack_channels=[CHANNEL])
    bot_sentinel = "xoxb-SENTINEL-BOT-TOKEN"

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"app_id": APP_ID, "bot_token": bot_sentinel},
    )

    assert response.status_code == 422
    body = response.json()
    assert set(body) == {"error"}
    assert isinstance(body["error"], str)
    assert bot_sentinel not in response.text


def test_connect_slack_bad_bot_token_returns_422_and_never_echoes_it(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, slack_app_id=APP_ID, slack_channels=[CHANNEL])
    bad_token = "not-a-bot-token"

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"app_id": APP_ID, "bot_token": bad_token, "app_token": APP_TOKEN},
    )

    assert response.status_code == 422
    body = response.json()
    assert "error" in body
    assert bad_token not in response.text
    assert APP_TOKEN not in response.text


def test_connect_slack_bad_app_token_returns_422_and_never_echoes_it(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, slack_app_id=APP_ID, slack_channels=[CHANNEL])
    bad_token = "not-an-app-token"

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"app_id": APP_ID, "bot_token": BOT_TOKEN, "app_token": bad_token},
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
    _seed_agent(
        session_factory, config, gojira_app_id=app_id, slack_app_id=APP_ID,
        slack_channels=[CHANNEL],
    )

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"app_id": APP_ID, "bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
    )

    assert response.status_code == 502
    assert response.json() == {"error": "couldn't set up Slack. Try again."}
    assert BOT_TOKEN not in response.text
    assert APP_TOKEN not in response.text


def test_connect_slack_gojira_failed_status_returns_502(session_factory, config):
    # wait_active's "failed" branch must map to the same plain 502 as every
    # other ClientError here — not bubble up as a raw, uncaught exception
    # through the route (the bug: wait_active raised bare RuntimeError,
    # which connect_slack's `except ClientError` does not catch, since
    # ClientError subclasses RuntimeError and not the reverse).
    gojira, app_id = _onboarded_gojira(
        end_status="failed", failed_steps=[{"name": "wire", "status": "failed"}]
    )
    client = _make_client(session_factory, config, github=_CapturingGitHub(), gojira=gojira)
    _seed_agent(
        session_factory, config, gojira_app_id=app_id, slack_app_id=APP_ID,
        slack_channels=[CHANNEL],
    )

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"app_id": APP_ID, "bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
    )

    assert response.status_code == 502
    assert response.json() == {"error": "couldn't set up Slack. Try again."}
    assert BOT_TOKEN not in response.text
    assert APP_TOKEN not in response.text


def test_connect_slack_gojira_timeout_returns_502(session_factory, config):
    # Same mapping, via wait_active's other RuntimeError-turned-ClientError
    # branch: exhausting every poll without reaching "active" or "failed".
    # Cheap to exercise here because the module's `_sleep` seam is
    # monkeypatched to a no-op (see `_no_real_sleep` above) — the 60-poll
    # budget runs with no real waiting.
    gojira, app_id = _onboarded_gojira(activate_after=10_000)
    client = _make_client(session_factory, config, github=_CapturingGitHub(), gojira=gojira)
    _seed_agent(
        session_factory, config, gojira_app_id=app_id, slack_app_id=APP_ID,
        slack_channels=[CHANNEL],
    )

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"app_id": APP_ID, "bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
    )

    assert response.status_code == 502
    assert response.json() == {"error": "couldn't set up Slack. Try again."}
    assert BOT_TOKEN not in response.text
    assert APP_TOKEN not in response.text


def test_connect_slack_client_error_from_github_returns_502(session_factory, config):
    gojira, app_id = _onboarded_gojira()
    client = _make_client(session_factory, config, github=_RaisingGitHub(), gojira=gojira)
    _seed_agent(
        session_factory, config, gojira_app_id=app_id, slack_app_id=APP_ID,
        slack_channels=[CHANNEL],
    )

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"app_id": APP_ID, "bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
    )

    assert response.status_code == 502
    assert response.json() == {"error": "couldn't set up Slack. Try again."}
    assert BOT_TOKEN not in response.text
    assert APP_TOKEN not in response.text


# --- PUT /api/agents/{name}/slack/channels ------------------------------------------


def test_put_channels_stores_and_reports_waiting(session_factory, config):
    # No tokens/wiring yet: 200, state "waiting" (a saved list with an
    # unwired agent), no "sha" key at all.
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)

    response = client.put(
        f"/api/agents/{AGENT_NAME}/slack/channels", json={"channels": [CHANNEL]}
    )

    assert response.status_code == 200
    assert response.json() == {"channels": [CHANNEL], "state": "waiting"}

    detail = client.get(f"/api/agents/{AGENT_NAME}").json()
    assert detail["slack"]["channels"] == [CHANNEL]
    assert detail["slack"]["state"] == "waiting"


def test_put_channels_rejects_bad_id(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)

    response = client.put(
        f"/api/agents/{AGENT_NAME}/slack/channels", json={"channels": ["not-a-channel"]}
    )

    assert response.status_code == 422
    assert response.json() == {
        "error": "that doesn't look like a channel ID (it starts with C)"
    }
    # nothing stored
    detail = client.get(f"/api/agents/{AGENT_NAME}").json()
    assert detail["slack"]["channels"] == []


def test_put_channels_rejects_legacy_private_channel_id(session_factory, config):
    # A legacy private-channel id ("G...") is well-formed but no longer
    # valid: Slack has migrated private channels to C-prefixed ids (see the
    # comment by SLACK_CHANNEL_RE in kobo_api.slack_state). Same plain
    # message as any other malformed id. This case used to be exercised
    # against the connect route directly; Two-lane Slack moved channel-
    # format validation to this route instead, so the regression case
    # moves with it.
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)

    response = client.put(
        f"/api/agents/{AGENT_NAME}/slack/channels", json={"channels": ["G1234567"]}
    )

    assert response.status_code == 422
    assert response.json() == {
        "error": "that doesn't look like a channel ID (it starts with C)"
    }


def test_put_channels_rejects_over_cap(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)
    channels = [f"C000000{i}" for i in range(9)]

    response = client.put(
        f"/api/agents/{AGENT_NAME}/slack/channels", json={"channels": channels}
    )

    assert response.status_code == 422
    assert response.json() == {"error": "Eight channels at most for now."}


def test_put_channels_dedupes(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)

    response = client.put(
        f"/api/agents/{AGENT_NAME}/slack/channels",
        json={"channels": [CHANNEL, CHANNEL, CHANNEL_2, CHANNEL]},
    )

    assert response.status_code == 200
    assert response.json()["channels"] == [CHANNEL, CHANNEL_2]


def test_put_channels_no_operator_gate(session_factory, config):
    # Unlike connect/app (operator-assisted since they carry real Slack
    # tokens), the channels lane is for everyone in the room: a non-admin-
    # operator identity still gets 200, even with slack_operators/IAP fully
    # engaged -- `slack_operators` (the retired admin allowlist this route
    # never consulted) stays irrelevant to it. Access-control Task A3 put a
    # DIFFERENT gate behind this route (owner-or-operator), so the caller
    # here is seeded as the agent's own owner to keep proving the original
    # point: no ADMIN gate blocks you from touching your own agent's
    # channels, even though a gate of some kind now exists.
    private_key, jwk = _generate_jwk()
    op_config = config.model_copy(
        update={"slack_operators": [OPERATOR_EMAIL], "iap_audience": IAP_AUDIENCE}
    )
    client = _make_client(session_factory, op_config, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, op_config, created_by=NON_OPERATOR_EMAIL)
    token = _sign(private_key, email=NON_OPERATOR_EMAIL)

    response = client.put(
        f"/api/agents/{AGENT_NAME}/slack/channels",
        json={"channels": [CHANNEL]},
        headers={ASSERTION_HEADER: token},
    )

    assert response.status_code == 200


def test_put_channels_rewires_when_wired(session_factory, config):
    # Fold-in review note (T2 review: only fresh/empty rewire paths were
    # integration-tested): seed a WIRED agent whose repo already carries a
    # DIFFERENT slack channel, proving the PUT route's rewire replaces it
    # rather than accumulating a second transport entry.
    github = _CapturingGitHub(
        agent_yaml_transports=[
            {"kind": "a2a", "host": "0.0.0.0", "port": 8080},
            {"kind": "slack", "channel": CHANNEL},
        ]
    )
    kubectl = FakeKubectl()
    gojira, app_id = _onboarded_gojira()
    client = _make_client(session_factory, config, github=github, kubectl=kubectl, gojira=gojira)
    _seed_agent(
        session_factory, config, gojira_app_id=app_id, slack_app_id="A0EXISTING",
        slack_channels=[CHANNEL], slack_wired=True,
    )

    response = client.put(
        f"/api/agents/{AGENT_NAME}/slack/channels", json={"channels": [CHANNEL_2]}
    )

    assert response.status_code == 200
    body = response.json()
    assert body == {"channels": [CHANNEL_2], "state": "connected", "sha": FAKE_LOCAL_HEAD_DEFAULT}

    # replaced, not accumulated
    agent_yaml = yaml.safe_load(github.captured_agent_yaml)
    assert agent_yaml["transports"] == [
        {"kind": "a2a", "host": "0.0.0.0", "port": 8080},
        {"kind": "slack", "channel": CHANNEL_2},
    ]
    assert github.pushes[-1][1] == "kobo: slack channels updated"

    # commit-only: rewire_slack_channels never touches kubectl/gojira
    assert kubectl.secrets_applied == []
    assert gojira.reprovision_calls == []

    detail = client.get(f"/api/agents/{AGENT_NAME}").json()
    assert detail["slack"]["channels"] == [CHANNEL_2]


def test_put_channels_unwired_empty_stores_and_reports_idle(session_factory, config):
    # PUT [] on an agent that was never wired (no slack_app_id, no
    # slack_channels): the disconnect path stores [] outright (skipping
    # clean_channels, same as every other empty-list PUT), but since
    # `agent.slack_wired` is False there's nothing to rewire — no GitHub
    # call, no "sha" key in the response, state derives to "idle" (neither
    # channels nor wired).
    github = FakeGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.put(f"/api/agents/{AGENT_NAME}/slack/channels", json={"channels": []})

    assert response.status_code == 200
    assert response.json() == {"channels": [], "state": "idle"}
    assert github.cloned == []

    detail = client.get(f"/api/agents/{AGENT_NAME}").json()
    assert detail["slack"]["channels"] == []
    assert detail["slack"]["state"] == "idle"


def test_put_channels_rewire_failure_returns_502_and_leaves_original_channels_stored(
    session_factory, config
):
    # The route's load-bearing invariant (fold-in review note): when the
    # rewire's GitHub call fails, the route returns before session.commit()
    # so the in-memory `agent.slack_channels = encode_channels(cleaned)`
    # mutation made earlier in the same request never reaches the DB (see
    # the route's own comment on get_session/Session.close()'s implicit
    # rollback). Proven end-to-end here: seed a WIRED agent with one
    # channel stored, make GitHub's clone (the first call both wire_slack
    # and rewire_slack_channels make) raise ClientError, PUT a DIFFERENT
    # channel list, and check both the 502 body AND that a follow-up GET
    # still shows the ORIGINAL stored channels — not the submitted ones.
    client = _make_client(session_factory, config, github=_RaisingGitHub())
    _seed_agent(
        session_factory, config, slack_app_id="A0EXISTING",
        slack_channels=[CHANNEL], slack_wired=True,
    )

    response = client.put(
        f"/api/agents/{AGENT_NAME}/slack/channels", json={"channels": [CHANNEL_2]}
    )

    assert response.status_code == 502
    assert response.json() == {"error": "couldn't update the channels. Try again."}

    detail = client.get(f"/api/agents/{AGENT_NAME}").json()
    assert detail["slack"]["channels"] == [CHANNEL]
    assert detail["slack"]["state"] == "connected"


def test_put_channels_empty_disconnects_when_wired(session_factory, config):
    github = _CapturingGitHub(
        agent_yaml_transports=[
            {"kind": "a2a", "host": "0.0.0.0", "port": 8080},
            {"kind": "slack", "channel": CHANNEL},
        ]
    )
    client = _make_client(session_factory, config, github=github)
    _seed_agent(
        session_factory, config, slack_app_id="A0EXISTING",
        slack_channels=[CHANNEL], slack_wired=True,
    )

    response = client.put(
        f"/api/agents/{AGENT_NAME}/slack/channels", json={"channels": []}
    )

    assert response.status_code == 200
    assert response.json() == {
        "channels": [], "state": "waiting", "sha": FAKE_LOCAL_HEAD_DEFAULT
    }

    agent_yaml = yaml.safe_load(github.captured_agent_yaml)
    assert agent_yaml["transports"] == [{"kind": "a2a", "host": "0.0.0.0", "port": 8080}]

    detail = client.get(f"/api/agents/{AGENT_NAME}").json()
    assert detail["slack"]["channels"] == []
    assert detail["slack"]["state"] == "waiting"


# --- GET /api/agents/{name}: "slack" field ------------------------------------------


def test_get_agent_slack_default_shape_before_any_setup(session_factory, config):
    # Two-lane Slack (2026-08-17): the block used to be None until an app
    # existed; it's ALWAYS present now (the UI tab is unconditional).
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}")

    assert response.status_code == 200
    assert response.json()["slack"] == {
        "app_id": None,
        "channels": [],
        "state": "idle",
        "wake": "mention",
    }


def test_get_agent_slack_carries_app_id_and_channels_after_connect(session_factory, config):
    github = _CapturingGitHub()
    gojira, app_id = _onboarded_gojira()
    client = _make_client(session_factory, config, github=github, gojira=gojira)
    _seed_agent(session_factory, config, gojira_app_id=app_id, slack_channels=[CHANNEL])
    # slack_app_id left at its default None: connect is what sets it now.

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"app_id": APP_ID, "bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
    )
    assert response.status_code == 200

    detail = client.get(f"/api/agents/{AGENT_NAME}").json()
    assert detail["slack"] == {
        "app_id": APP_ID, "channels": [CHANNEL], "state": "connected",
        "wake": "mention",
    }


def test_agent_payload_no_longer_carries_slack_admin_visible(session_factory, config):
    # Slack self-service (2026-08-17): the setup lane is no longer gated,
    # so the UI has nothing left to hide behind this key. Its predecessor
    # test (`slack_admin_visible` replacing an even older `slack_visible`)
    # is fully retired along with the key itself.
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}")

    assert response.status_code == 200
    assert "slack_admin_visible" not in response.json()


# --- PUT .../slack/wake (2026-10-06) --------------------------------------------


class _GojiraCapturingGitHub(FakeGitHub):
    """Snapshots gojira.yaml at commit_push: the wake route edits only that file."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.captured_gojira: dict | None = None

    def commit_push(self, workdir: Path, message: str) -> bool:
        target = workdir / "gojira.yaml"
        if target.is_file():
            self.captured_gojira = yaml.safe_load(target.read_text())
        return super().commit_push(workdir, message)


def test_put_wake_writes_the_env_key_and_says_when_the_manifest_changed(session_factory, config):
    gojira, app_id = _onboarded_gojira()
    github = _GojiraCapturingGitHub()
    client = _make_client(session_factory, config, github=github, gojira=gojira)
    _seed_agent(session_factory, config, gojira_app_id=app_id)

    # A row from before the choice reads as mention, and the manifest says so.
    assert client.get(f"/api/agents/{AGENT_NAME}").json()["slack"]["wake"] == "mention"
    events = client.get(f"/api/agents/{AGENT_NAME}/slack/manifest").json()["manifest"]["settings"][
        "event_subscriptions"
    ]["bot_events"]
    assert "message.channels" not in events

    response = client.put(f"/api/agents/{AGENT_NAME}/slack/wake", json={"wake": "channel"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["wake"], body["previous_wake"], body["manifest_changed"]) == ("channel", "mention", True)
    assert github.captured_gojira is not None
    assert github.captured_gojira["env"]["SLACK_WAKE_MODE"] == "channel"

    assert client.get(f"/api/agents/{AGENT_NAME}").json()["slack"]["wake"] == "channel"
    events = client.get(f"/api/agents/{AGENT_NAME}/slack/manifest").json()["manifest"]["settings"][
        "event_subscriptions"
    ]["bot_events"]
    assert "message.channels" in events and "message.groups" in events

    # thread <-> channel keeps the same events: nothing to re-apply in Slack.
    again = client.put(f"/api/agents/{AGENT_NAME}/slack/wake", json={"wake": "thread"}).json()
    assert (again["previous_wake"], again["manifest_changed"]) == ("channel", False)


def test_put_wake_refuses_a_word_that_is_not_a_mode(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)
    response = client.put(f"/api/agents/{AGENT_NAME}/slack/wake", json={"wake": "sometimes"})
    assert response.status_code == 422
    assert response.json() == {"error": "choose one of: mention, thread, channel"}
    assert client.get(f"/api/agents/{AGENT_NAME}").json()["slack"]["wake"] == "mention"
