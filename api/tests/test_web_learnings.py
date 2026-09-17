"""Tests for the Learnings HTTP surface (Task 7, daily-learnings spec):
`GET /api/agents/{name}/learnings`, `POST .../learnings/{id}/approve`,
`POST .../learnings/{id}/deny`, `POST .../learnings/bulk`, plus the two
`pending_learnings`/`can_decide_learnings` fields on `GET /api/agents/
{name}`. Same harness shape as test_web_teach.py (own `session_factory`/
`config` fixtures, a `TestClient` built from `create_app`, a
`_CapturingGitHub` that snapshots `bundle/intake/*` content the moment
`commit_push` fires) -- all against the fakes, no real git/kubectl
involved. Approve rides the teach lane byte-for-byte, so its archive-lane
assertions reuse that same fixture.

The decide-gate slice (review fix E) reuses test_web_teach.py's real-JWT
idiom too: a locally generated ES256 keypair signs the assertion, verified
through the actual `IapMiddleware`/`IapVerifier`, not a mock -- every other
test in this file runs IAP-disabled, where `_decider_or_403` short-circuits
to allowed, so those tests exercise nothing about the gate itself.
"""

from __future__ import annotations

import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.clients import ClientError, Clients
from kobo_api.config import Config
from kobo_api.iap import ASSERTION_HEADER, IAP_ISSUER
from kobo_api.main import create_app
from kobo_api.models import (
    LEARNING_APPROVED,
    LEARNING_DENIED,
    LEARNING_PENDING,
    Agent,
    AgentLearning,
    AgentStatus,
    AgentUserGrant,
    Base,
)
from tests.fakes import (
    FAKE_LOCAL_HEAD_DEFAULT,
    FakeAgent,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
    FakeStorage,
)

AGENT_NAME = "clio"
PERSONA = "helpful-generalist"
REPO = "acme/agent-clio"

SHA = FAKE_LOCAL_HEAD_DEFAULT

AUDIENCE = "test-audience"
KID = "test-key-1"
OWNER_EMAIL = "owner@example.com"
CALLER_EMAIL = "caller@example.com"


class _CapturingGitHub(FakeGitHub):
    """Same snapshot-on-commit_push trick as test_web_intake.py's own
    `_CapturingGitHub`, replicated locally -- captures `bundle/intake/*`
    content the moment `commit_push` is called, the only window in which
    the caller's cloned workdir still exists before `kobo_api.intake`
    discards it."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.captured_intake: dict[str, str] = {}
        self.captured_dockerfile = ""

    def commit_push(self, workdir: Path, message: str) -> bool:
        intake_dir = workdir / "bundle" / "intake"
        if intake_dir.is_dir():
            self.captured_intake = {
                path.name: path.read_text()
                for path in intake_dir.iterdir()
                if path.is_file()
            }
        self.captured_dockerfile = (workdir / "Dockerfile").read_text()
        return super().commit_push(workdir, message)

    @property
    def intake_contents(self) -> list[str]:
        """The brief's own name for what these tests want to scan: the
        plain note bodies captured at commit time, adapted from
        `captured_intake` (a filename -> content dict, same shape
        test_web_teach.py's own `_CapturingGitHub` exposes)."""
        return list(self.captured_intake.values())


class _RaisingGitHub(FakeGitHub):
    """A GitHubClient whose `clone` raises `ClientError` -- same 502-guard
    fixture as test_web_teach.py's own `_RaisingGitHub`, replicated locally
    per this file's convention (each web_ test file owns its fixtures)."""

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
    return Config(workdir=tmp_path / "work")


def _make_client(session_factory, config, *, github=None, iap_jwks_fetcher=None, cabal=None):
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
                storage=FakeStorage(),
                cabal=cabal,
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
            created_by=created_by,
        )
    )
    session.commit()
    session.close()


def _seed_learning(session_factory, agent_name=AGENT_NAME, **overrides):
    session = session_factory()
    try:
        defaults = {
            "agent_name": agent_name,
            "day": "2026-09-01",
            "content": "the canary gate is manual",
            "kind": "fact",
            "source": "Slack, Sep 1",
            "evidence": "quote",
            "content_hash": "a" * 64,
        }
        defaults.update(overrides)
        row = AgentLearning(**defaults)
        session.add(row)
        session.commit()
        return row.id
    finally:
        session.close()


def test_list_learnings_empty(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)

    response = client.get(f"/api/agents/{AGENT_NAME}/learnings")

    assert response.status_code == 200
    assert response.json() == {"learnings": []}


