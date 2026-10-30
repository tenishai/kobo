"""Tests for threads (threads arc, Tasks 8-9): a shared, multi-agent
conversation where every participant sees the same messages.

Operator rulings (law) this file pins:
- The initiating human accepts an agent's suggestion or adds participants
  directly (`POST /api/threads`, `POST /api/threads/{id}/participants`).
- Same-group agents join a thread freely; a cross-group agent needs an
  EXISTING accepted `A2AInvite` with every agent already seated -- the same
  consent model `kobo_api.pairs`/`A2AInvite` already establish, no new
  concept.
- A thread is readable by its INITIATOR ONLY and appears in no agent's
  feed. The negative case -- a member of a participating agent's own group
  who is NOT the initiator gets the room's not-found answer -- is the
  ruling's whole point and is written first, per this arc's test law.
- Turns are delivered to participants in sequence (never a fan-out), each
  seeing the messages that came before, and an agent never answers twice
  in one turn.

Same real-JWT idiom test_api_invites.py/test_web_visibility.py use: a
locally generated ES256 keypair signs the assertion, verified through the
actual `IapMiddleware`/`IapVerifier`. Group-derived visibility is faked
with `FakeGroupResolver`, the same plain `groups_for` stand-in test_web_
visibility.py defines for the `GroupResolver` Protocol.
"""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.clients import Clients
from kobo_api.config import Config, GroupChoice
from kobo_api.iap import ASSERTION_HEADER, IAP_ISSUER
from kobo_api.main import _flatten_conversation_entries, create_app
from kobo_api.models import INVITE_ACCEPTED, A2AInvite, Agent, AgentStatus, Base
from tests.fakes import (
    FakeAgent,
    FakeCabal,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
)

PERSONA = "helpful-generalist"
MODEL_ID = "vertex_ai/claude-sonnet-4-5"
REPO_TEMPLATE = "acme/agent-{name}"
ENDPOINT_TEMPLATE = "http://agent-{name}.default.svc.cluster.local"

OPERATOR_EMAIL = "ops@example.com"
INITIATOR_EMAIL = "initiator@example.com"
GROUP_MEMBER_EMAIL = "member@example.com"
STRANGER_EMAIL = "stranger@example.com"

AUDIENCE = "test-audience"
KID = "test-key-1"

GROUPS = [
    GroupChoice(name="growth", okta_group="okta-growth"),
    GroupChoice(name="payments-eng", okta_group="okta-payments"),
]

CANNED_REPLY_TEXT = "Here's my take."


class FakeGroupResolver:
    """The plainest possible stand-in for `GroupResolver` (kobo_api.groups),
    lifted verbatim from test_web_visibility.py's own copy: a bare object
    exposing `groups_for(email) -> list[str] | None`, nothing more."""

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


def _headers(private_key, *, email: str) -> dict:
    return {ASSERTION_HEADER: _sign(private_key, email=email)}


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
        groups=GROUPS,
        cabal_url="http://cabal.internal",
        agent_endpoint_template=ENDPOINT_TEMPLATE,
    )


def _make_client(
    session_factory,
    config,
    *,
    groups_resolver=None,
    cabal=None,
    iap_jwks_fetcher,
) -> TestClient:
    return TestClient(
        create_app(
            session_factory,
            config=config,
            clients=Clients(
                github=FakeGitHub(),
                gojira=FakeGojira(),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=FakeKubectl(),
                groups=groups_resolver,
                cabal=cabal,
            ),
            iap_jwks_fetcher=iap_jwks_fetcher,
        )
    )


def _seed_agent(
    session_factory,
    *,
    name: str,
    group_name: str | None = None,
    status: str = AgentStatus.READY,
    created_by: str | None = None,
) -> None:
    session = session_factory()
    session.add(
        Agent(
            name=name,
            persona=PERSONA,
            model_id=MODEL_ID,
            status=status,
            repo_full_name=REPO_TEMPLATE.format(name=name),
            created_by=created_by,
            group_name=group_name,
        )
    )
    session.commit()
    session.close()


def _seed_invite(session_factory, *, from_agent: str, to_agent: str, status: str = INVITE_ACCEPTED) -> None:
    session = session_factory()
    session.add(A2AInvite(from_agent=from_agent, to_agent=to_agent, status=status))
    session.commit()
    session.close()


