"""Agent Jira read broker (2026-09-04, AREQ lane): the PR broker's custody
model applied to tracker reads.

Route tests drive the same `create_app` client every other route test uses;
the tracker calls and the cabal JWT verification are seamed out (the
module-level fetch/search names monkeypatched on main, `verify_agent_token`
the same way the PR broker's tests do) so nothing here touches a network.
The unit half drives `kobo_api.jira_broker` through a fake sync client and
pins the walls that make the lane honest: project pinning before any
network call, server-side JQL assembly from bounded inputs, ADF flattening,
and comment-read refusals failing loudly instead of reading as "none".
"""

import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kobo_api.clients import ClientError
from kobo_api.config import Config
from kobo_api.jira_broker import _adf_text, fetch_issue, search_issues
from kobo_api.main import create_app
from kobo_api.pr_broker import BrokerAuthError


@pytest.fixture
def jira_config(tmp_path):
    pat = tmp_path / "jira-pat"
    pat.write_text("not-a-real-token\n")
    return Config(
        workdir=tmp_path / "work",
        cabal_url="http://cabal.test",
        jira_domain="tracker.test",
        jira_email="reader@acme.io",
        jira_pat_path=pat,
        jira_projects=["AREQ"],
        jira_read_agents=["ledger"],
    )


@pytest.fixture
def jira_client(session_factory, jira_config):
    return TestClient(create_app(session_factory, config=jira_config))


def _authed(monkeypatch):
    import kobo_api.main as main_mod

    monkeypatch.setattr(main_mod, "verify_agent_token", lambda *a, **k: None)
    return main_mod


# --- feature gate -------------------------------------------------------------


def test_routes_answer_503_when_jira_broker_unconfigured(client):
    assert client.get("/broker/agents/x/jira/issues/AREQ-1").status_code == 503
    assert client.get("/broker/agents/x/jira/search", params={"text": "y"}).status_code == 503


def test_routes_answer_503_without_the_switchboard(session_factory, jira_config):
    config = jira_config.model_copy(update={"cabal_url": None})
    local = TestClient(create_app(session_factory, config=config))
    r = local.get("/broker/agents/ledger/jira/issues/AREQ-1")
    assert r.status_code == 503
    assert "switchboard" in r.json()["error"]


# --- the agent walls ----------------------------------------------------------


def test_missing_bearer_is_401(jira_client):
    assert jira_client.get("/broker/agents/ledger/jira/issues/AREQ-1").status_code == 401


