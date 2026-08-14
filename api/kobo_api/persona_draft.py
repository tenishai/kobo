"""Persona drafting (Persona v3 addendum): the generator
(`kobo_api.clients.VertexNameClient.draft`) supplies a best-effort
name/who_they_are/what_they_do object; this module is what guarantees the
room's contract of "the room always drafts SOMETHING editable" — validating
each field against `kobo_api.personas.PersonaFields`' real bounds and
falling each one back INDIVIDUALLY to a deterministic, job-derived value
when it's missing, mistyped, or out of bounds.

Kept separate from `kobo_api.main` (mirrors `kobo_api.namegen`'s own split
from the name-suggestions route: pure validation/fallback logic here,
route wiring/response-shape there) so the fallback matrix below is testable
with no FastAPI/DB/HTTP involved at all.
"""

from __future__ import annotations

import re

from kobo_api.personas import PersonaError, persona_id

# Mirrors PersonaFields' real Field bounds (kobo_api.personas) — duplicated
# as plain constants here rather than imported, the same "mirror the bounds,
# don't cross-import validation" convention `NameSuggestionsRequest`
# (kobo_api.main) already uses for who_they_are/what_they_do.
_NAME_MAX = 60
_WHO_MAX = 200
_WHAT_MAX = 500

_WORD_RE = re.compile(r"[a-z0-9]+")

# The fallback name when the job text has no alphanumeric words at all
# (e.g. pure punctuation) — PersonaFields.name still needs >=1 char.
_FALLBACK_NAME_DEFAULT = "new-agent"

# How many leading words of the job text the fallback name slugifies —
# enough to be recognizable, short enough to comfortably clear
# PersonaFields.name's 60-char cap even with long words.
_FALLBACK_NAME_MAX_WORDS = 5

# A "clause" boundary for the fallback who_they_are line: the first
# sentence-ish break in the job text (never a comma — that would cut most
# short job descriptions down to almost nothing).
_CLAUSE_END_RE = re.compile(r"[.!?;\n]")


def _fallback_name(job: str) -> str:
    """Deterministic fallback persona name: the leading words of `job`,
    lowercased and hyphen-joined (e.g. "Help customers with billing." ->
    "help-customers-with-billing"), clamped to PersonaFields.name's bound.

    Unlike an agent name (`kobo_api.namegen`'s `_FALLBACK_NAMES`/
    `validate_name`), a drafted persona name has no 2-24-char slug shape to
    respect and no taken/reserved collision to avoid — it only needs to be
    a valid PersonaFields.name (1-60 chars), since it lands in the SAME
    editable v2 preview a hand-typed name would.

    Additionally, the name must pass persona_id() validation, which requires
    it to start with a letter after slugification. If the job text's leading
    words slugify to something starting with a digit (e.g. "3D print parts"
    -> "3d-print-parts"), prefix "the-" to ensure it passes; if even that
    fails, fall back to "new-agent".
    """
    words = _WORD_RE.findall(job.lower())[:_FALLBACK_NAME_MAX_WORDS]
    slug = "-".join(words) if words else _FALLBACK_NAME_DEFAULT

    # Validate against persona_id() — if it fails, try with "the-" prefix
    try:
        persona_id(slug)
        return slug[:_NAME_MAX]
    except PersonaError:
        # If the slug is digit-led, prefix "the-" to make it valid
        the_prefixed = f"the-{slug}"
        try:
            persona_id(the_prefixed)
            return the_prefixed[:_NAME_MAX]
        except PersonaError:
            # Even the "the-" prefix failed; fall back to the default
            return _FALLBACK_NAME_DEFAULT


