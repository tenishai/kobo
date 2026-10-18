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

from kobo_api.groups import OktaGroupResolver, OktaPeopleSearch

DOMAIN = "acme.okta.com"
TOKEN = "SUPER_SECRET_OKTA_TOKEN_9f3a"
UNIVERSE = {"SRE": ["okta-sre-group"], "QA": ["okta-qa-group"]}


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


def test_groups_for_returns_stable_response_order_across_two_calls():
    """The dedup in `_fetch` must yield the RESPONSE's own order (first
    sight in the JSON list), not a `set`'s hash-seed order -- two different
    emails (separate cache entries, so both actually re-fetch) against the
    exact same response, which lists the QA okta group before the SRE one,
    must both come back QA-then-SRE, not just agree with each other by
    coincidence of a stable hash seed within one process."""
    response = httpx.Response(
        200,
        json=[_okta_group("okta-qa-group"), _okta_group("okta-sre-group")],
    )
    resolver = OktaGroupResolver(DOMAIN, TOKEN, UNIVERSE, client=_client(lambda r: response))

    first = resolver.groups_for("alice@example.com")
    second = resolver.groups_for("bob@example.com")

    assert first == ["QA", "SRE"]
    assert second == ["QA", "SRE"]


# --- Multi-okta groups: one config name -> several regional okta groups ------
# The directory splits one human team's membership across several regional
# groups rather than one group per team -- a room group's universe entry is
# therefore a LIST of okta names, all backing the same config name.

TEAM_UNIVERSE = {
    "infra-engineering": [
        "engineering_operations_infra-engineering_usa",
        "engineering_operations_infra-engineering_india",
        "engineering_operations_infra-engineering_france",
        "engineering_operations_infra-engineering_lead",
    ],
}


def test_membership_in_one_regional_okta_group_yields_the_config_name():
    """A member of ONLY the india regional group is still a member of the
    room's config-level group -- membership is a union across the list, not
    a match against one specific entry."""
    response = httpx.Response(
        200, json=[_okta_group("engineering_operations_infra-engineering_india")]
    )
    resolver = OktaGroupResolver(
        DOMAIN, TOKEN, TEAM_UNIVERSE, client=_client(lambda r: response)
    )

    result = resolver.groups_for("alice@example.com")

    assert result == ["infra-engineering"]


def test_membership_in_two_regional_okta_groups_dedupes_to_one_config_name():
    """A member of both the usa AND lead regional groups (a team lead based
    in the US, say) must get the config name back exactly ONCE, not once per
    matching okta group."""
    response = httpx.Response(
        200,
        json=[
            _okta_group("engineering_operations_infra-engineering_usa"),
            _okta_group("engineering_operations_infra-engineering_lead"),
        ],
    )
    resolver = OktaGroupResolver(
        DOMAIN, TOKEN, TEAM_UNIVERSE, client=_client(lambda r: response)
    )

    result = resolver.groups_for("alice@example.com")

    assert result == ["infra-engineering"]


def test_cross_config_name_okta_collision_rejected_at_construction():
    """The same okta group name claimed by two DIFFERENT config names is an
    ambiguous mapping the old dict-keyed-by-config-name shape could
    silently produce (last write wins); inverted construction must reject
    it outright instead."""
    universe = {
        "SRE": ["okta-shared-group"],
        "QA": ["okta-shared-group"],
    }

    with pytest.raises(ValueError, match="okta-shared-group"):
        OktaGroupResolver(DOMAIN, TOKEN, universe)


def test_same_config_name_duplicate_okta_names_merge_without_error():
    """The same okta name appearing twice under the SAME config name (a
    literal duplicate in the list) is not a collision -- it's a no-op."""
    universe = {"SRE": ["okta-sre-group", "okta-sre-group"]}

    resolver = OktaGroupResolver(DOMAIN, TOKEN, universe)

    assert resolver._universe == {"okta-sre-group": "SRE"}


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


def test_call_after_failure_ttl_expiry_retries_the_directory():
    """A directory outage must not be replayed forever -- once the short
    failure floor (failure_ttl_s) elapses, the next call tries the network
    again rather than staying fail-closed until the much longer success TTL
    would have expired."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, text="internal error")
        return httpx.Response(200, json=[_okta_group("okta-sre-group")])

    clock = {"now": 1_000.0}
    resolver = OktaGroupResolver(
        DOMAIN, TOKEN, UNIVERSE, ttl_s=900, failure_ttl_s=60,
        client=_client(handler), now=lambda: clock["now"],
    )

    first = resolver.groups_for("alice@example.com")
    assert first is None
    assert calls["n"] == 1

    clock["now"] += 61  # just past the 60s failure floor
    second = resolver.groups_for("alice@example.com")
    assert second == ["SRE"]
    assert calls["n"] == 2


# --- Negative-cache floor: a flapping directory isn't hammered -----------------


def test_second_call_within_failure_ttl_makes_no_http_call_and_returns_none():
    """A flapping/slow directory must not make every group-needing request
    pay the full HTTP timeout for the whole outage -- within the short
    failure floor, groups_for answers None straight from memory, no HTTP
    call at all."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, text="internal error")

    clock = {"now": 1_000.0}
    resolver = OktaGroupResolver(
        DOMAIN, TOKEN, UNIVERSE, failure_ttl_s=60,
        client=_client(handler), now=lambda: clock["now"],
    )

    first = resolver.groups_for("alice@example.com")
    assert first is None
    assert calls["n"] == 1

    clock["now"] += 59  # still within the 60s failure floor
    second = resolver.groups_for("alice@example.com")
    assert second is None
    assert calls["n"] == 1  # no new HTTP call


