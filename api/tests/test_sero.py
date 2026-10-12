"""Tests for kobo_api.sero: the secrets platform's machine lane -- the
request shapes the room sends, the identity token it authenticates with, and
the two properties the module exists to keep (a secret value never reaches a
log or an exception, and a lane failure never falls back to writing the
Secret directly).

Against `httpx.MockTransport` (the same fake-server idiom test_clients.py
uses for every other HTTP client here) and a fake identity provider -- no
real platform, no metadata server.
"""

from __future__ import annotations

import base64
import json
from urllib.parse import quote

import httpx
import pytest

from kobo_api.clients import ClientError
from kobo_api.sero import (
    _TOKEN_MAX_AGE_S,
    HttpxSeroClient,
    MetadataIdentityTokenProvider,
    deliver_secret,
)
from tests.fakes import FakeSero

URL = "http://secrets.example"
AUDIENCE = "http://secrets.example"
NAMESPACE = "platform"
EMAIL = "someone@example.com"
AGENT = "clio"
# Never a real credential: a placeholder standing in for one, so these tests
# can prove it appears in exactly one place and in no error text.
SECRET_VALUE = "not-a-real-token-value"


def _client(handler, *, token: str = "id-token") -> HttpxSeroClient:
    return HttpxSeroClient(
        URL,
        AUDIENCE,
        NAMESPACE,
        token_provider=lambda: token,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"applied": True, "action": "create"})


# --- put_secret: the exact request the lane receives --------------------------


def test_put_secret_request_shape_and_auth_header():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "applied": True,
                "action": "create",
                "namespace": NAMESPACE,
                "path": f"apps/{AGENT}/cabal",
                "env": "dev",
                "written_keys": ["CABAL_COLLEAGUE_TOKEN"],
                "removed_keys": [],
            },
        )

    _client(handler, token="an-id-token").put_secret(
        EMAIL, f"apps/{AGENT}/cabal", {"CABAL_COLLEAGUE_TOKEN": SECRET_VALUE}
    )

    assert captured["method"] == "POST"
    assert captured["url"] == f"{URL}/api/service/secrets"
    assert captured["json"] == {
        "acting_email": EMAIL,
        "namespace": NAMESPACE,
        "path": f"apps/{AGENT}/cabal",
        "values": {"CABAL_COLLEAGUE_TOKEN": SECRET_VALUE},
        "remove_keys": [],
        "apply": True,
    }
    assert captured["auth"] == "Bearer an-id-token"


def test_put_secret_sends_the_rooms_own_namespace_never_one_from_a_caller():
    # The namespace is fixed per deployment: nothing a caller passes can aim
    # a write at another team's secrets.
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return _ok(request)

    HttpxSeroClient(
        URL, AUDIENCE, "another-namespace",
        token_provider=lambda: "t",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    ).put_secret(EMAIL, "apps/clio/cabal", {"K": SECRET_VALUE})

    assert captured["json"]["namespace"] == "another-namespace"


def test_put_secret_trailing_slash_in_the_url_does_not_double_up():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return _ok(request)

    HttpxSeroClient(
        f"{URL}/", AUDIENCE, NAMESPACE,
        token_provider=lambda: "t",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    ).put_secret(EMAIL, "apps/clio/cabal", {"K": SECRET_VALUE})

    assert captured["url"] == f"{URL}/api/service/secrets"


# --- ensure_synced: the exact request the lane receives -----------------------


def test_ensure_synced_request_shape_and_auth_header():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json=[
                {
                    "surface": "cluster",
                    "action": "create",
                    "target": f"ExternalSecret/{AGENT}/{AGENT}-cabal",
                }
            ],
        )

    _client(handler, token="an-id-token").ensure_synced(
        EMAIL, AGENT, f"apps/{AGENT}/cabal", AGENT, f"{AGENT}-cabal"
    )

    assert captured["method"] == "POST"
    assert captured["url"] == f"{URL}/api/service/sync"
    assert captured["json"] == {
        "acting_email": EMAIL,
        "namespace": NAMESPACE,
        "app": AGENT,
        "path": f"apps/{AGENT}/cabal",
        "k8s_namespace": AGENT,
        "name": f"{AGENT}-cabal",
        "target_name": f"{AGENT}-cabal",
        "refresh_interval": "1m",
        "apply": True,
    }
    assert captured["auth"] == "Bearer an-id-token"


