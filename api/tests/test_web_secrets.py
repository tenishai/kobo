"""The Secrets and environment routes (design 2026-09-22).

Four routes over one panel: the secrets half, held on the secrets
platform's machine lane and delivered into the agent's namespace, and the
environment half, held in the agent's own `gojira.yaml`.

What every case here is really checking is one of three promises:

- A VALUE never comes back. The secrets read answers key names, the
  secrets write answers key names, and no response, error or log line the
  room produces carries what was sent.
- The room never pushes to a repository it does not own. A composed
  agent's change is committed; a connected agent's is proposed, and the
  pull request's link is what the panel shows.
- A retried save answers what the first one did. Every write here is a
  merge plus an append-if-missing, so nothing about running it twice is
  different from running it once.
"""

from __future__ import annotations

import time

import jwt
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.agent_env import ENV_PR_ALREADY_OPEN, ROOM_SETS_THIS_ONE
from kobo_api.clients import Clients
from kobo_api.config import Config
from kobo_api.iap import ASSERTION_HEADER, IAP_ISSUER
from kobo_api.kinds import KIND_COMPOSED, KIND_CONNECTED
from kobo_api.main import NO_SECRETS_PLATFORM, create_app
from kobo_api.models import Agent, AgentStatus, Base
from tests.fakes import (
    FakeAgent,
    FakeCabal,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeImage,
    FakeKubectl,
    FakeLadon,
    FakeSlack,
)

AGENT = "clio"
REPO = "example-org/agent-clio"
OWNER = "owner@example.com"
STRANGER = "someone-else@example.com"
PR_URL = "https://example.test/pull/7"

# Never a real credential: a placeholder standing in for one, so a test can
# prove it reaches the lane and appears in no response, error or log.
VALUE = "not-a-real-key-value"

BASE_GOJIRA_YAML = "name: clio\nservice_account: agent-clio\n"

AUDIENCE = "test-audience"
KID = "test-key-1"


# The room's real perimeter, signed for real, because a file about who may
# read and write an agent's secrets cannot turn the gate off to test it:
# with `iap_audience` empty every owner-or-operator gate short-circuits to
# allowed. Same idiom as test_web_ownership_gate.py.


@pytest.fixture(scope="module")
def _key():
    private_key = ec.generate_private_key(ec.SECP256R1())
    jwk = jwt.algorithms.ECAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk.update({"kid": KID, "use": "sig", "alg": "ES256"})
    return private_key, jwk


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config(tmp_path, fake_storage):
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
    session_factory,
    config,
    fake_storage,
    key,
    *,
    kind: str = KIND_COMPOSED,
    ladon: FakeLadon | None = None,
    github: FakeGitHub | None = None,
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
        kubectl=FakeKubectl(),
        storage=fake_storage,
        slack=FakeSlack(),
        cabal=FakeCabal(),
        image=FakeImage(),
        ladon=ladon,
    )

    session = session_factory()
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
        session_factory,
        config=config,
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


# --- Reading the secrets list -------------------------------------------------


def test_the_secrets_list_answers_key_names_and_nothing_else(
    session_factory, config, fake_storage, _key
):
    ladon = FakeLadon()
    ladon.stored[f"apps/{AGENT}/env"] = {"API_KEY": VALUE, "DB_URL": VALUE}
    client, _ = _client(session_factory, config, fake_storage, _key, ladon=ladon)

    resp = client.get(f"/api/agents/{AGENT}/secrets", headers=_as(_key))

    assert resp.status_code == 200
    body = resp.json()
    assert body["keys"] == ["API_KEY", "DB_URL"]
    assert body["platform"] is True
    assert body["secret_name"] == f"{AGENT}-env"
    assert VALUE not in resp.text


