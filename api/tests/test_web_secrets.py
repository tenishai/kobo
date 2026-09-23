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

import tempfile
import time
from pathlib import Path

import jwt
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.agent_env import (
    ENV_BRANCH,
    ENV_PR_ALREADY_OPEN,
    MAX_ENV_VALUE_LENGTH,
    MAX_KEYS,
    ROOM_SETS_THIS_ONE,
    SECRETS_WIRING_BRANCH,
    SYNC_FAILED,
    SYNC_NOT_DELIVERED,
    SYNC_UNKNOWN,
    UNREADABLE_GOJIRA_YAML,
    UNREADABLE_REPOSITORY,
)
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
        # Not "unreadable": there is nothing to read, which is a
        # different sentence from "the room cannot ask right now".
        "unreadable": False,
        "sync": {"state": "unwired", "message": SYNC_NOT_DELIVERED},
        "wired": None,
        "managed": [],
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


def test_removing_the_last_secret_takes_the_path_the_object_and_the_line(
    session_factory, config, fake_storage, _key
):
    # Emptying the list is allowed, and it is a DELETE rather than a write
    # of nothing: the path goes, the ExternalSecret goes (taking the
    # Kubernetes Secret with it), and the line naming that Secret comes
    # out of gojira.yaml in the same call. A pod whose envFrom names a
    # Secret that no longer exists does not start.
    ladon = FakeLadon()
    ladon.stored[f"apps/{AGENT}/env"] = {"API_KEY": VALUE}
    github = _github(
        repo_files={
            "gojira.yaml": BASE_GOJIRA_YAML + f"secrets_from:\n  - {AGENT}-env\n"
        }
    )
    client, clients = _client(
        session_factory, config, fake_storage, _key, ladon=ladon, github=github
    )

    body = client.put(
        f"/api/agents/{AGENT}/secrets",
        json={"remove": ["API_KEY"]},
        headers=_as(_key),
    ).json()

    assert body["keys"] == []
    assert body["emptied"] is True
    assert body["wiring"] == "committed"
    assert body["secret_name"] is None
    assert ladon.deletes == [(OWNER, f"apps/{AGENT}/env")]
    assert ladon.unsyncs == [(OWNER, AGENT, AGENT, f"{AGENT}-env")]
    assert ladon.puts == []
    pushed = yaml.safe_load(github.pushed_file("gojira.yaml"))
    assert pushed["secrets_from"] == []
    assert clients.gojira.reprovision_calls


def test_emptying_an_agent_that_was_never_wired_touches_no_repository(
    session_factory, config, fake_storage, _key
):
    ladon = FakeLadon()
    ladon.stored[f"apps/{AGENT}/env"] = {"API_KEY": VALUE}
    github = _github()
    client, _ = _client(
        session_factory, config, fake_storage, _key, ladon=ladon, github=github
    )

    body = client.put(
        f"/api/agents/{AGENT}/secrets",
        json={"remove": ["API_KEY"]},
        headers=_as(_key),
    ).json()

    assert body["emptied"] is True
    assert body["wiring"] == "unchanged"
    assert github.pushes == []


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
    # The platform's own sentence can quote the request it refused, and
    # that request carried a value. So neither the response NOR the log
    # gets it: what is logged is the exception's class and the agent's
    # name, which is what an operator acts on.
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
    assert VALUE not in caplog.text
    assert "refused the body" not in caplog.text
    # Still enough to act on.
    assert AGENT in caplog.text
    assert "ClientError" in caplog.text


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
    # Its own branch, not the environment one: the line it proposes is
    # always the same, and a pending environment request is a different
    # change entirely.
    assert github.open_pr_calls[0]["branch"] == SECRETS_WIRING_BRANCH
    # Stored all the same: the value is on the platform whether or not
    # anybody has merged the one line that names it yet.
    assert ladon.puts[0][2] == {"API_KEY": VALUE}


def test_a_connected_agents_later_secret_reuses_a_request_that_carries_the_line(
    session_factory, config, fake_storage, _key
):
    ladon = FakeLadon()
    github = _github(
        open_prs={f"{REPO}#{SECRETS_WIRING_BRANCH}": PR_URL},
        branch_files={
            SECRETS_WIRING_BRANCH: {
                "gojira.yaml": BASE_GOJIRA_YAML + f"secrets_from:\n  - {AGENT}-env\n"
            }
        },
    )
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


