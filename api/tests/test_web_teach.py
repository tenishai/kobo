"""Tests for the "teach from chat" HTTP route (Task 1): `POST
/api/agents/{name}/teach` -- a thin route that formats an operator's
distilled chat note (`kobo_api.teach.format_note`/`note_filename`) and
hands the result to the same one-shot `kobo_api.intake.upload_intake_file`
seam the existing intake upload route already uses. Same harness shape as
test_web_intake.py (own `session_factory`/`config` fixtures, a `TestClient`
built from `create_app`, a `_CapturingGitHub` that snapshots `bundle/
intake/*` content the moment `commit_push` fires) -- all against the
fakes, no real git/kubectl involved.

The identity-attribution slice (IAP on with/without an `email` claim, IAP
off entirely) reuses test_web_destroy.py's real-JWT idiom: a locally
generated ES256 keypair signs the assertion, verified through the actual
`IapMiddleware`/`IapVerifier`, not a mock.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
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
from kobo_api.intake import INTAKE_MAX_BYTES
from kobo_api.main import create_app
from kobo_api.models import Agent, AgentStatus, Base
from kobo_api.teach import format_note, note_filename
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


class _RaisingGitHub(FakeGitHub):
    """A GitHubClient whose `clone` raises `ClientError` -- the 502 guard's
    fixture, same idiom as test_web_intake.py's own."""

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


def _make_client(session_factory, config, *, github=None, iap_jwks_fetcher=None):
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
            ),
            iap_jwks_fetcher=iap_jwks_fetcher,
        )
    )


def _seed_agent(session_factory, config, *, status=AgentStatus.READY, repo_full_name=REPO):
    session = session_factory()
    session.add(
        Agent(
            name=AGENT_NAME,
            persona=PERSONA,
            model_id=config.models[0].id,
            status=status,
            repo_full_name=repo_full_name,
        )
    )
    session.commit()
    session.close()


NON_READY_STATUSES = [AgentStatus.PENDING, AgentStatus.PROVISIONING, AgentStatus.FAILED]


def _today_candidates() -> list[str]:
    """The one or two ISO dates ("today", UTC) the route could plausibly
    have stamped a note with, bracketing the instant of the actual request
    -- avoids any freezegun-style time-mocking dependency while staying
    robust to a request that happens to straddle a UTC midnight rollover."""
    before = datetime.now(UTC).date()
    return sorted({before.isoformat(), datetime.now(UTC).date().isoformat()})


# --- POST /api/agents/{name}/teach: happy path + seam wiring -----------------


def test_teach_route_returns_filename_and_sha(session_factory, config):
    github = _CapturingGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/teach", json={"text": "always answer in one paragraph"}
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"filename", "sha"}
    assert body["sha"] == SHA
    assert body["filename"].startswith("chat-")
    assert body["filename"].endswith(".md")


def test_teach_route_calls_intake_seam_with_formatted_artifacts_verbatim(
    session_factory, config
):
    github = _CapturingGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    text = "always answer in one paragraph"
    response = client.post(f"/api/agents/{AGENT_NAME}/teach", json={"text": text})
    assert response.status_code == 200
    filename = response.json()["filename"]

    candidate_filenames = {note_filename(text, on_date=d) for d in _today_candidates()}
    candidate_notes = {
        format_note(text, taught_by=None, on_date=d) for d in _today_candidates()
    }

    assert filename in candidate_filenames
    assert list(github.captured_intake.keys()) == [filename]
    assert github.captured_intake[filename] in candidate_notes
    assert github.pushes[-1][1] == f"kobo: add intake/{filename}"


def test_teach_route_rewrites_dockerfile_to_configured_runtime_image(session_factory, config):
    # Same false-ingested-window fix upload_intake_file already gives the
    # plain intake upload route: teach is just another way to write into
    # bundle/intake/, so it must carry the same runtime_image re-pin.
    from tests.fakes import FAKE_DOCKERFILE_OLD_IMAGE

    assert FAKE_DOCKERFILE_OLD_IMAGE != config.runtime_image

    github = _CapturingGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/teach", json={"text": "be concise"}
    )
    assert response.status_code == 200
    assert github.captured_dockerfile == (
        "# Agent image: the shared runtime plus this agent's bundle.\n"
        f"FROM {config.runtime_image}\n"
        "COPY bundle/ /bundle\n"
    )