def _first_clause(job: str) -> str:
    """The first clause of `job` (up to the first `.`/`!`/`?`/`;`/newline,
    or the whole text if none), whitespace-collapsed to a single line —
    `who_they_are` must be one line, so this can never hand back embedded
    newlines even when `job` itself has them.

    Falls back to the whole (collapsed) job text if the clause found before
    the first boundary is empty (e.g. `job` starts with "." itself) — an
    empty clause would make for a useless who-line otherwise. That fallback
    also strips any leading clause-boundary punctuation of its own (the
    same "." that made the first slice empty), so the who-line never reads
    "...is . lead with something" — just "...is lead with something".
    """
    match = _CLAUSE_END_RE.search(job)
    raw_clause = job[: match.start()] if match else job
    clause = " ".join(raw_clause.split())
    if not clause:
        clause = " ".join(job.split()).lstrip(".!?;").strip()
    return clause


def _fallback_who(job: str) -> str:
    """Deterministic fallback who_they_are: "an agent whose whole job is
    {job's first clause}", clamped to PersonaFields.who_they_are's bound
    (a long first clause could otherwise push the templated line over 200
    chars even though `job` itself is capped at 500).

    When the job text is all punctuation (the first clause is empty after
    stripping), returns "an agent with one clear job" instead of leaving
    the template dangling.
    """
    clause = _first_clause(job)
    if not clause:
        who = "an agent with one clear job"
    else:
        who = f"an agent whose whole job is {clause}"
    return who[:_WHO_MAX]


def _fallback_what(job: str) -> str:
    """Deterministic fallback what_they_do: `job` verbatim, clamped to
    PersonaFields.what_they_do's bound. In practice never actually
    truncates — the draft endpoint's own `job` field shares the exact same
    1-500 bound, so any `job` that reached here already fits; the clamp is
    just defense-in-depth against a caller that skips that check."""
    return job[:_WHAT_MAX]


def _extract_field(
    raw: dict, key: str, *, max_len: int, reject_newline: bool = False
) -> str | None:
    """`raw[key]`, validated against PersonaFields' real bounds for this
    field, or `None` when it's missing, not a string, empty after
    stripping, over `max_len`, or (when `reject_newline`) contains a
    newline — the exact set of ways PersonaFields itself would reject the
    value, checked here so each field can fall back independently instead
    of one bad field failing the whole draft.
    """
    value = raw.get(key)
    if not isinstance(value, str):
        return None
    value = value.strip()
    if reject_newline and "\n" in value:
        return None
    if not (1 <= len(value) <= max_len):
        return None
    return value


def finalize_draft(raw: dict | None, *, job: str) -> dict:
    """Turn the generator's best-effort `raw` (or `None`/anything non-dict,
    when the generator is unconfigured or raised `ClientError`) into a
    `{"name", "who_they_are", "what_they_do"}` dict that's ALWAYS
    PersonaFields-valid.

    Each of the three fields is validated independently
    (`_extract_field`) and falls back to its own deterministic, job-derived
    value on its own — a generator that nails two fields and mangles the
    third keeps the two good ones, exactly the addendum's "invalid fields
    individually fall back" contract. Passing `raw=None` (or `{}`) falls
    every field back at once, which is also exactly the "generator down/
    unusable" full deterministic fallback the addendum describes — the same
    function serves both cases, since an empty/absent dict fails every
    field's presence check identically.

    Name fields are further validated against persona_id() — a name that
    passes PersonaFields bounds may still fail persona_id if it slugifies
    to a digit-led string (e.g. "3D print parts"). Any such name falls back
    to the job-derived fallback.
    """
    raw = raw if isinstance(raw, dict) else {}

    name = _extract_field(raw, "name", max_len=_NAME_MAX)
    # Validate that the extracted name also passes persona_id()
    if name is not None:
        try:
            persona_id(name)
        except PersonaError:
            # Name failed persona_id validation; treat as None to trigger fallback
            name = None

    who = _extract_field(raw, "who_they_are", max_len=_WHO_MAX, reject_newline=True)
    what = _extract_field(raw, "what_they_do", max_len=_WHAT_MAX)

    return {
        "name": name if name is not None else _fallback_name(job),
        "who_they_are": who if who is not None else _fallback_who(job),
        "what_they_do": what if what is not None else _fallback_what(job),
    }
