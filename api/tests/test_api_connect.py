"""Tests for the three connect routes: the scan a team runs before it commits
to anything, the connect that actually takes its repository on, and the
operator-only convert that turns an agent the room composed into a connected
one.

Both run the same scan server-side. A report the browser is holding is a
claim about a repository some minutes ago, and the route has to answer for
the repository now, so the connect route never trusts the form's copy of it.

The group gate is the create route's own, factored into one helper both
routes call rather than copied, so the consent branch of it stays pinned
where it already was (`test_web_visibility.py`'s create-route cases); what
these tests pin is that the connect route asks the same question at all.

Same no-real-sleep discipline as test_api_agents.py: the pipeline's poll
loops must never block, and `_poll_until` is the only place that sleeps for
real, waiting on the pipeline's daemon thread.
"""

from __future__ import annotations

import logging
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.clients import Clients, InstallationRepo
from kobo_api.config import Config, GroupChoice
from kobo_api.connect_scan import (
    ADDS,
    ADDS_PREAMBLE,
    NO_DOCKERFILE,
    REPO_NOT_IN_ORG,
    REPO_NOT_VISIBLE,
)
from kobo_api.iap import ASSERTION_HEADER, IAP_ISSUER
from kobo_api.kinds import (
    KIND_COMPOSED,
    KIND_CONNECTED,
    RUN_MODE_HEADLESS,
    RUN_MODE_HTTP,
)
from kobo_api.main import (
    GITHUB_UNREACHABLE,
    MAX_SECRET_NAMES,
    REPO_SEARCH_LIMIT,
    REPOS_UNREADABLE,
    SCAN_LIMIT,
    create_app,
)
from kobo_api.mcp import rate_limit
from kobo_api.models import Agent, AgentStatus, Base
from kobo_api.repo_search import MAX_MATCHES, RECENT_MATCHES, TTL_S, RepoIndex
from tests.fakes import (
    FakeAgent,
    FakeCabal,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeImage,
    FakeKubectl,
)

AGENT_NAME = "clio"
ORG = "example-org"
REPO = f"{ORG}/team-chatbot"

CONNECTED_STEP_NAMES = [
    "connect", "avatar", "onboard", "access", "wire", "build", "deploy",
]

POLL_DEADLINE_S = 5
POLL_INTERVAL_S = 0.02


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    import kobo_api.steps as steps_module

    monkeypatch.setattr(steps_module, "_sleep", lambda seconds: None)


@pytest.fixture(autouse=True)
def _fresh_scan_window():
    """The scan limiter's window is module state shared by every caller in
    the process, so each case starts with an empty one rather than one
    carrying the previous case's scans."""
    rate_limit.reset()
    yield
    rate_limit.reset()


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config(tmp_path):
    return Config(
        workdir=tmp_path / "work",
        skills_bucket="skills-bucket",
        personas_bucket="personas-bucket",
        github_org=ORG,
    )


@pytest.fixture
def github():
    return FakeGitHub(existing_repos={REPO})


@pytest.fixture
def clients(github, fake_storage):
    return Clients(
        github=github,
        gojira=FakeGojira(activate_after=1),
        cloud=FakeCloud(),
        agent=FakeAgent(reply="hello yourself, human"),
        kubectl=FakeKubectl(),
        storage=fake_storage,
        image=FakeImage(),
    )


@pytest.fixture
def client(session_factory, config, clients):
    return TestClient(create_app(session_factory, config=config, clients=clients))


@pytest.fixture
def client_for(session_factory, config, fake_storage):
    """A client over one particular repository, for the cases that are about
    what the scan finds in it rather than about the route."""

    def _build(github: FakeGitHub) -> TestClient:
        clients = Clients(
            github=github,
            gojira=FakeGojira(activate_after=1),
            cloud=FakeCloud(),
            agent=FakeAgent(reply="hello yourself, human"),
            kubectl=FakeKubectl(),
            storage=fake_storage,
            image=FakeImage(),
        )
        return TestClient(create_app(session_factory, config=config, clients=clients))

    return _build


def _payload(**overrides) -> dict:
    payload = {
        "repo": REPO,
        "name": AGENT_NAME,
        "run_mode": RUN_MODE_HTTP,
        # The room cannot check who owns a repository, so every connect
        # carries the caller's word that they may. Tests for its absence
        # are in their own section below.
        "confirm_repo_owner": True,
    }
    payload.update(overrides)
    return payload


def _row(session_factory, name=AGENT_NAME) -> Agent | None:
    session = session_factory()
    try:
        return session.query(Agent).filter_by(name=name).first()
    finally:
        session.close()


def _poll_until(client, name, statuses, deadline_s=POLL_DEADLINE_S):
    deadline = time.monotonic() + deadline_s
    body = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/agents/{name}")
        assert response.status_code == 200
        body = response.json()
        if body["status"] in statuses:
            return body
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"agent {name!r} did not reach {statuses}: {body}")


# --- POST /api/connect/scan -------------------------------------------------------


def test_scan_reports_nothing_for_a_repository_the_room_can_work_with(client):
    response = client.post("/api/connect/scan", json=_payload())

    assert response.status_code == 200
    assert response.json() == {
        "blockers": [],
        "warnings": [],
        "adds": ADDS,
        "adds_preamble": ADDS_PREAMBLE,
        "repo": REPO,
    }


def test_scan_reports_blockers_with_their_codes_and_their_sentences(client_for):
    client = client_for(FakeGitHub())

    response = client.post("/api/connect/scan", json=_payload())

    assert response.status_code == 200
    body = response.json()
    assert [blocker["code"] for blocker in body["blockers"]] == [REPO_NOT_VISIBLE]
    assert "could not be reached" in body["blockers"][0]["message"]


def test_scan_reports_warnings_without_blocking(client_for):
    client = client_for(
        FakeGitHub(existing_repos={REPO}, root_entries=["Dockerfile", ".env"])
    )

    body = client.post("/api/connect/scan", json=_payload()).json()

    assert body["blockers"] == []
    assert [warning["code"] for warning in body["warnings"]] == ["env_file_committed"]


