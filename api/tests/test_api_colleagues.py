"""Tests for the room's colleagues proxy surface (colleagues switchboard
plan, Task 4/B4 "the watching window"): `GET /api/colleagues/conversations`,
`GET /api/colleagues/conversations/{task_id}`, `GET /api/colleagues/stream`,
and `GET /api/colleagues/feed` (terminal redesign, the flattened chronological
feed that replaced the card grid's per-conversation queries).

Any signed-in user (no operator gate, same reach as the directory-sync route
in test_api_directory.py), 409 plain detail when the switchboard isn't
configured, 502 plain detail on an upstream failure, the agent-only/
everything filter on the list route, full passthrough on the detail route,
and SSE content-type + byte-for-byte frame passthrough on the stream route —
against `tests/fakes.FakeCabal` for the first two, and a real `CabalClient`
wired to `httpx.MockTransport` for the operator-token-never-leaks proof
(FakeCabal never touches a real token at all, so it can't stand in for that
one). `/api/colleagues/feed` gets its own section further down: same 409/502
conventions, plus the one thing that sets it apart from the other three --
`_visible_agent_or_404` gating, since it hands back full conversation text.

Same harness shape as test_api_directory.py: own `session_factory`/`config`
fixtures, a `TestClient` built from `create_app`.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import time

import jwt
from cryptography.hazmat.primitives.asymmetric import ec

from kobo_api.clients import CabalClient, Clients
from kobo_api.config import Config, GroupChoice
from kobo_api.iap import ASSERTION_HEADER, IAP_ISSUER
from kobo_api.main import create_app
from kobo_api.models import Agent, AgentStatus, Base
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


# --- GET /api/colleagues/conversations: ?agent= scoping (access-tab refinement
# wave, conversation scoping) -------------------------------------------------
#
# CONVERSATIONS_FIXTURE: t-1 target=clio caller=agent:iris; t-2 target=clio
# caller=operator:kgreiner@example.com; t-3 target=warden caller=service:kobo.
# The `agent` filter is layered ON TOP of the existing all/agent-only
# filtering (AND semantics), never a replacement for it.


def test_list_agent_filter_matches_target_side_with_all_true(session_factory, config, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    fake_cabal = FakeCabal(conversations_result=CONVERSATIONS_FIXTURE)
    client = _make_client(session_factory, cfg, cabal=fake_cabal)

    response = client.get(
        "/api/colleagues/conversations", params={"all": "true", "agent": "clio"}
    )

    assert response.status_code == 200
    assert [c["task_id"] for c in response.json()["conversations"]] == ["t-1", "t-2"]


def test_list_agent_filter_matches_caller_side(session_factory, config, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    fake_cabal = FakeCabal(conversations_result=CONVERSATIONS_FIXTURE)
    client = _make_client(session_factory, cfg, cabal=fake_cabal)

    response = client.get(
        "/api/colleagues/conversations", params={"all": "true", "agent": "iris"}
    )

    assert response.status_code == 200
    assert [c["task_id"] for c in response.json()["conversations"]] == ["t-1"]


def test_list_agent_filter_composes_with_default_agent_only_filter(
    session_factory, config, monkeypatch
):
    # Default (all=false) already drops t-2 (a human caller) -- scoping to
    # clio (the target of both t-1 and t-2) must still only surface t-1,
    # the one row that survives BOTH filters.
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    fake_cabal = FakeCabal(conversations_result=CONVERSATIONS_FIXTURE)
    client = _make_client(session_factory, cfg, cabal=fake_cabal)

    response = client.get("/api/colleagues/conversations", params={"agent": "clio"})

    assert response.status_code == 200
    assert [c["task_id"] for c in response.json()["conversations"]] == ["t-1"]


def test_list_agent_filter_no_participant_match_is_empty(session_factory, config, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    fake_cabal = FakeCabal(conversations_result=CONVERSATIONS_FIXTURE)
    client = _make_client(session_factory, cfg, cabal=fake_cabal)

    response = client.get(
        "/api/colleagues/conversations", params={"all": "true", "agent": "nobody"}
    )

    assert response.status_code == 200
    assert response.json()["conversations"] == []


def test_list_no_agent_param_is_unaffected(session_factory, config, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    fake_cabal = FakeCabal(conversations_result=CONVERSATIONS_FIXTURE)
    client = _make_client(session_factory, cfg, cabal=fake_cabal)

    response = client.get("/api/colleagues/conversations", params={"all": "true"})

    assert response.status_code == 200
    assert [c["task_id"] for c in response.json()["conversations"]] == ["t-1", "t-2", "t-3"]


# --- GET /api/colleagues/conversations: thread exclusion (Critical 3 fix) --
#
# A thread is a private, initiator-only conversation (operator ruling) that
# must never surface through this room-wide proxy route -- mirroring
# `_flatten_conversation_entries`' own exclusion for the colleague feed. The
# list row shape here (`CabalClient.list_conversations`) carries no
# `metadata` field at all (only `task_id`/`target_agent`/`caller_agent`/.../
# `result_snippet`), so each surviving row's full detail is fetched to check
# for the `metadata.thread` marker -- the same per-row `get_conversation`
# cost `colleague_feed` already pays for every row it renders.


def _thread_tagged_detail(
    task_id: str, *, target_agent: str, caller_agent: str, thread_id: int = 1
) -> dict:
    return {
        "conversation": {
            "task_id": task_id,
            "target_agent": target_agent,
            "caller_agent": caller_agent,
            "result": {
                "status": {
                    "state": "completed",
                    "message": {"role": "agent", "parts": [{"kind": "text", "text": "reply"}]},
                },
                "history": [{"role": "user", "parts": [{"kind": "text", "text": "ask"}]}],
                "metadata": {"thread": thread_id},
            },
        },
        "events": [],
    }


_THREAD_ROW = {
    "task_id": "t-thread",
    "target_agent": "clio",
    "target_generation": 1,
    "caller_agent": "operator:kgreiner@example.com",
    "state": "completed",
    "cabal_escalated_from": None,
    "created_at": "2026-08-19T13:00:00Z",
    "updated_at": "2026-08-19T13:00:01Z",
    "result_snippet": "the whole private thread transcript",
}


def test_list_all_true_excludes_a_thread_tagged_conversation(session_factory, config, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    fake_cabal = FakeCabal(
        conversations_result=CONVERSATIONS_FIXTURE + [_THREAD_ROW],
        conversation_results={
            "t-thread": _thread_tagged_detail(
                "t-thread", target_agent="clio", caller_agent="operator:kgreiner@example.com"
            )
        },
    )
    client = _make_client(session_factory, cfg, cabal=fake_cabal)

    response = client.get("/api/colleagues/conversations", params={"all": "true"})

    assert response.status_code == 200
    assert [c["task_id"] for c in response.json()["conversations"]] == ["t-1", "t-2", "t-3"]


def test_list_thread_exclusion_survives_agent_scoping(session_factory, config, monkeypatch):
    # The thread row's target IS "clio" -- exclusion must win over a
    # matching ?agent= scope, not just the default/all split.
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    fake_cabal = FakeCabal(
        conversations_result=[_THREAD_ROW],
        conversation_results={
            "t-thread": _thread_tagged_detail(
                "t-thread", target_agent="clio", caller_agent="operator:kgreiner@example.com"
            )
        },
    )
    client = _make_client(session_factory, cfg, cabal=fake_cabal)

    response = client.get(
        "/api/colleagues/conversations", params={"all": "true", "agent": "clio"}
    )

    assert response.status_code == 200
    assert response.json()["conversations"] == []


def test_list_thread_check_upstream_failure_is_a_plain_502(session_factory, config, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    fake_cabal = FakeCabal(
        conversations_result=CONVERSATIONS_FIXTURE, conversation_error="boom upstream"
    )
    client = _make_client(session_factory, cfg, cabal=fake_cabal)

    response = client.get("/api/colleagues/conversations", params={"all": "true"})

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


# --- GET /api/colleagues/conversations/{task_id}: ?agent= scoping ----------


def _detail_fixture(*, target_agent: str, caller_agent: str) -> dict:
    return {
        "conversation": {
            "task_id": "t-1",
            "target_agent": target_agent,
            "caller_agent": caller_agent,
            "result": "The full untruncated answer.",
        },
        "events": [],
    }


def test_detail_agent_filter_matches_target_side(session_factory, config, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    detail = _detail_fixture(target_agent="clio", caller_agent="agent:iris")
    fake_cabal = FakeCabal(conversation_result=detail)
    client = _make_client(session_factory, cfg, cabal=fake_cabal)

    response = client.get("/api/colleagues/conversations/t-1", params={"agent": "clio"})

    assert response.status_code == 200
    assert response.json() == detail


def test_detail_agent_filter_matches_caller_side(session_factory, config, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    detail = _detail_fixture(target_agent="clio", caller_agent="agent:iris")
    fake_cabal = FakeCabal(conversation_result=detail)
    client = _make_client(session_factory, cfg, cabal=fake_cabal)

    response = client.get("/api/colleagues/conversations/t-1", params={"agent": "iris"})

    assert response.status_code == 200
    assert response.json() == detail


def test_detail_agent_filter_no_match_is_404(session_factory, config, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    detail = _detail_fixture(target_agent="clio", caller_agent="agent:iris")
    fake_cabal = FakeCabal(conversation_result=detail)
    client = _make_client(session_factory, cfg, cabal=fake_cabal)

    response = client.get("/api/colleagues/conversations/t-1", params={"agent": "warden"})

    assert response.status_code == 404
    assert response.json() == {"error": "no such conversation: 't-1'"}


def test_detail_no_agent_param_is_unaffected(session_factory, config, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    detail = _detail_fixture(target_agent="clio", caller_agent="agent:iris")
    fake_cabal = FakeCabal(conversation_result=detail)
    client = _make_client(session_factory, cfg, cabal=fake_cabal)

    response = client.get("/api/colleagues/conversations/t-1")

    assert response.status_code == 200
    assert response.json() == detail


# --- GET /api/colleagues/conversations/{task_id}: thread exclusion
# (Critical 3 fix) -----------------------------------------------------------
#
# The same not-found answer a bogus task_id gets, never a distinguishable
# 403/200-with-a-warning -- consistent with the ?agent= scoping's own
# existence-hiding discipline just above.


def test_detail_thread_tagged_conversation_is_the_same_not_found_answer(
    session_factory, config, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    detail = _thread_tagged_detail(
        "t-1", target_agent="clio", caller_agent="operator:kgreiner@example.com"
    )
    fake_cabal = FakeCabal(conversation_result=detail)
    client = _make_client(session_factory, cfg, cabal=fake_cabal)

    response = client.get("/api/colleagues/conversations/t-1")

    assert response.status_code == 404
    assert response.json() == {"error": "no such conversation: 't-1'"}


def test_detail_thread_tagged_conversation_is_404_even_when_agent_matches(
    session_factory, config, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    detail = _thread_tagged_detail(
        "t-1", target_agent="clio", caller_agent="operator:kgreiner@example.com"
    )
    fake_cabal = FakeCabal(conversation_result=detail)
    client = _make_client(session_factory, cfg, cabal=fake_cabal)

    response = client.get("/api/colleagues/conversations/t-1", params={"agent": "clio"})

    assert response.status_code == 404
    assert response.json() == {"error": "no such conversation: 't-1'"}


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


# --- GET /api/colleagues/stream: ?agent= scoping ----------------------------


def _dispatch_start_chunk(task_id: str, *, target: str, caller: str) -> bytes:
    payload = {
        "event_type": "dispatch_start",
        "task_id": task_id,
        "payload": {"target": target, "caller": caller},
    }
    return f"event: dispatch_start\ndata: {json.dumps(payload)}\n\n".encode()


def _dispatch_success_chunk(task_id: str) -> bytes:
    payload = {"event_type": "dispatch_success", "task_id": task_id, "payload": {"state": "completed"}}
    return f"event: dispatch_success\ndata: {json.dumps(payload)}\n\n".encode()


def test_stream_agent_filter_forwards_only_matching_conversation_frames(
    session_factory, config, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    clio_start = _dispatch_start_chunk("t-1", target="clio", caller="agent:iris")
    clio_success = _dispatch_success_chunk("t-1")
    warden_start = _dispatch_start_chunk("t-2", target="warden", caller="service:kobo")
    chunks = [clio_start, warden_start, clio_success]
    fake_cabal = FakeCabal(stream_chunks=chunks)
    client = _make_client(session_factory, cfg, cabal=fake_cabal)

    with client.stream("GET", "/api/colleagues/stream", params={"agent": "clio"}) as response:
        assert response.status_code == 200
        body = b"".join(response.iter_bytes())

    assert body == clio_start + clio_success


def test_stream_no_agent_param_is_unaffected(session_factory, config, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    chunks = [b": hello\n\n", b"event: dispatch_start\ndata: {}\n\n"]
    fake_cabal = FakeCabal(stream_chunks=chunks)
    client = _make_client(session_factory, cfg, cabal=fake_cabal)

    with client.stream("GET", "/api/colleagues/stream") as response:
        body = b"".join(response.iter_bytes())

    assert body == b"".join(chunks)


# --- GET /api/colleagues/stream: thread exclusion (Critical 3 fix) ---------
#
# Before this fix, omitting ?agent= skipped filter_cabal_event_stream
# entirely -- the raw upstream bytes went straight to the browser, thread
# turns included (full transcript, since a thread turn resends the whole
# conversation on every relay). The filter now always runs.


def _thread_dispatch_start_chunk(
    task_id: str, *, target: str, caller: str, thread_id: int
) -> bytes:
    payload = {
        "event_type": "dispatch_start",
        "task_id": task_id,
        "payload": {"target": target, "caller": caller, "metadata": {"thread": thread_id}},
    }
    return f"event: dispatch_start\ndata: {json.dumps(payload)}\n\n".encode()


def test_stream_drops_a_thread_tagged_frame_with_no_agent_param(session_factory, config, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    thread_frame = _thread_dispatch_start_chunk(
        "t-thread", target="clio", caller="operator:kgreiner@example.com", thread_id=7
    )
    ordinary_frame = _dispatch_start_chunk("t-1", target="clio", caller="agent:iris")
    fake_cabal = FakeCabal(stream_chunks=[thread_frame, ordinary_frame])
    client = _make_client(session_factory, cfg, cabal=fake_cabal)

    with client.stream("GET", "/api/colleagues/stream") as response:
        body = b"".join(response.iter_bytes())

    assert body == ordinary_frame


def test_stream_drops_a_thread_tagged_frame_even_when_agent_matches(session_factory, config, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    thread_frame = _thread_dispatch_start_chunk(
        "t-thread", target="clio", caller="operator:kgreiner@example.com", thread_id=7
    )
    fake_cabal = FakeCabal(stream_chunks=[thread_frame])
    client = _make_client(session_factory, cfg, cabal=fake_cabal)

    with client.stream("GET", "/api/colleagues/stream", params={"agent": "clio"}) as response:
        body = b"".join(response.iter_bytes())

    assert body == b""


# --- Operator-token-never-leaks proof (all three routes) -------------------
#
# FakeCabal is a pure test double that never touches a real token at all, so
# it can't stand in for this proof — a real CabalClient wired to
# httpx.MockTransport (same idiom as test_clients.py's own token-safety
# tests) is used instead, driven straight through the room's own routes.


def _real_cabal_client(handler, token: str) -> CabalClient:
    # Same handler wired to both the sync and async transports - `stream_events`
    # (fix round 1) now goes through `async_client`, while every other method
    # here still goes through the plain sync `client`. `MockTransport` accepts
    # either a sync or async handler function and answers both `handle_request`
    # and `handle_async_request` from it, so one plain handler covers both.
    return CabalClient(
        "http://cabal.internal", token,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
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


# --- GET /api/colleagues/feed (terminal redesign) ---------------------------
#
# The terminal's one flattened, chronological feed: `_flatten_conversation_
# entries` (kobo_api.main) turns each conversation's `get_conversation`
# detail into 0-2 lines (ask, then reply), sorted ascending by `ts` across
# every conversation, capped to the most recent 200. UNLIKE the three
# room-wide proxy routes above, this one is gated by `_visible_agent_or_404`
# -- the same room-wide 409/502 conventions still apply on top of that.


def _seed_agent(session_factory, config, name: str = "ward", **kwargs) -> None:
    session = session_factory()
    session.add(
        Agent(
            name=name,
            persona="helpful-generalist",
            model_id=config.models[0].id,
            status=AgentStatus.READY,
            repo_full_name=f"acme/agent-{name}",
            **kwargs,
        )
    )
    session.commit()
    session.close()


def _task_result(*, ask_text: str, reply_text: str) -> dict:
    """A completed a2a Task body shaped like a REAL daedalus agent's persisted
    `result_json` (the shape `daedalus.transports.a2a.task_to_a2a` emits and
    the switchboard stores): `status.message` is the reply, and `history`
    holds the FULL transcript -- the user's question AND the agent's reply,
    in order. NOTE the earlier fixture modeled only cabal's builtin EchoAgent
    (history = [user] alone), where `history[-1]` was the ask; every real
    colleague (a daedalus agent) persists [user, agent], so `history[-1]` is
    the ANSWER, not the ask. The feed must take the ask from the user-role
    message, not blindly from `history[-1]`."""
    return {
        "id": "irrelevant-to-the-feed",
        "status": {
            "state": "completed",
            "message": {"role": "agent", "parts": [{"kind": "text", "text": reply_text}]},
        },
        "history": [
            {"role": "user", "parts": [{"kind": "text", "text": ask_text}]},
            {"role": "agent", "parts": [{"kind": "text", "text": reply_text}]},
        ],
    }


def _feed_summary(task_id: str, *, target: str, caller: str) -> dict:
    return {
        "task_id": task_id,
        "target_agent": target,
        "target_generation": 1,
        "caller_agent": caller,
        "state": "completed",
        "cabal_escalated_from": None,
        "created_at": "unused-by-the-feed-route",
        "updated_at": "unused-by-the-feed-route",
        "result_snippet": "unused-by-the-feed-route",
    }


def _feed_detail(
    task_id: str,
    *,
    target: str,
    caller: str,
    created_at: str,
    updated_at: str,
    ask_text: str,
    reply_text: str,
) -> dict:
    return {
        "conversation": {
            "task_id": task_id,
            "target_agent": target,
            "caller_agent": caller,
            "created_at": created_at,
            "updated_at": updated_at,
            "result": _task_result(ask_text=ask_text, reply_text=reply_text),
        },
        "events": [],
    }


def test_feed_requires_the_agent_query_param(session_factory, config):
    client = _make_client(session_factory, config)

    response = client.get("/api/colleagues/feed")

    assert response.status_code == 422


def test_feed_404s_for_an_unknown_agent(session_factory, config, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    client = _make_client(session_factory, cfg, cabal=FakeCabal())

    response = client.get("/api/colleagues/feed", params={"agent": "ghost"})

    assert response.status_code == 404
    assert response.json() == {"error": "no such agent: 'ghost'"}


def test_feed_409s_when_switchboard_not_configured(session_factory, config):
    _seed_agent(session_factory, config)
    client = _make_client(session_factory, config)

    response = client.get("/api/colleagues/feed", params={"agent": "ward"})

    assert response.status_code == 409
    assert response.json() == {"error": "the switchboard is not configured"}


def test_feed_flattens_one_conversation_into_ask_then_reply(
    session_factory, config, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    _seed_agent(session_factory, cfg)
    summaries = [_feed_summary("t-1", target="network", caller="agent:ward")]
    detail = _feed_detail(
        "t-1",
        target="network",
        caller="agent:ward",
        created_at="2026-08-19T10:00:00Z",
        updated_at="2026-08-19T10:00:05Z",
        ask_text="What's the config?",
        reply_text="Here's the config.",
    )
    fake_cabal = FakeCabal(conversations_result=summaries, conversation_results={"t-1": detail})
    client = _make_client(session_factory, cfg, cabal=fake_cabal)

    response = client.get("/api/colleagues/feed", params={"agent": "ward"})

    assert response.status_code == 200
    assert response.json() == {
        "entries": [
            {
                "ts": "2026-08-19T10:00:00Z",
                "conversation_id": "t-1",
                "from": "ward",
                "to": "network",
                "role": "ask",
                "text": "What's the config?",
            },
            {
                "ts": "2026-08-19T10:00:05Z",
                "conversation_id": "t-1",
                "from": "network",
                "to": "ward",
                "role": "reply",
                "text": "Here's the config.",
            },
        ],
        "truncated": False,
    }
    # Fix round 2: TWO server-side-scoped queries (the switchboard's
    # omitted-limit default was 50, never unbounded) -- see
    # `colleague_feed`'s own docstring.
    assert fake_cabal.list_conversations_calls[-2:] == [
        {"limit": 200, "target": "ward", "caller": None},
        {"limit": 200, "target": None, "caller": "agent:ward"},
    ]


def test_feed_ask_line_shows_the_question_not_the_answer_when_history_holds_both(
    session_factory, config, monkeypatch
):
    """Regression: the 'ward told gemini-one what a PSC is' display bug. A
    completed daedalus conversation's `history` ends with the AGENT reply, so
    deriving the ask from `history[-1]` put the answer on the outgoing (ask)
    line. Here the ask is a short question and the reply is a long, distinct
    answer -- the ask entry MUST carry the question, never the answer text."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    _seed_agent(session_factory, cfg)
    question = "what a psc is"
    answer = "PSC stands for Private Service Connect, a Google Cloud networking feature..."
    summaries = [_feed_summary("t-psc", target="gemini-one", caller="agent:ward")]
    detail = _feed_detail(
        "t-psc",
        target="gemini-one",
        caller="agent:ward",
        created_at="2026-08-23T04:30:40Z",
        updated_at="2026-08-23T04:30:46Z",
        ask_text=question,
        reply_text=answer,
    )
    fake_cabal = FakeCabal(conversations_result=summaries, conversation_results={"t-psc": detail})
    client = _make_client(session_factory, cfg, cabal=fake_cabal)

    entries = client.get("/api/colleagues/feed", params={"agent": "ward"}).json()["entries"]

    ask = next(e for e in entries if e["role"] == "ask")
    reply = next(e for e in entries if e["role"] == "reply")
    assert ask["from"] == "ward" and ask["to"] == "gemini-one"
    assert ask["text"] == question  # the QUESTION, not the answer
    assert answer not in ask["text"]  # the answer never bleeds onto the ask line
    assert reply["from"] == "gemini-one" and reply["to"] == "ward"
    assert reply["text"] == answer


