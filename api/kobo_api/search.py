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


def _is_marketplace_dialect(parsed: object) -> bool:
    """Shape sniff, not a config flag: the marketplace dialect (persona-
    metadata plan Task 1b) is a top-level JSON object with a dict-shaped
    `skills` mapping and a `count` key. The legacy dialect (a top-level
    `entries` list) never has either, so this is a safe, unambiguous test
    that never consults any config or org setting -- ingestion and the
    fetch-path dialect probe (`read_marketplace_catalog`, below) both
    delegate to this single definition.
    """
    return (
        isinstance(parsed, dict)
        and isinstance(parsed.get("skills"), dict)
        and "count" in parsed
    )


def _load_catalog_entries(
    storage: StorageClient, bucket: str, prefix: str, kind: str
) -> list[dict]:
    """The validated raw entry dicts for one catalog's `<prefix>index.json`.

    An unset bucket name or a `StorageNotFound` (no index published yet)
    both contribute zero entries -- a normal, expected state for an unwired
    or not-yet-published catalog, only logged, never raised. Anything else
    wrong with the LEGACY-dialect object -- invalid JSON, a missing
    `entries` list, or an entry missing a required field -- raises
    `ClientError` naming the bucket, since that's a real publish-side
    problem the caller (the refresh route) needs to surface, not silently
    swallow.

    Dialect sniffing (persona-metadata plan Task 1b): once the bytes parse
    as JSON, the shape decides which of two readers handles them --
    `_is_marketplace_dialect` for the marketplace's `{"skills": {...},
    "count": N}` object, or the legacy `{"entries": [...]}` list -- never a
    config flag. The marketplace reader has its own, per-entry tolerance
    (see `_load_marketplace_entries`); only a legacy-shaped index still goes
    through the whole-ingest-fails-on-one-bad-entry path above.
    """
    if not bucket:
        log.info("catalog ingest: no %s bucket configured -- 0 entries", kind)
        return []

    key = f"{prefix}index.json"
    try:
        data = storage.read_bytes(bucket, key)
    except StorageNotFound:
        log.info("catalog ingest: no %s at %s/%s yet -- 0 entries", key, bucket, key)
        return []

    try:
        parsed = json.loads(data)
        if _is_marketplace_dialect(parsed):
            return _load_marketplace_entries(parsed["skills"], kind)

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
                    "team": _entry_team(raw, kind),
                    "tags": list(raw["tags"]),
                    "sha": raw.get("sha", ""),
                    "builtin": bool(raw.get("builtin", False)),
                }
            )
        return entries
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ClientError(f"malformed index.json in bucket {bucket!r}: {exc}") from exc


def _load_marketplace_entries(skills: Mapping[str, object], kind: str) -> list[dict]:
    """The validated raw entry dicts for one marketplace-dialect `skills`
    mapping (persona-metadata plan Task 1b).

    Unlike the legacy list-shaped dialect above -- whose per-entry
    validation failures raise `ClientError` for the WHOLE ingest, since a
    hand-authored index getting a required field wrong is a real
    publish-side bug worth surfacing -- a malformed entry here (missing or
    wrong-typed `name`/`description`, or a value that isn't even an object)
    is skipped with a debug log naming its id, never fails the ingest: this
    is a marketplace's own catalog, potentially carrying far more entries
    than any team hand-authors, and one bad listing shouldn't take every
    other skill down with it.

    Mapping: id = the dict key, name/description/tags as published,
    team = `_entry_team`'s owner-precedence rules (identical to the legacy
    dialect, including the empty-string-owner guard), sha = `content_hash`.
    `when_to_use`/`profiles`/`size` are read by nothing here -- ignored.
    """
    entries = []
    for id_, raw in skills.items():
        if not isinstance(raw, dict):
            log.debug(
                "catalog ingest: marketplace skill %r is not an object -- skipping", id_
            )
            continue

        name = raw.get("name")
        description = raw.get("description")
        if not isinstance(name, str) or not isinstance(description, str):
            log.debug(
                "catalog ingest: marketplace skill %r missing/invalid name or "
                "description -- skipping",
                id_,
            )
            continue

        try:
            tags = list(raw.get("tags", []))
        except TypeError:
            log.debug(
                "catalog ingest: marketplace skill %r has non-iterable tags -- skipping",
                id_,
            )
            continue

        entries.append(
            {
                "id": id_,
                "name": name,
                "description": description,
                "team": _entry_team({**raw, "id": raw.get("id", id_)}, kind),
                "tags": tags,
                "sha": raw.get("content_hash", ""),
                "builtin": bool(raw.get("builtin", False)),
            }
        )
    return entries


