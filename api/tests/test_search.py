"""Unit tests for kobo_api.search: catalog ingestion + the query functions
built on top of it, exercised against `FakeStorage` (tests/fakes.py) — no
real GCS or network involved anywhere.

Dual-engine (resilience arc, Task 12): every test in this module runs
against whichever backend the `engine` fixture below hands it. By default
(no `POSTGRES_TEST_URL` set) that's the exact same per-test SQLite file this
module has always used — every existing assertion here is completely
unaffected. Setting `POSTGRES_TEST_URL` to a real Postgres connection string
switches the WHOLE module's `engine` fixture to that database instead
(dropping `catalog_fts` between tests for isolation, since a real Postgres
instance is long-lived rather than a fresh tmp_path file per test), so the
identical test bodies — hostile-input escaping included — prove the
Postgres full-text path (`kobo_api.search`'s `to_tsvector`/`to_tsquery`
branch) behaves the same as the SQLite FTS5 path they were written against.
"""

from __future__ import annotations

import json
import logging
import os

import pytest
from sqlalchemy import text

from kobo_api.clients import ClientError
from kobo_api.db import make_engine
from kobo_api.search import (
    CATALOG_TTL_S,
    CatalogEntry,
    catalog_is_empty,
    distinct_skill_teams,
    ensure_fts,
    get_entry,
    ingest_indexes,
    read_marketplace_catalog,
    search,
    teams,
)
from tests.fakes import FakeStorage

# Resilience arc (Task 12): the env-var skip-if-absent seam this module's
# Postgres proof runs through. Chosen over a testcontainers dependency
# deliberately -- it needs no new test-only package, and a room without
# docker (or without network access to pull an image) still runs this whole
# module unchanged against SQLite; see the module docstring above.
POSTGRES_TEST_URL = os.environ.get("POSTGRES_TEST_URL", "").strip()


def _index_bytes(entries: list[dict]) -> bytes:
    return json.dumps({"generated_at": "2026-08-03T00:00:00Z", "entries": entries}).encode()