def test_an_agent_with_no_secrets_yet_reads_as_an_empty_list(
    session_factory, config, fake_storage, _key
):
    client, _ = _client(session_factory, config, fake_storage, _key, ladon=FakeLadon())

    body = client.get(f"/api/agents/{AGENT}/secrets", headers=_as(_key)).json()

    assert body["keys"] == []
    assert body["updated_at"] is None
    assert body["platform"] is True


def test_a_room_with_no_secrets_platform_says_so_rather_than_failing(
    session_factory, config, fake_storage, _key
):
    client, _ = _client(session_factory, config, fake_storage, _key, ladon=None)

    resp = client.get(f"/api/agents/{AGENT}/secrets", headers=_as(_key))

    assert resp.status_code == 200
    assert resp.json() == {
        "keys": [],
        "updated_at": None,
        "platform": False,
        "secret_name": None,
    }


def test_a_stranger_cannot_read_which_secrets_an_agent_holds(
    session_factory, config, fake_storage, _key
):
    client, _ = _client(session_factory, config, fake_storage, _key, ladon=FakeLadon())

    resp = client.get(f"/api/agents/{AGENT}/secrets", headers=_as(_key, STRANGER))

    assert resp.status_code == 403


def test_the_secrets_list_is_not_offered_before_the_agent_is_ready(
    session_factory, config, fake_storage, _key
):
    client, _ = _client(
        session_factory,
        config,
        fake_storage,
        _key,
        ladon=FakeLadon(),
        status=AgentStatus.PROVISIONING,
    )

    resp = client.get(f"/api/agents/{AGENT}/secrets", headers=_as(_key))

    assert resp.status_code == 409


# --- Writing secrets ----------------------------------------------------------


def test_the_first_secret_is_stored_synced_and_wired_into_the_agents_files(
    session_factory, config, fake_storage, _key
):
    ladon = FakeLadon()
    github = _github()
    client, _ = _client(session_factory, config, fake_storage, _key, ladon=ladon, github=github)

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
    assert ladon.puts == [(OWNER, f"apps/{AGENT}/env", {"API_KEY": VALUE})]
    assert ladon.syncs == [
        (OWNER, AGENT, f"apps/{AGENT}/env", AGENT, f"{AGENT}-env")
    ]
    assert VALUE not in resp.text
    # And the file now names the Secret the platform delivers into.
    pushed = yaml.safe_load(github.pushed_file("gojira.yaml"))
    assert pushed["secrets_from"] == [f"{AGENT}-env"]


def test_a_second_secret_stores_but_does_not_wire_a_second_time(
    session_factory, config, fake_storage, _key
):
    ladon = FakeLadon()
    github = _github(
        repo_files={
            "gojira.yaml": BASE_GOJIRA_YAML + f"secrets_from:\n  - {AGENT}-env\n"
        }
    )
    ladon.stored[f"apps/{AGENT}/env"] = {"API_KEY": VALUE}
    client, _ = _client(session_factory, config, fake_storage, _key, ladon=ladon, github=github)

    body = client.put(
        f"/api/agents/{AGENT}/secrets",
        json={"set": {"DB_URL": VALUE}},
        headers=_as(_key),
    ).json()

    assert body["keys"] == ["API_KEY", "DB_URL"]
    assert body["wiring"] == "unchanged"
    assert github.pushes == []


def test_removing_a_secret_names_it_to_the_lane_and_leaves_the_rest(
    session_factory, config, fake_storage, _key
):
    ladon = FakeLadon()
    ladon.stored[f"apps/{AGENT}/env"] = {"API_KEY": VALUE, "DB_URL": VALUE}
    github = _github(
        repo_files={
            "gojira.yaml": BASE_GOJIRA_YAML + f"secrets_from:\n  - {AGENT}-env\n"
        }
    )
    client, _ = _client(session_factory, config, fake_storage, _key, ladon=ladon, github=github)

    body = client.put(
        f"/api/agents/{AGENT}/secrets",
        json={"remove": ["DB_URL"]},
        headers=_as(_key),
    ).json()

    assert body["keys"] == ["API_KEY"]
    assert ladon.removes == [(OWNER, f"apps/{AGENT}/env", ["DB_URL"])]