def test_feed_ask_survives_the_echo_agent_shape_history_user_only(
    session_factory, config, monkeypatch
):
    """The fix must not regress cabal's builtin EchoAgent shape, whose
    persisted `history` is the user message ALONE (no agent turn). The last
    user-role message is still the ask; the reply comes from status.message."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    _seed_agent(session_factory, cfg)
    summaries = [_feed_summary("t-echo", target="echo", caller="agent:ward")]
    detail = {
        "conversation": {
            "task_id": "t-echo",
            "target_agent": "echo",
            "caller_agent": "agent:ward",
            "created_at": "2026-08-23T05:00:00Z",
            "updated_at": "2026-08-23T05:00:01Z",
            "result": {
                "status": {
                    "state": "completed",
                    "message": {"role": "agent", "parts": [{"kind": "text", "text": "echo: hi"}]},
                },
                # EchoAgent shape: history is the user message ALONE.
                "history": [{"role": "user", "parts": [{"kind": "text", "text": "hi"}]}],
            },
        },
        "events": [],
    }
    fake_cabal = FakeCabal(conversations_result=summaries, conversation_results={"t-echo": detail})
    client = _make_client(session_factory, cfg, cabal=fake_cabal)

    entries = client.get("/api/colleagues/feed", params={"agent": "ward"}).json()["entries"]
    ask = next(e for e in entries if e["role"] == "ask")
    assert ask["text"] == "hi"
    reply = next(e for e in entries if e["role"] == "reply")
    assert reply["text"] == "echo: hi"


def test_feed_reply_joins_all_text_parts_of_a_multi_part_message(
    session_factory, config, monkeypatch
):
    """The family's join-all-parts contract (`_join_text_parts`,
    kobo_api.clients) reaches the terminal feed too: a reply whose
    `status.message` carries SEVERAL text parts must render them all, in
    order -- `parts[0]`-only extraction silently truncates the day any
    a2a-compliant peer (a federated colleague, not this room's own
    single-part daedalus agents) emits a multi-part reply."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    _seed_agent(session_factory, cfg)
    summaries = [_feed_summary("t-multi", target="network", caller="agent:ward")]
    detail = _feed_detail(
        "t-multi",
        target="network",
        caller="agent:ward",
        created_at="2026-08-23T06:00:00Z",
        updated_at="2026-08-23T06:00:05Z",
        ask_text="What's the config?",
        reply_text="unused-overridden-below",
    )
    detail["conversation"]["result"]["status"]["message"]["parts"] = [
        {"kind": "text", "text": "Part one."},
        {"kind": "text", "text": "Part two."},
    ]
    fake_cabal = FakeCabal(conversations_result=summaries, conversation_results={"t-multi": detail})
    client = _make_client(session_factory, cfg, cabal=fake_cabal)

    entries = client.get("/api/colleagues/feed", params={"agent": "ward"}).json()["entries"]

    reply = next(e for e in entries if e["role"] == "reply")
    assert reply["text"] == "Part one.\nPart two."


