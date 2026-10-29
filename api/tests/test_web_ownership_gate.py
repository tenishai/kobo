"""Tests for the owner-or-operator mutation gate (access-control Task A3):
`_owner_or_operator_or_403` and its wiring into every mutating agent route
except destroy (kept operator-only, its own standing rule) and teach/chat
(open by design to any signed-in user who can see the agent).

Two layers of coverage, same shape as test_web_slack_gate.py/
test_web_destroy.py:

- Unit-level: the helper called directly against a minimal stand-in for
  `Request` (only `.state`/`.app.state.config` are read) and a minimal
  stand-in for `Agent` (only `.created_by` is read) -- no HTTP involved.
- Route-level: real `TestClient` requests, real IAP JWTs (the
  test_web_teach.py idiom: a locally generated ES256 keypair signs the
  assertion, verified through the actual `IapMiddleware`/`IapVerifier`, not
  a mock). One representative route per gated CLASS gets the full
  owner/operator/stranger/ownerless matrix; every other gated route gets a
  lighter stranger-must-403 smoke test proving it's actually wired, since
  the gate is identical everywhere -- the interesting behavior lives in the
  helper, already pinned by the unit tests and the one full matrix.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.clients import Clients
from kobo_api.config import Config
from kobo_api.iap import ASSERTION_HEADER, IAP_ISSUER
from kobo_api.main import PNG_SIGNATURE, _owner_or_operator_or_403, create_app
from kobo_api.models import Agent, AgentStatus, Base
from tests.fakes import (
    FakeAgent,
    FakeCabal,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
    FakeStorage,
)

AGENT_NAME = "clio"
PERSONA = "helpful-generalist"
REPO = "acme/agent-clio"

OWNER_EMAIL = "owner@example.com"
OPERATOR_EMAIL = "Ops@Example.com"
STRANGER_EMAIL = "stranger@example.com"

VERBATIM_403 = {"error": "Only this agent's owner or a room operator can change it."}

AUDIENCE = "test-audience"
KID = "test-key-1"


# --- Unit-level: _owner_or_operator_or_403 called directly -------------------


def _req(email: str | None, config: Config):
    """A minimal stand-in for `Request` -- the helper only ever reads
    `request.state.iap_email` (via `getattr`) and `request.app.state.config`,
    so a bare namespace tree is enough; when `email` is None, `.state`
    carries no `iap_email` attribute at all, mirroring a real request that
    never went through IapMiddleware. Same idiom as test_web_slack_gate.py's
    own `_req`, extended one level for `.app.state.config`."""
    state = SimpleNamespace()
    if email is not None:
        state.iap_email = email
    return SimpleNamespace(state=state, app=SimpleNamespace(state=SimpleNamespace(config=config)))


def _agent(created_by: str | None):
    """A minimal stand-in for `Agent` -- the helper only ever reads
    `.created_by`."""
    return SimpleNamespace(created_by=created_by)


def test_helper_operator_passes_even_without_ownership():
    config = Config(operators=[OPERATOR_EMAIL], iap_audience=AUDIENCE)

    result = _owner_or_operator_or_403(_agent(OWNER_EMAIL), _req(OPERATOR_EMAIL, config))

    assert result is None


def test_helper_operator_match_is_case_insensitive():
    config = Config(operators=["ops@example.com"], iap_audience=AUDIENCE)

    result = _owner_or_operator_or_403(
        _agent(None), _req("OPS@EXAMPLE.COM", config)
    )

    assert result is None


def test_helper_owner_passes_even_when_not_an_operator():
    config = Config(operators=[], iap_audience=AUDIENCE)

    result = _owner_or_operator_or_403(_agent(OWNER_EMAIL), _req(OWNER_EMAIL, config))

    assert result is None


def test_helper_owner_match_is_case_insensitive():
    config = Config(operators=[], iap_audience=AUDIENCE)

    result = _owner_or_operator_or_403(
        _agent("Owner@Example.com"), _req("owner@example.com", config)
    )

    assert result is None


def test_helper_stranger_returns_403_verbatim():
    config = Config(operators=[OPERATOR_EMAIL], iap_audience=AUDIENCE)

    result = _owner_or_operator_or_403(_agent(OWNER_EMAIL), _req(STRANGER_EMAIL, config))

    assert result is not None
    assert result.status_code == 403
    import json

    assert json.loads(result.body) == VERBATIM_403


def test_helper_no_email_at_all_returns_403():
    config = Config(operators=[OPERATOR_EMAIL], iap_audience=AUDIENCE)

    result = _owner_or_operator_or_403(_agent(OWNER_EMAIL), _req(None, config))

    assert result is not None
    assert result.status_code == 403


def test_helper_ownerless_agent_blocks_a_non_operator():
    # The fail-closed branch: a legacy/no-owner agent is operator-only to
    # mutate, even for a caller with a perfectly good identity.
    config = Config(operators=[OPERATOR_EMAIL], iap_audience=AUDIENCE)

    result = _owner_or_operator_or_403(_agent(None), _req(STRANGER_EMAIL, config))

    assert result is not None
    assert result.status_code == 403


def test_helper_ownerless_agent_still_lets_an_operator_through():
    config = Config(operators=[OPERATOR_EMAIL], iap_audience=AUDIENCE)

    result = _owner_or_operator_or_403(_agent(None), _req(OPERATOR_EMAIL, config))

    assert result is None


def test_helper_iap_off_bypasses_even_with_no_owner_and_no_email():
    # IAP disabled: the room trusts its perimeter only when it has one, same
    # escape hatch _operator_block/_slack_operator_block already use. Without
    # this, a room that never turns IAP on could never mutate any of these
    # routes again once an agent's created_by is null -- which is every
    # agent, since IAP-off means created_by is never recorded either.
    config = Config(operators=[], iap_audience="")

    result = _owner_or_operator_or_403(_agent(None), _req(None, config))

    assert result is None


# --- Route-level: real TestClient, real IAP JWT (same idiom as test_web_teach.py) --


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
        operators=[OPERATOR_EMAIL],
        iap_audience=AUDIENCE,
    )


class _CapturingGitHub(FakeGitHub):
    """Same snapshot-on-commit_push trick every sibling web-route test file
    uses, replicated locally -- lets a happy-path assertion look at what
    actually landed in the cloned workdir before it's discarded."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.captured: dict[str, str] = {}

    def commit_push(self, workdir: Path, message: str) -> bool:
        for sub in ("tools", "skills/word-games"):
            d = workdir / "bundle" / sub
            if d.is_dir():
                for path in d.iterdir():
                    if path.is_file():
                        self.captured[path.name] = path.read_text()
        return super().commit_push(workdir, message)