def test_an_open_environment_request_does_not_block_a_connected_agents_first_secret(
    session_factory, config, fake_storage, _key
):
    # The exact scenario the two branches exist for: somebody saved an
    # environment variable, the request is still open, and now they add a
    # secret. The wiring is a SECOND pull request, on its own branch,
    # carrying its own line.
    ladon = FakeLadon()
    github = _github(
        open_prs={f"{REPO}#{ENV_BRANCH}": "https://example.test/pull/env"},
        # So the pull request's own content can be read back below: the
        # fake snapshots what `open_pr` was handed, and a later clone
        # reflects it.
        track_pushed_content=True,
    )
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
    assert body["pull_request"] != "https://example.test/pull/env"
    assert [call["branch"] for call in github.open_pr_calls] == [SECRETS_WIRING_BRANCH]
    assert github.pushes == []
    # And what it proposes really is the wiring line.
    with tempfile.TemporaryDirectory() as tmp:
        proposed = Path(tmp)
        github.clone(REPO, proposed)
        data = yaml.safe_load((proposed / "gojira.yaml").read_text())
    assert data["secrets_from"] == [f"{AGENT}-env"]


def test_a_stale_unwire_request_is_closed_before_a_secret_is_added_back(
    session_factory, config, fake_storage, _key
):
    # The room proposed taking the line away, nobody merged it, and now
    # somebody is adding a secret. Merging that request later would take
    # the line from an agent that needs it, so it is closed with a word
    # about why and its branch removed, and a fresh request proposes the
    # line again.
    ladon = FakeLadon()
    github = _github(
        open_prs={f"{REPO}#{SECRETS_WIRING_BRANCH}": "https://example.test/pull/stale"},
        # The branch head does NOT carry the line: this request proposes
        # removing it.
        branch_files={SECRETS_WIRING_BRANCH: {"gojira.yaml": BASE_GOJIRA_YAML}},
        track_pushed_content=True,
    )
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

    assert [url for _, url, _ in github.close_pr_calls] == [
        "https://example.test/pull/stale"
    ]
    assert "Superseded" in github.close_pr_calls[0][2]
    assert github.deleted_branches == [(REPO, SECRETS_WIRING_BRANCH)]
    assert body["wiring"] == "pull_request"
    assert body["pull_request"] == PR_URL
    with tempfile.TemporaryDirectory() as tmp:
        proposed = Path(tmp)
        github.clone(REPO, proposed)
        data = yaml.safe_load((proposed / "gojira.yaml").read_text())
    assert data["secrets_from"] == [f"{AGENT}-env"]


def test_a_stale_wire_request_is_closed_before_the_last_secret_is_removed(
    session_factory, config, fake_storage, _key
):
    # The mirror. The room proposed adding the line, nobody merged it, and
    # the last secret is going away: merged later, that request would name
    # a Secret that no longer exists, and the agent would not start.
    ladon = FakeLadon()
    ladon.stored[f"apps/{AGENT}/env"] = {"API_KEY": VALUE}
    github = _github(
        open_prs={f"{REPO}#{SECRETS_WIRING_BRANCH}": "https://example.test/pull/stale"},
        # The branch head DOES carry the line: this request proposes
        # adding it.
        branch_files={
            SECRETS_WIRING_BRANCH: {
                "gojira.yaml": BASE_GOJIRA_YAML + f"secrets_from:\n  - {AGENT}-env\n"
            }
        },
    )
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
        json={"remove": ["API_KEY"]},
        headers=_as(_key),
    ).json()

    assert [url for _, url, _ in github.close_pr_calls] == [
        "https://example.test/pull/stale"
    ]
    assert github.deleted_branches == [(REPO, SECRETS_WIRING_BRANCH)]
    # The default branch never had the line, so there is nothing to
    # propose removing and the deletes can happen now.
    assert body["emptied"] is True
    assert ladon.deletes == [(OWNER, f"apps/{AGENT}/env")]