def test_feed_ask_survives_a_leading_non_text_part(session_factory, config, monkeypatch):
    """An ask message whose FIRST part is not text (a data part leading, the
    text behind it) must still put its text on the ask line -- `parts[0]`-only
    extraction returned None here, which dropped the ask line entirely and
    rendered the conversation as a reply from nowhere."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    _seed_agent(session_factory, cfg)
    summaries = [_feed_summary("t-data", target="network", caller="agent:ward")]
    detail = _feed_detail(
        "t-data",
        target="network",
        caller="agent:ward",
        created_at="2026-08-23T06:10:00Z",
        updated_at="2026-08-23T06:10:05Z",
        ask_text="unused-overridden-below",
        reply_text="Here's the config.",
    )
    detail["conversation"]["result"]["history"][0]["parts"] = [
        {"kind": "data", "data": {"trace_id": "irrelevant-to-the-feed"}},
        {"kind": "text", "text": "What's the config?"},
    ]
    fake_cabal = FakeCabal(conversations_result=summaries, conversation_results={"t-data": detail})
    client = _make_client(session_factory, cfg, cabal=fake_cabal)

    entries = client.get("/api/colleagues/feed", params={"agent": "ward"}).json()["entries"]

    asks = [e["text"] for e in entries if e["role"] == "ask"]
    assert asks == ["What's the config?"]


def test_feed_emits_no_line_for_an_empty_text_reply(session_factory, config, monkeypatch):
    """A reply whose only text part is the empty string contributes NO line,
    same as a message with no text-shaped part at all -- pinned deliberately:
    the pre-join extractor returned "" here and the terminal rendered a blank
    reply line, which is noise, not conversation content."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    _seed_agent(session_factory, cfg)
    summaries = [_feed_summary("t-blank", target="network", caller="agent:ward")]
    detail = _feed_detail(
        "t-blank",
        target="network",
        caller="agent:ward",
        created_at="2026-08-23T06:20:00Z",
        updated_at="2026-08-23T06:20:05Z",
        ask_text="What's the config?",
        reply_text="unused-overridden-below",
    )
    detail["conversation"]["result"]["status"]["message"]["parts"] = [
        {"kind": "text", "text": ""},
    ]
    fake_cabal = FakeCabal(conversations_result=summaries, conversation_results={"t-blank": detail})
    client = _make_client(session_factory, cfg, cabal=fake_cabal)

    entries = client.get("/api/colleagues/feed", params={"agent": "ward"}).json()["entries"]

    assert [e["role"] for e in entries] == ["ask"]


