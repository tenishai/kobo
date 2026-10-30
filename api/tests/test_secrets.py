"""Secrets handling: a secret value is never logged, echoed or written
where it does not belong, and the room's own keys stay the room's.

Tokens absent from error messages, logs, audit rows and pull-request
bodies; the secrets panel answering names and never values; room-owned
keys refused to callers; the secrets platform's lane writing nothing
directly; platform text never repeated to a person. Folded on 2026-10-29
from test_web_secrets.py, test_clients.py, test_sero.py, test_colleagues.py,
test_slack_setup.py, test_agent_env.py and others.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import jwt
import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi.testclient import TestClient
from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.context import RequestContext
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api import audit as room_audit
from kobo_api.agent_env import (
    CONTAINER_SETS_THIS_ONE,
    ENV_PR_BODY,
    ROOM_SETS_THIS_ONE,
    SECRETS_WIRING_PR_BODY,
    SYNC_FAILED,
    apply_env,
    key_refusal,
    sync_state,
)
from kobo_api.clients import (
    CabalClient,
    CabalError,
    ClientError,
    Clients,
    CommandRunner,
    GhCLI,
    HttpxSlackClient,
    KubectlCLI,
    SlackApiError,
    VertexImageClient,
    _write_slack_tokens,
)
from kobo_api.colleagues import deliver_pass
from kobo_api.config import Config, GroupChoice, MCPConfig
from kobo_api.github_app import InstallationTokenProvider
from kobo_api.groups import OktaGroupResolver, OktaPeopleSearch
from kobo_api.iap import ASSERTION_HEADER, IAP_ISSUER
from kobo_api.kinds import KIND_COMPOSED, KIND_CONNECTED, RUN_MODE_HTTP
from kobo_api.main import RESTART_FAILED, create_app
from kobo_api.mcp import audit, rate_limit
from kobo_api.mcp.server import build_mcp_server
from kobo_api.models import Agent, AgentStatus, Base, MCPToolCallLog
from kobo_api.org import load_org_config
from kobo_api.personas import seed_default_personas
from kobo_api.provisioner import Ctx
from kobo_api.redact import REDACTED, redact_secrets
from kobo_api.room_services import RoomServices
from kobo_api.sero import HttpxSeroClient, deliver_secret
from kobo_api.slack_setup import wire_slack
from kobo_api.slack_state import encode_channels
from kobo_api.steps import ConnectedWireStep
from tests.fakes import (
    FakeAgent,
    FakeCabal,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeImage,
    FakeKubectl,
    FakeSero,
    FakeSlack,
    FakeStorage,
)

# --- from test_web_secrets.py ---


AGENT = "clio"


REPO = "example-org/agent-clio"


OWNER = "owner@example.com"


PR_URL = "https://example.test/pull/7"


# Never a real credential: a placeholder standing in for one, so a test can
# prove it reaches the lane and appears in no response, error or log.
VALUE = "not-a-real-key-value"


BASE_GOJIRA_YAML = "name: clio\nservice_account: agent-clio\n"


AUDIENCE = "test-audience"


KID = "test-key-1"


@pytest.fixture(scope="module")
def _key():
    private_key = ec.generate_private_key(ec.SECP256R1())
    jwk = jwt.algorithms.ECAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk.update({"kid": KID, "use": "sig", "alg": "ES256"})
    return private_key, jwk


@pytest.fixture
def session_factory__web_secrets(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__web_secrets(tmp_path, fake_storage):
    # `iap_audience` is what makes the owner-or-operator gate real: without
    # a perimeter the room short-circuits every one of those gates to
    # allowed, so a file about who may read and write an agent's secrets
    # has to turn it on.
    return Config(
        workdir=tmp_path / "work",
        skills_bucket="skills-bucket",
        personas_bucket="personas-bucket",
        iap_audience=AUDIENCE,
        operators=[],
    )


def _github(**kwargs) -> FakeGitHub:
    """A repository whose `gojira.yaml` reads the same two ways the room
    reads it: through `read_file` (the panel's own read) and through a
    clone (what a write edits and pushes)."""
    repo_files = kwargs.pop("repo_files", {"gojira.yaml": BASE_GOJIRA_YAML})
    extra = kwargs.pop("extra_clone_files", None)
    if extra is None and "gojira.yaml" in repo_files:
        extra = {"gojira.yaml": repo_files["gojira.yaml"]}
    return FakeGitHub(
        existing_repos={REPO},
        repo_files=repo_files,
        extra_clone_files=extra,
        pr_url=PR_URL,
        **kwargs,
    )


def _client(
    session_factory__web_secrets,
    config__web_secrets,
    fake_storage,
    key,
    *,
    kind: str = KIND_COMPOSED,
    sero: FakeSero | None = None,
    github: FakeGitHub | None = None,
    kubectl: FakeKubectl | None = None,
    created_by: str | None = OWNER,
    status: AgentStatus = AgentStatus.READY,
) -> tuple[TestClient, Clients]:
    github = github or _github()
    gojira = FakeGojira()
    app_id = gojira.create_app(
        name=AGENT, repo_full_name=REPO, cluster="a-cluster", namespace=AGENT
    )
    clients = Clients(
        github=github,
        gojira=gojira,
        cloud=FakeCloud(),
        agent=FakeAgent(reply="sure"),
        kubectl=kubectl or FakeKubectl(),
        storage=fake_storage,
        slack=FakeSlack(),
        cabal=FakeCabal(),
        image=FakeImage(),
        sero=sero,
    )

    session = session_factory__web_secrets()
    session.add(
        Agent(
            name=AGENT,
            persona="connected" if kind == KIND_CONNECTED else "helpful-generalist",
            model_id="vertex_ai/claude-sonnet-4-5",
            skills=[],
            status=status,
            repo_full_name=REPO,
            gojira_app_id=app_id,
            created_by=created_by,
            kind=kind,
        )
    )
    session.commit()
    session.close()

    _, jwk = key
    app = create_app(
        session_factory__web_secrets,
        config=config__web_secrets,
        clients=clients,
        iap_jwks_fetcher=lambda: {"keys": [jwk]},
    )
    return TestClient(app), clients


def _as(key, email: str = OWNER) -> dict[str, str]:
    """A signed assertion naming `email`, the way the perimeter hands one
    to the room. Named once so every case reads as "who is asking" rather
    than as plumbing."""
    private_key, _ = key
    now = int(time.time())
    claims = {
        "aud": AUDIENCE,
        "iss": IAP_ISSUER,
        "iat": now,
        "exp": now + 3600,
        "sub": "accounts.google.com:1234567890",
        "email": email,
    }
    return {ASSERTION_HEADER: jwt.encode(claims, private_key, algorithm="ES256",
                                         headers={"kid": KID})}


def _ready(status: str, reason: str | None = None, message: str | None = None) -> dict:
    condition: dict = {"type": "Ready", "status": status}
    if reason is not None:
        condition["reason"] = reason
    if message is not None:
        condition["message"] = message
    return {"conditions": [condition]}


def _with_secret(sero: FakeSero | None = None) -> FakeSero:
    sero = sero or FakeSero()
    sero.stored[f"apps/{AGENT}/env"] = {"API_KEY": VALUE}
    return sero


@pytest.fixture(autouse=True)
def _clear_restart_window():
    rate_limit.reset()
    yield
    rate_limit.reset()


def test_the_secrets_list_answers_key_names_and_nothing_else(
    session_factory__web_secrets, config__web_secrets, fake_storage, _key
):
    """Secrets handling: The secrets list answers key names and nothing else."""
    sero = FakeSero()
    sero.stored[f"apps/{AGENT}/env"] = {"API_KEY": VALUE, "DB_URL": VALUE}
    client, _ = _client(session_factory__web_secrets, config__web_secrets, fake_storage, _key, sero=sero)

    resp = client.get(f"/api/agents/{AGENT}/secrets", headers=_as(_key))

    assert resp.status_code == 200
    body = resp.json()
    assert body["keys"] == ["API_KEY", "DB_URL"]
    assert body["platform"] is True
    assert body["secret_name"] == f"{AGENT}-env"
    assert VALUE not in resp.text


def test_the_first_secret_is_stored_synced_and_wired_into_the_agents_files(
    session_factory__web_secrets, config__web_secrets, fake_storage, _key
):
    """Secrets handling: The first secret is stored synced and wired into the agents files."""
    sero = FakeSero()
    github = _github()
    client, _ = _client(session_factory__web_secrets, config__web_secrets, fake_storage, _key, sero=sero, github=github)

    resp = client.put(
        f"/api/agents/{AGENT}/secrets",
        json={"set": {"API_KEY": VALUE}},
        headers=_as(_key),
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["keys"] == ["API_KEY"]
    assert body["wiring"] == "committed"
    # The value went to the lane, once, and nowhere else.
    assert sero.puts == [(OWNER, f"apps/{AGENT}/env", {"API_KEY": VALUE})]
    assert sero.syncs == [
        (OWNER, AGENT, f"apps/{AGENT}/env", AGENT, f"{AGENT}-env")
    ]
    assert VALUE not in resp.text
    # And the file now names the Secret the platform delivers into.
    pushed = yaml.safe_load(github.pushed_file("gojira.yaml"))
    assert pushed["secrets_from"] == [f"{AGENT}-env"]


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("lower_case", "capital letters"),
        ("SLACK_BOT_TOKEN", ROOM_SETS_THIS_ONE),
        ("VERTEXAI_LOCATION", ROOM_SETS_THIS_ONE),
    ],
)
def test_a_key_the_rule_refuses_never_reaches_the_lane(
    session_factory__web_secrets, config__web_secrets, fake_storage, _key, key, expected
):
    """Secrets handling: A key the rule refuses never reaches the lane."""
    sero = FakeSero()
    client, _ = _client(session_factory__web_secrets, config__web_secrets, fake_storage, _key, sero=sero)

    resp = client.put(
        f"/api/agents/{AGENT}/secrets", json={"set": {key: VALUE}}, headers=_as(_key)
    )

    assert resp.status_code == 422
    assert expected in resp.json()["error"]
    assert sero.puts == []


def test_an_agent_with_nobody_on_record_is_refused_rather_than_written_anonymously(
    session_factory__web_secrets, tmp_path, fake_storage, _key
):
    # A room with no perimeter in front of it AND an agent with no creator
    # recorded: there is nobody for the platform to record the write
    # against, which is a standing state rather than a blip, so it reads as
    # a refusal with the reason rather than as something to retry.
    """Secrets handling: An agent with nobody on record is refused rather than written anonymously."""
    config__web_secrets = Config(
        workdir=tmp_path / "work",
        skills_bucket="skills-bucket",
        personas_bucket="personas-bucket",
    )
    sero = FakeSero()
    client, _ = _client(
        session_factory__web_secrets, config__web_secrets, fake_storage, _key, sero=sero, created_by=None
    )

    resp = client.put(
        f"/api/agents/{AGENT}/secrets", json={"set": {"API_KEY": VALUE}}
    )

    assert resp.status_code == 409
    assert sero.puts == []


def test_a_lane_failure_never_repeats_the_platforms_own_text_or_the_value(
    session_factory__web_secrets, config__web_secrets, fake_storage, _key, caplog
):
    # The platform's own sentence can quote the request it refused, and
    # that request carried a value. So neither the response NOR the log
    # gets it: what is logged is the exception's class and the agent's
    # name, which is what an operator acts on.
    """Secrets handling: A lane failure never repeats the platforms own text or the value."""
    sero = FakeSero(put_error=f"refused the body {VALUE}")
    client, _ = _client(session_factory__web_secrets, config__web_secrets, fake_storage, _key, sero=sero)

    with caplog.at_level("WARNING"):
        resp = client.put(
            f"/api/agents/{AGENT}/secrets",
            json={"set": {"API_KEY": VALUE}},
            headers=_as(_key),
        )

    assert resp.status_code == 502
    assert VALUE not in resp.text
    assert "refused the body" not in resp.text
    assert VALUE not in caplog.text
    assert "refused the body" not in caplog.text
    # Still enough to act on.
    assert AGENT in caplog.text
    assert "ClientError" in caplog.text


def test_a_read_names_the_caller_not_the_creator(
    session_factory__web_secrets, config__web_secrets, fake_storage, _key
):
    # The platform's audit answers "who asked about this path", and the
    # person who opened the panel is the honest answer. An operator
    # reading somebody else's agent is recorded as themselves.
    """Secrets handling: A read names the caller not the creator."""
    sero = FakeSero()
    sero.stored[f"apps/{AGENT}/env"] = {"API_KEY": VALUE}
    operator = "ops@example.com"
    operator_config = config__web_secrets.model_copy(update={"operators": [operator]})
    client, _ = _client(
        session_factory__web_secrets, operator_config, fake_storage, _key, sero=sero
    )

    client.get(f"/api/agents/{AGENT}/secrets", headers=_as(_key, operator))

    # The agent's own path first, then the two the room sets itself --
    # every one of them on the caller's behalf, never the creator's.
    assert sero.reads == [
        (operator, f"apps/{AGENT}/env"),
        (operator, f"apps/{AGENT}/cabal"),
        (operator, f"apps/{AGENT}/slack"),
    ]


def test_a_failing_delivery_reads_as_an_error_and_never_quotes_the_platform(
    session_factory__web_secrets, config__web_secrets, fake_storage, _key
):
    """Secrets handling: A failing delivery reads as an error and never quotes the platform."""
    kubectl = FakeKubectl(
        external_secrets={
            (AGENT, f"{AGENT}-env"): _ready(
                "False",
                reason="SecretSyncedError",
                message=f"could not store {VALUE} at apps/{AGENT}/env",
            )
        }
    )
    client, _ = _client(
        session_factory__web_secrets, config__web_secrets, fake_storage, _key, sero=_with_secret(), kubectl=kubectl
    )

    resp = client.get(f"/api/agents/{AGENT}/secrets", headers=_as(_key))

    assert resp.json()["sync"] == {
        "state": "error",
        "message": f"{SYNC_FAILED} It reported SecretSyncedError.",
    }
    assert VALUE not in resp.text


def test_the_read_never_asks_for_the_kubernetes_secret_itself(
    session_factory__web_secrets, config__web_secrets, fake_storage, _key
):
    # The one thing this feature must not do to answer "is it set".
    """Secrets handling: The read never asks for the kubernetes secret itself."""
    kubectl = FakeKubectl(external_secrets={(AGENT, f"{AGENT}-env"): _ready("True")})
    client, _ = _client(
        session_factory__web_secrets, config__web_secrets, fake_storage, _key, sero=_with_secret(), kubectl=kubectl
    )

    client.get(f"/api/agents/{AGENT}/secrets", headers=_as(_key))

    assert kubectl.external_secret_reads == [(AGENT, f"{AGENT}-env")]
    assert kubectl.secrets == {}


def test_the_rooms_own_secrets_come_back_as_locked_rows(
    session_factory__web_secrets, config__web_secrets, fake_storage, _key
):
    """Secrets handling: The rooms own secrets come back as locked rows."""
    sero = _with_secret()
    sero.stored[f"apps/{AGENT}/cabal"] = {"CABAL_COLLEAGUE_TOKEN": VALUE}
    sero.stored[f"apps/{AGENT}/slack"] = {"SLACK_BOT_TOKEN": VALUE, "SLACK_APP_TOKEN": VALUE}
    kubectl = FakeKubectl(
        external_secrets={
            (AGENT, f"{AGENT}-cabal"): _ready("True"),
            (AGENT, f"{AGENT}-slack"): {},
        }
    )
    client, _ = _client(
        session_factory__web_secrets, config__web_secrets, fake_storage, _key, sero=sero, kubectl=kubectl
    )

    resp = client.get(f"/api/agents/{AGENT}/secrets", headers=_as(_key))

    assert resp.json()["managed"] == [
        {
            "name": "CABAL_COLLEAGUE_TOKEN",
            "purpose": "cabal",
            "secret_name": f"{AGENT}-cabal",
            "sync": {"state": "live", "message": None},
        },
        {
            "name": "SLACK_APP_TOKEN",
            "purpose": "slack",
            "secret_name": f"{AGENT}-slack",
            "sync": {"state": "syncing", "message": None},
        },
        {
            "name": "SLACK_BOT_TOKEN",
            "purpose": "slack",
            "secret_name": f"{AGENT}-slack",
            "sync": {"state": "syncing", "message": None},
        },
    ]
    assert VALUE not in resp.text


def test_a_room_managed_name_still_cannot_be_written_or_removed(
    session_factory__web_secrets, config__web_secrets, fake_storage, _key
):
    # Showing these rows changes nothing about who may set them: the same
    # refusal the key rules have always given, on both sides of a write.
    """Secrets handling: A room managed name still cannot be written or removed."""
    sero = _with_secret()
    sero.stored[f"apps/{AGENT}/slack"] = {"SLACK_BOT_TOKEN": VALUE}
    client, _ = _client(session_factory__web_secrets, config__web_secrets, fake_storage, _key, sero=sero)

    written = client.put(
        f"/api/agents/{AGENT}/secrets",
        json={"set": {"SLACK_BOT_TOKEN": VALUE}, "remove": []},
        headers=_as(_key),
    )
    removed = client.put(
        f"/api/agents/{AGENT}/secrets",
        json={"set": {}, "remove": ["CABAL_COLLEAGUE_TOKEN"]},
        headers=_as(_key),
    )

    assert written.status_code == 422
    assert ROOM_SETS_THIS_ONE in written.json()["error"]
    assert removed.status_code == 422
    assert ROOM_SETS_THIS_ONE in removed.json()["error"]
    # And the stored value is untouched: nothing reached the platform.
    assert sero.puts == []


def test_a_cluster_that_refuses_says_the_house_sentence_and_nothing_of_its_own(
    session_factory__web_secrets, config__web_secrets, fake_storage, _key
):
    """Secrets handling: A cluster that refuses says the house sentence and nothing of its own."""
    kubectl = FakeKubectl(restart_error=f'deployments.apps "{AGENT}" not found')
    client, _ = _client(session_factory__web_secrets, config__web_secrets, fake_storage, _key, kubectl=kubectl)

    resp = client.post(f"/api/agents/{AGENT}/restart", headers=_as(_key))

    assert resp.status_code == 502
    assert resp.json()["error"] == RESTART_FAILED
    assert "deployments.apps" not in resp.text


# --- from test_agent_env.py ---


def _ready__agent_env(status: str, reason: str | None = None, message: str | None = None) -> dict:
    condition: dict = {"type": "Ready", "status": status}
    if reason is not None:
        condition["reason"] = reason
    if message is not None:
        condition["message"] = message
    return {"conditions": [{"type": "Deleted", "status": "False"}, condition]}


@pytest.mark.parametrize(
    "key",
    [
        "CABAL_COLLEAGUE_TOKEN",
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "SLACK_SIGNING_SECRET",
        "VERTEXAI_PROJECT",
        "VERTEXAI_LOCATION",
        "AGENT_MODEL",
        "SLACK_WAKE_MODE",
    ],
)
def test_a_name_the_room_manages_is_refused_with_the_rooms_own_sentence(key):
    """Secrets handling: A name the room manages is refused with the rooms own sentence."""
    refusal = key_refusal(key)
    assert refusal is not None
    assert ROOM_SETS_THIS_ONE in refusal
    assert key in refusal


@pytest.mark.parametrize(
    "key",
    ["PATH", "HOME", "PYTHONPATH", "PYTHONHOME", "LD_PRELOAD", "LD_LIBRARY_PATH"],
)
def test_a_name_the_container_owns_is_refused_with_its_own_sentence(key):
    # A different fact from "the room sets this one", and a different fix:
    # nobody here decided these, the image and the runtime did, and
    # overwriting one gives an agent that cannot find its own interpreter
    # rather than a custom variable.
    """Secrets handling: A name the container owns is refused with its own sentence."""
    refusal = key_refusal(key)
    assert refusal is not None
    assert CONTAINER_SETS_THIS_ONE in refusal
    assert key in refusal


def test_apply_env_never_lets_a_write_reach_a_key_the_room_sets():
    # The route refuses this first (`write_refusal`), so this is the second
    # wall rather than the first: nothing that gets here can overwrite the
    # model wiring even if a caller found a way past the route.
    """Secrets handling: Apply env never lets a write reach a key the room sets."""
    data = {"env": {"VERTEXAI_PROJECT": "a-project"}}
    apply_env(data, {"VERTEXAI_PROJECT": "somewhere else"}, [])
    assert data["env"]["VERTEXAI_PROJECT"] == "a-project"


def test_the_pull_request_body_promises_no_secret_value_is_in_the_file():
    """Secrets handling: The pull request body promises no secret value is in the file."""
    assert "VALUES are not here" in SECRETS_WIRING_PR_BODY
    assert "—" not in SECRETS_WIRING_PR_BODY
    assert "—" not in ENV_PR_BODY


def test_the_platforms_own_message_never_reaches_the_answer():
    # The one field of somebody else's object that could carry what was
    # stored. The reason word comes along; the sentence around it does not.
    """Secrets handling: The platforms own message never reaches the answer."""
    answer = sync_state(
        _ready__agent_env(
            "False",
            reason="SecretSyncedError",
            message="could not store not-a-real-key-value at apps/clio/env",
        )
    )

    assert "not-a-real-key-value" not in str(answer)
    assert "apps/clio/env" not in str(answer)


# --- from test_sero.py ---


URL = "http://secrets.example"


AUDIENCE__sero = "http://secrets.example"


NAMESPACE = "platform"


EMAIL = "someone@example.com"


AGENT = "clio"


# Never a real credential: a placeholder standing in for one, so these tests
# can prove it appears in exactly one place and in no error text.
SECRET_VALUE = "not-a-real-token-value"


def _client__sero(handler, *, token: str = "id-token") -> HttpxSeroClient:
    return HttpxSeroClient(
        URL,
        AUDIENCE__sero,
        NAMESPACE,
        token_provider=lambda: token,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"applied": True, "action": "create"})


def test_put_secret_sends_the_rooms_own_namespace_never_one_from_a_caller():
    # The namespace is fixed per deployment: nothing a caller passes can aim
    # a write at another team's secrets.
    """Secrets handling: Put secret sends the rooms own namespace never one from a caller."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return _ok(request)

    HttpxSeroClient(
        URL, AUDIENCE__sero, "another-namespace",
        token_provider=lambda: "t",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    ).put_secret(EMAIL, "apps/clio/cabal", {"K": SECRET_VALUE})

    assert captured["json"]["namespace"] == "another-namespace"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json={"error": "denied"}),
        httpx.Response(429),
        httpx.Response(503, json={"error": "backend unavailable"}),
        # The worst case this module is written for: a server that echoes
        # the request body back in its own error text.
        httpx.Response(400, json={"error": f"bad request: {{'K': '{SECRET_VALUE}'}}"[:20]}),
        httpx.Response(500, text=f"values={SECRET_VALUE}"),
    ],
)
def test_no_failure_ever_puts_a_secret_value_in_the_raised_message(response):
    """Secrets handling: No failure ever puts a secret value in the raised message."""
    def handler(_request: httpx.Request) -> httpx.Response:
        return response

    with pytest.raises(ClientError) as put_info:
        _client__sero(handler).put_secret(EMAIL, f"apps/{AGENT}/cabal", {"K": SECRET_VALUE})

    assert SECRET_VALUE not in str(put_info.value)