def read_marketplace_catalog(
    storage: StorageClient, bucket: str, prefix: str = ""
) -> dict[str, dict] | None:
    """The marketplace-dialect `skills` mapping at `<prefix>index.json` in
    `bucket`, or `None` when the bucket doesn't speak that dialect (legacy
    list-shaped index, no index.json published yet, or unreadable JSON).

    This is the attach-time fetch path's dialect probe
    (`kobo_api.steps._stage_skills`): the FTS row shape ingestion feeds
    (id, name, description, team, tags, kind, sha, builtin) is deliberately
    fixed and carries no room for a per-entry `version` or a dialect
    marker, so rather than threading either through it, the fetch path
    re-reads and re-sniffs the exact same object `_load_catalog_entries`
    already sniffed at ingest time, through the identical shape predicate
    (`_is_marketplace_dialect`) -- one definition of "what marketplace
    dialect looks like" in this codebase, asked fresh whenever attach needs
    the answer, never two independently-maintained ones.
    """
    key = f"{prefix}index.json"
    try:
        data = storage.read_bytes(bucket, key)
    except StorageNotFound:
        return None
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError:
        return None
    if not _is_marketplace_dialect(parsed):
        return None
    return parsed["skills"]


def _entry_team(raw: dict, kind: str) -> str:
    """The FTS `team` value for one raw catalog entry.

    Personas keep their persona-era semantics byte-untouched: `team` is a
    required field, read straight off the raw entry with no fallback (a
    missing one still raises via the caller's `KeyError` handling).

    Skills have no `team` field of their own. Upstream skill publishing is
    adding an OPTIONAL `owner` key naming the team that owns the skill --
    when it's present, a string, AND non-blank, it becomes the row's team,
    taking precedence over any legacy `team` key the entry might still
    carry. A missing `owner`; one present but not a string; or one present
    as a string but blank or whitespace-only (a publish-side bug either
    way, never worth failing a whole ingest over -- only logged at debug)
    all fall back to that legacy `team` key if there is one, or `""`
    (today's norm) if not.
    """
    if kind != "skill":
        return raw["team"]

    owner = raw.get("owner")
    if isinstance(owner, str) and owner.strip():
        return owner
    if owner is not None:
        log.debug(
            "catalog ingest: skill entry %r has invalid owner %r -- ignoring",
            raw.get("id"),
            owner,
        )
    return raw.get("team", "")


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


def distinct_skill_teams(engine: Engine) -> list[str]:
    """Sorted distinct non-empty team values across `kind='skill'` rows only
    (persona-metadata plan Task 1).

    Unlike `teams()` above — which spans every kind and doesn't filter out
    `""` — this is scoped to skills alone and excludes the still-un-owned
    majority (`team == ""`) rather than surfacing a blank entry. This is
    what `GET /api/teams` serves: the list of skill owners a picker can
    show or filter by, with no persona-only team ever leaking in.
    """
    ensure_fts(engine)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT DISTINCT team FROM catalog_fts "
                "WHERE kind = 'skill' AND team != '' ORDER BY team"
            )
        ).all()
    return [row[0] for row in rows]