SKILL_ENTRIES = [
    {
        "id": "code-review",
        "name": "Code review",
        "description": "Reviews pull requests for quality issues",
        "team": "shared",
        "tags": ["review", "quality"],
        "sha": "abc123",
        "builtin": False,
    },
    {
        "id": "commit-helper",
        "name": "Commit helper",
        "description": "Writes commit messages",
        "team": "platform",
        "tags": ["git", "commits"],
        "sha": "def456",
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


@pytest.fixture
def engine(tmp_path):
    if POSTGRES_TEST_URL:
        pg_engine = make_engine(POSTGRES_TEST_URL)
        # A real Postgres instance persists across tests (unlike the
        # per-test tmp_path SQLite file below), so each test gets a clean
        # slate by dropping whatever the previous test's `ensure_fts`/
        # `ingest_indexes` left behind -- `ensure_fts` recreates it on the
        # very first call any test in this module makes.
        with pg_engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS catalog_fts"))
        try:
            yield pg_engine
        finally:
            pg_engine.dispose()
    else:
        yield make_engine(f"sqlite:///{tmp_path}/search.db")


@pytest.fixture
def storage():
    return FakeStorage(
        buckets={
            "skills-bucket": {"index.json": (_index_bytes(SKILL_ENTRIES), {})},
            "personas-bucket": {"index.json": (_index_bytes(PERSONA_ENTRIES), {})},
        }
    )


CATALOGS = {"skill": "skills-bucket", "persona": "personas-bucket"}


def _ingest(engine, storage):
    return ingest_indexes(engine, storage, CATALOGS)


def test_ensure_fts_is_idempotent(engine):
    ensure_fts(engine)
    ensure_fts(engine)  # a second call must not raise "table already exists"


def test_search_on_never_ingested_db_returns_empty(engine):
    # ensure_fts is called by search() itself — a fresh DB with no ingest
    # ever run must not raise "no such table", just report nothing found.
    assert search(engine, "skill", "") == []


def test_teams_on_never_ingested_db_returns_empty(engine):
    assert teams(engine) == []


# --- catalog_is_empty (Task 3 perf fix: cold-start vs stale-while-revalidate)


def test_catalog_is_empty_true_on_never_ingested_db(engine):
    # ensure_fts is called by catalog_is_empty itself -- a fresh DB with no
    # ingest ever run must not raise "no such table", just report empty.
    assert catalog_is_empty(engine) is True


def test_catalog_is_empty_false_after_ingest(engine, storage):
    _ingest(engine, storage)
    assert catalog_is_empty(engine) is False


def test_catalog_is_empty_true_again_once_the_table_is_cleared(engine, storage):
    _ingest(engine, storage)
    ingest_indexes(engine, storage, {})  # rebuild with nothing configured
    assert catalog_is_empty(engine) is True


def test_catalog_ttl_is_five_minutes():
    # Raised from 60s (Task 3 perf fix): the request path no longer blocks
    # on a stale refresh, so a longer TTL just means fewer background
    # refreshes, not a slower create modal. POST /api/catalogs/refresh
    # remains the operator's own now-button regardless of this value.
    assert CATALOG_TTL_S == 300


def test_ingest_indexes_returns_counts(engine, storage):
    counts = _ingest(engine, storage)
    assert counts == {"skill": 2, "persona": 1}


def test_ingest_drops_archived_entries(engine):
    storage = FakeStorage(
        buckets={
            "personas-bucket": {
                "index.json": (
                    _index_bytes(
                        [
                            {"id": "active-one", "name": "Active", "description": "d",
                             "team": "shared", "tags": [], "builtin": False},
                            {"id": "shelved-one", "name": "Shelved", "description": "d",
                             "team": "shared", "tags": [], "builtin": False,
                             "archived": True},
                        ]
                    ),
                    {},
                )
            },
        }
    )
    counts = ingest_indexes(engine, storage, {"persona": "personas-bucket"})
    assert counts == {"persona": 1}
    ids = [e.id for e in search(engine, "persona", "")]
    assert ids == ["active-one"]


def test_ingest_records_exactly_one_read_per_bucket(engine, storage):
    _ingest(engine, storage)
    assert storage.calls.count("read_bytes:skills-bucket:index.json") == 1
    assert storage.calls.count("read_bytes:personas-bucket:index.json") == 1


def test_empty_q_returns_all_of_kind_ordered_by_name(engine, storage):
    _ingest(engine, storage)
    results = search(engine, "skill", "")
    assert [e.id for e in results] == ["code-review", "commit-helper"]


def test_search_is_scoped_to_kind(engine, storage):
    _ingest(engine, storage)
    results = search(engine, "persona", "")
    assert [e.id for e in results] == ["helpful-generalist"]
    assert results[0].builtin is True
    assert results[0].tags == []  # empty tags round-trip to [], never [""]


def test_search_team_filter(engine, storage):
    _ingest(engine, storage)
    results = search(engine, "skill", "", team="platform")
    assert [e.id for e in results] == ["commit-helper"]


def test_search_prefix_match_as_you_type(engine, storage):
    _ingest(engine, storage)
    # "revi" is a prefix of "review", which only appears (in name and
    # description) on the code-review skill.
    results = search(engine, "skill", "revi")
    assert [e.id for e in results] == ["code-review"]


def test_search_implicit_and_across_tokens(engine, storage):
    _ingest(engine, storage)
    # "co" prefixes both "code" and "commit" (both entries), but "revi" only
    # ever appears on code-review — the implicit AND of both tokens must
    # exclude commit-helper even though it matches the first token alone.
    results = search(engine, "skill", "co revi")
    assert [e.id for e in results] == ["code-review"]


def test_search_ranked_prefix_match_orders_by_relevance(engine):
    engine_storage = FakeStorage(
        buckets={
            "skills-bucket": {
                "index.json": (
                    _index_bytes(
                        [
                            {
                                "id": "alpha",
                                "name": "Alpha widget",
                                "description": "Just a widget, nothing special",
                                "team": "shared",
                                "tags": ["misc"],
                                "sha": "",
                                "builtin": False,
                            },
                            {
                                "id": "widget-pro",
                                "name": "Widget Pro widget kit",
                                "description": "A widget for building widgets",
                                "team": "shared",
                                "tags": ["widget", "widget-tools"],
                                "sha": "",
                                "builtin": False,
                            },
                        ]
                    ),
                    {},
                )
            },
            "personas-bucket": {},
        }
    )
    ingest_indexes(engine, engine_storage, CATALOGS)
    results = search(engine, "skill", "widget")
    # widget-pro mentions "widget" far more densely across id/name/
    # description/tags than alpha (a single mention, in description only) —
    # bm25 ascending must put the denser match first.
    assert [e.id for e in results] == ["widget-pro", "alpha"]


# The hostile-input contract (resilience arc, Task 12): whatever `q` a
# caller sends, it must land as a literal term against BOTH backends' query
# engines -- never as FTS5 MATCH syntax, never as Postgres tsquery syntax.
# A naive f-string-built query (on either engine) would either blow up with
# a syntax error or, worse, let a bare `OR` widen the query to match every
# row. This exact set of cases -- the original `OR`-injection case plus
# unbalanced parens, a stray leading dash, and an embedded quote -- is the
# contract every backend's escaper must satisfy; extend this list, never
# shrink it.
HOSTILE_QUERIES = [
    'foo" OR 1',
    "foo(bar",
    "foo)bar",
    "-foo",
    "foo'bar",
]


@pytest.mark.parametrize(
    "hostile_q",
    HOSTILE_QUERIES,
    ids=["or-injection", "unbalanced-open-paren", "unbalanced-close-paren", "stray-dash", "embedded-quote"],
)
def test_search_hostile_q_is_escaped_not_injected(engine, storage, hostile_q):
    _ingest(engine, storage)
    # None of these strings appear anywhere in the seeded fixture data, so a
    # correctly escaped literal-term search always comes back empty -- the
    # failure mode this guards against is a widened match (every row) or a
    # raised query-syntax error, not a false positive on real content.
    results = search(engine, "skill", hostile_q)
    assert results == []


def test_get_entry_round_trips_tags(engine, storage):
    _ingest(engine, storage)
    entry = get_entry(engine, "skill", "code-review", "shared")
    assert entry == CatalogEntry(
        kind="skill",
        id="code-review",
        name="Code review",
        description="Reviews pull requests for quality issues",
        team="shared",
        tags=["review", "quality"],
        sha="abc123",
        builtin=False,
    )


def test_get_entry_wrong_team_is_none(engine, storage):
    _ingest(engine, storage)
    assert get_entry(engine, "skill", "code-review", "platform") is None


def test_get_entry_missing_is_none(engine, storage):
    _ingest(engine, storage)
    assert get_entry(engine, "skill", "nonexistent", "shared") is None


def test_get_entry_team_none_matches_any_team(engine):
    # Task 5 controller decision #1: team is optional — None matches any
    # team, since a persona lookup (org-wide, id unique across the whole
    # personas index) shouldn't have to know which team authored it first.
    storage = FakeStorage(
        buckets={
            "personas-bucket": {
                "index.json": (
                    _index_bytes(
                        [
                            {
                                "id": "helpful-generalist",
                                "name": "Helpful Generalist",
                                "description": "A friendly all-rounder",
                                "team": "platform",
                                "tags": [],
                                "builtin": True,
                            }
                        ]
                    ),
                    {},
                )
            }
        }
    )
    ingest_indexes(engine, storage, {"persona": "personas-bucket"})

    entry = get_entry(engine, "persona", "helpful-generalist")
    assert entry is not None
    assert entry.team == "platform"


def test_get_entry_team_positional_still_scopes(engine, storage):
    # team, when given, is still a positional-or-keyword filter — unchanged
    # behavior for skill lookups, which must stay scoped to one team.
    assert get_entry(engine, "skill", "code-review", "platform") is None


def test_teams_distinct_and_sorted(engine, storage):
    _ingest(engine, storage)
    assert teams(engine) == ["platform", "shared"]


def test_unset_bucket_contributes_zero(engine, storage):
    counts = ingest_indexes(engine, storage, {"skill": "", "persona": "personas-bucket"})
    assert counts == {"skill": 0, "persona": 1}
    assert "read_bytes:skills-bucket:index.json" not in storage.calls


def test_missing_index_contributes_zero(engine):
    storage = FakeStorage(buckets={"skills-bucket": {}, "personas-bucket": {}})
    counts = ingest_indexes(engine, storage, CATALOGS)
    assert counts == {"skill": 0, "persona": 0}


def test_malformed_json_raises_client_error_naming_bucket(engine):
    storage = FakeStorage(buckets={"skills-bucket": {"index.json": (b"not json", {})}})
    with pytest.raises(ClientError, match="skills-bucket"):
        ingest_indexes(engine, storage, {"skill": "skills-bucket", "persona": ""})


def test_malformed_entry_raises_client_error_naming_bucket(engine):
    storage = FakeStorage(
        buckets={"skills-bucket": {"index.json": (_index_bytes([{"id": "x"}]), {})}}
    )
    with pytest.raises(ClientError, match="skills-bucket"):
        ingest_indexes(engine, storage, {"skill": "skills-bucket", "persona": ""})


def test_malformed_index_leaves_table_unchanged(engine, storage):
    # A good ingest, then a bad one against a *different* bucket name (still
    # "skills-bucket", replaced with bad content) must not wipe out rows
    # already successfully loaded for the other catalog.
    _ingest(engine, storage)
    bad_storage = FakeStorage(
        buckets={
            "skills-bucket": {"index.json": (b"not json", {})},
            "personas-bucket": {"index.json": (_index_bytes(PERSONA_ENTRIES), {})},
        }
    )
    with pytest.raises(ClientError):
        ingest_indexes(engine, bad_storage, CATALOGS)
    # the previous good ingest's rows are still there — the failed rebuild
    # never reached the DELETE/INSERT transaction at all.
    assert [e.id for e in search(engine, "skill", "")] == ["code-review", "commit-helper"]


def test_ingest_with_third_kind_is_searchable_with_no_code_changes(engine, storage):
    # Extensibility proof (Task 3 fix round 1 finding): `ingest_indexes`
    # takes a plain kind->bucket mapping, so a brand-new "tool" kind (the
    # upcoming third catalog) ingests and is searchable via the exact same
    # `search()` used for "skill"/"persona" — no change to either function.
    storage.write_bytes(
        "tools-bucket",
        "index.json",
        _index_bytes(
            [
                {
                    "id": "http-fetch",
                    "name": "HTTP fetch",
                    "description": "Fetches a URL over HTTP",
                    "team": "shared",
                    "tags": ["network"],
                    "sha": "tool123",
                    "builtin": False,
                }
            ]
        ),
    )

    counts = ingest_indexes(
        engine,
        storage,
        {**CATALOGS, "tool": "tools-bucket"},
    )

    assert counts == {"skill": 2, "persona": 1, "tool": 1}
    results = search(engine, "tool", "fetch")
    assert [e.id for e in results] == ["http-fetch"]


# --- Task 1 (persona-metadata plan): owner -> team on skill entries --------


def _skill_index(entries: list[dict]) -> FakeStorage:
    return FakeStorage(buckets={"skills-bucket": {"index.json": (_index_bytes(entries), {})}})


def test_skill_entry_with_owner_sets_team(engine):
    storage = _skill_index(
        [
            {
                "id": "owned-skill",
                "name": "Owned skill",
                "description": "Has an owner",
                "owner": "growth",
                "tags": [],
                "sha": "",
                "builtin": False,
            }
        ]
    )
    ingest_indexes(engine, storage, {"skill": "skills-bucket"})
    entry = get_entry(engine, "skill", "owned-skill", "growth")
    assert entry is not None
    assert entry.team == "growth"


def test_skill_entry_without_owner_or_team_defaults_to_empty(engine):
    # Upstream's actual current shape (design fact): a skill entry with
    # neither "owner" nor "team" at all — not a KeyError, just "".
    storage = _skill_index(
        [
            {
                "id": "unowned-skill",
                "name": "Unowned skill",
                "description": "No owner key at all",
                "tags": [],
                "sha": "",
                "builtin": False,
            }
        ]
    )
    ingest_indexes(engine, storage, {"skill": "skills-bucket"})
    entry = get_entry(engine, "skill", "unowned-skill", "")
    assert entry is not None
    assert entry.team == ""


def test_skill_entry_garbage_owner_int_ingest_succeeds_team_empty_debug_logged(engine, caplog):
    storage = _skill_index(
        [
            {
                "id": "int-owner-skill",
                "name": "Int owner skill",
                "description": "owner is an int, not a string",
                "owner": 42,
                "tags": [],
                "sha": "",
                "builtin": False,
            }
        ]
    )
    with caplog.at_level(logging.DEBUG, logger="kobo_api.search"):
        counts = ingest_indexes(engine, storage, {"skill": "skills-bucket"})

    assert counts == {"skill": 1}
    entry = get_entry(engine, "skill", "int-owner-skill", "")
    assert entry is not None
    assert entry.team == ""
    assert any("int-owner-skill" in record.message for record in caplog.records)


def test_skill_entry_garbage_owner_dict_ingest_succeeds_team_empty_debug_logged(engine, caplog):
    storage = _skill_index(
        [
            {
                "id": "dict-owner-skill",
                "name": "Dict owner skill",
                "description": "owner is a dict, not a string",
                "owner": {"not": "a string"},
                "tags": [],
                "sha": "",
                "builtin": False,
            }
        ]
    )
    with caplog.at_level(logging.DEBUG, logger="kobo_api.search"):
        counts = ingest_indexes(engine, storage, {"skill": "skills-bucket"})

    assert counts == {"skill": 1}
    entry = get_entry(engine, "skill", "dict-owner-skill", "")
    assert entry is not None
    assert entry.team == ""
    assert any("dict-owner-skill" in record.message for record in caplog.records)


def test_skill_entry_owner_wins_over_legacy_team_key(engine):
    # An entry carrying both the old "team" key (today's always-"" norm,
    # or any leftover value) and the new "owner" key: owner is the
    # authoritative field going forward.
    storage = _skill_index(
        [
            {
                "id": "both-keys-skill",
                "name": "Both keys skill",
                "description": "has team and owner",
                "team": "shared",
                "owner": "platform",
                "tags": [],
                "sha": "",
                "builtin": False,
            }
        ]
    )
    ingest_indexes(engine, storage, {"skill": "skills-bucket"})
    entry = get_entry(engine, "skill", "both-keys-skill", "platform")
    assert entry is not None
    assert entry.team == "platform"


def test_mixed_catalog_owner_matrix_ingest_succeeds(engine, caplog):
    # With owner, without owner, and garbage owner all in the same index —
    # a single bad `owner` value must never fail the whole ingest.
    storage = _skill_index(
        [
            {
                "id": "with-owner",
                "name": "With owner",
                "description": "d",
                "owner": "growth",
                "tags": [],
                "sha": "",
                "builtin": False,
            },
            {
                "id": "without-owner",
                "name": "Without owner",
                "description": "d",
                "tags": [],
                "sha": "",
                "builtin": False,
            },
            {
                "id": "garbage-owner",
                "name": "Garbage owner",
                "description": "d",
                "owner": ["not", "a", "string"],
                "tags": [],
                "sha": "",
                "builtin": False,
            },
        ]
    )
    with caplog.at_level(logging.DEBUG, logger="kobo_api.search"):
        counts = ingest_indexes(engine, storage, {"skill": "skills-bucket"})

    assert counts == {"skill": 3}
    results = {e.id: e.team for e in search(engine, "skill", "")}
    assert results == {"with-owner": "growth", "without-owner": "", "garbage-owner": ""}


def test_persona_entry_owner_key_is_ignored_team_semantics_byte_untouched(engine):
    # Personas keep reading the required "team" key exactly as before — a
    # stray "owner" key on a persona entry (should never happen, but must
    # never be allowed to change persona behavior) is simply never looked
    # at.
    storage = FakeStorage(
        buckets={
            "personas-bucket": {
                "index.json": (
                    _index_bytes(
                        [
                            {
                                "id": "helpful-generalist",
                                "name": "Helpful Generalist",
                                "description": "A friendly all-rounder",
                                "team": "platform",
                                "owner": "some-other-team",
                                "tags": [],
                                "builtin": True,
                            }
                        ]
                    ),
                    {},
                )
            }
        }
    )
    ingest_indexes(engine, storage, {"persona": "personas-bucket"})
    entry = get_entry(engine, "persona", "helpful-generalist")
    assert entry is not None
    assert entry.team == "platform"


def test_ingest_existing_persona_team_fixture_rows_still_work(engine, storage):
    # Extends the original ingest test (never weakens it): the personas
    # fixture used everywhere else in this file keeps ingesting and
    # round-tripping its "team" value exactly as before this task.
    _ingest(engine, storage)
    entry = get_entry(engine, "persona", "helpful-generalist")
    assert entry is not None
    assert entry.team == "shared"


# --- distinct_skill_teams ---------------------------------------------------


def test_distinct_skill_teams_on_never_ingested_db_returns_empty(engine):
    assert distinct_skill_teams(engine) == []


def test_distinct_skill_teams_sorted_deduped_excludes_empty(engine):
    storage = _skill_index(
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
            {
                "id": "s3",
                "name": "s3",
                "description": "d",
                "owner": "growth",  # duplicate — must be deduped
                "tags": [],
                "sha": "",
                "builtin": False,
            },
            {
                "id": "s4",
                "name": "s4",
                "description": "d",
                # no owner at all — team "" must be excluded
                "tags": [],
                "sha": "",
                "builtin": False,
            },
        ]
    )
    ingest_indexes(engine, storage, {"skill": "skills-bucket"})
    assert distinct_skill_teams(engine) == ["billing", "growth"]


def test_distinct_skill_teams_excludes_persona_teams(engine):
    # Personas contribute team values too (e.g. "platform" below) — those
    # must never leak into the skill-only teams list `distinct_skill_teams`
    # exists to produce (contrast with the pre-existing `teams()`, which
    # spans both kinds).
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
                            }
                        ]
                    ),
                    {},
                )
            },
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
    ingest_indexes(engine, storage, CATALOGS)
    assert distinct_skill_teams(engine) == ["growth"]