def test_a_platform_that_does_not_answer_raises_without_the_request_body():
    """Secrets handling: A platform that does not answer raises without the request body."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(ClientError) as exc_info:
        _client__sero(handler).put_secret(EMAIL, f"apps/{AGENT}/cabal", {"K": SECRET_VALUE})

    message = str(exc_info.value)
    assert "did not answer" in message
    assert SECRET_VALUE not in message


@pytest.mark.parametrize("email", [None, "", "   "])
def test_deliver_secret_refuses_a_write_with_nobody_to_record_it_against(email):
    """Secrets handling: Deliver secret refuses a write with nobody to record it against."""
    sero = FakeSero()

    with pytest.raises(ClientError) as exc_info:
        deliver_secret(
            sero,
            acting_email=email,
            agent_name=AGENT,
            purpose="cabal",
            values={"K": SECRET_VALUE},
            secret_name=f"{AGENT}-cabal",
        )

    assert AGENT in str(exc_info.value)
    assert SECRET_VALUE not in str(exc_info.value)
    assert sero.puts == []
    assert sero.syncs == []


def test_deliver_secret_logs_one_line_naming_the_agent_and_purpose_only(caplog):
    """Secrets handling: Deliver secret logs one line naming the agent and purpose only."""
    sero = FakeSero()

    with caplog.at_level("INFO", logger="kobo_api.sero"):
        deliver_secret(
            sero,
            acting_email=EMAIL,
            agent_name=AGENT,
            purpose="cabal",
            values={"K": SECRET_VALUE},
            secret_name=f"{AGENT}-cabal",
        )

    lines = [record.getMessage() for record in caplog.records]
    assert len(lines) == 1
    assert AGENT in lines[0]
    assert "cabal" in lines[0]
    assert SECRET_VALUE not in lines[0]
    assert EMAIL not in lines[0]


# --- from test_colleagues.py ---


AGENT_NAME = "clio"


PERSONA = "helpful-generalist"


MODEL_ID = "vertex_ai/claude-sonnet-4-5"


CREATOR = "someone@example.com"


def _agent(**overrides) -> Agent:
    fields = {"name": AGENT_NAME, "persona": PERSONA, "model_id": MODEL_ID}
    fields.update(overrides)
    return Agent(**fields)


def test_deliver_pass_never_leaks_the_minted_token_in_a_raised_message():
    """Secrets handling: Deliver pass never leaks the minted token in a raised message."""
    cabal = FakeCabal(mint_colleague_result="ultra-secret-value")
    kubectl = FakeKubectl(apply_secret_error="kubectl apply failed")

    with pytest.raises(ClientError) as exc_info:
        deliver_pass(kubectl, cabal, _agent())

    assert "ultra-secret-value" not in str(exc_info.value)


def test_deliver_pass_through_the_lane_writes_no_secret_directly():
    # Never both paths: an agent whose Secret has two writers has two
    # sources of truth, and the next sync would undo a human's repair.
    """Secrets handling: Deliver pass through the lane writes no secret directly."""
    cabal = FakeCabal()
    kubectl = FakeKubectl()

    deliver_pass(kubectl, cabal, _agent(created_by=CREATOR), FakeSero())

    assert kubectl.secrets_applied == []


def test_deliver_pass_lane_failure_fails_the_step_with_no_fallback_write():
    """Secrets handling: Deliver pass lane failure fails the step with no fallback write."""
    cabal = FakeCabal()
    kubectl = FakeKubectl()
    sero = FakeSero(put_error="the secrets platform is unavailable right now")

    with pytest.raises(ClientError) as exc_info:
        deliver_pass(kubectl, cabal, _agent(created_by=CREATOR), sero)

    assert "unavailable" in str(exc_info.value)
    assert kubectl.secrets_applied == []


def test_deliver_pass_through_the_lane_refuses_an_agent_with_no_creator():
    # Nobody to record the write against is a refusal, not a default: the
    # lane exists so every stored secret has a person's name on it.
    """Secrets handling: Deliver pass through the lane refuses an agent with no creator."""
    cabal = FakeCabal()
    kubectl = FakeKubectl()
    sero = FakeSero()

    with pytest.raises(ClientError) as exc_info:
        deliver_pass(kubectl, cabal, _agent(created_by=None), sero)

    assert AGENT_NAME in str(exc_info.value)
    assert sero.puts == []
    assert kubectl.secrets_applied == []


# --- from test_connected_pipeline.py ---


AGENT_NAME = "clio"


REPO_NAME = "team-chatbot"


MODEL_ID = "vertex_ai/claude-sonnet-4-5"


WIRE_PR_URL = "https://github.com/example-org/team-chatbot/pull/4"


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    import kobo_api.steps as steps_module

    monkeypatch.setattr(steps_module, "_sleep", lambda seconds: None)


def _repo(config) -> str:
    return f"{config.github_org}/{REPO_NAME}"


def _make_agent(
    session_factory,
    config,
    *,
    kind: str = KIND_CONNECTED,
    run_mode: str | None = RUN_MODE_HTTP,
    secret_names: list[str] | None = None,
    repo_full_name: str | None = None,
    gojira_app_id: int | None = None,
    purpose_tags: str | None = None,
):
    session = session_factory()
    agent = Agent(
        name=AGENT_NAME,
        persona="connected",
        model_id=MODEL_ID,
        skills=[],
        kind=kind,
        run_mode=run_mode,
        secret_names=secret_names or [],
        repo_full_name=repo_full_name if repo_full_name is not None else _repo(config),
        gojira_app_id=gojira_app_id,
        purpose_tags=purpose_tags,
    )
    session.add(agent)
    session.commit()
    return session, agent


def _make_ctx(session, agent, config, clients) -> Ctx:
    workdir = config.workdir / agent.name
    workdir.mkdir(parents=True, exist_ok=True)
    return Ctx(agent=agent, session=session, config=config, clients=clients, workdir=workdir)


def _clients(*, github=None, gojira=None, cloud=None, kubectl=None, image=None, agent=None):
    return Clients(
        github=github,
        gojira=gojira,
        cloud=cloud,
        agent=agent,
        kubectl=kubectl,
        image=image,
    )


def _seeded_gojira(config, repo, *, activate_after: int = 1) -> FakeGojira:
    """A FakeGojira that already holds app id 1 for `repo`, so a step which
    only waits (never onboards) has something to poll."""
    gojira = FakeGojira(activate_after=activate_after)
    gojira.create_app(
        name=AGENT_NAME,
        repo_full_name=repo,
        cluster=config.gojira_cluster,
        namespace=AGENT_NAME,
    )
    return gojira


@dataclass
class _Wiring:
    """Everything a ConnectedWireStep test needs, named rather than
    unpacked positionally -- each test reads two or three of these five."""

    session: object
    agent: Agent
    github: FakeGitHub
    gojira: FakeGojira
    ctx: Ctx


def _wire_ctx(session_factory, config, *, github=None, gojira=None, **agent_kwargs):
    repo = _repo(config)
    session, agent = _make_agent(session_factory, config, gojira_app_id=1, **agent_kwargs)
    github = github if github is not None else FakeGitHub(pr_url=WIRE_PR_URL)
    gojira = gojira if gojira is not None else _seeded_gojira(config, repo)
    return _Wiring(
        session=session,
        agent=agent,
        github=github,
        gojira=gojira,
        ctx=_make_ctx(session, agent, config, _clients(github=github, gojira=gojira)),
    )


@pytest.mark.parametrize(
    "broken",
    ["clone_error", "find_pr", "branch_exists", "open_pr", "merge_pr", "reprovision"],
)
def test_connected_wire_step_never_puts_a_clients_own_error_text_on_the_step(
    session_factory, config, broken
):
    """Secrets handling: Connected wire step never puts a clients own error text on the step.

    Whatever this step raises lands verbatim on `AgentStep.error`, which
    any viewer of the agent can read. A `gh` or platform error carries a
    command line, a URL and sometimes a token; none of that belongs there."""
    secret = "ghs_notatoken_but_shaped_like_one"
    github = FakeGitHub(
        pr_url=WIRE_PR_URL,
        clone_error=secret if broken == "clone_error" else None,
    )
    wiring = _wire_ctx(session_factory, config, github=github)

    def _boom(*args, **kwargs):
        raise ClientError(secret)

    if broken in {"find_pr", "branch_exists", "open_pr", "merge_pr"}:
        setattr(wiring.github, broken, _boom)
    if broken == "reprovision":
        wiring.gojira.reprovision = _boom

    with pytest.raises(RuntimeError) as exc_info:
        ConnectedWireStep().run(wiring.ctx)

    message = str(exc_info.value)
    assert secret not in message
    assert "—" not in message
    assert message


def test_connected_wire_step_carries_the_owners_own_secret_names(session_factory, config):
    """Secrets handling: Connected wire step carries the owners own secret names.

    The room never holds these; it only tells the platform which Secrets
    the owner will create in the agent's namespace."""
    wiring = _wire_ctx(session_factory, config, secret_names=["clio-api", "clio-db"])

    ConnectedWireStep().run(wiring.ctx)

    data = yaml.safe_load((wiring.ctx.workdir / "wire" / "gojira.yaml").read_text())
    assert data["secrets_from"] == ["clio-api", "clio-db"]


