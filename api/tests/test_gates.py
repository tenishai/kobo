"""The route gates, each tested once over a route table.

A gate is a rule, not a route: owner-or-operator, operator-only, the
connected-repository guard, visibility (404, never 403, for an agent the
caller cannot see), the ready-state 409 and the unknown-agent 404. Every
rule here is one parametrized test over a hand-written table of routes, so
a new route is covered by adding a row and a per-route copy of a gate test
is not wanted (TESTING.md, Layout).

The route-table idiom and the IAP-JWT fixtures come from the two tables the
2026-10-01 wave wrote: test_web_ownership_gate.py and
test_web_connected_guard.py, folded here on 2026-10-29.
"""

from __future__ import annotations

import json
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
from kobo_api.config import Config, GroupChoice
from kobo_api.iap import ASSERTION_HEADER, IAP_ISSUER
from kobo_api.kinds import KIND_COMPOSED, KIND_CONNECTED
from kobo_api.main import (
    PNG_SIGNATURE,
    _operator_block,
    _owner_or_operator_or_403,
    create_app,
)
from kobo_api.models import (
    GRANT_ROLE_MANAGE,
    GRANT_ROLE_VIEW,
    Agent,
    AgentLearning,
    AgentStatus,
    AgentUserGrant,
    Base,
)
from kobo_api.personas import seed_default_personas
from kobo_api.slack_state import encode_channels
from tests.fakes import (
    FakeAgent,
    FakeCabal,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeImage,
    FakeKubectl,
    FakeSlack,
    FakeStorage,
)

# --- from test_web_ownership_gate.py ---


AGENT_NAME = "clio"


PERSONA = "helpful-generalist"


REPO = "acme/agent-clio"


OWNER_EMAIL = "owner@example.com"


OPERATOR_EMAIL = "Ops@Example.com"


STRANGER_EMAIL = "stranger@example.com"


MANAGER_EMAIL = "manager@example.com"


VERBATIM_403 = {"error": "Only this agent's owner or a room operator can change it."}


AUDIENCE = "test-audience"


KID = "test-key-1"


def _req(email: str | None, config__web_ownership_gate: Config):
    """A minimal stand-in for `Request` -- the helper only ever reads
    `request.state.iap_email` (via `getattr`) and `request.app.state.config`,
    so a bare namespace tree is enough; when `email` is None, `.state`
    carries no `iap_email` attribute at all, mirroring a real request that
    never went through IapMiddleware. Same idiom as test_web_slack_gate.py's
    own `_req`, extended one level for `.app.state.config`."""
    state = SimpleNamespace()
    if email is not None:
        state.iap_email = email
    return SimpleNamespace(state=state, app=SimpleNamespace(state=SimpleNamespace(config=config__web_ownership_gate)))