def test_feed_emits_no_line_for_an_empty_text_ask(session_factory, config, monkeypatch):
    """The ask-side twin of the empty-text pin above: an ask whose only text
    part is the empty string contributes no line either -- both directions go
    through the same extractor, documented symmetrically."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    _seed_agent(session_factory, cfg)
    summaries = [_feed_summary("t-blank-ask", target="network", caller="agent:ward")]
    detail = _feed_detail(
        "t-blank-ask",
        target="network",
        caller="agent:ward",
        created_at="2026-08-23T06:30:00Z",
        updated_at="2026-08-23T06:30:05Z",
        ask_text="unused-overridden-below",
        reply_text="Here's the config.",
    )
    detail["conversation"]["result"]["history"][0]["parts"] = [{"kind": "text", "text": ""}]
    fake_cabal = FakeCabal(
        conversations_result=summaries, conversation_results={"t-blank-ask": detail}
    )
    client = _make_client(session_factory, cfg, cabal=fake_cabal)

    entries = client.get("/api/colleagues/feed", params={"agent": "ward"}).json()["entries"]

    assert [e["role"] for e in entries] == ["reply"]


def test_feed_strips_a_raw_no_prefix_caller_unchanged(session_factory, config, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    _seed_agent(session_factory, cfg, name="network")
    summaries = [_feed_summary("t-1", target="network", caller="weird-caller-no-prefix")]
    detail = _feed_detail(
        "t-1",
        target="network",
        caller="weird-caller-no-prefix",
        created_at="2026-08-19T10:00:00Z",
        updated_at="2026-08-19T10:00:05Z",
        ask_text="ask",
        reply_text="reply",
    )
    fake_cabal = FakeCabal(conversations_result=summaries, conversation_results={"t-1": detail})
    client = _make_client(session_factory, cfg, cabal=fake_cabal)

    response = client.get("/api/colleagues/feed", params={"agent": "network"})

    assert response.status_code == 200
    entries = response.json()["entries"]
    assert entries[0]["from"] == "weird-caller-no-prefix"
    assert entries[1]["to"] == "weird-caller-no-prefix"


def test_feed_sorts_ascending_across_multiple_conversations(
    session_factory, config, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    _seed_agent(session_factory, cfg)
    # Listed most-recent-first (t-2 before t-1), same as the real
    # switchboard's own `list_conversations` ordering -- the feed's own
    # ascending sort must undo that, not just pass it through.
    summaries = [
        _feed_summary("t-2", target="network", caller="agent:ward"),
        _feed_summary("t-1", target="network", caller="agent:ward"),
    ]
    details = {
        "t-1": _feed_detail(
            "t-1",
            target="network",
            caller="agent:ward",
            created_at="2026-08-19T10:00:00Z",
            updated_at="2026-08-19T10:00:05Z",
            ask_text="first ask",
            reply_text="first reply",
        ),
        "t-2": _feed_detail(
            "t-2",
            target="network",
            caller="agent:ward",
            created_at="2026-08-19T11:00:00Z",
            updated_at="2026-08-19T11:00:03Z",
            ask_text="second ask",
            reply_text="second reply",
        ),
    }
    fake_cabal = FakeCabal(conversations_result=summaries, conversation_results=details)
    client = _make_client(session_factory, cfg, cabal=fake_cabal)

    response = client.get("/api/colleagues/feed", params={"agent": "ward"})

    assert response.status_code == 200
    texts = [e["text"] for e in response.json()["entries"]]
    assert texts == ["first ask", "first reply", "second ask", "second reply"]


def test_feed_only_includes_conversations_the_agent_participates_in(
    session_factory, config, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    _seed_agent(session_factory, cfg)
    summaries = [
        _feed_summary("t-1", target="network", caller="agent:ward"),
        _feed_summary("t-9", target="someone-else", caller="agent:other"),
    ]
    details = {
        "t-1": _feed_detail(
            "t-1",
            target="network",
            caller="agent:ward",
            created_at="2026-08-19T10:00:00Z",
            updated_at="2026-08-19T10:00:05Z",
            ask_text="ask",
            reply_text="reply",
        ),
    }
    fake_cabal = FakeCabal(conversations_result=summaries, conversation_results=details)
    client = _make_client(session_factory, cfg, cabal=fake_cabal)

    response = client.get("/api/colleagues/feed", params={"agent": "ward"})

    assert response.status_code == 200
    # t-9 never even gets a get_conversation call -- filtered before the
    # detail fetch, same efficiency the existing list route's own
    # agent-filter buys.
    assert fake_cabal.get_conversation_calls == ["t-1"]
    assert [e["conversation_id"] for e in response.json()["entries"]] == ["t-1", "t-1"]


def test_feed_skips_a_conversation_with_no_completed_result_yet(
    session_factory, config, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    _seed_agent(session_factory, cfg)
    summaries = [_feed_summary("t-1", target="network", caller="agent:ward")]
    in_flight_detail = {
        "conversation": {
            "task_id": "t-1",
            "target_agent": "network",
            "caller_agent": "agent:ward",
            "created_at": "2026-08-19T10:00:00Z",
            "updated_at": "2026-08-19T10:00:00Z",
            "result": None,
        },
        "events": [],
    }
    fake_cabal = FakeCabal(
        conversations_result=summaries, conversation_results={"t-1": in_flight_detail}
    )
    client = _make_client(session_factory, cfg, cabal=fake_cabal)

    response = client.get("/api/colleagues/feed", params={"agent": "ward"})

    assert response.status_code == 200
    assert response.json() == {"entries": [], "truncated": False}


def test_feed_caps_at_200_most_recent_entries_and_flags_truncated(
    session_factory, config, monkeypatch
):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    _seed_agent(session_factory, cfg)
    n = 101  # 101 conversations * 2 entries each = 202 > the 200 cap
    summaries = [_feed_summary(f"t-{i}", target="network", caller="agent:ward") for i in range(n)]
    details = {}
    for i in range(n):
        hh, mm = divmod(i, 60)
        details[f"t-{i}"] = _feed_detail(
            f"t-{i}",
            target="network",
            caller="agent:ward",
            created_at=f"2026-08-19T{hh:02d}:{mm:02d}:00Z",
            updated_at=f"2026-08-19T{hh:02d}:{mm:02d}:01Z",
            ask_text=f"ask-{i}",
            reply_text=f"reply-{i}",
        )
    fake_cabal = FakeCabal(conversations_result=summaries, conversation_results=details)
    client = _make_client(session_factory, cfg, cabal=fake_cabal)

    response = client.get("/api/colleagues/feed", params={"agent": "ward"})

    assert response.status_code == 200
    body = response.json()
    assert body["truncated"] is True
    assert len(body["entries"]) == 200
    # The 2 earliest entries (t-0's ask + reply) were dropped to make room.
    assert body["entries"][0]["text"] == "ask-1"
    assert body["entries"][-1]["text"] == "reply-100"


def test_feed_not_truncated_when_at_or_under_the_cap(session_factory, config, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    _seed_agent(session_factory, cfg)
    summaries = [_feed_summary("t-1", target="network", caller="agent:ward")]
    detail = _feed_detail(
        "t-1",
        target="network",
        caller="agent:ward",
        created_at="2026-08-19T10:00:00Z",
        updated_at="2026-08-19T10:00:05Z",
        ask_text="ask",
        reply_text="reply",
    )
    fake_cabal = FakeCabal(conversations_result=summaries, conversation_results={"t-1": detail})
    client = _make_client(session_factory, cfg, cabal=fake_cabal)

    response = client.get("/api/colleagues/feed", params={"agent": "ward"})

    assert response.json()["truncated"] is False


# --- Regression: scope FIRST, cap AFTER (fix round 1) -----------------------
#
# The High finding this fixed: the room-wide `list_conversations(limit=200)`
# call truncated to the room's 200 MOST-RECENT conversations before `agent`
# scoping ever ran, so a quiet agent whose own conversations fell outside
# that room-wide window vanished from its own feed with `truncated: false`
# -- correct-looking, silently wrong. Both tests below rely on `FakeCabal.
# list_conversations` actually honoring `limit` (the separate Low finding:
# the old fake ignored it entirely, which is exactly why no test caught the
# High bug in the first place).


def test_feed_finds_the_agents_history_past_a_busy_rooms_recent_traffic(
    session_factory, config, monkeypatch
):
    """250 conversations room-wide (most-recent-first, matching the real
    switchboard's own ordering); only 3 belong to `ward`, and those 3 are
    the OLDEST of the 250 -- the last three in the list. A room-wide fetch
    (default 50, clamp 200 upstream) would return only the most recent,
    none of which are `ward`'s, and the feed would come back empty with
    `truncated: false`. The server-side-scoped queries (target=ward +
    caller=agent:ward) find them no matter how busy the room is."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    _seed_agent(session_factory, cfg)
    noise = [
        _feed_summary(f"t-noise-{i}", target="someone-else", caller="agent:other")
        for i in range(247)
    ]
    wards_own = [
        _feed_summary(f"t-ward-{i}", target="network", caller="agent:ward") for i in range(3)
    ]
    summaries = noise + wards_own  # ward's 3 are the last (oldest) of the 250
    details = {
        f"t-ward-{i}": _feed_detail(
            f"t-ward-{i}",
            target="network",
            caller="agent:ward",
            created_at=f"2026-08-01T{i:02d}:00:00Z",
            updated_at=f"2026-08-01T{i:02d}:00:05Z",
            ask_text=f"ask-{i}",
            reply_text=f"reply-{i}",
        )
        for i in range(3)
    }
    fake_cabal = FakeCabal(conversations_result=summaries, conversation_results=details)
    client = _make_client(session_factory, cfg, cabal=fake_cabal)

    response = client.get("/api/colleagues/feed", params={"agent": "ward"})

    assert response.status_code == 200
    body = response.json()
    assert body["truncated"] is False
    assert [e["text"] for e in body["entries"]] == [
        "ask-0", "reply-0", "ask-1", "reply-1", "ask-2", "reply-2",
    ]
    # The noise conversations never even got a detail fetch -- filtered
    # before that, same as the existing agent-participant-scoping test.
    assert fake_cabal.get_conversation_calls == ["t-ward-0", "t-ward-1", "t-ward-2"]
    # Both directions were fetched server-side-scoped -- the room-wide
    # window can no longer crowd ward out (fix round 2).
    assert fake_cabal.list_conversations_calls[-2:] == [
        {"limit": 200, "target": "ward", "caller": None},
        {"limit": 200, "target": None, "caller": "agent:ward"},
    ]


def test_feed_caps_the_agents_own_history_even_amid_busier_room_wide_traffic(
    session_factory, config, monkeypatch
):
    """`ward`'s own 101 conversations (-> 202 entries, 2 over the cap) sit
    BEHIND 150 noisier, more-recent conversations from other agents in the
    room-wide list -- so a room-wide `limit=200` fetch would only surface
    50 of `ward`'s 101 conversations (100 entries, under the cap, `
    truncated: false`), silently dropping the other half of a legitimately
    busy agent's own history. The fix (server-side-scoped queries, both
    directions) finds the full 101 and applies the 200-entry cap to
    `ward`'s OWN scoped, flattened history -- not the room's -- landing on
    exactly the 200 newest entries and `truncated: true`."""
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    _seed_agent(session_factory, cfg)
    noise = [
        _feed_summary(f"t-noise-{i}", target="someone-else", caller="agent:other")
        for i in range(150)
    ]
    n = 101
    wards_own = [
        _feed_summary(f"t-ward-{i}", target="network", caller="agent:ward") for i in range(n)
    ]
    summaries = noise + wards_own  # ward's own are the older (latter) block
    details = {}
    for i in range(n):
        hh, mm = divmod(i, 60)
        details[f"t-ward-{i}"] = _feed_detail(
            f"t-ward-{i}",
            target="network",
            caller="agent:ward",
            created_at=f"2026-08-19T{hh:02d}:{mm:02d}:00Z",
            updated_at=f"2026-08-19T{hh:02d}:{mm:02d}:01Z",
            ask_text=f"ask-{i}",
            reply_text=f"reply-{i}",
        )
    fake_cabal = FakeCabal(conversations_result=summaries, conversation_results=details)
    client = _make_client(session_factory, cfg, cabal=fake_cabal)

    response = client.get("/api/colleagues/feed", params={"agent": "ward"})

    assert response.status_code == 200
    body = response.json()
    assert body["truncated"] is True
    assert len(body["entries"]) == 200
    # The 2 oldest entries (t-ward-0's ask + reply) were dropped to make
    # room -- the noise conversations never contributed an entry at all.
    assert body["entries"][0]["text"] == "ask-1"
    assert body["entries"][-1]["text"] == "reply-100"
    assert fake_cabal.list_conversations_calls[-2:] == [
        {"limit": 200, "target": "ward", "caller": None},
        {"limit": 200, "target": None, "caller": "agent:ward"},
    ]


def test_feed_list_failure_returns_502_plain_copy(session_factory, config, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    _seed_agent(session_factory, cfg)
    fake_cabal = FakeCabal(conversations_error="switchboard unreachable")
    client = _make_client(session_factory, cfg, cabal=fake_cabal)

    response = client.get("/api/colleagues/feed", params={"agent": "ward"})

    assert response.status_code == 502
    assert response.json() == {
        "error": "The switchboard did not answer. Try again shortly."
    }


def test_feed_detail_failure_returns_502_plain_copy(session_factory, config, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    cfg = _enabled_config(config)
    _seed_agent(session_factory, cfg)
    summaries = [_feed_summary("t-1", target="network", caller="agent:ward")]
    fake_cabal = FakeCabal(conversations_result=summaries, conversation_error="boom upstream")
    client = _make_client(session_factory, cfg, cabal=fake_cabal)

    response = client.get("/api/colleagues/feed", params={"agent": "ward"})

    assert response.status_code == 502
    assert response.json() == {
        "error": "The switchboard did not answer. Try again shortly."
    }


# --- Visibility (the feed's one real gate beyond the room-wide 409) --------
#
# Same real-JWT idiom test_web_visibility.py uses for its own IAP slices: a
# locally generated ES256 keypair signs the assertion, verified through the
# actual IapMiddleware/IapVerifier, not a mock.

_VISIBILITY_AUDIENCE = "feed-visibility-audience"
_VISIBILITY_KID = "feed-visibility-key-1"


class _StrangerGroupResolver:
    """The plainest possible `GroupResolver` stand-in: everyone belongs to
    nothing, so ownership is the only route to visibility left standing."""

    def groups_for(self, email: str) -> list[str] | None:
        return []


def _generate_jwk(kid: str = _VISIBILITY_KID):
    private_key = ec.generate_private_key(ec.SECP256R1())
    jwk = jwt.algorithms.ECAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk["kid"] = kid
    jwk["use"] = "sig"
    jwk["alg"] = "ES256"
    return private_key, jwk


def _sign(private_key, *, email: str, kid: str = _VISIBILITY_KID) -> str:
    now = int(time.time())
    claims = {
        "aud": _VISIBILITY_AUDIENCE,
        "iss": IAP_ISSUER,
        "iat": now,
        "exp": now + 3600,
        "sub": "accounts.google.com:1234567890",
        "email": email,
    }
    return jwt.encode(claims, private_key, algorithm="ES256", headers={"kid": kid})


def _iap_headers(private_key, *, email: str) -> dict:
    return {ASSERTION_HEADER: _sign(private_key, email=email)}


def test_feed_404s_when_the_caller_cannot_see_the_agent(session_factory, tmp_path, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    private_key, jwk = _generate_jwk()
    cfg = Config(
        workdir=tmp_path / "work",
        cabal_url="http://cabal.internal",
        agent_endpoint_template=ENDPOINT_TEMPLATE,
        iap_audience=_VISIBILITY_AUDIENCE,
        groups=[GroupChoice(name="payments-eng", okta_group="okta-payments")],
    )
    _seed_agent(session_factory, cfg, name="ward", group_name="payments-eng")
    client = TestClient(
        create_app(
            session_factory,
            config=cfg,
            clients=Clients(
                github=FakeGitHub(),
                gojira=FakeGojira(),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=FakeKubectl(),
                cabal=FakeCabal(),
                groups=_StrangerGroupResolver(),
            ),
            iap_jwks_fetcher=lambda: {"keys": [jwk]},
        )
    )

    response = client.get(
        "/api/colleagues/feed",
        params={"agent": "ward"},
        headers=_iap_headers(private_key, email="stranger@example.com"),
    )

    assert response.status_code == 404
    assert response.json() == {"error": "no such agent: 'ward'"}


def test_feed_200s_for_the_agents_own_owner(session_factory, tmp_path, monkeypatch):
    monkeypatch.setenv("CABAL_OPERATOR_TOKEN", "op-token")
    private_key, jwk = _generate_jwk()
    owner_email = "owner@example.com"
    cfg = Config(
        workdir=tmp_path / "work",
        cabal_url="http://cabal.internal",
        agent_endpoint_template=ENDPOINT_TEMPLATE,
        iap_audience=_VISIBILITY_AUDIENCE,
        groups=[GroupChoice(name="payments-eng", okta_group="okta-payments")],
    )
    _seed_agent(
        session_factory, cfg, name="ward", group_name="payments-eng", created_by=owner_email
    )
    client = TestClient(
        create_app(
            session_factory,
            config=cfg,
            clients=Clients(
                github=FakeGitHub(),
                gojira=FakeGojira(),
                cloud=FakeCloud(),
                agent=FakeAgent(),
                kubectl=FakeKubectl(),
                cabal=FakeCabal(),
                groups=_StrangerGroupResolver(),
            ),
            iap_jwks_fetcher=lambda: {"keys": [jwk]},
        )
    )

    response = client.get(
        "/api/colleagues/feed",
        params={"agent": "ward"},
        headers=_iap_headers(private_key, email=owner_email),
    )

    assert response.status_code == 200
    assert response.json() == {"entries": [], "truncated": False}