# --- from test_slack_setup.py ---


REPO__slack_setup = "acme/agent-clio"


NAME = "clio"


BOT_TOKEN = "xoxb-super-secret-bot-token"


APP_TOKEN = "xapp-super-secret-app-token"


# The human doing the wiring, recorded against every write the secrets
# platform's machine lane performs.
OPERATOR = "someone@example.com"


# The "current" configured runtime image every wire_slack test passes by
# default — deliberately distinct from FakeGitHub's own FAKE_DOCKERFILE_OLD_IMAGE
# default so a test that doesn't override either sees the FROM line actually
# change (proving the rewrite ran, not just that nothing happened to differ).
RUNTIME_IMAGE = "registry.example/agents/daedalus-runtime:new"


@pytest.fixture(autouse=True)
def _no_real_sleep__slack_setup(monkeypatch):
    """wait_active (direct, and via wire_slack) runs a poll loop — never let
    it sleep for real, same discipline as test_steps_wire.py's own fixture."""
    import kobo_api.slack_setup as slack_setup_module

    monkeypatch.setattr(slack_setup_module, "_sleep", lambda seconds: None)


def _onboarded_gojira(**kwargs) -> tuple[FakeGojira, int]:
    """A FakeGojira with one already-onboarded app (mirrors a READY agent:
    OnboardStep already ran create_app for it during original provisioning)."""
    gojira = FakeGojira(**kwargs)
    app_id = gojira.create_app(
        name=NAME, repo_full_name=REPO__slack_setup, cluster="example-cluster", namespace=NAME
    )
    return gojira, app_id