def test_list_learnings_returns_rows_and_validates_status(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)
    _seed_learning(session_factory)

    listed = client.get(f"/api/agents/{AGENT_NAME}/learnings")
    assert listed.status_code == 200
    body = listed.json()["learnings"]
    assert len(body) == 1
    assert body[0]["content"] == "the canary gate is manual"
    assert body[0]["status"] == LEARNING_PENDING

    bogus = client.get(f"/api/agents/{AGENT_NAME}/learnings?status=nonsense")
    assert bogus.status_code == 422


def test_list_learnings_includes_said_by(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)
    _seed_learning(session_factory, said_by="kgreiner")

    listed = client.get(f"/api/agents/{AGENT_NAME}/learnings")
    assert listed.status_code == 200
    body = listed.json()["learnings"]
    assert body[0]["said_by"] == "kgreiner"


def test_list_learnings_unknown_agent_404(session_factory, config):
    client = _make_client(session_factory, config)

    assert client.get("/api/agents/ghost/learnings").status_code == 404


def test_deny_flips_status_and_second_deny_conflicts(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)
    learning_id = _seed_learning(session_factory)

    first = client.post(f"/api/agents/{AGENT_NAME}/learnings/{learning_id}/deny")
    assert first.status_code == 200
    assert first.json()["status"] == LEARNING_DENIED

    second = client.post(f"/api/agents/{AGENT_NAME}/learnings/{learning_id}/deny")
    assert second.status_code == 409


def test_approve_rides_teach_lane_archive_fallback(session_factory, config):
    github = _CapturingGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)
    learning_id = _seed_learning(session_factory)

    response = client.post(f"/api/agents/{AGENT_NAME}/learnings/{learning_id}/approve")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == LEARNING_APPROVED
    assert body["mode"] == "archive"
    assert body["filename"].startswith("learning-")
    # The note landed in bundle/intake with the taught content.
    assert any("the canary gate is manual" in c for c in github.intake_contents)


# --- approve with edited content (learnings rethink Task 5: remember with --
# a fix) -- the operator's chance to correct wording while approving, in the
# same request. Same harness (session_factory/config, _seed_agent/
# _seed_learning, the _CapturingGitHub archive-lane seam) as every other
# approve test above.


def test_approve_with_content_teaches_the_edited_text_and_keeps_the_hash(
    session_factory, config
):
    github = _CapturingGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)
    learning_id = _seed_learning(session_factory, content="Suspend keeps memberships.")

    session = session_factory()
    original_hash = session.query(AgentLearning).filter_by(id=learning_id).one().content_hash
    session.close()

    response = client.post(
        f"/api/agents/{AGENT_NAME}/learnings/{learning_id}/approve",
        json={"content": "  Suspend keeps group memberships.  "},
    )

    assert response.status_code == 200
    # The taught note body contains the EDITED sentence, not the original.
    assert any("Suspend keeps group memberships." in c for c in github.intake_contents)
    assert not any("Suspend keeps memberships." in c for c in github.intake_contents)

    session = session_factory()
    refreshed = session.query(AgentLearning).filter_by(id=learning_id).one()
    session.close()
    assert refreshed.content == "Suspend keeps group memberships."
    assert refreshed.content_hash == original_hash  # original phrasing stays tombstoned
    assert refreshed.status == LEARNING_APPROVED


@pytest.mark.parametrize("bad", ["", "   ", "x" * 201, "two\nlines"])
def test_approve_rejects_bad_edited_content(session_factory, config, bad):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)
    learning_id = _seed_learning(session_factory, content="Suspend keeps memberships.")

    response = client.post(
        f"/api/agents/{AGENT_NAME}/learnings/{learning_id}/approve",
        json={"content": bad},
    )

    assert response.status_code == 422
    assert response.json()["error"] == "a learning is one sentence, 200 characters or fewer"

    session = session_factory()
    row = session.query(AgentLearning).filter_by(id=learning_id).one()
    session.close()
    assert row.status == LEARNING_PENDING
    assert row.content == "Suspend keeps memberships."


def test_approve_without_body_is_unchanged(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)
    learning_id = _seed_learning(session_factory)

    response = client.post(f"/api/agents/{AGENT_NAME}/learnings/{learning_id}/approve")

    assert response.status_code == 200
    session = session_factory()
    row = session.query(AgentLearning).filter_by(id=learning_id).one()
    session.close()
    assert row.content == "the canary gate is manual"
    assert row.status == LEARNING_APPROVED