def test_an_open_request_that_already_proposes_this_is_reused_not_reopened(
    session_factory, config, fake_storage, _key
):
    ladon = FakeLadon()
    github = _github(
        open_prs={f"{REPO}#{SECRETS_WIRING_BRANCH}": PR_URL},
        branch_files={
            SECRETS_WIRING_BRANCH: {
                "gojira.yaml": BASE_GOJIRA_YAML + f"secrets_from:\n  - {AGENT}-env\n"
            }
        },
    )
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

    assert body["pull_request"] == PR_URL
    assert github.close_pr_calls == []
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


# --- the file, and the repository, the room may not be able to read -----------


def test_an_unparseable_gojira_yaml_stops_a_secret_before_it_is_stored(
    session_factory, config, fake_storage, _key
):
    # The wiring line could never be added to a file nobody can parse, so
    # finding that out first costs one read. Finding it out afterwards
    # would leave a value stored that the agent can never read.
    ladon = FakeLadon()
    github = _github(repo_files={"gojira.yaml": "env: [unclosed\n"})
    client, _ = _client(
        session_factory, config, fake_storage, _key, ladon=ladon, github=github
    )

    resp = client.put(
        f"/api/agents/{AGENT}/secrets",
        json={"set": {"API_KEY": VALUE}},
        headers=_as(_key),
    )

    assert resp.status_code == 409
    assert resp.json()["error"] == UNREADABLE_GOJIRA_YAML
    assert ladon.puts == []
    assert ladon.syncs == []


def test_an_unparseable_gojira_yaml_is_named_on_the_environment_read_too(
    session_factory, config, fake_storage, _key
):
    github = _github(repo_files={"gojira.yaml": "env: [unclosed\n"})
    client, _ = _client(session_factory, config, fake_storage, _key, github=github)

    resp = client.get(f"/api/agents/{AGENT}/env", headers=_as(_key))

    assert resp.status_code == 409
    assert resp.json()["error"] == UNREADABLE_GOJIRA_YAML


def test_a_repository_the_room_cannot_read_says_so_rather_than_no_file(
    session_factory, config, fake_storage, _key
):
    github = FakeGitHub(existing_repos=set(), repo_files={})
    client, _ = _client(session_factory, config, fake_storage, _key, github=github)

    resp = client.get(f"/api/agents/{AGENT}/env", headers=_as(_key))

    assert resp.status_code == 409
    assert resp.json()["error"] == UNREADABLE_REPOSITORY


def test_a_secrets_read_that_the_lane_refuses_is_a_plain_failure(
    session_factory, config, fake_storage, _key, caplog
):
    ladon = FakeLadon(read_error="that namespace is not yours")
    client, _ = _client(session_factory, config, fake_storage, _key, ladon=ladon)

    with caplog.at_level("WARNING"):
        resp = client.get(f"/api/agents/{AGENT}/secrets", headers=_as(_key))

    assert resp.status_code == 502
    assert "that namespace is not yours" not in resp.text
    assert "that namespace is not yours" not in caplog.text


# --- the deploy a retry would otherwise never make ----------------------------


def test_an_environment_save_that_changes_nothing_still_triggers_a_deploy(
    session_factory, config, fake_storage, _key
):
    # The case this exists for: a save whose push landed and whose
    # reprovision then failed. A retry has nothing left to push, so
    # refusing to reprovision would leave that change undeployed forever.
    github = _github(
        repo_files={"gojira.yaml": "name: clio\nenv:\n  API_BASE: https://example.test\n"}
    )
    client, clients = _client(
        session_factory, config, fake_storage, _key, github=github
    )

    body = client.put(
        f"/api/agents/{AGENT}/env",
        json={"set": {"API_BASE": "https://example.test"}},
        headers=_as(_key),
    ).json()

    assert body["wiring"] == "unchanged"
    assert github.pushes == []
    assert clients.gojira.reprovision_calls


# --- who the platform records a read against ----------------------------------