class _FailingRunner:
    """Mimics CommandRunner's real on-failure behavior (a ClientError whose
    message embeds the full argv it ran) for whichever call index
    `fail_at` names — the fixture proving apply_secret's own error handling
    strips literal secret values back out of a re-raised message rather
    than passing the argv-embedding message straight through."""

    def __init__(self, fail_at: int = 0):
        self._fail_at = fail_at
        self.calls: list[list[str]] = []

    def run(
        self, args: list[str], stdin: str | None = None, *, redact_args: bool = False
    ) -> str:
        index = len(self.calls)
        self.calls.append(list(args))
        if index == self._fail_at:
            raise ClientError(
                f"{' '.join(args)} failed (rc=1): error from server",
                returncode=1, stdout="", stderr="error from server",
            )
        return ""


# FakeGitHub.clone only writes bundle/agent.yaml when agent_yaml_transports
# is given (default None => no bundle/ tree at all, so bare FakeGitHub()
# stays inert for every pre-existing non-wire_slack test). Every wire_slack
# test needs the file on disk to edit, so _CapturingGitHub below defaults
# the knob to a real bundle's starting shape (matching kobo_api.composer's
# rendering) unless a test passes its own transports list explicitly.
_DEFAULT_AGENT_YAML_TRANSPORTS = [{"kind": "a2a", "host": "0.0.0.0", "port": 8080}]


class _CapturingGitHub(FakeGitHub):
    """Snapshots bundle/agent.yaml and gojira.yaml content at the moment
    commit_push is called — the only window in which wire_slack's cloned
    workdir still exists before it's discarded. Same trick as
    test_api_knowledge.py's own _CapturingGitHub, duplicated locally rather
    than imported cross-file."""

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


def _wire(
    github=None, kubectl=None, gojira=None, app_id=None, channels=None, **overrides
):
    github = github if github is not None else _CapturingGitHub()
    kubectl = kubectl if kubectl is not None else FakeKubectl()
    if gojira is None:
        gojira, app_id = _onboarded_gojira()
    if channels is None:
        channels = ["C1234567"]
    kwargs = {
        "repo": REPO__slack_setup,
        "name": NAME,
        "gojira_app_id": app_id,
        "channels": channels,
        "bot_token": BOT_TOKEN,
        "app_token": APP_TOKEN,
        "runtime_image": RUNTIME_IMAGE,
    }
    kwargs.update(overrides)
    sha = wire_slack(github, kubectl, gojira, **kwargs)
    return sha, github, kubectl, gojira


def test_apply_secret_dry_run_failure_message_excludes_token_values():
    """Secrets handling: Apply secret dry run failure message excludes token values."""
    runner = _FailingRunner(fail_at=0)
    client = KubectlCLI(runner=runner)

    with pytest.raises(ClientError) as exc_info:
        client.apply_secret(NAME, f"{NAME}-slack", {"SLACK_BOT_TOKEN": BOT_TOKEN})

    assert BOT_TOKEN not in str(exc_info.value)


def test_wire_slack_tokens_absent_from_every_file_written_to_the_clone():
    """Secrets handling: Wire slack tokens absent from every file written to the clone."""
    github = _CapturingGitHub()
    _wire(github=github)

    assert BOT_TOKEN not in github.captured_agent_yaml
    assert APP_TOKEN not in github.captured_agent_yaml
    assert BOT_TOKEN not in github.captured_gojira_yaml
    assert APP_TOKEN not in github.captured_gojira_yaml


def test_wire_slack_through_the_lane_writes_no_secret_directly():
    """Secrets handling: Wire slack through the lane writes no secret directly."""
    kubectl = FakeKubectl()

    _wire(kubectl=kubectl, sero=FakeSero(), acting_email=OPERATOR)

    assert kubectl.secrets_applied == []


def test_wire_slack_lane_failure_stops_before_the_push_with_no_fallback_write():
    # A lane failure fails this call the way a failed apply_secret does:
    # nothing pushed, nothing reprovisioned, and no direct Secret written.
    """Secrets handling: Wire slack lane failure stops before the push with no fallback write."""
    github = _CapturingGitHub()
    kubectl = FakeKubectl()
    gojira, app_id = _onboarded_gojira()

    with pytest.raises(ClientError, match="unavailable"):
        _wire(
            github=github, kubectl=kubectl, gojira=gojira, app_id=app_id,
            sero=FakeSero(put_error="the secrets platform is unavailable right now"),
            acting_email=OPERATOR,
        )

    assert github.pushes == []
    assert gojira.reprovision_calls == []
    assert kubectl.secrets_applied == []


# --- from test_api_slack.py ---


AGENT_NAME = "clio"


PERSONA = "helpful-generalist"


REPO__api_slack = "acme/agent-clio"


APP_ID = "A0EXISTING"


BOT_TOKEN = "xoxb-super-secret-bot-token"


APP_TOKEN = "xapp-super-secret-app-token"


CHANNEL = "C1234567"


# --- IAP JWT helpers (no-operator-gate proofs: channels lane, manifest route) --
#
# Duplicated locally rather than imported from test_web_slack_gate.py, same
# "don't cross-import test fixtures" convention this file's own
# _CapturingGitHub/_RaisingGitHub docstrings already follow.
OPERATOR_EMAIL = "ops@example.com"


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
_DEFAULT_AGENT_YAML_TRANSPORTS__api_slack = [{"kind": "a2a", "host": "0.0.0.0", "port": 8080}]


class _CapturingGitHub__api_slack(FakeGitHub):
    """Snapshots bundle/agent.yaml and gojira.yaml content at the moment
    commit_push is called — the only window in which wire_slack's cloned
    workdir still exists before it's discarded. Same trick as
    test_api_knowledge.py's/test_slack_setup.py's own _CapturingGitHub,
    duplicated locally rather than imported cross-file."""

    def __init__(self, agent_yaml_transports=None, **kwargs):
        if agent_yaml_transports is None:
            agent_yaml_transports = _DEFAULT_AGENT_YAML_TRANSPORTS__api_slack
        super().__init__(agent_yaml_transports=agent_yaml_transports, **kwargs)
        self.captured_agent_yaml = ""
        self.captured_gojira_yaml = ""
        self.captured_dockerfile = ""

    def commit_push(self, workdir: Path, message: str) -> bool:
        self.captured_agent_yaml = (workdir / "bundle" / "agent.yaml").read_text()
        self.captured_gojira_yaml = (workdir / "gojira.yaml").read_text()
        self.captured_dockerfile = (workdir / "Dockerfile").read_text()
        return super().commit_push(workdir, message)


@pytest.fixture(autouse=True)
def _no_real_sleep__api_slack(monkeypatch):
    """wire_slack's wait_active polls gojira in a loop; never let it sleep
    for real (same discipline as test_slack_setup.py's own fixture)."""
    import kobo_api.slack_setup as slack_setup_module

    monkeypatch.setattr(slack_setup_module, "_sleep", lambda seconds: None)


@pytest.fixture
def session_factory__api_slack(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__api_slack(tmp_path):
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
    session_factory__api_slack,
    config__api_slack,
    *,
    github=None,
    kubectl=None,
    gojira=None,
    slack=_SLACK_UNSET,
    storage=None,
    sero=None,
    iap_jwks_fetcher=None,
):
    if storage is None:
        # Seeded the same way a real bootstrap seeds a fresh org's bucket —
        # gives create_slack_app's persona lookup something real to find
        # ("helpful-generalist") without every call site here needing to set
        # this up itself.
        storage = FakeStorage()
        seed_default_personas(storage, bucket=config__api_slack.personas_bucket)
    return TestClient(
        create_app(
            session_factory__api_slack,
            config=config__api_slack,
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
                sero=sero,
            ),
            iap_jwks_fetcher=iap_jwks_fetcher,
        )
    )


