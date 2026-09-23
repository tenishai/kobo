"""Tests for `PATCH /api/agents/{name}/runtime` (#15) — moving an existing
agent to a different runtime image instead of destroy-and-recreate.

Same harness shape as test_api_metadata.py: own `session_factory`/`config`
fixtures and a `TestClient` over `create_app`, against the fakes, no real
git or kubectl.

THE TEST THAT CARRIES THIS FEATURE is
`test_patch_runtime_moves_the_row_so_a_later_repin_does_not_revert_it`.
`runtime_image` lives in two stores — the agents row and the repo's
Dockerfile — and four unrelated write paths (wire_slack,
upload_intake_file, and both tooling uploads) re-pin the Dockerfile from
`_runtime_image_for_repo`, which reads the ROW. A change that moved only
the file would be silently reverted by the next teach note or tool upload,
and because the generated CI hard-rotates the pod on every push, the revert
would take effect. That test pins the row; without it the feature looks
green and undoes itself in production.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.clients import ClientError, Clients
from kobo_api.config import Config
from kobo_api.main import _runtime_image_for_repo, create_app
from kobo_api.models import Agent, AgentStatus, Base
from kobo_api.personas import seed_default_personas
from kobo_api.runtime import validate_runtime_image
from tests.fakes import (
    FAKE_DOCKERFILE_OLD_IMAGE,
    FakeAgent,
    FakeCabal,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
    FakeName,
    FakeStorage,
)

AGENT_NAME = "clio"
PERSONA = "helpful-generalist"
REPO = "acme/agent-clio"
PERSONAS_BUCKET = "personas-bucket"

# A second ref under the org default's own registry root — the per-agent
# pin this route exists to set. Derived from the default rather than
# written out, because the root is the whole rule.
OTHER_RUNTIME = Config().runtime_image.rsplit("/", 1)[0] + "/daedalus-runtime:v9"

# An image OUTSIDE that root, as an agent created back when the room
# offered a second runtime still carries in its row. Nothing accepts it any
# more; the re-pin paths must keep using it all the same.
RETIRED_RUNTIME = "eu.registry.example/agents/sdk-runtime:v2"


class _CapturingGitHub(FakeGitHub):
    """Snapshots the Dockerfile at the moment `commit_push` is called — the
    only window in which the cloned workdir still exists before it is
    discarded. Same trick as test_api_slack.py's own `_CapturingGitHub`,
    duplicated locally rather than imported cross-file, per this suite's
    existing convention."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.captured_dockerfile = ""

    def commit_push(self, workdir: Path, message: str) -> bool:
        dockerfile = workdir / "Dockerfile"
        if dockerfile.is_file():
            self.captured_dockerfile = dockerfile.read_text()
        return super().commit_push(workdir, message)


class _RaisingGitHub(FakeGitHub):
    """Every clone raises `ClientError` — the 502 fixture, modeling a
    transient GitHub outage without a real subprocess."""

    def clone(self, repo: str, dest: Path) -> None:
        raise ClientError("gh: rate limited")


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config(tmp_path):
    return Config(workdir=tmp_path / "work", personas_bucket=PERSONAS_BUCKET)


def _make_client(session_factory, config, *, github=None):
    storage = FakeStorage()
    seed_default_personas(storage, bucket=PERSONAS_BUCKET)
    return TestClient(
        create_app(
            session_factory,
            config=config,
            clients=Clients(
                github=github if github is not None else FakeGitHub(),
                gojira=FakeGojira(),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=FakeKubectl(),
                storage=storage,
                name=FakeName(),
                cabal=FakeCabal(),
            ),
        )
    )


def _seed_agent(session_factory, config, *, status=AgentStatus.READY, runtime_image=None):
    session = session_factory()
    session.add(
        Agent(
            name=AGENT_NAME,
            persona=PERSONA,
            model_id=config.models[0].id,
            status=status,
            repo_full_name=REPO,
            team="shared",
            skills=[],
            runtime_image=runtime_image,
        )
    )
    session.commit()
    session.close()


def _stored_runtime(session_factory) -> str | None:
    session = session_factory()
    try:
        return session.query(Agent).filter_by(name=AGENT_NAME).one().runtime_image
    finally:
        session.close()


def test_patch_runtime_unknown_agent_404s(session_factory, config):
    client = _make_client(session_factory, config)
    response = client.patch(
        f"/api/agents/{AGENT_NAME}/runtime", json={"runtime_image": OTHER_RUNTIME}
    )
    assert response.status_code == 404