def test_a_read_names_the_caller_not_the_creator(
    session_factory, config, fake_storage, _key
):
    # The platform's audit answers "who asked about this path", and the
    # person who opened the panel is the honest answer. An operator
    # reading somebody else's agent is recorded as themselves.
    ladon = FakeLadon()
    ladon.stored[f"apps/{AGENT}/env"] = {"API_KEY": VALUE}
    operator = "ops@example.com"
    operator_config = config.model_copy(update={"operators": [operator]})
    client, _ = _client(
        session_factory, operator_config, fake_storage, _key, ladon=ladon
    )

    client.get(f"/api/agents/{AGENT}/secrets", headers=_as(_key, operator))

    # The agent's own path first, then the two the room sets itself --
    # every one of them on the caller's behalf, never the creator's.
    assert ladon.reads == [
        (operator, f"apps/{AGENT}/env"),
        (operator, f"apps/{AGENT}/cabal"),
        (operator, f"apps/{AGENT}/slack"),
    ]


def test_a_read_of_an_agent_with_no_creator_still_answers(
    session_factory, config, fake_storage, _key
):
    # A write refuses when there is nobody to record it against; a read
    # never does, because the caller IS that person. An agent whose
    # creator has left is still one its operator can open the panel on.
    ladon = FakeLadon()
    ladon.stored[f"apps/{AGENT}/env"] = {"API_KEY": VALUE}
    operator = "ops@example.com"
    operator_config = config.model_copy(update={"operators": [operator]})
    client, _ = _client(
        session_factory,
        operator_config,
        fake_storage,
        _key,
        ladon=ladon,
        created_by=None,
    )

    resp = client.get(f"/api/agents/{AGENT}/secrets", headers=_as(_key, operator))

    assert resp.status_code == 200
    assert resp.json()["keys"] == ["API_KEY"]


# --- the two caps, at the request rather than at the lane ---------------------


def test_more_keys_than_a_list_may_hold_is_refused(
    session_factory, config, fake_storage, _key
):
    ladon = FakeLadon()
    client, _ = _client(session_factory, config, fake_storage, _key, ladon=ladon)

    resp = client.put(
        f"/api/agents/{AGENT}/secrets",
        json={"set": {f"K{i}": VALUE for i in range(MAX_KEYS + 1)}},
        headers=_as(_key),
    )

    assert resp.status_code == 422
    assert ladon.puts == []


def test_a_value_longer_than_the_ceiling_is_refused(
    session_factory, config, fake_storage, _key
):
    ladon = FakeLadon()
    client, _ = _client(session_factory, config, fake_storage, _key, ladon=ladon)

    resp = client.put(
        f"/api/agents/{AGENT}/secrets",
        json={"set": {"API_KEY": "x" * (MAX_ENV_VALUE_LENGTH + 1)}},
        headers=_as(_key),
    )

    assert resp.status_code == 422
    assert ladon.puts == []
    # And the oversized value is not echoed back in the refusal.
    assert "x" * 100 not in resp.text


def test_a_write_that_fills_a_list_exactly_to_the_cap_is_allowed(
    session_factory, config, fake_storage, _key
):
    ladon = FakeLadon()
    client, _ = _client(session_factory, config, fake_storage, _key, ladon=ladon)

    resp = client.put(
        f"/api/agents/{AGENT}/secrets",
        json={"set": {f"K{i}": VALUE for i in range(MAX_KEYS)}},
        headers=_as(_key),
    )

    assert resp.status_code == 200
    assert len(resp.json()["keys"]) == MAX_KEYS


# --- emptying: the line goes before the Secret does ----------------------------
#
# The invariant every case here defends: gojira.yaml must never name a
# Kubernetes Secret that does not exist. The deploy platform turns
# `secrets_from` into `envFrom`, and a pod whose envFrom names a missing
# Secret does not start at all.


def _wired_github(**kwargs):
    return _github(
        repo_files={
            "gojira.yaml": BASE_GOJIRA_YAML + f"secrets_from:\n  - {AGENT}-env\n"
        },
        **kwargs,
    )