# --- Task 1b (persona-metadata plan): the marketplace dialect ---------------


def _marketplace_index_bytes(skills: dict) -> bytes:
    return json.dumps({"skills": skills, "count": len(skills)}).encode()


def _marketplace_storage(skills: dict, *, bucket: str = "skills-bucket") -> FakeStorage:
    return FakeStorage(buckets={bucket: {"index.json": (_marketplace_index_bytes(skills), {})}})


def test_marketplace_dialect_ingests_mapped_field_by_field(engine):
    storage = _marketplace_storage(
        {
            "code-review": {
                "name": "Code review",
                "version": "3",
                "description": "Reviews pull requests for quality issues",
                "when_to_use": "when reviewing a pull request",
                "tags": ["review", "quality"],
                "profiles": ["default"],
                "content_hash": "abc123",
                "size": 512,
                "owner": "growth",
            }
        }
    )
    counts = ingest_indexes(engine, storage, {"skill": "skills-bucket"})
    assert counts == {"skill": 1}

    entry = get_entry(engine, "skill", "code-review", "growth")
    assert entry is not None
    assert entry.name == "Code review"
    assert entry.description == "Reviews pull requests for quality issues"
    assert entry.tags == ["review", "quality"]
    assert entry.team == "growth"
    assert entry.sha == "abc123"
    assert entry.builtin is False
    # when_to_use/profiles/size are ignored -- not part of the FTS row shape.


