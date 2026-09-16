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
from kobo_api.config import Config, RuntimeChoice
from kobo_api.main import _runtime_image_for_repo, create_app
from kobo_api.models import Agent, AgentStatus, Base
from kobo_api.personas import seed_default_personas
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

# A second operator-vetted runtime, on a DIFFERENT registry host than the
# org default — the real reason `validate_runtime_image` accepts a
# configured image outright rather than only checking the registry root
# (the sdk runtime lives on its own regional host).
OTHER_RUNTIME = "eu.registry.example/agents/sdk-runtime:v2"


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
    return Config(
        workdir=tmp_path / "work",
        personas_bucket=PERSONAS_BUCKET,
        runtimes=[RuntimeChoice(image=OTHER_RUNTIME, label="SDK runtime")],
    )


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
    # The same rule create enforces, stated the same way — the point of
    # sharing `validate_runtime_image` rather than writing a second check.
    assert "must be one of the configured runtimes" in response.json()["error"]
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