def test_removing_the_last_secret_is_refused_in_the_rooms_own_words(
    session_factory, config, fake_storage, _key
):
    ladon = FakeLadon()
    ladon.stored[f"apps/{AGENT}/env"] = {"API_KEY": VALUE}
    client, _ = _client(session_factory, config, fake_storage, _key, ladon=ladon)

    resp = client.put(
        f"/api/agents/{AGENT}/secrets",
        json={"remove": ["API_KEY"]},
        headers=_as(_key),
    )

    assert resp.status_code == 422
    assert "destroy the agent" in resp.json()["error"]
    assert ladon.puts == []


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("lower_case", "capital letters"),
        ("SLACK_BOT_TOKEN", ROOM_SETS_THIS_ONE),
        ("VERTEXAI_LOCATION", ROOM_SETS_THIS_ONE),
    ],
)
def test_a_key_the_rule_refuses_never_reaches_the_lane(
    session_factory, config, fake_storage, _key, key, expected
):
    ladon = FakeLadon()
    client, _ = _client(session_factory, config, fake_storage, _key, ladon=ladon)

    resp = client.put(
        f"/api/agents/{AGENT}/secrets", json={"set": {key: VALUE}}, headers=_as(_key)
    )

    assert resp.status_code == 422
    assert expected in resp.json()["error"]
    assert ladon.puts == []


def test_a_write_to_a_room_with_no_secrets_platform_says_so(
    session_factory, config, fake_storage, _key
):
    client, _ = _client(session_factory, config, fake_storage, _key, ladon=None)

    resp = client.put(
        f"/api/agents/{AGENT}/secrets",
        json={"set": {"API_KEY": VALUE}},
        headers=_as(_key),
    )

    assert resp.status_code == 503
    assert resp.json()["error"] == NO_SECRETS_PLATFORM


def test_an_agent_with_nobody_on_record_is_refused_rather_than_written_anonymously(
    session_factory, tmp_path, fake_storage, _key
):
    # A room with no perimeter in front of it AND an agent with no creator
    # recorded: there is nobody for the platform to record the write
    # against, which is a standing state rather than a blip, so it reads as
    # a refusal with the reason rather than as something to retry.
    config = Config(
        workdir=tmp_path / "work",
        skills_bucket="skills-bucket",
        personas_bucket="personas-bucket",
    )
    ladon = FakeLadon()
    client, _ = _client(
        session_factory, config, fake_storage, _key, ladon=ladon, created_by=None
    )

    resp = client.put(
        f"/api/agents/{AGENT}/secrets", json={"set": {"API_KEY": VALUE}}
    )

    assert resp.status_code == 409
    assert ladon.puts == []


def test_a_lane_failure_never_repeats_the_platforms_own_text_or_the_value(
    session_factory, config, fake_storage, _key, caplog
):
    ladon = FakeLadon(put_error=f"refused the body {VALUE}")
    client, _ = _client(session_factory, config, fake_storage, _key, ladon=ladon)

    with caplog.at_level("WARNING"):
        resp = client.put(
            f"/api/agents/{AGENT}/secrets",
            json={"set": {"API_KEY": VALUE}},
            headers=_as(_key),
        )

    assert resp.status_code == 502
    assert VALUE not in resp.text
    assert "refused the body" not in resp.text


def test_a_stranger_cannot_write_an_agents_secrets(
    session_factory, config, fake_storage, _key
):
    ladon = FakeLadon()
    client, _ = _client(session_factory, config, fake_storage, _key, ladon=ladon)

    resp = client.put(
        f"/api/agents/{AGENT}/secrets",
        json={"set": {"API_KEY": VALUE}},
        headers=_as(_key, STRANGER),
    )

    assert resp.status_code == 403
    assert ladon.puts == []