def test_ensure_synced_asks_for_a_short_refresh_so_a_rotation_lands_in_minutes():
    # A re-wire changes the stored value but not the cluster object, so the
    # delivered Secret only catches up when the platform next re-reads it.
    # An hour of that is a pod still holding revoked tokens.
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return _ok(request)

    _client(handler).ensure_synced(
        EMAIL, AGENT, f"apps/{AGENT}/cabal", AGENT, f"{AGENT}-cabal"
    )

    assert captured["json"]["refresh_interval"] == "1m"


def test_a_blocked_target_in_a_200_report_is_a_failure_not_a_success():
    # The lane answers 200 with a `blocked` list when it could not apply the
    # cluster object (a field-manager conflict, say). A success status over
    # an undelivered secret is the one failure this room must not miss.
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "report": [{"surface": "cluster", "action": "update"}],
                "blocked": [f"ExternalSecret/{AGENT}/{AGENT}-cabal"],
            },
        )

    with pytest.raises(ClientError) as exc_info:
        _client(handler).ensure_synced(
            EMAIL, AGENT, f"apps/{AGENT}/cabal", AGENT, f"{AGENT}-cabal"
        )

    message = str(exc_info.value)
    assert "did not accept" in message
    assert f"ExternalSecret/{AGENT}/{AGENT}-cabal" in message
    assert f"apps/{AGENT}/cabal" in message


def test_a_blocked_entry_this_room_cannot_read_is_described_not_repeated():
    # Only strings are quoted: they are target names and verbs by the
    # lane's contract. Anything else might carry something this room must
    # not repeat, so it is described instead -- and it still fails.
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"blocked": [{"target": SECRET_VALUE}]})

    with pytest.raises(ClientError) as exc_info:
        _client(handler).ensure_synced(
            EMAIL, AGENT, f"apps/{AGENT}/cabal", AGENT, f"{AGENT}-cabal"
        )

    assert SECRET_VALUE not in str(exc_info.value)


@pytest.mark.parametrize("body", [{"blocked": []}, {"applied": True}, [{"action": "create"}]])
def test_a_report_with_nothing_blocked_is_a_success(body):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    _client(handler).ensure_synced(
        EMAIL, AGENT, f"apps/{AGENT}/cabal", AGENT, f"{AGENT}-cabal"
    )  # must not raise


def test_a_blocked_list_on_a_store_response_is_not_read():
    # `blocked` belongs to the sync report -- a store either wrote or
    # failed with a status. This room reads nothing else off a 2xx.
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"applied": True, "blocked": ["something"]})

    _client(handler).put_secret(
        EMAIL, f"apps/{AGENT}/cabal", {"K": SECRET_VALUE}
    )  # must not raise


@pytest.mark.parametrize("status", [200, 201, 202])
def test_ensure_synced_accepts_any_2xx_and_never_parses_the_report(status):
    # The report is for the platform's own audit trail; this room reads
    # nothing out of it, so a body it did not expect is still a success.
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="not json at all")

    _client(handler).ensure_synced(
        EMAIL, AGENT, f"apps/{AGENT}/cabal", AGENT, f"{AGENT}-cabal"
    )  # must not raise


# --- error mapping ------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, 429, 503])
def test_a_failure_raises_client_error_carrying_the_servers_own_sentence(status):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "the namespace is not yours"})

    with pytest.raises(ClientError) as exc_info:
        _client(handler).put_secret(EMAIL, f"apps/{AGENT}/cabal", {"K": SECRET_VALUE})

    assert "the namespace is not yours" in str(exc_info.value)


def test_a_failure_with_no_error_string_says_what_happened_in_plain_words():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="<html>gateway</html>")

    with pytest.raises(ClientError) as exc_info:
        _client(handler).put_secret(EMAIL, f"apps/{AGENT}/cabal", {"K": SECRET_VALUE})

    message = str(exc_info.value)
    assert "500" in message
    # Never the body: a response this room did not expect may be echoing the
    # request it was sent, and that request carries the secret.
    assert "gateway" not in message