def test_a_composed_agent_commits_the_line_away_before_deleting_the_secret(
    session_factory, config, fake_storage, _key
):
    order: list[str] = []

    class _Ordered(FakeLadon):
        def unsync(self, acting_email, app, k8s_namespace, name):
            order.append("unsync")
            super().unsync(acting_email, app, k8s_namespace, name)

        def delete_secret(self, acting_email, path):
            order.append("delete")
            super().delete_secret(acting_email, path)

    class _OrderedGitHub(FakeGitHub):
        def commit_push(self, workdir, message):
            order.append("push")
            return super().commit_push(workdir, message)

    ladon = _Ordered()
    ladon.stored[f"apps/{AGENT}/env"] = {"API_KEY": VALUE}
    github = _OrderedGitHub(
        existing_repos={REPO},
        repo_files={
            "gojira.yaml": BASE_GOJIRA_YAML + f"secrets_from:\n  - {AGENT}-env\n"
        },
        extra_clone_files={
            "gojira.yaml": BASE_GOJIRA_YAML + f"secrets_from:\n  - {AGENT}-env\n"
        },
        pr_url=PR_URL,
    )
    client, clients = _client(
        session_factory, config, fake_storage, _key, ladon=ladon, github=github
    )

    body = client.put(
        f"/api/agents/{AGENT}/secrets",
        json={"remove": ["API_KEY"]},
        headers=_as(_key),
    ).json()

    assert order == ["push", "unsync", "delete"]
    assert body["emptied"] is True
    assert body["pending_unwire"] is False
    pushed = yaml.safe_load(github.pushed_file("gojira.yaml"))
    assert pushed["secrets_from"] == []
    # And the deploy is asked for after the Secret is gone, not between.
    assert clients.gojira.reprovision_calls


def test_a_connected_agent_proposes_the_removal_and_deletes_nothing(
    session_factory, config, fake_storage, _key
):
    ladon = FakeLadon()
    ladon.stored[f"apps/{AGENT}/env"] = {"API_KEY": VALUE}
    github = _wired_github()
    client, clients = _client(
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
        json={"remove": ["API_KEY"]},
        headers=_as(_key),
    ).json()

    assert body["pending_unwire"] is True
    assert body["emptied"] is False
    assert body["wiring"] == "pull_request"
    assert body["pull_request"] == PR_URL
    # Nothing was taken away: the file still names the Secret.
    assert ladon.deletes == []
    assert ladon.unsyncs == []
    assert ladon.stored[f"apps/{AGENT}/env"] == {"API_KEY": VALUE}
    assert clients.gojira.reprovision_calls == []


def test_the_save_after_that_pull_request_merges_finishes_the_removal(
    session_factory, config, fake_storage, _key
):
    # The default branch no longer names the Secret, so there is nothing
    # left to propose and the deletes can happen.
    ladon = FakeLadon()
    ladon.stored[f"apps/{AGENT}/env"] = {"API_KEY": VALUE}
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
        json={"remove": ["API_KEY"]},
        headers=_as(_key),
    ).json()

    assert body["emptied"] is True
    assert body["pending_unwire"] is False
    assert ladon.deletes == [(OWNER, f"apps/{AGENT}/env")]
    assert github.open_pr_calls == []


def test_a_removal_that_fails_halfway_leaves_the_line_gone_and_retries_cleanly(
    session_factory, config, fake_storage, _key
):
    # The commit lands, the lane refuses the delete. The agent is left
    # with a file that names nothing and a stored Secret nobody reads,
    # which is the safe half to be stuck on: a retry finishes it.
    ladon = FakeLadon(delete_error="the platform is having a moment")
    ladon.stored[f"apps/{AGENT}/env"] = {"API_KEY": VALUE}
    github = _wired_github(track_pushed_content=True)
    client, _ = _client(
        session_factory, config, fake_storage, _key, ladon=ladon, github=github
    )

    resp = client.put(
        f"/api/agents/{AGENT}/secrets",
        json={"remove": ["API_KEY"]},
        headers=_as(_key),
    )

    assert resp.status_code == 502
    # Never "couldn't store that": nothing was being stored.
    assert "store" not in resp.json()["error"]
    assert "remove" in resp.json()["error"]
    # The line is gone even though the Secret is not.
    assert github.pushes


def test_retrying_a_finished_removal_never_recreates_the_path(
    session_factory, config, fake_storage, _key
):
    # The dangerous retry: keys gone, line gone. An empty `put_secret`
    # here would create the path again, and the sync behind it would put
    # an ExternalSecret back over a Secret nothing wants.
    ladon = FakeLadon()
    github = _github()
    client, _ = _client(
        session_factory, config, fake_storage, _key, ladon=ladon, github=github
    )

    body = client.put(
        f"/api/agents/{AGENT}/secrets",
        json={"remove": ["API_KEY"]},
        headers=_as(_key),
    ).json()

    assert body["emptied"] is True
    assert body["keys"] == []
    assert ladon.puts == []
    assert ladon.syncs == []
    assert ladon.stored == {}