def test_scan_refuses_a_name_that_could_never_be_an_agent(client):
    response = client.post("/api/connect/scan", json=_payload(name="Not A Name"))

    assert response.status_code == 422
    assert "invalid name" in response.json()["error"]


def test_scan_refuses_a_run_mode_the_room_does_not_have(client):
    response = client.post("/api/connect/scan", json=_payload(run_mode="magic"))

    assert response.status_code == 422
    assert response.json()["error"] == (
        "pick how this agent runs: it serves HTTP, or it has no HTTP port"
    )


def test_scan_reads_only_as_often_as_a_person_could_mean_to(client):
    """The scan reads private repositories with the room's own credentials,
    which makes it an oracle worth spending a moment's thought on. A window
    of a few scans a minute is far past any form and far below anything
    that could walk the organisation."""
    for _ in range(SCAN_LIMIT):
        assert client.post("/api/connect/scan", json=_payload()).status_code == 200

    response = client.post("/api/connect/scan", json=_payload())

    assert response.status_code == 429
    assert response.json() == {"error": "that is a lot of scans. Wait a minute"}


def test_a_refused_scan_never_reads_the_repository(client, github):
    for _ in range(SCAN_LIMIT):
        client.post("/api/connect/scan", json=_payload())
    github.calls.clear()

    client.post("/api/connect/scan", json=_payload())

    assert github.calls == []


def test_scan_answers_502_when_github_cannot_be_read(client_for):
    """A backend that is down is not a finding about the repository, and a
    report saying "nothing wrong" would be a lie. Same 502-with-a-plain-
    sentence shape as every other backend failure here."""
    client = client_for(
        FakeGitHub(existing_repos={REPO}, repo_access_error="gh: gateway timeout")
    )

    response = client.post("/api/connect/scan", json=_payload())

    assert response.status_code == 502
    assert response.json() == {
        "error": "the repository could not be read right now, try again shortly"
    }


def test_scan_never_writes_anything(client, github, session_factory):
    client.post("/api/connect/scan", json=_payload())

    assert github.cloned == []
    assert github.pushes == []
    assert github.created_from_dir == []
    assert _row(session_factory) is None


# --- GET /api/connect/repos -------------------------------------------------------
#
# The repository field's typeahead. Open to any signed-in person and not
# group-gated: it only ever names repositories the room's App can already
# reach -- a short glance at the most recent ones for a field nobody has
# typed into yet, and matches for what somebody is typing. The matching and
# ordering rules themselves are pinned in test_repo_search.py; what these
# pin is the route -- its shape, its bounds, and its cache.


def _typeahead_repo(full_name: str, **overrides) -> InstallationRepo:
    return InstallationRepo(
        full_name=full_name,
        archived=overrides.get("archived", False),
        private=overrides.get("private", True),
        default_branch=overrides.get("default_branch", "main"),
        pushed_at=overrides.get("pushed_at", "2026-01-01T00:00:00Z"),
    )


def test_repos_offers_the_most_recent_for_an_empty_query(client_for):
    """What a person sees the moment they click into the field: a few
    repositories the room can already reach, and how many there are. A
    blank box reads as "the room has nothing", which was never true."""
    github = FakeGitHub(
        existing_repos={REPO},
        installation_repos=[
            _typeahead_repo(f"{ORG}/warden", pushed_at="2019-01-01T00:00:00Z"),
            _typeahead_repo(f"{ORG}/team-chatbot", pushed_at="2026-09-01T00:00:00Z"),
        ],
    )

    response = client_for(github).get("/api/connect/repos?q=")

    assert response.status_code == 200
    assert response.json() == {
        "repos": [
            {"full_name": f"{ORG}/team-chatbot", "private": True},
            {"full_name": f"{ORG}/warden", "private": True},
        ],
        "truncated": False,
        "total": 2,
    }


def test_repos_offers_at_most_eight_for_an_empty_query(client_for):
    """The rows are a way in, not an index. The total beside them is what
    says how much more there is."""
    github = FakeGitHub(
        existing_repos={REPO},
        installation_repos=[
            _typeahead_repo(f"{ORG}/repo-{i:02d}") for i in range(RECENT_MATCHES + 5)
        ],
    )

    body = client_for(github).get("/api/connect/repos?q=").json()

    assert len(body["repos"]) == RECENT_MATCHES
    assert body["truncated"] is True
    assert body["total"] == RECENT_MATCHES + 5


def test_repos_answers_an_empty_query_from_a_warm_listing_without_asking_github(
    client_for,
):
    """Focus is not worth a GitHub read. The warmer has the listing in
    hand before anyone opens the form, and every focus after that is free
    -- which is the rule this route has to keep, since an empty query is
    not rate limited."""
    github = FakeGitHub(
        existing_repos={REPO},
        installation_repos=[_typeahead_repo(f"{ORG}/team-chatbot")],
    )
    typeahead = client_for(github)
    typeahead.app.state.repo_index.warm()
    assert github.calls.count("list_installation_repos") == 1

    for _ in range(5):
        assert typeahead.get("/api/connect/repos?q=").status_code == 200

    assert github.calls.count("list_installation_repos") == 1


def test_repos_answers_the_same_when_the_query_is_left_off_entirely(client):
    response = client.get("/api/connect/repos")

    assert response.status_code == 200
    assert response.json() == {
        "repos": [{"full_name": REPO, "private": True}],
        "truncated": False,
        "total": 1,
    }


def test_repos_names_the_matches_best_first(client_for):
    github = FakeGitHub(
        existing_repos={REPO},
        installation_repos=[
            _typeahead_repo(f"{ORG}/team-chatbot"),
            _typeahead_repo(f"{ORG}/warden"),
            _typeahead_repo(
                f"{ORG}/chatbot-archive",
                private=False,
                default_branch="trunk",
                pushed_at="2019-01-01T00:00:00Z",
            ),
        ],
    )

    response = client_for(github).get("/api/connect/repos?q=chat")

    assert response.status_code == 200
    assert response.json() == {
        "repos": [
            {"full_name": f"{ORG}/chatbot-archive", "private": False},
            {"full_name": f"{ORG}/team-chatbot", "private": True},
        ],
        "truncated": False,
        "total": 3,
    }