def _make_client(session_factory, config, *, github=None, cabal=None, iap_jwks_fetcher=None):
    return TestClient(
        create_app(
            session_factory,
            config=config,
            clients=Clients(
                github=github if github is not None else _CapturingGitHub(),
                gojira=FakeGojira(),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=FakeKubectl(),
                storage=FakeStorage(),
                # None (the default) whenever a test doesn't care -- matches
                # every pre-existing call site here, none of which touched
                # the switchboard. The directory-sync fix-round tests below
                # (Task A3 fix round 1) are the first callers here to pass a
                # real `FakeCabal()`.
                cabal=cabal,
            ),
            iap_jwks_fetcher=iap_jwks_fetcher,
        )
    )


def _seed_agent(
    session_factory,
    config,
    *,
    created_by=None,
    status=AgentStatus.READY,
    repo_full_name=REPO,
    skills=None,
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
            skills=skills if skills is not None else [],
        )
    )
    session.commit()
    session.close()


def _headers(private_key, *, email: str | None) -> dict:
    if email is None:
        return {}
    return {ASSERTION_HEADER: _sign(private_key, email=email)}


# --- The gate matrix, one representative route per class --------------------
#
# Each entry is (class_name, request_fn) where request_fn(client, headers)
# performs the mutating call with a fixed, valid-shaped body -- valid enough
# that an owner/operator caller's request actually succeeds end to end
# (proving the gate lets the RIGHT work through, not just that it blocks the
# wrong callers). A stranger/ownerless caller never reaches that business
# logic at all: the gate is wired right after the ready-agent fetch, before
# any work, so the fixture doesn't need to be "valid" from that caller's
# point of view.


def _upload_tooling(client, headers):
    return client.post(
        f"/api/agents/{AGENT_NAME}/tooling",
        json={"filename": "tool.py", "content": "def post_draft(draft):\n    return draft\n"},
        headers=headers,
    )


def _save_local_skill(client, headers):
    return client.put(
        f"/api/agents/{AGENT_NAME}/skills/local/notes",
        json={"content": "---\nname: notes\ndescription: A guardrail.\n---\nBody.\n"},
        headers=headers,
    )


