"""Persona builder (Phase A Task 4): fixed form fields rendered through a
deterministic template into the personas bucket, plus the seeding function
the bootstrap uses to populate a fresh bucket with the packaged defaults
(`templates/personas/*.md`).

Both write paths (`create_persona`, `seed_default_personas`) are storage
only — neither ever calls `pnyx_api.search.ingest_indexes`. That's a
deliberate split, not an oversight: `ingest_indexes` does a FULL rebuild of
every catalog kind it's handed, so a function here that ingested just the
personas catalog would silently wipe every skill entry out of search. The
route (`pnyx_api.main`) does the ingest itself, right after a successful
create, with the FULL kind->bucket mapping — see `create_persona`'s
docstring for the detail. `seed_default_personas` is the same story: the
bootstrap's own ingest step follows it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from pnyx_api.catalog import _parse_persona_file
from pnyx_api.clients import StorageClient, StorageNotFound
from pnyx_api.search import CatalogEntry

TONES = ("warm", "direct", "playful", "formal")

# A persona id: lowercase, starts with a letter, up to 40 chars of
# letters/digits/hyphens after that (trailing hyphens are fine — see
# persona_id's docstring for why that can legitimately happen after
# truncation; contrast with pnyx_api.catalog.NAME_RE, which bans them).
PERSONA_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")

_TAG_RE = re.compile(r"^[a-z][a-z0-9-]{0,23}$")

_NON_ALNUM_RUN_RE = re.compile(r"[^a-z0-9]+")

_PACKAGE_PERSONAS_DIR = Path(__file__).parent / "templates" / "personas"


class PersonaError(Exception):
    """A persona field is invalid in a way pydantic alone can't catch:
    currently just `persona_id` failing to derive a valid id from the
    submitted name (e.g. it slugifies to empty, or to something that
    starts with a digit)."""


class PersonaExists(Exception):
    """create_persona: a persona with this id is already in the index."""


class PersonaFields(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    role: str = Field(min_length=1, max_length=120)
    purpose: str = Field(min_length=1, max_length=500)
    tone: Literal["warm", "direct", "playful", "formal"]
    tone_note: str = Field(default="", max_length=200)
    boundaries: list[str] = Field(default_factory=list, max_length=8)
    # Deliberately no default (unlike boundaries/tone_note, both explicitly
    # documented as optional in the brief): the brief's own comment for this
    # field never says "optional", so at least one tag is required — a
    # default of `[]` here would make a completely tag-less submission
    # silently pass pydantic (defaults aren't validated unless the caller
    # opts into validate_default), which would contradict "1-6 items".
    tags: list[str] = Field(min_length=1, max_length=6)

    @field_validator("role")
    @classmethod
    def _role_one_line(cls, v: str) -> str:
        if "\n" in v:
            raise ValueError("role must be a single line (no newlines)")
        return v

    @field_validator("boundaries")
    @classmethod
    def _boundaries_shape(cls, v: list[str]) -> list[str]:
        for item in v:
            if not (1 <= len(item) <= 200):
                raise ValueError(f"each boundary must be 1-200 chars: {item!r}")
        return v

    @field_validator("tags")
    @classmethod
    def _tags_shape(cls, v: list[str]) -> list[str]:
        for tag in v:
            if not _TAG_RE.fullmatch(tag):
                raise ValueError(f"invalid tag: {tag!r}")
        return v


def persona_id(name: str) -> str:
    """Slugify `name` into a persona id: lowercase, runs of non-alphanumeric
    characters collapsed to a single "-", leading/trailing "-" stripped,
    then capped to 40 characters (in that order, per the brief) — raises
    `PersonaError` when the result doesn't satisfy `PERSONA_ID_RE` (e.g.
    `name` slugifies to nothing at all, or to something starting with a
    digit).

    The 40-char cap is applied AFTER stripping, so truncation can leave a
    trailing "-" (when the cut lands right on a separator) — that's still a
    valid id: PERSONA_ID_RE has no rule against a trailing hyphen.
    """
    slug = _NON_ALNUM_RUN_RE.sub("-", name.lower()).strip("-")[:40]
    if not PERSONA_ID_RE.fullmatch(slug):
        raise PersonaError(f"could not derive a valid persona id from name: {name!r}")
    return slug


def render_persona(fields: PersonaFields) -> str:
    """Render `fields` into a persona.md body: deterministic (the same
    fields always render identical bytes) and built by plain string
    concatenation, NEVER `str.format`.

    Two things `.format()` would get wrong: the literal `{name}` token in
    the "Identity" line must survive verbatim in the output (it's the
    agent-name placeholder substituted at provision time — the same
    convention the packaged templates already use, see
    `pnyx_api.catalog.PersonaInfo.body`'s docstring) — a `.format(name=...)`
    call would eat it. And `purpose`/`boundaries` are free-form user text
    that may itself contain `{`/`}` — running the assembled template back
    through `.format()` would raise (or silently misinterpret) on those.
    Concatenating the field values in as plain strings sidesteps both.
    """
    tone_note_suffix = " " + fields.tone_note if fields.tone_note else ""
    lines = [
        "<!-- description: " + fields.role + " -->",
        "# Identity",
        "You are {name} — " + fields.role + ".",
        "",
        "# What you do",
        fields.purpose,
        "",
        "# How you speak",
        "Your tone is " + fields.tone + "." + tone_note_suffix,
    ]
    if fields.boundaries:
        lines.append("")
        lines.append("# Boundaries")
        lines.extend("- " + boundary for boundary in fields.boundaries)
    return "\n".join(lines) + "\n"


def _read_index(storage: StorageClient, bucket: str, prefix: str) -> list[dict]:
    """The current `<prefix>index.json` entries, or `[]` when the object
    isn't there yet (a bucket that's never had a persona published)."""
    try:
        data = storage.read_bytes(bucket, f"{prefix}index.json")
    except StorageNotFound:
        return []
    parsed = json.loads(data)
    return list(parsed.get("entries", []))


def _write_index(storage: StorageClient, bucket: str, prefix: str, entries: list[dict]) -> None:
    """Rewrite `<prefix>index.json` with exactly `entries`, sorted by id.

    Read-modify-write, not a compare-and-swap: two callers racing each
    other (two room users creating a persona at nearly the same instant)
    can both read the same starting index and each write back their own
    version, and the second write simply wins — the first caller's own
    entry, appended before its write, is NOT lost (each caller's `entries`
    already includes it, since both started from a read that had it...
    except when the two creates are concurrent enough that neither read
    saw the other's write yet, in which case whichever write lands second
    overwrites the first's addition). That's last-write-wins, acceptable at
    catalog scale (Task 4 controller decision #5) — no optimistic-locking/
    generation check is implemented here.
    """
    payload = {"entries": sorted(entries, key=lambda e: e["id"])}
    storage.write_bytes(
        bucket,
        f"{prefix}index.json",
        json.dumps(payload).encode(),
        content_type="application/json",
    )


def create_persona(
    storage: StorageClient,
    *,
    bucket: str,
    prefix: str = "",
    fields: PersonaFields,
    team: str,
) -> CatalogEntry:
    """Write a room-authored persona: storage only, never touches search.

    `search.ingest_indexes` does a full catalog rebuild from every
    configured bucket at once (Task 3) — calling it here with just the
    personas bucket would silently wipe every skill entry out of the FTS
    table. The route (`pnyx_api.main`) re-ingests with the FULL
    kind->bucket mapping right after this returns successfully (Task 4
    controller decision #1 — the change that closes that hazard; see
    test_api_personas.py's regression test proving a pre-existing skill
    entry survives a persona create).

    Raises `PersonaError` if `fields.name` doesn't slugify to a valid id,
    `PersonaExists` if that id is already in the index.
    """
    id_ = persona_id(fields.name)

    index = _read_index(storage, bucket, prefix)
    if any(entry["id"] == id_ for entry in index):
        raise PersonaExists(f"a persona with id {id_!r} already exists")

    body = render_persona(fields)
    storage.write_bytes(
        bucket,
        f"{prefix}{id_}/persona.md",
        body.encode(),
        metadata={"team": team, "creator": "room"},
        content_type="text/markdown",
    )

    tags = list(fields.tags)
    index.append(
        {
            "id": id_,
            "name": fields.name,
            "description": fields.role,
            "team": team,
            "tags": tags,
            "builtin": False,
        }
    )
    _write_index(storage, bucket, prefix, index)

    return CatalogEntry(
        kind="persona",
        id=id_,
        name=fields.name,
        description=fields.role,
        team=team,
        tags=tags,
        builtin=False,
    )


def seed_default_personas(storage: StorageClient, *, bucket: str, prefix: str = "") -> dict:
    """Seed the packaged default personas (`templates/personas/*.md`) into
    `bucket`, idempotently: a `<prefix><id>/persona.md` that's already
    there is left completely untouched (never overwritten — an operator,
    or a prior seed run, may have hand-edited it), only an absent one gets
    written, byte-for-byte from the packaged file (so its `{name}`
    placeholder survives exactly as the un-rendered templates already carry
    it). `index.json` is rewritten every call, MERGING each packaged
    persona's entry into whatever's already there (`builtin: True`, `team:
    "shared"`, `tags: []`) rather than replacing the file outright — a
    room-authored persona already in the index survives a seed run.

    Like `create_persona`, this never touches search — the bootstrap's own
    ingest step (Task 4 controller decision #2) follows it.

    Returns `{"seeded": n, "skipped": n}` — the bootstrap step's own
    done/skipped report, and how its idempotence gets proven (a second run
    is all-skipped).
    """
    seeded = 0
    skipped = 0
    index = _read_index(storage, bucket, prefix)
    index_by_id = {entry["id"]: entry for entry in index}

    # Inlines what `catalog.list_personas(None)` used to do (Task 5 deleted
    # it along with the old local-clone directory-scanning path it also
    # supported) — glob the packaged templates, parsing each with the same
    # `_parse_persona_file` helper, skipping a malformed one rather than
    # raising.
    for path in sorted(_PACKAGE_PERSONAS_DIR.glob("*.md")):
        info = _parse_persona_file(path)
        if info is None:
            continue
        key = f"{prefix}{info.id}/persona.md"
        try:
            storage.read_bytes(bucket, key)
            exists = True
        except StorageNotFound:
            exists = False

        if exists:
            skipped += 1
        else:
            storage.write_bytes(
                bucket,
                key,
                path.read_bytes(),
                metadata={"team": "shared", "creator": "pnyx"},
                content_type="text/markdown",
            )
            seeded += 1

        # "name" has no separate concept in the packaged templates (only id
        # + description are parsed from them) — the id itself is used as
        # the display name, same as how these ship with no other naming
        # convention to draw from.
        index_by_id[info.id] = {
            "id": info.id,
            "name": info.id,
            "description": info.description,
            "team": "shared",
            "tags": [],
            "builtin": True,
        }

    _write_index(storage, bucket, prefix, list(index_by_id.values()))
    return {"seeded": seeded, "skipped": skipped}
