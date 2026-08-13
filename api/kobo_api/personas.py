"""Persona builder (Phase A Task 4): fixed form fields rendered through a
deterministic template into the personas bucket, plus the seeding function
the bootstrap uses to populate a fresh bucket with the packaged defaults
(`templates/personas/*.md`).

Both write paths (`create_persona`, `seed_default_personas`) are storage
only — neither ever calls `kobo_api.search.ingest_indexes`. That's a
deliberate split, not an oversight: `ingest_indexes` does a FULL rebuild of
every catalog kind it's handed, so a function here that ingested just the
personas catalog would silently wipe every skill entry out of search. The
route (`kobo_api.main`) does the ingest itself, right after a successful
create, with the FULL kind->bucket mapping — see `create_persona`'s
docstring for the detail. `seed_default_personas` is the same story: the
bootstrap's own ingest step follows it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from kobo_api.catalog import _parse_persona_file
from kobo_api.clients import StorageClient, StorageNotFound
from kobo_api.search import CatalogEntry

# A persona id: lowercase, starts with a letter, up to 40 chars of
# letters/digits/hyphens after that (trailing hyphens are fine — see
# persona_id's docstring for why that can legitimately happen after
# truncation; contrast with kobo_api.catalog.NAME_RE, which bans them).
PERSONA_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")

_TAG_RE = re.compile(r"^[a-z][a-z0-9-]{0,23}$")

_NON_ALNUM_RUN_RE = re.compile(r"[^a-z0-9]+")

_PACKAGE_PERSONAS_DIR = Path(__file__).parent / "templates" / "personas"

# --- tag derivation (Persona builder v2 addendum) --------------------------

# Every lowercase alphanumeric "word" (`_WORD_RE`) out of Name + Who-they-are
# + What-they-do is a tag candidate UNLESS it's one of these plain function
# words — including the single-letter/two-letter remnants a contraction
# splits into once the apostrophe itself is just a word boundary to
# `_WORD_RE` ("who's" -> "who" + "s", "they're" -> "they" + "re", "I've" ->
# "i" + "ve", "don't" -> "don" + "t", "we'll" -> "we" + "ll", "I'm" -> "i" +
# "m"). Deliberately NOT pruned further (no directional/temporal words like
# "down"/"before"/"again"): those can carry real meaning in a short persona
# blurb, and `_derive_tags`'s own longest-first ordering already pushes
# short filler words to the bottom before the cap(6) cuts them.
_STOPWORDS = frozenset(
    "a an the and or but nor of to in on at for with without by from as "
    "is are was were be been being "
    "who whom whose what which when where why how "
    "they them their theirs you your yours we us our ours i me my mine "
    "he him his she her hers it its this that these those itself himself "
    "herself myself yourself "
    "do does did done doing have has had having "
    "will would shall should could can may might must "
    "not no so if then than "
    "each every any all some one two three first second next last more "
    "most other another such only own same too very "
    "s t re ve ll d m just also".split()
)

_WORD_RE = re.compile(r"[a-z0-9]+")


class PersonaError(Exception):
    """A persona field is invalid in a way pydantic alone can't catch:
    currently just `persona_id` failing to derive a valid id from the
    submitted name (e.g. it slugifies to empty, or to something that
    starts with a digit)."""


class PersonaExists(Exception):
    """create_persona: a persona with this id is already in the index."""


class PersonaFields(BaseModel):
    """The Persona builder v2 form (addendum): four fields, down from
    seven. Role/Purpose/Tone/Tone-note/Boundaries/Tags are gone — Rules
    absorbs Boundaries' job (a short list of always/never one-liners), and
    tags are no longer client-supplied at all (see `_derive_tags`, called
    from `create_persona`, never from here — this model has no `tags`
    field, so there's nothing for a caller to smuggle a client-authored
    value through as)."""

    name: str = Field(min_length=1, max_length=60)
    who_they_are: str = Field(min_length=1, max_length=200)
    what_they_do: str = Field(min_length=1, max_length=500)
    # 0-8 optional one-liners ("things they always or never do") — Rules is
    # explicitly optional in the brief, unlike who_they_are/what_they_do.
    rules: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("who_they_are")
    @classmethod
    def _who_they_are_one_line(cls, v: str) -> str:
        if "\n" in v:
            raise ValueError("who_they_are must be a single line (no newlines)")
        return v

    @field_validator("rules")
    @classmethod
    def _rules_shape(cls, v: list[str]) -> list[str]:
        for item in v:
            if not (1 <= len(item) <= 200):
                raise ValueError(f"each rule must be 1-200 chars: {item!r}")
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
    the opening line must survive verbatim in the output (it's the
    agent-name placeholder substituted at provision time — the same
    convention the packaged templates already use, see
    `kobo_api.catalog.PersonaInfo.body`'s docstring, and the exact
    replacement `kobo_api.steps` later does with
    `body.replace("{name}", agent.name)`) — a `.format(name=...)` call would
    eat it. And `who_they_are`/`what_they_do`/`rules` are free-form user
    text that may itself contain `{`/`}` — running the assembled template
    back through `.format()` would raise (or silently misinterpret) on
    those. Concatenating the field values in as plain strings sidesteps
    both.

    Emits the "house skeleton" the Persona builder v2 addendum specifies: a
    fixed, org-neutral voice paragraph (structural template text, not
    client branding), then a What-you-do section, then an optional Rules
    section — Rules is omitted entirely (no heading, no blank line) when
    `fields.rules` is empty, the same conditional-section convention v1's
    Boundaries used.
    """
    lines = [
        "You are {name}, " + fields.who_they_are + ".",
        "",
        "Speak in the first person, plainly. Lead with the answer; skip",
        "throat-clearing. Say \"I don't know yet\" when it's true.",
        "",
        "## What you do",
        "",
        fields.what_they_do,
    ]
    if fields.rules:
        lines.append("")
        lines.append("## Rules")
        lines.append("")
        lines.extend("- " + rule for rule in fields.rules)
    return "\n".join(lines) + "\n"