def test_a_repository_the_room_cannot_read_never_reads_as_not_wired(
    session_factory, config, fake_storage, _key
):
    # The wiring question goes through the same visibility gate the
    # environment read does. Answering "not wired" for an unreadable
    # repository would have the room propose a line already sitting on
    # the default branch.
    ladon = FakeLadon()
    github = FakeGitHub(existing_repos=set(), repo_files={})
    client, _ = _client(
        session_factory, config, fake_storage, _key, ladon=ladon, github=github
    )

    resp = client.put(
        f"/api/agents/{AGENT}/secrets",
        json={"set": {"API_KEY": VALUE}},
        headers=_as(_key),
    )

    assert resp.status_code == 409
    assert resp.json()["error"] == UNREADABLE_REPOSITORY
    assert ladon.puts == []


# --- a branch that carries the room's name and not its work -------------------


def test_a_branch_the_room_did_not_make_is_never_deleted(
    session_factory, config, fake_storage, _key
):
    from kobo_api.clients import BranchComparison

    github = _github(
        existing_branches={f"{REPO}#{ENV_BRANCH}"},
        comparisons={
            ENV_BRANCH: BranchComparison(
                found=True,
                files=("gojira.yaml", "src/main.py"),
                authors=("someone@example.com",),
            )
        },
    )
    client, _ = _client(
        session_factory, config, fake_storage, _key, kind=KIND_CONNECTED, github=github
    )

    resp = client.put(
        f"/api/agents/{AGENT}/env",
        json={"set": {"API_BASE": "https://example.test"}},
        headers=_as(_key),
    )

    assert resp.status_code == 409
    assert "the room did not make it" in resp.json()["error"]
    assert github.deleted_branches == []
    assert github.open_pr_calls == []


# --- nobody to read as --------------------------------------------------------


def test_a_read_with_nobody_to_act_as_says_so_rather_than_an_empty_list(
    session_factory, config, fake_storage, _key
):
    # An empty list and "the room cannot ask" look the same to a person
    # and mean opposite things. The panel shows one line for the second.
    ladon = FakeLadon()
    ladon.stored[f"apps/{AGENT}/env"] = {"API_KEY": VALUE}
    plain = config.model_copy(update={"iap_audience": ""})
    client, _ = _client(
        session_factory, plain, fake_storage, _key, ladon=ladon, created_by=None
    )

    body = client.get(f"/api/agents/{AGENT}/secrets").json()

    assert body["platform"] is True
    assert body["unreadable"] is True
    assert body["keys"] == []
    assert ladon.reads == []


def test_an_ordinary_read_is_not_marked_unreadable(
    session_factory, config, fake_storage, _key
):
    ladon = FakeLadon()
    client, _ = _client(session_factory, config, fake_storage, _key, ladon=ladon)

    assert (
        client.get(f"/api/agents/{AGENT}/secrets", headers=_as(_key)).json()["unreadable"]
        is False
    )


# --- the deploy trigger, told apart from the save ------------------------------


def test_a_no_op_save_whose_deploy_fails_says_the_files_were_unchanged(
    session_factory, config, fake_storage, _key
):
    github = _github(
        repo_files={"gojira.yaml": "name: clio\nenv:\n  API_BASE: https://example.test\n"}
    )
    client, clients = _client(
        session_factory, config, fake_storage, _key, github=github
    )
    clients.gojira.reprovision_error = "the platform is having a moment"

    resp = client.put(
        f"/api/agents/{AGENT}/env",
        json={"set": {"API_BASE": "https://example.test"}},
        headers=_as(_key),
    )

    assert resp.status_code == 502
    assert "already said this" in resp.json()["error"]
    assert "redeploy did not work" in resp.json()["error"]
    assert github.pushes == []


def test_a_real_save_whose_deploy_fails_says_the_change_is_saved(
    session_factory, config, fake_storage, _key
):
    github = _github()
    client, clients = _client(
        session_factory, config, fake_storage, _key, github=github
    )
    clients.gojira.reprovision_error = "the platform is having a moment"

    resp = client.put(
        f"/api/agents/{AGENT}/env",
        json={"set": {"API_BASE": "https://example.test"}},
        headers=_as(_key),
    )

    assert resp.status_code == 502
    assert "is saved" in resp.json()["error"]
    assert github.pushes


