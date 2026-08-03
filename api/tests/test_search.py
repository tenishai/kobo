"""Unit tests for pnyx_api.search: FTS5 ingestion + the query functions built
on top of it, exercised against `FakeStorage` (tests/fakes.py) — no real GCS
or network involved anywhere.
"""

from __future__ import annotations

import json

import pytest

from pnyx_api.clients import ClientError
from pnyx_api.db import make_engine
from pnyx_api.search import (
    CatalogEntry,
    ensure_fts,
    get_entry,
    ingest_indexes,
    search,
    teams,
)
from tests.fakes import FakeStorage


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
    return make_engine(f"sqlite:///{tmp_path}/search.db")


@pytest.fixture
def storage():
    return FakeStorage(
        buckets={
            "skills-bucket": {"index.json": (_index_bytes(SKILL_ENTRIES), {})},
            "personas-bucket": {"index.json": (_index_bytes(PERSONA_ENTRIES), {})},
        }
    )


def _ingest(engine, storage):
    return ingest_indexes(
        engine, storage, skills_bucket="skills-bucket", personas_bucket="personas-bucket"
    )


def test_ensure_fts_is_idempotent(engine):
    ensure_fts(engine)
    ensure_fts(engine)  # a second call must not raise "table already exists"


def test_search_on_never_ingested_db_returns_empty(engine):
    # ensure_fts is called by search() itself — a fresh DB with no ingest
    # ever run must not raise "no such table", just report nothing found.
    assert search(engine, "skill", "") == []


def test_teams_on_never_ingested_db_returns_empty(engine):
    assert teams(engine) == []


def test_ingest_indexes_returns_counts(engine, storage):
    counts = _ingest(engine, storage)
    assert counts == {"skills": 2, "personas": 1}


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
    ingest_indexes(
        engine, engine_storage, skills_bucket="skills-bucket", personas_bucket="personas-bucket"
    )
    results = search(engine, "skill", "widget")
    # widget-pro mentions "widget" far more densely across id/name/
    # description/tags than alpha (a single mention, in description only) —
    # bm25 ascending must put the denser match first.
    assert [e.id for e in results] == ["widget-pro", "alpha"]


def test_search_hostile_q_is_escaped_not_injected(engine, storage):
    _ingest(engine, storage)
    # A naive f-string MATCH build would either blow up with an FTS5 syntax
    # error or, worse, let `OR 1` widen the query to match every row.
    # Escaping quotes keeps this a literal (zero-hit) phrase search instead.
    results = search(engine, "skill", 'foo" OR 1')
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


def test_teams_distinct_and_sorted(engine, storage):
    _ingest(engine, storage)
    assert teams(engine) == ["platform", "shared"]


def test_unset_bucket_contributes_zero(engine, storage):
    counts = ingest_indexes(engine, storage, skills_bucket="", personas_bucket="personas-bucket")
    assert counts == {"skills": 0, "personas": 1}
    assert "read_bytes:skills-bucket:index.json" not in storage.calls


def test_missing_index_contributes_zero(engine):
    storage = FakeStorage(buckets={"skills-bucket": {}, "personas-bucket": {}})
    counts = ingest_indexes(
        engine, storage, skills_bucket="skills-bucket", personas_bucket="personas-bucket"
    )
    assert counts == {"skills": 0, "personas": 0}


def test_malformed_json_raises_client_error_naming_bucket(engine):
    storage = FakeStorage(buckets={"skills-bucket": {"index.json": (b"not json", {})}})
    with pytest.raises(ClientError, match="skills-bucket"):
        ingest_indexes(engine, storage, skills_bucket="skills-bucket", personas_bucket="")


def test_malformed_entry_raises_client_error_naming_bucket(engine):
    storage = FakeStorage(
        buckets={"skills-bucket": {"index.json": (_index_bytes([{"id": "x"}]), {})}}
    )
    with pytest.raises(ClientError, match="skills-bucket"):
        ingest_indexes(engine, storage, skills_bucket="skills-bucket", personas_bucket="")


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
        ingest_indexes(
            engine, bad_storage, skills_bucket="skills-bucket", personas_bucket="personas-bucket"
        )
    # the previous good ingest's rows are still there — the failed rebuild
    # never reached the DELETE/INSERT transaction at all.
    assert [e.id for e in search(engine, "skill", "")] == ["code-review", "commit-helper"]