def test_deny_ignores_content(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)
    learning_id = _seed_learning(session_factory, content="the canary gate is manual")

    response = client.post(
        f"/api/agents/{AGENT_NAME}/learnings/{learning_id}/deny",
        json={"content": "edited"},
    )

    assert response.status_code == 200
    session = session_factory()
    row = session.query(AgentLearning).filter_by(id=learning_id).one()
    session.close()
    assert row.content == "the canary gate is manual"
    assert row.status == LEARNING_DENIED


def test_approve_with_content_delivery_failure_does_not_persist_edit(session_factory, config):
    """502 path (upload_intake_file raises): the edit must not be
    persisted -- `get_session`'s implicit rollback-on-close discards the
    in-memory `row.content = edited` mutation, the same idiom
    `update_agent_metadata`/`set_runtime_image` already rely on for their
    own failed-delivery paths."""
    client = _make_client(session_factory, config, github=_RaisingGitHub())
    _seed_agent(session_factory, config)
    learning_id = _seed_learning(session_factory, content="Suspend keeps memberships.")

    response = client.post(
        f"/api/agents/{AGENT_NAME}/learnings/{learning_id}/approve",
        json={"content": "Suspend keeps group memberships."},
    )

    assert response.status_code == 502
    session = session_factory()
    row = session.query(AgentLearning).filter_by(id=learning_id).one()
    session.close()
    assert row.content == "Suspend keeps memberships."
    assert row.status == LEARNING_PENDING


def test_approve_unknown_id_404(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)

    response = client.post(f"/api/agents/{AGENT_NAME}/learnings/999/approve")

    assert response.status_code == 404


def test_bulk_deny(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)
    first = _seed_learning(session_factory, content_hash="a" * 64)
    second = _seed_learning(session_factory, content="another fact", content_hash="b" * 64)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/learnings/bulk",
        json={"ids": [first, second], "action": "deny"},
    )

    assert response.status_code == 200
    results = response.json()["results"]
    assert [r["status_code"] for r in results] == [200, 200]


def test_agent_detail_carries_learnings_fields(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)
    _seed_learning(session_factory)

    body = client.get(f"/api/agents/{AGENT_NAME}").json()

    assert body["pending_learnings"] == 1
    assert body["can_decide_learnings"] is True  # IAP disabled: gate passes


# --- decide gate (review fix E): real IAP JWT, owner set to a DIFFERENT --
# email than the caller so ownership can't leak the caller through --------


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


def test_decide_gate_denies_non_owner_no_grant(session_factory, config):
    private_key, jwk = _generate_jwk()
    iap_config = config.model_copy(update={"iap_audience": AUDIENCE})
    client = _make_client(session_factory, iap_config, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, iap_config, created_by=OWNER_EMAIL)
    learning_id = _seed_learning(session_factory)

    token = _sign(private_key, email=CALLER_EMAIL)
    response = client.post(
        f"/api/agents/{AGENT_NAME}/learnings/{learning_id}/deny",
        headers={ASSERTION_HEADER: token},
    )

    assert response.status_code == 403

    session = session_factory()
    row = session.query(AgentLearning).filter_by(id=learning_id).one()
    assert row.status == LEARNING_PENDING
    session.close()


def test_decide_gate_allows_a_grantee(session_factory, config):
    private_key, jwk = _generate_jwk()
    iap_config = config.model_copy(update={"iap_audience": AUDIENCE})
    client = _make_client(session_factory, iap_config, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, iap_config, created_by=OWNER_EMAIL)
    learning_id = _seed_learning(session_factory)

    session = session_factory()
    session.add(AgentUserGrant(agent_name=AGENT_NAME, email=CALLER_EMAIL))
    session.commit()
    session.close()

    token = _sign(private_key, email=CALLER_EMAIL)
    response = client.post(
        f"/api/agents/{AGENT_NAME}/learnings/{learning_id}/deny",
        headers={ASSERTION_HEADER: token},
    )

    assert response.status_code == 200
    assert response.json()["status"] == LEARNING_DENIED


def test_agent_detail_can_decide_learnings_false_for_non_owner_no_grant(
    session_factory, config
):
    private_key, jwk = _generate_jwk()
    iap_config = config.model_copy(update={"iap_audience": AUDIENCE})
    client = _make_client(session_factory, iap_config, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, iap_config, created_by=OWNER_EMAIL)

    token = _sign(private_key, email=CALLER_EMAIL)
    response = client.get(
        f"/api/agents/{AGENT_NAME}",
        headers={ASSERTION_HEADER: token},
    )

    assert response.status_code == 200
    assert response.json()["can_decide_learnings"] is False