def test_marketplace_dialect_entry_without_owner_defaults_to_empty_team(engine):
    storage = _marketplace_storage(
        {
            "no-owner-skill": {
                "name": "No owner skill",
                "version": "1",
                "description": "No owner key at all",
                "tags": [],
                "content_hash": "shaX",
            }
        }
    )
    ingest_indexes(engine, storage, {"skill": "skills-bucket"})
    entry = get_entry(engine, "skill", "no-owner-skill", "")
    assert entry is not None
    assert entry.team == ""


def test_marketplace_malformed_entries_skipped_not_fatal(engine, caplog):
    storage = _marketplace_storage(
        {
            "good-skill": {
                "name": "Good skill",
                "version": "1",
                "description": "A fine skill",
                "tags": [],
                "content_hash": "goodsha",
            },
            "missing-name": {
                "description": "No name field at all",
                "tags": [],
                "content_hash": "x",
            },
            "wrong-type-name": {
                "name": 42,
                "description": "name is an int, not a string",
                "tags": [],
                "content_hash": "y",
            },
            "wrong-type-description": {
                "name": "Bad description",
                "description": {"not": "a string"},
                "tags": [],
                "content_hash": "z",
            },
            "not-an-object": "just a bare string, not an object at all",
        }
    )
    with caplog.at_level(logging.DEBUG, logger="kobo_api.search"):
        counts = ingest_indexes(engine, storage, {"skill": "skills-bucket"})

    # Only the one well-formed entry survives -- every malformed entry is
    # skipped, never fails the whole ingest.
    assert counts == {"skill": 1}
    assert get_entry(engine, "skill", "good-skill", "") is not None
    for bad_id in ("missing-name", "wrong-type-name", "wrong-type-description", "not-an-object"):
        assert get_entry(engine, "skill", bad_id, "") is None
        assert any(bad_id in record.message for record in caplog.records)