def test_patch_runtime_not_ready_409s(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, status=AgentStatus.PROVISIONING)

    response = client.patch(
        f"/api/agents/{AGENT_NAME}/runtime", json={"runtime_image": OTHER_RUNTIME}
    )

    assert response.status_code == 409
    assert response.json() == {"error": "agent is not ready for a runtime change yet"}


def test_patch_runtime_rewrites_the_dockerfile_and_reports_both_values(session_factory, config):
    # FakeGitHub's clone writes FAKE_DOCKERFILE_OLD_IMAGE, distinct from
    # both the org default and OTHER_RUNTIME, so a matching FROM line proves
    # the rewrite happened rather than the two agreeing by coincidence.
    assert FAKE_DOCKERFILE_OLD_IMAGE != OTHER_RUNTIME
    github = _CapturingGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config, runtime_image=config.runtime_image)

    response = client.patch(
        f"/api/agents/{AGENT_NAME}/runtime", json={"runtime_image": OTHER_RUNTIME}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["runtime_image"] == OTHER_RUNTIME
    assert body["previous_runtime_image"] == config.runtime_image
    assert body["sha"]
    assert github.captured_dockerfile == (
        "# Agent image: the shared runtime plus this agent's bundle.\n"
        f"FROM {OTHER_RUNTIME}\n"
        "COPY bundle/ /bundle\n"
    )


def test_patch_runtime_moves_the_row_so_a_later_repin_does_not_revert_it(session_factory, config):
    """The regression this feature exists to avoid.

    `_runtime_image_for_repo` is what wire_slack, upload_intake_file and
    both tooling uploads pass into `update_dockerfile_runtime_image` on
    their own commits. If the PATCH moved only the Dockerfile, this
    accessor would still answer with the OLD image and the next unrelated
    write would re-pin the repo back — a revert that reaches the running
    pod, since every push hard-rotates it.
    """
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, runtime_image=config.runtime_image)

    response = client.patch(
        f"/api/agents/{AGENT_NAME}/runtime", json={"runtime_image": OTHER_RUNTIME}
    )
    assert response.status_code == 200

    assert _stored_runtime(session_factory) == OTHER_RUNTIME
    # The accessor every re-pinning caller reads, asked exactly as they ask it.
    assert _runtime_image_for_repo(client.app, REPO) == OTHER_RUNTIME


def test_patch_runtime_refuses_an_image_this_org_does_not_run(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)

    response = client.patch(
        f"/api/agents/{AGENT_NAME}/runtime",
        json={"runtime_image": "docker.io/somebody/whatever:latest"},
    )

    assert response.status_code == 422
    # Provenance is the whole rule: the org default's own registry root,
    # and nothing else. There is no second list of accepted images.
    assert "must live under 'registry.example/agents/'" in response.json()["error"]
    assert _stored_runtime(session_factory) is None


def test_patch_runtime_refuses_a_blank_image(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)

    response = client.patch(f"/api/agents/{AGENT_NAME}/runtime", json={"runtime_image": "   "})

    assert response.status_code == 422
    assert response.json() == {"error": "runtime_image can't be blank"}


def test_patch_runtime_accepts_a_freeform_ref_under_the_org_registry_root(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)
    root = config.runtime_image.rsplit("/", 1)[0] + "/"

    response = client.patch(
        f"/api/agents/{AGENT_NAME}/runtime", json={"runtime_image": f"{root}experimental:sha-abc123"}
    )

    assert response.status_code == 200
    assert _stored_runtime(session_factory) == f"{root}experimental:sha-abc123"


def test_an_agent_on_a_retired_runtime_keeps_repinning_and_can_be_moved_onto_the_default(
    session_factory, config
):
    """An agent created back when the room offered a second runtime still
    has that image in its row, and nothing would accept it today.

    Two halves, both required. The re-pin paths (wire_slack,
    upload_intake_file, both tooling uploads) read `_runtime_image_for_repo`
    and never validate — a teach note on such an agent must not error, and
    must not quietly rebuild it on an image it does not run. And this route
    is how it gets moved, so it has to take a row it would itself refuse as
    the INPUT while validating only the target.
    """
    assert validate_runtime_image(RETIRED_RUNTIME, org_default=config.runtime_image)
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, runtime_image=RETIRED_RUNTIME)

    # The accessor every re-pinning caller reads, asked exactly as they ask it.
    assert _runtime_image_for_repo(client.app, REPO) == RETIRED_RUNTIME

    response = client.patch(
        f"/api/agents/{AGENT_NAME}/runtime", json={"runtime_image": config.runtime_image}
    )

    assert response.status_code == 200
    assert response.json()["previous_runtime_image"] == RETIRED_RUNTIME
    assert _stored_runtime(session_factory) == config.runtime_image


