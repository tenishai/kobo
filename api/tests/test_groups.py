"""Tests for kobo_api.groups: OktaGroupResolver — the directory-backed
GroupResolver behind the access-control arc's group axis. Every network call
goes through an httpx.MockTransport (no real network); the wall clock is an
injected `now`, mirroring kobo_api.github_app.InstallationTokenProvider's own
test seam.

The fail-closed contract is the load-bearing property this file pins: `None`
means "the resolver failed, grant nothing"; `[]` means "the directory
answered, this email belongs to none of the configured groups" — the two
must never be conflated, and every error path (auth failure, server error,
timeout, network error, malformed body) must land on `None`, never raise,
and never put the Okta token anywhere a log record could carry it.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from kobo_api.groups import GroupResolver, OktaGroupResolver

DOMAIN = "acme.okta.com"
TOKEN = "SUPER_SECRET_OKTA_TOKEN_9f3a"
UNIVERSE = {"SRE": "okta-sre-group", "QA": "okta-qa-group"}


def _okta_group(name: str) -> dict:
    """One Okta group object shaped like the real API's response — display
    name at profile.name, plus a couple of fields a real Okta group object
    carries that the resolver must simply ignore."""
    return {
        "id": f"00g{name}",
        "profile": {"name": name, "description": f"{name} description"},
        "type": "OKTA_GROUP",
    }


def _recording_transport(captured: list, response_or_fn):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if callable(response_or_fn):
            return response_or_fn(request)
        return response_or_fn

    return handler


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# --- Protocol shape -----------------------------------------------------------


def test_okta_group_resolver_satisfies_the_group_resolver_protocol():
    resolver = OktaGroupResolver(DOMAIN, TOKEN, UNIVERSE)
    assert isinstance(resolver, GroupResolver)


# --- Happy path: request shape, filtering, translation ------------------------


def test_groups_for_sends_correct_url_and_ssws_auth_header():
    captured: list[httpx.Request] = []
    response = httpx.Response(200, json=[_okta_group("okta-sre-group")])
    resolver = OktaGroupResolver(
        DOMAIN, TOKEN, UNIVERSE, client=_client(_recording_transport(captured, response))
    )

    resolver.groups_for("alice@example.com")

    assert len(captured) == 1
    request = captured[0]
    assert request.method == "GET"
    assert request.url.host == DOMAIN
    # The decoded path (httpx's `.path` convenience property) round-trips
    # back to the exact plain email; see the dedicated encoding test below
    # for the raw (percent-encoded-on-the-wire) form.
    assert request.url.path == "/api/v1/users/alice@example.com/groups"
    assert request.headers["authorization"] == f"SSWS {TOKEN}"


def test_groups_for_url_encodes_a_plus_tagged_email_in_the_path():
    """someone+tag@example.com must not break the path — the '+' is
    percent-encoded rather than passed through raw."""
    captured: list[httpx.Request] = []
    response = httpx.Response(200, json=[])
    resolver = OktaGroupResolver(
        DOMAIN, TOKEN, UNIVERSE, client=_client(_recording_transport(captured, response))
    )

    resolver.groups_for("someone+tag@example.com")

    assert len(captured) == 1
    request = captured[0]
    # The wire form (what actually reaches Okta) is percent-encoded: '+' and
    # '@' must not ride along raw, since some servers/proxies treat '+' in a
    # path as meaningful. httpx's decoded `.path` convenience property would
    # hand the '+'/'@' straight back unencoded, so the raw encoded bytes are
    # what this asserts against.
    assert request.url.raw_path == b"/api/v1/users/someone%2Btag%40example.com/groups"
    # And it decodes back to the exact original email — proof the encoding
    # is reversible, not lossy/mangled.
    assert request.url.path == "/api/v1/users/someone+tag@example.com/groups"


def test_groups_for_filters_to_universe_and_returns_config_names():
    response = httpx.Response(
        200,
        json=[_okta_group("okta-sre-group"), _okta_group("okta-qa-group")],
    )
    resolver = OktaGroupResolver(DOMAIN, TOKEN, UNIVERSE, client=_client(lambda r: response))

    result = resolver.groups_for("alice@example.com")

    assert sorted(result) == ["QA", "SRE"]


def test_groups_for_ignores_unknown_okta_groups_not_in_the_universe():
    response = httpx.Response(
        200,
        json=[
            _okta_group("okta-sre-group"),
            _okta_group("some-other-unrelated-okta-group"),
        ],
    )
    resolver = OktaGroupResolver(DOMAIN, TOKEN, UNIVERSE, client=_client(lambda r: response))

    result = resolver.groups_for("alice@example.com")

    assert result == ["SRE"]


def test_groups_for_returns_empty_list_when_no_universe_group_matches():
    response = httpx.Response(200, json=[_okta_group("totally-unrelated-group")])
    resolver = OktaGroupResolver(DOMAIN, TOKEN, UNIVERSE, client=_client(lambda r: response))

    result = resolver.groups_for("alice@example.com")

    assert result == []


def test_groups_for_tolerates_group_objects_missing_a_profile_or_name():
    response = httpx.Response(
        200,
        json=[
            {"id": "00g1"},  # no profile at all
            {"id": "00g2", "profile": {}},  # profile with no name
            {"id": "00g3", "profile": "not-a-dict"},  # malformed profile
            _okta_group("okta-sre-group"),
        ],
    )
    resolver = OktaGroupResolver(DOMAIN, TOKEN, UNIVERSE, client=_client(lambda r: response))

    result = resolver.groups_for("alice@example.com")

    assert result == ["SRE"]


# --- [] vs None: distinct outcomes --------------------------------------------


def test_empty_memberships_and_failed_lookup_are_distinguishable():
    """A successful call with zero matching memberships is `[]`; a failed
    call is `None` — these must never be the same value, since callers
    branch on `is None` to decide whether to grant nothing at all."""
    ok_response = httpx.Response(200, json=[])
    ok_resolver = OktaGroupResolver(DOMAIN, TOKEN, UNIVERSE, client=_client(lambda r: ok_response))
    assert ok_resolver.groups_for("alice@example.com") == []

    failing_response = httpx.Response(500, text="internal error")
    failing_resolver = OktaGroupResolver(
        DOMAIN, TOKEN, UNIVERSE, client=_client(lambda r: failing_response)
    )
    result = failing_resolver.groups_for("alice@example.com")
    assert result is None
    assert result != []


# --- Caching: TTL honored via injected now ------------------------------------


def test_second_call_within_ttl_makes_no_http_call():
    captured: list[httpx.Request] = []
    response = httpx.Response(200, json=[_okta_group("okta-sre-group")])
    clock = {"now": 1_000.0}
    resolver = OktaGroupResolver(
        DOMAIN, TOKEN, UNIVERSE, ttl_s=900,
        client=_client(_recording_transport(captured, response)),
        now=lambda: clock["now"],
    )

    first = resolver.groups_for("alice@example.com")
    clock["now"] += 100  # still well within the 900s TTL
    second = resolver.groups_for("alice@example.com")

    assert first == ["SRE"]
    assert second == ["SRE"]
    assert len(captured) == 1


def test_call_after_ttl_expiry_refreshes_from_the_directory():
    captured: list[httpx.Request] = []
    responses = [
        httpx.Response(200, json=[_okta_group("okta-sre-group")]),
        httpx.Response(200, json=[_okta_group("okta-qa-group")]),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        idx = min(len(captured), len(responses) - 1)
        captured.append(request)
        return responses[idx]

    clock = {"now": 1_000.0}
    resolver = OktaGroupResolver(
        DOMAIN, TOKEN, UNIVERSE, ttl_s=900,
        client=_client(handler),
        now=lambda: clock["now"],
    )

    first = resolver.groups_for("alice@example.com")
    assert first == ["SRE"]
    assert len(captured) == 1

    clock["now"] += 901  # just past the TTL
    second = resolver.groups_for("alice@example.com")
    assert second == ["QA"]
    assert len(captured) == 2


def test_cache_is_keyed_per_email_independently():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if "alice" in str(request.url):
            return httpx.Response(200, json=[_okta_group("okta-sre-group")])
        return httpx.Response(200, json=[_okta_group("okta-qa-group")])

    resolver = OktaGroupResolver(DOMAIN, TOKEN, UNIVERSE, client=_client(handler))

    assert resolver.groups_for("alice@example.com") == ["SRE"]
    assert resolver.groups_for("bob@example.com") == ["QA"]
    assert resolver.groups_for("alice@example.com") == ["SRE"]
    # Two distinct emails -> two HTTP calls; the repeat alice call is cached.
    assert len(captured) == 2


def test_failed_lookup_is_not_cached_next_call_retries():
    """A directory outage must not be replayed for the rest of the TTL
    window once Okta recovers — a failure is never written into the cache,
    so the very next call tries the network again."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, text="internal error")
        return httpx.Response(200, json=[_okta_group("okta-sre-group")])

    clock = {"now": 1_000.0}
    resolver = OktaGroupResolver(
        DOMAIN, TOKEN, UNIVERSE, ttl_s=900, client=_client(handler), now=lambda: clock["now"]
    )

    first = resolver.groups_for("alice@example.com")
    assert first is None
    assert calls["n"] == 1

    # No time has passed at all -- if the failure had been cached, this
    # would still return None without a second HTTP call.
    second = resolver.groups_for("alice@example.com")
    assert second == ["SRE"]
    assert calls["n"] == 2


