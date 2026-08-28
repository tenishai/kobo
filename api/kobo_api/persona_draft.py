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

from kobo_api.personas import PersonaError, PersonaFields, persona_id
from kobo_api.purpose_tags import normalize_tags

# _NAME_MAX/_WHO_MAX mirror PersonaFields' real Field bounds (kobo_api.
# personas) — duplicated as plain constants here rather than imported, the
# same "mirror the bounds, don't cross-import validation" convention
# `NameSuggestionsRequest` (kobo_api.main) already uses for who_they_are/
# what_they_do.
#
# _WHAT_MAX is deliberately NOT mirrored to PersonaFields.what_they_do's own
# bound (49152). This constant governs how much of the GENERATOR's own
# draft output gets accepted verbatim before falling back to the
# job-derived text (`_fallback_what`). Create-flow polish (2026-08-28):
# the draft is now a FULL working brief (the prompt asks for 40-120 lines
# of handbook-depth markdown), so the bound sits at 12000 chars — room for
# every reasonable draft, still comfortably under the render ceiling that
# create_persona enforces for real.
_NAME_MAX = 60
_WHO_MAX = 200
_WHAT_MAX = 12000

# purpose_tags (persona-metadata plan, Task 2): the draft's own cap, narrower
# than the create route's MAX_CREATE_TAGS (kobo_api.purpose_tags) — a
# best-effort model suggestion is silently trimmed to this many rather than
# rejected, since there's no caller input to reject here at all.
_DRAFT_TAG_CAP = 5

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