def _agent(created_by: str | None):
    """A minimal stand-in for `Agent` -- the helper only ever reads
    `.created_by`."""
    return SimpleNamespace(created_by=created_by)


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
def session_factory__web_ownership_gate(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__web_ownership_gate(tmp_path):
    return Config(
        workdir=tmp_path / "work",
        operators=[OPERATOR_EMAIL],
        iap_audience=AUDIENCE,
        # The room-made Slack app routes (issue #371) exist only with the
        # switch on; off they are a 404 before any gate.
        slack_relay=True,
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


def _make_client(session_factory__web_ownership_gate, config__web_ownership_gate, *, github=None, cabal=None, iap_jwks_fetcher=None):
    return TestClient(
        create_app(
            session_factory__web_ownership_gate,
            config=config__web_ownership_gate,
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
    session_factory__web_ownership_gate,
    config__web_ownership_gate,
    *,
    created_by=None,
    status=AgentStatus.READY,
    repo_full_name=REPO,
    skills=None,
):
    session = session_factory__web_ownership_gate()
    session.add(
        Agent(
            name=AGENT_NAME,
            persona=PERSONA,
            model_id=config__web_ownership_gate.models[0].id,
            status=status,
            repo_full_name=repo_full_name,
            created_by=created_by,
            skills=skills if skills is not None else [],
        )
    )
    session.commit()
    session.close()


def _seed_grant(session_factory__web_ownership_gate, *, email: str, role: str):
    session = session_factory__web_ownership_gate()
    session.add(AgentUserGrant(agent_name=AGENT_NAME, email=email, role=role))
    session.commit()
    session.close()


def _headers(private_key, *, email: str | None) -> dict:
    if email is None:
        return {}
    return {ASSERTION_HEADER: _sign(private_key, email=email)}


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


def _create_slack_app(client, headers):
    return client.post(f"/api/agents/{AGENT_NAME}/slack/app", json={"wake": "thread"}, headers=headers)


def _remove_slack_app(client, headers):
    return client.delete(f"/api/agents/{AGENT_NAME}/slack/app", headers=headers)


def _set_colleague_mode(client, headers):
    return client.put(
        f"/api/agents/{AGENT_NAME}/colleague-mode", json={"mode": "roster-only"}, headers=headers
    )


def _delete_knowledge(client, headers):
    return client.delete(f"/api/agents/{AGENT_NAME}/knowledge/gotchas.md", headers=headers)


def _retry(client, headers):
    return client.post(f"/api/agents/{AGENT_NAME}/retry", headers=headers)


def _sync_directory(client, headers):
    return client.post(f"/api/agents/{AGENT_NAME}/directory/sync", headers=headers)


def _set_persona(client, headers):
    return client.put(
        f"/api/agents/{AGENT_NAME}/persona", json={"persona": PERSONA}, headers=headers
    )


def _restart(client, headers):
    return client.post(f"/api/agents/{AGENT_NAME}/restart", headers=headers)


def _read_secrets(client, headers):
    return client.get(f"/api/agents/{AGENT_NAME}/secrets", headers=headers)


def _write_secrets(client, headers):
    return client.put(
        f"/api/agents/{AGENT_NAME}/secrets", json={"set": {"API_KEY": "x"}}, headers=headers
    )


def _read_env(client, headers):
    return client.get(f"/api/agents/{AGENT_NAME}/env", headers=headers)


def _write_env(client, headers):
    return client.put(f"/api/agents/{AGENT_NAME}/env", json={"set": {"MODE": "x"}}, headers=headers)


def _list_grants(client, headers):
    return client.get(f"/api/agents/{AGENT_NAME}/grants", headers=headers)


def _add_grant(client, headers):
    return client.post(
        f"/api/agents/{AGENT_NAME}/grants", json={"email": "friend@example.com"}, headers=headers
    )


def _remove_grant(client, headers):
    return client.delete(f"/api/agents/{AGENT_NAME}/grants/friend@example.com", headers=headers)


def _list_invites(client, headers):
    return client.get(f"/api/agents/{AGENT_NAME}/invites", headers=headers)


def _create_invite(client, headers):
    return client.post(
        f"/api/agents/{AGENT_NAME}/invites", json={"to_agent": "ward"}, headers=headers
    )


def _list_requestable(client, headers):
    return client.get(f"/api/agents/{AGENT_NAME}/requestable", headers=headers)


def _read_build_log(client, headers):
    return client.get(f"/api/agents/{AGENT_NAME}/build-log", headers=headers)


# Every other route wired to `_owner_or_operator_or_403`, by the path from
# its decorator to the gate (the map is in main.py; a route that gains the
# gate gains a row here). The stranger never reaches the route's own
# business logic, so the bodies only have to parse. Routes that answer
# "this lane is off for the room" before any gate (repo-access, access
# requests) are gated in test_auth.py and test_access.py with the lane on.
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
    ("slack-app-create", _create_slack_app),
    ("slack-app-remove", _remove_slack_app),
    ("colleague-mode", _set_colleague_mode),
    ("knowledge-delete", _delete_knowledge),
    ("retry", _retry),
    ("directory-sync", _sync_directory),
    ("persona", _set_persona),
    ("restart", _restart),
    ("secrets-read", _read_secrets),
    ("secrets-write", _write_secrets),
    ("env-read", _read_env),
    ("env-write", _write_env),
    ("grants-list", _list_grants),
    ("grants-add", _add_grant),
    ("grants-remove", _remove_grant),
    ("invites-list", _list_invites),
    ("invites-create", _create_invite),
    ("requestable", _list_requestable),
    ("build-log", _read_build_log),
]

# A route that only reaches its gate from a particular status seeds that
# status: retry refuses a READY agent on its own 409 before the gate.
SMOKE_ROUTE_STATUS = {"retry": AgentStatus.FAILED}


def test_helper_operator_passes_even_without_ownership():
    """Gate (ownership): Helper operator passes even without ownership."""
    config__web_ownership_gate = Config(operators=[OPERATOR_EMAIL], iap_audience=AUDIENCE)

    result = _owner_or_operator_or_403(_agent(OWNER_EMAIL), _req(OPERATOR_EMAIL, config__web_ownership_gate))

    assert result is None


def test_helper_operator_match_is_case_insensitive():
    """Gate (ownership): Helper operator match is case insensitive."""
    config__web_ownership_gate = Config(operators=["ops@example.com"], iap_audience=AUDIENCE)

    result = _owner_or_operator_or_403(
        _agent(None), _req("OPS@EXAMPLE.COM", config__web_ownership_gate)
    )

    assert result is None


def test_helper_owner_passes_even_when_not_an_operator():
    """Gate (ownership): Helper owner passes even when not an operator."""
    config__web_ownership_gate = Config(operators=[], iap_audience=AUDIENCE)

    result = _owner_or_operator_or_403(_agent(OWNER_EMAIL), _req(OWNER_EMAIL, config__web_ownership_gate))

    assert result is None


def test_helper_stranger_returns_403_verbatim():
    """Gate (ownership): Helper stranger returns 403 verbatim."""
    config__web_ownership_gate = Config(operators=[OPERATOR_EMAIL], iap_audience=AUDIENCE)

    result = _owner_or_operator_or_403(_agent(OWNER_EMAIL), _req(STRANGER_EMAIL, config__web_ownership_gate))

    assert result is not None
    assert result.status_code == 403
    import json

    assert json.loads(result.body) == VERBATIM_403


def test_helper_no_email_at_all_returns_403():
    """Gate (ownership): Helper no email at all returns 403."""
    config__web_ownership_gate = Config(operators=[OPERATOR_EMAIL], iap_audience=AUDIENCE)

    result = _owner_or_operator_or_403(_agent(OWNER_EMAIL), _req(None, config__web_ownership_gate))

    assert result is not None
    assert result.status_code == 403


def test_helper_ownerless_agent_blocks_a_non_operator():
    # The fail-closed branch: a legacy/no-owner agent is operator-only to
    # mutate, even for a caller with a perfectly good identity.
    """Gate (ownership): Helper ownerless agent blocks a non operator."""
    config__web_ownership_gate = Config(operators=[OPERATOR_EMAIL], iap_audience=AUDIENCE)

    result = _owner_or_operator_or_403(_agent(None), _req(STRANGER_EMAIL, config__web_ownership_gate))

    assert result is not None
    assert result.status_code == 403


def test_helper_iap_off_bypasses_even_with_no_owner_and_no_email():
    # IAP disabled: the room trusts its perimeter only when it has one, same
    # escape hatch _operator_block/_slack_operator_block already use. Without
    # this, a room that never turns IAP on could never mutate any of these
    # routes again once an agent's created_by is null -- which is every
    # agent, since IAP-off means created_by is never recorded either.
    """Gate (ownership): Helper iap off bypasses even with no owner and no email."""
    config__web_ownership_gate = Config(operators=[], iap_audience="")

    result = _owner_or_operator_or_403(_agent(None), _req(None, config__web_ownership_gate))

    assert result is None


@pytest.mark.parametrize("class_name,request_fn", GATE_MATRIX_ROUTES, ids=[c for c, _ in GATE_MATRIX_ROUTES])
def test_owner_may_mutate(session_factory__web_ownership_gate, config__web_ownership_gate, class_name, request_fn):
    """Gate (ownership): Owner may mutate."""
    private_key, jwk = _generate_jwk()
    client = _make_client(session_factory__web_ownership_gate, config__web_ownership_gate, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory__web_ownership_gate, config__web_ownership_gate, created_by=OWNER_EMAIL)

    response = request_fn(client, _headers(private_key, email=OWNER_EMAIL))

    assert response.status_code == 200, response.text


@pytest.mark.parametrize("class_name,request_fn", GATE_MATRIX_ROUTES, ids=[c for c, _ in GATE_MATRIX_ROUTES])
def test_operator_may_mutate_even_without_ownership(session_factory__web_ownership_gate, config__web_ownership_gate, class_name, request_fn):
    """Gate (ownership): Operator may mutate even without ownership."""
    private_key, jwk = _generate_jwk()
    client = _make_client(session_factory__web_ownership_gate, config__web_ownership_gate, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory__web_ownership_gate, config__web_ownership_gate, created_by=OWNER_EMAIL)

    response = request_fn(client, _headers(private_key, email=OPERATOR_EMAIL))

    assert response.status_code == 200, response.text


@pytest.mark.parametrize("class_name,request_fn", GATE_MATRIX_ROUTES, ids=[c for c, _ in GATE_MATRIX_ROUTES])
def test_stranger_gets_403_verbatim(session_factory__web_ownership_gate, config__web_ownership_gate, class_name, request_fn):
    """Gate (ownership): Stranger gets 403 verbatim."""
    private_key, jwk = _generate_jwk()
    client = _make_client(session_factory__web_ownership_gate, config__web_ownership_gate, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory__web_ownership_gate, config__web_ownership_gate, created_by=OWNER_EMAIL)

    response = request_fn(client, _headers(private_key, email=STRANGER_EMAIL))

    assert response.status_code == 403
    assert response.json() == VERBATIM_403


@pytest.mark.parametrize("class_name,request_fn", GATE_MATRIX_ROUTES, ids=[c for c, _ in GATE_MATRIX_ROUTES])
def test_ownerless_agent_is_operator_only(session_factory__web_ownership_gate, config__web_ownership_gate, class_name, request_fn):
    """Gate (ownership): Ownerless agent is operator only."""
    private_key, jwk = _generate_jwk()
    client = _make_client(session_factory__web_ownership_gate, config__web_ownership_gate, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory__web_ownership_gate, config__web_ownership_gate, created_by=None)

    stranger_response = request_fn(client, _headers(private_key, email=STRANGER_EMAIL))
    assert stranger_response.status_code == 403
    assert stranger_response.json() == VERBATIM_403

    operator_response = request_fn(client, _headers(private_key, email=OPERATOR_EMAIL))
    assert operator_response.status_code == 200, operator_response.text


@pytest.mark.parametrize("class_name,request_fn", GATE_MATRIX_ROUTES, ids=[c for c, _ in GATE_MATRIX_ROUTES])
def test_manage_grant_passes_the_ownership_gate_but_not_destroy_or_grants(
    session_factory__web_ownership_gate, config__web_ownership_gate, class_name, request_fn
):
    """Gate (ownership): a `manage` grant (issue #309) mutates like the owner;
    a `view` grant does not; neither destroys (operator-only) nor adds grants
    (owner or operator only)."""
    private_key, jwk = _generate_jwk()
    client = _make_client(session_factory__web_ownership_gate, config__web_ownership_gate, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory__web_ownership_gate, config__web_ownership_gate, created_by=OWNER_EMAIL)
    _seed_grant(session_factory__web_ownership_gate, email=MANAGER_EMAIL, role=GRANT_ROLE_MANAGE)
    _seed_grant(session_factory__web_ownership_gate, email=STRANGER_EMAIL, role=GRANT_ROLE_VIEW)
    manager = _headers(private_key, email=MANAGER_EMAIL)

    assert request_fn(client, manager).status_code == 200
    assert request_fn(client, _headers(private_key, email=STRANGER_EMAIL)).status_code == 403
    assert client.delete(f"/api/agents/{AGENT_NAME}", headers=manager).status_code == 403
    assert _add_grant(client, manager).status_code == 403


@pytest.mark.parametrize("route_name,request_fn", SMOKE_ROUTES, ids=[r for r, _ in SMOKE_ROUTES])
def test_stranger_is_blocked_on_every_other_gated_route(
    session_factory__web_ownership_gate, config__web_ownership_gate, route_name, request_fn
):
    """Gate (ownership): Stranger is blocked on every other gated route."""
    private_key, jwk = _generate_jwk()
    client = _make_client(session_factory__web_ownership_gate, config__web_ownership_gate, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(
        session_factory__web_ownership_gate,
        config__web_ownership_gate,
        created_by=OWNER_EMAIL,
        status=SMOKE_ROUTE_STATUS.get(route_name, AgentStatus.READY),
    )

    response = request_fn(client, _headers(private_key, email=STRANGER_EMAIL))

    assert response.status_code == 403, response.text
    assert response.json() == VERBATIM_403


# Every route that goes through `_ready_agent_or_error`: 409 with the agent's
# status while it is not READY, 404 when there is no such agent. One row per
# route; the per-route copies (`*_non_ready_agent_returns_409`,
# `*_unknown_agent_returns_404`) were folded here on 2026-10-29.
READY_GATED_ROUTES = [
    ("GET", f"/api/agents/{AGENT_NAME}/knowledge", {}),
    ("PUT", f"/api/agents/{AGENT_NAME}/knowledge/notes.md", {"json": {"content": "hi"}}),
    ("DELETE", f"/api/agents/{AGENT_NAME}/knowledge/notes.md", {}),
    ("GET", f"/api/agents/{AGENT_NAME}/knowledge/delivery/" + "a" * 40, {}),
    ("GET", f"/api/agents/{AGENT_NAME}/skills", {}),
    ("POST", f"/api/agents/{AGENT_NAME}/skills/catalog", {"json": {"name": "word-games", "team": "shared"}}),
    (
        "POST",
        f"/api/agents/{AGENT_NAME}/skills/catalog/batch",
        {"json": {"skills": [{"name": "word-games", "team": "shared"}]}},
    ),
    ("DELETE", f"/api/agents/{AGENT_NAME}/skills/word-games", {}),
    ("GET", f"/api/agents/{AGENT_NAME}/skills/local/notes", {}),
    ("PUT", f"/api/agents/{AGENT_NAME}/skills/local/notes", {"json": {"content": "# Notes\n"}}),
    ("GET", f"/api/agents/{AGENT_NAME}/intake", {}),
    ("POST", f"/api/agents/{AGENT_NAME}/intake", {"json": {"filename": "note.md", "content": "hello"}}),
    ("DELETE", f"/api/agents/{AGENT_NAME}/intake/note.md", {}),
    ("POST", f"/api/agents/{AGENT_NAME}/teach", {"json": {"text": "remember this"}}),
    ("GET", f"/api/agents/{AGENT_NAME}/tooling", {}),
    ("POST", f"/api/agents/{AGENT_NAME}/tooling", {"json": {"filename": "helper.py", "content": "def go():\n    return 1\n"}}),
    ("POST", f"/api/agents/{AGENT_NAME}/tooling/import", {"json": {"repo": "acme/other", "path": "tools"}}),
    ("DELETE", f"/api/agents/{AGENT_NAME}/tooling/helper.py", {}),
    ("GET", f"/api/agents/{AGENT_NAME}/slack/manifest", {}),
    (
        "POST",
        f"/api/agents/{AGENT_NAME}/slack/connect",
        {"json": {"app_id": "A01234567", "bot_token": "xoxb-x", "app_token": "xapp-x"}},
    ),
    ("PUT", f"/api/agents/{AGENT_NAME}/slack/channels", {"json": {"channels": ["C01234567"]}}),
    ("PUT", f"/api/agents/{AGENT_NAME}/slack/wake", {"json": {"wake": "thread"}}),
    ("POST", f"/api/agents/{AGENT_NAME}/slack/app", {"json": {"wake": "thread"}}),
    ("DELETE", f"/api/agents/{AGENT_NAME}/slack/app", {}),
    ("PUT", f"/api/agents/{AGENT_NAME}/persona", {"json": {"persona": PERSONA}}),
    ("PATCH", f"/api/agents/{AGENT_NAME}/model", {"json": {"model": "vertex_ai/claude-sonnet-4-5"}}),
    ("POST", f"/api/agents/{AGENT_NAME}/restart", {}),
    ("PATCH", f"/api/agents/{AGENT_NAME}/runtime", {"json": {"runtime_image": "example.dev/runtime:v2"}}),
    ("PUT", f"/api/agents/{AGENT_NAME}/colleague-mode", {"json": {"mode": "roster-only"}}),
    ("PATCH", f"/api/agents/{AGENT_NAME}/metadata", {"json": {"purpose_tags": ["billing"]}}),
    ("POST", f"/api/agents/{AGENT_NAME}/metadata/regenerate", {}),
    ("POST", f"/api/agents/{AGENT_NAME}/directory/sync", {}),
    ("POST", f"/api/agents/{AGENT_NAME}/avatar", {}),
    ("PUT", f"/api/agents/{AGENT_NAME}/avatar", {"content": PNG_SIGNATURE + b"rest", "headers": {"Content-Type": "image/png"}}),
    # not ready-gated but visibility-fetched: an unknown name is still a 404
    ("POST", f"/api/agents/{AGENT_NAME}/chat", {"json": {"messages": [{"role": "user", "text": "hi"}]}}),
]


def _operator_request(client, private_key, method, path, body):
    kwargs = dict(body)
    headers = {**_headers(private_key, email=OPERATOR_EMAIL), **kwargs.pop("headers", {})}
    return client.request(method, path, headers=headers, **kwargs)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    READY_GATED_ROUTES,
    ids=[f"{m} {p}" for m, p, _ in READY_GATED_ROUTES],
)
def test_every_ready_gated_route_is_409_while_the_agent_is_not_ready(
    session_factory__web_ownership_gate, config__web_ownership_gate, method, path, body
):
    """Gate (visibility): a route on an agent that is not READY answers 409 with its status."""
    if path.endswith("/chat"):
        pytest.skip("chat has its own not-ready answer; the unknown-agent row is what it shares")
    private_key, jwk = _generate_jwk()
    client = _make_client(session_factory__web_ownership_gate, config__web_ownership_gate, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(
        session_factory__web_ownership_gate,
        config__web_ownership_gate,
        created_by=OWNER_EMAIL,
        status=AgentStatus.PENDING,
    )

    response = _operator_request(client, private_key, method, path, body)

    assert response.status_code == 409, f"{method} {path}: {response.status_code} {response.text}"
    assert any(word in response.json()["error"] for word in ("ready", "running")), response.text


@pytest.mark.parametrize(
    ("method", "path", "body"),
    READY_GATED_ROUTES,
    ids=[f"{m} {p}" for m, p, _ in READY_GATED_ROUTES],
)
def test_every_agent_route_is_404_for_an_unknown_agent(
    session_factory__web_ownership_gate, config__web_ownership_gate, method, path, body
):
    """Gate (visibility): a route on a name the room does not have answers 404."""
    private_key, jwk = _generate_jwk()
    client = _make_client(session_factory__web_ownership_gate, config__web_ownership_gate, iap_jwks_fetcher=_fetcher(jwk))

    response = _operator_request(client, private_key, method, path, body)

    assert response.status_code == 404, f"{method} {path}: {response.status_code} {response.text}"


def test_destroy_stays_operator_only_owner_alone_is_not_enough(session_factory__web_ownership_gate, config__web_ownership_gate):
    # Access-control Task A3 loosens every OTHER mutating surface to
    # owner-or-operator; destroy deliberately keeps its own, stricter,
    # operator-only `_operator_block` gate (the destruction arc's standing
    # rule). An owner who is not a listed operator must still be turned away
    # here, with destroy's own copy, not the new owner-or-operator one.
    """Gate (ownership): Destroy stays operator only owner alone is not enough."""
    private_key, jwk = _generate_jwk()
    client = _make_client(session_factory__web_ownership_gate, config__web_ownership_gate, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory__web_ownership_gate, config__web_ownership_gate, created_by=OWNER_EMAIL)

    response = client.delete(
        f"/api/agents/{AGENT_NAME}", headers=_headers(private_key, email=OWNER_EMAIL)
    )

    assert response.status_code == 403
    assert response.json() == {"error": "Destroying agents is operator-only on this room"}


def test_destroy_still_works_for_a_listed_operator(session_factory__web_ownership_gate, config__web_ownership_gate):
    """Gate (ownership): Destroy still works for a listed operator."""
    private_key, jwk = _generate_jwk()
    client = _make_client(session_factory__web_ownership_gate, config__web_ownership_gate, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory__web_ownership_gate, config__web_ownership_gate, created_by=OWNER_EMAIL)

    response = client.delete(
        f"/api/agents/{AGENT_NAME}", headers=_headers(private_key, email=OPERATOR_EMAIL)
    )

    assert response.status_code == 202


def test_teach_stays_open_to_a_stranger(session_factory__web_ownership_gate, config__web_ownership_gate):
    """Gate (ownership): Teach stays open to a stranger."""
    private_key, jwk = _generate_jwk()
    client = _make_client(session_factory__web_ownership_gate, config__web_ownership_gate, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory__web_ownership_gate, config__web_ownership_gate, created_by=OWNER_EMAIL)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/teach",
        json={"text": "always answer briefly"},
        headers=_headers(private_key, email=STRANGER_EMAIL),
    )

    assert response.status_code == 200


def test_chat_stays_open_to_a_stranger(session_factory__web_ownership_gate, config__web_ownership_gate):
    """Gate (ownership): Chat stays open to a stranger."""
    private_key, jwk = _generate_jwk()
    client = _make_client(session_factory__web_ownership_gate, config__web_ownership_gate, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory__web_ownership_gate, config__web_ownership_gate, created_by=OWNER_EMAIL)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/chat",
        json={"messages": [{"role": "user", "text": "hi"}]},
        headers=_headers(private_key, email=STRANGER_EMAIL),
    )

    assert response.status_code == 200


# --- from test_web_connected_guard.py ---


AGENT_NAME = "clio"


REPO__web_connected_guard = "example-org/team-chatbot"


LEARNING_ID = 1


REFUSAL = "this agent's repository belongs to its team, so the room does not change it"


# A 1x1 PNG, for the avatar upload route's own magic-byte check.
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08"
    b"\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00"
    b"\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


# Every route that reaches a repo write, by the path the reviewer walked
# from its decorator down to `github.commit_push`. `files`/`data` carry a
# minimally valid body: the guard must fire BEFORE any validation that
# would otherwise 422 first, so a body here that is merely plausible is
# enough, and a row that starts 422-ing is itself a finding.
GUARDED_ROUTES = [
    ("PUT", f"/api/agents/{AGENT_NAME}/knowledge/notes.md", {"json": {"content": "hi"}}),
    ("DELETE", f"/api/agents/{AGENT_NAME}/knowledge/notes.md", {}),
    (
        "POST",
        f"/api/agents/{AGENT_NAME}/skills/catalog",
        {"json": {"name": "word-games", "team": "shared"}},
    ),
    (
        "POST",
        f"/api/agents/{AGENT_NAME}/skills/catalog/batch",
        {"json": {"skills": [{"name": "word-games", "team": "shared"}]}},
    ),
    ("DELETE", f"/api/agents/{AGENT_NAME}/skills/word-games", {}),
    (
        "PUT",
        f"/api/agents/{AGENT_NAME}/skills/local/note-taking",
        {"json": {"content": "# Note taking\n"}},
    ),
    (
        "POST",
        f"/api/agents/{AGENT_NAME}/intake",
        {"json": {"filename": "note.md", "content": "hello"}},
    ),
    ("DELETE", f"/api/agents/{AGENT_NAME}/intake/note.md", {}),
    ("POST", f"/api/agents/{AGENT_NAME}/teach", {"json": {"text": "remember this"}}),
    (
        "POST",
        f"/api/agents/{AGENT_NAME}/learnings/{LEARNING_ID}/approve",
        {"json": {}},
    ),
    (
        "POST",
        f"/api/agents/{AGENT_NAME}/learnings/bulk",
        {"json": {"action": "approve", "ids": [LEARNING_ID]}},
    ),
    (
        "POST",
        f"/api/agents/{AGENT_NAME}/tooling",
        {"json": {"filename": "helper.py", "content": "def go():\n    return 1\n"}},
    ),
    (
        "POST",
        f"/api/agents/{AGENT_NAME}/tooling/import",
        {"json": {"repo": "example-org/other", "path": "tools/helper.py"}},
    ),
    ("DELETE", f"/api/agents/{AGENT_NAME}/tooling/helper.py", {}),
    (
        "POST",
        f"/api/agents/{AGENT_NAME}/slack/connect",
        {
            "json": {
                "app_id": "A01234567",
                "bot_token": "xoxb-placeholder",
                "app_token": "xapp-placeholder",
            }
        },
    ),
    (
        "PUT",
        f"/api/agents/{AGENT_NAME}/slack/channels",
        {"json": {"channels": ["C01234567"]}},
    ),
    (
        "PATCH",
        f"/api/agents/{AGENT_NAME}/runtime",
        {"json": {"runtime_image": "example.dev/runtime@sha256:" + "a" * 64}},
    ),
    (
        "PATCH",
        f"/api/agents/{AGENT_NAME}/metadata",
        {"json": {"purpose_tags": ["billing"]}},
    ),
    ("POST", f"/api/agents/{AGENT_NAME}/metadata/regenerate", {"json": {}}),
    ("POST", f"/api/agents/{AGENT_NAME}/avatar", {"json": {}}),
    (
        "PUT",
        f"/api/agents/{AGENT_NAME}/avatar",
        {"files": {"file": ("avatar.png", PNG_BYTES, "image/png")}},
    ),
    # Not a repo write, but the same rule: registering a connected agent in
    # the switchboard would create a directory entry destroy then declines
    # to retire, because a connected agent is never listed there.
    ("POST", f"/api/agents/{AGENT_NAME}/directory/sync", {"json": {}}),
    # Folded 2026-10-29 from their own files: the colleague document and a
    # chat relay are the room's hand on a bundle it does not own.
    ("PUT", f"/api/agents/{AGENT_NAME}/colleague-mode", {"json": {"mode": "roster-only"}}),
    ("POST", f"/api/agents/{AGENT_NAME}/chat", {"json": {"messages": [{"role": "user", "text": "hi"}]}}),
    ("POST", f"/api/agents/{AGENT_NAME}/slack/app", {"json": {"wake": "thread"}}),
]


# What this guard must NOT touch. Reading an agent is never refused: a
# connected agent's status, checklist and build log are as real as any
# other's, and an empty skill or knowledge listing is an honest answer.
READ_ROUTES = [
    f"/api/agents/{AGENT_NAME}",
    f"/api/agents/{AGENT_NAME}/knowledge",
    f"/api/agents/{AGENT_NAME}/skills",
    f"/api/agents/{AGENT_NAME}/intake",
    f"/api/agents/{AGENT_NAME}/tooling",
]


@pytest.fixture
def session_factory__web_connected_guard(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__web_connected_guard(tmp_path, fake_storage):
    return Config(
        workdir=tmp_path / "work",
        skills_bucket="skills-bucket",
        personas_bucket="personas-bucket",
        cabal_url="http://cabal.internal",
        agent_endpoint_template="http://agent-{name}.default.svc.cluster.local",
        slack_relay=True,
    )


@pytest.fixture
def github():
    return FakeGitHub(existing_repos={REPO__web_connected_guard})


@pytest.fixture
def clients(github, fake_storage):
    return Clients(
        github=github,
        gojira=FakeGojira(),
        cloud=FakeCloud(),
        agent=FakeAgent(reply="sure"),
        kubectl=FakeKubectl(),
        storage=fake_storage,
        slack=FakeSlack(),
        cabal=FakeCabal(),
        image=FakeImage(),
    )


def _seed(session_factory__web_connected_guard, *, kind: str) -> None:
    session = session_factory__web_connected_guard()
    agent = Agent(
        name=AGENT_NAME,
        persona="connected" if kind == KIND_CONNECTED else "helpful-generalist",
        model_id="vertex_ai/claude-sonnet-4-5",
        skills=[],
        status=AgentStatus.READY,
        repo_full_name=REPO__web_connected_guard,
        kind=kind,
    )
    session.add(agent)
    session.add(
        AgentLearning(
            id=LEARNING_ID,
            agent_name=AGENT_NAME,
            day="2026-09-21",
            content="the billing export runs at 02:00",
            kind="fact",
            source="Slack, Sep 21",
            evidence="quote",
            content_hash="a" * 64,
        )
    )
    session.commit()
    session.close()


@pytest.fixture
def client__web_connected_guard(session_factory__web_connected_guard, config__web_connected_guard, clients, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    return TestClient(create_app(session_factory__web_connected_guard, config=config__web_connected_guard, clients=clients))


def _touched_the_repo(github: FakeGitHub) -> list[str]:
    reaching = ("clone:", "commit_push:", "open_pr:", "create_repo_from_dir:")
    return [call for call in github.calls if call.startswith(reaching)]


@pytest.mark.parametrize(
    ("method", "path", "body"),
    GUARDED_ROUTES,
    ids=[f"{method} {path}" for method, path, _ in GUARDED_ROUTES],
)
def test_every_repo_writing_route_refuses_a_connected_agent(
    client__web_connected_guard, session_factory__web_connected_guard, github, method, path, body
):
    """Gate (ownership): Every repo writing route refuses a connected agent."""
    _seed(session_factory__web_connected_guard, kind=KIND_CONNECTED)

    response = client__web_connected_guard.request(method, path, **body)

    assert response.status_code == 409, response.text
    assert response.json()["error"] == REFUSAL
    assert _touched_the_repo(github) == []


@pytest.mark.parametrize(
    ("method", "path", "body"),
    GUARDED_ROUTES,
    ids=[f"{method} {path}" for method, path, _ in GUARDED_ROUTES],
)
def test_no_repo_writing_route_refuses_a_composed_agent_with_this_message(
    client__web_connected_guard, session_factory__web_connected_guard, method, path, body
):
    """Gate (ownership): No repo writing route refuses a composed agent with this message.

    The guard must be about the kind and nothing else. A composed agent
    may still fail these calls for its own reasons (a missing skill, a
    Slack client that is not wired) -- it must never fail with THIS
    sentence."""
    _seed(session_factory__web_connected_guard, kind=KIND_COMPOSED)

    response = client__web_connected_guard.request(method, path, **body)

    if response.status_code == 409:
        assert response.json().get("error") != REFUSAL


@pytest.mark.parametrize("path", READ_ROUTES)
def test_reading_a_connected_agent_is_never_refused(client__web_connected_guard, session_factory__web_connected_guard, path):
    """Gate (ownership): Reading a connected agent is never refused."""
    _seed(session_factory__web_connected_guard, kind=KIND_CONNECTED)

    response = client__web_connected_guard.get(path)

    assert response.status_code == 200, response.text


def test_listing_skills_for_a_connected_agent_never_pushes(
    client__web_connected_guard, session_factory__web_connected_guard, github
):
    """Gate (ownership): Listing skills for a connected agent never pushes.

    `GET .../skills` reconciles the ledger against the bundle and can
    COMMIT rows to the database, which is fine. It must never push."""
    _seed(session_factory__web_connected_guard, kind=KIND_CONNECTED)

    response = client__web_connected_guard.get(f"/api/agents/{AGENT_NAME}/skills")

    assert response.status_code == 200
    assert not any(call.startswith("commit_push:") for call in github.calls)
    assert not any(call.startswith("open_pr:") for call in github.calls)


# --- from test_web_destroy.py ---


AGENT_NAME = "clio"


OWNER = "acme-corp"


REPO__web_destroy = f"{OWNER}/agent-{AGENT_NAME}"


NON_OPERATOR_EMAIL = "someone-else@example.com"


VERBATIM_403__web_destroy = {"error": "Destroying agents is operator-only on this room"}


AUDIENCE = "test-audience"


KID = "test-key-1"


def _req__web_destroy(email: str | None, config__web_destroy: Config):
    """A minimal stand-in for `Request` -- `_operator_block` reads `request.
    state.iap_email` directly, but now also reaches `_is_admin(request)`
    (operator-status-by-group ruling), which reads `request.app.state.
    config`/`.clients` -- so this stand-in carries the same `config` the
    test already passes as `_operator_block`'s own second argument, same
    idiom test_web_ownership_gate.py's own `_req` uses. `clients` is left
    unset (not even `None`-valued): `_is_admin` reaches it through `getattr`
    with a default, so a resolver-free room needs nothing here at all."""
    state = SimpleNamespace()
    if email is not None:
        state.iap_email = email
    return SimpleNamespace(state=state, app=SimpleNamespace(state=SimpleNamespace(config=config__web_destroy)))


ADMIN_GROUP = "infra-engineering"


ADMIN_MEMBER_EMAIL = "admin-member@example.com"


class FakeGroupResolver:
    """The plainest possible `GroupResolver` (kobo_api.groups) stand-in --
    same shape as test_web_admin_groups.py's own of the same name, duplicated
    here rather than imported across test modules (this file's own local
    `_req`/`_generate_jwk` already follow that "each gate-test file keeps its
    own small fakes" convention)."""

    def __init__(self, mapping: dict[str, list[str]] | None = None, *, down: bool = False):
        self._mapping = dict(mapping) if mapping else {}
        self._down = down

    def groups_for(self, email: str) -> list[str] | None:
        if self._down:
            return None
        return self._mapping.get(email.lower(), [])


def _req_with_resolver(email: str | None, config__web_destroy: Config, resolver) -> SimpleNamespace:
    """Same `_req` shape above, but with a real `clients.groups` resolver
    wired on -- needed for the group-membership cells of the matrix, where
    `_is_admin` must actually reach a resolver rather than short-circuit on
    `clients is None`."""
    state = SimpleNamespace()
    if email is not None:
        state.iap_email = email
    clients = SimpleNamespace(groups=resolver)
    return SimpleNamespace(
        state=state, app=SimpleNamespace(state=SimpleNamespace(config=config__web_destroy, clients=clients))
    )


def _admin_groups_only_config(**overrides) -> Config:
    defaults = {
        "operators": [],
        "admin_groups": [ADMIN_GROUP],
        "groups": [GroupChoice(name=ADMIN_GROUP, okta_group="okta-infra-engineering")],
        "iap_audience": AUDIENCE,
    }
    defaults.update(overrides)
    return Config(**defaults)


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
def session_factory__web_destroy(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__web_destroy(tmp_path):
    return Config(
        workdir=tmp_path / "work",
        skills_bucket="skills-bucket",
        personas_bucket="personas-bucket",
    )


def _make_client__web_destroy(
    session_factory__web_destroy,
    config__web_destroy,
    *,
    github=None,
    gojira=None,
    slack=None,
    groups=None,
    iap_jwks_fetcher=None,
):
    storage = FakeStorage()
    seed_default_personas(storage, bucket=config__web_destroy.personas_bucket)
    return TestClient(
        create_app(
            session_factory__web_destroy,
            config=config__web_destroy,
            clients=Clients(
                github=github if github is not None else FakeGitHub(existing_repos={REPO__web_destroy}),
                gojira=gojira if gojira is not None else FakeGojira(),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=FakeKubectl(),
                slack=slack if slack is not None else FakeSlack(),
                storage=storage,
                groups=groups,
            ),
            iap_jwks_fetcher=iap_jwks_fetcher,
        )
    )


def test_helper_non_listed_email_returns_403_body_verbatim():
    """Gate (authorization): Helper non listed email returns 403 body verbatim."""
    config__web_destroy = Config(operators=["ops@example.com"], iap_audience=AUDIENCE)

    result = _operator_block(_req__web_destroy(NON_OPERATOR_EMAIL, config__web_destroy), config__web_destroy)

    assert result is not None
    assert result.status_code == 403
    assert json.loads(result.body) == VERBATIM_403__web_destroy


def test_helper_iap_off_is_none_even_with_operators_set_and_no_email():
    """Gate (authorization): Helper iap off is none even with operators set and no email."""
    config__web_destroy = Config(operators=["ops@example.com"], iap_audience="")

    assert _operator_block(_req__web_destroy(None, config__web_destroy), config__web_destroy) is None


def test_helper_admin_groups_only_resolver_down_is_403_for_everyone():
    # The fail-closed cliff: a resolver that cannot answer at all grants
    # nothing to anyone, even the email that WOULD be a member had the
    # directory actually answered.
    """Gate (authorization): Helper admin groups only resolver down is 403 for everyone."""
    config__web_destroy = _admin_groups_only_config()
    resolver = FakeGroupResolver(down=True)

    result = _operator_block(_req_with_resolver(ADMIN_MEMBER_EMAIL, config__web_destroy, resolver), config__web_destroy)

    assert result is not None
    assert result.status_code == 403
    assert json.loads(result.body) == VERBATIM_403__web_destroy


def test_non_operator_gets_403_before_agent_lookup(session_factory__web_destroy, config__web_destroy):
    """Gate (authorization): Non operator gets 403 before agent lookup.

    The gate runs before the agent 404 check too — a non-operator gets
    the same 403 whether or not the named agent even exists."""
    private_key, jwk = _generate_jwk()
    op_config = config__web_destroy.model_copy(update={"operators": ["ops@example.com"], "iap_audience": AUDIENCE})
    client = _make_client__web_destroy(session_factory__web_destroy, op_config, iap_jwks_fetcher=_fetcher(jwk))
    token = _sign(private_key, email=NON_OPERATOR_EMAIL)

    response = client.delete("/api/agents/no-such-agent", headers={ASSERTION_HEADER: token})

    assert response.status_code == 403
    assert response.json() == VERBATIM_403__web_destroy


# --- from test_web_slack_gate.py ---


AGENT_NAME = "clio"


PERSONA = "helpful-generalist"


REPO = "acme/agent-clio"


APP_ID = "A0EXISTING"


BOT_TOKEN = "xoxb-super-secret-bot-token"


APP_TOKEN = "xapp-super-secret-app-token"


CHANNEL = "C1234567"


NON_OPERATOR_EMAIL = "someone-else@example.com"


# FakeGitHub.clone only writes bundle/agent.yaml when agent_yaml_transports
# is given; the one connect happy-path test in this file needs the file on
# disk for wire_slack to edit — same fixture/idiom as test_api_slack.py's
# own _CapturingGitHub (duplicated locally, not imported: this file's own
# "don't cross-import test fixtures" convention, see its docstring above).
_DEFAULT_AGENT_YAML_TRANSPORTS = [{"kind": "a2a", "host": "0.0.0.0", "port": 8080}]


AUDIENCE = "test-audience"


KID = "test-key-1"


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
def session_factory__web_slack_gate(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__web_slack_gate(tmp_path):
    return Config(
        workdir=tmp_path / "work",
        skills_bucket="skills-bucket",
        personas_bucket="personas-bucket",
    )


# Sentinel distinguishing "slack kwarg not passed" (-> default FakeSlack())
# from an explicit `slack=None` (-> Clients.slack is None, the "Slack isn't
# configured" 503 path) — same distinction as test_api_slack.py's own
# _SLACK_UNSET, since plain `None` can't serve as both.
_SLACK_UNSET = object()


def _make_client__web_slack_gate(
    session_factory__web_slack_gate,
    config__web_slack_gate,
    *,
    github=None,
    kubectl=None,
    gojira=None,
    slack=_SLACK_UNSET,
    iap_jwks_fetcher=None,
):
    storage = FakeStorage()
    seed_default_personas(storage, bucket=config__web_slack_gate.personas_bucket)
    return TestClient(
        create_app(
            session_factory__web_slack_gate,
            config=config__web_slack_gate,
            clients=Clients(
                github=github if github is not None else FakeGitHub(),
                gojira=gojira if gojira is not None else FakeGojira(activate_after=1),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=kubectl if kubectl is not None else FakeKubectl(),
                slack=FakeSlack() if slack is _SLACK_UNSET else slack,
                storage=storage,
            ),
            iap_jwks_fetcher=iap_jwks_fetcher,
        )
    )


def _onboarded_gojira(**kwargs):
    """A FakeGojira with one already-onboarded app (mirrors a READY agent),
    at the cadence connect_slack's wait_active expects. Duplicated locally
    from test_api_slack.py's own helper of the same name, same
    "don't cross-import test fixtures" convention this file follows
    throughout."""
    kwargs.setdefault("activate_after", 1)
    gojira = FakeGojira(**kwargs)
    app_id = gojira.create_app(
        name=AGENT_NAME, repo_full_name=REPO, cluster="example-cluster", namespace=AGENT_NAME
    )
    return gojira, app_id


def _seed_agent__web_slack_gate(
    session_factory__web_slack_gate,
    config__web_slack_gate,
    *,
    status=AgentStatus.READY,
    slack_app_id=None,
    gojira_app_id=None,
    slack_channels=None,
    created_by=None,
):
    session = session_factory__web_slack_gate()
    session.add(
        Agent(
            name=AGENT_NAME,
            persona=PERSONA,
            model_id=config__web_slack_gate.models[0].id,
            status=status,
            repo_full_name=REPO,
            slack_app_id=slack_app_id,
            gojira_app_id=gojira_app_id,
            slack_channels=encode_channels(slack_channels) if slack_channels is not None else None,
            created_by=created_by,
        )
    )
    session.commit()
    session.close()


def test_non_operator_gets_200_from_connect_slack(session_factory__web_slack_gate, config__web_slack_gate):
    """Gate (authorization): Non operator gets 200 from connect slack."""
    private_key, jwk = _generate_jwk()
    op_config = config__web_slack_gate.model_copy(
        update={"slack_operators": ["ops@example.com"], "iap_audience": AUDIENCE}
    )
    github = FakeGitHub(agent_yaml_transports=_DEFAULT_AGENT_YAML_TRANSPORTS)
    gojira, app_id = _onboarded_gojira()
    client = _make_client__web_slack_gate(
        session_factory__web_slack_gate, op_config, github=github, gojira=gojira,
        iap_jwks_fetcher=_fetcher(jwk),
    )
    _seed_agent__web_slack_gate(
        session_factory__web_slack_gate,
        op_config,
        gojira_app_id=app_id,
        slack_channels=[CHANNEL],
        created_by=NON_OPERATOR_EMAIL,
    )
    token = _sign(private_key, email=NON_OPERATOR_EMAIL)

    response = client.post(
        f"/api/agents/{AGENT_NAME}/slack/connect",
        json={"app_id": APP_ID, "bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
        headers={ASSERTION_HEADER: token},
    )

    assert response.status_code == 200
    assert BOT_TOKEN not in response.text
    assert APP_TOKEN not in response.text


# --- from test_api_avatars.py ---


AGENT_NAME = "clio"


PERSONA = "helpful-generalist"


REPO = "acme/agent-clio"


@pytest.fixture
def session_factory__api_avatars(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__api_avatars(tmp_path):
    # Task 5: generate_avatar's persona-description lookup now reads the
    # bucket catalog (via search.get_entry) instead of the old local-clone
    # scanner — buckets must be named for that lookup to find anything.
    return Config(
        workdir=tmp_path / "work",
        skills_bucket="skills-bucket",
        personas_bucket="personas-bucket",
    )


# Sentinel distinguishing "image kwarg not passed" (-> default FakeImage())
# from an explicit `image=None` (-> Clients.image is None, the "picture
# generation is not available" 503 path) — plain `None` can't serve as both,
# same idiom as test_api_slack.py's own _SLACK_UNSET.
_IMAGE_UNSET = object()


def _make_client__api_avatars(session_factory__api_avatars, config__api_avatars, *, github=None, image=_IMAGE_UNSET, storage=None):
    if storage is None:
        # Seeded the same way a real bootstrap seeds a fresh org's bucket —
        # gives generate_avatar's persona-description lookup something real
        # to find ("helpful-generalist") without every call site here
        # needing to set this up itself.
        storage = FakeStorage()
        seed_default_personas(storage, bucket=config__api_avatars.personas_bucket)
    return TestClient(
        create_app(
            session_factory__api_avatars,
            config=config__api_avatars,
            clients=Clients(
                github=github if github is not None else FakeGitHub(),
                gojira=FakeGojira(),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=FakeKubectl(),
                image=FakeImage() if image is _IMAGE_UNSET else image,
                storage=storage,
            ),
        )
    )


def _seed_agent__api_avatars(
    session_factory__api_avatars,
    config__api_avatars,
    *,
    status=AgentStatus.READY,
    repo_full_name=REPO,
    avatar_png=None,
    purpose_tags=None,
):
    session = session_factory__api_avatars()
    session.add(
        Agent(
            name=AGENT_NAME,
            persona=PERSONA,
            model_id=config__api_avatars.models[0].id,
            status=status,
            repo_full_name=repo_full_name,
            avatar_png=avatar_png,
            purpose_tags=purpose_tags,
        )
    )
    session.commit()
    session.close()


NON_READY_STATUSES = [AgentStatus.PENDING, AgentStatus.PROVISIONING, AgentStatus.FAILED]


@pytest.mark.parametrize("status", NON_READY_STATUSES)
def test_get_avatar_no_ready_gate_non_ready_agent_still_gets_house_404(
    session_factory__api_avatars, config__api_avatars, status
):
    """Gate (visibility): Get avatar no ready gate non ready agent still gets house 404.

    GET carries no READY gate (unlike every other agent-scoped route in
    this app) — a provisioning agent's page may render; the <img> just 404s
    the same way it would for a READY agent with no cache yet."""
    client = _make_client__api_avatars(session_factory__api_avatars, config__api_avatars)
    _seed_agent__api_avatars(session_factory__api_avatars, config__api_avatars, status=status)

    response = client.get(f"/api/agents/{AGENT_NAME}/avatar")

    assert response.status_code == 404
    assert response.json() == {"error": "no picture yet"}


# --- from test_web_visibility.py ---


PERSONA = "helpful-generalist"


REPO_TEMPLATE = "acme/agent-{name}"


OPERATOR_EMAIL__web_visibility = "ops@example.com"


OWNER_EMAIL = "owner@example.com"


STRANGER_EMAIL = "stranger@example.com"


AUDIENCE = "test-audience"


KID = "test-key-1"


GROUPS = [
    GroupChoice(name="payments-eng", okta_group="okta-payments"),
    GroupChoice(name="growth", okta_group="okta-growth"),
]


class FakeGroupResolver__web_visibility:
    """The plainest possible stand-in for `GroupResolver` (kobo_api.groups):
    a bare object exposing `groups_for(email) -> list[str] | None`, nothing
    more -- exactly the Protocol shape, so a test using this can never
    accidentally depend on anything the real `OktaGroupResolver` carries
    beyond it.

    `mapping` answers a known email with its configured groups; any email
    not in it answers `[]` (a directory that successfully reports "belongs
    to nothing"), UNLESS `down=True`, in which case every call answers
    `None` regardless of `mapping` -- the fail-closed "the directory itself
    couldn't be reached" case (kobo_api.groups' own None/`[]` distinction).
    """

    def __init__(self, mapping: dict[str, list[str]] | None = None, *, down: bool = False):
        self._mapping = dict(mapping) if mapping else {}
        self._down = down

    def groups_for(self, email: str) -> list[str] | None:
        if self._down:
            return None
        return self._mapping.get(email.lower(), [])


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


def _headers__web_visibility(private_key, *, email: str) -> dict:
    return {ASSERTION_HEADER: _sign(private_key, email=email)}


@pytest.fixture
def session_factory__web_visibility(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config__web_visibility(tmp_path):
    return Config(
        workdir=tmp_path / "work",
        operators=[OPERATOR_EMAIL__web_visibility],
        iap_audience=AUDIENCE,
        groups=GROUPS,
    )


def _make_client__web_visibility(session_factory__web_visibility, config__web_visibility, *, groups_resolver=None, iap_jwks_fetcher):
    return TestClient(
        create_app(
            session_factory__web_visibility,
            config=config__web_visibility,
            clients=Clients(
                github=FakeGitHub(),
                gojira=FakeGojira(),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=FakeKubectl(),
                storage=FakeStorage(),
                groups=groups_resolver,
            ),
            iap_jwks_fetcher=iap_jwks_fetcher,
        )
    )


def _seed_agent__web_visibility(session_factory__web_visibility, config__web_visibility, *, name, created_by=None, group_name=None):
    session = session_factory__web_visibility()
    session.add(
        Agent(
            name=name,
            persona=PERSONA,
            model_id=config__web_visibility.models[0].id,
            status=AgentStatus.READY,
            repo_full_name=REPO_TEMPLATE.format(name=name),
            created_by=created_by,
            group_name=group_name,
        )
    )
    session.commit()
    session.close()


@pytest.fixture
def visible_client(session_factory__web_visibility, config__web_visibility):
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver__web_visibility({})
    client = _make_client__web_visibility(session_factory__web_visibility, config__web_visibility, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent__web_visibility(session_factory__web_visibility, config__web_visibility, name="clio", created_by=OWNER_EMAIL, group_name="payments-eng")
    return client, private_key


# Every agent-scoped route answers a caller who cannot see the agent with the
# same 404 it gives a name that does not exist, and never a 403: the
# ownership gate must sit behind the visibility fetch, not in front of it.
# The table was folded on 2026-10-29 from one test per route in
# test_web_visibility.py, test_web_secrets.py and test_api_colleagues.py.
VISIBILITY_ROUTES = [
    ("GET", "/api/agents/clio", {}),
    ("POST", "/api/agents/clio/chat", {"json": {"messages": [{"role": "user", "text": "hi"}]}}),
    ("POST", "/api/agents/clio/retry", {}),
    ("GET", "/api/agents/clio/build-log", {}),
    ("GET", "/api/agents/clio/knowledge", {}),
    ("PUT", "/api/agents/clio/knowledge/gotchas.md", {"json": {"content": "whatever"}}),
    ("DELETE", "/api/agents/clio/knowledge/gotchas.md", {}),
    ("GET", "/api/agents/clio/skills", {}),
    ("GET", "/api/agents/clio/intake", {}),
    ("GET", "/api/agents/clio/tooling", {}),
    ("POST", "/api/agents/clio/teach", {"json": {"text": "remember this"}}),
    ("GET", "/api/agents/clio/learnings", {}),
    ("GET", "/api/agents/clio/activity", {}),
    ("GET", "/api/agents/clio/slack/manifest", {}),
    ("GET", "/api/agents/clio/secrets", {}),
    ("PUT", "/api/agents/clio/secrets", {"json": {"set": {"API_KEY": "x"}}}),
    ("GET", "/api/agents/clio/env", {}),
    ("PUT", "/api/agents/clio/env", {"json": {"set": {"MODE": "x"}}}),
    ("PUT", "/api/agents/clio/persona", {"json": {"persona": PERSONA}}),
    ("PATCH", "/api/agents/clio/model", {"json": {"model": "vertex_ai/claude-sonnet-4-5"}}),
    ("POST", "/api/agents/clio/restart", {}),
    ("GET", "/api/agents/clio/avatar", {}),
    ("GET", "/api/agents/clio/invites", {}),
    ("POST", "/api/agents/clio/invites", {"json": {"to_agent": "ward"}}),
    ("GET", "/api/agents/clio/pairs", {}),
    ("GET", "/api/agents/clio/requestable", {}),
    ("GET", "/api/agents/clio/grants", {}),
    ("GET", "/api/agents/clio/access", {}),
    ("GET", "/api/agents/clio/github/repositories", {}),
    ("PUT", "/api/agents/clio/github/repositories", {"json": {"repositories": ["acme/a"]}}),
    ("GET", "/api/agents/clio/leases", {}),
    ("GET", "/api/colleagues/feed?agent=clio", {}),
]


@pytest.mark.parametrize(
    ("method", "path", "body"),
    VISIBILITY_ROUTES,
    ids=[f"{m} {p}" for m, p, _ in VISIBILITY_ROUTES],
)
def test_every_agent_route_is_404_not_403_for_a_stranger_to_the_group(
    visible_client, method, path, body
):
    """Gate (visibility): an agent the caller cannot see is 404 on every route, never 403."""
    client, private_key = visible_client

    response = client.request(
        method, path, headers=_headers__web_visibility(private_key, email=STRANGER_EMAIL), **body
    )

    assert response.status_code == 404, f"{method} {path}: {response.status_code} {response.text}"


# --- from test_api_org_status.py ---


WHOAMI_AUDIENCE = "test-audience"


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


def _fetcher(jwk):
    def fetch():
        return {"keys": [jwk]}

    return fetch


def _iap_client(config: Config, tmp_path, *, jwk) -> TestClient:
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    return TestClient(
        create_app(
            session_factory,
            config=config,
            iap_jwks_fetcher=_fetcher(jwk),
        )
    )


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


# --- from test_api_admin_addons.py ---


def _config(tmp_path, **overrides) -> Config:
    return Config(
        workdir=tmp_path / "work",
        iap_audience=WHOAMI_AUDIENCE,
        operators=["ops@example.com"],
        **overrides,
    )


def test_panel_off_is_404_even_for_an_admin(tmp_path):
    """Gate (authorization): Panel off is 404 even for an admin."""
    private_key, jwk = _generate_jwk__api_org_status()
    client = _iap_client(_config(tmp_path), tmp_path, jwk=jwk)
    token = _sign_whoami(private_key, email="ops@example.com")

    response = client.get("/api/admin/addons", headers={ASSERTION_HEADER: token})

    assert response.status_code == 404
    assert response.json() == {"error": "the admin panel is off for this room"}


# Every route for the room's admins or operators: a signed-in caller who is
# neither answers 403, before the route looks anything up. The admin-panel
# routes share `_admin_panel_gate`; the rest carry `_operator_block` or
# `_is_admin` of their own. Folded on 2026-10-29 from one test per route
# in test_api_admin_*.py, test_api_group_requests.py, test_directory_sweep.py,
# test_web_admin_sync.py and test_api_connect.py.
ADMIN_ROUTES = [
    ("GET", "/api/admin/room", {}),
    ("GET", "/api/admin/addons", {}),
    ("GET", "/api/admin/people", {}),
    ("GET", "/api/admin/audit", {}),
    ("GET", "/api/admin/audit/export", {}),
    ("GET", "/api/admin/roles", {}),
    ("PUT", "/api/admin/roles/custom", {"json": {"name": "Custom", "groups": []}}),
    ("DELETE", "/api/admin/roles/custom", {}),
    ("GET", "/api/admin/integrations", {}),
    ("POST", "/api/admin/integrations", {"json": {"vendor": "datadog", "name": "Datadog", "base_access": "read"}}),
    ("DELETE", "/api/admin/integrations/datadog", {}),
    ("GET", "/api/admin/integrations/aws/setup", {}),
    ("GET", "/api/admin/connections", {}),
    ("PUT", "/api/admin/connections/clio/datadog", {"json": {"access": "read"}}),
    ("DELETE", "/api/admin/connections/clio/datadog", {}),
    ("GET", "/api/admin/mcp/revocations", {}),
    ("POST", "/api/admin/mcp/revocations", {"json": {"email": "someone@example.com"}}),
    ("POST", "/api/admin/org/reload", {}),
    ("GET", "/api/admin/room-services", {}),
    (
        "PUT",
        "/api/admin/room-services/slack",
        {"json": {"config_token": "xoxe.xoxp-1-room-config", "refresh_token": "xoxe-1-room-refresh"}},
    ),
    ("DELETE", "/api/admin/room-services/slack", {}),
    ("GET", "/api/admin/access/requests", {}),
    ("POST", "/api/admin/access/decide", {"json": {"ids": [1], "verdict": "approve"}}),
    ("GET", "/api/admin/access/leases", {}),
    ("POST", "/api/admin/access/sync", {}),
    ("POST", "/api/admin/sync-pairs", {}),
    ("GET", "/api/group-requests", {}),
    ("POST", "/api/group-requests/1/done", {}),
    ("POST", "/api/group-requests/1/declined", {}),
    ("POST", "/api/agents/clio/convert-to-connected", {"json": {"run_mode": "http"}}),
]


@pytest.mark.parametrize(
    ("method", "path", "body"),
    ADMIN_ROUTES,
    ids=[f"{m} {p}" for m, p, _ in ADMIN_ROUTES],
)
def test_every_admin_route_is_403_for_a_signed_in_non_admin(tmp_path, method, path, body):
    """Gate (authorization): a signed-in caller who is not an admin or operator is 403 on every admin route."""
    private_key, jwk = _generate_jwk__api_org_status()
    client = _iap_client(_config(tmp_path, admin_panel=True), tmp_path, jwk=jwk)
    token = _sign_whoami(private_key, email="someone@example.com")

    response = client.request(method, path, headers={ASSERTION_HEADER: token}, **body)

    assert response.status_code == 403, f"{method} {path}: {response.status_code} {response.text}"



# --- the pull request broker's lane for agents retired (kobo#365) -----------------

RETIRED_ROUTES = [
    ("POST", "/broker/agents/wren/github-token"),
    ("POST", "/api/agents/wren/repo-access"),
    ("GET", "/api/repo-access"),
    ("POST", "/api/repo-access/decide"),
]


@pytest.mark.parametrize(("method", "path"), RETIRED_ROUTES, ids=[f"{m} {p}" for m, p in RETIRED_ROUTES])
def test_every_retired_broker_route_is_gone(client, method, path):
    """Gate (authorization, kobo#365): the agent-facing GitHub token broker and the repo-access
    routes behind it no longer exist on any room, whatever the configuration; an agent reaches
    GitHub through the gateway with a token the permission service leases it."""
    assert client.request(method, path, json={}).status_code == 404