def test_marketplace_dialect_does_not_disturb_legacy_ingest_in_same_call(engine, storage):
    # `storage` (module fixture) already carries the legacy list-shaped
    # personas-bucket index -- a marketplace-dialect skills bucket ingesting
    # alongside it must not affect persona ingestion at all.
    storage.write_bytes(
        "skills-bucket",
        "index.json",
        _marketplace_index_bytes(
            {
                "mp-skill": {
                    "name": "Marketplace skill",
                    "version": "1",
                    "description": "d",
                    "tags": [],
                    "content_hash": "mpsha",
                }
            }
        ),
    )
    counts = ingest_indexes(engine, storage, CATALOGS)
    assert counts == {"skill": 1, "persona": 1}
    assert get_entry(engine, "persona", "helpful-generalist") is not None
    assert get_entry(engine, "skill", "mp-skill", "") is not None


# --- Fix round 1 (T1b review): _load_marketplace_entries tags coercion -----


def test_marketplace_string_typed_tags_skipped_not_exploded_into_characters(engine, caplog):
    # `list("fun,cool")` doesn't raise TypeError -- a string is iterable, so
    # the old `except TypeError` guard let it through as one character per
    # "tag" instead of catching the malformed value. A string-typed `tags`
    # must be treated the same as any other malformed entry: skipped,
    # debug-logged, never fatal to the whole ingest.
    storage = _marketplace_storage(
        {
            "string-tags-skill": {
                "name": "String tags skill",
                "version": "1",
                "description": "tags is a string, not a list",
                "tags": "fun,cool",
                "content_hash": "strtagsha",
            }
        }
    )
    with caplog.at_level(logging.DEBUG, logger="kobo_api.search"):
        counts = ingest_indexes(engine, storage, {"skill": "skills-bucket"})

    assert counts == {"skill": 0}
    assert get_entry(engine, "skill", "string-tags-skill", "") is None
    assert any("string-tags-skill" in record.message for record in caplog.records)