# --- The lock: is what the list names actually ON the agent? ------------------
#
# The operator's own words for why this exists: somebody who adds a secret
# should know at a glance that it is really set. A key name in a list is
# not a credential in a pod, so the read says which of the two it has.


def _ready(status: str, reason: str | None = None, message: str | None = None) -> dict:
    condition: dict = {"type": "Ready", "status": status}
    if reason is not None:
        condition["reason"] = reason
    if message is not None:
        condition["message"] = message
    return {"conditions": [condition]}


def _with_secret(ladon: FakeLadon | None = None) -> FakeLadon:
    ladon = ladon or FakeLadon()
    ladon.stored[f"apps/{AGENT}/env"] = {"API_KEY": VALUE}
    return ladon


def test_a_delivered_secret_reads_as_live(session_factory, config, fake_storage, _key):
    kubectl = FakeKubectl(external_secrets={(AGENT, f"{AGENT}-env"): _ready("True")})
    client, _ = _client(
        session_factory, config, fake_storage, _key, ladon=_with_secret(), kubectl=kubectl
    )

    body = client.get(f"/api/agents/{AGENT}/secrets", headers=_as(_key)).json()

    assert body["keys"] == ["API_KEY"]
    assert body["sync"] == {"state": "live", "message": None}


def test_a_secret_the_cluster_has_not_settled_yet_reads_as_syncing(
    session_factory, config, fake_storage, _key
):
    kubectl = FakeKubectl(external_secrets={(AGENT, f"{AGENT}-env"): {}})
    client, _ = _client(
        session_factory, config, fake_storage, _key, ladon=_with_secret(), kubectl=kubectl
    )

    body = client.get(f"/api/agents/{AGENT}/secrets", headers=_as(_key)).json()

    assert body["sync"] == {"state": "syncing", "message": None}


def test_a_stored_secret_with_nothing_delivering_it_reads_as_unwired(
    session_factory, config, fake_storage, _key
):
    # Nothing seeded: the namespace holds no such object at all.
    client, _ = _client(session_factory, config, fake_storage, _key, ladon=_with_secret())

    body = client.get(f"/api/agents/{AGENT}/secrets", headers=_as(_key)).json()

    assert body["sync"] == {"state": "unwired", "message": SYNC_NOT_DELIVERED}


def test_a_failing_delivery_reads_as_an_error_and_never_quotes_the_platform(
    session_factory, config, fake_storage, _key
):
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
        session_factory, config, fake_storage, _key, ladon=_with_secret(), kubectl=kubectl
    )

    resp = client.get(f"/api/agents/{AGENT}/secrets", headers=_as(_key))

    assert resp.json()["sync"] == {
        "state": "error",
        "message": f"{SYNC_FAILED} It reported SecretSyncedError.",
    }
    assert VALUE not in resp.text


def test_a_cluster_the_room_could_not_ask_does_not_take_the_list_away(
    session_factory, config, fake_storage, _key
):
    kubectl = FakeKubectl(external_secret_error="Unable to connect to the server")
    client, _ = _client(
        session_factory, config, fake_storage, _key, ladon=_with_secret(), kubectl=kubectl
    )

    resp = client.get(f"/api/agents/{AGENT}/secrets", headers=_as(_key))

    assert resp.status_code == 200
    assert resp.json()["keys"] == ["API_KEY"]
    assert resp.json()["sync"] == {"state": "syncing", "message": SYNC_UNKNOWN}


def test_the_read_never_asks_for_the_kubernetes_secret_itself(
    session_factory, config, fake_storage, _key
):
    # The one thing this feature must not do to answer "is it set".
    kubectl = FakeKubectl(external_secrets={(AGENT, f"{AGENT}-env"): _ready("True")})
    client, _ = _client(
        session_factory, config, fake_storage, _key, ladon=_with_secret(), kubectl=kubectl
    )

    client.get(f"/api/agents/{AGENT}/secrets", headers=_as(_key))

    assert kubectl.external_secret_reads == [(AGENT, f"{AGENT}-env")]
    assert kubectl.secrets == {}