def _patch_metadata(client, headers):
    return client.patch(
        f"/api/agents/{AGENT_NAME}/metadata", json={"team": "acme-team"}, headers=headers
    )


def _upload_intake(client, headers):
    return client.post(
        f"/api/agents/{AGENT_NAME}/intake",
        json={"filename": "notes.md", "content": "remember this"},
        headers=headers,
    )


def _upload_avatar(client, headers):
    body_headers = {**headers, "Content-Type": "image/png"}
    return client.put(
        f"/api/agents/{AGENT_NAME}/avatar",
        content=PNG_SIGNATURE + b"rest-of-the-picture",
        headers=body_headers,
    )


def _save_slack_channels(client, headers):
    return client.put(
        f"/api/agents/{AGENT_NAME}/slack/channels",
        json={"channels": ["C1234567"]},
        headers=headers,
    )


def _save_knowledge(client, headers):
    # Task A3 fix round 1 (HIGH): the route sweep found `save_knowledge`
    # (PUT) ungated -- a stranger could overwrite another agent's knowledge
    # file. A fresh filename against the default `_CapturingGitHub()` (no
    # pre-existing knowledge_files) is enough for an owner/operator call to
    # actually succeed end to end, same as `_upload_intake`/`_upload_tooling`
    # above.
    return client.put(
        f"/api/agents/{AGENT_NAME}/knowledge/gotchas.md",
        json={"content": "new content"},
        headers=headers,
    )


GATE_MATRIX_ROUTES = [
    ("tooling", _upload_tooling),
    ("skills", _save_local_skill),
    ("metadata", _patch_metadata),
    ("intake", _upload_intake),
    ("avatar", _upload_avatar),
    ("slack", _save_slack_channels),
    ("knowledge", _save_knowledge),
]


@pytest.mark.parametrize("class_name,request_fn", GATE_MATRIX_ROUTES, ids=[c for c, _ in GATE_MATRIX_ROUTES])
def test_owner_may_mutate(session_factory, config, class_name, request_fn):
    private_key, jwk = _generate_jwk()
    client = _make_client(session_factory, config, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, config, created_by=OWNER_EMAIL)

    response = request_fn(client, _headers(private_key, email=OWNER_EMAIL))

    assert response.status_code == 200, response.text


@pytest.mark.parametrize("class_name,request_fn", GATE_MATRIX_ROUTES, ids=[c for c, _ in GATE_MATRIX_ROUTES])
def test_operator_may_mutate_even_without_ownership(session_factory, config, class_name, request_fn):
    private_key, jwk = _generate_jwk()
    client = _make_client(session_factory, config, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, config, created_by=OWNER_EMAIL)

    response = request_fn(client, _headers(private_key, email=OPERATOR_EMAIL))

    assert response.status_code == 200, response.text


@pytest.mark.parametrize("class_name,request_fn", GATE_MATRIX_ROUTES, ids=[c for c, _ in GATE_MATRIX_ROUTES])
def test_stranger_gets_403_verbatim(session_factory, config, class_name, request_fn):
    private_key, jwk = _generate_jwk()
    client = _make_client(session_factory, config, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, config, created_by=OWNER_EMAIL)

    response = request_fn(client, _headers(private_key, email=STRANGER_EMAIL))

    assert response.status_code == 403
    assert response.json() == VERBATIM_403


@pytest.mark.parametrize("class_name,request_fn", GATE_MATRIX_ROUTES, ids=[c for c, _ in GATE_MATRIX_ROUTES])
def test_ownerless_agent_is_operator_only(session_factory, config, class_name, request_fn):
    private_key, jwk = _generate_jwk()
    client = _make_client(session_factory, config, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, config, created_by=None)

    stranger_response = request_fn(client, _headers(private_key, email=STRANGER_EMAIL))
    assert stranger_response.status_code == 403
    assert stranger_response.json() == VERBATIM_403

    operator_response = request_fn(client, _headers(private_key, email=OPERATOR_EMAIL))
    assert operator_response.status_code == 200, operator_response.text


# --- Smoke tests: every other gated route, proving it's actually wired ------
#
# The gate is called identically everywhere (right after the ready-agent
# fetch, before any work), so its interesting behavior is already pinned by
# the unit tests and the one full matrix above. These just prove each
# remaining route in the brief's wiring list actually calls it -- a stranger
# never gets past the gate, so none of these need a fixture valid enough for
# the route's OWN business logic to succeed.


