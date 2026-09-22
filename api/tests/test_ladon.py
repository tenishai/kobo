"""Tests for kobo_api.ladon: the secrets platform's machine lane -- the
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

import httpx
import pytest

from kobo_api.clients import ClientError
from kobo_api.ladon import (
    HttpxLadonClient,
    MetadataIdentityTokenProvider,
    deliver_secret,
)
from tests.fakes import FakeLadon

URL = "http://secrets.example"
AUDIENCE = "http://secrets.example"
NAMESPACE = "platform"
EMAIL = "someone@example.com"
AGENT = "clio"
# Never a real credential: a placeholder standing in for one, so these tests
# can prove it appears in exactly one place and in no error text.
SECRET_VALUE = "not-a-real-token-value"


def _client(handler, *, token: str = "id-token") -> HttpxLadonClient:
    return HttpxLadonClient(
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

    HttpxLadonClient(
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

    HttpxLadonClient(
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


def test_several_blocked_targets_are_all_named():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"blocked": [f"ExternalSecret/{AGENT}/{AGENT}-cabal", "Secret/clio/other"]},
        )

    with pytest.raises(ClientError) as exc_info:
        _client(handler).ensure_synced(
            EMAIL, AGENT, f"apps/{AGENT}/cabal", AGENT, f"{AGENT}-cabal"
        )

    assert f"ExternalSecret/{AGENT}/{AGENT}-cabal, Secret/clio/other" in str(exc_info.value)


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
    # A misconfigured `ladon_url` must read as a failed delivery with a
    # plain sentence, not an unhandled exception surfacing as a 500 on
    # whichever route happened to trigger the write.
    client = HttpxLadonClient(
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

    client = HttpxLadonClient(
        URL, AUDIENCE, NAMESPACE,
        token_provider=no_token,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(ClientError):
        client.put_secret(EMAIL, f"apps/{AGENT}/cabal", {"K": SECRET_VALUE})

    assert sent == []


# --- MetadataIdentityTokenProvider --------------------------------------------


def _jwt(exp: int) -> str:
    """A JWT-shaped string with `exp` in its payload -- unsigned and never
    verified here or in kobo_api.ladon (the platform verifies; this room only
    reads `exp` to know how long to cache)."""

    def segment(payload: dict) -> str:
        raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
        return raw.rstrip("=")

    return f"{segment({'alg': 'RS256'})}.{segment({'exp': exp})}.signature"


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


def test_identity_token_is_cached_until_shortly_before_it_expires():
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
    clock["now"] += 3600 - 61  # still inside the 60s margin before exp
    assert provider() == first
    assert len(calls) == 1

    clock["now"] += 2  # now past exp - 60
    second = provider()

    assert len(calls) == 2
    assert second != first


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
    ladon = FakeLadon()

    deliver_secret(
        ladon,
        acting_email=EMAIL,
        agent_name=AGENT,
        purpose="cabal",
        values={"CABAL_COLLEAGUE_TOKEN": SECRET_VALUE},
        secret_name=f"{AGENT}-cabal",
    )

    assert ladon.puts == [
        (EMAIL, f"apps/{AGENT}/cabal", {"CABAL_COLLEAGUE_TOKEN": SECRET_VALUE})
    ]
    assert ladon.syncs == [
        (EMAIL, AGENT, f"apps/{AGENT}/cabal", AGENT, f"{AGENT}-cabal")
    ]


def test_deliver_secret_does_not_sync_a_path_whose_write_failed():
    # A sync for a path with nothing at it would deliver an empty Secret
    # over a good one.
    ladon = FakeLadon(put_error="the namespace is not yours")

    with pytest.raises(ClientError):
        deliver_secret(
            ladon,
            acting_email=EMAIL,
            agent_name=AGENT,
            purpose="cabal",
            values={"K": SECRET_VALUE},
            secret_name=f"{AGENT}-cabal",
        )

    assert ladon.syncs == []


@pytest.mark.parametrize("email", [None, "", "   "])
def test_deliver_secret_refuses_a_write_with_nobody_to_record_it_against(email):
    ladon = FakeLadon()

    with pytest.raises(ClientError) as exc_info:
        deliver_secret(
            ladon,
            acting_email=email,
            agent_name=AGENT,
            purpose="cabal",
            values={"K": SECRET_VALUE},
            secret_name=f"{AGENT}-cabal",
        )

    assert AGENT in str(exc_info.value)
    assert SECRET_VALUE not in str(exc_info.value)
    assert ladon.puts == []
    assert ladon.syncs == []


def test_deliver_secret_logs_one_line_naming_the_agent_and_purpose_only(caplog):
    ladon = FakeLadon()

    with caplog.at_level("INFO", logger="kobo_api.ladon"):
        deliver_secret(
            ladon,
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
    ladon = FakeLadon(sync_error="the cluster refused it")

    with caplog.at_level("INFO", logger="kobo_api.ladon"), pytest.raises(ClientError):
        deliver_secret(
            ladon,
            acting_email=EMAIL,
            agent_name=AGENT,
            purpose="cabal",
            values={"K": SECRET_VALUE},
            secret_name=f"{AGENT}-cabal",
        )

    assert caplog.records == []


def test_deliver_secret_trims_the_acting_email():
    ladon = FakeLadon()

    deliver_secret(
        ladon,
        acting_email=f"  {EMAIL} ",
        agent_name=AGENT,
        purpose="slack",
        values={"K": SECRET_VALUE},
        secret_name=f"{AGENT}-slack",
    )

    assert ladon.puts[0][0] == EMAIL
    assert ladon.syncs[0][0] == EMAIL
