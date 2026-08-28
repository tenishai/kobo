"""Route-level tests for the FTS catalog endpoints (Phase A Task 3):
GET /api/skills/search, GET /api/personas/search, GET /api/catalog/teams, and
POST /api/catalogs/refresh — exercised through a full TestClient/create_app
stack with `FakeStorage` standing in for GCS, same harness shape as
test_api_agents.py's own fixtures.

Also covers `GET /api/teams` (persona-metadata plan Task 1): the skill-only,
owner-derived counterpart to `/api/catalog/teams` above.
"""

from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kobo_api.clients import Clients, ClientError
from kobo_api.config import Config
from kobo_api.main import _maybe_refresh_catalogs, create_app
from kobo_api.models import Base
from kobo_api.search import CATALOG_TTL_S
from tests.fakes import (
    FakeAgent,
    FakeCloud,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
    FakeStorage,
)


def _index_bytes(entries: list[dict]) -> bytes:
    return json.dumps({"generated_at": "2026-08-03T00:00:00Z", "entries": entries}).encode()


SKILL_ENTRIES = [
    {
        "id": "code-review",
        "name": "Code review",
        "description": "Reviews pull requests",
        "team": "shared",
        "tags": ["review", "quality"],
        "sha": "abc123",
        "builtin": False,
    },
    {
        "id": "terraform-check",
        "name": "Terraform check",
        "description": "Validates terraform plans against policy",
        "team": "sre",
        "tags": ["terraform", "infrastructure"],
        "sha": "def456",
        "builtin": False,
    },
    {
        "id": "word-games",
        "name": "Word games",
        "description": "Plays word games with the user",
        "team": "shared",
        "tags": ["fun"],
        "sha": "ghi789",
        "builtin": False,
    },
]

PERSONA_ENTRIES = [
    {
        "id": "helpful-generalist",
        "name": "Helpful Generalist",
        "description": "A friendly all-rounder",
        "team": "shared",
        "tags": [],
        "builtin": True,
    },
]


def _minimal_clients(**kwargs) -> Clients:
    """A Clients with every non-storage field a bare fake — these routes
    never touch github/gojira/cloud/agent/kubectl, but the Clients dataclass
    requires them (no defaults)."""
    return Clients(
        github=FakeGitHub(),
        gojira=FakeGojira(),
        cloud=FakeCloud(),
        agent=FakeAgent(),
        kubectl=FakeKubectl(),
        **kwargs,
    )


def _make_client(config: Config, clients: Clients, tmp_path) -> TestClient:
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    return TestClient(create_app(session_factory, config=config, clients=clients))


def _make_app_and_client(config: Config, clients: Clients, tmp_path):
    """Same wiring as `_make_client` above, but also hands back the `FastAPI`
    app itself -- the stale-while-revalidate tests below need `app.state`
    directly (to force staleness, poll `catalog_refreshing`, ...), which a
    bare `TestClient` doesn't expose."""
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    app = create_app(session_factory, config=config, clients=clients)
    return app, TestClient(app)


