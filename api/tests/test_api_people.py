"""Tests for `GET /api/people/search` (access-tab refinement wave, people
search): a signed-in-only proxy of the directory's own user search
(kobo_api.groups.OktaPeopleSearch) -- no owner/operator gate beyond the
app-wide IAP middleware every `/api/*` route already carries, empty `q` ->
422, a directory failure (or the search not being configured at all) ->
fail-soft `{"people": [], "unavailable": true}`, never a 5xx, and the Okta
token never appearing anywhere in a response.

Same harness shape as test_api_directory.py: own `session_factory`/`config`
fixtures, a `TestClient` built from `create_app`.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.clients import Clients
from kobo_api.config import Config
from kobo_api.groups import OktaPeopleSearch
from kobo_api.main import create_app
from kobo_api.models import Base
from tests.fakes import (
    FakeAgent,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
    FakePeopleSearch,
)


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


def _make_client(session_factory, config, *, people_search=None):
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
                people_search=people_search,
            ),
        )
    )


# --- Happy path ----------------------------------------------------------------


def test_search_happy_path_returns_capped_people_shape(session_factory, config):
    fake = FakePeopleSearch(result=[{"name": "Alice Smith", "email": "alice@example.com"}])
    client = _make_client(session_factory, config, people_search=fake)

    response = client.get("/api/people/search", params={"q": "ali"})

    assert response.status_code == 200
    assert response.json() == {
        "people": [{"name": "Alice Smith", "email": "alice@example.com"}]
    }
    assert fake.search_calls == ["ali"]


def test_search_no_matches_is_a_plain_empty_list_not_unavailable(session_factory, config):
    fake = FakePeopleSearch(result=[])
    client = _make_client(session_factory, config, people_search=fake)

    response = client.get("/api/people/search", params={"q": "zzz-nobody"})

    assert response.status_code == 200
    assert response.json() == {"people": []}
    assert "unavailable" not in response.json()


# --- Empty/missing q -> 422 ------------------------------------------------------


def test_search_empty_q_is_422(session_factory, config):
    fake = FakePeopleSearch()
    client = _make_client(session_factory, config, people_search=fake)

    response = client.get("/api/people/search", params={"q": ""})

    assert response.status_code == 422
    assert fake.search_calls == []


def test_search_missing_q_is_422(session_factory, config):
    fake = FakePeopleSearch()
    client = _make_client(session_factory, config, people_search=fake)

    response = client.get("/api/people/search")

    assert response.status_code == 422
    assert fake.search_calls == []


# --- Fail-soft: directory failure and "not configured" both read the same ------


def test_search_directory_failure_is_fail_soft_not_5xx(session_factory, config):
    fake = FakePeopleSearch(fails=True)
    client = _make_client(session_factory, config, people_search=fake)

    response = client.get("/api/people/search", params={"q": "ali"})

    assert response.status_code == 200
    assert response.json() == {"people": [], "unavailable": True}


def test_search_not_configured_is_also_fail_soft(session_factory, config):
    # No people_search wired at all (a room with no Okta token/domain) --
    # the same fail-soft shape as a live directory failure, never a 503.
    client = _make_client(session_factory, config, people_search=None)

    response = client.get("/api/people/search", params={"q": "ali"})

    assert response.status_code == 200
    assert response.json() == {"people": [], "unavailable": True}


# --- Token never leaks (real OktaPeopleSearch behind httpx.MockTransport) ------


def test_okta_token_never_appears_in_the_response_on_failure(session_factory, config):
    secret = "ultra-secret-okta-token"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f"token was {secret}")

    real_search = OktaPeopleSearch(
        "acme.okta.com", secret, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    client = _make_client(session_factory, config, people_search=real_search)

    response = client.get("/api/people/search", params={"q": "ali"})

    assert response.status_code == 200
    assert response.json() == {"people": [], "unavailable": True}
    assert secret not in response.text
    assert secret not in str(response.headers)
