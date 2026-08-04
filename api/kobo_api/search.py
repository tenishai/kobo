"""FTS5 catalog search (Phase A Task 3): every configured catalog's
`index.json` object, ingested into one SQLite FTS5 table and queried by kind
("skill", "persona", and any further kind a caller's `catalogs` mapping
names), with an optional team filter.

Ingestion is deliberately atomic, idempotent, and kind-generic:
`ingest_indexes` reads and fully validates every catalog in its `catalogs`
mapping *before* touching the table at all, so a malformed index in one
bucket can never leave the table half-rebuilt, and adding a new catalog kind
is just one more mapping entry — nothing here needs reworking. `ensure_fts`
(called by every entry point here, ingestion or query) means a fresh
database with no FTS table yet behaves exactly like an empty one rather than
raising — a never-ingested search is just `[]`, not an error.

The MATCH string `search()` builds from a caller's `q` is never string-
interpolated: every token is quoted and its embedded quotes doubled per
FTS5's own escaping rule (`_escape_match_token`), then passed to SQLite as a
single bound parameter. That's what makes an adversarial `q` like
`foo" OR 1` land as a harmless literal phrase search instead of injected FTS5
query syntax.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Engine

from kobo_api.clients import ClientError, StorageClient, StorageNotFound

log = logging.getLogger(__name__)

# How long a loaded catalog table is considered fresh before a route's TTL
# hook (kobo_api.main._maybe_refresh_catalogs) re-ingests it.
CATALOG_TTL_S = 60

_COLUMNS = "id, name, description, team, tags, kind, sha, builtin"

_INSERT_SQL = (
    "INSERT INTO catalog_fts (id, name, description, team, tags, kind, sha, builtin) "
    "VALUES (:id, :name, :description, :team, :tags, :kind, :sha, :builtin)"
)


@dataclass
class CatalogEntry:
    kind: str  # "skill" | "persona"
    id: str
    name: str
    description: str
    team: str
    tags: list[str]  # stored space-joined in FTS, split back to a list on read
    sha: str = ""
    builtin: bool = False


def ensure_fts(engine: Engine) -> None:
    """Create the `catalog_fts` virtual table if it isn't there yet.

    Idempotent (IF NOT EXISTS) and cheap enough to call unconditionally from
    every entry point in this module — ingestion and every query function —
    so none of them can ever hit "no such table" on a fresh database.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE VIRTUAL TABLE IF NOT EXISTS catalog_fts USING fts5("
                "id, name, description, team, tags, "
                "kind UNINDEXED, sha UNINDEXED, builtin UNINDEXED)"
            )
        )


def _load_catalog_entries(
    storage: StorageClient, bucket: str, prefix: str, kind: str
) -> list[dict]:
    """The validated raw entry dicts for one catalog's `<prefix>index.json`.

    An unset bucket name or a `StorageNotFound` (no index published yet)
    both contribute zero entries — a normal, expected state for an unwired
    or not-yet-published catalog, only logged, never raised. Anything else
    wrong with the object — invalid JSON, a missing `entries` list, or an
    entry missing a required field — raises `ClientError` naming the bucket,
    since that's a real publish-side problem the caller (the refresh route)
    needs to surface, not silently swallow.
    """
    if not bucket:
        log.info("catalog ingest: no %s bucket configured — 0 entries", kind)
        return []

    key = f"{prefix}index.json"
    try:
        data = storage.read_bytes(bucket, key)
    except StorageNotFound:
        log.info("catalog ingest: no %s at %s/%s yet — 0 entries", key, bucket, key)
        return []

    try:
        parsed = json.loads(data)
        raw_entries = parsed["entries"]
        if not isinstance(raw_entries, list):
            raise TypeError("'entries' must be a list")
        entries = []
        for raw in raw_entries:
            if not isinstance(raw, dict):
                raise TypeError("each entry must be an object")
            entries.append(
                {
                    "id": raw["id"],
                    "name": raw["name"],
                    "description": raw["description"],
                    "team": raw["team"],
                    "tags": list(raw["tags"]),
                    "sha": raw.get("sha", ""),
                    "builtin": bool(raw.get("builtin", False)),
                }
            )
        return entries
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ClientError(f"malformed index.json in bucket {bucket!r}: {exc}") from exc