def _connect_slack(client, headers):
    return client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"app_id": "A123", "bot_token": "xoxb-x", "app_token": "xapp-x"},
        headers=headers,
    )


def _import_tooling(client, headers):
    return client.post(
        f"/api/agents/{AGENT_NAME}/tooling/import",
        json={"repo": "acme/tools", "path": "tools"},
        headers=headers,
    )


def _delete_tooling(client, headers):
    return client.delete(f"/api/agents/{AGENT_NAME}/tooling/tool.py", headers=headers)


def _add_skill_from_catalog(client, headers):
    return client.post(
        f"/api/agents/{AGENT_NAME}/skills/catalog",
        json={"name": "word-games", "team": "shared"},
        headers=headers,
    )


def _add_skills_from_catalog(client, headers):
    return client.post(
        f"/api/agents/{AGENT_NAME}/skills/catalog/batch",
        json={"skills": [{"name": "word-games", "team": "shared"}]},
        headers=headers,
    )


def _delete_skill(client, headers):
    return client.delete(f"/api/agents/{AGENT_NAME}/skills/word-games", headers=headers)


def _regenerate_metadata(client, headers):
    return client.post(f"/api/agents/{AGENT_NAME}/metadata/regenerate", headers=headers)


def _delete_intake(client, headers):
    return client.delete(f"/api/agents/{AGENT_NAME}/intake/notes.md", headers=headers)


def _generate_avatar(client, headers):
    return client.post(f"/api/agents/{AGENT_NAME}/avatar", headers=headers)


def _set_runtime(client, headers):
    return client.patch(
        f"/api/agents/{AGENT_NAME}/runtime",
        json={"runtime_image": "registry.example/agents/daedalus-runtime:v2"},
        headers=headers,
    )


def _set_model(client, headers):
    return client.patch(
        f"/api/agents/{AGENT_NAME}/model", json={"model": "vertex_ai/claude-sonnet-4-5"}, headers=headers
    )


def _set_slack_wake(client, headers):
    return client.put(f"/api/agents/{AGENT_NAME}/slack/wake", json={"wake": "thread"}, headers=headers)


def _set_colleague_mode(client, headers):
    return client.put(
        f"/api/agents/{AGENT_NAME}/colleague-mode", json={"mode": "roster-only"}, headers=headers
    )


SMOKE_ROUTES = [
    ("slack-connect", _connect_slack),
    ("tooling-import", _import_tooling),
    ("tooling-delete", _delete_tooling),
    ("skills-catalog", _add_skill_from_catalog),
    ("skills-catalog-batch", _add_skills_from_catalog),
    ("skills-delete", _delete_skill),
    ("metadata-regenerate", _regenerate_metadata),
    ("intake-delete", _delete_intake),
    ("avatar-generate", _generate_avatar),
    ("runtime", _set_runtime),
    ("model", _set_model),
    ("slack-wake", _set_slack_wake),
    ("colleague-mode", _set_colleague_mode),
]


@pytest.mark.parametrize("route_name,request_fn", SMOKE_ROUTES, ids=[r for r, _ in SMOKE_ROUTES])
def test_stranger_is_blocked_on_every_other_gated_route(
    session_factory, config, route_name, request_fn
):
    private_key, jwk = _generate_jwk()
    client = _make_client(session_factory, config, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, config, created_by=OWNER_EMAIL)

    response = request_fn(client, _headers(private_key, email=STRANGER_EMAIL))

    assert response.status_code == 403
    assert response.json() == VERBATIM_403


# --- destroy stays operator-only, even for the agent's own owner -----------


def test_destroy_stays_operator_only_owner_alone_is_not_enough(session_factory, config):
    # Access-control Task A3 loosens every OTHER mutating surface to
    # owner-or-operator; destroy deliberately keeps its own, stricter,
    # operator-only `_operator_block` gate (the destruction arc's standing
    # rule). An owner who is not a listed operator must still be turned away
    # here, with destroy's own copy, not the new owner-or-operator one.
    private_key, jwk = _generate_jwk()
    client = _make_client(session_factory, config, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, config, created_by=OWNER_EMAIL)

    response = client.delete(
        f"/api/agents/{AGENT_NAME}", headers=_headers(private_key, email=OWNER_EMAIL)
    )

    assert response.status_code == 403
    assert response.json() == {"error": "Destroying agents is operator-only on this room"}