def _derive_tags(fields: PersonaFields, slug: str) -> list[str]:
    """Auto-derive 1-6 persona tags server-side (Persona builder v2
    addendum: tags are no longer a form field at all — the UI sends none).

    Every lowercase alphanumeric word out of Name + Who-they-are +
    What-they-do (`_WORD_RE`) is a candidate, minus `_STOPWORDS`, minus
    anything that couldn't itself pass as a tag (`_TAG_RE` — must start
    with a letter, max 24 chars, the exact shape client-supplied tags were
    always required to have back when there was a tags field at all),
    de-duplicated (first occurrence wins), then sorted longest-word-first —
    `sorted(..., reverse=True)` is still a *stable* sort, so same-length
    words keep that first-occurrence order rather than reversing too — and
    capped at 6.

    If NONE of that survives (every word in the source text was a
    stopword), the persona's own slug is the fallback tag, truncated to the
    same 24-char cap: `slug` is already lowercase alphanumeric-or-hyphen and
    always starts with a letter (`PERSONA_ID_RE`), so `slug[:24]` is always
    `_TAG_RE`-valid — guaranteeing at least one tag either way.
    """
    text = f"{fields.name} {fields.who_they_are} {fields.what_they_do}".lower()
    seen: dict[str, None] = {}
    for word in _WORD_RE.findall(text):
        if word in _STOPWORDS or not _TAG_RE.fullmatch(word):
            continue
        seen.setdefault(word, None)
    ranked = sorted(seen, key=len, reverse=True)
    return ranked[:6] if ranked else [slug[:24]]


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
    table. The route (`kobo_api.main`) re-ingests with the FULL
    kind->bucket mapping right after this returns successfully (Task 4
    controller decision #1 — the change that closes that hazard; see
    test_api_personas.py's regression test proving a pre-existing skill
    entry survives a persona create).

    Raises `PersonaError` if `fields.name` doesn't slugify to a valid id,
    `PersonaExists` if that id is already in the index. Tags are derived
    here (`_derive_tags`), not read off `fields` — Persona builder v2 has no
    client-supplied tags field at all.
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

    tags = _derive_tags(fields, id_)
    index.append(
        {
            "id": id_,
            "name": fields.name,
            "description": fields.who_they_are,
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
        description=fields.who_they_are,
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
                metadata={"team": "shared", "creator": "kobo"},
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
