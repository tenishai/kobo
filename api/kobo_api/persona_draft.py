"""Persona drafting (Persona v3 addendum): the generator
(`kobo_api.clients.VertexNameClient.draft`) supplies a best-effort
name/who_they_are/what_they_do/rules object; this module is what guarantees
the room's contract of "the room always drafts SOMETHING editable" —
validating each field against `kobo_api.personas.PersonaFields`' real
bounds and falling each one back INDIVIDUALLY to a deterministic,
source-derived value when it's missing, mistyped, or out of bounds.

One way in (2026-11-17). The draft's source is whatever the person has: a
few sentences about the job, or a whole document they already wrote. There
used to be a second path for the document (`distill`), which pulled a name
and a who-line out of it and stored the document itself, as it came, as
what-they-do; a persona made that way kept its own title, its own opening
and its own sections under the house's. Now a document is material for the
same draft, rewritten into the same three parts as every other persona.

Kept separate from `kobo_api.main` (mirrors `kobo_api.namegen`'s own split
from the name-suggestions route: pure validation/fallback logic here,
route wiring/response-shape there) so the fallback matrix below is testable
with no FastAPI/DB/HTTP involved at all.
"""

from __future__ import annotations

import re

from kobo_api.personas import (
    PersonaError,
    clean_rule,
    clean_who,
    house_body,
    persona_id,
)
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
# create_persona enforces for real. Raised to 30000 (2026-11-17): a
# draft made from a document keeps the document's working detail, up to
# 120 lines, and a real one came to 11,500 characters. The bound is on
# characters only: a reply that overshoots the prompt's 120 lines can
# still render past the 200-line ceiling, and the form then fills and
# Create refuses it with the line count, for the person to trim.
_NAME_MAX = 60
_WHO_MAX = 200
_WHAT_MAX = 30000

# purpose_tags (persona-metadata plan, Task 2): the draft's own cap, narrower
# than the create route's MAX_CREATE_TAGS (kobo_api.purpose_tags) — a
# best-effort model suggestion is silently trimmed to this many rather than
# rejected, since there's no caller input to reject here at all.
_DRAFT_TAG_CAP = 5

# The most rules one draft hands back. The prompt asks for eight to twelve
# from a description and every rule a document states, up to forty; a
# reply with more is cut here rather than refused, the same as the tags.
_DRAFT_RULES_CAP = 40

_WORD_RE = re.compile(r"[a-z0-9]+")

# The fallback name when the job text has no alphanumeric words at all
# (e.g. pure punctuation) — PersonaFields.name still needs >=1 char.
_FALLBACK_NAME_DEFAULT = "new-agent"

# How many leading words of the job text the fallback name slugifies.
# Short-names ruling (2026-09-24): a persona name is one or two words, so
# the fallback takes two, and a model-drafted name with more is cut to its
# LAST two (`_shorten_name`) -- the tail of "code-grounded-player-intel" is
# the noun, the head is the trait list that belongs in who_they_are.
_FALLBACK_NAME_MAX_WORDS = 2
_NAME_MAX_WORDS = 2

# A "clause" boundary for the fallback who_they_are line: the first
# sentence-ish break in the job text (never a comma — that would cut most
# short job descriptions down to almost nothing).
_CLAUSE_END_RE = re.compile(r"[.!?;\n]")


def _slugify_name_candidate(words: list[str]) -> str:
    """Shared tail of every deterministic name fallback in this module
    (`_fallback_name` below):
    hyphen-join `words` (already lowercase alphanumeric — the caller
    extracts via `_WORD_RE`) into a slug, validate it against `persona_id()`
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


def _shorten_name(name: str) -> str:
    """Cut a model-drafted `name` down to its last `_NAME_MAX_WORDS` words
    when it came back longer (words = alphanumeric runs). A name already
    within the cap is returned untouched, spacing and case included, so a
    short hand-shaped name like "Ada (Release)" still slugifies as before.
    """
    words = _WORD_RE.findall(name.lower())
    if len(words) <= _NAME_MAX_WORDS:
        return name
    return _slugify_name_candidate(words[-_NAME_MAX_WORDS:])


def _fallback_name(job: str) -> str:
    """Deterministic fallback persona name: the leading words of `job`,
    lowercased and hyphen-joined (e.g. "Help customers with billing." ->
    "help-customers"), clamped to PersonaFields.name's bound.

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
    if _is_document(job):
        return _fallback_document_name(job)
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
    if _is_document(job):
        return _fallback_document_who(job)
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
    return house_body(job[:_WHAT_MAX])


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
    if key == "who_they_are":
        # The renderer prepends "You are {name}, " to this line -- a draft
        # that opens with its own "You are" would read doubled in the
        # composed persona ("You are X, You are the ..."). Observed live
        # 2026-08-28. The one cleaner the stored field uses
        # (`kobo_api.personas.clean_who`), so the bounds check sees the
        # real line: it keeps the article ("a calm ..."), which the first
        # version of this strip took off along with "You are".
        value = clean_who(value)
    elif key == "what_they_do":
        value = house_body(value)
    if reject_newline and "\n" in value:
        return None
    if not (1 <= len(value) <= max_len):
        return None
    return value