# --- Writing secrets on an agent the room does not own ------------------------


def test_a_connected_agents_first_secret_is_proposed_not_pushed(
    session_factory, config, fake_storage, _key
):
    ladon = FakeLadon()
    github = _github()
    client, _ = _client(
        session_factory,
        config,
        fake_storage,
        _key,
        kind=KIND_CONNECTED,
        ladon=ladon,
        github=github,
    )

    body = client.put(
        f"/api/agents/{AGENT}/secrets",
        json={"set": {"API_KEY": VALUE}},
        headers=_as(_key),
    ).json()

    assert body["wiring"] == "pull_request"
    assert body["pull_request"] == PR_URL
    assert github.pushes == []
    assert github.open_pr_calls[0]["branch"] == "kobo/env"
    # Stored all the same: the value is on the platform whether or not
    # anybody has merged the one line that names it yet.
    assert ladon.puts[0][2] == {"API_KEY": VALUE}


def test_a_connected_agents_later_secret_reuses_the_open_pull_request(
    session_factory, config, fake_storage, _key
):
    ladon = FakeLadon()
    github = _github(open_prs={f"{REPO}#kobo/env": PR_URL})
    client, _ = _client(
        session_factory,
        config,
        fake_storage,
        _key,
        kind=KIND_CONNECTED,
        ladon=ladon,
        github=github,
    )

    body = client.put(
        f"/api/agents/{AGENT}/secrets",
        json={"set": {"API_KEY": VALUE}},
        headers=_as(_key),
    ).json()

    assert body["wiring"] == "pull_request"
    assert body["pull_request"] == PR_URL
    assert github.open_pr_calls == []


# --- Reading and writing environment variables --------------------------------


def test_the_environment_list_shows_what_the_file_holds_without_the_model_keys(
    session_factory, config, fake_storage, _key
):
    github = _github(
        repo_files={
            "gojira.yaml": (
                "name: clio\nenv:\n  VERTEXAI_PROJECT: a-project\n"
                "  API_BASE: https://example.test\n"
            )
        }
    )
    client, _ = _client(session_factory, config, fake_storage, _key, github=github)

    body = client.get(f"/api/agents/{AGENT}/env", headers=_as(_key)).json()

    assert body["env"] == {"API_BASE": "https://example.test"}


def test_the_environment_list_works_without_a_secrets_platform(
    session_factory, config, fake_storage, _key
):
    # The panel's Secrets half goes away in a room with no platform; this
    # half does not, and this is the route that has to prove it.
    client, _ = _client(session_factory, config, fake_storage, _key, ladon=None)

    assert client.get(f"/api/agents/{AGENT}/env", headers=_as(_key)).status_code == 200
    resp = client.put(
        f"/api/agents/{AGENT}/env",
        json={"set": {"API_BASE": "https://example.test"}},
        headers=_as(_key),
    )
    assert resp.status_code == 200
    assert resp.json()["wiring"] == "committed"


def test_an_environment_write_is_committed_and_the_platform_is_asked_to_rebuild(
    session_factory, config, fake_storage, _key
):
    github = _github()
    client, clients = _client(session_factory, config, fake_storage, _key, github=github)

    body = client.put(
        f"/api/agents/{AGENT}/env",
        json={"set": {"API_BASE": "https://example.test"}},
        headers=_as(_key),
    ).json()

    assert body["env"] == {"API_BASE": "https://example.test"}
    assert body["wiring"] == "committed"
    assert clients.gojira.reprovision_calls
    pushed = yaml.safe_load(github.pushed_file("gojira.yaml"))
    assert pushed["env"]["API_BASE"] == "https://example.test"