def test_repos_says_when_more_matched_than_it_shows(client_for):
    github = FakeGitHub(
        existing_repos={REPO},
        installation_repos=[
            _typeahead_repo(f"{ORG}/chat-{i:02d}") for i in range(MAX_MATCHES + 4)
        ],
    )

    body = client_for(github).get("/api/connect/repos?q=chat").json()

    assert len(body["repos"]) == MAX_MATCHES
    assert body["truncated"] is True


def test_repos_refuses_a_query_longer_than_the_bound(client):
    response = client.get("/api/connect/repos", params={"q": "x" * 101})

    assert response.status_code == 422


def test_repos_accepts_a_query_right_at_the_bound(client):
    """Taken and answered, rather than refused: a hundred characters is a
    query nothing could match, not a query nobody may ask."""
    response = client.get("/api/connect/repos", params={"q": "x" * 100})

    assert response.status_code == 200
    assert response.json() == {"repos": [], "truncated": False, "total": 1}


def test_repos_says_so_when_the_room_has_no_github_at_all(session_factory, config):
    """Same sentence and same status the scan gives for it: the room cannot
    answer anything about GitHub, and the field says so rather than reading
    as an installation with nothing in it."""
    app_client = TestClient(create_app(session_factory, config=config, clients=None))

    response = app_client.get("/api/connect/repos?q=chat")

    assert response.status_code == 503
    assert response.json() == {"error": GITHUB_UNREACHABLE}


def test_repos_says_so_for_an_empty_query_too_when_the_room_has_no_github(
    session_factory, config
):
    """The empty query used to be answered before the client was even
    looked at. It reads the listing now, so it has to say the same thing
    the typed query says rather than an empty list that reads as "no
    repositories"."""
    app_client = TestClient(create_app(session_factory, config=config, clients=None))

    response = app_client.get("/api/connect/repos?q=")

    assert response.status_code == 503
    assert response.json() == {"error": GITHUB_UNREACHABLE}


def test_repos_says_so_when_github_will_not_answer(client_for):
    github = FakeGitHub(
        existing_repos={REPO},
        installation_repos_error="gh: Bad credentials (HTTP 401)",
    )

    response = client_for(github).get("/api/connect/repos?q=chat")

    assert response.status_code == 502
    assert response.json() == {"error": REPOS_UNREADABLE}


def test_repos_is_not_group_gated(grouped_client):
    """A room that requires a group for every agent still answers this: it
    names nothing the App could not already read, and a person has to be
    signed in to ask at all."""
    response = grouped_client.get("/api/connect/repos?q=team")

    assert response.status_code == 200
    assert response.json() == {
        "repos": [{"full_name": REPO, "private": True}],
        "truncated": False,
        "total": 1,
    }


def test_repos_searches_only_as_often_as_a_person_could_type(client):
    """A ceiling of its own, on its own bucket. The prefix rule is what
    keeps this from being a listing; the limit is what keeps somebody from
    walking the alphabet through it anyway."""
    for _ in range(REPO_SEARCH_LIMIT):
        assert client.get("/api/connect/repos?q=team").status_code == 200

    response = client.get("/api/connect/repos?q=team")

    assert response.status_code == 429
    assert response.json() == {"error": "that is a lot of searching. Wait a minute"}


def test_a_refused_search_does_not_spend_the_rooms_scans(client):
    """Its own bucket: somebody who has typed a lot can still scan the
    repository they were looking for."""
    for _ in range(REPO_SEARCH_LIMIT + 1):
        client.get("/api/connect/repos?q=team")

    assert client.post("/api/connect/scan", json=_payload()).status_code == 200


def test_an_empty_query_is_not_counted_against_the_search_limit(client):
    """It reads nothing and answers nothing, so it costs nothing."""
    for _ in range(REPO_SEARCH_LIMIT + 5):
        assert client.get("/api/connect/repos?q=").status_code == 200

    assert client.get("/api/connect/repos?q=team").status_code == 200


def test_repos_reads_the_listing_once_inside_the_ttl_and_again_after(client_for):
    """One GitHub listing serves the whole room for five minutes. The clock
    is injected rather than slept through."""
    github = FakeGitHub(
        existing_repos={REPO},
        installation_repos=[_typeahead_repo(f"{ORG}/team-chatbot")],
    )
    typeahead = client_for(github)
    clock = {"now": 0.0}
    typeahead.app.state.repo_index = RepoIndex(github, now=lambda: clock["now"])

    typeahead.get("/api/connect/repos?q=chat")
    clock["now"] = TTL_S - 1
    typeahead.get("/api/connect/repos?q=chatb")

    assert github.calls.count("list_installation_repos") == 1

    clock["now"] = TTL_S
    typeahead.get("/api/connect/repos?q=chatbo")

    assert github.calls.count("list_installation_repos") == 2


# --- POST /api/agents/connect -----------------------------------------------------


def test_connect_returns_201_pending_then_provisions_to_ready(
    client, github, session_factory
):
    response = client.post("/api/agents/connect", json=_payload())

    assert response.status_code == 201, response.text
    assert response.json() == {"name": AGENT_NAME, "status": "pending"}

    body = _poll_until(client, AGENT_NAME, {"ready", "failed"})
    assert body["status"] == "ready", body
    assert [step["name"] for step in body["steps"]] == CONNECTED_STEP_NAMES
    assert body["kind"] == KIND_CONNECTED


def test_connect_never_creates_or_writes_to_the_repository(client, github):
    client.post("/api/agents/connect", json=_payload())
    _poll_until(client, AGENT_NAME, {"ready", "failed"})

    forbidden = ("create_repo_from_dir:", "commit_push:", "rename_repo:", "archive_repo:")
    assert [call for call in github.calls if call.startswith(forbidden)] == []