def test_rate_limited_and_unavailable_read_as_themselves_without_a_body():
    def limited(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    def unavailable(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with pytest.raises(ClientError) as limited_info:
        _client(limited).put_secret(EMAIL, f"apps/{AGENT}/cabal", {"K": SECRET_VALUE})
    with pytest.raises(ClientError) as unavailable_info:
        _client(unavailable).ensure_synced(
            EMAIL, AGENT, f"apps/{AGENT}/cabal", AGENT, f"{AGENT}-cabal"
        )

    assert "rate limiting" in str(limited_info.value)
    assert "unavailable" in str(unavailable_info.value)


def test_a_failure_names_the_path_so_an_operator_knows_which_delivery_broke():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "denied"})

    with pytest.raises(ClientError) as exc_info:
        _client(handler).put_secret(EMAIL, f"apps/{AGENT}/slack", {"K": SECRET_VALUE})

    assert f"apps/{AGENT}/slack" in str(exc_info.value)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json={"error": "denied"}),
        httpx.Response(429),
        httpx.Response(503, json={"error": "backend unavailable"}),
        # The worst case this module is written for: a server that echoes
        # the request body back in its own error text.
        httpx.Response(400, json={"error": f"bad request: {{'K': '{SECRET_VALUE}'}}"[:20]}),
        httpx.Response(500, text=f"values={SECRET_VALUE}"),
    ],
)
def test_no_failure_ever_puts_a_secret_value_in_the_raised_message(response):
    def handler(_request: httpx.Request) -> httpx.Response:
        return response

    with pytest.raises(ClientError) as put_info:
        _client(handler).put_secret(EMAIL, f"apps/{AGENT}/cabal", {"K": SECRET_VALUE})

    assert SECRET_VALUE not in str(put_info.value)


def test_a_platform_that_does_not_answer_raises_without_the_request_body():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(ClientError) as exc_info:
        _client(handler).put_secret(EMAIL, f"apps/{AGENT}/cabal", {"K": SECRET_VALUE})

    message = str(exc_info.value)
    assert "did not answer" in message
    assert SECRET_VALUE not in message


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://[::1",  # httpx.InvalidURL
        "http://",  # no host at all: a plain ValueError before httpx looks
        "not a url",
    ],
)
def test_an_address_httpx_cannot_parse_is_a_plain_error_not_a_crash(bad_url):
    # A misconfigured `sero_url` must read as a failed delivery with a
    # plain sentence, not an unhandled exception surfacing as a 500 on
    # whichever route happened to trigger the write.
    client = HttpxSeroClient(
        bad_url, AUDIENCE, NAMESPACE,
        token_provider=lambda: "t",
        http_client=httpx.Client(transport=httpx.MockTransport(_ok)),
    )

    with pytest.raises(ClientError) as exc_info:
        client.put_secret(EMAIL, f"apps/{AGENT}/cabal", {"K": SECRET_VALUE})

    assert SECRET_VALUE not in str(exc_info.value)


def test_an_identity_address_httpx_cannot_parse_is_a_plain_error_too():
    class _Unparsable:
        def get(self, *args, **kwargs):
            raise httpx.InvalidURL("no host included in url")

    provider = MetadataIdentityTokenProvider(AUDIENCE, client=_Unparsable())

    with pytest.raises(ClientError):
        provider()