def _seed_agent(
    session_factory__api_slack,
    config__api_slack,
    *,
    status=AgentStatus.READY,
    repo_full_name=REPO,
    gojira_app_id=None,
    slack_app_id=None,
    slack_channels=None,
    slack_wired=False,
    created_by=None,
):
    session = session_factory__api_slack()
    session.add(
        Agent(
            name=AGENT_NAME,
            persona=PERSONA,
            model_id=config__api_slack.models[0].id,
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


def _onboarded_gojira__api_slack(**kwargs):
    """A FakeGojira with one already-onboarded app (mirrors a READY agent:
    OnboardStep already ran create_app for it during original provisioning),
    at the cadence connect_slack's wait_active expects (activate_after=1
    unless a test overrides it)."""
    kwargs.setdefault("activate_after", 1)
    gojira = FakeGojira(**kwargs)
    app_id = gojira.create_app(
        name=AGENT_NAME, repo_full_name=REPO__api_slack, cluster="example-cluster", namespace=AGENT_NAME
    )
    return gojira, app_id


def test_connect_slack_through_the_secrets_platform_records_the_caller(
    session_factory__api_slack, config__api_slack
):
    # The lane configured: the tokens go to the secrets platform instead of
    # straight into the namespace, recorded against the VERIFIED caller
    # doing the wiring -- not the agent's creator, who is someone else here.
    """Secrets handling: Connect slack through the secrets platform records the caller."""
    private_key, jwk = _generate_jwk()
    iap_config = config__api_slack.model_copy(update={"iap_audience": IAP_AUDIENCE})
    sero = FakeSero()
    kubectl = FakeKubectl()
    gojira, app_id = _onboarded_gojira__api_slack()
    client = _make_client(
        session_factory__api_slack, iap_config, github=_CapturingGitHub__api_slack(), kubectl=kubectl,
        gojira=gojira, sero=sero, iap_jwks_fetcher=_fetcher(jwk),
    )
    _seed_agent(
        session_factory__api_slack, iap_config, gojira_app_id=app_id, slack_app_id=APP_ID,
        slack_channels=[CHANNEL], created_by=OPERATOR_EMAIL,
    )

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"app_id": APP_ID, "bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
        headers={ASSERTION_HEADER: _sign(private_key, email=OPERATOR_EMAIL)},
    )

    assert response.status_code == 200
    assert sero.puts == [
        (
            OPERATOR_EMAIL,
            f"apps/{AGENT_NAME}/slack",
            {"SLACK_BOT_TOKEN": BOT_TOKEN, "SLACK_APP_TOKEN": APP_TOKEN},
        )
    ]
    assert sero.syncs == [
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


def test_connect_slack_with_nobody_to_record_the_write_against_is_a_409(
    session_factory__api_slack, config__api_slack
):
    # Not the retryable 502: retrying changes nothing, since there is no
    # verified caller and the agent has no creator on record either. The
    # refusal says so in one sentence naming only the agent.
    """Secrets handling: Connect slack with nobody to record the write against is a 409."""
    github = _CapturingGitHub__api_slack()
    sero = FakeSero()
    kubectl = FakeKubectl()
    gojira, app_id = _onboarded_gojira__api_slack()
    client = _make_client(
        session_factory__api_slack, config__api_slack, github=github, kubectl=kubectl, gojira=gojira, sero=sero
    )
    _seed_agent(
        session_factory__api_slack, config__api_slack, gojira_app_id=app_id, slack_app_id=APP_ID,
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
    assert sero.puts == []
    assert kubectl.secrets_applied == []
    assert github.pushes == []


def test_connect_slack_bad_app_id_returns_422_and_never_echoes_tokens(session_factory__api_slack, config__api_slack):
    """Secrets handling: Connect slack bad app id returns 422 and never echoes tokens."""
    client = _make_client(session_factory__api_slack, config__api_slack)
    _seed_agent(session_factory__api_slack, config__api_slack, slack_channels=[CHANNEL])
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


# --- from test_api_colleagues.py ---


ENDPOINT_TEMPLATE = "http://agent-{name}.default.svc.cluster.local"


@pytest.fixture
def session_factory__api_colleagues(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__api_colleagues(tmp_path):
    return Config(workdir=tmp_path / "work")


def _enabled_config(config__api_colleagues):
    return config__api_colleagues.model_copy(
        update={
            "cabal_url": "http://cabal.internal",
            "agent_endpoint_template": ENDPOINT_TEMPLATE,
        }
    )


def _make_client__api_colleagues(session_factory__api_colleagues, config__api_colleagues, *, cabal=None):
    return TestClient(
        create_app(
            session_factory__api_colleagues,
            config=config__api_colleagues,
            clients=Clients(
                github=FakeGitHub(),
                gojira=FakeGojira(),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=FakeKubectl(),
                cabal=cabal,
            ),
        )
    )


def _real_cabal_client(handler, token: str) -> CabalClient:
    # Same handler wired to both the sync and async transports - `stream_events`
    # (fix round 1) now goes through `async_client`, while every other method
    # here still goes through the plain sync `client`. `MockTransport` accepts
    # either a sync or async handler function and answers both `handle_request`
    # and `handle_async_request` from it, so one plain handler covers both.
    return CabalClient(
        "http://cabal.internal", token,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def test_operator_token_never_appears_in_list_502_response(session_factory__api_colleagues, config__api_colleagues, monkeypatch):
    """Secrets handling: Operator token never appears in list 502 response."""
    secret = "ultra-secret-operator-token"
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", secret)
    cfg = _enabled_config(config__api_colleagues)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = _make_client__api_colleagues(session_factory__api_colleagues, cfg, cabal=_real_cabal_client(handler, secret))

    response = client.get("/api/colleagues/conversations")

    assert response.status_code == 502
    assert secret not in response.text
    assert secret not in str(response.headers)


def test_operator_token_never_appears_in_stream_response_or_headers(
    session_factory__api_colleagues, config__api_colleagues, monkeypatch
):
    """Secrets handling: Operator token never appears in stream response or headers."""
    secret = "ultra-secret-operator-token"
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", secret)
    cfg = _enabled_config(config__api_colleagues)

    def handler(request: httpx.Request) -> httpx.Response:
        # Simulate an upstream failure (e.g. a stale/bad token) - the
        # stream route must still hand back a clean 200 with an
        # immediately-ended body, never surfacing the token anywhere.
        return httpx.Response(401, text=f"token was {secret}")

    client = _make_client__api_colleagues(session_factory__api_colleagues, cfg, cabal=_real_cabal_client(handler, secret))

    with client.stream("GET", "/api/colleagues/stream") as response:
        assert secret not in str(response.headers)
        body = b"".join(response.iter_bytes())

    assert secret.encode() not in body


# --- from test_api_people.py ---


@pytest.fixture
def session_factory__api_people(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__api_people(tmp_path):
    return Config(workdir=tmp_path / "work")


def _make_client__api_people(session_factory__api_people, config__api_people, *, people_search=None):
    return TestClient(
        create_app(
            session_factory__api_people,
            config=config__api_people,
            clients=Clients(
                github=FakeGitHub(),
                gojira=FakeGojira(),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=FakeKubectl(),
                people_search=people_search,
            ),
        )
    )


def test_okta_token_never_appears_in_the_response_on_failure(session_factory__api_people, config__api_people):
    """Secrets handling: Okta token never appears in the response on failure."""
    secret = "ultra-secret-okta-token"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f"token was {secret}")

    real_search = OktaPeopleSearch(
        "acme.okta.com", secret, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    client = _make_client__api_people(session_factory__api_people, config__api_people, people_search=real_search)

    response = client.get("/api/people/search", params={"q": "ali"})

    assert response.status_code == 200
    assert response.json() == {"people": [], "unavailable": True}
    assert secret not in response.text
    assert secret not in str(response.headers)


# --- from test_clients.py ---


class RecordingRunner:
    """Test double for CommandRunner: records exact argv (and, per Task 3,
    the exact `env` overlay) for every call, and replays canned (returncode,
    stdout, stderr) results in order (or reuses a single configured result
    for every call). Raises ClientError on rc != 0, exactly like the real
    CommandRunner, so real client branching logic (e.g. ensure_gsa's
    describe-then-create) exercises for real against it.
    """

    def __init__(self, results: list[tuple[int, str, str]] | None = None):
        self._results = list(results) if results is not None else [(0, "", "")]
        self.calls: list[list[str]] = []
        # Parallel to `calls`: the `env` kwarg each call was made with (None
        # when the caller passed none) — GhCLI's token_provider wiring is
        # the only thing exercising this today.
        self.envs: list[dict[str, str] | None] = []

    def run(
        self,
        args: list[str],
        stdin: str | None = None,
        *,
        redact_args: bool = False,
        env: dict[str, str] | None = None,
    ) -> str:
        self.calls.append(list(args))
        self.envs.append(env)
        if len(self._results) > 1:
            rc, out, err = self._results.pop(0)
        else:
            rc, out, err = self._results[0]
        if rc != 0:
            raise ClientError(
                f"{' '.join(args)} failed (rc={rc}): {err or out}",
                returncode=rc,
                stdout=out,
                stderr=err,
            )
        return out


SECRET_CONFIG_TOKEN = "xoxe.xoxp-TEST-config"


SECRET_REFRESH_TOKEN = "xoxe-TEST-refresh"


def _secret_get_json(**literals: str) -> str:
    data = {
        key: base64.b64encode(value.encode()).decode() for key, value in literals.items()
    }
    return json.dumps({"kind": "Secret", "metadata": {"name": "kobo-slack-tokens"}, "data": data})


SLACK_TOKENS_TEMPLATE = """\
# Slack app-configuration credentials for P2c (sandbox workspace).
# GITIGNORED (*.local.yaml) — never commit.
# Access token expires ~12h from issue; rotate BOTH via tooling.tokens.rotate.
slack_app_config_token: {config_token}
slack_app_config_refresh_token: {refresh_token}
"""


def _write_slack_tokens_file(tmp_path: Path, *, config_token: str, refresh_token: str) -> Path:
    path = tmp_path / "slack.local.yaml"
    path.write_text(
        SLACK_TOKENS_TEMPLATE.format(config_token=config_token, refresh_token=refresh_token)
    )
    return path


def test_run_env_never_appears_in_client_error_message():
    """Secrets handling: Run env never appears in client error message."""
    runner = CommandRunner()
    with pytest.raises(ClientError) as exc_info:
        runner.run([sys.executable, "-c", "import sys; sys.exit(1)"], env={"GH_TOKEN": "ghs_SECRETVALUE"})
    assert "ghs_SECRETVALUE" not in str(exc_info.value)


def test_ghcli_real_runner_failing_command_error_never_contains_the_token(caplog):
    # Same property, but against the REAL CommandRunner (real subprocess,
    # real env passing) rather than RecordingRunner — belt and suspenders on
    # the actual code path a deployed room runs.
    """Secrets handling: Ghcli real runner failing command error never contains the token."""
    runner = CommandRunner()
    client = GhCLI(
        runner=runner,
        git=sys.executable,
        gh=sys.executable,
        token_provider=lambda: "ghs_REALRUNNERSECRET",
    )
    with (
        caplog.at_level(logging.DEBUG, logger="kobo_api.clients"),
        pytest.raises(ClientError) as exc_info,
    ):
        # "git" here is sys.executable with no script — a real failure
        # (python prints its own usage/error and exits nonzero) with no
        # involvement from kobo's own code in choosing what to print.
        client.local_head(Path("/nonexistent/path/xyz"))
    assert "ghs_REALRUNNERSECRET" not in str(exc_info.value)
    assert "ghs_REALRUNNERSECRET" not in caplog.text


def test_secret_literals_failure_cannot_leak_stdout_into_the_raised_error():
    # CommandRunner.run's own ClientError message is `f"... failed (rc=N):
    # {stderr or stdout}"` and it carries stdout/stderr on the exception --
    # for THIS command, stdout is the Secret's (base64) values. A kubectl
    # that fails with an empty stderr after already printing part of the
    # JSON must not put any of it into the error, its attributes, or the
    # chained cause a traceback would print.
    """Secrets handling: Secret literals failure cannot leak stdout into the raised error."""
    leaky_stdout = _secret_get_json(
        config_token=SECRET_CONFIG_TOKEN, refresh_token=SECRET_REFRESH_TOKEN
    )
    encoded = base64.b64encode(SECRET_REFRESH_TOKEN.encode()).decode()
    runner = RecordingRunner(results=[(1, leaky_stdout, "")])
    client = KubectlCLI(runner=runner)

    with pytest.raises(ClientError) as exc_info:
        client.secret_literals("kobo", "kobo-slack-tokens")

    error = exc_info.value
    for leak in (leaky_stdout, encoded, SECRET_REFRESH_TOKEN, SECRET_CONFIG_TOKEN):
        assert leak not in str(error)
        assert leak not in error.stdout
        assert leak not in error.stderr
        assert leak not in str(error.__cause__ or "")
    # The original (whose message embeds that stdout) is still the implicit
    # context, as it is for any raise inside an except block -- what keeps
    # it out of a printed traceback is the `from None` suppression.
    assert error.__cause__ is None
    assert error.__suppress_context__ is True


def test_apply_secret_failure_cannot_leak_literals_through_the_traceback():
    # Stage 1's argv is the one place the literal values exist, and the
    # runner's own error message embeds that argv; its stdout is the
    # rendered manifest. Neither may reach the message, the attributes, or
    # a cause a traceback would print.
    """Secrets handling: Apply secret failure cannot leak literals through the traceback."""
    import traceback

    # Built at runtime: a traceback prints the source of the calling line, so
    # a value spelled out there would show up in `printed` on its own.
    parts = ["secret", "value"]
    value = "-".join(parts)
    encoded = base64.b64encode(value.encode()).decode()
    literals = {"TOKEN": value}
    runner = RecordingRunner(results=[(1, f"data:\n  TOKEN: {encoded}\n", "boom")])
    client = KubectlCLI(runner=runner)

    with pytest.raises(ClientError) as exc_info:
        client.apply_secret("kobo", "kobo-slack-tokens", literals)

    error = exc_info.value
    printed = "".join(traceback.format_exception(error))
    for leak in (value, encoded):
        assert leak not in str(error)
        assert leak not in error.stdout
        assert leak not in printed
    assert error.__cause__ is None
    assert error.__suppress_context__ is True


def test_write_slack_tokens_tmp_file_never_wider_than_0600_before_copymode(tmp_path, monkeypatch):
    # Quick-fold: the tmp file used to be created via plain write_text(),
    # which lands it at umask-default perms (e.g. 0644) for the window
    # between "content written" and "copymode fixes the mode up" — during
    # which it already holds the live refresh token. Spy on shutil.copymode
    # (called right after the content write, right before the atomic
    # replace) to catch the tmp file's mode at exactly that moment: it must
    # already be 0600, not merely corrected to it afterward.
    """Secrets handling: Write slack tokens tmp file never wider than 0600 before copymode."""
    tokens_path = _write_slack_tokens_file(
        tmp_path, config_token="old-config", refresh_token="old-refresh"
    )
    os.chmod(tokens_path, 0o644)  # an unhardened original, so 0600 can only come from the fix
    real_copymode = shutil.copymode
    captured: dict[str, int] = {}

    def spy_copymode(src, dst):
        captured["tmp_mode_before_copymode"] = os.stat(dst).st_mode & 0o777
        return real_copymode(src, dst)

    monkeypatch.setattr("kobo_api.clients.shutil.copymode", spy_copymode)

    _write_slack_tokens(tokens_path, "new-config", "new-refresh")

    assert captured["tmp_mode_before_copymode"] == 0o600


def test_slack_create_app_non_auth_error_raises_slack_api_error_without_token_material(tmp_path):
    """Secrets handling: Slack create app non auth error raises slack api error without token material."""
    tokens_path = _write_slack_tokens_file(
        tmp_path,
        config_token="xoxe.xoxp-1-super-secret-config",
        refresh_token="xoxe-1-super-secret-refresh",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "invalid_manifest"})

    client = HttpxSlackClient(
        tokens_path, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(SlackApiError) as exc_info:
        client.create_app({"display_information": {"name": "clio"}})

    assert isinstance(exc_info.value, ClientError)
    assert "invalid_manifest" in str(exc_info.value)
    assert "xoxe.xoxp-1-super-secret-config" not in str(exc_info.value)
    assert "xoxe-1-super-secret-refresh" not in str(exc_info.value)


def test_vertex_image_client_token_provider_failure_raises_client_error_without_leaking_gcloud_output():
    # A failing token_provider (the default shells `gcloud auth
    # print-access-token`, which raises ClientError carrying the full
    # command line + stderr — possibly the operator's account email) must
    # not escape generate() unwrapped: every failure mode raises the same
    # plain _IMAGE_GENERATION_FAILED message, never raw gcloud output.
    """Secrets handling: Vertex image client token provider failure raises client error without leaking gcloud output."""
    def failing_token_provider() -> str:
        raise ClientError(
            "gcloud auth print-access-token failed (rc=1): ERROR: account details"
        )

    client = VertexImageClient(
        "acme-project", "us-central1", "gemini-2.5-flash-image",
        http_client=httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={})
        )),
        token_provider=failing_token_provider,
    )

    with pytest.raises(ClientError) as exc_info:
        client.generate("a prompt")

    assert str(exc_info.value) == "the picture didn't come through. Try again."
    assert "gcloud" not in str(exc_info.value)
    assert "account details" not in str(exc_info.value)


def test_cabal_mint_colleague_error_message_never_contains_the_operator_token():
    """Secrets handling: Cabal mint colleague error message never contains the operator token."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal failure detail")

    client = CabalClient(
        "http://cabal.local", "super-secret-operator-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(CabalError) as exc_info:
        client.mint_colleague("clio")
    assert "super-secret-operator-token" not in str(exc_info.value)
    # Same stricter convention graduate/relay already pin: never the
    # response body either (it's logged server-side, truncated, instead).
    assert "internal failure detail" not in str(exc_info.value)


def test_cabal_mint_colleague_returned_token_never_appears_in_a_debug_log(caplog):
    """Secrets handling: Cabal mint colleague returned token never appears in a debug log.

    The minted token is a brand-new secret this call exists to hand back
    — it must never end up in a log line, at any level, the same way the
    operator token this client already holds never does."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"access_token": "ultra-secret-minted-colleague-token"}
        )

    client = CabalClient(
        "http://cabal.local", "op-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with caplog.at_level(logging.DEBUG, logger="kobo_api.clients"):
        token = client.mint_colleague("clio")

    assert token == "ultra-secret-minted-colleague-token"
    assert "ultra-secret-minted-colleague-token" not in caplog.text


# --- from test_github_app.py ---


APP_ID__github_app = "app-123"


INSTALLATION_ID = "installation-456"


def _generate_rsa_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _write_pem(tmp_path, private_key, name: str = "app.pem"):
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path = tmp_path / name
    path.write_bytes(pem)
    return path


def _recording_transport(captured: list, responses):
    """A MockTransport handler that appends every request to `captured` and
    replays `responses[0]`, `responses[1]`, ... in order (repeating the last
    for any call past the end)."""

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        idx = min(len(captured) - 1, len(responses) - 1)
        return responses[idx]

    return handler


def test_non_201_error_message_never_contains_the_app_jwt_or_any_token(tmp_path):
    """Secrets handling: Non 201 error message never contains the app jwt or any token."""
    private_key, _ = _generate_rsa_keypair()
    key_path = _write_pem(tmp_path, private_key)
    captured: list[httpx.Request] = []
    response = httpx.Response(403, text="Resource not accessible by integration")
    http_client = httpx.Client(
        transport=httpx.MockTransport(_recording_transport(captured, [response]))
    )
    provider = InstallationTokenProvider(
        APP_ID__github_app, INSTALLATION_ID, key_path, http_client=http_client
    )

    with pytest.raises(ClientError) as exc_info:
        provider.token()

    app_jwt = captured[0].headers["authorization"].removeprefix("Bearer ")
    assert app_jwt not in str(exc_info.value)


# --- from test_groups.py ---


DOMAIN = "acme.okta.com"


TOKEN = "SUPER_SECRET_OKTA_TOKEN_9f3a"


UNIVERSE = {"SRE": ["okta-sre-group"], "QA": ["okta-qa-group"]}


def _client__groups(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _handler_401(request: httpx.Request) -> httpx.Response:
    return httpx.Response(401, text="unauthorized")


def _handler_403(request: httpx.Request) -> httpx.Response:
    return httpx.Response(403, text="forbidden")


def _handler_500(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, text="internal error")


def _handler_timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.TimeoutException("timed out", request=request)


def _handler_network_error(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


def _okta_user(first: str | None, last: str | None, email: str | None) -> dict:
    """One Okta user object shaped like the real API's response -- profile
    fields at `profile.*`, plus a couple of fields a real user object
    carries that the search must simply ignore (login, status)."""
    profile: dict = {}
    if first is not None:
        profile["firstName"] = first
    if last is not None:
        profile["lastName"] = last
    if email is not None:
        profile["email"] = email
        profile["login"] = email
    return {"id": "00u1", "status": "ACTIVE", "profile": profile}


@pytest.mark.parametrize(
    "handler",
    [_handler_401, _handler_403, _handler_500, _handler_timeout, _handler_network_error],
    ids=["401", "403", "500", "timeout", "network-error"],
)
def test_every_error_path_never_raises_and_never_logs_the_token(caplog, handler):
    """Secrets handling: Every error path never raises and never logs the token."""
    resolver = OktaGroupResolver(DOMAIN, TOKEN, UNIVERSE, client=_client__groups(handler))

    with caplog.at_level(logging.DEBUG, logger="kobo_api.groups"):
        result = resolver.groups_for("alice@example.com")

    assert result is None
    assert TOKEN not in caplog.text
    for record in caplog.records:
        assert TOKEN not in record.getMessage()


def test_search_caps_result_to_name_and_email_only():
    """Secrets handling: Search caps result to name and email only."""
    response = httpx.Response(200, json=[_okta_user("Alice", "Smith", "alice@example.com")])
    searcher = OktaPeopleSearch(DOMAIN, TOKEN, client=_client__groups(lambda r: response))

    result = searcher.search("ali")

    assert result == [{"name": "Alice Smith", "email": "alice@example.com"}]


# --- from test_mcp_audit.py ---


EMAIL__mcp_audit = "person@example.com"


@pytest.fixture(autouse=True)
def _reset_rate_limit_and_audit(monkeypatch):
    """Every test starts with a clean sliding window and an unconfigured
    audit layer, so one test's calls/rows never bleed into the next."""
    rate_limit.reset()
    monkeypatch.setattr(audit, "_session_factory", None)
    yield
    rate_limit.reset()


class _StubAccessToken:
    def __init__(self, user_email):
        self.user_email = user_email


class _StubUser:
    def __init__(self, user_email):
        self.access_token = _StubAccessToken(user_email)


class _StubRequest:
    def __init__(self, user_email):
        self.user = _StubUser(user_email)


class _StubRequestContext:
    def __init__(self, user_email):
        self.request = _StubRequest(user_email)


class _StubCtx:
    """A stand-in for the real MCP `ctx` argument, shaped exactly like the
    design spec's `ctx.request_context.request.user.access_token.
    user_email` chain."""

    def __init__(self, user_email):
        self.request_context = _StubRequestContext(user_email)


def _ctx(email=EMAIL):
    return _StubCtx(email)


def test_redact_replaces_exactly_the_sensitive_keys():
    """Secrets handling: Redact replaces exactly the sensitive keys."""
    kwargs = {
        "note": "hello",
        "message": "hi",
        "bot_token": "xoxb-1",
        "app_token": "xapp-1",
        "secret": "s3cr3t",
        "token": "abc",
        "value": "v",
        "secret_values": {"a": "b"},
        "name": "my-agent",
        "limit": 50,
        "agent": "kobo-1",
    }

    redacted = audit._redact(kwargs)

    for key in (
        "note",
        "message",
        "bot_token",
        "app_token",
        "secret",
        "token",
        "value",
        "secret_values",
    ):
        assert redacted[key] == "***REDACTED***"
    assert redacted["name"] == "my-agent"
    assert redacted["limit"] == 50
    assert redacted["agent"] == "kobo-1"


def test_audited_ok_call_redacts_the_logged_kwargs(session_factory):
    """Secrets handling: Audited ok call redacts the logged kwargs."""
    audit.configure(session_factory)

    @audit.audited("kobo_teach")
    async def tool(ctx, agent, note):
        return "done"

    asyncio.run(tool(_ctx(), agent="kobo-1", note="a secret note"))

    session = session_factory()
    try:
        row = session.query(MCPToolCallLog).one()
    finally:
        session.close()

    logged = json.loads(row.redacted_args)
    assert logged["agent"] == "kobo-1"
    assert logged["note"] == "***REDACTED***"


# --- from test_mcp_tools_write.py ---


PERSONA = "helpful-generalist"


REPO_TEMPLATE = "acme/agent-{name}"


GROUP_A = "growth"


GROUP_B = "design-team"


AUDIENCE__mcp_tools_write = "test-iap-audience"


CLIENT_ID = "test-mcp-client"


WORKSPACE_DOMAIN = "example.com"


ISSUER_BASE_URL = "https://kobo.example.org"


SKILLS_BUCKET = "skills-bucket"


PERSONAS_BUCKET = "personas-bucket"


OWNER_A_EMAIL = "owner-a@example.com"


OWNER_B_EMAIL = "owner-b@example.com"


MEMBER_A_EMAIL = "member-a@example.com"


OPERATOR_EMAIL__mcp_tools_write = "ops@example.com"


BOT_TOKEN = "xoxb-super-secret-bot-token"


APP_TOKEN = "xapp-super-secret-app-token"


APP_ID = "A0EXISTING"


CHANNEL = "C1234567"


_AGENT_YAML_TRANSPORTS = [{"kind": "a2a", "host": "0.0.0.0", "port": 8080}]


@pytest.fixture(autouse=True)
def _no_real_sleep__mcp_tools_write(monkeypatch):
    """kobo_create_agent's pipeline (OnboardStep/WireStep/BuildStep) and
    kobo_wire_slack's wait_active both poll in a loop -- never let either
    sleep for real (same discipline as test_api_agents.py/test_api_slack.py's
    own copies of this fixture)."""
    import kobo_api.slack_setup as slack_setup_module
    import kobo_api.steps as steps_module

    monkeypatch.setattr(steps_module, "_sleep", lambda seconds: None)
    monkeypatch.setattr(slack_setup_module, "_sleep", lambda seconds: None)


@pytest.fixture(autouse=True)
def _reset_rate_limit_and_audit__mcp_tools_write():
    rate_limit.reset()
    audit.configure(None)
    yield
    rate_limit.reset()
    audit.configure(None)


@pytest.fixture
def session_factory__mcp_tools_write(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__mcp_tools_write(tmp_path):
    return Config(
        workdir=tmp_path / "work",
        operators=[OPERATOR_EMAIL__mcp_tools_write],
        groups=[
            GroupChoice(name=GROUP_A, okta_group="okta-growth"),
            GroupChoice(name=GROUP_B, okta_group="okta-design-team"),
        ],
        iap_audience=AUDIENCE__mcp_tools_write,
        skills_bucket=SKILLS_BUCKET,
        personas_bucket=PERSONAS_BUCKET,
        mcp=MCPConfig(
            allow_without_iap=True, oauth_client_id=CLIENT_ID,
            workspace_domain=WORKSPACE_DOMAIN,
            issuer_base_url=ISSUER_BASE_URL,
        ),
    )


@pytest.fixture
def fake_storage__mcp_tools_write():
    """Seeded the same way conftest.py's own `fake_storage` is -- kept as a
    local copy (not imported) so this file's `config` fixture, which needs
    the MCP block conftest's own `config` doesn't carry, isn't forced to
    also match conftest's bucket-name constants by coincidence."""
    storage = FakeStorage()
    seed_default_personas(storage, bucket=PERSONAS_BUCKET)
    return storage


class FakeGroupResolver:
    """Mapping-driven, or always-`None` ("the directory is down") when
    `down=True` -- same seam `test_mcp_tools_read.py`'s own copy of this
    fake uses for `kobo_whoami`'s `groups_unavailable` case, reused here
    for `kobo_create_skill`/`kobo_create_tool`'s membership gate (MCP
    marketplace wave, Task 5)."""

    def __init__(self, mapping: dict[str, list[str]] | None = None, *, down: bool = False):
        self._mapping = dict(mapping) if mapping else {}
        self._down = down

    def groups_for(self, email: str) -> list[str] | None:
        if self._down:
            return None
        return self._mapping.get(email.lower(), [])


def _resolver():
    return FakeGroupResolver(
        {
            MEMBER_A_EMAIL: [GROUP_A],
            OWNER_A_EMAIL: [GROUP_A],
            OWNER_B_EMAIL: [GROUP_B],
        }
    )


def _app(
    session_factory__mcp_tools_write,
    config__mcp_tools_write,
    *,
    groups_resolver=None,
    storage=None,
    github=None,
    gojira=None,
    agent=None,
    slack=None,
    cabal=None,
    name=None,
    sero=None,
    kubectl=None,
):
    clients = Clients(
        github=github if github is not None else FakeGitHub(),
        gojira=gojira if gojira is not None else FakeGojira(),
        cloud=FakeCloud(),
        agent=agent if agent is not None else FakeAgent(),
        kubectl=kubectl if kubectl is not None else FakeKubectl(),
        groups=groups_resolver,
        storage=storage,
        slack=slack,
        cabal=cabal,
        name=name,
        sero=sero,
    )
    return create_app(session_factory__mcp_tools_write, config=config__mcp_tools_write, clients=clients)


def _seed_agent__mcp_tools_write(
    session_factory__mcp_tools_write,
    *,
    name,
    created_by=None,
    group_name=None,
    team=None,
    status=AgentStatus.READY,
    repo_full_name=None,
    gojira_app_id=None,
    slack_app_id=None,
    slack_channels=None,
    slack_wired=False,
    skills=None,
    kind=KIND_COMPOSED,
):
    session = session_factory__mcp_tools_write()
    session.add(
        Agent(
            name=name,
            persona=PERSONA,
            model_id="vertex_ai/claude-sonnet-4-5",
            status=status,
            repo_full_name=repo_full_name or REPO_TEMPLATE.format(name=name),
            created_by=created_by,
            group_name=group_name,
            team=team,
            gojira_app_id=gojira_app_id,
            slack_app_id=slack_app_id,
            slack_channels=encode_channels(slack_channels) if slack_channels is not None else None,
            slack_wired=slack_wired,
            skills=skills if skills is not None else [],
            kind=kind,
        )
    )
    session.commit()
    session.close()


def _onboarded_gojira__mcp_tools_write(name, repo, **kwargs):
    kwargs.setdefault("activate_after", 1)
    gojira = FakeGojira(**kwargs)
    app_id = gojira.create_app(
        name=name, repo_full_name=repo, cluster="example-cluster", namespace=name
    )
    return gojira, app_id


class _StubAccessToken:
    def __init__(self, user_email):
        self.user_email = user_email


class _StubUser:
    def __init__(self, user_email):
        self.access_token = _StubAccessToken(user_email)


class _StubRequest:
    def __init__(self, user_email):
        self.user = _StubUser(user_email)


def _ctx_for(email):
    request_context = RequestContext(
        request_id="test-request",
        meta=None,
        session=None,
        lifespan_context=None,
        request=_StubRequest(email),
    )
    return Context(request_context=request_context)


def _call_tool(mcp, tool_name, ctx, **arguments):
    return asyncio.run(mcp._tool_manager.call_tool(tool_name, arguments, context=ctx))


def _last_audit_row(session_factory__mcp_tools_write) -> MCPToolCallLog:
    session = session_factory__mcp_tools_write()
    try:
        return session.query(MCPToolCallLog).order_by(MCPToolCallLog.id.desc()).first()
    finally:
        session.close()


SECRETS_AGENT = "clio"


SECRETS_REPO = REPO_TEMPLATE.format(name=SECRETS_AGENT)


# Never a real credential: a placeholder standing in for one, so a test can
# prove it reaches the lane and appears in no tool result or audit row.
SECRET_VALUE__mcp_tools_write = "not-a-real-key-value"


SECRETS_GOJIRA_YAML = "name: clio\nservice_account: agent-clio\n"


def _secrets_app(
    session_factory__mcp_tools_write, config__mcp_tools_write, fake_storage__mcp_tools_write, *, sero=None, github=None, kubectl=None
):
    return _app(
        session_factory__mcp_tools_write,
        config__mcp_tools_write,
        groups_resolver=_resolver(),
        storage=fake_storage__mcp_tools_write,
        sero=sero,
        kubectl=kubectl,
        github=github
        or FakeGitHub(
            existing_repos={SECRETS_REPO},
            repo_files={"gojira.yaml": SECRETS_GOJIRA_YAML},
            extra_clone_files={"gojira.yaml": SECRETS_GOJIRA_YAML},
        ),
    )


def _seed_secrets_agent(session_factory__mcp_tools_write, *, created_by=MEMBER_A_EMAIL):
    _seed_agent__mcp_tools_write(
        session_factory__mcp_tools_write,
        name=SECRETS_AGENT,
        created_by=created_by,
        group_name=GROUP_A,
        repo_full_name=SECRETS_REPO,
    )


def test_wire_slack_happy_path_wires_tokens_and_never_returns_them(
    session_factory__mcp_tools_write, config__mcp_tools_write, fake_storage__mcp_tools_write
):
    """Secrets handling: Wire slack happy path wires tokens and never returns them."""
    fake_github = FakeGitHub(agent_yaml_transports=_AGENT_YAML_TRANSPORTS)
    fake_gojira, app_id = _onboarded_gojira__mcp_tools_write("ward", REPO_TEMPLATE.format(name="ward"))
    _seed_agent__mcp_tools_write(
        session_factory__mcp_tools_write,
        name="ward",
        created_by=OWNER_A_EMAIL,
        group_name=GROUP_A,
        gojira_app_id=app_id,
        slack_app_id=APP_ID,
    )
    app = _app(
        session_factory__mcp_tools_write,
        config__mcp_tools_write,
        groups_resolver=_resolver(),
        storage=fake_storage__mcp_tools_write,
        github=fake_github,
        gojira=fake_gojira,
        slack=FakeSlack(),
    )
    mcp = build_mcp_server(app)

    result = _call_tool(
        mcp,
        "kobo_wire_slack",
        _ctx_for(OWNER_A_EMAIL),
        agent="ward",
        bot_token=BOT_TOKEN,
        app_token=APP_TOKEN,
        channel_id=CHANNEL,
    )

    assert result["connected"] is True
    assert BOT_TOKEN not in json.dumps(result)
    assert APP_TOKEN not in json.dumps(result)
    session = session_factory__mcp_tools_write()
    try:
        agent = session.query(Agent).filter_by(name="ward").one()
        assert agent.slack_wired is True
    finally:
        session.close()


def test_wire_slack_audit_row_redacts_both_tokens_but_not_the_channel(
    session_factory__mcp_tools_write, config__mcp_tools_write, fake_storage__mcp_tools_write
):
    """Secrets handling: Wire slack audit row redacts both tokens but not the channel."""
    fake_github = FakeGitHub(agent_yaml_transports=_AGENT_YAML_TRANSPORTS)
    fake_gojira, app_id = _onboarded_gojira__mcp_tools_write("ward", REPO_TEMPLATE.format(name="ward"))
    _seed_agent__mcp_tools_write(
        session_factory__mcp_tools_write,
        name="ward",
        created_by=OWNER_A_EMAIL,
        group_name=GROUP_A,
        gojira_app_id=app_id,
        slack_app_id=APP_ID,
    )
    app = _app(
        session_factory__mcp_tools_write,
        config__mcp_tools_write,
        groups_resolver=_resolver(),
        storage=fake_storage__mcp_tools_write,
        github=fake_github,
        gojira=fake_gojira,
        slack=FakeSlack(),
    )
    mcp = build_mcp_server(app)

    _call_tool(
        mcp,
        "kobo_wire_slack",
        _ctx_for(OWNER_A_EMAIL),
        agent="ward",
        bot_token=BOT_TOKEN,
        app_token=APP_TOKEN,
        channel_id=CHANNEL,
    )

    row = _last_audit_row(session_factory__mcp_tools_write)
    assert row is not None
    assert row.tool == "kobo_wire_slack"
    assert row.status == "ok"
    logged = json.loads(row.redacted_args)
    assert logged["bot_token"] == "***REDACTED***"
    assert logged["app_token"] == "***REDACTED***"
    assert logged["channel_id"] == CHANNEL
    assert BOT_TOKEN not in row.redacted_args
    assert APP_TOKEN not in row.redacted_args


def test_list_agent_secrets_answers_names_and_never_a_value(
    session_factory__mcp_tools_write, config__mcp_tools_write, fake_storage__mcp_tools_write
):
    """Secrets handling: List agent secrets answers names and never a value."""
    sero = FakeSero()
    sero.stored[f"apps/{SECRETS_AGENT}/env"] = {"API_KEY": SECRET_VALUE__mcp_tools_write}
    _seed_secrets_agent(session_factory__mcp_tools_write)
    mcp = build_mcp_server(_secrets_app(session_factory__mcp_tools_write, config__mcp_tools_write, fake_storage__mcp_tools_write, sero=sero))

    result = _call_tool(
        mcp, "kobo_list_agent_secrets", _ctx_for(MEMBER_A_EMAIL), agent=SECRETS_AGENT
    )

    assert result["secrets"] == ["API_KEY"]
    assert result["secrets_available"] is True
    assert result["delivered_as"] == f"{SECRETS_AGENT}-env"
    assert SECRET_VALUE__mcp_tools_write not in json.dumps(result)


def test_set_agent_secrets_never_puts_a_value_in_the_audit_row(
    session_factory__mcp_tools_write, config__mcp_tools_write, fake_storage__mcp_tools_write
):
    """Secrets handling: Set agent secrets never puts a value in the audit row."""
    _seed_secrets_agent(session_factory__mcp_tools_write)
    mcp = build_mcp_server(
        _secrets_app(session_factory__mcp_tools_write, config__mcp_tools_write, fake_storage__mcp_tools_write, sero=FakeSero())
    )

    _call_tool(
        mcp,
        "kobo_set_agent_secrets",
        _ctx_for(MEMBER_A_EMAIL),
        agent=SECRETS_AGENT,
        secret_values={"API_KEY": SECRET_VALUE__mcp_tools_write},
    )

    row = _last_audit_row(session_factory__mcp_tools_write)
    assert row.tool == "kobo_set_agent_secrets"
    # `set` matches the audit layer's own "secret"/"value" substring rule,
    # so the whole mapping is redacted before the row is ever written.
    assert SECRET_VALUE__mcp_tools_write not in row.redacted_args
    assert "REDACTED" in row.redacted_args


def test_a_bad_secret_value_is_refused_by_field_name_and_never_echoed(
    session_factory__mcp_tools_write, config__mcp_tools_write, fake_storage__mcp_tools_write
):
    # Pydantic renders the offending INPUT in its own message
    # (`input_value=...`). A tool argument here can be a credential, so
    # the refusal is built from the field names that failed and nothing
    # else -- in the answer AND in the audit row.
    """Secrets handling: A bad secret value is refused by field name and never echoed."""
    _seed_secrets_agent(session_factory__mcp_tools_write)
    mcp = build_mcp_server(
        _secrets_app(session_factory__mcp_tools_write, config__mcp_tools_write, fake_storage__mcp_tools_write, sero=FakeSero())
    )

    with pytest.raises(ToolError) as exc:
        _call_tool(
            mcp,
            "kobo_set_agent_secrets",
            _ctx_for(MEMBER_A_EMAIL),
            agent=SECRETS_AGENT,
            secret_values={"API_KEY": {"not": SECRET_VALUE__mcp_tools_write}},
        )

    assert SECRET_VALUE__mcp_tools_write not in str(exc.value)
    assert "input_value" not in str(exc.value)
    assert "set" in str(exc.value)
    row = _last_audit_row(session_factory__mcp_tools_write)
    assert SECRET_VALUE__mcp_tools_write not in (row.error or "")
    assert SECRET_VALUE__mcp_tools_write not in row.redacted_args


# --- from test_api_org_status.py ---


WHOAMI_AUDIENCE = "test-audience"


# --- from test_api_org_status.py ---


def _fetcher__api_org_status(jwk):
    def fetch():
        return {"keys": [jwk]}

    return fetch


# --- from test_api_org_status.py ---


WHOAMI_KID = "test-key-1"


def _generate_jwk__api_org_status(kid: str = WHOAMI_KID):
    private_key = ec.generate_private_key(ec.SECP256R1())
    jwk = jwt.algorithms.ECAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk["kid"] = kid
    jwk["use"] = "sig"
    jwk["alg"] = "ES256"
    return private_key, jwk


# --- from test_api_org_status.py ---


def _sign_whoami(private_key, *, email: str | None, kid: str = WHOAMI_KID) -> str:
    now = int(time.time())
    claims = {
        "aud": WHOAMI_AUDIENCE,
        "iss": IAP_ISSUER,
        "iat": now,
        "exp": now + 3600,
        "sub": "accounts.google.com:1234567890",
    }
    if email is not None:
        claims["email"] = email
    return jwt.encode(claims, private_key, algorithm="ES256", headers={"kid": kid})


# --- from test_api_room_services.py ---


SLACK_PAIR = {"config_token": "xoxe.xoxp-1-room-config", "refresh_token": "xoxe-1-room-refresh"}


GITHUB_APP = {"app_id": "4741722", "installation_id": "157070479", "private_key": "-----BEGIN RSA PRIVATE KEY-----\nroom\n-----END RSA PRIVATE KEY-----"}


def _client__api_room_services(tmp_path, config: Config, *, kubectl: FakeKubectl | None, namespace: str | None = "kobo"):
    engine = create_engine(f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    private_key, jwk = _generate_jwk__api_org_status()
    kubectl = kubectl or FakeKubectl()
    services = RoomServices(kubectl, namespace, config) if namespace else None
    client = TestClient(
        create_app(
            session_factory,
            config=config,
            clients=Clients(
                github=FakeGitHub(), gojira=FakeGojira(), cloud=FakeCloud(), agent=FakeAgent(),
                kubectl=kubectl, slack=None, room_services=services,
            ),
            iap_jwks_fetcher=_fetcher__api_org_status(jwk),
        )
    )
    return client, kubectl, (lambda email="ops@example.com": {ASSERTION_HEADER: _sign_whoami(private_key, email=email)})


def _config(tmp_path, **overrides) -> Config:
    values = {"workdir": tmp_path / "work", "iap_audience": WHOAMI_AUDIENCE, "operators": ["ops@example.com"], "admin_panel": True}
    values.update(overrides)
    return Config(**values)


def test_values_are_stored_in_the_rooms_secret_and_never_echoed(tmp_path):
    """Secrets handling: Values are stored in the rooms secret and never echoed."""
    client, kubectl, headers = _client__api_room_services(tmp_path, _config(tmp_path), kubectl=None)

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


# --- from test_redact.py ---


def test_vendor_token_shapes_are_redacted():
    """Secrets handling: Vendor token shapes are redacted."""
    text = (
        "pushing with ghp_AbCdEfGhIjKlMnOpQrStUvWxYz012345 done\n"
        "slack xoxb-1234567890-abcdefghij ok\n"
        "aws AKIAABCDEFGHIJKLMNOP\n"
        "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhZ2VudCJ9.c2lnbmF0dXJlLXNpZ25hdHVyZQ\n"
    )
    out = redact_secrets(text)
    assert "ghp_" not in out and "xoxb-" not in out and "AKIA" not in out and "eyJ" not in out
    assert out.count(REDACTED) == 4
    assert out.startswith("pushing with [redacted] done")


def test_private_key_blocks_go_as_one():
    """Secrets handling: Private key blocks go as one."""
    out = redact_secrets("before\n-----BEGIN RSA PRIVATE KEY-----\nMIIE\nxyz\n-----END RSA PRIVATE KEY-----\nafter")
    assert out == f"before\n{REDACTED}\nafter"


# --- from test_org.py ---


ORG_REPO = "acme-corp/kobo-config"


def test_composite_override_is_disclosed_without_dumping_the_value(caplog):
    """Secrets handling: Composite override is disclosed without dumping the value.

    A differing composite (list/dict) key is disclosed by shape, not by
    value — `groups` or `models` dumped verbatim would drown the line that
    matters and could echo operator emails into logs."""
    import logging

    local = Config(org_repo=ORG_REPO, operators=["a@acme.test"])
    github = FakeGitHub(org_yaml="operators: [a@acme.test, b@acme.test]\n")

    with caplog.at_level(logging.WARNING, logger="kobo_api.org"):
        _merged, org = load_org_config(local, github)

    assert org.wired is True
    disclosures = [
        r.getMessage() for r in caplog.records if "org.yaml overrides" in r.getMessage()
    ]
    assert len(disclosures) == 1
    assert "operators" in disclosures[0]
    assert "a@acme.test" not in disclosures[0]
    assert "list with 1" in disclosures[0]
    assert "list with 2" in disclosures[0]


# --- from test_config.py ---


def test_slack_tokens_secret_is_not_org_settable():
    # Deployment-local, exactly like slack_tokens_path: it names a Secret in
    # THIS cluster's namespace, not anything the org hands out.
    """Secrets handling: Slack tokens secret is not org settable."""
    from kobo_api.org import ORG_KEYS

    assert "slack_tokens_secret" not in ORG_KEYS
    assert "slack_tokens_path" not in ORG_KEYS



# --- the room's audit log (issue #316) ---


def test_audit_detail_never_stores_a_key_named_like_a_secret(tmp_path):
    """Secrets handling: a detail handed to the audit log drops the VALUE of
    every key named token, secret, key or password, at any depth, and clips
    the rest, so a pasted credential never reaches the audit table."""
    engine = create_engine(f"sqlite:///{tmp_path}/kobo.db")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        room_audit.record(
            session, action="slack.connect", target_kind="agent", target_id="ward", agent_name="ward",
            summary="someone connected ward to Slack", actor="someone@example.com",
            detail={
                "bot_token": "xoxb-live-value", "app_token": "xapp-live-value", "config": {"client_secret": "shh", "api_key": "k"},
                "password": "pw", "signing_key": "sk", "channels": ["#ops"], "note": "x" * 1000,
            },
        )
        session.commit()
        [row] = session.query(room_audit.AuditLog).all()
    flat = json.dumps(row.detail)
    for live in ("xoxb-live-value", "xapp-live-value", "shh", '"k"', '"pw"', '"sk"'):
        assert live not in flat
    assert row.detail["channels"] == ["#ops"]
    assert len(row.detail["note"]) <= room_audit.MAX_VALUE_CHARS