def test_an_environment_write_keeps_the_model_keys_the_room_set(
    session_factory, config, fake_storage, _key
):
    github = _github(
        repo_files={
            "gojira.yaml": "name: clio\nenv:\n  VERTEXAI_PROJECT: a-project\n"
        }
    )
    client, _ = _client(session_factory, config, fake_storage, _key, github=github)

    client.put(
        f"/api/agents/{AGENT}/env",
        json={"set": {"API_BASE": "https://example.test"}},
        headers=_as(_key),
    )

    pushed = yaml.safe_load(github.pushed_file("gojira.yaml"))
    assert pushed["env"]["VERTEXAI_PROJECT"] == "a-project"


def test_a_connected_agents_environment_change_is_proposed_not_pushed(
    session_factory, config, fake_storage, _key
):
    github = _github()
    client, clients = _client(
        session_factory, config, fake_storage, _key, kind=KIND_CONNECTED, github=github
    )

    body = client.put(
        f"/api/agents/{AGENT}/env",
        json={"set": {"API_BASE": "https://example.test"}},
        headers=_as(_key),
    ).json()

    assert body["wiring"] == "pull_request"
    assert body["pull_request"] == PR_URL
    assert github.pushes == []
    assert clients.gojira.reprovision_calls == []


def test_a_connected_agents_second_environment_change_names_the_open_request(
    session_factory, config, fake_storage, _key
):
    github = _github(open_prs={f"{REPO}#kobo/env": PR_URL})
    client, _ = _client(
        session_factory, config, fake_storage, _key, kind=KIND_CONNECTED, github=github
    )

    resp = client.put(
        f"/api/agents/{AGENT}/env",
        json={"set": {"API_BASE": "https://example.test"}},
        headers=_as(_key),
    )

    assert resp.status_code == 409
    assert resp.json()["error"] == f"{ENV_PR_ALREADY_OPEN}{PR_URL}"


def test_an_environment_key_the_rule_refuses_never_reaches_the_repository(
    session_factory, config, fake_storage, _key
):
    github = _github()
    client, _ = _client(session_factory, config, fake_storage, _key, github=github)

    resp = client.put(
        f"/api/agents/{AGENT}/env",
        json={"set": {"VERTEXAI_PROJECT": "somewhere else"}},
        headers=_as(_key),
    )

    assert resp.status_code == 422
    assert ROOM_SETS_THIS_ONE in resp.json()["error"]
    assert github.pushes == []


def test_an_environment_write_that_changes_nothing_pushes_nothing(
    session_factory, config, fake_storage, _key
):
    github = _github(
        repo_files={
            "gojira.yaml": "name: clio\nenv:\n  API_BASE: https://example.test\n"
        }
    )
    client, _ = _client(session_factory, config, fake_storage, _key, github=github)

    body = client.put(
        f"/api/agents/{AGENT}/env",
        json={"set": {"API_BASE": "https://example.test"}},
        headers=_as(_key),
    ).json()

    assert body["wiring"] == "unchanged"
    assert github.pushes == []


def test_a_stranger_cannot_read_or_write_the_environment(
    session_factory, config, fake_storage, _key
):
    client, _ = _client(session_factory, config, fake_storage, _key)

    assert (
        client.get(f"/api/agents/{AGENT}/env", headers=_as(_key, STRANGER)).status_code
        == 403
    )
    assert (
        client.put(
            f"/api/agents/{AGENT}/env",
            json={"set": {"API_BASE": "https://example.test"}},
            headers=_as(_key, STRANGER),
        ).status_code
        == 403
    )


def test_an_agent_nobody_can_see_is_a_404_on_every_one_of_the_four(
    session_factory, config, fake_storage, _key
):
    client, _ = _client(session_factory, config, fake_storage, _key, ladon=FakeLadon())

    for method, path in [
        ("get", "secrets"),
        ("put", "secrets"),
        ("get", "env"),
        ("put", "env"),
    ]:
        call = getattr(client, method)
        kwargs = {"json": {}} if method == "put" else {}
        resp = call(f"/api/agents/nobody/{path}", headers=_as(_key), **kwargs)
        assert resp.status_code == 404, (method, path)