def finalize_draft(raw: dict | None, *, job: str) -> dict:
    """Turn the generator's best-effort `raw` (or `None`/anything non-dict,
    when the generator is unconfigured or raised `ClientError`) into a
    `{"name", "who_they_are", "what_they_do", "rules", "purpose_tags"}` dict
    that's ALWAYS PersonaFields-valid (`purpose_tags` isn't a PersonaFields field
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

    `rules` is the model's own list, each entry cleaned to one bullet's
    text (`kobo_api.personas.clean_rule`), anything that is not a one-line
    string dropped, capped at `_DRAFT_RULES_CAP`. There is no deterministic
    fallback for rules: a sentence about a job does not contain them, so an
    unusable reply hands back none and the form asks for them.
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
        else:
            name = _shorten_name(name)

    who = _extract_field(raw, "who_they_are", max_len=_WHO_MAX, reject_newline=True)
    what = _extract_field(raw, "what_they_do", max_len=_WHAT_MAX)

    final_name = name if name is not None else _fallback_name(job)
    if who is not None:
        # The renderer writes "You are {name}, " ahead of this line, and a
        # reply sometimes opens it with the name again ("renewal-helper, a
        # specialist who ...", seen on dev 2026-11-17).
        # Only ahead of an article, so a line that lists the name's own
        # word ("triage, routing and follow-up for ...") is left alone.
        for said in (raw.get("name"), final_name):
            if isinstance(said, str) and said.strip():
                repeat = re.match(
                    re.escape(said.strip()) + r",\s+(?=(?:a|an|the)\b)", who, re.IGNORECASE
                )
                if repeat:
                    who = who[repeat.end():]
                    break
    final_who = who if who is not None else _fallback_who(job)
    final_what = what if what is not None else _fallback_what(job)

    tags = normalize_tags(raw.get("purpose_tags"))[:_DRAFT_TAG_CAP]
    if not tags:
        tags = _fallback_tags(final_name, final_who, final_what)

    return {
        "name": final_name,
        "who_they_are": final_who,
        "what_they_do": final_what,
        "rules": _draft_rules(raw.get("rules")),
        "purpose_tags": tags,
        # False when who-they-are or what-they-do is the room's stand-in,
        # which is the source handed back, not a persona in the house shape.
        "generated": who is not None and what is not None,
    }


def _draft_rules(value: object) -> list[str]:
    """The model's `rules`, as the form's Rules box takes them: one-line
    strings, list markers off, blanks and repeats dropped, capped."""
    if not isinstance(value, list):
        return []
    rules: list[str] = []
    for item in value:
        if not isinstance(item, str) or "\n" in item.strip():
            continue
        rule = clean_rule(item)
        if rule and rule not in rules:
            rules.append(rule)
    return rules[:_DRAFT_RULES_CAP]


# --- when the source is a document ------------------------------------------
#
# The fallbacks above read `job` as a sentence or two: the name is its first
# two words, the who-line its first clause. A pasted document starts with a
# title or a preamble, so the same reading gives "papi-alert" and "an agent
# whose whole job is # PAPI Alert Triage". When the source looks like a
# document the fallbacks read it as one instead: the name from its first
# heading, the who-line from its first line of prose.

# A source longer than the old one-line job box took, or one with a heading
# in it, is read as a document.
_DOCUMENT_MIN_CHARS = 500

# A markdown ATX heading line ("#" through "######", a space/tab, then the
# heading text), found anywhere in the text (`re.search`, not anchored to
# line one: a short preamble before the first heading is common enough in a
# hand-written document that this shouldn't miss it).
_MARKDOWN_HEADING_RE = re.compile(r"^#{1,6}[ \t]+(.+)$", re.MULTILINE)

# The same shape matched against a single stripped line, and also a heading
# marker with nothing after it (a bare "#"): a heading with nothing to say
# still isn't a line of prose worth using as who_they_are.
_MARKDOWN_HEADING_LINE_RE = re.compile(r"^#{1,6}(?:[ \t]|$)")

# The who-line when a document has no line of prose at all.
_DOCUMENT_WHO_FALLBACK_TEXT = "an agent with one clear job"


def _is_document(job: str) -> bool:
    return len(job) > _DOCUMENT_MIN_CHARS or _MARKDOWN_HEADING_RE.search(job) is not None


def _fallback_document_name(body: str) -> str:
    """The document's first markdown heading, or its first two words when
    it has none, through the same slugify-validate-clamp pipeline as
    `_fallback_name` (`_slugify_name_candidate`)."""
    heading_match = _MARKDOWN_HEADING_RE.search(body)
    if heading_match:
        words = _WORD_RE.findall(heading_match.group(1).lower())[:_FALLBACK_NAME_MAX_WORDS]
        if words:
            return _slugify_name_candidate(words)
    words = _WORD_RE.findall(body.lower())[:_FALLBACK_NAME_MAX_WORDS]
    return _slugify_name_candidate(words)


def _fallback_document_who(body: str) -> str:
    """The first non-blank, non-heading line of the document, cleaned the
    way a typed who-line is and clamped to its bound; the plain fallback
    text when there is no such line."""
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or _MARKDOWN_HEADING_LINE_RE.match(stripped):
            continue
        who = clean_who(stripped[:_WHO_MAX])
        if who:
            return who
    return _DOCUMENT_WHO_FALLBACK_TEXT