def test_the_read_says_whether_the_agents_own_files_name_the_secret(
    session_factory, config, fake_storage, _key
):
    wired = _github(
        repo_files={
            "gojira.yaml": BASE_GOJIRA_YAML + f"secrets_from:\n- {AGENT}-env\n"
        }
    )
    client, _ = _client(
        session_factory, config, fake_storage, _key, ladon=_with_secret(), github=wired
    )

    assert client.get(f"/api/agents/{AGENT}/secrets", headers=_as(_key)).json()["wired"] is True


def test_a_file_with_no_line_reads_as_not_wired(
    session_factory, config, fake_storage, _key
):
    client, _ = _client(session_factory, config, fake_storage, _key, ladon=_with_secret())

    assert client.get(f"/api/agents/{AGENT}/secrets", headers=_as(_key)).json()["wired"] is False


def test_a_repository_the_room_cannot_read_makes_no_claim_about_the_file(
    session_factory, config, fake_storage, _key
):
    # Null, not false: "the room cannot look" is not "the line is not
    # there", and the panel must not turn one into the other.
    client, _ = _client(
        session_factory,
        config,
        fake_storage,
        _key,
        ladon=_with_secret(),
        github=FakeGitHub(existing_repos=set(), repo_files={}),
    )

    resp = client.get(f"/api/agents/{AGENT}/secrets", headers=_as(_key))

    assert resp.status_code == 200
    assert resp.json()["wired"] is None


# --- The rows the ROOM set ----------------------------------------------------


def test_the_rooms_own_secrets_come_back_as_locked_rows(
    session_factory, config, fake_storage, _key
):
    ladon = _with_secret()
    ladon.stored[f"apps/{AGENT}/cabal"] = {"CABAL_COLLEAGUE_TOKEN": VALUE}
    ladon.stored[f"apps/{AGENT}/slack"] = {"SLACK_BOT_TOKEN": VALUE, "SLACK_APP_TOKEN": VALUE}
    kubectl = FakeKubectl(
        external_secrets={
            (AGENT, f"{AGENT}-cabal"): _ready("True"),
            (AGENT, f"{AGENT}-slack"): {},
        }
    )
    client, _ = _client(
        session_factory, config, fake_storage, _key, ladon=ladon, kubectl=kubectl
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


def test_a_purpose_the_room_never_set_is_simply_not_a_row(
    session_factory, config, fake_storage, _key
):
    ladon = _with_secret()
    ladon.stored[f"apps/{AGENT}/cabal"] = {"CABAL_COLLEAGUE_TOKEN": VALUE}
    client, _ = _client(session_factory, config, fake_storage, _key, ladon=ladon)

    managed = client.get(f"/api/agents/{AGENT}/secrets", headers=_as(_key)).json()["managed"]

    assert [row["purpose"] for row in managed] == ["cabal"]


def test_an_agent_the_room_has_set_nothing_on_has_no_locked_rows(
    session_factory, config, fake_storage, _key
):
    client, _ = _client(session_factory, config, fake_storage, _key, ladon=_with_secret())

    assert client.get(f"/api/agents/{AGENT}/secrets", headers=_as(_key)).json()["managed"] == []


def test_a_room_managed_name_still_cannot_be_written_or_removed(
    session_factory, config, fake_storage, _key
):
    # Showing these rows changes nothing about who may set them: the same
    # refusal the key rules have always given, on both sides of a write.
    ladon = _with_secret()
    ladon.stored[f"apps/{AGENT}/slack"] = {"SLACK_BOT_TOKEN": VALUE}
    client, _ = _client(session_factory, config, fake_storage, _key, ladon=ladon)

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
    assert ladon.puts == []


def test_a_room_with_no_secrets_platform_makes_no_claim_about_the_cluster(
    session_factory, config, fake_storage, _key
):
    client, _ = _client(session_factory, config, fake_storage, _key, ladon=None)

    body = client.get(f"/api/agents/{AGENT}/secrets", headers=_as(_key)).json()

    assert body["sync"] == {"state": "unwired", "message": SYNC_NOT_DELIVERED}
    assert body["managed"] == []
    assert body["wired"] is None