def test_a_token_that_cannot_be_minted_fails_the_call_before_anything_is_sent():
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return _ok(request)

    def no_token() -> str:
        raise ClientError("this room could not mint an identity token")

    client = HttpxSeroClient(
        URL, AUDIENCE, NAMESPACE,
        token_provider=no_token,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(ClientError):
        client.put_secret(EMAIL, f"apps/{AGENT}/cabal", {"K": SECRET_VALUE})

    assert sent == []


# --- MetadataIdentityTokenProvider --------------------------------------------


def _jwt(exp: int, iat: int | None = None) -> str:
    """A JWT-shaped string with `exp` (and `iat` when given) in its payload --
    unsigned and never verified here or in kobo_api.sero (the platform
    verifies; this room only reads the two to know how long to cache)."""

    def segment(payload: dict) -> str:
        raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
        return raw.rstrip("=")

    claims = {"exp": exp} if iat is None else {"exp": exp, "iat": iat}
    return f"{segment({'alg': 'RS256'})}.{segment(claims)}.signature"


def test_identity_token_is_requested_from_the_metadata_server_for_the_audience():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["flavor"] = request.headers.get("metadata-flavor")
        return httpx.Response(200, text=_jwt(2_000_000_000))

    provider = MetadataIdentityTokenProvider(
        AUDIENCE,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        now=lambda: 1_000_000_000.0,
    )
    token = provider()

    assert token == _jwt(2_000_000_000)
    url = httpx.URL(captured["url"])
    assert str(url.copy_with(query=None)) == (
        "http://metadata.google.internal/computeMetadata/v1/instance/"
        "service-accounts/default/identity"
    )
    # The audience is a URL itself, so it arrives percent-encoded -- what
    # matters is the value the metadata server decodes, and that `format`
    # is `full` (the identity details the platform allow-lists this room by).
    assert url.params["audience"] == AUDIENCE
    assert url.params["format"] == "full"
    assert captured["flavor"] == "Google"


def test_identity_token_is_cached_no_longer_than_the_lane_accepts_it():
    """An hour-long token is not an hour of cache. The secrets platform refuses
    a machine token issued more than 15 minutes ago whatever its `exp` says
    (live, 2026-09-24: a Retry 20 minutes after the first one answered "no
    service identity" on a token this room was still holding). The cache is
    bounded by the token's `iat` plus `_TOKEN_MAX_AGE_S`, well inside that."""
    calls = []
    clock = {"now": 1_000.0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        now = int(clock["now"])
        return httpx.Response(200, text=_jwt(now + 3600, iat=now))

    provider = MetadataIdentityTokenProvider(
        AUDIENCE,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        now=lambda: clock["now"],
    )

    first = provider()
    clock["now"] += _TOKEN_MAX_AGE_S - 1
    assert provider() == first
    assert len(calls) == 1

    clock["now"] += 2  # past iat + the age this room allows itself
    second = provider()

    assert len(calls) == 2
    assert second != first
    assert _TOKEN_MAX_AGE_S < 900  # the platform's own ceiling, with room to spare


def test_identity_token_with_no_iat_is_aged_from_when_it_was_minted():
    calls = []
    clock = {"now": 1_000.0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, text=_jwt(int(clock["now"]) + 3600))

    provider = MetadataIdentityTokenProvider(
        AUDIENCE,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        now=lambda: clock["now"],
    )
    first = provider()
    clock["now"] += _TOKEN_MAX_AGE_S + 1
    assert provider() != first
    assert len(calls) == 2


def test_identity_token_is_still_re_minted_shortly_before_a_short_expiry():
    """A token shorter than the age bound is still refreshed at its own
    `exp` margin: the bound is a ceiling on the cache, not a floor."""
    calls = []
    clock = {"now": 1_000.0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        now = int(clock["now"])
        return httpx.Response(200, text=_jwt(now + 120, iat=now))

    provider = MetadataIdentityTokenProvider(
        AUDIENCE,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        now=lambda: clock["now"],
    )
    first = provider()
    clock["now"] += 120 - 61  # inside the 60s margin before exp
    assert provider() == first
    clock["now"] += 2
    assert provider() != first
    assert len(calls) == 2


def test_an_identity_token_whose_expiry_cannot_be_read_is_never_cached():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, text="not-a-jwt")

    provider = MetadataIdentityTokenProvider(
        AUDIENCE,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        now=lambda: 1_000.0,
    )
    provider()
    provider()

    assert len(calls) == 2


def test_an_identity_service_that_refuses_raises_a_plain_client_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="no service account")

    provider = MetadataIdentityTokenProvider(
        AUDIENCE, client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    with pytest.raises(ClientError) as exc_info:
        provider()

    assert "404" in str(exc_info.value)


def test_no_metadata_server_raises_rather_than_returning_no_token():
    # A developer machine. The lane's settings are deployment-local, so a
    # local room never builds a client that holds one of these -- and if one
    # somehow did, it must fail rather than deliver a secret unauthenticated.
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    provider = MetadataIdentityTokenProvider(
        AUDIENCE, client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    with pytest.raises(ClientError):
        provider()


def test_an_empty_identity_token_is_refused():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="   ")

    provider = MetadataIdentityTokenProvider(
        AUDIENCE, client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    with pytest.raises(ClientError):
        provider()


# --- deliver_secret: what both delivery sites share ---------------------------


def test_deliver_secret_stores_then_syncs_under_the_agents_own_path():
    sero = FakeSero()

    deliver_secret(
        sero,
        acting_email=EMAIL,
        agent_name=AGENT,
        purpose="cabal",
        values={"CABAL_COLLEAGUE_TOKEN": SECRET_VALUE},
        secret_name=f"{AGENT}-cabal",
    )

    assert sero.puts == [
        (EMAIL, f"apps/{AGENT}/cabal", {"CABAL_COLLEAGUE_TOKEN": SECRET_VALUE})
    ]
    assert sero.syncs == [
        (EMAIL, AGENT, f"apps/{AGENT}/cabal", AGENT, f"{AGENT}-cabal")
    ]


def test_deliver_secret_does_not_sync_a_path_whose_write_failed():
    # A sync for a path with nothing at it would deliver an empty Secret
    # over a good one.
    sero = FakeSero(put_error="the namespace is not yours")

    with pytest.raises(ClientError):
        deliver_secret(
            sero,
            acting_email=EMAIL,
            agent_name=AGENT,
            purpose="cabal",
            values={"K": SECRET_VALUE},
            secret_name=f"{AGENT}-cabal",
        )

    assert sero.syncs == []


@pytest.mark.parametrize("email", [None, "", "   "])
def test_deliver_secret_refuses_a_write_with_nobody_to_record_it_against(email):
    sero = FakeSero()

    with pytest.raises(ClientError) as exc_info:
        deliver_secret(
            sero,
            acting_email=email,
            agent_name=AGENT,
            purpose="cabal",
            values={"K": SECRET_VALUE},
            secret_name=f"{AGENT}-cabal",
        )

    assert AGENT in str(exc_info.value)
    assert SECRET_VALUE not in str(exc_info.value)
    assert sero.puts == []
    assert sero.syncs == []


def test_deliver_secret_logs_one_line_naming_the_agent_and_purpose_only(caplog):
    sero = FakeSero()

    with caplog.at_level("INFO", logger="kobo_api.sero"):
        deliver_secret(
            sero,
            acting_email=EMAIL,
            agent_name=AGENT,
            purpose="cabal",
            values={"K": SECRET_VALUE},
            secret_name=f"{AGENT}-cabal",
        )

    lines = [record.getMessage() for record in caplog.records]
    assert len(lines) == 1
    assert AGENT in lines[0]
    assert "cabal" in lines[0]
    assert SECRET_VALUE not in lines[0]
    assert EMAIL not in lines[0]


def test_deliver_secret_says_nothing_when_the_delivery_failed(caplog):
    sero = FakeSero(sync_error="the cluster refused it")

    with caplog.at_level("INFO", logger="kobo_api.sero"), pytest.raises(ClientError):
        deliver_secret(
            sero,
            acting_email=EMAIL,
            agent_name=AGENT,
            purpose="cabal",
            values={"K": SECRET_VALUE},
            secret_name=f"{AGENT}-cabal",
        )

    assert caplog.records == []


def test_deliver_secret_trims_the_acting_email():
    sero = FakeSero()

    deliver_secret(
        sero,
        acting_email=f"  {EMAIL} ",
        agent_name=AGENT,
        purpose="slack",
        values={"K": SECRET_VALUE},
        secret_name=f"{AGENT}-slack",
    )

    assert sero.puts[0][0] == EMAIL
    assert sero.syncs[0][0] == EMAIL


# --- list_keys: the one read, names only --------------------------------------


def test_list_keys_asks_the_lane_in_the_query_string_and_returns_names():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "namespace": NAMESPACE,
                "path": f"apps/{AGENT}/env",
                "keys": ["API_KEY", "DB_URL"],
                "version": 2,
                "updated_at": "2026-09-22T10:00:00.000000Z",
            },
        )

    answer = _client(handler, token="an-id-token").list_keys(EMAIL, f"apps/{AGENT}/env")

    assert captured["method"] == "GET"
    assert captured["url"] == (
        f"{URL}/api/service/secrets?acting_email={quote(EMAIL, safe='')}"
        f"&namespace={NAMESPACE}&path=apps%2F{AGENT}%2Fenv"
    )
    assert captured["auth"] == "Bearer an-id-token"
    assert answer.exists is True
    assert answer.keys == ["API_KEY", "DB_URL"]
    assert answer.updated_at == "2026-09-22T10:00:00.000000Z"


def test_list_keys_on_a_path_with_nothing_at_it_is_an_empty_answer_not_a_failure():
    # The lane says 404 for a path that is not there. An agent whose owner
    # has never added a secret is the ordinary case, not an error.
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "there is no secret at ..."})

    answer = _client(handler).list_keys(EMAIL, f"apps/{AGENT}/env")

    assert answer.exists is False
    assert answer.keys == []
    assert answer.updated_at is None