# --- Error paths: every failure mode -> None, never raises --------------------


@pytest.mark.parametrize("status_code", [401, 403, 500])
def test_http_error_status_codes_return_none(status_code):
    response = httpx.Response(status_code, text=f"error body for {status_code}")
    resolver = OktaGroupResolver(DOMAIN, TOKEN, UNIVERSE, client=_client(lambda r: response))

    assert resolver.groups_for("alice@example.com") is None


def test_timeout_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    resolver = OktaGroupResolver(DOMAIN, TOKEN, UNIVERSE, client=_client(handler))

    assert resolver.groups_for("alice@example.com") is None


def test_network_error_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    resolver = OktaGroupResolver(DOMAIN, TOKEN, UNIVERSE, client=_client(handler))

    assert resolver.groups_for("alice@example.com") is None


def test_malformed_json_body_returns_none():
    response = httpx.Response(200, text="not valid json{{{")
    resolver = OktaGroupResolver(DOMAIN, TOKEN, UNIVERSE, client=_client(lambda r: response))

    assert resolver.groups_for("alice@example.com") is None


def test_non_list_json_body_returns_none():
    response = httpx.Response(200, json={"not": "a list"})
    resolver = OktaGroupResolver(DOMAIN, TOKEN, UNIVERSE, client=_client(lambda r: response))

    assert resolver.groups_for("alice@example.com") is None


# --- Never raises, and the token never appears in any log record -------------


def _handler_401(request: httpx.Request) -> httpx.Response:
    return httpx.Response(401, text="unauthorized")


def _handler_403(request: httpx.Request) -> httpx.Response:
    return httpx.Response(403, text="forbidden")


def _handler_500(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, text="internal error")


def _handler_timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.TimeoutException("timed out", request=request)


def _handler_network_error(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


@pytest.mark.parametrize(
    "handler",
    [_handler_401, _handler_403, _handler_500, _handler_timeout, _handler_network_error],
    ids=["401", "403", "500", "timeout", "network-error"],
)
def test_every_error_path_never_raises_and_never_logs_the_token(caplog, handler):
    resolver = OktaGroupResolver(DOMAIN, TOKEN, UNIVERSE, client=_client(handler))

    with caplog.at_level(logging.DEBUG, logger="kobo_api.groups"):
        result = resolver.groups_for("alice@example.com")

    assert result is None
    assert TOKEN not in caplog.text
    for record in caplog.records:
        assert TOKEN not in record.getMessage()