def test_patch_runtime_push_failure_502s_and_leaves_the_row_at_its_old_value(
    session_factory, config
):
    # The two stores must not be able to disagree. A delivery failure
    # returns before session.commit(), so get_session's rollback-on-close
    # discards the in-memory row mutation — proven through a FRESH session,
    # which can only see what the route actually committed.
    client = _make_client(session_factory, config, github=_RaisingGitHub())
    _seed_agent(session_factory, config, runtime_image=config.runtime_image)

    response = client.patch(
        f"/api/agents/{AGENT_NAME}/runtime", json={"runtime_image": OTHER_RUNTIME}
    )

    assert response.status_code == 502
    assert response.json() == {"error": "couldn't change the runtime image. Try again."}
    assert _stored_runtime(session_factory) == config.runtime_image


def test_patch_runtime_to_the_current_value_still_repins_the_file(session_factory, config):
    # No short-circuit on an unchanged value, deliberately: the row and the
    # file are two stores and can drift apart, so asking for the image the
    # row already holds is a repair of the file. The clone starts on
    # FAKE_DOCKERFILE_OLD_IMAGE, so a correct run rewrites it even though
    # the row does not move.
    github = _CapturingGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config, runtime_image=OTHER_RUNTIME)

    response = client.patch(
        f"/api/agents/{AGENT_NAME}/runtime", json={"runtime_image": OTHER_RUNTIME}
    )

    assert response.status_code == 200
    assert response.json()["previous_runtime_image"] == OTHER_RUNTIME
    assert f"FROM {OTHER_RUNTIME}\n" in github.captured_dockerfile
    assert _stored_runtime(session_factory) == OTHER_RUNTIME


# --- The two runtime facts on GET /api/agents/{name} (harness wave) --------
#
# The Harness tab's Model and runtime section says what an agent runs on and
# whether that is still what the room builds on. Both halves of that sentence
# have to come from the server: the agent's own image lives in a column that
# is usually NULL (every agent is built on the org default and only a PATCH
# fills it in), and the room's current default lives in config, which no
# read route exposed before this.


def test_get_agent_reports_its_own_runtime_image_and_the_room_s(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, runtime_image=OTHER_RUNTIME)

    body = client.get(f"/api/agents/{AGENT_NAME}").json()

    assert body["runtime_image"] == OTHER_RUNTIME
    assert body["room_runtime_image"] == config.runtime_image
    # The whole point of carrying both: this agent is behind.
    assert body["runtime_image"] != body["room_runtime_image"]


def test_get_agent_falls_back_to_the_room_s_runtime_when_the_row_names_none(
    session_factory, config
):
    # The normal case, and the reason the fallback is the server's job: an
    # agent built on the org default carries NULL, and a page that read the
    # column raw would print nothing for almost every agent in the room.
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, runtime_image=None)

    body = client.get(f"/api/agents/{AGENT_NAME}").json()

    assert body["runtime_image"] == config.runtime_image
    assert body["room_runtime_image"] == config.runtime_image


def test_get_agent_reports_a_retired_runtime_verbatim_rather_than_hiding_it(
    session_factory, config
):
    # An agent created back when the room offered a second runtime. Nothing
    # accepts that image any more, which is exactly why its page has to say
    # so instead of quietly showing the default.
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, runtime_image=RETIRED_RUNTIME)

    body = client.get(f"/api/agents/{AGENT_NAME}").json()

    assert body["runtime_image"] == RETIRED_RUNTIME
    assert body["room_runtime_image"] == config.runtime_image


def test_the_agent_list_keeps_its_shape(session_factory, config):
    # One card per agent on the dashboard, and none of them says what it
    # runs on -- the two fields above are a detail-page fact, so the list
    # does not grow a column for them.
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, runtime_image=OTHER_RUNTIME)

    row = client.get("/api/agents").json()["agents"][0]

    assert "runtime_image" not in row
    assert "room_runtime_image" not in row