# --- Fix round 1 (T1b review): blank/whitespace owner via the dict-shaped --
# --- marketplace path (the Task 1 guard exercised only via the legacy list-
# --- shaped `_skill_index` helper below, until now) -------------------------


def test_marketplace_dialect_blank_owner_falls_through_dict_shaped_path(engine, caplog):
    storage = _marketplace_storage(
        {
            "blank-owner-mp-skill": {
                "name": "Blank owner marketplace skill",
                "version": "1",
                "description": "owner is whitespace-only in the dict-shaped mapping",
                "owner": "   ",
                "tags": [],
                "content_hash": "mpsha-blank",
            }
        }
    )
    with caplog.at_level(logging.DEBUG, logger="kobo_api.search"):
        ingest_indexes(engine, storage, {"skill": "skills-bucket"})

    entry = get_entry(engine, "skill", "blank-owner-mp-skill", "")
    assert entry is not None
    assert entry.team == ""
    assert any("blank-owner-mp-skill" in record.message for record in caplog.records)


# --- Task 1b: the Task 1 review gap -- empty-string owner is garbage too ----


def test_skill_entry_empty_string_owner_falls_through_legacy_team_logged(engine, caplog):
    storage = _skill_index(
        [
            {
                "id": "blank-owner-skill",
                "name": "Blank owner skill",
                "description": "owner is an empty string",
                "owner": "",
                "team": "legacy-team",
                "tags": [],
                "sha": "",
                "builtin": False,
            }
        ]
    )
    with caplog.at_level(logging.DEBUG, logger="kobo_api.search"):
        ingest_indexes(engine, storage, {"skill": "skills-bucket"})

    entry = get_entry(engine, "skill", "blank-owner-skill", "legacy-team")
    assert entry is not None
    assert entry.team == "legacy-team"
    assert any("blank-owner-skill" in record.message for record in caplog.records)