def test_destroy_still_works_for_a_listed_operator(session_factory, config):
    private_key, jwk = _generate_jwk()
    client = _make_client(session_factory, config, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, config, created_by=OWNER_EMAIL)

    response = client.delete(
        f"/api/agents/{AGENT_NAME}", headers=_headers(private_key, email=OPERATOR_EMAIL)
    )

    assert response.status_code == 202


# --- teach and chat stay open, by design, to any signed-in user ------------


def test_teach_stays_open_to_a_stranger(session_factory, config):
    private_key, jwk = _generate_jwk()
    client = _make_client(session_factory, config, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, config, created_by=OWNER_EMAIL)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/teach",
        json={"text": "always answer briefly"},
        headers=_headers(private_key, email=STRANGER_EMAIL),
    )

    assert response.status_code == 200


def test_chat_stays_open_to_a_stranger(session_factory, config):
    private_key, jwk = _generate_jwk()
    client = _make_client(session_factory, config, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, config, created_by=OWNER_EMAIL)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/chat",
        json={"messages": [{"role": "user", "text": "hi"}]},
        headers=_headers(private_key, email=STRANGER_EMAIL),
    )

    assert response.status_code == 200


# --- Task A3 fix round 1: three more mutating routes the sweep found -------
#
# The route sweep after 7b226bc landed found three more mutating agent
# routes the gate never reached: `delete_knowledge` (DELETE, PUT's sibling
# above via the "knowledge" gate-matrix entry -- both HIGH, a stranger could
# overwrite OR delete another agent's knowledge files), `retry_agent`
# (MEDIUM, a stranger could relaunch someone else's failed pipeline), and
# `sync_directory_route` (LOW, idempotent but gated for uniformity with
# every other mutating surface). Each gets its own small owner/operator/
# stranger trio here rather than folding into `GATE_MATRIX_ROUTES` above,
# since each needs fixture state that table's shared `_seed_agent`/
# `_make_client` pair doesn't carry (an existing knowledge file, a FAILED
# status, a wired switchboard) -- but the three assertions per route are
# the same ones that table already pins: stranger 403 verbatim, owner
# passes, operator passes without needing ownership.


# --- delete_knowledge (HIGH): needs an existing file for a delete to ---------
# actually succeed, unlike save_knowledge's fresh-filename PUT above.


def _delete_knowledge_client(session_factory, config, iap_jwks_fetcher):
    github = _CapturingGitHub(knowledge_files={"gotchas.md": "old content"})
    return _make_client(session_factory, config, github=github, iap_jwks_fetcher=iap_jwks_fetcher)


def test_knowledge_delete_owner_may_delete(session_factory, config):
    private_key, jwk = _generate_jwk()
    client = _delete_knowledge_client(session_factory, config, _fetcher(jwk))
    _seed_agent(session_factory, config, created_by=OWNER_EMAIL)

    response = client.delete(
        f"/api/agents/{AGENT_NAME}/knowledge/gotchas.md",
        headers=_headers(private_key, email=OWNER_EMAIL),
    )

    assert response.status_code == 200, response.text


def test_knowledge_delete_operator_may_delete_even_without_ownership(session_factory, config):
    private_key, jwk = _generate_jwk()
    client = _delete_knowledge_client(session_factory, config, _fetcher(jwk))
    _seed_agent(session_factory, config, created_by=OWNER_EMAIL)

    response = client.delete(
        f"/api/agents/{AGENT_NAME}/knowledge/gotchas.md",
        headers=_headers(private_key, email=OPERATOR_EMAIL),
    )

    assert response.status_code == 200, response.text


def test_knowledge_delete_stranger_gets_403_verbatim(session_factory, config):
    # This is the exact hole the sweep found: today, before the fix, this
    # stranger call actually deletes `gotchas.md` and returns 200.
    private_key, jwk = _generate_jwk()
    client = _delete_knowledge_client(session_factory, config, _fetcher(jwk))
    _seed_agent(session_factory, config, created_by=OWNER_EMAIL)

    response = client.delete(
        f"/api/agents/{AGENT_NAME}/knowledge/gotchas.md",
        headers=_headers(private_key, email=STRANGER_EMAIL),
    )

    assert response.status_code == 403
    assert response.json() == VERBATIM_403


# --- retry_agent (MEDIUM): inlines its own fetch/FAILED-status check -------
# (never `_ready_agent_or_error` -- see that helper's own docstring on why),
# so a FAILED-status agent is needed just to reach the gate at all; a READY
# one 409s on the status check before ever getting there. `run_pipeline` is
# monkeypatched to a recording no-op, same idiom as test_api_agents.py's own
# `test_retry_agent_race_second_call_409s_and_pipeline_fires_once`, so this
# never races a real daemon thread against the test's own session.