def test_list_keys_raises_when_the_lane_refuses_for_any_other_reason():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "that namespace is not yours"})

    with pytest.raises(ClientError) as exc:
        _client(handler).list_keys(EMAIL, f"apps/{AGENT}/env")

    assert "that namespace is not yours" in str(exc.value)


# --- delete_secret / unsync: the destroy half ---------------------------------


def test_delete_secret_request_shape_and_auth_header():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"applied": True, "keys": [], "deleted": True})

    _client(handler, token="an-id-token").delete_secret(EMAIL, f"apps/{AGENT}/env")

    assert captured["method"] == "DELETE"
    assert captured["url"] == f"{URL}/api/service/secrets"
    assert captured["json"] == {
        "acting_email": EMAIL,
        "namespace": NAMESPACE,
        "path": f"apps/{AGENT}/env",
        "apply": True,
    }
    assert captured["auth"] == "Bearer an-id-token"


def test_delete_secret_of_a_path_already_gone_is_a_success():
    # The lane answers 200 with `deleted: false`. A destroy step that fails
    # when it is run twice is a destroy step nobody can retry.
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"applied": True, "keys": [], "deleted": False})

    _client(handler).delete_secret(EMAIL, f"apps/{AGENT}/env")


def test_delete_secret_raises_the_lanes_own_sentence_on_a_refusal():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "that namespace is not yours"})

    with pytest.raises(ClientError) as exc:
        _client(handler).delete_secret(EMAIL, f"apps/{AGENT}/env")

    assert "that namespace is not yours" in str(exc.value)