def test_skill_entry_whitespace_owner_falls_through_empty_team_logged(engine, caplog):
    storage = _skill_index(
        [
            {
                "id": "whitespace-owner-skill",
                "name": "Whitespace owner skill",
                "description": "owner is whitespace-only",
                "owner": "   ",
                "tags": [],
                "sha": "",
                "builtin": False,
            }
        ]
    )
    with caplog.at_level(logging.DEBUG, logger="kobo_api.search"):
        ingest_indexes(engine, storage, {"skill": "skills-bucket"})

    entry = get_entry(engine, "skill", "whitespace-owner-skill", "")
    assert entry is not None
    assert entry.team == ""
    assert any("whitespace-owner-skill" in record.message for record in caplog.records)


# --- Task 1b: read_marketplace_catalog (the fetch path's dialect probe) -----


def test_read_marketplace_catalog_returns_skills_mapping_when_marketplace(engine):
    storage = _marketplace_storage(
        {"code-review": {"name": "Code review", "version": "3", "description": "d", "tags": []}}
    )
    catalog = read_marketplace_catalog(storage, "skills-bucket")
    assert catalog is not None
    assert catalog["code-review"]["version"] == "3"


def test_read_marketplace_catalog_returns_none_for_legacy_dialect(engine, storage):
    assert read_marketplace_catalog(storage, "skills-bucket") is None