def test_teach_route_repeated_same_day_same_words_overwrites_same_filename(
    session_factory, config
):
    # Collision-tolerant by design (task brief): re-teaching the same gist
    # on the same day reuses the same chat-<date>-<slug>.md name, and the
    # intake seam's own semantics (same name, new content = new ingestion)
    # handle it from there -- no hash/uniqueness suffix needed here.
    github = _CapturingGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    first = client.post(
        f"/api/agents/{AGENT_NAME}/teach", json={"text": "always answer briefly"}
    )
    second = client.post(
        f"/api/agents/{AGENT_NAME}/teach", json={"text": "always answer briefly, please"}
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["filename"] == second.json()["filename"]


# --- POST /api/agents/{name}/teach: 422s -------------------------------------


def test_teach_route_empty_text_returns_422(session_factory, config):
    github = _CapturingGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.post(f"/api/agents/{AGENT_NAME}/teach", json={"text": ""})

    assert response.status_code == 422
    assert "empty" in response.json()["error"]
    assert "—" not in response.json()["error"]
    assert github.pushes == []


def test_teach_route_whitespace_only_text_returns_422(session_factory, config):
    github = _CapturingGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.post(f"/api/agents/{AGENT_NAME}/teach", json={"text": "   \n\t  "})

    assert response.status_code == 422
    assert "empty" in response.json()["error"]
    assert github.pushes == []


def test_teach_route_oversize_text_returns_422(session_factory, config):
    github = _CapturingGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/teach",
        json={"text": "a" * (INTAKE_MAX_BYTES + 1)},
    )

    assert response.status_code == 422
    assert "exceeds" in response.json()["error"]
    assert "—" not in response.json()["error"]
    assert github.pushes == []


def test_teach_route_missing_text_field_returns_422(session_factory, config):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config)

    response = client.post(f"/api/agents/{AGENT_NAME}/teach", json={})

    assert response.status_code == 422
    assert "error" in response.json()


# --- POST /api/agents/{name}/teach: gates ------------------------------------


def test_teach_route_unknown_agent_returns_404(session_factory, config):
    client = _make_client(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/teach", json={"text": "be concise"}
    )

    assert response.status_code == 404
    assert "error" in response.json()


@pytest.mark.parametrize("status", NON_READY_STATUSES)
def test_teach_route_non_ready_agent_returns_409(session_factory, config, status):
    client = _make_client(session_factory, config)
    _seed_agent(session_factory, config, status=status)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/teach", json={"text": "be concise"}
    )

    assert response.status_code == 409
    assert response.json() == {"error": "agent is not ready for memory changes"}


def test_teach_route_github_error_returns_502(session_factory, config):
    client = _make_client(session_factory, config, github=_RaisingGitHub())
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/teach", json={"text": "be concise"}
    )

    assert response.status_code == 502
    assert response.json() == {"error": "couldn't reach your agent's files. Try again."}


# --- POST /api/agents/{name}/teach: identity attribution (real IAP JWT) ------


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


def test_teach_route_with_iap_email_attributes_the_note(session_factory, config):
    private_key, jwk = _generate_jwk()
    iap_config = config.model_copy(update={"iap_audience": AUDIENCE})
    github = _CapturingGitHub()
    client = _make_client(
        session_factory, iap_config, github=github, iap_jwks_fetcher=_fetcher(jwk)
    )
    _seed_agent(session_factory, iap_config)

    token = _sign(private_key, email="teacher@example.com")
    response = client.post(
        f"/api/agents/{AGENT_NAME}/teach",
        json={"text": "always answer in one paragraph"},
        headers={ASSERTION_HEADER: token},
    )

    assert response.status_code == 200
    filename = response.json()["filename"]
    assert "teacher@example.com" in github.captured_intake[filename]


def test_teach_route_iap_on_but_no_email_claim_omits_attribution_gracefully(
    session_factory, config
):
    private_key, jwk = _generate_jwk()
    iap_config = config.model_copy(update={"iap_audience": AUDIENCE})
    github = _CapturingGitHub()
    client = _make_client(
        session_factory, iap_config, github=github, iap_jwks_fetcher=_fetcher(jwk)
    )
    _seed_agent(session_factory, iap_config)

    token = _sign(private_key, email=None)
    response = client.post(
        f"/api/agents/{AGENT_NAME}/teach",
        json={"text": "always answer in one paragraph"},
        headers={ASSERTION_HEADER: token},
    )

    assert response.status_code == 200
    filename = response.json()["filename"]
    assert "Taught by" not in github.captured_intake[filename]


def test_teach_route_iap_disabled_omits_attribution_gracefully(session_factory, config):
    # config.iap_audience is "" by default (no IapMiddleware registered at
    # all) -- request.state.iap_email is never set, and the route must
    # fall back to omitting the attribution line rather than raising on a
    # missing attribute.
    assert config.iap_audience == ""

    github = _CapturingGitHub()
    client = _make_client(session_factory, config, github=github)
    _seed_agent(session_factory, config)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/teach",
        json={"text": "always answer in one paragraph"},
    )

    assert response.status_code == 200
    filename = response.json()["filename"]
    assert "Taught by" not in github.captured_intake[filename]