def _relay_result(text: str) -> dict:
    return {
        "status": {
            "state": "completed",
            "message": {"role": "agent", "parts": [{"kind": "text", "text": text}]},
        },
    }


# --- Task 8: thread creation and participation ------------------------------


def test_initiator_creates_thread_naming_one_agent_it_can_see(session_factory, config):
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({INITIATOR_EMAIL: ["growth"]})
    client = _make_client(session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, name="a", group_name="growth")

    response = client.post(
        "/api/threads", json={"agent": "a"}, headers=_headers(private_key, email=INITIATOR_EMAIL)
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["initiator_email"] == INITIATOR_EMAIL
    assert body["participants"] == ["a"]
    assert isinstance(body["id"], int)
    assert "created_at" in body


def test_create_thread_agent_not_ready_is_409(session_factory, config):
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({INITIATOR_EMAIL: ["growth"]})
    client = _make_client(session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, name="a", group_name="growth", status=AgentStatus.PROVISIONING)

    response = client.post(
        "/api/threads", json={"agent": "a"}, headers=_headers(private_key, email=INITIATOR_EMAIL)
    )

    assert response.status_code == 409


def test_create_thread_unknown_agent_is_404(session_factory, config):
    private_key, jwk = _generate_jwk()
    client = _make_client(session_factory, config, iap_jwks_fetcher=_fetcher(jwk))

    response = client.post(
        "/api/threads", json={"agent": "ghost"}, headers=_headers(private_key, email=INITIATOR_EMAIL)
    )

    assert response.status_code == 404
    assert response.json() == {"error": "no such agent: 'ghost'"}


def test_create_thread_agent_the_caller_cannot_see_is_refused_with_anti_enumeration_answer(
    session_factory, config
):
    """Adding an agent the initiator cannot see is refused with the room's
    existing anti-enumeration answer, not a distinguishable error -- the
    same body a genuinely unknown name gets above."""
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({INITIATOR_EMAIL: ["growth"]})
    client = _make_client(session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, name="hidden", group_name="payments-eng")

    response = client.post(
        "/api/threads", json={"agent": "hidden"}, headers=_headers(private_key, email=INITIATOR_EMAIL)
    )

    assert response.status_code == 404
    assert response.json() == {"error": "no such agent: 'hidden'"}


def test_adding_a_same_group_agent_succeeds(session_factory, config):
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({INITIATOR_EMAIL: ["growth"]})
    client = _make_client(session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, name="a", group_name="growth")
    _seed_agent(session_factory, name="b", group_name="growth")
    thread_id = client.post(
        "/api/threads", json={"agent": "a"}, headers=_headers(private_key, email=INITIATOR_EMAIL)
    ).json()["id"]

    response = client.post(
        f"/api/threads/{thread_id}/participants",
        json={"agent": "b"},
        headers=_headers(private_key, email=INITIATOR_EMAIL),
    )

    assert response.status_code == 201, response.text
    assert response.json()["participants"] == ["a", "b"]


def test_adding_a_cross_group_agent_without_an_accepted_invite_is_refused(session_factory, config):
    """Proven from a non-operator identity: INITIATOR_EMAIL never appears in
    config.operators. This reuses the existing consent handshake (`kobo_api.
    pairs.implicitly_paired` / `A2AInvite`) -- it introduces no new consent
    concept and must not bypass the existing one."""
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({INITIATOR_EMAIL: ["growth", "payments-eng"]})
    client = _make_client(session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, name="a", group_name="growth")
    _seed_agent(session_factory, name="c", group_name="payments-eng")
    thread_id = client.post(
        "/api/threads", json={"agent": "a"}, headers=_headers(private_key, email=INITIATOR_EMAIL)
    ).json()["id"]

    response = client.post(
        f"/api/threads/{thread_id}/participants",
        json={"agent": "c"},
        headers=_headers(private_key, email=INITIATOR_EMAIL),
    )

    assert response.status_code == 422
    assert "not connected" in response.json()["error"]


def test_adding_a_cross_group_agent_with_an_accepted_invite_is_allowed(session_factory, config):
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({INITIATOR_EMAIL: ["growth", "payments-eng"]})
    client = _make_client(session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, name="a", group_name="growth")
    _seed_agent(session_factory, name="c", group_name="payments-eng")
    _seed_invite(session_factory, from_agent="a", to_agent="c", status=INVITE_ACCEPTED)
    thread_id = client.post(
        "/api/threads", json={"agent": "a"}, headers=_headers(private_key, email=INITIATOR_EMAIL)
    ).json()["id"]

    response = client.post(
        f"/api/threads/{thread_id}/participants",
        json={"agent": "c"},
        headers=_headers(private_key, email=INITIATOR_EMAIL),
    )

    assert response.status_code == 201, response.text
    assert response.json()["participants"] == ["a", "c"]


def test_adding_an_agent_the_initiator_cannot_see_is_refused_with_anti_enumeration_answer(
    session_factory, config
):
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({INITIATOR_EMAIL: ["growth"]})
    client = _make_client(session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, name="a", group_name="growth")
    _seed_agent(session_factory, name="hidden", group_name="payments-eng")
    thread_id = client.post(
        "/api/threads", json={"agent": "a"}, headers=_headers(private_key, email=INITIATOR_EMAIL)
    ).json()["id"]

    response = client.post(
        f"/api/threads/{thread_id}/participants",
        json={"agent": "hidden"},
        headers=_headers(private_key, email=INITIATOR_EMAIL),
    )

    assert response.status_code == 404
    assert response.json() == {"error": "no such agent: 'hidden'"}


def test_adding_a_participant_by_a_non_initiator_is_not_found(session_factory, config):
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({INITIATOR_EMAIL: ["growth"], STRANGER_EMAIL: ["growth"]})
    client = _make_client(session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, name="a", group_name="growth")
    _seed_agent(session_factory, name="b", group_name="growth")
    thread_id = client.post(
        "/api/threads", json={"agent": "a"}, headers=_headers(private_key, email=INITIATOR_EMAIL)
    ).json()["id"]

    response = client.post(
        f"/api/threads/{thread_id}/participants",
        json={"agent": "b"},
        headers=_headers(private_key, email=STRANGER_EMAIL),
    )

    assert response.status_code == 404
    assert response.json() == {"error": f"no such thread: {thread_id}"}


def test_adding_an_already_seated_agent_is_409(session_factory, config):
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({INITIATOR_EMAIL: ["growth"]})
    client = _make_client(session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, name="a", group_name="growth")
    thread_id = client.post(
        "/api/threads", json={"agent": "a"}, headers=_headers(private_key, email=INITIATOR_EMAIL)
    ).json()["id"]

    response = client.post(
        f"/api/threads/{thread_id}/participants",
        json={"agent": "a"},
        headers=_headers(private_key, email=INITIATOR_EMAIL),
    )

    assert response.status_code == 409


# --- Task 9: thread visibility -----------------------------------------------
#
# The negative case first, per this arc's own test law: it is the ruling's
# whole point.


def test_thread_read_by_a_same_group_member_who_is_not_the_initiator_is_not_found(
    session_factory, config
):
    """Operator ruling: a thread is readable by its initiator and by
    nobody else. GROUP_MEMBER_EMAIL shares agent "a"'s own group (growth),
    so ordinary group-derived visibility lets them see "a" freely -- but
    they did not initiate this thread, and must get the SAME not-found
    answer a bogus thread id gets, never a 403 that would confirm the
    thread is real."""
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver(
        {INITIATOR_EMAIL: ["growth"], GROUP_MEMBER_EMAIL: ["growth"]}
    )
    client = _make_client(session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, name="a", group_name="growth")
    thread_id = client.post(
        "/api/threads", json={"agent": "a"}, headers=_headers(private_key, email=INITIATOR_EMAIL)
    ).json()["id"]

    response = client.get(
        f"/api/threads/{thread_id}", headers=_headers(private_key, email=GROUP_MEMBER_EMAIL)
    )

    assert response.status_code == 404
    assert response.json() == {"error": f"no such thread: {thread_id}"}
    # Not a visibility gap: this same caller can see agent "a" itself just fine.
    agent_response = client.get(
        "/api/agents/a", headers=_headers(private_key, email=GROUP_MEMBER_EMAIL)
    )
    assert agent_response.status_code == 200


def test_thread_read_by_an_operator_who_is_not_the_initiator_is_also_not_found(
    session_factory, config
):
    """No operator carve-out: unlike every owner-or-operator gate elsewhere
    in this room, the ruling says "by nobody else," full stop."""
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({INITIATOR_EMAIL: ["growth"]})
    client = _make_client(session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, name="a", group_name="growth")
    thread_id = client.post(
        "/api/threads", json={"agent": "a"}, headers=_headers(private_key, email=INITIATOR_EMAIL)
    ).json()["id"]

    response = client.get(
        f"/api/threads/{thread_id}", headers=_headers(private_key, email=OPERATOR_EMAIL)
    )

    assert response.status_code == 404
    assert response.json() == {"error": f"no such thread: {thread_id}"}


def test_thread_read_by_its_own_initiator_succeeds(session_factory, config):
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({INITIATOR_EMAIL: ["growth"]})
    client = _make_client(session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, name="a", group_name="growth")
    thread_id = client.post(
        "/api/threads", json={"agent": "a"}, headers=_headers(private_key, email=INITIATOR_EMAIL)
    ).json()["id"]

    response = client.get(
        f"/api/threads/{thread_id}", headers=_headers(private_key, email=INITIATOR_EMAIL)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["participants"] == ["a"]
    assert body["initiator_email"] == INITIATOR_EMAIL


def test_thread_read_for_an_unknown_id_is_404(session_factory, config):
    private_key, jwk = _generate_jwk()
    client = _make_client(session_factory, config, iap_jwks_fetcher=_fetcher(jwk))

    response = client.get("/api/threads/999999", headers=_headers(private_key, email=INITIATOR_EMAIL))

    assert response.status_code == 404
    assert response.json() == {"error": "no such thread: 999999"}


# --- Task 9: turn delivery ----------------------------------------------------


def test_broadcast_turn_relays_to_participants_in_sequence_each_seeing_prior_messages(
    session_factory, config, monkeypatch
):
    """`broadcast: true` is the old fan-out, on request: not parallel, the
    first participant sees only the human's own message; the second sees
    that message PLUS the first participant's reply from earlier in this
    same turn, so a later answer can build on an earlier one."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({INITIATOR_EMAIL: ["growth"]})
    fake_cabal = FakeCabal(relay_result=_relay_result(CANNED_REPLY_TEXT))
    client = _make_client(
        session_factory,
        config,
        groups_resolver=resolver,
        cabal=fake_cabal,
        iap_jwks_fetcher=_fetcher(jwk),
    )
    _seed_agent(session_factory, name="a", group_name="growth")
    _seed_agent(session_factory, name="b", group_name="growth")
    thread_id = client.post(
        "/api/threads",
        json={"agent": "a"},
        headers=_headers(private_key, email=INITIATOR_EMAIL),
    ).json()["id"]
    client.post(
        f"/api/threads/{thread_id}/participants",
        json={"agent": "b"},
        headers=_headers(private_key, email=INITIATOR_EMAIL),
    )

    response = client.post(
        f"/api/threads/{thread_id}/turns",
        json={
            "messages": [{"role": "user", "text": "what do you both think?"}],
            "broadcast": True,
        },
        headers=_headers(private_key, email=INITIATOR_EMAIL),
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "replies": [
            {"agent": "a", "text": CANNED_REPLY_TEXT, "error": None, "consults": []},
            {"agent": "b", "text": CANNED_REPLY_TEXT, "error": None, "consults": []},
        ],
        "to": ["a", "b"],
    }

    # Sequential, not fan-out: exactly one relay call per participant, in
    # participant (seat) order, and no agent answers twice.
    assert [call["name"] for call in fake_cabal.relay_calls] == ["a", "b"]

    first_envelope = fake_cabal.relay_calls[0]["task_envelope"]
    second_envelope = fake_cabal.relay_calls[1]["task_envelope"]

    # "a" sees only the human's own message.
    assert first_envelope["history"] == []
    assert (
        first_envelope["status"]["message"]["parts"][0]["text"]
        == "what do you both think?"
    )

    # "b" sees the human's message in history, PLUS "a"'s reply as the
    # current turn -- proof that a later participant sees what came before.
    # The reply arrives as something "b" HEARD (role "user", speaker named),
    # never as "b"'s own last turn: a trailing agent-role message becomes an
    # assistant prefill at the model, which Claude 4.6+ rejects with a 400
    # and older models misread as the agent's own prior answer.
    assert [m["parts"][0]["text"] for m in second_envelope["history"]] == [
        "what do you both think?"
    ]
    assert second_envelope["status"]["message"]["role"] == "user"
    assert (
        second_envelope["status"]["message"]["parts"][0]["text"]
        == f"a: {CANNED_REPLY_TEXT}"
    )
    roles = [m["role"] for m in second_envelope["history"]] + [
        second_envelope["status"]["message"]["role"]
    ]
    assert "agent" not in roles


def test_turn_relay_envelope_context_id_equals_the_thread_id(
    session_factory, config, monkeypatch
):
    """Every relay for a thread passes the thread's own id as `context_id`
    to `_build_task_envelope`, so the switchboard's loop guard groups a
    thread's hops under one real key."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({INITIATOR_EMAIL: ["growth"]})
    fake_cabal = FakeCabal(relay_result=_relay_result(CANNED_REPLY_TEXT))
    client = _make_client(
        session_factory,
        config,
        groups_resolver=resolver,
        cabal=fake_cabal,
        iap_jwks_fetcher=_fetcher(jwk),
    )
    _seed_agent(session_factory, name="a", group_name="growth")
    _seed_agent(session_factory, name="b", group_name="growth")
    thread_id = client.post(
        "/api/threads",
        json={"agent": "a"},
        headers=_headers(private_key, email=INITIATOR_EMAIL),
    ).json()["id"]
    client.post(
        f"/api/threads/{thread_id}/participants",
        json={"agent": "b"},
        headers=_headers(private_key, email=INITIATOR_EMAIL),
    )

    client.post(
        f"/api/threads/{thread_id}/turns",
        json={"messages": [{"role": "user", "text": "hello both"}], "broadcast": True},
        headers=_headers(private_key, email=INITIATOR_EMAIL),
    )

    assert len(fake_cabal.relay_calls) == 2
    for call in fake_cabal.relay_calls:
        assert call["task_envelope"]["contextId"] == str(thread_id)


def test_turn_by_a_non_initiator_is_not_found_and_never_relayed(session_factory, config, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({INITIATOR_EMAIL: ["growth"], STRANGER_EMAIL: []})
    fake_cabal = FakeCabal(relay_result=_relay_result(CANNED_REPLY_TEXT))
    client = _make_client(
        session_factory, config, groups_resolver=resolver, cabal=fake_cabal, iap_jwks_fetcher=_fetcher(jwk)
    )
    _seed_agent(session_factory, name="a", group_name="growth")
    thread_id = client.post(
        "/api/threads", json={"agent": "a"}, headers=_headers(private_key, email=INITIATOR_EMAIL)
    ).json()["id"]

    response = client.post(
        f"/api/threads/{thread_id}/turns",
        json={"messages": [{"role": "user", "text": "hi"}]},
        headers=_headers(private_key, email=STRANGER_EMAIL),
    )

    assert response.status_code == 404
    assert response.json() == {"error": f"no such thread: {thread_id}"}
    assert fake_cabal.relay_calls == []


def _two_agent_thread(session_factory, config, fake_cabal, private_key, jwk):
    resolver = FakeGroupResolver({INITIATOR_EMAIL: ["growth"]})
    client = _make_client(
        session_factory,
        config,
        groups_resolver=resolver,
        cabal=fake_cabal,
        iap_jwks_fetcher=_fetcher(jwk),
    )
    _seed_agent(session_factory, name="a", group_name="growth")
    _seed_agent(session_factory, name="b", group_name="growth")
    thread_id = client.post(
        "/api/threads",
        json={"agent": "a"},
        headers=_headers(private_key, email=INITIATOR_EMAIL),
    ).json()["id"]
    client.post(
        f"/api/threads/{thread_id}/participants",
        json={"agent": "b"},
        headers=_headers(private_key, email=INITIATOR_EMAIL),
    )
    return client, thread_id


def test_turn_goes_to_the_primary_only_by_default(session_factory, config, monkeypatch):
    """A thread is one agent you talk to (kobo #240): with no `to` and no
    `broadcast`, only the agent the thread was started with answers."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    private_key, jwk = _generate_jwk()
    fake_cabal = FakeCabal(relay_result=_relay_result(CANNED_REPLY_TEXT))
    client, thread_id = _two_agent_thread(
        session_factory, config, fake_cabal, private_key, jwk
    )
    response = client.post(
        f"/api/threads/{thread_id}/turns",
        json={"messages": [{"role": "user", "text": "hi"}]},
        headers=_headers(private_key, email=INITIATOR_EMAIL),
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "replies": [
            {"agent": "a", "text": CANNED_REPLY_TEXT, "error": None, "consults": []}
        ],
        "to": ["a"],
    }
    assert [call["name"] for call in fake_cabal.relay_calls] == ["a"]


def test_turn_addressed_to_another_seated_agent_goes_only_there(
    session_factory, config, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    private_key, jwk = _generate_jwk()
    fake_cabal = FakeCabal(relay_result=_relay_result(CANNED_REPLY_TEXT))
    client, thread_id = _two_agent_thread(
        session_factory, config, fake_cabal, private_key, jwk
    )
    response = client.post(
        f"/api/threads/{thread_id}/turns",
        json={
            "messages": [{"role": "user", "text": "b, what do you think?"}],
            "to": "b",
        },
        headers=_headers(private_key, email=INITIATOR_EMAIL),
    )
    assert response.status_code == 200, response.text
    assert response.json()["to"] == ["b"]
    assert [call["name"] for call in fake_cabal.relay_calls] == ["b"]
    refused = client.post(
        f"/api/threads/{thread_id}/turns",
        json={"messages": [{"role": "user", "text": "hi"}], "to": "nobody"},
        headers=_headers(private_key, email=INITIATOR_EMAIL),
    )
    assert refused.status_code == 422
    assert refused.json() == {"error": "nobody is not in this thread"}
    assert len(fake_cabal.relay_calls) == 1


def test_a_participant_that_does_not_answer_is_an_entry_not_a_failed_turn(
    session_factory, config, monkeypatch
):
    """One down agent used to fail the whole turn with a bare 502 and throw
    away the replies already gathered. Now it is that agent's own entry."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    private_key, jwk = _generate_jwk()
    fake_cabal = FakeCabal(
        relay_result=_relay_result(CANNED_REPLY_TEXT),
        relay_errors={"b": "connection refused"},
    )
    client, thread_id = _two_agent_thread(
        session_factory, config, fake_cabal, private_key, jwk
    )
    response = client.post(
        f"/api/threads/{thread_id}/turns",
        json={"messages": [{"role": "user", "text": "hi"}], "broadcast": True},
        headers=_headers(private_key, email=INITIATOR_EMAIL),
    )
    assert response.status_code == 200, response.text
    assert response.json()["replies"] == [
        {"agent": "a", "text": CANNED_REPLY_TEXT, "error": None, "consults": []},
        {"agent": "b", "text": None, "error": "didn't answer", "consults": []},
    ]


def test_the_addressees_consults_during_the_turn_ride_along_with_its_reply(
    session_factory, config, monkeypatch
):
    """What the addressee asked its colleagues while answering appears under
    its reply: cabal's conversations with the addressee as caller that began
    after the turn started, flattened like the colleague feed. Older
    conversations and thread relays themselves do not."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    private_key, jwk = _generate_jwk()
    fake_cabal = FakeCabal(relay_result=_relay_result(CANNED_REPLY_TEXT))
    fresh = "2999-01-01T00:00:05+00:00"
    stale = "2000-01-01T00:00:00+00:00"
    fake_cabal._conversations_result = [
        {
            "task_id": "ask-new",
            "target_agent": "b",
            "caller_agent": "agent:a",
            "created_at": fresh,
            "updated_at": fresh,
        },
        {
            "task_id": "ask-old",
            "target_agent": "b",
            "caller_agent": "agent:a",
            "created_at": stale,
            "updated_at": stale,
        },
    ]
    fake_cabal._conversation_results = {
        "ask-new": {
            "conversation": {
                "task_id": "ask-new",
                "target_agent": "b",
                "caller_agent": "agent:a",
                "created_at": fresh,
                "updated_at": fresh,
                "result": {
                    "history": [
                        {
                            "role": "user",
                            "parts": [
                                {"kind": "text", "text": "b, is the cluster healthy?"}
                            ],
                        }
                    ],
                    "status": {
                        "message": {
                            "role": "agent",
                            "parts": [{"kind": "text", "text": "yes, all green"}],
                        }
                    },
                },
            }
        },
    }
    client, thread_id = _two_agent_thread(
        session_factory, config, fake_cabal, private_key, jwk
    )
    response = client.post(
        f"/api/threads/{thread_id}/turns",
        json={"messages": [{"role": "user", "text": "how is the cluster?"}]},
        headers=_headers(private_key, email=INITIATOR_EMAIL),
    )
    assert response.status_code == 200, response.text
    (reply,) = response.json()["replies"]
    assert reply["agent"] == "a" and reply["error"] is None
    assert [(c["from"], c["to"], c["role"], c["text"]) for c in reply["consults"]] == [
        ("a", "b", "ask", "b, is the cluster healthy?"),
        ("b", "a", "reply", "yes, all green"),
    ]
    assert [c["caller"] for c in fake_cabal.list_conversations_calls] == ["agent:a"]
    assert [c for c in fake_cabal.get_conversation_calls] == ["ask-new"]


def test_flatten_conversation_entries_excludes_a_thread_tagged_conversation():
    """Direct, discriminating unit test of the filter itself: a completed
    conversation whose result carries `metadata.thread` contributes zero
    feed lines, mirroring the existing "incomplete conversation" early
    return this function already makes."""
    detail = {
        "conversation": {
            "task_id": "t-1",
            "target_agent": "a",
            "caller_agent": "operator:initiator@example.com",
            "created_at": "2026-08-27T00:00:00Z",
            "updated_at": "2026-08-27T00:00:01Z",
            "result": {
                "status": {
                    "state": "completed",
                    "message": {"role": "agent", "parts": [{"kind": "text", "text": "reply"}]},
                },
                "history": [{"role": "user", "parts": [{"kind": "text", "text": "ask"}]}],
                "metadata": {"thread": 7},
            },
        },
        "events": [],
    }

    assert _flatten_conversation_entries(detail) == []


def test_flatten_conversation_entries_unrelated_metadata_is_not_mistaken_for_a_thread():
    """Regression guard: a completed conversation's result may legitimately
    carry other metadata one day; only the `thread` key trips exclusion."""
    detail = {
        "conversation": {
            "task_id": "t-1",
            "target_agent": "a",
            "caller_agent": "agent:b",
            "created_at": "2026-08-27T00:00:00Z",
            "updated_at": "2026-08-27T00:00:01Z",
            "result": {
                "status": {
                    "state": "completed",
                    "message": {"role": "agent", "parts": [{"kind": "text", "text": "reply"}]},
                },
                "history": [{"role": "user", "parts": [{"kind": "text", "text": "ask"}]}],
                "metadata": {"something_else": True},
            },
        },
        "events": [],
    }

    assert len(_flatten_conversation_entries(detail)) == 2


def test_thread_turn_does_not_appear_in_a_participants_colleague_feed(session_factory, config, monkeypatch):
    """The thread does not appear in any participant's conversation feed
    (operator ruling): a thread turn's relay stamps `metadata.thread`
    (`post_thread_turn`), and once the switchboard has recorded that
    conversation, `GET /api/colleagues/feed` for the participant must show
    nothing from it."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({INITIATOR_EMAIL: ["growth"]})
    fake_cabal = FakeCabal(relay_result=_relay_result(CANNED_REPLY_TEXT))
    client = _make_client(
        session_factory, config, groups_resolver=resolver, cabal=fake_cabal, iap_jwks_fetcher=_fetcher(jwk)
    )
    _seed_agent(session_factory, name="a", group_name="growth")
    thread_id = client.post(
        "/api/threads", json={"agent": "a"}, headers=_headers(private_key, email=INITIATOR_EMAIL)
    ).json()["id"]

    turn_response = client.post(
        f"/api/threads/{thread_id}/turns",
        json={"messages": [{"role": "user", "text": "what's up"}]},
        headers=_headers(private_key, email=INITIATOR_EMAIL),
    )
    assert turn_response.status_code == 200, turn_response.text
    # The relay really did carry the thread marker -- the mechanism the
    # switchboard's own persisted record depends on to be filterable.
    assert fake_cabal.relay_calls[0]["task_envelope"]["metadata"] == {"thread": thread_id}

    # Simulate what the switchboard now has recorded for that relay: a
    # completed conversation whose result echoes the same metadata this
    # room's own relay stamped on the way in.
    fake_cabal._conversations_result = [
        {
            "task_id": "t-thread-1",
            "target_agent": "a",
            "target_generation": 1,
            "caller_agent": f"operator:{INITIATOR_EMAIL}",
            "state": "completed",
            "cabal_escalated_from": None,
            "created_at": "2026-08-27T00:00:00Z",
            "updated_at": "2026-08-27T00:00:01Z",
            "result_snippet": CANNED_REPLY_TEXT,
        }
    ]
    fake_cabal._conversation_results = {
        "t-thread-1": {
            "conversation": {
                "task_id": "t-thread-1",
                "target_agent": "a",
                "caller_agent": f"operator:{INITIATOR_EMAIL}",
                "created_at": "2026-08-27T00:00:00Z",
                "updated_at": "2026-08-27T00:00:01Z",
                "result": {
                    "status": {
                        "state": "completed",
                        "message": {
                            "role": "agent",
                            "parts": [{"kind": "text", "text": CANNED_REPLY_TEXT}],
                        },
                    },
                    "history": [
                        {"role": "user", "parts": [{"kind": "text", "text": "what's up"}]},
                    ],
                    "metadata": {"thread": thread_id},
                },
            },
            "events": [],
        }
    }

    feed_response = client.get(
        "/api/colleagues/feed",
        params={"agent": "a"},
        headers=_headers(private_key, email=INITIATOR_EMAIL),
    )

    assert feed_response.status_code == 200
    assert feed_response.json() == {"entries": [], "truncated": False}


# --- GET /api/agents/{name}/pairs: who may join a thread (issue #289) --------


def test_pairs_lists_the_agents_the_viewed_agent_may_share_a_thread_with(session_factory, config):
    """Visibility gate: a same-group colleague and an accepted request in
    either direction are paired; an agent with neither is not."""
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({INITIATOR_EMAIL: ["growth", "payments-eng"]})
    client = _make_client(session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, name="a", group_name="growth")
    _seed_agent(session_factory, name="b", group_name="growth")
    _seed_agent(session_factory, name="c", group_name="payments-eng")
    _seed_agent(session_factory, name="d", group_name="payments-eng")
    _seed_invite(session_factory, from_agent="c", to_agent="a", status=INVITE_ACCEPTED)

    response = client.get("/api/agents/a/pairs", headers=_headers(private_key, email=INITIATOR_EMAIL))

    assert response.status_code == 200, response.text
    assert response.json()["pairs"] == [
        {"name": "b", "group": "growth", "via": "group"},
        {"name": "c", "group": "payments-eng", "via": "invite"},
    ]


def test_pairs_never_names_an_agent_the_caller_cannot_see(session_factory, config):
    private_key, jwk = _generate_jwk()
    resolver = FakeGroupResolver({INITIATOR_EMAIL: ["growth"]})
    client = _make_client(session_factory, config, groups_resolver=resolver, iap_jwks_fetcher=_fetcher(jwk))
    _seed_agent(session_factory, name="a", group_name="growth")
    _seed_agent(session_factory, name="c", group_name="payments-eng")
    _seed_invite(session_factory, from_agent="a", to_agent="c", status=INVITE_ACCEPTED)

    response = client.get("/api/agents/a/pairs", headers=_headers(private_key, email=INITIATOR_EMAIL))

    assert response.status_code == 200, response.text
    assert response.json()["pairs"] == []