def test_connect_records_the_row_a_connected_agent_needs(client, session_factory, config):
    client.post(
        "/api/agents/connect",
        json=_payload(secret_names=["chat-tokens", "billing-api"]),
    )

    agent = _row(session_factory)
    assert agent.kind == KIND_CONNECTED
    assert agent.repo_full_name == REPO
    assert agent.run_mode == RUN_MODE_HTTP
    assert agent.secret_names == ["chat-tokens", "billing-api"]
    assert agent.skills == []
    assert agent.team == "shared"
    # NOT NULL, and a connected agent has none: one fixed value, never a
    # real persona id, so nothing ever tries to resolve it.
    assert agent.persona == "connected"
    assert agent.model_id == config.models[0].id
    assert agent.status in {AgentStatus.PENDING, AgentStatus.PROVISIONING, AgentStatus.READY}


def test_connect_says_who_connected_what(client, caplog):
    """One line per connect, at WARNING because INFO does not reach the pod
    logs here. Arbitrary code with model access is the risk this whole
    feature carries, and the record of who took it on is part of the
    answer."""
    with caplog.at_level(logging.WARNING, logger="kobo_api.main"):
        client.post("/api/agents/connect", json=_payload())

    lines = [record.getMessage() for record in caplog.records if "connect" in record.getMessage()]
    assert any(REPO in line and AGENT_NAME in line for line in lines), lines


def test_connect_records_githubs_own_spelling_of_the_repository(
    client, session_factory
):
    """One repository is one row, whatever case it was typed in."""
    response = client.post(
        "/api/agents/connect", json=_payload(repo=f"{ORG.title()}/Team-Chatbot")
    )

    assert response.status_code == 201, response.text
    assert _row(session_factory).repo_full_name == REPO


# --- who says they may connect it -------------------------------------------------
#
# The room cannot check who owns a repository: its App can write to every
# repository in the organisation it is installed on, and the token it holds
# cannot see who the caller is on GitHub. So the connect asks, records the
# answer in the log line, and says as much in the scan report. An operator
# is exempt, the same standing every other operator gate here grants.


def test_connect_refuses_without_the_callers_word_that_they_may(
    client, session_factory
):
    response = client.post(
        "/api/agents/connect", json=_payload(confirm_repo_owner=False)
    )

    assert response.status_code == 422
    assert response.json() == {
        "error": (
            "confirm that you own this repository or have its owner's agreement"
        )
    }
    assert _row(session_factory) is None


def test_connect_refuses_when_the_question_is_not_answered_at_all(
    client, session_factory
):
    payload = _payload()
    del payload["confirm_repo_owner"]

    response = client.post("/api/agents/connect", json=payload)

    assert response.status_code == 422
    assert _row(session_factory) is None


def test_an_operator_does_not_have_to_answer_for_the_repository(
    client, session_factory, monkeypatch
):
    import kobo_api.main as main_module

    monkeypatch.setattr(main_module, "_is_admin", lambda request: True)

    response = client.post(
        "/api/agents/connect", json=_payload(confirm_repo_owner=False)
    )

    assert response.status_code == 201, response.text
    assert _row(session_factory) is not None


def test_the_refusal_costs_no_github_reads(client, github):
    client.post("/api/agents/connect", json=_payload(confirm_repo_owner=False))

    assert github.calls == []


# --- refusals ---------------------------------------------------------------------


def test_connect_refuses_an_invalid_name(client, session_factory):
    response = client.post("/api/agents/connect", json=_payload(name="Not A Name"))

    assert response.status_code == 422
    assert response.json() == {"error": "invalid name (invalid name): 'Not A Name'"}
    assert _row(session_factory, "Not A Name") is None


def test_connect_refuses_a_run_mode_the_room_does_not_have(client):
    response = client.post("/api/agents/connect", json=_payload(run_mode="magic"))

    assert response.status_code == 422


@pytest.mark.parametrize(
    "secret_name",
    ["Chat-Tokens", "chat_tokens", "-chat", "chat-", "chat tokens", "", "a" * 64],
)
def test_connect_refuses_a_secret_name_kubernetes_would_not_take(
    client, session_factory, secret_name
):
    response = client.post(
        "/api/agents/connect", json=_payload(secret_names=[secret_name])
    )

    assert response.status_code == 422, response.text
    assert "secret name" in response.json()["error"]
    assert _row(session_factory) is None


def test_connect_refuses_more_secret_names_than_a_pod_should_carry(client):
    response = client.post(
        "/api/agents/connect",
        json=_payload(secret_names=[f"secret-{index}" for index in range(9)]),
    )

    assert response.status_code == 422
    assert response.json()["error"] == "this room takes at most 8 secret names"


def test_connect_counts_the_secret_names_it_was_given_before_it_dedupes_them(
    client, session_factory
):
    """A pasted list of a hundred copies of one name is still a pasted list
    of a hundred names, and the cap is there to bound what arrives, not
    what survives."""
    response = client.post(
        "/api/agents/connect",
        json=_payload(secret_names=["chat-tokens"] * (MAX_SECRET_NAMES + 1)),
    )

    assert response.status_code == 422
    assert response.json()["error"] == "this room takes at most 8 secret names"
    assert _row(session_factory) is None


def test_connect_keeps_one_of_each_secret_name_in_the_order_they_were_given(
    client, session_factory
):
    client.post(
        "/api/agents/connect",
        json=_payload(secret_names=["billing-api", "chat-tokens", "billing-api"]),
    )

    assert _row(session_factory).secret_names == ["billing-api", "chat-tokens"]


def test_connect_refuses_with_the_blockers_and_their_sentences(
    client_for, session_factory
):
    """The refusal carries what the form would have shown, so the UI never
    has to scan a second time to say why."""
    client = client_for(
        FakeGitHub(existing_repos={REPO}, root_entries=["README.md"])
    )

    response = client.post("/api/agents/connect", json=_payload())

    assert response.status_code == 422
    body = response.json()
    assert [blocker["code"] for blocker in body["blockers"]] == [NO_DOCKERFILE]
    assert body["blockers"][0]["message"].startswith("There is no Dockerfile")
    assert body["error"]
    assert _row(session_factory) is None