def _wait_until_refresh_settles(app, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and app.state.catalog_refreshing:
        time.sleep(0.01)


@pytest.fixture
def fake_storage():
    return FakeStorage(
        buckets={
            "skills-bucket": {"index.json": (_index_bytes(SKILL_ENTRIES), {})},
            "personas-bucket": {"index.json": (_index_bytes(PERSONA_ENTRIES), {})},
        }
    )


@pytest.fixture
def config(tmp_path):
    return Config(
        workdir=tmp_path / "work",
        skills_bucket="skills-bucket",
        personas_bucket="personas-bucket",
    )


@pytest.fixture
def clients(fake_storage):
    return _minimal_clients(storage=fake_storage)


@pytest.fixture
def client(config, clients, tmp_path):
    return _make_client(config, clients, tmp_path)


def test_search_skills_returns_entries(client, fake_storage):
    response = client.get("/api/skills/search", params={"q": "revi"})

    assert response.status_code == 200
    assert response.json() == {
        "results": [
            {
                "id": "code-review",
                "name": "Code review",
                "description": "Reviews pull requests",
                "team": "shared",
                "tags": ["review", "quality"],
                "sha": "abc123",
            }
        ]
    }
    # getting anything back at all means the TTL hook had to ingest first —
    # nothing pre-seeds the table.
    assert "read_bytes:skills-bucket:index.json" in fake_storage.calls


def test_search_skills_team_filter(client):
    response = client.get("/api/skills/search", params={"q": "", "team": "platform"})

    assert response.status_code == 200
    assert response.json() == {"results": []}


def test_search_personas_returns_entries_with_builtin_not_sha(client):
    response = client.get("/api/personas/search", params={"q": ""})

    assert response.status_code == 200
    body = response.json()
    assert body["results"] == [
        {
            "id": "helpful-generalist",
            "name": "Helpful Generalist",
            "description": "A friendly all-rounder",
            "team": "shared",
            "tags": [],
            "builtin": True,
        }
    ]
    assert "sha" not in body["results"][0]


def test_catalog_teams_route(client):
    response = client.get("/api/catalog/teams")

    assert response.status_code == 200
    assert response.json() == {"teams": ["shared", "sre"]}


def test_refresh_route_returns_counts(client):
    response = client.post("/api/catalogs/refresh")

    assert response.status_code == 200
    # HTTP response shape stays the plural skills/personas keys even though
    # ingest_indexes itself now returns kind-keyed counts internally (Task 3
    # fix round 1 extensibility refactor) — the route translates.
    assert response.json() == {"skills": 3, "personas": 1}


def test_ttl_refresh_runs_once_for_two_immediate_calls(client, fake_storage):
    client.get("/api/catalog/teams")
    client.get("/api/catalog/teams")

    assert fake_storage.calls.count("read_bytes:skills-bucket:index.json") == 1
    assert fake_storage.calls.count("read_bytes:personas-bucket:index.json") == 1


def test_ttl_lock_serializes_concurrent_refreshes(config, tmp_path):
    """White-box concurrency check that `catalog_lock` — not just the outer
    TTL check the sequential test above already exercises — actually
    serializes two refreshes racing at the same instant: thread 1 is parked
    inside `read_bytes` (holding the lock, mid-ingest) when thread 2 reaches
    `_maybe_refresh_catalogs`; thread 2 must block on the lock and then find
    the table already fresh, rather than starting a second ingest.
    """
    entered_read = threading.Event()
    release_read = threading.Event()

    class BlockingStorage(FakeStorage):
        def read_bytes(self, bucket, key):
            if bucket == "skills-bucket":
                entered_read.set()
                release_read.wait(timeout=2)
            return super().read_bytes(bucket, key)

    storage = BlockingStorage(
        buckets={
            "skills-bucket": {"index.json": (_index_bytes(SKILL_ENTRIES), {})},
            "personas-bucket": {"index.json": (_index_bytes(PERSONA_ENTRIES), {})},
        }
    )
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    app = create_app(session_factory, config=config, clients=_minimal_clients(storage=storage))

    t1 = threading.Thread(target=_maybe_refresh_catalogs, args=(app, engine))
    t1.start()
    assert entered_read.wait(timeout=2), "thread 1 never reached read_bytes"

    # thread 1 holds catalog_lock right now (mid-ingest, blocked inside
    # read_bytes) — thread 2 must block trying to acquire it.
    t2 = threading.Thread(target=_maybe_refresh_catalogs, args=(app, engine))
    t2.start()

    release_read.set()
    t1.join(timeout=2)
    t2.join(timeout=2)

    assert storage.calls.count("read_bytes:skills-bucket:index.json") == 1
    assert storage.calls.count("read_bytes:personas-bucket:index.json") == 1


# --- Stale-while-revalidate (Task 3 perf fix) --------------------------------
#
# The create modal's search calls were measured at 6+ seconds in the deployed
# pod because a stale hit used to ingest the FULL catalog inline, on the
# request path, holding the caller the entire time. The tests below pin down
# the replacement contract: a stale (but already-loaded) table is served
# as-is immediately, and the actual refresh happens on a background thread,
# single-flighted so N concurrent stale hits still cost exactly one ingest.
# Cold start (never loaded AND the table is genuinely empty) is the one
# carve-out that still pays for an inline ingest -- serving nothing there
# helps nobody -- and stays covered by the plain fixture-backed tests above.


def test_cold_empty_ingest_never_spawns_a_background_thread(config, tmp_path, monkeypatch):
    """The cold-start branch performs its ingest itself, on the calling
    thread -- unlike the stale-while-revalidate branch below, it must never
    hand the work to a background thread."""
    import kobo_api.main as main_module

    spawned: list[threading.Thread] = []
    real_thread = threading.Thread

    def spy_thread(*args, **kwargs):
        t = real_thread(*args, **kwargs)
        spawned.append(t)
        return t

    monkeypatch.setattr(main_module.threading, "Thread", spy_thread)

    storage = FakeStorage(
        buckets={
            "skills-bucket": {"index.json": (_index_bytes(SKILL_ENTRIES), {})},
            "personas-bucket": {"index.json": (_index_bytes(PERSONA_ENTRIES), {})},
        }
    )
    client = _make_client(config, _minimal_clients(storage=storage), tmp_path)

    response = client.get("/api/skills/search", params={"q": ""})

    assert response.status_code == 200
    assert response.json()["results"][0]["id"] == "code-review"
    assert spawned == []


def test_stale_hit_serves_stale_rows_without_inline_ingest(config, tmp_path, monkeypatch):
    """A stale hit must return immediately with whatever's already in the
    table -- never waiting on the refresh it triggers. Proven by pausing the
    background ingest mid-flight (via a paused `ingest_indexes`) and showing
    the response already came back, still carrying the OLD row, before the
    paused ingest is ever allowed to finish."""
    storage = FakeStorage(
        buckets={
            "skills-bucket": {"index.json": (_index_bytes(SKILL_ENTRIES), {})},
            "personas-bucket": {"index.json": (_index_bytes(PERSONA_ENTRIES), {})},
        }
    )
    app, client = _make_app_and_client(config, _minimal_clients(storage=storage), tmp_path)

    # Warm the table (cold path, synchronous -- untouched by this fix).
    warm = client.get("/api/skills/search", params={"q": ""})
    assert warm.json()["results"][0]["id"] == "code-review"

    # Publish a change the request path must NOT see yet, then force
    # staleness directly -- CATALOG_TTL_S is 300s now, far too long to sleep
    # through in a test.
    v2_entry = {**SKILL_ENTRIES[0], "id": "code-review-v2", "name": "Code review v2"}
    storage.write_bytes("skills-bucket", "index.json", _index_bytes([v2_entry]))
    app.state.catalog_loaded_at -= CATALOG_TTL_S + 1

    import kobo_api.main as main_module

    real_ingest = main_module.ingest_indexes
    ingest_started = threading.Event()
    release_ingest = threading.Event()
    calls: list[int] = []

    def paused_ingest(*args, **kwargs):
        calls.append(1)
        ingest_started.set()
        release_ingest.wait(timeout=2)
        return real_ingest(*args, **kwargs)

    monkeypatch.setattr(main_module, "ingest_indexes", paused_ingest)

    response = client.get("/api/skills/search", params={"q": ""})

    # Stale row served, not the new one -- and the response above only
    # returned because the request path never waited on `paused_ingest` at
    # all (it's still parked on `release_ingest.wait`, which this test
    # hasn't set yet).
    assert response.status_code == 200
    assert response.json()["results"][0]["id"] == "code-review"

    assert ingest_started.wait(timeout=2), "the background refresh never started"
    release_ingest.set()
    _wait_until_refresh_settles(app)

    assert len(calls) == 1
    refreshed = client.get("/api/skills/search", params={"q": ""})
    assert refreshed.json()["results"][0]["id"] == "code-review-v2"


def test_stale_refresh_is_single_flight_for_concurrent_hits(config, tmp_path, monkeypatch):
    """N concurrent stale hits must still cost exactly one ingest -- the
    single-flight guard (`catalog_refreshing`, alongside the pre-existing
    `catalog_lock`) collapses them into one background thread."""
    storage = FakeStorage(
        buckets={
            "skills-bucket": {"index.json": (_index_bytes(SKILL_ENTRIES), {})},
            "personas-bucket": {"index.json": (_index_bytes(PERSONA_ENTRIES), {})},
        }
    )
    app, client = _make_app_and_client(config, _minimal_clients(storage=storage), tmp_path)

    client.get("/api/skills/search", params={"q": ""})
    app.state.catalog_loaded_at -= CATALOG_TTL_S + 1

    import kobo_api.main as main_module

    real_ingest = main_module.ingest_indexes
    release_ingest = threading.Event()
    calls: list[int] = []

    def paused_ingest(*args, **kwargs):
        calls.append(1)
        release_ingest.wait(timeout=2)
        return real_ingest(*args, **kwargs)

    monkeypatch.setattr(main_module, "ingest_indexes", paused_ingest)

    callers = [
        threading.Thread(
            target=client.get,
            args=("/api/skills/search",),
            kwargs={"params": {"q": ""}},
        )
        for _ in range(5)
    ]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join(timeout=2)

    release_ingest.set()
    _wait_until_refresh_settles(app)

    assert len(calls) == 1


def test_warm_restart_nonempty_table_with_loaded_at_none_uses_background_path(
    config, tmp_path, monkeypatch
):
    """Coverage gap: a freshly-booted process starts with `catalog_loaded_at
    is None` (nothing ingested in this process yet), but a warm restart can
    inherit an on-disk `catalog_fts` table a PRIOR process already
    populated. `catalog_is_empty(engine)` is what actually tells cold apart
    from warm here -- `catalog_loaded_at is None` alone is not enough --
    so this must take the stale-while-revalidate background path (serve the
    inherited rows immediately, spawn exactly one background thread), NOT
    the cold-start inline-ingest path."""
    import kobo_api.main as main_module

    storage = FakeStorage(
        buckets={
            "skills-bucket": {"index.json": (_index_bytes(SKILL_ENTRIES), {})},
            "personas-bucket": {"index.json": (_index_bytes(PERSONA_ENTRIES), {})},
        }
    )
    engine = create_engine(
        f"sqlite:///{tmp_path}/kobo.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    # Simulate a previous process's completed ingest sitting on disk --
    # done directly against the engine, before `create_app` ever runs, so
    # the new app below starts with `catalog_loaded_at is None` despite the
    # table already holding rows.
    main_module.ingest_indexes(
        engine, storage, {"skill": "skills-bucket", "persona": "personas-bucket"}
    )

    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    app = create_app(session_factory, config=config, clients=_minimal_clients(storage=storage))
    client = TestClient(app)
    assert app.state.catalog_loaded_at is None

    spawned: list[threading.Thread] = []
    real_thread = threading.Thread

    def spy_thread(*args, **kwargs):
        t = real_thread(*args, **kwargs)
        spawned.append(t)
        return t

    monkeypatch.setattr(main_module.threading, "Thread", spy_thread)

    real_ingest = main_module.ingest_indexes
    ingest_calls: list[int] = []

    def counting_ingest(*args, **kwargs):
        ingest_calls.append(1)
        return real_ingest(*args, **kwargs)

    monkeypatch.setattr(main_module, "ingest_indexes", counting_ingest)

    response = client.get("/api/skills/search", params={"q": ""})

    # The inherited rows are served immediately -- nothing on the request
    # path blocked on an inline ingest to get them.
    assert response.status_code == 200
    assert response.json()["results"][0]["id"] == "code-review"
    assert len(spawned) == 1

    _wait_until_refresh_settles(app)
    # Exactly one ingest -- the background refresh -- and it happened off
    # the request path, not inline.
    assert len(ingest_calls) == 1


def test_manual_refresh_returns_409_while_background_refresh_in_flight(
    config, tmp_path, monkeypatch
):
    """Reproduces the reviewer's concurrency regression: `refresh_catalogs`
    (POST /api/catalogs/refresh) used to believe `catalog_lock` excluded
    every ingest, but `_background_refresh_catalogs` only ever ran its
    ingest outside that lock (just the flag flips were locked). A manual
    refresh landing while a background refresh was mid-ingest could open a
    second concurrent `engine.begin()` writer -- past sqlite's busy_timeout
    that raised an uncaught `sqlite3.OperationalError` (a raw 500 on this
    route). The fix gives the ingest itself one true mutual-exclusion lock
    (`catalog_ingest_lock`), and lets this route fast-path to 409 the moment
    it sees `catalog_refreshing` already set -- honesty instead of a
    pile-up.

    Proven here with the reviewer's own repro shape: a paused `ingest_indexes`
    mock, a stale hit that spawns a background refresh parked inside it,
    then a manual refresh during that window. It must come back 409 --
    never a second entry into the mock -- and once the background refresh
    finishes, the manual route works again.
    """
    storage = FakeStorage(
        buckets={
            "skills-bucket": {"index.json": (_index_bytes(SKILL_ENTRIES), {})},
            "personas-bucket": {"index.json": (_index_bytes(PERSONA_ENTRIES), {})},
        }
    )
    app, client = _make_app_and_client(config, _minimal_clients(storage=storage), tmp_path)

    client.get("/api/skills/search", params={"q": ""})  # cold warm-up
    app.state.catalog_loaded_at -= CATALOG_TTL_S + 1  # force staleness

    import kobo_api.main as main_module

    real_ingest = main_module.ingest_indexes
    ingest_started = threading.Event()
    release_ingest = threading.Event()
    calls: list[int] = []

    def paused_ingest(*args, **kwargs):
        calls.append(1)
        ingest_started.set()
        release_ingest.wait(timeout=2)
        return real_ingest(*args, **kwargs)

    monkeypatch.setattr(main_module, "ingest_indexes", paused_ingest)

    # A stale hit spawns the background refresh, which parks mid-ingest.
    client.get("/api/skills/search", params={"q": ""})
    assert ingest_started.wait(timeout=2), "the background refresh never started"

    # A manual refresh landing in that window must not queue behind the
    # in-flight ingest -- it must answer immediately, never becoming a
    # second concurrent entry into ingest_indexes.
    response = client.post("/api/catalogs/refresh")

    assert response.status_code == 409
    assert response.json() == {
        "error": "a refresh is already running. Try again in a moment."
    }
    assert len(calls) == 1  # still just the one in-flight background call

    release_ingest.set()
    _wait_until_refresh_settles(app)

    # Now that the background refresh has finished, the manual route works
    # again -- this is a second, distinct call, not a queued continuation
    # of the one above.
    again = client.post("/api/catalogs/refresh")
    assert again.status_code == 200
    assert len(calls) == 2


def test_background_refresh_failure_keeps_serving_stale(config, tmp_path):
    """A background refresh that fails (a bucket blip) must never disturb
    what's already being served -- the stale table stays exactly as it was,
    same fail-soft posture the old inline path always had, and the TTL
    clock (`catalog_loaded_at`) must not advance on a failed attempt, so the
    very next hit retries rather than believing the table is fresh again."""

    class FailsAfterFirstReadStorage(FakeStorage):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._skill_reads = 0

        def read_bytes(self, bucket, key):
            if bucket == "skills-bucket":
                self._skill_reads += 1
                if self._skill_reads > 1:
                    raise ClientError("bucket is on fire")
            return super().read_bytes(bucket, key)

    storage = FailsAfterFirstReadStorage(
        buckets={
            "skills-bucket": {"index.json": (_index_bytes(SKILL_ENTRIES), {})},
            "personas-bucket": {"index.json": (_index_bytes(PERSONA_ENTRIES), {})},
        }
    )
    app, client = _make_app_and_client(config, _minimal_clients(storage=storage), tmp_path)

    client.get("/api/skills/search", params={"q": ""})  # cold, first read succeeds
    stale_since = app.state.catalog_loaded_at - (CATALOG_TTL_S + 1)
    app.state.catalog_loaded_at = stale_since

    response = client.get("/api/skills/search", params={"q": ""})
    assert response.status_code == 200
    assert response.json()["results"][0]["id"] == "code-review"

    _wait_until_refresh_settles(app)

    assert app.state.catalog_loaded_at == stale_since
    again = client.get("/api/skills/search", params={"q": ""})
    assert again.json()["results"][0]["id"] == "code-review"


def test_ttl_respected_fresh_hit_never_reads_the_bucket_again(client, fake_storage):
    client.get("/api/skills/search", params={"q": ""})
    reads_after_warm = fake_storage.calls.count("read_bytes:skills-bucket:index.json")

    # Still fresh -- a second hit right away must neither ingest inline nor
    # spawn a background refresh.
    response = client.get("/api/skills/search", params={"q": ""})

    assert response.status_code == 200
    assert fake_storage.calls.count("read_bytes:skills-bucket:index.json") == reads_after_warm


def test_search_never_500s_on_malformed_index(config, tmp_path):
    bad_storage = FakeStorage(
        buckets={
            "skills-bucket": {"index.json": (b"not json", {})},
            "personas-bucket": {},
        }
    )
    client = _make_client(config, _minimal_clients(storage=bad_storage), tmp_path)

    response = client.get("/api/skills/search", params={"q": ""})

    # the TTL hook's ingest failed and was swallowed — the (empty) table is
    # served instead of a 500.
    assert response.status_code == 200
    assert response.json() == {"results": []}


def test_refresh_route_503_when_storage_none(config, tmp_path):
    client = _make_client(config, _minimal_clients(), tmp_path)

    response = client.post("/api/catalogs/refresh")

    assert response.status_code == 503
    assert response.json() == {"error": "catalogs aren't set up yet"}


def test_refresh_route_503_when_both_buckets_unset(tmp_path):
    config = Config(workdir=tmp_path / "work")  # skills_bucket/personas_bucket default ""
    client = _make_client(config, _minimal_clients(storage=FakeStorage()), tmp_path)

    response = client.post("/api/catalogs/refresh")

    assert response.status_code == 503
    assert response.json() == {"error": "catalogs aren't set up yet"}


def test_refresh_route_502_on_storage_error(config, tmp_path):
    erroring_storage = FakeStorage(read_error="storage is on fire")
    client = _make_client(config, _minimal_clients(storage=erroring_storage), tmp_path)

    response = client.post("/api/catalogs/refresh")

    assert response.status_code == 502


# --- GET /api/teams (persona-metadata plan Task 1) --------------------------


def test_teams_route_default_fixture_shape(client):
    # Default fixture's one skill entry carries no "owner" — its team falls
    # back to the legacy "team": "shared" key already on it.
    response = client.get("/api/teams")

    assert response.status_code == 200
    assert response.json() == {"teams": ["shared", "sre"], "default": "shared"}


def test_teams_route_reflects_owner_derived_teams_sorted(config, tmp_path):
    storage = FakeStorage(
        buckets={
            "skills-bucket": {
                "index.json": (
                    _index_bytes(
                        [
                            {
                                "id": "s1",
                                "name": "s1",
                                "description": "d",
                                "owner": "growth",
                                "tags": [],
                                "sha": "",
                                "builtin": False,
                            },
                            {
                                "id": "s2",
                                "name": "s2",
                                "description": "d",
                                "owner": "billing",
                                "tags": [],
                                "sha": "",
                                "builtin": False,
                            },
                        ]
                    ),
                    {},
                )
            },
            "personas-bucket": {"index.json": (_index_bytes(PERSONA_ENTRIES), {})},
        }
    )
    client = _make_client(config, _minimal_clients(storage=storage), tmp_path)

    response = client.get("/api/teams")

    assert response.status_code == 200
    assert response.json() == {"teams": ["billing", "growth"], "default": "shared"}


def test_teams_route_excludes_persona_only_teams(config, tmp_path):
    # A persona-only team ("platform" below, no skill owned by it) must not
    # show up here — contrast with /api/catalog/teams, which spans both
    # kinds.
    storage = FakeStorage(
        buckets={
            "skills-bucket": {"index.json": (_index_bytes(SKILL_ENTRIES), {})},
            "personas-bucket": {
                "index.json": (
                    _index_bytes(
                        [
                            {
                                "id": "p1",
                                "name": "p1",
                                "description": "d",
                                "team": "platform",
                                "tags": [],
                                "builtin": True,
                            }
                        ]
                    ),
                    {},
                )
            },
        }
    )
    client = _make_client(config, _minimal_clients(storage=storage), tmp_path)

    response = client.get("/api/teams")

    assert response.status_code == 200
    assert response.json() == {"teams": ["shared", "sre"], "default": "shared"}


def test_teams_route_never_ingested_still_returns_default(config, tmp_path):
    client = _make_client(config, _minimal_clients(storage=FakeStorage()), tmp_path)

    response = client.get("/api/teams")

    assert response.status_code == 200
    assert response.json() == {"teams": [], "default": "shared"}


def test_teams_route_ttl_refresh_runs_once_for_two_immediate_calls(client, fake_storage):
    client.get("/api/teams")
    client.get("/api/teams")

    assert fake_storage.calls.count("read_bytes:skills-bucket:index.json") == 1
    assert fake_storage.calls.count("read_bytes:personas-bucket:index.json") == 1


def test_search_skills_response_includes_owner_derived_team(config, tmp_path):
    storage = FakeStorage(
        buckets={
            "skills-bucket": {
                "index.json": (
                    _index_bytes(
                        [
                            {
                                "id": "owned-skill",
                                "name": "Owned skill",
                                "description": "Owned by growth",
                                "owner": "growth",
                                "tags": [],
                                "sha": "abc",
                                "builtin": False,
                            }
                        ]
                    ),
                    {},
                )
            },
            "personas-bucket": {},
        }
    )
    client = _make_client(config, _minimal_clients(storage=storage), tmp_path)

    response = client.get("/api/skills/search", params={"q": ""})

    assert response.status_code == 200
    assert response.json() == {
        "results": [
            {
                "id": "owned-skill",
                "name": "Owned skill",
                "description": "Owned by growth",
                "team": "growth",
                "tags": [],
                "sha": "abc",
            }
        ]
    }



# --- skills recommendation (create-flow polish, 2026-08-28) ---------------------


def test_recommend_skills_ranks_by_keyword_hits(client):
    """A persona about terraform review recommends BOTH matching skills --
    the terraform skill and the review skill -- and never the word-games
    one its text shares no keyword with."""
    response = client.get(
        "/api/skills/recommended",
        params={
            "who": "an infrastructure change reviewer",
            "what": "You review terraform plans and pull requests before anything merges.",
        },
    )
    assert response.status_code == 200
    ids = [r["id"] for r in response.json()["results"]]
    assert "terraform-check" in ids
    assert "code-review" in ids
    assert "word-games" not in ids


def test_recommend_skills_empty_persona_recommends_nothing(client):
    response = client.get("/api/skills/recommended", params={"who": "", "what": ""})
    assert response.status_code == 200
    assert response.json() == {"results": []}



def test_recommend_skills_keeps_three_letter_domain_words(client):
    """The live failure: 'A GCP expert' must surface gcp-tagged skills --
    gcp is three letters, and persona-generic words like 'expert' must
    never be what the match hangs on."""
    response = client.get(
        "/api/skills/recommended",
        params={"who": "A GCP expert", "what": "gcp terraform infrastructure"},
    )
    assert response.status_code == 200
    ids = [r["id"] for r in response.json()["results"]]
    assert "terraform-check" in ids