def test_read_marketplace_catalog_returns_none_when_index_missing(engine):
    storage = FakeStorage(buckets={"skills-bucket": {}})
    assert read_marketplace_catalog(storage, "skills-bucket") is None


def test_read_marketplace_catalog_returns_none_on_malformed_json(engine):
    storage = FakeStorage(buckets={"skills-bucket": {"index.json": (b"not json", {})}})
    assert read_marketplace_catalog(storage, "skills-bucket") is None


# --- Fix round 2 (whole-arc review): the marketplace bucket's REAL catalog --
# --- path is `catalog/catalog.json`, not `index.json` -----------------------
#
# Task 1b's dialect sniffing (above) was applied to the right shape at the
# wrong object path — a real marketplace bucket never carries `index.json`
# at all, it ships `catalog/catalog.json`. `index.json` is tried FIRST and
# wins whenever both exist (legacy wins because existing room buckets must
# never change behavior — every bucket kobo has ever ingested from already
# speaks that contract); only a bucket that has never carried `index.json`
# falls through to `catalog/catalog.json`.


def _catalog_json_only_storage(skills: dict, *, bucket: str = "skills-bucket") -> FakeStorage:
    """A bucket carrying ONLY `catalog/catalog.json` — no `index.json` key
    at all — the real marketplace layout this fix round makes ingestible.
    """
    return FakeStorage(buckets={bucket: {"catalog/catalog.json": (_marketplace_index_bytes(skills), {})}})


def test_ingest_resolves_catalog_json_when_index_json_absent(engine):
    storage = _catalog_json_only_storage(
        {
            "code-review": {
                "name": "Code review",
                "version": "3",
                "description": "Reviews pull requests for quality issues",
                "tags": ["review", "quality"],
                "content_hash": "abc123",
                "owner": "growth",
            }
        }
    )
    counts = ingest_indexes(engine, storage, {"skill": "skills-bucket"})
    assert counts == {"skill": 1}
    entry = get_entry(engine, "skill", "code-review", "growth")
    assert entry is not None
    assert entry.name == "Code review"


def test_read_marketplace_catalog_resolves_catalog_json_when_index_json_absent(engine):
    storage = _catalog_json_only_storage(
        {"code-review": {"name": "Code review", "version": "3", "description": "d", "tags": []}}
    )
    catalog = read_marketplace_catalog(storage, "skills-bucket")
    assert catalog is not None
    assert catalog["code-review"]["version"] == "3"


def test_both_present_index_json_wins(engine):
    # index.json here is the LEGACY list-shaped dialect, carrying an entry
    # `catalog/catalog.json` (a marketplace-shaped dict) does not — proof
    # that when both are present, `index.json` is read and `catalog/
    # catalog.json` is never even consulted, exactly today's byte-untouched
    # behavior for every bucket that has always carried `index.json`.
    storage = FakeStorage(
        buckets={
            "skills-bucket": {
                "index.json": (_index_bytes(SKILL_ENTRIES), {}),
                "catalog/catalog.json": (
                    _marketplace_index_bytes(
                        {"marketplace-only-skill": {"name": "x", "description": "d", "tags": []}}
                    ),
                    {},
                ),
            }
        }
    )
    counts = ingest_indexes(engine, storage, {"skill": "skills-bucket"})
    assert counts == {"skill": 2}
    assert [e.id for e in search(engine, "skill", "")] == ["code-review", "commit-helper"]
    assert get_entry(engine, "skill", "marketplace-only-skill", "") is None
    assert "read_bytes:skills-bucket:catalog/catalog.json" not in storage.calls