def test_fresh_success_is_never_shadowed_by_a_later_failure():
    """A cached success that's still within its TTL always wins -- no fetch
    happens at all while it's fresh, so a later failure can never evict or
    shadow it."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json=[_okta_group("okta-sre-group")])
        return httpx.Response(500, text="internal error")

    clock = {"now": 1_000.0}
    resolver = OktaGroupResolver(
        DOMAIN, TOKEN, UNIVERSE, ttl_s=900,
        client=_client(handler), now=lambda: clock["now"],
    )

    first = resolver.groups_for("alice@example.com")
    assert first == ["SRE"]
    assert calls["n"] == 1

    clock["now"] += 100  # well within the 900s success TTL
    second = resolver.groups_for("alice@example.com")
    assert second == ["SRE"]
    assert calls["n"] == 1  # still no new HTTP call


def test_success_ttl_expiry_then_failed_refetch_returns_none_not_the_stale_success():
    """Honest semantics: once the success TTL is up, a stale cached answer is
    never served just because the refetch that would have replaced it
    failed -- fail-closed applies here too."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json=[_okta_group("okta-sre-group")])
        return httpx.Response(500, text="internal error")

    clock = {"now": 1_000.0}
    resolver = OktaGroupResolver(
        DOMAIN, TOKEN, UNIVERSE, ttl_s=900,
        client=_client(handler), now=lambda: clock["now"],
    )

    first = resolver.groups_for("alice@example.com")
    assert first == ["SRE"]
    assert calls["n"] == 1

    clock["now"] += 901  # success TTL now expired
    second = resolver.groups_for("alice@example.com")
    assert second is None  # NOT ["SRE"] -- stale success isn't served past its TTL
    assert calls["n"] == 2


# --- Email case normalization: one identity regardless of case ----------------


def test_email_case_is_normalized_to_one_cache_entry_and_lowercased_url():
    """Okta identities are case-insensitive; "User@Example.com" and
    "user@example.com" must resolve to one cache entry (one HTTP call), and
    the URL path Okta actually receives must carry the lowercased form."""
    captured: list[httpx.Request] = []
    response = httpx.Response(200, json=[_okta_group("okta-sre-group")])
    resolver = OktaGroupResolver(
        DOMAIN, TOKEN, UNIVERSE, client=_client(_recording_transport(captured, response))
    )

    first = resolver.groups_for("User@Example.com")
    second = resolver.groups_for("user@example.com")

    assert first == ["SRE"]
    assert second == ["SRE"]
    assert len(captured) == 1
    assert captured[0].url.path == "/api/v1/users/user@example.com/groups"


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


# --- OktaPeopleSearch: the directory's own user typeahead ----------------------
#
# Access-tab refinement wave (people search): a sibling to OktaGroupResolver
# above, sharing the exact same client/token/fail-soft conventions (an
# httpx.Client test seam, `Authorization: SSWS {token}`, every failure mode
# caught and logged without the token ever appearing, never raises) but
# answering a different question -- "which real people match this text" --
# so it is its own class rather than a method bolted onto a resolver whose
# whole shape (a `universe` of CONFIGURED groups) has nothing to do with a
# free-text person search.


def _okta_user(first: str | None, last: str | None, email: str | None) -> dict:
    """One Okta user object shaped like the real API's response -- profile
    fields at `profile.*`, plus a couple of fields a real user object
    carries that the search must simply ignore (login, status)."""
    profile: dict = {}
    if first is not None:
        profile["firstName"] = first
    if last is not None:
        profile["lastName"] = last
    if email is not None:
        profile["email"] = email
        profile["login"] = email
    return {"id": "00u1", "status": "ACTIVE", "profile": profile}


# --- Protocol/construction shape -----------------------------------------------


# --- Happy path: request shape, response shaping -------------------------------


def test_search_sends_correct_url_query_and_ssws_auth_header():
    captured: list[httpx.Request] = []
    response = httpx.Response(200, json=[_okta_user("Alice", "Smith", "alice@example.com")])
    searcher = OktaPeopleSearch(
        DOMAIN, TOKEN, client=_client(_recording_transport(captured, response))
    )

    searcher.search("ali")

    assert len(captured) == 1
    request = captured[0]
    assert request.method == "GET"
    assert request.url.host == DOMAIN
    assert request.url.path == "/api/v1/users"
    assert request.url.params["q"] == "ali"
    assert request.url.params["limit"] == "10"
    assert request.headers["authorization"] == f"SSWS {TOKEN}"