def ingest_indexes(
    engine: Engine,
    storage: StorageClient,
    catalogs: Mapping[str, str],
    *,
    prefix: str = "",
) -> dict[str, int]:
    """Rebuild `catalog_fts` from every catalog in `catalogs` (kind -> bucket
    name, e.g. `{"skill": "skills-bucket", "persona": "personas-bucket"}`).

    Generic over the number and names of kinds: adding a third catalog (the
    upcoming "tool" kind) is one more mapping entry from the caller, nothing
    here needs reworking. Every catalog is read and fully validated first
    (per-kind post-processing — skills carry `sha`, personas carry
    `builtin` — already lives in `_load_catalog_entries`); only once all have
    succeeded does the table get touched, and then as one atomic
    DELETE-then-INSERT transaction — a malformed index in one bucket never
    leaves the table half-rebuilt, and a successful rebuild never mixes rows
    from a stale ingest with a partial new one. Returns entry counts keyed by
    kind, e.g. `{"skill": 2, "persona": 1}`.
    """
    ensure_fts(engine)

    entries_by_kind = {
        kind: _load_catalog_entries(storage, bucket, prefix, kind)
        for kind, bucket in catalogs.items()
    }

    rows = [
        {
            "id": e["id"],
            "name": e["name"],
            "description": e["description"],
            "team": e["team"],
            "tags": " ".join(e["tags"]),
            "kind": kind,
            "sha": e["sha"],
            "builtin": e["builtin"],
        }
        for kind, entries in entries_by_kind.items()
        for e in entries
    ]

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM catalog_fts"))
        if rows:
            conn.execute(text(_INSERT_SQL), rows)

    return {kind: len(entries) for kind, entries in entries_by_kind.items()}


def _row_to_entry(row) -> CatalogEntry:
    return CatalogEntry(
        kind=row.kind,
        id=row.id,
        name=row.name,
        description=row.description,
        team=row.team,
        tags=row.tags.split() if row.tags else [],
        sha=row.sha or "",
        builtin=bool(row.builtin),
    )


def _escape_match_token(token: str) -> str:
    """`token` as an FTS5 quoted-phrase prefix query: embedded double quotes
    doubled per FTS5's own escape rule, the whole thing wrapped in quotes and
    given a trailing `*`. Any token — however hostile — becomes a literal
    phrase match this way; there is no way for it to inject FTS5 query
    syntax (a bare `OR`, unbalanced parens, a stray `-`, ...).
    """
    return '"' + token.replace('"', '""') + '"*'


def _build_match(q: str) -> str:
    """Implicit-AND MATCH string from `q`'s whitespace-separated tokens, each
    independently escaped and prefix-starred — e.g. `co revi` becomes
    `"co"* "revi"*`, matching rows containing a term starting with `co` AND
    one starting with `revi`."""
    return " ".join(_escape_match_token(token) for token in q.split())


def search(
    engine: Engine, kind: str, q: str, team: str | None = None, limit: int = 50
) -> list[CatalogEntry]:
    """Entries of `kind`, optionally filtered to `team`, matching `q`.

    Blank/whitespace-only `q` returns everything of `kind` (team-filtered),
    ordered by name — the "browse" case. A non-blank `q` runs a ranked FTS5
    MATCH built from its tokens (see `_build_match`), ordered by
    `bm25(catalog_fts)` ascending (lower is more relevant).
    """
    ensure_fts(engine)
    q = q.strip()

    params: dict = {"kind": kind, "limit": limit}
    if not q:
        sql = f"SELECT {_COLUMNS} FROM catalog_fts WHERE kind = :kind"
        if team is not None:
            sql += " AND team = :team"
            params["team"] = team
        sql += " ORDER BY name LIMIT :limit"
    else:
        params["match"] = _build_match(q)
        sql = f"SELECT {_COLUMNS} FROM catalog_fts WHERE catalog_fts MATCH :match AND kind = :kind"
        if team is not None:
            sql += " AND team = :team"
            params["team"] = team
        sql += " ORDER BY bm25(catalog_fts) LIMIT :limit"

    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).all()
    return [_row_to_entry(row) for row in rows]


def get_entry(engine: Engine, kind: str, id: str, team: str | None = None) -> CatalogEntry | None:
    """The single `(kind, id[, team])` entry, or None if there's no such row.

    A skill id is only unique within its own team, so a skill lookup should
    always pass `team`. A persona id, by contrast, is unique across the
    whole personas index regardless of team (personas are org-wide) — Task 5
    callers resolving `payload.persona` don't know (or care) which team
    authored it, so `team` is optional here: `None` matches any team,
    rather than forcing every caller to look the team up first just to ask
    for it back.
    """
    ensure_fts(engine)
    sql = f"SELECT {_COLUMNS} FROM catalog_fts WHERE kind = :kind AND id = :id"
    params: dict = {"kind": kind, "id": id}
    if team is not None:
        sql += " AND team = :team"
        params["team"] = team
    with engine.connect() as conn:
        row = conn.execute(text(sql), params).first()
    return _row_to_entry(row) if row is not None else None


def teams(engine: Engine) -> list[str]:
    """Every distinct team name across both catalogs, sorted."""
    ensure_fts(engine)
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT DISTINCT team FROM catalog_fts ORDER BY team")).all()
    return [row[0] for row in rows]
