"""Tests for the room's colleagues proxy surface (colleagues switchboard
plan, Task 4/B4 "the watching window"): `GET /api/colleagues/conversations`,
`GET /api/colleagues/conversations/{task_id}`, `GET /api/colleagues/stream`.

Any signed-in user (no operator gate, same reach as the directory-sync route
in test_api_directory.py), 409 plain detail when the switchboard isn't
configured, 502 plain detail on an upstream failure, the agent-only/
everything filter on the list route, full passthrough on the detail route,
and SSE content-type + byte-for-byte frame passthrough on the stream route —
against `tests/fakes.FakeCabal` for the first two, and a real `CabalClient`
wired to `httpx.MockTransport` for the operator-token-never-leaks proof
(FakeCabal never touches a real token at all, so it can't stand in for that
one).

Same harness shape as test_api_directory.py: own `session_factory`/`config`
fixtures, a `TestClient` built from `create_app`.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.clients import CabalClient, Clients
from kobo_api.config import Config
from kobo_api.main import create_app
from kobo_api.models import Base
from tests.fakes import (
    FakeAgent,
    FakeCabal,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
)

ENDPOINT_TEMPLATE = "http://agent-{name}.default.svc.cluster.local"

CONVERSATIONS_FIXTURE = [
    {
        "task_id": "t-1",
        "target_agent": "clio",
        "target_generation": 1,
        "caller_agent": "agent:iris",
        "state": "completed",
        "cabal_escalated_from": None,
        "created_at": "2026-08-19T10:00:00Z",
        "updated_at": "2026-08-19T10:00:05Z",
        "result_snippet": "Sure, here's how you configure that.",
    },
    {
        "task_id": "t-2",
        "target_agent": "clio",
        "target_generation": 1,
        "caller_agent": "operator:kgreiner@example.com",
        "state": "completed",
        "cabal_escalated_from": None,
        "created_at": "2026-08-19T11:00:00Z",
        "updated_at": "2026-08-19T11:00:02Z",
        "result_snippet": "Hello there.",
    },
    {
        "task_id": "t-3",
        "target_agent": "warden",
        "target_generation": 1,
        "caller_agent": "service:kobo",
        "state": "completed",
        "cabal_escalated_from": None,
        "created_at": "2026-08-19T12:00:00Z",
        "updated_at": "2026-08-19T12:00:01Z",
        "result_snippet": "System check ok.",
    },
]


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


def _enabled_config(config):
    return config.model_copy(
        update={
            "cabal_url": "http://cabal.internal",
            "agent_endpoint_template": ENDPOINT_TEMPLATE,
        }
    )


def _make_client(session_factory, config, *, cabal=None):
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
                cabal=cabal,
            ),
        )
    )


# --- GET /api/colleagues/conversations --------------------------------------


def test_list_disabled_switchboard_409s(session_factory, config):
    client = _make_client(session_factory, config)

    response = client.get("/api/colleagues/conversations")

    assert response.status_code == 409
    assert response.json() == {"error": "the switchboard is not configured"}


def test_list_default_filters_to_agent_callers_only(session_factory, config, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    fake_cabal = FakeCabal(conversations_result=CONVERSATIONS_FIXTURE)
    client = _make_client(session_factory, cfg, cabal=fake_cabal)

    response = client.get("/api/colleagues/conversations")

    assert response.status_code == 200
    body = response.json()
    assert [c["task_id"] for c in body["conversations"]] == ["t-1"]


def test_list_all_true_returns_every_row_unfiltered(session_factory, config, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    fake_cabal = FakeCabal(conversations_result=CONVERSATIONS_FIXTURE)
    client = _make_client(session_factory, cfg, cabal=fake_cabal)

    response = client.get("/api/colleagues/conversations", params={"all": "true"})

    assert response.status_code == 200
    body = response.json()
    assert [c["task_id"] for c in body["conversations"]] == ["t-1", "t-2", "t-3"]


def test_list_limit_passthrough_only_when_given(session_factory, config, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    fake_cabal = FakeCabal(conversations_result=[])
    client = _make_client(session_factory, cfg, cabal=fake_cabal)

    client.get("/api/colleagues/conversations")
    assert fake_cabal.list_conversations_calls[-1] == {"limit": None}

    client.get("/api/colleagues/conversations", params={"limit": "7"})
    assert fake_cabal.list_conversations_calls[-1] == {"limit": 7}


def test_list_upstream_failure_returns_502_plain_copy(session_factory, config, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    fake_cabal = FakeCabal(conversations_error="switchboard unreachable")
    client = _make_client(session_factory, cfg, cabal=fake_cabal)

    response = client.get("/api/colleagues/conversations")

    assert response.status_code == 502
    assert response.json() == {
        "error": "The switchboard did not answer. Try again shortly."
    }


# --- GET /api/colleagues/conversations/{task_id} ----------------------------


def test_detail_disabled_switchboard_409s(session_factory, config):
    client = _make_client(session_factory, config)

    response = client.get("/api/colleagues/conversations/t-1")

    assert response.status_code == 409
    assert response.json() == {"error": "the switchboard is not configured"}


def test_detail_success_passthrough(session_factory, config, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    detail = {
        "conversation": {"task_id": "t-1", "result": "The full untruncated answer."},
        "events": [
            {"id": 1, "event_type": "dispatch_start", "payload": {}, "created_at": "x"},
            {"id": 2, "event_type": "dispatch_success", "payload": {}, "created_at": "y"},
        ],
    }
    fake_cabal = FakeCabal(conversation_result=detail)
    client = _make_client(session_factory, cfg, cabal=fake_cabal)

    response = client.get("/api/colleagues/conversations/t-1")

    assert response.status_code == 200
    assert response.json() == detail
    assert fake_cabal.get_conversation_calls == ["t-1"]


def test_detail_upstream_failure_returns_502_plain_copy(session_factory, config, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    fake_cabal = FakeCabal(conversation_error="not found upstream")
    client = _make_client(session_factory, cfg, cabal=fake_cabal)

    response = client.get("/api/colleagues/conversations/t-404")

    assert response.status_code == 502
    assert response.json() == {
        "error": "The switchboard did not answer. Try again shortly."
    }


# --- GET /api/colleagues/stream ---------------------------------------------


def test_stream_disabled_switchboard_409s(session_factory, config):
    client = _make_client(session_factory, config)

    response = client.get("/api/colleagues/stream")

    assert response.status_code == 409
    assert response.json() == {"error": "the switchboard is not configured"}


def test_stream_content_type_and_frame_passthrough(session_factory, config, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    chunks = [b": hello\n\n", b"event: dispatch_start\ndata: {}\n\n"]
    fake_cabal = FakeCabal(stream_chunks=chunks)
    client = _make_client(session_factory, cfg, cabal=fake_cabal)

    with client.stream("GET", "/api/colleagues/stream") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = b"".join(response.iter_bytes())

    assert body == b"".join(chunks)
    assert fake_cabal.stream_events_calls == 1


# --- Operator-token-never-leaks proof (all three routes) -------------------
#
# FakeCabal is a pure test double that never touches a real token at all, so
# it can't stand in for this proof — a real CabalClient wired to
# httpx.MockTransport (same idiom as test_clients.py's own token-safety
# tests) is used instead, driven straight through the room's own routes.


def _real_cabal_client(handler, token: str) -> CabalClient:
    return CabalClient(
        "http://cabal.internal", token,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_operator_token_never_appears_in_list_502_response(session_factory, config, monkeypatch):
    secret = "ultra-secret-operator-token"
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", secret)
    cfg = _enabled_config(config)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = _make_client(session_factory, cfg, cabal=_real_cabal_client(handler, secret))

    response = client.get("/api/colleagues/conversations")

    assert response.status_code == 502
    assert secret not in response.text
    assert secret not in str(response.headers)


def test_operator_token_never_appears_in_detail_502_response(session_factory, config, monkeypatch):
    secret = "ultra-secret-operator-token"
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", secret)
    cfg = _enabled_config(config)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = _make_client(session_factory, cfg, cabal=_real_cabal_client(handler, secret))

    response = client.get("/api/colleagues/conversations/t-1")

    assert response.status_code == 502
    assert secret not in response.text
    assert secret not in str(response.headers)


def test_operator_token_never_appears_in_stream_response_or_headers(
    session_factory, config, monkeypatch
):
    secret = "ultra-secret-operator-token"
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", secret)
    cfg = _enabled_config(config)

    def handler(request: httpx.Request) -> httpx.Response:
        # Simulate an upstream failure (e.g. a stale/bad token) - the
        # stream route must still hand back a clean 200 with an
        # immediately-ended body, never surfacing the token anywhere.
        return httpx.Response(401, text=f"token was {secret}")

    client = _make_client(session_factory, cfg, cabal=_real_cabal_client(handler, secret))

    with client.stream("GET", "/api/colleagues/stream") as response:
        assert secret not in str(response.headers)
        body = b"".join(response.iter_bytes())

    assert secret.encode() not in body