def test_search_caps_result_to_name_and_email_only():
    response = httpx.Response(200, json=[_okta_user("Alice", "Smith", "alice@example.com")])
    searcher = OktaPeopleSearch(DOMAIN, TOKEN, client=_client(lambda r: response))

    result = searcher.search("ali")

    assert result == [{"name": "Alice Smith", "email": "alice@example.com"}]


def test_search_returns_multiple_people_in_response_order():
    response = httpx.Response(
        200,
        json=[
            _okta_user("Alice", "Smith", "alice@example.com"),
            _okta_user("Bob", "Jones", "bob@example.com"),
        ],
    )
    searcher = OktaPeopleSearch(DOMAIN, TOKEN, client=_client(lambda r: response))

    result = searcher.search("a")

    assert result == [
        {"name": "Alice Smith", "email": "alice@example.com"},
        {"name": "Bob Jones", "email": "bob@example.com"},
    ]


def test_search_missing_first_or_last_name_still_yields_a_trimmed_name():
    response = httpx.Response(
        200,
        json=[
            _okta_user(None, "Smith", "alice@example.com"),
            _okta_user("Bob", None, "bob@example.com"),
        ],
    )
    searcher = OktaPeopleSearch(DOMAIN, TOKEN, client=_client(lambda r: response))

    result = searcher.search("a")

    assert result == [
        {"name": "Smith", "email": "alice@example.com"},
        {"name": "Bob", "email": "bob@example.com"},
    ]


def test_search_entry_missing_email_is_dropped():
    response = httpx.Response(
        200,
        json=[
            _okta_user("Alice", "Smith", None),
            _okta_user("Bob", "Jones", "bob@example.com"),
        ],
    )
    searcher = OktaPeopleSearch(DOMAIN, TOKEN, client=_client(lambda r: response))

    result = searcher.search("a")

    assert result == [{"name": "Bob Jones", "email": "bob@example.com"}]


def test_search_no_matches_returns_empty_list_not_none():
    """A genuine "the directory answered, nobody matched" must be `[]`, not
    `None` -- the same distinction OktaGroupResolver draws between a real
    empty answer and an outright failure."""
    response = httpx.Response(200, json=[])
    searcher = OktaPeopleSearch(DOMAIN, TOKEN, client=_client(lambda r: response))

    result = searcher.search("zzz-no-such-person")

    assert result == []
    assert result is not None


# --- Error paths: every failure mode -> None, never raises ---------------------


@pytest.mark.parametrize("status_code", [401, 403, 500])
def test_search_http_error_status_codes_return_none(status_code):
    response = httpx.Response(status_code, text=f"error body for {status_code}")
    searcher = OktaPeopleSearch(DOMAIN, TOKEN, client=_client(lambda r: response))

    assert searcher.search("ali") is None


def test_search_timeout_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    searcher = OktaPeopleSearch(DOMAIN, TOKEN, client=_client(handler))

    assert searcher.search("ali") is None


def test_search_network_error_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    searcher = OktaPeopleSearch(DOMAIN, TOKEN, client=_client(handler))

    assert searcher.search("ali") is None


def test_search_malformed_json_body_returns_none():
    response = httpx.Response(200, text="not valid json{{{")
    searcher = OktaPeopleSearch(DOMAIN, TOKEN, client=_client(lambda r: response))

    assert searcher.search("ali") is None


def test_search_non_list_json_body_returns_none():
    response = httpx.Response(200, json={"not": "a list"})
    searcher = OktaPeopleSearch(DOMAIN, TOKEN, client=_client(lambda r: response))

    assert searcher.search("ali") is None


def test_search_tolerates_entries_missing_a_profile_entirely():
    response = httpx.Response(
        200,
        json=[
            {"id": "00u1"},  # no profile at all
            {"id": "00u2", "profile": "not-a-dict"},  # malformed profile
            _okta_user("Alice", "Smith", "alice@example.com"),
        ],
    )
    searcher = OktaPeopleSearch(DOMAIN, TOKEN, client=_client(lambda r: response))

    result = searcher.search("a")

    assert result == [{"name": "Alice Smith", "email": "alice@example.com"}]


# --- Never raises, and the token never appears in any log record --------------


@pytest.mark.parametrize(
    "handler",
    [_handler_401, _handler_403, _handler_500, _handler_timeout, _handler_network_error],
    ids=["401", "403", "500", "timeout", "network-error"],
)
def test_search_every_error_path_never_raises_and_never_logs_the_token(caplog, handler):
    searcher = OktaPeopleSearch(DOMAIN, TOKEN, client=_client(handler))

    with caplog.at_level(logging.DEBUG, logger="kobo_api.groups"):
        result = searcher.search("ali")

    assert result is None
    assert TOKEN not in caplog.text
    for record in caplog.records:
        assert TOKEN not in record.getMessage()