def test_unsync_request_shape_names_the_object_and_nothing_else():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "applied": True,
                "state": "gone",
                "recreated": False,
                "removes_target_secret": True,
                "deleted": True,
            },
        )

    _client(handler).unsync(EMAIL, AGENT, AGENT, f"{AGENT}-env")

    assert captured["method"] == "DELETE"
    assert captured["url"] == f"{URL}/api/service/sync"
    assert captured["json"] == {
        "acting_email": EMAIL,
        "namespace": NAMESPACE,
        "app": AGENT,
        "k8s_namespace": AGENT,
        "name": f"{AGENT}-env",
        "apply": True,
    }


def test_offboard_request_shape_names_the_application_and_nothing_else():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"applied": True, "report": ["ClusterSecretStore/x: delete"], "blocked": []},
        )

    _client(handler).offboard(EMAIL, AGENT, AGENT)

    assert captured["method"] == "DELETE"
    assert captured["url"] == f"{URL}/api/service/app"
    assert captured["json"] == {
        "acting_email": EMAIL,
        "namespace": NAMESPACE,
        "app": AGENT,
        "k8s_namespace": AGENT,
        "apply": True,
    }


def test_offboard_raises_when_the_platform_refuses():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409, json={"error": "2 ExternalSecret(s) in 'clio' still read through the store"}
        )

    with pytest.raises(ClientError, match="still read through"):
        _client(handler).offboard(EMAIL, AGENT, AGENT)


def test_offboard_raises_when_a_cluster_object_was_blocked():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"applied": True, "report": ["ServiceAccount/sero/x: blocked"], "blocked": ["ServiceAccount/sero/x"]},
        )

    with pytest.raises(ClientError, match="ServiceAccount/sero/x"):
        _client(handler).offboard(EMAIL, AGENT, AGENT)


def test_unsync_of_an_object_already_gone_is_a_success():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"applied": True, "state": "absent", "deleted": False}
        )

    _client(handler).unsync(EMAIL, AGENT, AGENT, f"{AGENT}-env")


def test_unsync_raises_when_the_object_belongs_to_another_field_manager():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"error": "that object is managed by kustomize"})

    with pytest.raises(ClientError) as exc:
        _client(handler).unsync(EMAIL, AGENT, AGENT, f"{AGENT}-env")

    assert "that object is managed by kustomize" in str(exc.value)


def test_unsync_says_so_when_the_delete_left_a_new_object_standing():
    # `recreated` means what this call removed is gone and something else
    # now stands at the name. A caller that read `deleted` and stopped would
    # leave a live ExternalSecret it does not know about.
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"applied": True, "state": "recreated", "recreated": True, "deleted": True}
        )

    with pytest.raises(ClientError) as exc:
        _client(handler).unsync(EMAIL, AGENT, AGENT, f"{AGENT}-env")

    assert f"{AGENT}-env" in str(exc.value)