def _slugify_name_candidate(words: list[str]) -> str:
    """Shared tail of every deterministic name fallback in this module
    (`_fallback_name` below, `_fallback_distilled_name` further down):
    hyphen-join `words` (already lowercase alphanumeric — both callers
    extract via `_WORD_RE`) into a slug, validate it against `persona_id()`
    — retrying with a "the-" prefix when the plain slug is digit-led
    (`persona_id()` rejects that) — and fall back to
    `_FALLBACK_NAME_DEFAULT` when neither passes. The accepted slug is
    clamped to PersonaFields.name's bound (`_NAME_MAX`) before it's
    returned.
    """
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
    fails, fall back to "new-agent". See `_slugify_name_candidate` for that
    shared slugify-validate-clamp pipeline.
    """
    words = _WORD_RE.findall(job.lower())[:_FALLBACK_NAME_MAX_WORDS]
    return _slugify_name_candidate(words)


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


def _fallback_tags(name: str, who: str, what: str) -> list[str]:
    """Fallback tier for purpose_tags: EMPTY (create-flow polish,
    2026-08-28). The retired tier noun-derived tags from the persona text
    and produced entries like "embarrassment" at scale — an empty tag list
    is honest; a derived one lied. Tags come from the model's draft or the
    human's hand, or not at all. The arguments stay so every call site
    reads unchanged."""
    return []


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
    `{"name", "who_they_are", "what_they_do", "purpose_tags"}` dict that's
    ALWAYS PersonaFields-valid (`purpose_tags` isn't a PersonaFields field
    at all — see `kobo_api.personas.PersonaFields`'s own docstring — so it
    rides alongside, never through, that validity check).

    Each of the three PersonaFields fields is validated independently
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

    `purpose_tags` is a different shape of field (a list, not a bounded
    string) with a different fallback story: a model-supplied list is
    normalized (`kobo_api.purpose_tags.normalize_tags`) and capped at
    `_DRAFT_TAG_CAP`; when that normalizes to nothing at all (missing,
    not a list, or every entry unusable), the fallback tier is "the
    persona's own tags" (`_fallback_tags`) — derived from THIS draft's own
    finalized name/who_they_are/what_they_do, not the raw job text
    directly.
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

    final_name = name if name is not None else _fallback_name(job)
    final_who = who if who is not None else _fallback_who(job)
    final_what = what if what is not None else _fallback_what(job)

    tags = normalize_tags(raw.get("purpose_tags"))[:_DRAFT_TAG_CAP]
    if not tags:
        tags = _fallback_tags(final_name, final_who, final_what)

    return {
        "name": final_name,
        "who_they_are": final_who,
        "what_they_do": final_what,
        "purpose_tags": tags,
    }


# --- distill (persona-first station addendum) --------------------------------
#
# The station's OTHER entry point: the operator arrives with a persona
# DOCUMENT already written (uploaded or pasted) rather than a one-line job
# description. `finalize_distill` is `finalize_draft`'s counterpart for that
# case — it produces discovery metadata (name/who_they_are/purpose_tags) from
# the document itself, following the exact same "each field validates and
# falls back on its own, the room never blocks" idiom `finalize_draft` uses.
# There is no `what_they_do` here at all: the document the operator handed
# over already IS the persona body, so there's nothing to draft for that
# field — the generator (`kobo_api.clients.VertexNameClient.distill`) is
# only ever asked to pull discovery metadata OUT of it.

# A markdown ATX heading line ("#" through "######", a space/tab, then the
# heading text) — used by `_fallback_distilled_name` to find the document's
# own title anywhere in the body (`re.search`, not anchored to line one: a
# short preamble before the first heading is common enough in a
# hand-written persona document that this shouldn't miss it).
_MARKDOWN_HEADING_RE = re.compile(r"^#{1,6}[ \t]+(.+)$", re.MULTILINE)

# Same shape as _MARKDOWN_HEADING_RE above, but matched against a single
# stripped line (`.match`, not `.search`) — used by `_fallback_distilled_who`
# to skip past heading lines while scanning for the document's first real
# line of prose. Also matches a heading marker with no text after it at all
# (e.g. a bare "#"), which `_MARKDOWN_HEADING_RE` above does not (that one
# requires `(.+)$`), since a heading with nothing to say still isn't a line
# of prose worth using as who_they_are.
_MARKDOWN_HEADING_LINE_RE = re.compile(r"^#{1,6}(?:[ \t]|$)")

# How many leading words of the whole document `_fallback_distilled_name`
# slugifies when there's no heading to use instead — short on purpose (a
# document's opening words are prose, not already name-shaped, unlike a
# heading's own text).
_DISTILL_NO_HEADING_WORDS = 2

# The plain fallback who_they_are text (persona-first station addendum) when
# `body` has no non-blank, non-heading line at all — mirrors
# `_fallback_who`'s own dangling-template rescue text for a job description
# that's all punctuation.
_DISTILL_WHO_FALLBACK_TEXT = "an agent with one clear job"


def _fallback_distilled_name(body: str) -> str:
    """Deterministic fallback persona name for a distilled persona document:
    the document's first markdown heading (found anywhere in `body`, not
    just its first line), or — when there's no heading at all, or a heading
    marker with no text after it — the document's own first two words
    instead. Either source runs through the same slugify-validate-clamp
    pipeline `_fallback_name` uses (`_slugify_name_candidate`).
    """
    heading_match = _MARKDOWN_HEADING_RE.search(body)
    if heading_match:
        words = _WORD_RE.findall(heading_match.group(1).lower())[:_FALLBACK_NAME_MAX_WORDS]
        if words:
            return _slugify_name_candidate(words)
    words = _WORD_RE.findall(body.lower())[:_DISTILL_NO_HEADING_WORDS]
    return _slugify_name_candidate(words)


def _fallback_distilled_who(body: str) -> str:
    """Deterministic fallback who_they_are for a distilled persona document:
    the first non-blank, non-heading line of `body`, clamped to
    PersonaFields.who_they_are's bound (`_WHO_MAX`) — already a single line
    by construction (one line of the body, taken as-is), so there's no
    clause-boundary search here the way `_fallback_who`'s job-description
    fallback needs.

    Falls back to `_DISTILL_WHO_FALLBACK_TEXT` when `body` has no such line
    at all (every line is blank or itself a heading).
    """
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _MARKDOWN_HEADING_LINE_RE.match(stripped):
            continue
        return stripped[:_WHO_MAX]
    return _DISTILL_WHO_FALLBACK_TEXT


def finalize_distill(raw: dict | None, *, body: str) -> dict:
    """Turn the generator's best-effort `raw` (or `None`/anything non-dict,
    when the generator is unconfigured or raised `ClientError`) into a
    `{"name", "who_they_are", "purpose_tags"}` dict — the persona-first
    station's discovery-metadata counterpart to `finalize_draft` above, for
    when the operator hands over a persona document rather than a one-line
    job description. Deliberately no `what_they_do` key at all: the
    document itself already IS the persona body, so there's nothing to
    draft or fall back for that field here.

    `name`/`who_they_are` each validate and fall back independently, same
    "one field's trouble doesn't take the others down with it" idiom
    `finalize_draft` uses — see `_extract_field`'s docstring for exactly
    which malformed shapes fall back (missing, non-string, empty after
    stripping, over bound, or — `who_they_are` only — containing a
    newline). A name that passes PersonaFields' own bound but fails
    `persona_id()` (e.g. it slugifies to a digit-led string) falls back the
    same way.

    `purpose_tags` fallback tier is a plain empty list, full stop — UNLIKE
    `finalize_draft`'s own fallback tier (the persona's own tags, derived
    from its finalized name/who_they_are/what_they_do): a distill has no
    what_they_do to derive anything from, so there's no second tier to fall
    through to here.
    """
    raw = raw if isinstance(raw, dict) else {}

    name = _extract_field(raw, "name", max_len=_NAME_MAX)
    if name is not None:
        try:
            persona_id(name)
        except PersonaError:
            name = None

    who = _extract_field(raw, "who_they_are", max_len=_WHO_MAX, reject_newline=True)

    final_name = name if name is not None else _fallback_distilled_name(body)
    final_who = who if who is not None else _fallback_distilled_who(body)

    tags = normalize_tags(raw.get("purpose_tags"))[:_DRAFT_TAG_CAP]

    return {
        "name": final_name,
        "who_they_are": final_who,
        "purpose_tags": tags,
    }