def test_rejected_token_is_401(jira_client, monkeypatch):
    import kobo_api.main as main_mod

    def refuse(*a, **k):
        raise BrokerAuthError("no")

    monkeypatch.setattr(main_mod, "verify_agent_token", refuse)
    r = jira_client.get(
        "/broker/agents/ledger/jira/issues/AREQ-1",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 401


def test_agent_off_the_allowlist_reads_as_404(jira_client, monkeypatch):
    _authed(monkeypatch)
    r = jira_client.get(
        "/broker/agents/intruder/jira/issues/AREQ-1",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 404
    assert "no such agent" in r.json()["error"]


def test_key_outside_pinned_projects_is_refused_before_any_call(
    jira_client, monkeypatch
):
    main_mod = _authed(monkeypatch)

    def explode(**kwargs):
        raise AssertionError("the tracker must not be called for an off-lane key")

    monkeypatch.setattr(main_mod, "jira_fetch_issue", explode)
    for key in ("SECRETPROJ-9", "AREQ"):
        r = jira_client.get(
            f"/broker/agents/ledger/jira/issues/{key}",
            headers={"Authorization": "Bearer x"},
        )
        assert r.status_code == 403
        assert "AREQ" in r.json()["error"]


# --- the read routes ----------------------------------------------------------


def test_issue_route_passes_the_lane_coordinates_and_answers(jira_client, monkeypatch, jira_config):
    main_mod = _authed(monkeypatch)
    seen = {}

    def fake_fetch(**kwargs):
        seen.update(kwargs)
        return {"issue": {"key": "AREQ-42"}, "comments": [], "comments_truncated": False}

    monkeypatch.setattr(main_mod, "jira_fetch_issue", fake_fetch)
    r = jira_client.get(
        "/broker/agents/ledger/jira/issues/AREQ-42",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    assert r.json()["issue"]["key"] == "AREQ-42"
    assert seen["domain"] == "tracker.test"
    assert seen["email"] == "reader@acme.io"
    assert seen["key"] == "AREQ-42"


def test_unknown_issue_reads_as_404(jira_client, monkeypatch):
    main_mod = _authed(monkeypatch)

    def missing(**kwargs):
        raise ClientError("no such issue")

    monkeypatch.setattr(main_mod, "jira_fetch_issue", missing)
    r = jira_client.get(
        "/broker/agents/ledger/jira/issues/AREQ-404",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 404


def test_tracker_failure_reads_as_502(jira_client, monkeypatch):
    main_mod = _authed(monkeypatch)

    def down(**kwargs):
        raise ClientError("failed to reach the tracker")

    monkeypatch.setattr(main_mod, "jira_fetch_issue", down)
    r = jira_client.get(
        "/broker/agents/ledger/jira/issues/AREQ-1",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 502


def test_search_route_pins_the_first_project(jira_client, monkeypatch):
    main_mod = _authed(monkeypatch)
    seen = {}

    def fake_search(**kwargs):
        seen.update(kwargs)
        return {"issues": [], "truncated": False}

    monkeypatch.setattr(main_mod, "jira_search_issues", fake_search)
    r = jira_client.get(
        "/broker/agents/ledger/jira/search",
        params={"text": "jdoe@acme.io", "limit": "5"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    assert seen["project"] == "AREQ"
    assert seen["text"] == "jdoe@acme.io"
    assert seen["limit"] == 5


def test_unsearchable_text_reads_as_400(jira_client, monkeypatch):
    main_mod = _authed(monkeypatch)

    def refuse(**kwargs):
        raise ClientError("nothing searchable in the given text")

    monkeypatch.setattr(main_mod, "jira_search_issues", refuse)
    r = jira_client.get(
        "/broker/agents/ledger/jira/search",
        params={"text": '"'},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 400


# --- jira_broker unit half ----------------------------------------------------


class _Resp:
    def __init__(self, status_code, body=None, text=""):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = text

    def json(self):
        return self._body


class FakeTracker:
    def __init__(self, routes):
        self.routes = routes  # list of (path_fragment, response)
        self.sent = []  # (url, headers, params)

    def get(self, url, headers=None, params=None):
        self.sent.append((url, dict(headers or {}), dict(params or {})))
        for fragment, response in self.routes:
            if fragment in url:
                return response
        raise AssertionError(f"unscripted tracker call: {url}")


def _pat(tmp_path) -> Path:
    pat = tmp_path / "pat"
    pat.write_text("s3cret\n")
    return pat


ADF_COMMENT = {
    "type": "doc",
    "content": [
        {
            "type": "paragraph",
            "content": [
                {"type": "text", "text": "Approved by "},
                {"type": "mention", "attrs": {"text": "@V. President"}},
                {"type": "hardBreak"},
                {"type": "text", "text": "ship it"},
            ],
        }
    ],
}


def test_adf_flattens_text_mentions_breaks_and_unknown_nodes():
    assert _adf_text(ADF_COMMENT) == "Approved by @V. President\nship it\n"
    # An unknown node type contributes its children, never a crash.
    weird = {"type": "futureNode", "content": [{"type": "text", "text": "hi"}]}
    assert _adf_text(weird) == "hi"


def test_fetch_issue_maps_fields_and_comments(tmp_path):
    issue_body = {
        "key": "AREQ-42",
        "fields": {
            "summary": "Access to the payments dashboard",
            "status": {"name": "Done"},
            "issuetype": {"name": "Access Request"},
            "reporter": {"emailAddress": "jdoe@acme.io", "displayName": "J. Doe"},
            "assignee": None,
            "resolution": {"name": "Done"},
            "resolutiondate": "2026-09-01T10:00:00Z",
            "created": "2026-08-30T09:00:00Z",
            "updated": "2026-09-01T10:00:00Z",
            "labels": ["quarterly-audit"],
            "description": {
                "type": "doc",
                "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "Please grant."}]}
                ],
            },
        },
    }
    comments_body = {
        "total": 1,
        "comments": [
            {
                "author": {"displayName": "V. President", "emailAddress": "vp@acme.io"},
                "created": "2026-08-31T12:00:00Z",
                "body": ADF_COMMENT,
            }
        ],
    }
    fake = FakeTracker(
        [("/comment", _Resp(200, comments_body)), ("/issue/AREQ-42", _Resp(200, issue_body))]
    )
    out = fetch_issue(
        domain="tracker.test",
        email="reader@acme.io",
        pat_path=_pat(tmp_path),
        key="AREQ-42",
        http_client=fake,
    )
    assert out["issue"]["summary"] == "Access to the payments dashboard"
    assert out["issue"]["status"] == "Done"
    assert out["issue"]["reporter"] == "jdoe@acme.io"
    assert out["issue"]["description"] == "Please grant."
    assert out["issue"]["url"] == "https://tracker.test/browse/AREQ-42"
    assert out["comments"] == [
        {
            "author": "V. President",
            "author_email": "vp@acme.io",
            "created": "2026-08-31T12:00:00Z",
            "body": "Approved by @V. President\nship it",
        }
    ]
    assert out["comments_truncated"] is False
    # Basic auth was assembled from the mounted file, read lazily.
    _, headers, _ = fake.sent[0]
    expected = base64.b64encode(b"reader@acme.io:s3cret").decode()
    assert headers["Authorization"] == f"Basic {expected}"


def test_fetch_issue_404_raises_no_such_issue(tmp_path):
    fake = FakeTracker([("/issue/", _Resp(404))])
    with pytest.raises(ClientError, match="no such issue"):
        fetch_issue(
            domain="t", email="e", pat_path=_pat(tmp_path), key="AREQ-9", http_client=fake
        )


def test_fetch_issue_comment_refusal_fails_loudly_not_as_empty(tmp_path):
    fake = FakeTracker(
        [("/comment", _Resp(403)), ("/issue/", _Resp(200, {"key": "AREQ-1", "fields": {}}))]
    )
    with pytest.raises(ClientError, match="comment read"):
        fetch_issue(
            domain="t", email="e", pat_path=_pat(tmp_path), key="AREQ-1", http_client=fake
        )


def test_search_assembles_jql_server_side_from_bounded_inputs(tmp_path):
    fake = FakeTracker(
        [("/search/jql", _Resp(200, {"issues": [], "nextPageToken": "more"}))]
    )
    out = search_issues(
        domain="tracker.test",
        email="reader@acme.io",
        pat_path=_pat(tmp_path),
        project="AREQ",
        text='jdoe" OR project = SECRET \\ ',
        limit=999,
        http_client=fake,
    )
    _, _, params = fake.sent[0]
    # The quoting-escape characters are stripped; the project pin and the
    # template come from here, never the caller.
    assert params["jql"] == (
        'project = "AREQ" AND text ~ "jdoe  OR project = SECRET" ORDER BY created DESC'
    )
    assert params["maxResults"] == 25  # capped
    assert out["truncated"] is True


def test_search_with_nothing_searchable_raises(tmp_path):
    with pytest.raises(ClientError, match="nothing searchable"):
        search_issues(
            domain="t",
            email="e",
            pat_path=_pat(tmp_path),
            project="AREQ",
            text='"\\',
            limit=5,
            http_client=FakeTracker([]),
        )


# --- migrated-key aliases (IE -> AREQ move, 2026-09-04) -----------------------


@pytest.fixture
def alias_client(session_factory, jira_config):
    config = jira_config.model_copy(update={"jira_alias_projects": ["IE"]})
    return TestClient(create_app(session_factory, config=config))


def test_alias_key_resolving_into_the_pin_is_returned(alias_client, monkeypatch):
    main_mod = _authed(monkeypatch)

    def fake_fetch(**kwargs):
        assert kwargs["key"] == "IE-5578"  # the tracker is asked by the OLD key
        return {"issue": {"key": "AREQ-1693"}, "comments": [], "comments_truncated": False}

    monkeypatch.setattr(main_mod, "jira_fetch_issue", fake_fetch)
    r = alias_client.get(
        "/broker/agents/ledger/jira/issues/IE-5578",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    assert r.json()["issue"]["key"] == "AREQ-1693"


def test_alias_key_resolving_elsewhere_is_refused_after_the_fetch(
    alias_client, monkeypatch
):
    main_mod = _authed(monkeypatch)

    def fake_fetch(**kwargs):
        return {"issue": {"key": "SECRETPROJ-9"}, "comments": [], "comments_truncated": False}

    monkeypatch.setattr(main_mod, "jira_fetch_issue", fake_fetch)
    r = alias_client.get(
        "/broker/agents/ledger/jira/issues/IE-1",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 403
    assert "AREQ" in r.json()["error"]


def test_alias_lane_off_by_default_keeps_the_pre_check(jira_client, monkeypatch):
    main_mod = _authed(monkeypatch)

    def explode(**kwargs):
        raise AssertionError("no alias configured: the tracker must not be called")

    monkeypatch.setattr(main_mod, "jira_fetch_issue", explode)
    r = jira_client.get(
        "/broker/agents/ledger/jira/issues/IE-5578",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 403


# --- created-date windows (audit-log-first workflow, 2026-09-04) --------------


def test_search_route_passes_the_created_window(jira_client, monkeypatch):
    main_mod = _authed(monkeypatch)
    seen = {}

    def fake_search(**kwargs):
        seen.update(kwargs)
        return {"issues": [], "truncated": False}

    monkeypatch.setattr(main_mod, "jira_search_issues", fake_search)
    r = jira_client.get(
        "/broker/agents/ledger/jira/search",
        params={
            "text": "jdoe@acme.io",
            "created_after": "2026-07-03",
            "created_before": "2026-07-17",
        },
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    assert seen["created_after"] == "2026-07-03"
    assert seen["created_before"] == "2026-07-17"


def test_search_route_rejects_a_malformed_date(jira_client, monkeypatch):
    main_mod = _authed(monkeypatch)

    def refuse(**kwargs):
        raise ClientError("created_after must be a bare date like 2026-07-10")

    monkeypatch.setattr(main_mod, "jira_search_issues", refuse)
    r = jira_client.get(
        "/broker/agents/ledger/jira/search",
        params={"text": "jdoe@acme.io", "created_after": "last july"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 400
    assert "bare date" in r.json()["error"]


def test_search_jql_carries_the_window_and_stays_server_assembled(tmp_path):
    fake = FakeTracker([("/search/jql", _Resp(200, {"issues": []}))])
    search_issues(
        domain="tracker.test",
        email="reader@acme.io",
        pat_path=_pat(tmp_path),
        project="AREQ",
        text="jdoe@acme.io",
        limit=10,
        created_after="2026-07-03",
        created_before="2026-07-17",
        http_client=fake,
    )
    _, _, params = fake.sent[0]
    assert params["jql"] == (
        'project = "AREQ" AND text ~ "jdoe@acme.io" '
        'AND created >= "2026-07-03" AND created <= "2026-07-17" '
        "ORDER BY created DESC"
    )


def test_search_refuses_a_date_that_could_carry_jql(tmp_path):
    with pytest.raises(ClientError, match="bare date"):
        search_issues(
            domain="t",
            email="e",
            pat_path=_pat(tmp_path),
            project="AREQ",
            text="jdoe@acme.io",
            limit=5,
            created_after='2026-07-03" OR project = "SECRET',
            http_client=FakeTracker([]),
        )