def _retry_client(session_factory, config, iap_jwks_fetcher, monkeypatch):
    import kobo_api.main as main_module

    monkeypatch.setattr(main_module, "run_pipeline", lambda *args, **kwargs: None)
    return _make_client(session_factory, config, iap_jwks_fetcher=iap_jwks_fetcher)


def test_retry_owner_may_relaunch(session_factory, config, monkeypatch):
    private_key, jwk = _generate_jwk()
    client = _retry_client(session_factory, config, _fetcher(jwk), monkeypatch)
    _seed_agent(session_factory, config, created_by=OWNER_EMAIL, status=AgentStatus.FAILED)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/retry", headers=_headers(private_key, email=OWNER_EMAIL)
    )

    assert response.status_code == 202, response.text


def test_retry_operator_may_relaunch_even_without_ownership(session_factory, config, monkeypatch):
    private_key, jwk = _generate_jwk()
    client = _retry_client(session_factory, config, _fetcher(jwk), monkeypatch)
    _seed_agent(session_factory, config, created_by=OWNER_EMAIL, status=AgentStatus.FAILED)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/retry", headers=_headers(private_key, email=OPERATOR_EMAIL)
    )

    assert response.status_code == 202, response.text


def test_retry_stranger_gets_403_verbatim(session_factory, config, monkeypatch):
    # This is the exact hole the sweep found: today, before the fix, this
    # stranger call actually relaunches someone else's failed pipeline and
    # returns 202.
    private_key, jwk = _generate_jwk()
    client = _retry_client(session_factory, config, _fetcher(jwk), monkeypatch)
    _seed_agent(session_factory, config, created_by=OWNER_EMAIL, status=AgentStatus.FAILED)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/retry", headers=_headers(private_key, email=STRANGER_EMAIL)
    )

    assert response.status_code == 403
    assert response.json() == VERBATIM_403


# --- sync_directory_route (LOW): needs the switchboard actually wired for --
# owner/operator to see a real 200, same setup as test_api_directory.py's
# own success test (cabal_url/agent_endpoint_template on the config, the
# CABAL_OPERATOR_TOKEN env var, and a real FakeCabal wired into Clients).


CABAL_ENDPOINT_TEMPLATE = "http://agent-{name}.default.svc.cluster.local"


def _enabled_cabal_config(config):
    return config.model_copy(
        update={
            "cabal_url": "http://cabal.internal",
            "agent_endpoint_template": CABAL_ENDPOINT_TEMPLATE,
        }
    )


def _directory_sync_client(session_factory, cfg, iap_jwks_fetcher, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    return _make_client(session_factory, cfg, cabal=FakeCabal(), iap_jwks_fetcher=iap_jwks_fetcher)


def test_directory_sync_owner_may_sync(session_factory, config, monkeypatch):
    cfg = _enabled_cabal_config(config)
    private_key, jwk = _generate_jwk()
    client = _directory_sync_client(session_factory, cfg, _fetcher(jwk), monkeypatch)
    _seed_agent(session_factory, cfg, created_by=OWNER_EMAIL)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/directory/sync",
        headers=_headers(private_key, email=OWNER_EMAIL),
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"registered": True}


def test_directory_sync_operator_may_sync_even_without_ownership(session_factory, config, monkeypatch):
    cfg = _enabled_cabal_config(config)
    private_key, jwk = _generate_jwk()
    client = _directory_sync_client(session_factory, cfg, _fetcher(jwk), monkeypatch)
    _seed_agent(session_factory, cfg, created_by=OWNER_EMAIL)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/directory/sync",
        headers=_headers(private_key, email=OPERATOR_EMAIL),
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"registered": True}


def test_directory_sync_stranger_gets_403_verbatim(session_factory, config, monkeypatch):
    # This is the exact hole the sweep found: today, before the fix, this
    # stranger call actually re-registers the agent in the switchboard's
    # directory and returns 200.
    cfg = _enabled_cabal_config(config)
    private_key, jwk = _generate_jwk()
    client = _directory_sync_client(session_factory, cfg, _fetcher(jwk), monkeypatch)
    _seed_agent(session_factory, cfg, created_by=OWNER_EMAIL)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/directory/sync",
        headers=_headers(private_key, email=STRANGER_EMAIL),
    )

    assert response.status_code == 403
    assert response.json() == VERBATIM_403