def test_connect_refuses_a_repository_in_another_organisation(client, session_factory):
    response = client.post(
        "/api/agents/connect", json=_payload(repo="elsewhere/team-chatbot")
    )

    assert response.status_code == 422
    assert [b["code"] for b in response.json()["blockers"]] == [REPO_NOT_IN_ORG]
    assert _row(session_factory) is None


def test_connect_refuses_headless_until_the_room_has_a_class_for_it(
    client, session_factory
):
    response = client.post("/api/agents/connect", json=_payload(run_mode=RUN_MODE_HEADLESS))

    assert response.status_code == 422
    assert [b["code"] for b in response.json()["blockers"]] == ["headless_unavailable"]
    assert response.json()["blockers"][0]["message"] == (
        "agents with no HTTP port are not available in this room yet"
    )
    assert _row(session_factory) is None


def test_connect_takes_headless_once_the_deploy_platform_names_a_class(
    session_factory, clients, tmp_path, fake_storage
):
    config = Config(
        workdir=tmp_path / "work",
        skills_bucket="skills-bucket",
        personas_bucket="personas-bucket",
        github_org=ORG,
        connected_headless_class="agent-headless",
    )
    client = TestClient(create_app(session_factory, config=config, clients=clients))

    response = client.post("/api/agents/connect", json=_payload(run_mode=RUN_MODE_HEADLESS))

    assert response.status_code == 201, response.text
    assert _row(session_factory).run_mode == RUN_MODE_HEADLESS


def test_connect_answers_502_when_github_cannot_be_read(client_for, session_factory):
    client = client_for(
        FakeGitHub(existing_repos={REPO}, repo_access_error="gh: gateway timeout")
    )

    response = client.post("/api/agents/connect", json=_payload())

    assert response.status_code == 502
    assert response.json() == {
        "error": "the repository could not be read right now, try again shortly"
    }
    assert _row(session_factory) is None


def test_connect_refuses_a_repository_this_room_already_holds(
    client, session_factory
):
    """One repository is one agent. The second row would deploy over the
    first, and destroying either would act on the other's app."""
    session = session_factory()
    session.add(
        Agent(
            name="atlas", persona="connected", model_id="m", skills=[],
            repo_full_name=REPO, status=AgentStatus.READY,
        )
    )
    session.commit()
    session.close()

    response = client.post("/api/agents/connect", json=_payload())

    assert response.status_code == 422
    assert [b["code"] for b in response.json()["blockers"]] == ["already_deployed"]
    assert _row(session_factory) is None


def test_connect_refuses_a_name_already_in_the_room(client, session_factory):
    session = session_factory()
    session.add(
        Agent(name=AGENT_NAME, persona="helpful-generalist", model_id="m", skills=[])
    )
    session.commit()
    session.close()

    response = client.post("/api/agents/connect", json=_payload())

    assert response.status_code == 409
    assert response.json() == {"error": f"name taken: {AGENT_NAME!r}"}


# --- the group gate ---------------------------------------------------------------


@pytest.fixture
def grouped_client(session_factory, clients, tmp_path, fake_storage):
    config = Config(
        workdir=tmp_path / "work",
        skills_bucket="skills-bucket",
        personas_bucket="personas-bucket",
        github_org=ORG,
        groups=[GroupChoice(name="payments-eng", okta_group="okta-payments")],
    )
    return TestClient(create_app(session_factory, config=config, clients=clients))


def test_connect_asks_for_a_group_once_the_room_has_named_some(
    grouped_client, session_factory
):
    response = grouped_client.post("/api/agents/connect", json=_payload())

    assert response.status_code == 422
    assert response.json() == {"error": "pick a group for this agent"}
    assert _row(session_factory) is None


def test_connect_refuses_a_group_this_room_has_never_heard_of(grouped_client):
    response = grouped_client.post(
        "/api/agents/connect", json=_payload(group_name="not-a-group")
    )

    assert response.status_code == 422
    assert response.json() == {"error": "unknown group: 'not-a-group'"}


def test_scan_asks_the_same_group_question_as_connect(grouped_client, github):
    """The scan reads private repositories with the room's own credentials.
    A caller who could not connect has no business asking it what is in
    one, so it is gated exactly as the connect is."""
    response = grouped_client.post("/api/connect/scan", json=_payload())

    assert response.status_code == 422
    assert response.json() == {"error": "pick a group for this agent"}
    assert github.calls == []


def test_scan_refuses_a_group_this_room_has_never_heard_of(grouped_client):
    response = grouped_client.post(
        "/api/connect/scan", json=_payload(group_name="not-a-group")
    )

    assert response.status_code == 422
    assert response.json() == {"error": "unknown group: 'not-a-group'"}


def test_scan_runs_for_a_group_the_room_has_named(grouped_client):
    response = grouped_client.post(
        "/api/connect/scan", json=_payload(group_name="payments-eng")
    )

    assert response.status_code == 200
    assert response.json()["blockers"] == []


def test_connect_takes_a_group_the_room_has_named(grouped_client, session_factory):
    response = grouped_client.post(
        "/api/agents/connect", json=_payload(group_name="payments-eng")
    )

    assert response.status_code == 201, response.text
    assert _row(session_factory).group_name == "payments-eng"


# --- POST /api/agents/{name}/convert-to-connected ----------------------------------
#
# The one operator action for an agent the room composed whose repository has
# since stopped running the room's runtime (design 2026-09-21, "Converting an
# existing agent"). It changes one row and retires one directory entry.
# Nothing is redeployed, no repository is touched, and the room cannot undo
# it.
#
# The gate is the room's operator gate, proven here through the real IAP
# middleware with a real ES256 keypair (the idiom test_web_destroy.py
# established) rather than a mock, since "operator-only" is the whole of the
# protection this route has.

OPERATOR_EMAIL = "Ops@Example.com"
NON_OPERATOR_EMAIL = "someone-else@example.com"
AUDIENCE = "test-audience"
KID = "test-key-1"

CONVERT_PATH = f"/api/agents/{AGENT_NAME}/convert-to-connected"

IN_FLIGHT_409 = {
    "error": "this agent is still being created or destroyed, so it cannot be converted yet"
}
ALREADY_409 = {"error": "this agent is already connected"}


def _generate_jwk(kid: str = KID):
    private_key = ec.generate_private_key(ec.SECP256R1())
    jwk = jwt.algorithms.ECAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk["kid"] = kid
    jwk["use"] = "sig"
    jwk["alg"] = "ES256"
    return private_key, jwk


def _sign(private_key, *, email: str | None, kid: str = KID) -> str:
    now = int(time.time())
    claims = {
        "aud": AUDIENCE,
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


def _seed_composed(
    session_factory,
    *,
    status=AgentStatus.READY,
    kind=KIND_COMPOSED,
    name=AGENT_NAME,
    secret_names=None,
    group_name=None,
):
    """One agent row as the create route leaves it: the room's own repository,
    a real persona, and whatever status this case is about."""
    session = session_factory()
    session.add(
        Agent(
            name=name,
            persona="helpful-generalist",
            model_id="m",
            skills=[],
            kind=kind,
            repo_full_name=f"{ORG}/agent-{name}",
            status=status,
            group_name=group_name,
            secret_names=secret_names if secret_names is not None else [],
            run_mode=RUN_MODE_HTTP if kind == KIND_CONNECTED else None,
        )
    )
    session.commit()
    session.close()


@pytest.fixture
def switchboard(session_factory, config, fake_storage, monkeypatch):
    """A room with the switchboard wired, the fakes the convert route must
    not touch, and a client over both.

    The switchboard has to be genuinely on for the retire to be attempted at
    all (`cabal_enabled` reads two config keys and the operator token), and
    the point of most of these cases is what the route does NOT do, which
    only means something when the clients that could have done it are there.
    """
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = config.model_copy(
        update={
            "cabal_url": "http://cabal.internal",
            "agent_endpoint_template": "http://agent-{name}.internal",
        }
    )
    fakes = SimpleNamespace(
        github=FakeGitHub(existing_repos={REPO}),
        gojira=FakeGojira(),
        kubectl=FakeKubectl(),
        cabal=FakeCabal(),
        config=cfg,
    )
    clients = Clients(
        github=fakes.github,
        gojira=fakes.gojira,
        cloud=FakeCloud(),
        agent=FakeAgent(),
        kubectl=fakes.kubectl,
        storage=fake_storage,
        image=FakeImage(),
        cabal=fakes.cabal,
    )
    fakes.client = TestClient(create_app(session_factory, config=cfg, clients=clients))
    return fakes


@pytest.fixture
def gated(session_factory, config, clients):
    """The same room with the operator gate genuinely on: named operators and
    IAP, so every request has to carry a signed assertion."""
    private_key, jwk = _generate_jwk()
    cfg = config.model_copy(
        update={"operators": [OPERATOR_EMAIL], "iap_audience": AUDIENCE}
    )
    client = TestClient(
        create_app(
            session_factory,
            config=cfg,
            clients=clients,
            iap_jwks_fetcher=_fetcher(jwk),
        )
    )
    return SimpleNamespace(
        client=client,
        headers=lambda email: {ASSERTION_HEADER: _sign(private_key, email=email)},
    )


# --- the gate ---------------------------------------------------------------------


def test_convert_refuses_a_caller_who_is_not_an_operator(gated, session_factory):
    _seed_composed(session_factory)

    response = gated.client.post(
        CONVERT_PATH,
        json={"run_mode": RUN_MODE_HTTP},
        headers=gated.headers(NON_OPERATOR_EMAIL),
    )

    assert response.status_code == 403
    assert response.json() == {
        "error": "Converting agents is operator-only on this room"
    }
    assert _row(session_factory).kind == KIND_COMPOSED


def test_the_page_hides_the_action_from_exactly_the_caller_the_route_refuses(
    gated, session_factory
):
    """The page has no signal of its own for this: it reads `destroy_visible`,
    the operator flag already on the detail. That is only honest while the
    two gates agree, so the same caller who is refused above must also see
    the flag false here."""
    _seed_composed(session_factory)

    response = gated.client.get(
        f"/api/agents/{AGENT_NAME}", headers=gated.headers(NON_OPERATOR_EMAIL)
    )

    assert response.status_code == 200
    assert response.json()["destroy_visible"] is False


def test_an_operator_may_convert(gated, session_factory):
    _seed_composed(session_factory)

    response = gated.client.post(
        CONVERT_PATH,
        json={"run_mode": RUN_MODE_HTTP},
        headers=gated.headers(OPERATOR_EMAIL.lower()),
    )

    assert response.status_code == 200, response.text
    assert _row(session_factory).kind == KIND_CONNECTED


def test_a_caller_who_is_not_an_operator_is_refused_before_the_agent_is_looked_up(
    gated
):
    """Info-safe, the same discipline destroy's own gate keeps: a
    non-operator learns nothing about which agents exist."""
    response = gated.client.post(
        "/api/agents/no-such-agent/convert-to-connected",
        json={"run_mode": RUN_MODE_HTTP},
        headers=gated.headers(NON_OPERATOR_EMAIL),
    )

    assert response.status_code == 403


# --- refusals ---------------------------------------------------------------------


def test_convert_refuses_an_agent_that_is_already_connected(client, session_factory):
    _seed_composed(session_factory, kind=KIND_CONNECTED)

    response = client.post(CONVERT_PATH, json={"run_mode": RUN_MODE_HTTP})

    assert response.status_code == 409
    assert response.json() == ALREADY_409


@pytest.mark.parametrize(
    "status",
    [
        AgentStatus.PENDING,
        AgentStatus.PROVISIONING,
        AgentStatus.DESTROYING,
        AgentStatus.DESTROY_FAILED,
    ],
)
def test_convert_refuses_an_agent_mid_flight(client, session_factory, status):
    """Provisioning and destroying both have a thread of their own holding
    this row, and both read the kind as they go."""
    _seed_composed(session_factory, status=status)

    response = client.post(CONVERT_PATH, json={"run_mode": RUN_MODE_HTTP})

    assert response.status_code == 409
    assert response.json() == IN_FLIGHT_409
    assert _row(session_factory).kind == KIND_COMPOSED


@pytest.mark.parametrize("status", [AgentStatus.READY, AgentStatus.FAILED])
def test_convert_takes_a_ready_or_failed_agent(client, session_factory, status):
    """A failed agent is the likelier case of the two: its repository stopped
    running the room's runtime, which is how it failed in the first place."""
    _seed_composed(session_factory, status=status)

    response = client.post(CONVERT_PATH, json={"run_mode": RUN_MODE_HTTP})

    assert response.status_code == 200, response.text
    assert _row(session_factory).kind == KIND_CONNECTED


def test_convert_refuses_a_run_mode_the_room_does_not_have(client, session_factory):
    _seed_composed(session_factory)

    response = client.post(CONVERT_PATH, json={"run_mode": "magic"})

    assert response.status_code == 422
    assert response.json() == {
        "error": "pick how this agent runs: it serves HTTP, or it has no HTTP port"
    }
    assert _row(session_factory).kind == KIND_COMPOSED


def test_convert_refuses_headless_whatever_the_room_has_a_class_for(
    client, session_factory
):
    """An agent the room composed serves HTTP by construction, and nothing
    is redeployed here, so a headless choice would not take effect until
    some later retry and would then fail a probe nobody was expecting."""
    _seed_composed(session_factory)

    response = client.post(CONVERT_PATH, json={"run_mode": RUN_MODE_HEADLESS})

    assert response.status_code == 422
    assert response.json() == {
        "error": "an agent the room composed serves web traffic; convert it as one"
    }
    assert _row(session_factory).kind == KIND_COMPOSED


def test_convert_refuses_headless_even_where_the_room_runs_headless_agents(
    session_factory, clients, tmp_path
):
    """The refusal is about where this agent came from, not about what the
    room can run: a room with a headless class still converts as HTTP."""
    config = Config(
        workdir=tmp_path / "work",
        skills_bucket="skills-bucket",
        personas_bucket="personas-bucket",
        github_org=ORG,
        connected_headless_class="agent-headless",
    )
    client = TestClient(create_app(session_factory, config=config, clients=clients))
    _seed_composed(session_factory)

    response = client.post(CONVERT_PATH, json={"run_mode": RUN_MODE_HEADLESS})

    assert response.status_code == 422
    assert _row(session_factory).kind == KIND_COMPOSED


def test_convert_404s_on_an_agent_this_room_does_not_have(client):
    response = client.post(
        "/api/agents/no-such-agent/convert-to-connected",
        json={"run_mode": RUN_MODE_HTTP},
    )

    assert response.status_code == 404


# --- what it actually does --------------------------------------------------------


def test_convert_answers_with_the_two_things_it_changed(switchboard, session_factory):
    _seed_composed(session_factory)

    response = switchboard.client.post(CONVERT_PATH, json={"run_mode": RUN_MODE_HTTP})

    assert response.status_code == 200, response.text
    assert response.json() == {
        "name": AGENT_NAME,
        "kind": KIND_CONNECTED,
        "run_mode": RUN_MODE_HTTP,
    }


def test_convert_leaves_the_secret_names_alone(switchboard, session_factory):
    """Nothing about the deployment changes here, so the names of the Secrets
    it already mounts are not the room's to rewrite."""
    _seed_composed(session_factory, secret_names=["chat-tokens"])

    switchboard.client.post(CONVERT_PATH, json={"run_mode": RUN_MODE_HTTP})

    assert _row(session_factory).secret_names == ["chat-tokens"]


def test_convert_deploys_nothing_and_touches_no_repository(
    switchboard, session_factory
):
    _seed_composed(session_factory)

    switchboard.client.post(CONVERT_PATH, json={"run_mode": RUN_MODE_HTTP})

    assert switchboard.github.calls == []
    assert switchboard.github.cloned == []
    assert switchboard.github.pushes == []
    assert switchboard.github.created_from_dir == []
    assert switchboard.gojira.calls == []
    assert switchboard.kubectl.calls == []
    assert switchboard.kubectl.restarts == []


def test_convert_retires_the_switchboard_identity(switchboard, session_factory):
    """A connected agent has no directory entry: the pipeline that builds one
    has no directory step. An agent that was composed does have one, and it
    stops being true the moment the kind changes."""
    _seed_composed(session_factory)

    switchboard.client.post(CONVERT_PATH, json={"run_mode": RUN_MODE_HTTP})

    assert switchboard.cabal.absorb_calls == [AGENT_NAME]


def test_a_switchboard_that_cannot_be_reached_does_not_fail_the_convert(
    session_factory, config, fake_storage, monkeypatch
):
    """Best effort, the same standing the destroy ledger's own directory step
    has: the row is the thing that matters, and a stale directory entry is
    resolved by a later sync rather than by holding this up."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = config.model_copy(
        update={
            "cabal_url": "http://cabal.internal",
            "agent_endpoint_template": "http://agent-{name}.internal",
        }
    )
    clients = Clients(
        github=FakeGitHub(),
        gojira=FakeGojira(),
        cloud=FakeCloud(),
        agent=FakeAgent(),
        kubectl=FakeKubectl(),
        storage=fake_storage,
        image=FakeImage(),
        cabal=FakeCabal(absorb_error="switchboard is down"),
    )
    client = TestClient(create_app(session_factory, config=cfg, clients=clients))
    _seed_composed(session_factory)

    response = client.post(CONVERT_PATH, json={"run_mode": RUN_MODE_HTTP})

    assert response.status_code == 200, response.text
    assert _row(session_factory).kind == KIND_CONNECTED


def test_convert_says_who_converted_what(gated, session_factory, caplog):
    """One line per convert, at WARNING for the same reason the connect's own
    line is: INFO does not reach the pod logs here, and this is the one
    record that an agent's page went quiet on purpose. Run through the
    IAP-gated client so the name in the line is a real verified caller
    rather than the no-perimeter fallback."""
    _seed_composed(session_factory)

    with caplog.at_level(logging.WARNING, logger="kobo_api.main"):
        gated.client.post(
            CONVERT_PATH,
            json={"run_mode": RUN_MODE_HTTP},
            headers=gated.headers(OPERATOR_EMAIL.lower()),
        )

    lines = [
        record.getMessage()
        for record in caplog.records
        if "convert" in record.getMessage()
    ]
    assert any(
        AGENT_NAME in line and OPERATOR_EMAIL.lower() in line for line in lines
    ), lines


def test_the_page_reads_as_connected_afterwards(client, session_factory):
    _seed_composed(session_factory)

    client.post(CONVERT_PATH, json={"run_mode": RUN_MODE_HTTP})

    body = client.get(f"/api/agents/{AGENT_NAME}").json()
    assert body["kind"] == KIND_CONNECTED


# --- unplugged from its colleagues ------------------------------------------------
#
# A connected agent has no colleague wiring: cabal holds no inbound route to
# it, and the room never wrote it a roster. A converted one has to lose the
# wiring it used to have, on the wire AND on the page every colleague reads,
# or it stays a name its group can dispatch to and nothing answers.

GROUP = "payments-eng"
PEER = "ward"


def _seed_peer(session_factory, *, name=PEER, group_name=GROUP):
    session = session_factory()
    session.add(
        Agent(
            name=name,
            persona="helpful-generalist",
            model_id="m",
            skills=[],
            repo_full_name=f"{ORG}/agent-{name}",
            status=AgentStatus.READY,
            group_name=group_name,
        )
    )
    session.commit()
    session.close()


def test_convert_takes_the_agent_off_the_dispatch_pairs_in_both_directions(
    switchboard, session_factory
):
    _seed_composed(session_factory, group_name=GROUP)
    _seed_peer(session_factory)

    switchboard.client.post(CONVERT_PATH, json={"run_mode": RUN_MODE_HTTP})

    assert switchboard.cabal.put_dispatch_pairs_calls, "no pair set was pushed"
    pushed = switchboard.cabal.put_dispatch_pairs_calls[-1]
    assert (f"agent:{AGENT_NAME}", PEER) not in pushed
    assert (f"agent:{PEER}", AGENT_NAME) not in pushed


def test_convert_leaves_the_rest_of_the_group_paired_with_each_other(
    switchboard, session_factory
):
    """One agent leaving is not the group being unplugged."""
    _seed_composed(session_factory, group_name=GROUP)
    _seed_peer(session_factory)
    _seed_peer(session_factory, name="atlas")

    switchboard.client.post(CONVERT_PATH, json={"run_mode": RUN_MODE_HTTP})

    pushed = switchboard.cabal.put_dispatch_pairs_calls[-1]
    assert (f"agent:{PEER}", "atlas") in pushed
    assert ("agent:atlas", PEER) in pushed


def test_convert_redelivers_the_roster_to_every_colleague_who_could_see_it(
    switchboard, session_factory
):
    """Best effort, over the relay, the same fast lane a destroy already
    uses: the colleagues who kept a line naming this agent get a fresh
    roster without it. Only composed agents are ever delivered to, and the
    room owns every one of those."""
    _seed_composed(session_factory, group_name=GROUP)
    _seed_peer(session_factory)

    switchboard.client.post(CONVERT_PATH, json={"run_mode": RUN_MODE_HTTP})

    delivered = [
        call["name"]
        for call in switchboard.cabal.relay_calls
        if call["task_envelope"]["metadata"].get("filename") == "colleagues.md"
    ]
    assert delivered == [PEER]


def test_a_roster_delivery_that_misses_does_not_fail_the_convert(
    session_factory, config, fake_storage, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = config.model_copy(
        update={
            "cabal_url": "http://cabal.internal",
            "agent_endpoint_template": "http://agent-{name}.internal",
        }
    )
    clients = Clients(
        github=FakeGitHub(),
        gojira=FakeGojira(),
        cloud=FakeCloud(),
        agent=FakeAgent(),
        kubectl=FakeKubectl(),
        storage=fake_storage,
        image=FakeImage(),
        cabal=FakeCabal(relay_error="switchboard is down"),
    )
    client = TestClient(create_app(session_factory, config=cfg, clients=clients))
    _seed_composed(session_factory, group_name=GROUP)
    _seed_peer(session_factory)

    response = client.post(CONVERT_PATH, json={"run_mode": RUN_MODE_HTTP})

    assert response.status_code == 200, response.text
    assert _row(session_factory).kind == KIND_CONNECTED


# --- what the room stops doing afterwards -----------------------------------------


def test_the_room_stops_editing_the_bundle_once_converted(client, session_factory):
    """The whole point of the action, proven through one of the routes it
    turns off: every bundle-editing route shares `_room_edits_this_agent_
    or_409`, so one of them standing for all of them is honest here."""
    _seed_composed(session_factory)
    client.post(CONVERT_PATH, json={"run_mode": RUN_MODE_HTTP})

    response = client.post(
        f"/api/agents/{AGENT_NAME}/teach", json={"text": "remember this"}
    )

    assert response.status_code == 409
    assert response.json() == {
        "error": (
            "this agent's repository belongs to its team, so the room "
            "does not change it"
        )
    }


def test_the_room_stops_chatting_once_converted(client, session_factory):
    """Chat was never gated on the kind before: the room would relay to an
    agent it has no wiring for and answer 502 every time, for as long as
    anyone kept asking."""
    _seed_composed(session_factory)
    client.post(CONVERT_PATH, json={"run_mode": RUN_MODE_HTTP})

    response = client.post(
        f"/api/agents/{AGENT_NAME}/chat",
        json={"messages": [{"role": "user", "text": "hello"}]},
    )

    assert response.status_code == 409
    assert response.json() == {
        "error": (
            "this agent's repository belongs to its team, so the room "
            "does not change it"
        )
    }
